"""run4.py = GCOM-C/SGLI L2 LAND RSRF official entry point (no network; synthetic v3002 HDF5 on a shrunken grid)."""
from __future__ import annotations

import json
import os
from datetime import date
from pathlib import Path

import numpy as np
import pytest
import rasterio

import run4
from src import gcomc
from tests.test_gcomc import N1K, N250, _record, make_rsrf

D409, D410, D315 = date(2024, 4, 9), date(2024, 4, 10), date(2024, 3, 15)


@pytest.fixture(autouse=True)
def small_grid(monkeypatch):
    monkeypatch.setitem(gcomc.GCOMC_TILE_PIXELS, "250m", N250)
    monkeypatch.setitem(gcomc.GCOMC_TILE_PIXELS, "1km", N1K)


def _raw(raw: Path, obs: date, tiles=((5, 28), (5, 29)), **kw):
    for t in tiles:
        g = run4.granule_for(obs, t)
        make_rsrf(gcomc.local_raw_path(raw, g), g.identifier, **kw)


def test_product_identity_and_tiles_keep_t0529():
    g = run4.granule_for(D409, (5, 29))
    assert g.filename == "GC1SG1_20240409D01D_T0529_L2SG_RSRFQ_3002.h5" and g.orbit_direction == "D" and g.version == "3002"
    tiles = {r: run4.region_tiles(r, run4.DEFAULT_HALO_M) for r in run4.REGION_GEOJSONS}
    assert set().union(*tiles.values()) == {(5, 28), (5, 29)}
    assert (5, 29) in tiles["region09"]


def test_period_is_every_day_not_reflectance_dates():
    days = run4._dates(run4.PERIOD_START, run4.PERIOD_END)
    assert len(days) == 51 and days[0] == date(2024, 2, 20) and days[-1] == D410


def test_dry_run_reports_presence_and_writes_nothing(tmp_path, capsys):
    raw = tmp_path / "raw"
    _raw(raw, D409, tiles=((5, 28),))
    before = sorted(raw.rglob("*"))
    assert run4.main(["--raw-root", str(raw), "--start", "2024-04-09", "--end", "2024-04-09", "--verify-raw"]) == 0
    assert sorted(raw.rglob("*")) == before
    out = capsys.readouterr().out
    assert '"PRESENT_VALID": 1' in out and '"RAW_MISSING": 1' in out and '"network_used": false' in out


def test_reproduce_distinguishes_observed_no_data_and_missing_then_verifies(tmp_path, capsys):
    raw, out = tmp_path / "raw", tmp_path / "out"
    _raw(raw, D409)
    _raw(raw, D315, dn250=None)
    assert run4.main(["--reproduce", "--raw-root", str(raw), "--output-root", str(out), "--regions", "region01",
                      "--start", "2024-04-09", "--end", "2024-04-10"]) == 0
    rep = json.loads(next((out / run4.REPORT_DIR).glob("run4_reproduce_*.json")).read_text(encoding="utf-8"))
    by_date = {r["date"]: r for r in rep["rows"]}
    assert by_date["2024-04-09"]["status"] in (gcomc.AOI_OBSERVED, gcomc.AOI_PARTIAL)
    assert by_date["2024-04-09"]["observation"]["label"] == "USABLE"
    assert by_date["2024-04-10"]["status"] == run4.D_RAW_MISSING            # not downloaded != no observation
    assert run4.main(["--reproduce", "--raw-root", str(raw), "--output-root", str(out), "--regions", "region01",
                      "--start", "2024-03-15", "--end", "2024-03-15"]) == 0
    d = run4.out_dir(out, "region01", D409)
    nd = json.loads(gcomc.summary_path(d, D315).read_text(encoding="utf-8"))
    assert nd["aoi_status"] == run4.D_NO_DATA and nd["files"] == [] and nd["observation"]["label"] == "NO_OBSERVATION"

    f250, f1k = gcomc.reflectance_output_path(d, "250m", D409), gcomc.reflectance_output_path(d, "1km", D409)
    with rasterio.open(f250) as a, rasterio.open(f1k) as b:
        assert a.count == 12 and b.count == 3 and a.descriptions[-1] == "SW03" and b.descriptions == ("SW01", "SW02", "SW04")
        assert b.tags(2)["reflectance_type"] == "TOA reflectance" and b.tags(1)["reflectance_type"] == "surface reflectance"
        assert abs(b.transform.a / a.transform.a - 4) < 1e-12                 # 1 km is not upsampled
    s = json.loads(gcomc.summary_path(d, D409).read_text(encoding="utf-8"))
    assert s["spatial_export"]["aoi_polygon_mask_applied"] is False
    assert all(len(p["sha256"]) == 64 for p in s["product_status"].values())  # output <-> raw HDF5 correspondence

    assert run4.main(["--verify", "--output-root", str(out), "--raw-root", str(raw), "--regions", "region01",
                      "--start", "2024-03-15", "--end", "2024-04-10"]) == 0
    v = json.loads(sorted((out / run4.REPORT_DIR).glob("run4_verify_*.json"))[-1].read_text(encoding="utf-8"))
    assert v["status_counts"] == {"SPEC_OK": 2, "OUTPUT_MISSING": 25}

    mtime = f250.stat().st_mtime_ns
    assert run4.main(["--reproduce", "--raw-root", str(raw), "--output-root", str(out), "--regions", "region01",
                      "--start", "2024-04-09", "--end", "2024-04-09"]) == 0
    assert f250.stat().st_mtime_ns == mtime                                     # ALREADY_PRESENT, never overwritten


