from __future__ import annotations

import hashlib
import json
from collections import Counter
from pathlib import Path

import numpy as np
import pytest
import rasterio
from rasterio.crs import CRS
from rasterio.transform import Affine, from_origin

import run2
from src import halo_context, modis_plan
from src import modis_native_window as nw
from src import modis_raw_hdf as rh
from src.constants import MODIS_SINUSOIDAL_PROJ4


@pytest.fixture(scope="module")
def frozen():
    return modis_plan.load_acquisition_manifest()


# --- plan -------------------------------------------------------------------

def test_literal_plan_is_unchanged_and_has_expected_scene_totals():
    canonical_plan = json.dumps(modis_plan.RUN2_DOWNLOAD_PLAN, sort_keys=True, separators=(",", ":"))
    assert hashlib.sha256(canonical_plan.encode()).hexdigest() == "b5b1eec3bce5914a4d527af01e06344fea91e646bee9ecb6ec316ba011ad8fdb"
    assert modis_plan.validate_download_plan() == {"aqua": 111, "terra": 107}
    assert len(modis_plan.plan_acquisitions()) == 218


def test_every_planned_region_has_a_geojson():
    for region, relative_path in modis_plan.REGION_GEOJSONS.items():
        assert region in modis_plan.RUN2_DOWNLOAD_PLAN
        assert (Path.cwd() / relative_path).is_file()


def test_region01_literal_plan_has_31_modis_scenes():
    region01 = modis_plan.RUN2_DOWNLOAD_PLAN["region01"]
    assert (len(region01["aqua"]), len(region01["terra"])) == (16, 15)


# --- frozen manifest ----------------------------------------------------------

def test_frozen_manifest_matches_plan_exactly(frozen):
    acqs, records = frozen
    assert sorted((a.region, a.platform, a.date) for a in acqs) == sorted(modis_plan.plan_acquisitions())
    assert Counter(a.platform for a in acqs) == {"aqua": 111, "terra": 107}
    assert len(records) == 88
    used = {g for a in acqs for g in a.granules}
    assert used == {r.granule_id for r in records}  # no unused or dangling granule


def test_frozen_manifest_granules_are_exact_061_with_exact_urls(frozen):
    acqs, records = frozen
    for r in records:
        assert r.version == "061" and r.product == modis_plan.PRODUCT_BY_PLATFORM[r.platform]
        assert r.https_url.startswith("https://") and r.https_url.endswith("/" + r.granule_id)
    by_id = {r.granule_id: r for r in records}
    for a in acqs:
        for g in a.granules:
            rec = by_id[g]
            assert rec.platform == a.platform and rec.date.replace("-", "") == a.date and a.region in rec.regions


def test_frozen_grids_are_native_sinusoidal_and_aligned(frozen):
    acqs, _ = frozen
    px = halo_context.MODIS_500M_PIXEL_SIZE_M
    for a in acqs:
        (sr_t, sr_s), (qc_t, qc_s), (st_t, st_s) = (a.grids[k] for k in modis_plan.GRID_KEYS)
        assert sr_t == qc_t and sr_s == qc_s
        assert abs(sr_t[0] - px) < 1e-9 and abs(sr_t[4] + px) < 1e-9
        assert abs(st_t[0] - 2 * px) < 1e-9
        for t in (sr_t, st_t):  # whole native pixels from the global origin
            for origin, v, step in ((-20015109.354, t[2], t[0]), (10007554.677, t[5], -t[4])):
                k = (v - origin) / step
                assert abs(k - round(k)) < 1e-4


def test_manifest_rejects_plan_mismatch(tmp_path):
    data = json.loads(modis_plan.DEFAULT_MANIFEST_PATH.read_text(encoding="utf-8"))
    data["acquisitions"] = data["acquisitions"][1:]
    p = tmp_path / "m.json"
    p.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ValueError):
        modis_plan.load_acquisition_manifest(p)


