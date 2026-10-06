"""Unit tests for src/viirs_l2.py (no network). Synthetic granules reproduce the audited VNP09/VJ109/VJ209 HDF4
schema and the VNP03/VJ103/VJ203 NetCDF4 geolocation schema with full along-scan widths and one scan of rows."""
from __future__ import annotations

import json
import math
from datetime import date
from pathlib import Path

import h5py
import numpy as np
import pytest
import rasterio

from src import viirs_l2 as v

P = v.PLATFORMS["noaa20"]
AOI = {"type": "Polygon", "coordinates": [[[130.95, 32.98], [131.05, 32.98], [131.05, 33.04], [130.95, 33.04], [130.95, 32.98]]]}


# ----------------------------------------------------------------- synthetic files

def _geo_arrays(rows, cols, lon0, lat0, dlon, dlat):
    r, c = np.meshgrid(np.arange(rows), np.arange(cols), indexing="ij")
    lon = lon0 + (c - cols / 2 + 0.5) * dlon
    lat = lat0 - (r + 0.5) * dlat
    return lon.astype(np.float32), lat.astype(np.float32)


def make_granule(raw: Path, hhmm="0436", orbit=32737, scans=1, lat0=33.06, dn=1000, fill_rows=None, sr_scale=v.SR_SCALE,
                 drop_sds=None, pointer_extra=None, qf=None, szen=3500):
    """Write SR (HDF4) + IMG/MOD (NetCDF4) granules into the raw layout; returns the SR path."""
    from pyhdf.SD import SD, SDC
    m_rows, i_rows = 16 * scans, 32 * scans
    sr_name = f"VJ109.A2024070.{hhmm}.002.2024070113811.hdf"
    img_name = f"VJ103IMG.A2024070.{hhmm}.021.2024070103022.nc"
    mod_name = f"VJ103MOD.A2024070.{hhmm}.021.2024070103022.nc"
    start = f"2024-03-10 {hhmm[:2]}:{hhmm[2:]}:00.000"
    end = f"2024-03-10 {hhmm[:2]}:{int(hhmm[2:]) + 6:02d}:00.000"
    sr_path = v.raw_path(raw, sr_name)
    sr_path.parent.mkdir(parents=True, exist_ok=True)
    sd = SD(str(sr_path), SDC.WRITE | SDC.CREATE)
    ptr = [f"VJ135_L2.A2024070.{hhmm}.002.x.hdf", img_name, mod_name] + (pointer_extra or [])
    for k, val in {"ShortName": "VJ109", "VersionID": "002", "OrbitNumber": orbit, "StartTime": start, "EndTime": end,
                   "InputPointer": ",".join(ptr), "PlatformShortName": "JPSS-1", "DayNightFlag": "Day"}.items():
        setattr(sd, k, val)
    for band in v.I_BANDS + v.M_BANDS:
        name = v.sr_sds_name(band)
        if name == drop_sds:
            continue
        res = "375m" if band.startswith("I") else "750m"
        rows = i_rows if res == "375m" else m_rows
        data = np.full((rows, v.N_SCAN_COLS[res]), dn + (100 if res == "750m" else 0), np.int16)
        data[v.bowtie_zone(rows, res)] = v.SR_FILL
        if fill_rows is not None and res == "375m":
            data[fill_rows] = v.SR_FILL
        ds = sd.create(name, SDC.INT16, data.shape)
        ds[:] = data
        ds.attr("scale_factor").set(SDC.FLOAT64, float(sr_scale))
        ds.attr("add_offset").set(SDC.FLOAT64, 0.0)
        ds.attr("_FillValue").set(SDC.INT16, int(v.SR_FILL))        # same int16 attribute types as the real files
        ds.attr("valid_range").set(SDC.INT16, [-100, 16000])
        ds.endaccess()
    for q in v.QA_SDS:
        ds = sd.create(q, SDC.UINT8, (m_rows, 3200))
        ds[:] = np.full((m_rows, 3200), (qf or {}).get(q, 0), np.uint8)
        ds.endaccess()
    sd.end()
    for name, rows, cols, dl in ((img_name, i_rows, 6400, 0.004), (mod_name, m_rows, 3200, 0.008)):
        p = v.raw_path(raw, name)
        p.parent.mkdir(parents=True, exist_ok=True)
        lon, lat = _geo_arrays(rows, cols, 131.0, lat0, dl, dl)
        with h5py.File(p, "w") as f:
            f.attrs["OrbitNumber"] = np.array([orbit], np.int32)
            f.attrs["StartTime"] = start
            f.attrs["EndTime"] = end
            f.attrs["TAI93_leapseconds"] = np.array([10], np.int32)
            g = f.create_group("geolocation_data")
            g["latitude"], g["longitude"] = lat, lon
            for k in v.GEOMETRY:
                d = g.create_dataset(k, data=np.full((rows, cols), szen if k == "sensor_zenith" else 1000, np.int16))
                d.attrs["scale_factor"] = np.array([0.01], np.float32)
                d.attrs["add_offset"] = np.array([0.0], np.float32)
            g["land_water_mask"] = np.ones((rows, cols), np.uint8)
            g["quality_flag"] = np.zeros((rows, cols), np.uint8)
            f.create_dataset("scan_line_attributes/scan_start_time", data=np.full(scans, 984198970.0 + int(hhmm) , np.float64))
    return sr_path


