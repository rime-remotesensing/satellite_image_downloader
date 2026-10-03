"""Real-data integration tests for the VIIRS L2 swath path (opt-in, offline).

Run only when VIIRS_L2_RAW_DIR points at the raw archive holding the audited 2024 granules (SR + IMG/MOD
geolocation + legacy GA tiles). A client that fails on any network call proves the raw cache is reused.
"""
from __future__ import annotations

import glob
import json
import os
from datetime import date
from pathlib import Path

import h5py
import numpy as np
import pytest
from pyproj import Transformer

from src import viirs_l2 as v

ROOT = Path(__file__).resolve().parents[1]
RAW = Path(os.environ.get("VIIRS_L2_RAW_DIR", "/nonexistent"))
ASO = json.loads((ROOT / "config" / "no5.geojson").read_text())["features"][0]["geometry"]
NEEDED = {
    "boundary": ("noaa20", date(2024, 2, 21), ["VJ109.A2024052.0512.*", "VJ109.A2024052.0518.*"]),
    "cloudy": ("noaa21", date(2024, 2, 23), ["VJ209.A2024054.0318.*", "VJ209.A2024054.0500.*"]),
    "multi": ("noaa21", date(2024, 3, 10), ["VJ209.A2024070.0318.*", "VJ209.A2024070.0500.*"]),
    "single": ("snpp", date(2024, 3, 10), ["VNP09.A2024070.0348.*"]),
}


def _srs(platform, d, patterns):
    p = v.PLATFORMS[platform]
    out = []
    for pat in patterns:
        hits = sorted(glob.glob(str(RAW / p.sr / f"{d:%Y}" / f"{d.timetuple().tm_yday:03d}" / (pat + ".hdf"))))
        out += hits
    return out


have = all(len(_srs(*NEEDED[k][:2], NEEDED[k][2])) == len(NEEDED[k][2]) for k in NEEDED)
pytestmark = pytest.mark.skipif(not have, reason="VIIRS_L2_RAW_DIR with the audited 2024 granules not available")


class NoNet:
    def fetch(self, *a):
        raise AssertionError("network used although the raw cache is complete")

    def find_by_name(self, *a):
        raise AssertionError("network used although the raw cache is complete")


WINDOW = v.grid_window_for_geometry(ASO)


def _run(case, tmp_path, patterns=None):
    platform, d, pats = NEEDED[case]
    srs = _srs(platform, d, patterns or pats)
    return v.process_platform_day(v.PLATFORMS[platform], d, [(Path(s).name, "", "DAY") for s in srs], RAW, NoNet(), ASO, WINDOW, tmp_path)


def _base(tmp_path, platform):
    return tmp_path / "viirs" / "surface_reflectance" / platform / "l2_swath"


def _read(path):
    import rasterio
    with rasterio.open(path) as r:
        return r.read(), r.read_masks(1) > 0, r.transform, r.crs, r.descriptions, r.tags()


def _alignment(tmp_path, platform, sub):
    i375 = next((_base(tmp_path, platform) / sub / "375m").glob("*_375m.tif"))
    m750 = next((_base(tmp_path, platform) / sub / "750m").glob("*_750m.tif"))
    a3, _, t3, c3, _, _ = _read(i375)
    a7, _, t7, c7, _, _ = _read(m750)
    assert c3.to_epsg() == c7.to_epsg() == 32652
    assert (t3.c, t3.f) == (t7.c, t7.f) == (WINDOW.x0, WINDOW.y1)
    assert t3.a == 375.0 and t7.a == 750.0 and a3.shape[1:] == (2 * a7.shape[1], 2 * a7.shape[2])
    assert t3.c % 750 == 0 and t3.f % 750 == 0


def test_granule_boundary_same_orbit_mosaic(tmp_path):
    s = _run("boundary", tmp_path)
    obs = [o for o in s["overpasses"] if o["aoi_status"] != "AOI_OUTSIDE_SWATH"]
    two = [o for o in obs if len(o["granules"]) == 2]
    assert two, "expected one overpass built from both 0512 and 0518"
    o = two[0]
    prov = next((_base(tmp_path, "noaa20") / "overpass" / "provenance").glob(f"*_o{o['orbit']}_provenance_375m.tif"))
    a, m, *_ = _read(prov)
    used = set(np.unique(a[0][m]).astype(int))
    assert used == {0, 1}                                        # both granules contribute to the AOI window
    assert o["stats"]["375m"]["bowtie_excluded"] > 0 and o["stats"]["375m"]["invalid_geolocation_excluded"] == 0
    # source_distance_m is recorded everywhere; at this swath-edge overpass (sensor zenith ~67 deg) footprints are large
    assert np.all(np.isfinite(a[4][m])) and np.nanmedian(a[4][m]) < 400 and np.nanmax(a[4][m]) < 1000
    _alignment(tmp_path, "noaa20", "overpass")


def test_granule_outside_aoi_is_reported_not_written(tmp_path):
    s = _run("boundary", tmp_path, patterns=["VJ109.A2024052.0512.*"])
    assert [o["aoi_status"] for o in s["overpasses"]] == ["AOI_OUTSIDE_SWATH"]
    assert s["overpasses"][0]["files"] == [] and s["daily"] is None