def test_cmr_lookup_requires_one_exact_match():
    g = "MOD09GA.A2024039.h28v05.061.2024041034305.hdf"
    url = f"https://data.lpdaac.earthdatacloud.nasa.gov/lp-prod-protected/MOD09GA.061/{g[:-4]}/{g}"

    class Resp:
        def __init__(self, entries):
            self.entries = entries

        def raise_for_status(self):
            pass

        def json(self):
            return {"feed": {"entry": self.entries}}

    ok = [{"producer_granule_id": g[:-4], "granule_size": "80", "links": [{"href": url}, {"href": url + ".xml"}]}]
    assert modis_plan.cmr_exact_lookup(g, get=lambda *a, **k: Resp(ok))["https_url"] == url
    other = [{"producer_granule_id": g[:-4].replace("2024041034305", "2024041999999"), "links": []}]
    with pytest.raises(RuntimeError):
        modis_plan.cmr_exact_lookup(g, get=lambda *a, **k: Resp(other))


# --- halo helpers kept for run3 / run4 ------------------------------------------

def test_region_halo_overrides_and_native_halo_context():
    assert halo_context.REGION_HALO_BUFFER_M == {"region01": 13_000.0}
    assert halo_context.halo_buffer_m_for_region("region01") == 13_000.0
    assert halo_context.halo_buffer_m_for_region("region07") == 10_000.0
    geometry = halo_context.load_geojson_geometry(Path.cwd() / modis_plan.REGION_GEOJSONS["region07"])
    halo = halo_context.build_halo_context(geometry, 10_000.0)
    o, e = halo["original_native_bounds"], halo["halo_native_bounds"]
    px = halo_context.MODIS_500M_PIXEL_SIZE_M
    assert min((o[0] - e[0]) / px, (o[1] - e[1]) / px, (e[2] - o[2]) / px, (e[3] - o[3]) / px) >= 18


def test_synthetic_geotiff_halo_validation(tmp_path):
    pixel = halo_context.MODIS_500M_PIXEL_SIZE_M
    path = tmp_path / "synthetic_halo.tif"
    with rasterio.open(path, "w", driver="GTiff", height=42, width=42, count=1, dtype="uint8",
                       crs=CRS.from_proj4(MODIS_SINUSOIDAL_PROJ4),
                       transform=from_origin(-20 * pixel, 22 * pixel, pixel, pixel)) as ds:
        ds.write(np.zeros((1, 42, 42), dtype=np.uint8))
    context = halo_context.validate_halo_raster(path, (0.0, 0.0, 2 * pixel, 2 * pixel))
    assert context == pytest.approx({"left": 20, "bottom": 20, "right": 20, "top": 20})


# --- CLI ------------------------------------------------------------------------

def _fake_verify(path, record, check_filename=True):
    ok = Path(path).is_file() and Path(path).read_bytes() == b"valid"
    return {"ok": ok, "path": str(path), "problems": [] if ok else ["fake invalid"], "sha256": "x"}


@pytest.fixture
def fake_verify(monkeypatch):
    real = rh.plan_recovery
    monkeypatch.setattr(rh, "plan_recovery", lambda *a, **k: real(*a, **{**k, "verify": _fake_verify}))


def _summary(out: str):
    return json.loads(out[: out.index("\n}\n") + 2])


def test_dry_run_classifies_everything_and_creates_nothing(tmp_path, capsys, fake_verify, frozen):
    acqs, records = frozen
    raw = tmp_path / "raw"
    elsewhere = tmp_path / "old_cache"
    elsewhere.mkdir()
    region07 = [a for a in acqs if a.region == "region07"]
    for g in {g for a in region07 for g in a.granules}:
        (elsewhere / g).write_bytes(b"valid")
    assert run2.main(["--raw-root", str(raw), "--existing-root", str(elsewhere)]) == 0
    assert not raw.exists()
    s = _summary(capsys.readouterr().out)
    assert s["mode"] == "dry_run" and s["network_used"] is False
    assert s["plan_acquisitions"] == 218 and s["raw_hdf"]["total"] == 88
    ready = s["acquisition_status_counts"]["READY"]
    assert ready >= len(region07)
    assert s["acquisition_status_counts"]["BLOCKED_MISSING_HDF"] == 218 - ready
    assert len(s["missing_hdf"]) == s["raw_hdf"]["counts"]["MISSING"]