def paired(raw, **kw):
    srp = make_granule(raw, **kw)
    sr = v.read_sr_header(srp, P)
    img, mod = v.pair_geolocation(sr, P, raw)
    return v.verify_pair(sr, v.raw_path(raw, img), v.raw_path(raw, mod))


class NoNet:
    def fetch(self, *a):
        raise AssertionError("network must not be used when the raw cache is complete")

    def find_by_name(self, *a):
        raise AssertionError("network must not be used when the raw cache is complete")


# ----------------------------------------------------------------- names / mapping

def test_granule_name_parsing():
    g = v.parse_granule_name("VJ109.A2024070.0436.002.2024070113811.hdf")
    assert (g.prefix, g.kind, g.acq_date, g.hhmm, g.collection) == ("VJ1", "09", date(2024, 3, 10), "0436", "002")
    assert g.start.isoformat() == "2024-03-10T04:36:00+00:00"
    assert v.parse_granule_name("VJ203IMG.A2024070.0318.021.2024312094153.nc").product == "VJ203IMG"
    for bad in ("VJ109GA.A2024070.h28v05.002.2024071105736.h5", "VNP09.A2024370.0348.002.2024070133120.hdf", "VNP02IMG.A2024070.0348.002.x.nc"):
        with pytest.raises(ValueError):
            v.parse_granule_name(bad)


def test_platform_product_mapping():
    assert {k: (p.sr, p.img, p.mod) for k, p in v.PLATFORMS.items()} == {
        "snpp": ("VNP09", "VNP03IMG", "VNP03MOD"), "noaa20": ("VJ109", "VJ103IMG", "VJ103MOD"),
        "noaa21": ("VJ209", "VJ203IMG", "VJ203MOD")}


NO5 = json.loads((Path(__file__).resolve().parents[1] / "config" / "no5.geojson").read_text())["features"][0]["geometry"]


def test_default_grid_is_the_modis_sinusoidal_crs():
    from pyproj import CRS as PCRS
    from src import constants
    from src.surface_reflectance import _sinusoidal_crs
    g = v.DEFAULT_ANALYSIS_GRID
    assert g is v.VIIRS_SINUSOIDAL_GRID and g.crs is constants.MODIS_SINUSOIDAL_PROJ4          # one authority, no copy
    assert (g.origin_e, g.origin_n) == (constants.MODIS_SINUSOIDAL_X_MIN, constants.MODIS_SINUSOIDAL_Y_MAX)
    assert g.cell_m == {"375m": 375.0, "750m": 750.0} and g.snap_m == 750.0                     # not MODIS 463/926 m
    ours, modis = PCRS.from_user_input(g.crs), PCRS.from_user_input(_sinusoidal_crs().to_wkt())
    assert ours.equals(modis)
    e = ours.ellipsoid
    assert e.semi_major_metre == e.semi_minor_metre == constants.MODIS_SINUSOIDAL_SPHERE_RADIUS_M == 6371007.181
    assert v.KYUSHU_UTM52N_GRID.crs == "EPSG:32652"                                             # kept for diagnostics