def test_cloudy_day_keeps_reflectance_and_raw_qa(tmp_path):
    s = _run("cloudy", tmp_path)
    assert len([o for o in s["overpasses"] if o["files"]]) == 2
    qa = next((_base(tmp_path, "noaa21") / "daily" / "qa").glob("*_qa.tif"))
    a, m, *_ , desc, _ = _read(qa)
    cloud = (a[0] >> 2) & 3
    assert np.mean(cloud[m] >= 2) > 0.3                           # genuinely cloudy
    m750 = next((_base(tmp_path, "noaa21") / "daily" / "750m").glob("*_750m.tif"))
    r, mm, *_ = _read(m750)
    assert np.all(np.isfinite(r[:, (cloud >= 2) & mm]))           # cloudy pixels are NOT masked by the downloader
    assert a.dtype == np.uint8 and desc[:7] == tuple(f"QF{i}" for i in range(1, 8))


def test_multiple_overpasses_daily_selection_consistency(tmp_path):
    s = _run("multi", tmp_path)
    orbits = sorted(o["orbit"] for o in s["overpasses"] if o["files"])
    assert orbits == [6892, 6893]
    base = _base(tmp_path, "noaa21") / "daily" / "provenance"
    p3, m3, *_ = _read(next(base.glob("*_provenance_375m.tif")))
    p7, m7, *_ = _read(next(base.glob("*_provenance_750m.tif")))
    sel3, sel7 = p3[5], p7[5]
    parent = np.repeat(np.repeat(sel7, 2, 0), 2, 1)
    assert np.array_equal(sel3[m3], parent[m3])                                  # I and M from the same overpass
    assert set(np.unique(sel7[m7]).astype(int)) <= {6892, 6893}
    # per-cell scan times of the daily 375 m layer equal those of the chosen overpass' own 375 m layer
    for orbit in (6892, 6893):
        op, om, *_ = _read(next((_base(tmp_path, "noaa21") / "overpass" / "provenance").glob(f"*_o{orbit}_provenance_375m.tif")))
        cells = m3 & (sel3 == orbit)
        assert np.array_equal(p3[3][cells], op[3][cells])
    _alignment(tmp_path, "noaa21", "daily")


def _ga_values(platform_ga, d, X, Y):
    """GA 1 km first layer sampled at our 750 m cell centres (comparison only)."""
    lon, lat = Transformer.from_crs("EPSG:32652", "EPSG:4326", always_xy=True).transform(X, Y)
    xs, ys = Transformer.from_crs("EPSG:4326", "+proj=sinu +lon_0=0 +R=6371007.181 +units=m +no_defs", always_xy=True).transform(lon, lat)
    TS = 20015109.354 * 2 / 36
    H = np.floor((xs + 20015109.354) / TS).astype(int)
    V = np.floor((10007554.677 - ys) / TS).astype(int)
    out = {k: np.full(X.shape, np.nan) for k in ("orbit", "szen")}
    for p in glob.glob(str(RAW / platform_ga / f"{d:%Y}" / f"{d.timetuple().tm_yday:03d}" / "*.h5")):
        hh, vv = int(p.split(".h")[1][:2]), int(p.split(".h")[1][3:5])
        m = (H == hh) & (V == vv)
        if not m.any():
            continue
        with h5py.File(p, "r") as f:
            n = int(np.asarray(f.attrs["NumberOfOrbits"]).reshape(-1)[0])
            orbits = np.array([int(np.asarray(f.attrs[f"OrbitNumber.{i + 1}"]).reshape(-1)[0]) for i in range(n)])
            g = f["HDFEOS/GRIDS/VIIRS_Grid_1km_2D/Data Fields"]
            px = TS / 1200
            cc = np.clip(np.floor((xs[m] - (-20015109.354 + hh * TS)) / px).astype(int), 0, 1199)
            rr = np.clip(np.floor(((10007554.677 - vv * TS) - ys[m]) / px).astype(int), 0, 1199)
            op = g["orbit_pnt_1"][()][rr, cc].astype(int)
            out["orbit"][m] = np.where(op >= 0, orbits[np.clip(op, 0, n - 1)], np.nan)
            sz = g["SensorZenith_1"][()][rr, cc].astype(float)
            out["szen"][m] = np.where(sz > -32768, sz * 0.01, np.nan)
    return out


@pytest.mark.parametrize("case,ga", [("multi", "VJ209GA"), ("cloudy", "VJ209GA"), ("single", "VNP09GA"), ("boundary", "VJ109GA")])
def test_legacy_ga_orbit_and_geometry_consistency(tmp_path, case, ga):
    platform, d, _ = NEEDED[case]
    if not glob.glob(str(RAW / ga / f"{d:%Y}" / f"{d.timetuple().tm_yday:03d}" / "*.h5")):
        pytest.skip(f"{ga} {d} not in the raw archive")
    _run(case, tmp_path, patterns=[f"{v.PLATFORMS[platform].sr}.A{d:%Y}{d.timetuple().tm_yday:03d}.*"])   # all daytime granules of the day
    p7, m7, *_ = _read(next((_base(tmp_path, platform) / "daily" / "provenance").glob("*_provenance_750m.tif")))
    g7, _, *_ = _read(next((_base(tmp_path, platform) / "daily" / "geometry").glob("*_geometry_750m.tif")))
    X, Y = WINDOW.centres("750m")
    gav = _ga_values(ga, d, X, Y)
    ok = m7 & np.isfinite(gav["orbit"])
    agree = float(np.mean(p7[5][ok] == gav["orbit"][ok]))
    same = ok & (p7[5] == gav["orbit"])
    assert agree >= 0.85, agree                                         # mostly the same overpass as NASA's L2G
    assert np.nanmedian(np.abs(g7[0][same] - gav["szen"][same])) < 0.5  # and the same view geometry there