def test_raw_root_is_required_and_never_defaulted(monkeypatch):
    monkeypatch.delenv(run2.RAW_ROOT_ENV, raising=False)
    with pytest.raises(SystemExit):
        run2.main([])


def test_output_must_be_separate_from_reference(tmp_path):
    ref = tmp_path / "ref"
    ref.mkdir()
    with pytest.raises(SystemExit):
        run2.main(["--verify", "--output-root", str(ref / "sub"), "--reference-root", str(ref)])


def test_download_fetches_only_missing_and_never_touches_present(tmp_path, capsys, monkeypatch, fake_verify, frozen):
    acqs, records = frozen
    raw = tmp_path / "raw"
    region01 = sorted({g for a in acqs if a.region == "region01" for g in a.granules})
    present = raw / rh.DEFAULT_RECOVERED_SUBDIR / region01[0]
    present.parent.mkdir(parents=True)
    present.write_bytes(b"valid")
    fetched = []

    def fake_download(statuses, recs, **kw):
        for s in statuses:
            if s.status == rh.STATUS_MISSING:
                fetched.append(s.granule_id)
                s.status = rh.STATUS_DOWNLOAD_FAILED  # simulate: no network in tests
        return statuses

    monkeypatch.setattr(rh, "download_missing", fake_download)
    assert run2.main(["--download", "--raw-root", str(raw), "--regions", "region01"]) == 1
    assert present.read_bytes() == b"valid"
    assert set(fetched) == set(region01[1:])
    reports = list((raw / rh.DEFAULT_RECOVERED_SUBDIR / run2.REPORT_DIR).glob("run2_download_*.json"))
    assert len(reports) == 1


def test_research_manifest_must_agree_with_frozen_records(tmp_path, frozen):
    import csv

    _, records = frozen
    r = records[0]
    row = {"granule_id": r.granule_id, "regions": ";".join(r.regions), "date": r.date, "platform": r.platform,
           "product": r.product, "version": r.version, "tile": r.tile, "https_url": r.https_url}
    good = tmp_path / "good.csv"
    with good.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=list(row))
        w.writeheader()
        w.writerow(row)
    assert run2._check_research_manifest(good, records) == [r.granule_id]
    bad = tmp_path / "bad.csv"
    with bad.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=list(row))
        w.writeheader()
        w.writerow({**row, "regions": "region99"})
    with pytest.raises(SystemExit):
        run2._check_research_manifest(bad, records)


def _write_fake_outputs(tmp_path, monkeypatch):
    from tests.test_modis_native_window import TILE, _fake_reader, _window_transform

    rng = np.random.default_rng(1)
    arrays = {f"sur_refl_b0{i}": rng.integers(0, 9000, (40, 40)).astype(np.int16) for i in range(1, 8)}
    arrays["QC_500m"] = rng.integers(0, 2**31, (40, 40)).astype(np.uint32)
    arrays["state_1km"] = rng.integers(0, 2**15, (20, 20)).astype(np.uint16)
    monkeypatch.setattr(nw, "_read_sds", _fake_reader(arrays))
    granule = f"MOD09GA.A2024039.{TILE}.061.2024041034305.hdf"
    grids = {"sr": (_window_transform(40, 4, 2), (6, 8)), "qc_500m": (_window_transform(40, 4, 2), (6, 8)),
             "state_1km": (_window_transform(20, 2, 1), (3, 4))}
    acq = nw.Acquisition("region01", "terra", "20240208")
    paths = nw.generate_acquisition(acq, [tmp_path / granule], grids, tmp_path / "out")
    spec = modis_plan.AcquisitionSpec("region01", "terra", "20240208", (granule,),
                                      {k: (tuple(t)[:6], s) for k, (t, s) in grids.items()})
    return spec, {k: Path(v) for k, v in paths.items()}