def _edge_index(edge, origin, cell):
    k = round((edge - origin) / cell)
    assert edge == origin + k * cell                       # bit-exact: the edge IS origin + k * cell
    return k


def test_fixed_grid_origin_and_exact_2to1_alignment():
    w = v.grid_window_for_geometry(NO5)
    g = w.grid
    for val, o in ((w.x0, g.origin_e), (w.x1, g.origin_e), (w.y0, g.origin_n), (w.y1, g.origin_n)):
        _edge_index(val, o, 750.0)                         # global anchor, 750 m multiples
        _edge_index(val, o, 375.0)
    (n3y, n3x), (n7y, n7x) = w.shape("375m"), w.shape("750m")
    assert (n3y, n3x) == (2 * n7y, 2 * n7x)
    t3, t7 = w.transform("375m"), w.transform("750m")
    assert (t3.c, t3.f) == (t7.c, t7.f) and t7.a == 2 * t3.a and t7.e == 2 * t3.e
    j, i = np.arange(n7x), np.arange(n7y)
    assert np.array_equal(t7.c + j * t7.a, t3.c + (2 * j) * t3.a) and np.array_equal(t7.c + (j + 1) * t7.a, t3.c + (2 * j + 2) * t3.a)
    assert np.array_equal(t7.f + i * t7.e, t3.f + (2 * i) * t3.e)
    X7, Y7 = w.centres("750m")
    X3, Y3 = w.centres("375m")
    assert np.array_equal(X7, 0.5 * (X3[::2, ::2] + X3[::2, 1::2])) and np.array_equal(Y7, 0.5 * (Y3[::2, ::2] + Y3[1::2, ::2]))


def test_utm_diagnostic_grid_still_reproduces_phase0_window():
    w = v.grid_window_for_geometry(NO5, grid=v.KYUSHU_UTM52N_GRID)
    assert (w.x0, w.y0, w.x1, w.y1) == (669750.0, 3636000.0, 719250.0, 3675750.0)   # Phase 0 window
    assert w.shape("375m") == tuple(2 * n for n in w.shape("750m"))


def test_bowtie_zone_matches_audit_counts():
    zi, zm = v.bowtie_zone(32, "375m"), v.bowtie_zone(16, "750m")
    assert [int(zi[r].sum()) for r in (0, 1, 2, 3, 15, 28, 29, 30, 31)] == [4032, 4032, 2560, 2560, 0, 2560, 2560, 4032, 4032]
    assert [int(zm[r].sum()) for r in (0, 1, 7, 14, 15)] == [2016, 1280, 0, 1280, 2016]


def test_tai93_to_utc():
    assert v.TAI93_UNIX + 984198970.3 - 10 == pytest.approx(1710045360.3)  # 2024-03-10T04:36:00.3Z (audited granule)


# ----------------------------------------------------------------- pairing / schema

def test_pairing_uses_input_pointer_names_and_checks_orbit_time(tmp_path):
    pg = paired(tmp_path)
    assert pg.img.name.startswith("VJ103IMG.A2024070.0436.021") and pg.mod.name.startswith("VJ103MOD.A2024070.0436.021")
    srp = make_granule(tmp_path / "b", pointer_extra=["VJ103IMG.A2024070.0436.021.9999999999999.nc"])
    with pytest.raises(v.VIIRSPairingError):
        v.pair_geolocation(v.read_sr_header(srp, P), P, tmp_path / "b")
    srp = make_granule(tmp_path / "c")
    sr = v.read_sr_header(srp, P)
    img, mod = v.pair_geolocation(sr, P, tmp_path / "c")
    with h5py.File(v.raw_path(tmp_path / "c", img), "a") as f:
        f.attrs["OrbitNumber"] = np.array([1], np.int32)
    with pytest.raises(v.VIIRSPairingError):
        v.verify_pair(sr, v.raw_path(tmp_path / "c", img), v.raw_path(tmp_path / "c", mod))


