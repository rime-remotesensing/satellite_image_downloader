"""run3.py = VIIRS L2 swath official entry point (no network)."""
from __future__ import annotations

import json
import os
from collections import Counter
from pathlib import Path

import numpy as np
import pytest

import run3
from src import viirs_l2, viirs_plan
from src.regions import REGION_GEOJSONS

REAL_EXISTING = os.environ.get("VIIRS_L2_EXISTING_ROOT")  # e.g. .../output_viirs_l2_swath_selective/{region}/viirs/_raw_l2
RESEARCH_SCENES = Path("/research_scenes")


@pytest.fixture(scope="module")
def tasks():
    return viirs_plan.load_acquisition_manifest()


# --- plan ---------------------------------------------------------------------

def test_python_plan_is_the_141_region_date_authority():
    plan = viirs_plan.RUN3_DOWNLOAD_PLAN
    assert viirs_plan.validate_download_plan() == {"region_date_tasks": 141, "platform_region_date_tasks": 423}
    assert set(plan) == set(REGION_GEOJSONS) and "region02" not in plan and "region10" not in plan
    assert len(viirs_plan.plan_tasks()) == 423 == len(set(viirs_plan.plan_tasks()))


def test_python_plan_equals_the_csv_record():
    """The literal was moved verbatim from config/viirs_required_dates_daily.csv (kept as a record)."""
    csv_plan = viirs_plan.load_required_dates_csv()
    assert csv_plan == {r: p["snpp"] for r, p in viirs_plan.RUN3_DOWNLOAD_PLAN.items()}
    assert [(r, p, d) for r in sorted(csv_plan) for d in csv_plan[r] for p in viirs_plan.PLATFORMS] == viirs_plan.plan_tasks()


def _plan_with(region, platform, dates):
    plan = {r: {p: list(v) for p, v in ps.items()} for r, ps in viirs_plan.RUN3_DOWNLOAD_PLAN.items()}
    plan.setdefault(region, {p: [] for p in viirs_plan.PLATFORMS})[platform] = dates
    return plan


@pytest.mark.parametrize("plan", [
    _plan_with("region02", "snpp", ["2024-02-12"]),                        # forbidden region
    _plan_with("region01", "snpp", ["2024-02-12", "2024-02-12"]),          # duplicate date
    _plan_with("region01", "snpp", ["2024-13-01"]),                        # invalid date
    _plan_with("region01", "noaa21", ["2024-02-12"]),                      # platforms disagree
])
def test_bad_plans_are_rejected(plan):
    with pytest.raises((ValueError, AssertionError)):
        viirs_plan.validate_download_plan(plan)


@pytest.mark.parametrize("rows", ["region,date\nregion02,2024-02-12\n",
                                  "region,date\nregion01,2024-02-12\nregion01,2024-02-12\n",
                                  "region,date\nregion01,2024-13-01\n"])
def test_bad_csv_record_rows_are_rejected(tmp_path, rows):
    p = tmp_path / "plan.csv"
    p.write_text(rows, encoding="utf-8")
    with pytest.raises(ValueError):
        viirs_plan.load_required_dates_csv(p)


def test_frozen_manifest_matches_plan_and_products(tasks):
    assert [(t.region, t.platform, t.date) for t in tasks] == viirs_plan.plan_tasks()
    assert Counter(t.status for t in tasks) == {"GRANULES": 416, "NO_GRANULE": 7}
    assert {(t.platform, t.date) for t in tasks if t.status == "NO_GRANULE"} == {("noaa21", "2024-02-27")}
    for t in tasks:
        p = viirs_l2.PLATFORMS[t.platform]
        for ov in t.overpasses:
            for sr, (img, mod) in zip(ov.granules, ov.geolocation):
                assert sr.startswith(p.sr + ".A") and ".002." in sr and sr.endswith(".hdf")
                assert img.startswith(p.img + ".") and mod.startswith(p.mod + ".")
                assert viirs_l2.parse_granule_name(img).hhmm == viirs_l2.parse_granule_name(sr).hhmm


def test_manifest_rejects_tasks_outside_the_plan(tmp_path):
    data = json.loads(viirs_plan.DEFAULT_MANIFEST_PATH.read_text(encoding="utf-8"))
    data["tasks"][0]["date"] = "2024-01-01"
    p = tmp_path / "m.json"
    p.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ValueError):
        viirs_plan.load_acquisition_manifest(p)


def test_default_halo_is_the_research_13km_for_every_region():
    assert run3.DEFAULT_HALO_M == viirs_plan.DEFAULT_HALO_M == 13_000.0


# --- raw location / no network ---------------------------------------------------

