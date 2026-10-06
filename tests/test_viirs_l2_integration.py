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
# The 0512/0518 granule boundary lies ~10-20 km south of the AOI; a wider halo keeps it inside the window on any grid CRS.
WINDOW_WIDE = v.grid_window_for_geometry(ASO, halo_m=20000.0)
# Fixed ground domain for the GA comparison, independent of the product grid: the 750 m cell centres of the Phase 0
# audit window (UTM 52N diagnostic grid) as lon/lat points.
_P0 = v.grid_window_for_geometry(ASO, grid=v.KYUSHU_UTM52N_GRID)
GA_LON, GA_LAT = (np.asarray(a) for a in Transformer.from_crs(_P0.grid.crs, "EPSG:4326", always_xy=True).transform(*_P0.centres("750m")))
_P0_RING = Transformer.from_crs(_P0.grid.crs, "EPSG:4326", always_xy=True).transform(
    [_P0.x0, _P0.x1, _P0.x1, _P0.x0, _P0.x0], [_P0.y0, _P0.y0, _P0.y1, _P0.y1, _P0.y0])
# processing window that contains the whole fixed GA domain on the product grid (snapped outward, no halo)
WINDOW_GA = v.grid_window_for_geometry({"type": "Polygon", "coordinates": [list(zip(*_P0_RING))]}, halo_m=0.0)


def _run(case, tmp_path, patterns=None, window=None):
    platform, d, pats = NEEDED[case]
    srs = _srs(platform, d, patterns or pats)
    return v.process_platform_day(v.PLATFORMS[platform], d, [(Path(s).name, "", "DAY") for s in srs], RAW, NoNet(), ASO,
                                  window or WINDOW, tmp_path)


def _base(tmp_path, platform):
    return tmp_path / "viirs" / "surface_reflectance" / platform / "l2_swath"


def _read(path):
    import rasterio
    with rasterio.open(path) as r:
        return r.read(), r.read_masks(1) > 0, r.transform, r.crs, r.descriptions, r.tags()


def _alignment(tmp_path, platform, sub, window=None):
    """375/750 m products: same CRS (the grid CRS), same anchor-aligned origin, exact 2:1 shapes and cell edges."""
    w = window or WINDOW
    from pyproj import CRS as PCRS
    i375 = next((_base(tmp_path, platform) / sub / "375m").glob("*_375m.tif"))
    m750 = next((_base(tmp_path, platform) / sub / "750m").glob("*_750m.tif"))
    a3, _, t3, c3, _, _ = _read(i375)
    a7, _, t7, c7, _, _ = _read(m750)
    grid = PCRS.from_user_input(w.grid.crs)
    assert PCRS.from_user_input(c3.to_wkt()).equals(grid) and PCRS.from_user_input(c7.to_wkt()).equals(grid)
    assert (t3.c, t3.f) == (t7.c, t7.f) == (w.x0, w.y1)
    assert t3.a == 375.0 and t7.a == 750.0 and t3.e == -375.0 and t7.e == -750.0
    ny7, nx7 = a7.shape[1:]
    assert a3.shape[1:] == (2 * ny7, 2 * nx7)                                   # coarse x 2 == fine
    for edge, origin in ((t7.c, w.grid.origin_e), (t7.f, w.grid.origin_n)):
        k = round((edge - origin) / 750.0)
        assert edge == origin + k * 750.0                                        # anchored to the global grid origin
    j, i = np.arange(nx7 + 1), np.arange(ny7 + 1)
    assert np.array_equal(t7.c + j * t7.a, t3.c + (2 * j) * t3.a)                # every 750 m edge == a 375 m edge (diff 0)
    assert np.array_equal(t7.f + i * t7.e, t3.f + (2 * i) * t3.e)


def test_granule_boundary_same_orbit_mosaic(tmp_path):
    s = _run("boundary", tmp_path, window=WINDOW_WIDE)
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
    _alignment(tmp_path, "noaa20", "overpass", WINDOW_WIDE)


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


def _ga_values(platform_ga, d, lon, lat):
    """GA 1 km first layer sampled at the given lon/lat points (comparison only)."""
    xs, ys = Transformer.from_crs("EPSG:4326", "+proj=sinu +lon_0=0 +R=6371007.181 +units=m +no_defs", always_xy=True).transform(lon, lat)
    TS = 20015109.354 * 2 / 36
    H = np.floor((xs + 20015109.354) / TS).astype(int)
    V = np.floor((10007554.677 - ys) / TS).astype(int)
    out = {k: np.full(lon.shape, np.nan) for k in ("orbit", "szen")}
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
    _run(case, tmp_path, patterns=[f"{v.PLATFORMS[platform].sr}.A{d:%Y}{d.timetuple().tm_yday:03d}.*"],   # all daytime granules
         window=WINDOW_GA)                                                         # contains the fixed GA domain
    _alignment(tmp_path, platform, "daily", WINDOW_GA)                            # exact 375/750 nesting for every case
    for ov375 in (_base(tmp_path, platform) / "overpass" / "375m").glob("*_375m.tif"):
        stem = ov375.name[: -len("_375m.tif")]
        a3, _, t3, *_ = _read(ov375)
        a7, _, t7, *_ = _read(_base(tmp_path, platform) / "overpass" / "750m" / f"{stem}_750m.tif")
        assert (t3.c, t3.f) == (t7.c, t7.f) and a3.shape[1:] == (2 * a7.shape[1], 2 * a7.shape[2])
    P, M, T, C, *_ = _read(next((_base(tmp_path, platform) / "daily" / "provenance").glob("*_provenance_750m.tif")))
    G, *_ = _read(next((_base(tmp_path, platform) / "daily" / "geometry").glob("*_geometry_750m.tif")))
    x, y = (np.asarray(a) for a in Transformer.from_crs("EPSG:4326", C.to_wkt(), always_xy=True).transform(GA_LON, GA_LAT))
    col, row = np.floor((x - T.c) / T.a).astype(int), np.floor((y - T.f) / T.e).astype(int)
    assert row.min() >= 0 and col.min() >= 0 and row.max() < M.shape[0] and col.max() < M.shape[1]   # domain inside window
    sel, szen, m7 = P[5][row, col], G[0][row, col], M[row, col]
    gav = _ga_values(ga, d, GA_LON, GA_LAT)
    ok = m7 & np.isfinite(gav["orbit"])
    agree = float(np.mean(sel[ok] == gav["orbit"][ok]))
    same = ok & (sel == gav["orbit"])
    assert agree >= 0.85, agree                                         # mostly the same overpass as NASA's L2G
    assert np.nanmedian(np.abs(szen[same] - gav["szen"][same])) < 0.5   # and the same view geometry there