def test_sr_schema_is_exact(tmp_path):
    with pytest.raises(v.VIIRSSchemaError, match="not found"):
        v.read_sr_header(make_granule(tmp_path / "a", drop_sds="750m Surface Reflectance Band M7"), P)
    with pytest.raises(v.VIIRSSchemaError, match="scale"):
        v.read_sr_header(make_granule(tmp_path / "b", sr_scale=1e-3), P)
    with pytest.raises(v.VIIRSSchemaError, match="not a VNP09"):
        v.read_sr_header(make_granule(tmp_path / "c"), v.PLATFORMS["snpp"])


# ----------------------------------------------------------------- NN mapping

GEOD = None


def _geod():
    global GEOD
    if GEOD is None:
        from pyproj import Geod
        GEOD = Geod(ellps="WGS84")
    return GEOD


def _src(lons, lats, half, szen=None, t=None):
    n = len(lons)
    return v.Sources(np.array(lons, float), np.array(lats, float), np.full(n, half), np.zeros(n, np.int32), np.arange(n, dtype=np.int32),
                     np.zeros(n, np.int32), np.array(t if t is not None else np.zeros(n), float),
                     data={"geom_sensor_zenith": np.array(szen if szen is not None else np.zeros(n), np.int16)})


def _small_window(nx750=2, ny750=1):
    """A few cells of the default Sinusoidal grid near Aso."""
    w = v.grid_window_for_geometry({"type": "Point", "coordinates": [131.0, 33.0]}, halo_m=0.0)
    return v.GridWindow(w.x0, w.y1 - ny750 * 750.0, w.x0 + nx750 * 750.0, w.y1, w.grid)


def _from(lon, lat, az, dist):
    lo, la, _ = _geod().fwd(lon, lat, az, dist)
    return float(lo), float(la)


def test_surface_distance_matches_wgs84_geodesic():
    for az in (0, 37, 90, 145, 260):
        for d in (10.0, 300.0, 1500.0):
            lo, la = _from(131.0, 33.0, az, d)
            assert float(v.surface_distance_m(131.0, 33.0, lo, la)) == pytest.approx(d, abs=1e-3)


def test_nn_picks_nearest_and_respects_footprint_limit():
    w = _small_window(nx750=2, ny750=1)                 # 375 m grid: 2 x 4 cells
    tlon, tlat = v.target_lonlat(w, "375m")
    s = _src(*zip(_from(tlon[0, 0], tlat[0, 0], 90, 50.0), _from(tlon[0, 1], tlat[0, 1], 0, 10.0),
                  _from(tlon[1, 3], tlat[1, 3], 200, 20.0)), half=300.0)
    m = v.nn_map(s, w, "375m")
    assert m.src[0, 0] == 0 and m.src[0, 1] == 1 and m.src[1, 3] == 2           # nearest of several sources
    assert m.dist[0, 0] == pytest.approx(50.0, abs=1e-3)                          # source_distance_m = surface distance
    assert m.src[0, 3] == -1 or m.dist[0, 3] <= 300.0                             # beyond the footprint -> unmapped
    far = v.nn_map(_src(*zip(_from(tlon[0, 0], tlat[0, 0], 90, 400.0)), half=300.0), w, "375m")
    assert far.src[0, 0] == -1 and np.isnan(far.dist[0, 0])


def test_nn_uses_surface_distance_not_sinusoidal_plane_distance():
    """At lon 131 deg the Sinusoidal plane is strongly sheared; plane distances misorder neighbours. The NN must follow
    the WGS84 surface distance."""
    from pyproj import Transformer
    w = _small_window(nx750=1, ny750=1)
    tlon, tlat = v.target_lonlat(w, "375m")
    lon0, lat0 = tlon[0, 0], tlat[0, 0]
    fwd = Transformer.from_crs("EPSG:4326", w.grid.crs, always_xy=True)
    X0, Y0 = fwd.transform(lon0, lat0)
    found = None
    for az_a in range(0, 360, 5):
        for az_b in range(0, 360, 5):
            a, b = _from(lon0, lat0, az_a, 100.0), _from(lon0, lat0, az_b, 110.0)        # a is nearer on the ground
            (xa, ya), (xb, yb) = fwd.transform(*a), fwd.transform(*b)
            if np.hypot(xb - X0, yb - Y0) < np.hypot(xa - X0, ya - Y0) - 5.0:          # but b is nearer on the plane
                found = (a, b)
                break
        if found:
            break
    assert found, "expected Sinusoidal shear to misorder distances at lon 131"
    (a, b) = found
    m = v.nn_map(_src([a[0], b[0]], [a[1], b[1]], half=300.0), w, "375m")
    assert m.src[0, 0] == 0 and m.dist[0, 0] == pytest.approx(100.0, abs=1e-3)