def _touch_task(root: Path, task, skip=()):
    for name in task.raw_files():
        if name not in skip:
            p = viirs_l2.raw_path(root, name)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(b"x")


def test_locate_task_ready_missing_split_and_no_granule(tmp_path, tasks):
    t = next(x for x in tasks if x.status == "GRANULES")
    a, b = tmp_path / "a", tmp_path / "b"
    assert run3.locate_task(t, [a])["status"] == run3.T_MISSING
    first, *rest = t.raw_files()
    _touch_task(a, t, skip={first})
    _touch_task(b, t, skip=set(rest))
    assert run3.locate_task(t, [a, b])["status"] == run3.T_SPLIT
    _touch_task(b, t)
    loc = run3.locate_task(t, [a, b])
    assert loc["status"] == run3.T_READY and loc["raw_dir"] == str(b)
    ng = next(x for x in tasks if x.status == "NO_GRANULE")
    assert run3.locate_task(ng, [b])["status"] == run3.T_NO_GRANULE


def test_offline_client_refuses_network():
    with pytest.raises(RuntimeError):
        run3.OfflineClient().find_by_name("C1", "x")


def test_dry_run_writes_nothing_and_finds_existing_root(tmp_path, tasks, capsys):
    t = next(x for x in tasks if x.region == "region01" and x.status == "GRANULES")
    _touch_task(tmp_path / "old" / "region01" / "raw", t)
    raw = tmp_path / "new_raw"
    before = sorted(tmp_path.rglob("*"))
    assert run3.main(["--raw-root", str(raw), "--existing-root", str(tmp_path / "old" / "{region}" / "raw"),
                      "--regions", "region01", "--platforms", t.platform, "--dates", t.date]) == 0
    assert sorted(tmp_path.rglob("*")) == before and not raw.exists()
    out = capsys.readouterr().out
    assert '"READY": 1' in out and '"network_used": false' in out


def test_raw_root_is_never_defaulted(monkeypatch):
    monkeypatch.delenv(run3.RAW_ROOT_ENV, raising=False)
    with pytest.raises(SystemExit):
        run3.main([])


def test_download_fetches_exact_names_only_for_missing(tmp_path, tasks):
    t = next(x for x in tasks if x.status == "GRANULES")
    first, *rest = t.raw_files()
    _touch_task(tmp_path, t, skip={first})
    calls = []

    class Client:
        def find_by_name(self, concept, name):
            calls.append((concept, name))
            return f"https://example.invalid/{name}"

        def fetch(self, url, dest):
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(b"new")

    assert run3._download_task(t, tmp_path, Client()) == [first]
    assert calls == [(viirs_l2.PLATFORMS[t.platform].sr_concept_id, first)]
    assert all(viirs_l2.raw_path(tmp_path, n).read_bytes() == b"x" for n in rest)  # never re-fetched / overwritten


def test_reproduce_never_overwrites(tmp_path, tasks, monkeypatch):
    t = next(x for x in tasks if x.region == "region01" and x.status == "GRANULES")
    raw = tmp_path / "raw"
    _touch_task(raw, t)
    out = tmp_path / "out"
    summary = run3._summary_path(out, t)
    summary.parent.mkdir(parents=True)
    summary.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(run3, "verify_raw_task", lambda *a: [])
    monkeypatch.setattr(run3, "reproduce_task", lambda *a: pytest.fail("must not regenerate"))
    args = ["--reproduce", "--raw-root", str(raw), "--output-root", str(out), "--regions", t.region,
            "--platforms", t.platform, "--dates", t.date]
    assert run3.main(args) == 0
    assert summary.read_text(encoding="utf-8") == "{}"
    summary.unlink()
    stray = run3._task_dir(out, t) / "375m" / f"X_{t.platform}_{t.date.replace('-', '')}_daily_375m.tif"
    stray.parent.mkdir(parents=True)
    stray.write_bytes(b"keep")
    assert run3.main(args) == 1  # PARTIAL_OUTPUT_PRESENT is reported, nothing is written
    assert stray.read_bytes() == b"keep"


# --- product spec / research comparison --------------------------------------------