def test_output_spec_check_accepts_generated_and_rejects_wrong_grid(tmp_path, monkeypatch):
    spec, paths = _write_fake_outputs(tmp_path, monkeypatch)
    assert run2.check_output_spec(spec, paths) == []
    t, s = spec.grids["sr"]
    shifted = modis_plan.AcquisitionSpec(spec.region, spec.platform, spec.date, spec.granules,
                                         {**spec.grids, "sr": ((t[0], 0, t[2] + t[0], 0, t[4], t[5]), s)})
    assert any("transform" in p for p in run2.check_output_spec(shifted, paths))
    other = modis_plan.AcquisitionSpec(spec.region, spec.platform, spec.date,
                                       ("MOD09GA.A2024039.h28v05.061.2099999999999.hdf",), spec.grids)
    assert any("source_granules" in p for p in run2.check_output_spec(other, paths))


def test_reproduce_skips_existing_outputs_without_overwriting(tmp_path, monkeypatch, fake_verify, frozen, capsys):
    acqs, _ = frozen
    a = next(x for x in acqs if x.region == "region01")
    raw = tmp_path / "raw"
    for g in a.granules:
        p = raw / rh.DEFAULT_RECOVERED_SUBDIR / g
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"valid")
    out = tmp_path / "out"
    existing = nw.output_paths(out, nw.Acquisition(a.region, a.platform, a.date))
    for p in existing.values():
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"do not touch")
    monkeypatch.setattr(nw, "generate_acquisition", lambda *x, **k: pytest.fail("must not regenerate"))
    monkeypatch.setattr(run2, "_sha256", lambda p: "x")
    assert run2.main(["--reproduce", "--raw-root", str(raw), "--output-root", str(out), "--regions", "region01"]) == 0
    assert all(p.read_bytes() == b"do not touch" for p in existing.values())
    report = json.loads(next((out / run2.REPORT_DIR).glob("run2_reproduce_*.json")).read_text(encoding="utf-8"))
    row = next(r for r in report["acquisitions"] if (r["platform"], r["date"]) == (a.platform, a.date))
    assert row["status"] == run2.ACQ_ALREADY
    assert [s["granule_id"] for s in row["source_granules"]] == list(a.granules)


def test_outputs_use_the_official_surface_reflectance_layout():
    """Official images live at Aso/MODIS/<region>/2024/surface_reflectance/<platform>/{500m,qa}/ (not *_halo)."""
    paths = nw.output_paths(Path("ROOT"), nw.Acquisition("region05", "aqua", "20240310"))
    assert paths["sr"] == Path("ROOT/region05/2024/surface_reflectance/aqua/500m/MYD09GA_aqua_20240310_500m.tif")
    assert paths["qc_500m"] == Path("ROOT/region05/2024/surface_reflectance/aqua/qa/MYD09GA_aqua_20240310_QC_500m.tif")
    assert paths["state_1km"] == Path("ROOT/region05/2024/surface_reflectance/aqua/qa/MYD09GA_aqua_20240310_state_1km.tif")
    assert all("surface_reflectance_halo" not in str(p) for p in paths.values())


# --- full-scene mode -------------------------------------------------------------