def test_nn_exact_tie_is_deterministic_by_sensor_zenith():
    w = _small_window(nx750=1, ny750=1)
    tlon, tlat = v.target_lonlat(w, "375m")
    e, west = _from(tlon[0, 0], tlat[0, 0], 90, 10.0), _from(tlon[0, 0], tlat[0, 0], 270, 10.0)
    s = _src([west[0], e[0]], [west[1], e[1]], half=400.0, szen=[5000, 3000])
    assert v.nn_map(s, w, "375m").src[0, 0] == 1
    s2 = _src([e[0], west[0]], [e[1], west[1]], half=400.0, szen=[3000, 5000])
    assert v.nn_map(s2, w, "375m").src[0, 0] == 0


# ----------------------------------------------------------------- overpass / daily on synthetic files

def _window():
    return v.grid_window_for_geometry(AOI, halo_m=0.0)


def test_overpass_routing_qa_raw_and_bowtie(tmp_path):
    pg = paired(tmp_path, qf={"QF1 Surface Reflectance": 0b00001100, "QF6 Surface Reflectance": 7})
    ov = v.build_overpass("noaa20", 32737, [pg], _window(), (118.0, 32.0, 144.0, 34.0))   # wide box includes the deletion zones
    L3, L7 = ov.layers["375m"], ov.layers["750m"]
    assert set(v.I_BANDS) <= set(L3) and not set(v.M_BANDS) & set(L3)          # I only on 375 m
    assert set(v.M_BANDS) <= set(L7) and not set(v.I_BANDS) & set(L7)          # M only on 750 m
    assert L3["I1"].shape == tuple(2 * s for s in L7["M1"].shape)
    ok3, ok7 = L3["mapped"], L7["mapped"]
    assert ok3.any() and ok7.any()
    assert np.allclose(L3["I1"][ok3], np.float32(1000 * v.SR_SCALE)) and np.allclose(L7["M5"][ok7], np.float32(1100 * v.SR_SCALE))
    assert L7["QF1"].dtype == np.uint8 and set(np.unique(L7["QF1"][ok7])) == {0b00001100}   # raw, unscaled
    assert set(np.unique(L7["QF6"][ok7])) == {7}
    assert ov.stats["375m"]["bowtie_excluded"] > 0                              # deleted pixels never used as sources
    assert np.all(np.isfinite(L3["I1"][ok3]))                                    # no deleted fill reached the grid
    assert np.all(L3["source_distance_m"][ok3] >= 0) and np.all(np.isfinite(L3["scan_time_unix"][ok3]))
    assert np.all(L7["sensor_zenith"][ok7] == np.float32(35.0))


def test_non_bowtie_fill_stays_fill_not_interpolated(tmp_path):
    pg = paired(tmp_path, fill_rows=slice(10, 20))
    ov = v.build_overpass("noaa20", 32737, [pg], _window(), (130.8, 32.8, 131.2, 33.2))
    L3 = ov.layers["375m"]
    assert np.any(L3["mapped"] & ~np.isfinite(L3["I1"]))                       # mapped but fill -> NaN, not filled in