def _write_synthetic_product(tmp_path: Path, platform="snpp", granule="VNP09.A2024043.0354.002.2024044075922.hdf"):
    geom = {"type": "Polygon", "coordinates": [[[131.05, 33.16], [131.07, 33.16], [131.07, 33.18], [131.05, 33.18], [131.05, 33.16]]]}
    window = viirs_l2.grid_window_for_geometry(geom, 1000.0)
    rng = np.random.default_rng(0)
    layers = {}
    for res in ("375m", "750m"):
        shape = window.shape(res)
        L = {"mapped": np.ones(shape, bool)}
        for n in (viirs_l2.I_BANDS if res == "375m" else viirs_l2.M_BANDS):
            L[n] = rng.uniform(0, 0.5, shape).astype(np.float32)
        for n in viirs_l2.QA_NAMES + ["geo_land_water_mask"]:
            L[n] = np.ones(shape, np.uint8)
        for n in viirs_l2.GEOMETRY + ["source_granule", "source_row", "source_col", "scan_time_unix", "source_distance_m", "selected_orbit"]:
            L[n] = np.zeros(shape)
        layers[res] = L
    base = tmp_path / "l2_swath"
    tags = {"product": "VNP09", "platform": platform, "source_granules": [granule]}
    viirs_l2.write_product(base / "daily", "VNP09_snpp_20240212_daily", layers, window, tags, extra_prov=True)
    files = {g: base / "daily" / viirs_l2.GROUP_DIR[g] / f"VNP09_snpp_20240212_daily_{g}.tif" for g in run3.EXPECTED_GROUPS}
    return files, layers, window


def test_product_spec_accepts_writer_output_and_flags_problems(tmp_path, tasks):
    t = next(x for x in tasks if x.platform == "snpp" and x.status == "GRANULES")
    files, _, _ = _write_synthetic_product(tmp_path, granule=t.overpasses[0].granules[0])
    assert run3.check_product_spec(files, t) == []
    other = next(x for x in tasks if x.platform == "noaa20" and x.status == "GRANULES")
    assert any("platform tag" in p for p in run3.check_product_spec(files, other))


def test_research_scene_comparison(tmp_path):
    files, layers, window = _write_synthetic_product(tmp_path)
    fine, coarse = layers["375m"], layers["750m"]
    h, w = window.shape("375m")
    rep = lambda a: np.repeat(np.repeat(a, 2, 0), 2, 1)[:h, :w]  # noqa: E731
    values = np.stack([fine["I1"], fine["I2"], rep(coarse["M3"]), rep(coarse["M4"]), rep(coarse["M8"]), fine["I3"],
                       rep(coarse["M11"]), np.zeros((h, w)), np.zeros((h, w))]).astype(np.float32)
    scene = tmp_path / "scene"
    scene.mkdir()
    t = np.asarray(tuple(window.transform("375m"))[:6])
    np.savez_compressed(scene / "scene.npz", values=values, valid=np.ones((h, w), bool), transform=t)
    assert run3.compare_research_scene(files, scene)["identical"]
    values[2, 0, 0] += 0.01
    np.savez_compressed(scene / "scene.npz", values=values, valid=np.ones((h, w), bool), transform=t)
    res = run3.compare_research_scene(files, scene)
    assert not res["identical"] and res["mismatch_values_on_valid"] == 1


# --- real data (only when the existing raw archive is mounted) -----------------------

@pytest.mark.skipif(not REAL_EXISTING, reason="set VIIRS_L2_EXISTING_ROOT to the existing per-region raw archive")
def test_real_task_reproduces_and_matches_research_scene(tmp_path, tasks):
    t = next(x for x in tasks if (x.region, x.platform, x.date) == ("region01", "snpp", "2024-02-12"))
    raw = Path(REAL_EXISTING.replace("{region}", t.region))
    assert run3.verify_raw_task(t, raw) == []
    run3.reproduce_task(t, raw, tmp_path, run3.DEFAULT_HALO_M)
    products = run3._task_products(tmp_path, t)
    assert all(run3.check_product_spec(v, t) == [] for v in products.values())
    scene = RESEARCH_SCENES / "region01_snpp_2024-02-12"
    if scene.is_dir():
        daily = next(v for k, v in products.items() if k.startswith("daily:"))
        assert run3.compare_research_scene(daily, scene)["identical"]


def test_research_tasks_equal_the_csv_record():
    """RUN3_RESEARCH_TASKS was moved verbatim from config/viirs_research_required_tasks.csv (kept as a record)."""
    assert viirs_plan.research_required_tasks() == viirs_plan.load_research_required_csv()


def test_research_required_scope_and_official_layout(tasks, tmp_path):
    req = viirs_plan.research_required_tasks()
    assert len(req) == 344 and {r for r, _, _ in req} == set(REGION_GEOJSONS) - {"region04"}
    by = {(t.region, t.platform, t.date): t for t in tasks}
    assert all(by[k].status == "GRANULES" for k in req)          # every research task has raw granules
    t = by[sorted(req)[0]]
    assert run3._task_dir(tmp_path, t) == tmp_path / t.region / t.date[:4] / "surface_reflectance" / t.platform


# --- external TEST (--scope test) -------------------------------------------------------