def test_full_scene_windows_contain_aoi_halo_and_patch_support_for_all_218(frozen):
    from src import modis_full_scene as fs

    acqs, _ = frozen
    for a in acqs:
        spec, rec = fs.full_scene_spec(a)
        img = fs.window_from_grid(spec.grids["sr"][0], spec.grids["sr"][1], "500m")
        aoi = tuple(rec["aoi_window"]["global_pixel_window"].values())
        assert fs.contains(img, aoi) and rec["halo_achieved_px_min"] >= halo_context.HALO_PIXELS
        assert rec["patch_support_window"]["contained"]                                     # official image lies inside
        assert rec["halo_m_requested"] == (13_000.0 if a.region == "region01" else 10_000.0)
        assert spec.grids["sr"] == spec.grids["qc_500m"] and spec.granules == a.granules   # same granules, same grid
        st = fs.window_from_grid(spec.grids["state_1km"][0], spec.grids["state_1km"][1], "1km")
        assert st[0] * 2 <= img[0] and st[2] * 2 <= img[2] and st[1] * 2 >= img[1] and st[3] * 2 >= img[3]
        t = spec.grids["sr"][0]                                                              # on the native lattice
        assert abs(t[0] - halo_context.MODIS_500M_PIXEL_SIZE_M) < 1e-9
        assert fs.window_from_grid(t, spec.grids["sr"][1], "500m") == img


def test_full_scene_halo_override_and_cli_guards(frozen):
    from src import modis_full_scene as fs

    a = frozen[0][0]
    assert fs.full_scene_spec(a, 20_000.0)[1]["halo_achieved_px_min"] > fs.full_scene_spec(a)[1]["halo_achieved_px_min"]
    with pytest.raises(SystemExit):
        run2.main(["--legacy-patch-extent", "--halo-m", "5000", "--raw-root", "x"])   # halo is not a legacy option
    with pytest.raises(SystemExit):
        run2.main(["--legacy-patch-extent", "--verify", "--output-root", "o", "--legacy-reference-root", "r"])
    with pytest.raises(SystemExit):
        run2.main(["--full-scene", "--raw-root", "x"])                                # the old flag is gone


# --- official extent = frozen AOI + halo manifest --------------------------------------

def test_frozen_official_manifest_is_the_adopted_one_and_equals_the_window_rule(frozen):
    from src import modis_full_scene as fs

    acqs, _ = frozen
    assert fs.file_sha256(fs.FROZEN_MANIFEST_PATH) == fs.FROZEN_MANIFEST_SHA256
    specs, records = fs.load_frozen_full_scene(acqs)
    assert len(specs) == 218
    by_region = {}
    for a, spec, rec in zip(acqs, specs, records):
        computed, crec = fs.full_scene_spec(a)
        assert spec.grids == computed.grids and rec == json.loads(json.dumps(crec))   # same rule, nothing drifted
        assert spec.granules == a.granules
        by_region.setdefault(a.region, set()).add(json.dumps(rec["grids"], sort_keys=True))
    assert all(len(g) == 1 for g in by_region.values())                                # fixed grid per region


def test_default_extent_is_official_full_scene_and_legacy_is_explicit(tmp_path, capsys, fake_verify):
    raw = tmp_path / "raw"
    assert run2.main(["--raw-root", str(raw), "--regions", "region01"]) == 0
    assert '"extent": "full_scene_aoi_halo"' in capsys.readouterr().out
    assert run2.main(["--raw-root", str(raw), "--regions", "region01", "--legacy-patch-extent"]) == 0
    assert '"extent": "legacy_training_patch_support"' in capsys.readouterr().out


def test_frozen_manifest_must_match_the_plan(tmp_path, frozen):
    from src import modis_full_scene as fs

    data = json.loads(fs.FROZEN_MANIFEST_PATH.read_text(encoding="utf-8"))
    data["acquisitions"] = data["acquisitions"][1:]
    p = tmp_path / "m.json"
    p.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ValueError):
        fs.load_frozen_full_scene(frozen[0], p)


def test_full_scene_manifest_is_never_rewritten_with_other_windows(tmp_path, frozen):
    from src import modis_full_scene as fs

    recs = [fs.full_scene_spec(a)[1] for a in frozen[0][:2]]
    p = fs.write_or_check_manifest(tmp_path, recs)
    before = p.read_bytes()
    assert fs.write_or_check_manifest(tmp_path, recs) == p and p.read_bytes() == before
    with pytest.raises(ValueError):
        fs.write_or_check_manifest(tmp_path, [fs.full_scene_spec(a, 20_000.0)[1] for a in frozen[0][:2]])
    assert p.read_bytes() == before