def test_same_orbit_mosaic_is_order_independent(tmp_path):
    b = paired(tmp_path, hhmm="0436", lat0=33.01 + 32 * 0.004)                   # earlier granule (north part of the window)
    a = paired(tmp_path, hhmm="0442", lat0=33.01)                                # next granule continues along-track (south part)
    box = (130.8, 32.8, 131.2, 33.2)
    o1 = v.build_overpass("noaa20", 32737, [a, b], _window(), box)
    o2 = v.build_overpass("noaa20", 32737, [b, a], _window(), box)
    m1, m2 = o1.layers["375m"], o2.layers["375m"]
    assert np.array_equal(m1["mapped"], m2["mapped"]) and np.array_equal(m1["scan_time_unix"], m2["scan_time_unix"], equal_nan=True)
    assert len(np.unique(m1["scan_time_unix"][m1["mapped"]])) == 2              # both granules contribute
    single = v.build_overpass("noaa20", 32737, [a], _window(), box).layers["375m"]["mapped"]
    assert m1["mapped"].sum() > single.sum()


def test_aoi_outside_swath_and_partial(tmp_path):
    far = paired(tmp_path / "far", lat0=34.5)                                     # swath far north of the AOI
    w = _window()
    aoi = v.aoi_cell_mask(AOI, w, "375m")
    ov = v.build_overpass("noaa20", 1, [far], w, (130.0, 32.0, 132.0, 35.0))
    assert v.aoi_status(ov, aoi) == ("AOI_OUTSIDE_SWATH", 0.0)
    part = paired(tmp_path / "part", lat0=33.0)                                   # one scan (~14 km) covers part of the AOI
    st, frac = v.aoi_status(v.build_overpass("noaa20", 1, [part], w, (130.0, 32.0, 132.0, 35.0)), aoi)
    assert st in ("PARTIAL_OBSERVATION", "OBSERVED") and frac > 0


def test_daily_selection_rule_and_overpass_consistency(tmp_path):
    clear_hi = paired(tmp_path / "a", hhmm="0318", orbit=6892, szen=6200, qf={"QF1 Surface Reflectance": 0})
    clear_lo = paired(tmp_path / "b", hhmm="0500", orbit=6893, szen=5900, qf={"QF1 Surface Reflectance": 0}, dn=2000)
    cloudy_lo = paired(tmp_path / "c", hhmm="0500", orbit=6893, szen=5900, qf={"QF1 Surface Reflectance": 0b00001100}, dn=2000)
    box, w = (130.8, 32.8, 131.2, 33.2), _window()
    A, B, C = (v.build_overpass("noaa21", o, [g], w, box) for o, g in ((6892, clear_hi), (6893, clear_lo), (6893, cloudy_lo)))
    sel = v.daily_selection([A, B])
    ok = sel >= 0
    assert np.all(sel[ok] == 1)                                                  # both clear -> lower sensor zenith wins
    sel2 = v.daily_selection([A, C])
    assert np.all(sel2[sel2 >= 0] == 0)                                          # cloud confidence outranks zenith
    day = v.daily_layers([A, B], sel)
    p3 = np.repeat(np.repeat(day["750m"]["selected_orbit"], 2, 0), 2, 1)
    assert np.array_equal(day["375m"]["selected_orbit"], p3)                     # 2x2 375 m cells use the parent's overpass
    m = day["375m"]["mapped"]
    assert np.allclose(day["375m"]["I1"][m], np.float32(2000 * v.SR_SCALE))       # value taken from that overpass, not averaged


def test_qf6_does_not_affect_daily_selection(tmp_path):
    box, w = (130.8, 32.8, 131.2, 33.2), _window()
    a = v.build_overpass("noaa21", 1, [paired(tmp_path / "a", hhmm="0318", orbit=1, szen=6200)], w, box)
    b = v.build_overpass("noaa21", 2, [paired(tmp_path / "b", hhmm="0500", orbit=2, szen=5900, qf={"QF6 Surface Reflectance": 255})], w, box)
    assert np.all(v.daily_selection([a, b])[v.daily_selection([a, b]) >= 0] == 1)