def test_test_plan_is_the_three_snpp_cases_and_separate_from_2024():
    assert viirs_plan.validate_test_plan() == {"test_tasks": 6}
    test = viirs_plan.test_plan_tasks()
    assert len(test) == 6 == len(set(test)) and {p for _, p, _ in test} == {"snpp"}
    assert {(r, p, d) for _, r, p, pre, post in viirs_plan.RUN3_TEST_CASES for d in (pre, post)} == set(test)
    assert not set(test) & set(viirs_plan.plan_tasks()) and {d[:4] for _, _, d in test} == {"2023", "2025"}
    assert len(viirs_plan.plan_tasks()) == 423 and len(viirs_plan.research_required_tasks()) == 344   # unchanged
    assert viirs_plan.region_geojson("hitoyoshi") == "config/hitoyoshi.geojson"
    assert viirs_plan.region_geojson("region05") == REGION_GEOJSONS["region05"]
    # each sensor keeps its own observations: only region05 2025-03-22 is shared with the MODIS TEST
    from src import modis_plan
    modis = {(r, d) for r, _, d in modis_plan.plan_acquisitions(scope="test")}
    assert modis & {(r, d.replace("-", "")) for r, _, d in test} == {("region05", "20250322")}


def test_bad_test_plans_are_rejected():
    plan = json.loads(json.dumps(viirs_plan.RUN3_TEST_PLAN))
    plan["hitoyoshi"]["noaa20"] = ["2025-03-07"]                       # the research used SNPP only
    with pytest.raises(ValueError):
        viirs_plan.validate_test_plan(plan)
    plan = json.loads(json.dumps(viirs_plan.RUN3_TEST_PLAN))
    plan["region05"]["snpp"] = ["2023-01-21", "2023-03-08", "2025-02-27", "2025-03-22"]   # not a case date
    with pytest.raises(ValueError):
        viirs_plan.validate_test_plan(plan)


def test_test_manifest_matches_the_test_plan_and_scopes_never_cross():
    tasks = viirs_plan.load_acquisition_manifest(scope="test")
    assert [(t.region, t.platform, t.date) for t in tasks] == viirs_plan.test_plan_tasks()
    for t in tasks:
        assert t.status == "GRANULES"
        for ov in t.overpasses:
            for sr, (img, mod) in zip(ov.granules, ov.geolocation):
                assert sr.startswith("VNP09.A") and ".002." in sr and img.startswith("VNP03IMG.") and mod.startswith("VNP03MOD.")
                n, i = viirs_l2.parse_granule_name(sr), viirs_l2.parse_granule_name(img)
                assert n.acq_date.isoformat() == t.date and i.hhmm == n.hhmm
    with pytest.raises(ValueError):
        viirs_plan.load_acquisition_manifest(viirs_plan.TEST_MANIFEST_PATH)          # not the 2024 plan
    with pytest.raises(ValueError):
        viirs_plan.load_acquisition_manifest(viirs_plan.DEFAULT_MANIFEST_PATH, scope="test")


def test_test_scope_dry_run_and_guards(tmp_path, capsys):
    raw = tmp_path / "raw"
    assert run3.main(["--scope", "test", "--raw-root", str(raw)]) == 0
    out = capsys.readouterr().out
    assert '"tasks_in_scope": 6' in out and '"scope": "test' in out and '"MISSING_RAW": 6' in out and not raw.exists()
    assert run3.main(["--raw-root", str(raw), "--regions", "region01"]) == 0
    assert '"scope": "research_required' in capsys.readouterr().out                    # default unchanged
    for bad in (["--scope", "test", "--all-plan-tasks"], ["--scope", "test", "--regions", "region01"],
                ["--regions", "hitoyoshi"], ["--legacy-l2g-root", "x"]):
        with pytest.raises(SystemExit):
            run3.main(bad + ["--raw-root", str(raw)])


def test_legacy_l2g_is_described_never_pixel_compared(tmp_path):
    t = viirs_plan.load_acquisition_manifest(scope="test")[0]
    files, _, _ = _write_synthetic_product(tmp_path, granule=t.overpasses[0].granules[0])
    d = run3.describe_legacy_l2g(tmp_path / "no_legacy", t, files)
    assert d["comparable"] is False and d["identical"] is None and "L2G" in d["reason"]
    assert d["legacy_500m"] is None and d["new_375m"]["pixel_m"] == [375.0, 375.0]


def test_window_check_flags_a_product_off_the_region_window(tmp_path):
    t = viirs_plan.load_acquisition_manifest(scope="test")[0]
    files, _, _ = _write_synthetic_product(tmp_path, granule=t.overpasses[0].granules[0])   # tiny synthetic AOI
    assert any("halo window" in p for p in run3._window_problems(files, t, run3.DEFAULT_HALO_M))