def test_verify_detects_a_wrong_reflectance_type(tmp_path):
    raw, out = tmp_path / "raw", tmp_path / "out"
    _raw(raw, D409)
    run4.reproduce_region_day("region01", D409, raw, out, run4.DEFAULT_HALO_M)
    f1k = gcomc.reflectance_output_path(run4.out_dir(out, "region01", D409), "1km", D409)
    with rasterio.open(f1k, "r+") as ds:
        ds.update_tags(2, reflectance_type="surface reflectance")
    assert any("SW02" in p for p in run4.check_region_day(out, "region01", D409, raw))


def test_invalid_raw_is_reported_and_left_untouched(tmp_path):
    raw, out = tmp_path / "raw", tmp_path / "out"
    _raw(raw, D409, tiles=((5, 29),))
    bad = gcomc.local_raw_path(raw, run4.granule_for(D409, (5, 28)))
    bad.write_bytes(b"not hdf5")
    row = run4.reproduce_region_day("region05", D409, raw, out, run4.DEFAULT_HALO_M)
    assert row["status"] == run4.D_RAW_INVALID and bad.read_bytes() == b"not hdf5" and not out.exists()


def test_partial_output_is_not_overwritten(tmp_path):
    raw, out = tmp_path / "raw", tmp_path / "out"
    _raw(raw, D409)
    stray = gcomc.reflectance_output_path(run4.out_dir(out, "region01", D409), "250m", D409)
    stray.parent.mkdir(parents=True)
    stray.write_bytes(b"keep")
    assert run4.reproduce_region_day("region01", D409, raw, out, run4.DEFAULT_HALO_M)["status"] == run4.D_PARTIAL_OUT
    assert stray.read_bytes() == b"keep"


def test_download_reuses_valid_local_files_and_reports_no_record(tmp_path, monkeypatch, capsys):
    raw = tmp_path / "raw"
    _raw(raw, D409)
    recs = [_record(run4.granule_for(D409, t).identifier) for t in ((5, 28), (5, 29))]
    monkeypatch.setattr(gcomc, "query_csw_records", lambda s, e, v, h: [r for r in recs if (r.granule.tile_v, r.granule.tile_h) == (v, h)])
    monkeypatch.setattr(gcomc, "GPortalSFTP", lambda *a, **k: pytest.fail("valid local files must not trigger SFTP"))
    assert run4.main(["--download", "--raw-root", str(raw), "--start", "2024-04-09", "--end", "2024-04-10"]) == 0
    rep = json.loads(next((raw / run4.REPORT_DIR).glob("run4_download_*.json")).read_text(encoding="utf-8"))
    st = {(r["date"], r["tile"]): r for r in rep["rows"]}
    assert st[("2024-04-09", "T0528")]["status"] == "AVAILABLE" and st[("2024-04-09", "T0528")]["source"] == "local_cache"
    assert st[("2024-04-10", "T0529")]["status"] == run4.D_NO_RECORD


def test_output_root_must_be_separate_from_raw(tmp_path):
    with pytest.raises(SystemExit):
        run4.main(["--reproduce", "--raw-root", str(tmp_path), "--output-root", str(tmp_path / "out")])


def test_official_layout_is_region_year_surface_reflectance(tmp_path):
    d = run4.out_dir(tmp_path, "region05", D409)
    assert d == tmp_path / "region05" / "2024" / "surface_reflectance"
    assert gcomc.reflectance_output_path(d, "250m", D409).relative_to(tmp_path).as_posix() ==         "region05/2024/surface_reflectance/250m/GCOMC_SGLI_RSRF_20240409_D_250m.tif"
    assert gcomc.reflectance_output_path(d, "1km", D409).parent.name == "1km"   # 1 km kept separate, never resampled


def test_download_without_csw_uses_exact_sftp_paths_and_reuses_local(tmp_path, monkeypatch):
    raw = tmp_path / "raw"
    _raw(raw, D409)
    monkeypatch.setattr(gcomc, "query_csw_records", lambda *a, **k: pytest.fail("CSW must not be queried"))
    monkeypatch.setattr(gcomc, "GPortalSFTP", lambda *a, **k: pytest.fail("valid local files must not trigger SFTP"))
    assert run4.main(["--download", "--sftp-without-csw", "--raw-root", str(raw), "--start", "2024-04-09", "--end", "2024-04-09"]) == 0
    rep = json.loads(next((raw / run4.REPORT_DIR).glob("run4_download_*.json")).read_text(encoding="utf-8"))
    assert {r["status"] for r in rep["rows"]} == {"AVAILABLE"} and "CSW not used" in rep["catalog"]
    assert all(r["sftp_path"].startswith("/product/Standard/GCOM-C/GCOM-C.SGLI/L2.LAND.RSRF/3/2024/04/09/") for r in rep["rows"])
    with pytest.raises(SystemExit):
        run4.main(["--sftp-without-csw", "--raw-root", str(raw)])