def test_process_day_outputs_cache_reuse_and_files(tmp_path):
    raw = tmp_path / "raw"
    srp = make_granule(raw)
    s = v.process_platform_day(P, date(2024, 3, 10), [(srp.name, "", "DAY")], raw, NoNet(), AOI, _window(), tmp_path / "out")
    files = s["daily"]["files"] + s["overpasses"][0]["files"]
    assert all(Path(f).exists() for f in files)
    base = tmp_path / "out" / "viirs" / "surface_reflectance" / "noaa20" / "l2_swath"
    i_tif = next((base / "overpass" / "375m").glob("*_375m.tif"))
    m_tif = next((base / "overpass" / "750m").glob("*_750m.tif"))
    with rasterio.open(i_tif) as ri, rasterio.open(m_tif) as rm:
        from pyproj import CRS as PCRS
        sinu = PCRS.from_user_input(v.VIIRS_SINUSOIDAL_GRID.crs)
        assert PCRS.from_user_input(ri.crs.to_wkt()).equals(sinu) and PCRS.from_user_input(rm.crs.to_wkt()).equals(sinu)
        assert ri.descriptions == tuple(v.I_BANDS) and rm.descriptions == tuple(v.M_BANDS)
        assert ri.transform.a == 375.0 and rm.transform.a == 750.0 and (ri.transform.c, ri.transform.f) == (rm.transform.c, rm.transform.f)
    prov = next((base / "daily" / "provenance").glob("*_provenance_375m.tif"))
    with rasterio.open(prov) as rp:
        assert rp.descriptions == ("source_granule", "source_row", "source_col", "scan_time_unix", "source_distance_m", "selected_orbit")
    assert not list(base.rglob("*.part*"))


def test_download_part_and_atomic_rename(tmp_path):
    class Resp:
        def __init__(self, body, length):
            self.body, self.headers = body, {"Content-Length": str(length)}

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def raise_for_status(self):
            pass

        def iter_content(self, n):
            yield self.body

    class Sess:
        def __init__(self, body, length):
            self.body, self.length = body, length

        def get(self, *a, **k):
            return Resp(self.body, self.length)

    good = b"\x0e\x03\x13\x01" + b"x" * 100
    dest = tmp_path / "VJ109.A2024070.0436.002.2024070113811.hdf"
    v._download("u", dest, Sess(good, len(good)))
    assert dest.read_bytes() == good and not dest.with_name(dest.name + ".part").exists()
    bad = tmp_path / "VJ109.A2024070.0442.002.2024070113811.hdf"
    with pytest.raises(v.VIIRSSchemaError):
        v._download("u", bad, Sess(b"<html>", 6))
    assert not bad.exists() and not bad.with_name(bad.name + ".part").exists()


# ----------------------------------------------------------------- pipeline switch

def test_pipeline_viirs_product_switch(tmp_path, monkeypatch):
    from unittest.mock import patch
    from src import pipeline
    cfg = {"geojson": str(Path(__file__).resolve().parents[1] / "config" / "no5.geojson"), "startday": "20240310", "endday": "20240310",
           "satellite": ["viirs"], "output": str(tmp_path), "activefire": "none"}
    with patch.object(pipeline, "_process_viirs_surface_reflectance", return_value={"legacy": 1}) as legacy, \
         patch("src.viirs_l2._process_viirs_l2_swath", return_value={"l2": 1}) as l2:
        r = pipeline.run_pipeline(dict(cfg), tmp_path)
        assert l2.call_count == 1 and legacy.call_count == 0 and r["viirs_l2_swath"] == {"l2": 1}            # default = l2_swath
        r = pipeline.run_pipeline(dict(cfg, surface_reflectance={"viirs_product": "daily_l2g_legacy"}), tmp_path)
        assert legacy.call_count == 1 and r["viirs_surface_reflectance"] == {"legacy": 1}                   # legacy on request
        with pytest.raises(ValueError):
            pipeline.run_pipeline(dict(cfg, surface_reflectance={"viirs_product": "l2g"}), tmp_path)


def test_overpass_with_no_source_near_window_is_outside_swath(tmp_path):
    """Regression: a granule whose swath has no pixel inside the source box must yield AOI_OUTSIDE_SWATH, not crash."""
    far = paired(tmp_path, lat0=34.5)
    w = _window()
    ov = v.build_overpass("noaa20", 1, [far], w, (130.8, 32.8, 131.2, 33.2))   # box excludes the swath entirely
    assert ov.stats["375m"]["sources_in_box"] == 0 and not ov.layers["375m"]["mapped"].any()
    assert v.aoi_status(ov, v.aoi_cell_mask(AOI, w, "375m")) == ("AOI_OUTSIDE_SWATH", 0.0)
