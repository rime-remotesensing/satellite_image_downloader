"""Unit tests for src/gcomc.py (no network).

Synthetic RSRF HDF5 files reproduce the audited schema of real version-3002
files on a shrunken grid (48 / 12 px per tile instead of 4800 / 1200).
"""
from __future__ import annotations

import errno
import json
import logging
import math
from datetime import date
from pathlib import Path
from typing import Dict, Optional, Tuple

import h5py
import numpy as np
import pytest
import rasterio

from src import gcomc
from src.config import _normalize_satellites

FIXTURES = Path(__file__).parent / "fixtures" / "gcomc"
N250, N1K = 48, 12
ASO_NO5 = json.loads((Path(__file__).resolve().parents[1] / "config" / "no5.geojson").read_text())["features"][0]["geometry"]
# Synthetic AOI crossing the T0528/T0529 boundary (lon*cos(lat) = 110 deg -> ~131.16E at 33N).
CROSS_AOI = {"type": "Polygon", "coordinates": [[[130.2, 32.4], [132.2, 32.4], [132.2, 33.6], [130.2, 33.6], [130.2, 32.4]]]}
TODAY = date(2026, 10, 3)


@pytest.fixture(autouse=True)
def small_grid(monkeypatch):
    monkeypatch.setitem(gcomc.GCOMC_TILE_PIXELS, "250m", N250)
    monkeypatch.setitem(gcomc.GCOMC_TILE_PIXELS, "1km", N1K)


def _s(text: str) -> np.ndarray:
    return np.array([text.encode()])


def make_rsrf(path: Path, ident: str, *, dn250: Optional[int] = 2000, qa: Optional[np.ndarray] = None,
              sw02_desc: str = "TOA reflectance of SW02", version: str = "3002", drop: str = "",
              max_valid: int = 65534) -> Path:
    """Write a synthetic RSRF file. dn250=None -> all Error_DN (no observation)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with h5py.File(path, "w") as f:
        ga = f.create_group("Global_attributes")
        for k, v in {"Product_version": version, "Product_file_name": ident + ".h5",
                     "Algorithm_version": "3.02", "Parameter_version": "002.08",
                     "Image_start_time": "20240409 01:00:00.000", "Image_end_time": "20240409 02:00:00.000"}.items():
            ga.attrs[k] = _s(v)
        f.create_group("Image_data")
        f.create_group("Geometry_data")
        for spec in gcomc.GCOMC_FIELDS:
            if spec.hdf5_path == drop:
                continue
            n = N250 if spec.grid == "250m" else N1K
            if spec.kind == "reflectance":
                base = 65535 if dn250 is None else (dn250 + (1000 if spec.grid == "1km" else 0))
                data = np.full((n, n), base, np.uint16)
                if dn250 is not None:
                    data[0, 0] = 65535  # one Error_DN pixel
                attrs = {"Slope": np.array([9.9999997e-05], np.float32), "Offset": np.array([0.0], np.float32),
                         "Error_DN": np.array([65535], np.int32), "Minimum_valid_DN": np.array([0], np.int32),
                         "Maximum_valid_DN": np.array([max_valid], np.int32),
                         "Data_description": _s(sw02_desc if spec.name == "SW02" else spec.description),
                         "Spatial_resolution": np.array([10.0 / n], np.float32), "Center_wavelength": np.array([500.0], np.float32)}
            elif spec.name == "QA_flag":
                data = qa if qa is not None else np.full((n, n), 1 if dn250 is None else 2, np.uint16)
                attrs = {"Slope": np.array([1.0], np.float32), "Offset": np.array([0.0], np.float32),
                         "Error_DN": np.array([1], np.int32), "Minimum_valid_DN": np.array([0], np.int32),
                         "Maximum_valid_DN": np.array([65534], np.int32),
                         "Data_description": np.array([x.encode() for x in gcomc.GCOMC_QA_FLAG_DESCRIPTION]),
                         "Spatial_resolution": np.array([10.0 / n], np.float32)}
            elif spec.name == "Land_water_flag":
                data = np.full((n, n), 255 if dn250 is None else 100, np.uint8)
                attrs = {"Slope": np.array([1.0], np.float32), "Offset": np.array([0.0], np.float32),
                         "Error_DN": np.array([255], np.int32), "Minimum_valid_DN": np.array([0], np.int32),
                         "Maximum_valid_DN": np.array([254], np.int32), "Data_description": _s(spec.description),
                         "Spatial_resolution": np.array([10.0 / n], np.float32)}
            else:  # Obs_time
                data = np.full((n, n), -32768 if dn250 is None else 1670, np.int16)
                attrs = {"Slope": np.array([0.001], np.float32), "Offset": np.array([0.0], np.float32),
                         "Error_DN": np.array([-32768], np.int32), "Minimum_valid_DN": np.array([-32767], np.int32),
                         "Maximum_valid_DN": np.array([32767], np.int32), "Data_description": _s(spec.description)}
            ds = f.create_dataset(spec.hdf5_path, data=data)
            for k, v in attrs.items():
                ds.attrs[k] = v
    return path


def _granule(ident: str) -> gcomc.RSRFGranule:
    return gcomc.parse_rsrf_identifier(ident)


def _record(ident: str) -> gcomc.CSWRecord:
    g = _granule(ident)
    d = g.observation_date
    path = f"product.gportal.jaxa.jp:/products/Standard/GCOM-C/GCOM-C.SGLI/L2.LAND.RSRF/3/{d:%Y}/{d:%m}/{d:%d}/{g.filename}"
    return gcomc.CSWRecord(g, path, 1000, "", "", 10.0)


def _cached(raw: Path, ident: str, **kw) -> gcomc.ProductResult:
    g = _granule(ident)
    p = make_rsrf(gcomc.local_raw_path(raw, g), ident, **kw)
    return gcomc.ProductResult(granule=g, status=gcomc.PRODUCT_AVAILABLE, source="local_cache", local_path=p,
                               metadata=gcomc.validate_rsrf_hdf5(p, g.filename))


# --------------------------------------------------------------------- parsing

def test_identifier_parsing_date_orbit_tile_version():
    g = gcomc.parse_rsrf_identifier("GC1SG1_20240315D01D_T0528_L2SG_RSRFQ_3002.h5")
    assert g.observation_date == date(2024, 3, 15)
    assert g.orbit_direction == "D"
    assert (g.tile_v, g.tile_h, g.tile_id) == (5, 28, "T0528")
    assert (g.version, g.algorithm_version, g.parameter_version) == ("3002", "3", "002")
    assert g.filename == "GC1SG1_20240315D01D_T0528_L2SG_RSRFQ_3002.h5"
    assert gcomc.parse_rsrf_identifier("GC1SG1_20240315A01D_T0528_L2SG_RSRFQ_3002").orbit_direction == "A"


@pytest.mark.parametrize("bad", [
    "GC1SG1_20240315D01D_T0528_L2SG_RSRFK_3002.h5",   # 1 km product variant
    "GC1SG1_20240315D08D_T0528_L2SG_RSRFQ_3002.h5",   # 8-day statistics
    "GC1SG1_20240315D01D_T1840_L2SG_RSRFQ_3002.h5",   # tile out of grid
    "GC1SG1_2024031XD01D_T0528_L2SG_RSRFQ_3002.h5",
])
def test_identifier_parsing_rejects_other_products(bad):
    with pytest.raises(ValueError):
        gcomc.parse_rsrf_identifier(bad)


def test_csw_parsing_real_records():
    payload = json.loads((FIXTURES / "csw_rsrf_sample.json").read_text())
    recs = gcomc.parse_csw_response(payload)
    ids = {r.granule.identifier for r in recs}
    assert "GC1SG1_20240315D01D_T0528_L2SG_RSRFQ_3002" in ids
    d = next(r for r in recs if r.granule.identifier == "GC1SG1_20240315D01D_T0528_L2SG_RSRFQ_3002")
    assert d.csw_path.startswith("product.gportal.jaxa.jp:/products/Standard/")
    sel = gcomc.select_daily_records(recs, [(5, 28), (5, 29)])
    assert set(sel) == {(date(2024, 3, 15), (5, 28)), (date(2024, 4, 9), (5, 28)), (date(2024, 4, 9), (5, 29))}
    assert all(r.granule.orbit_direction == "D" for r in sel.values())  # ascending records ignored


def test_csw_rejects_inconsistent_tile_and_unknown_version_and_duplicates():
    payload = json.loads((FIXTURES / "csw_rsrf_sample.json").read_text())
    bad = json.loads(json.dumps(payload))
    bad["features"][0]["properties"]["gpp"]["tileHNo"] = "27"
    with pytest.raises(gcomc.GCOMCCatalogError):
        gcomc.parse_csw_response(bad)
    newer = [_record("GC1SG1_20240409D01D_T0528_L2SG_RSRFQ_3003")]
    with pytest.raises(gcomc.GCOMCCatalogError, match="unknown RSRF product version"):
        gcomc.select_daily_records(newer, [(5, 28)])
    both = newer + [_record("GC1SG1_20240409D01D_T0528_L2SG_RSRFQ_3002")]
    assert gcomc.select_daily_records(both, [(5, 28)])[(date(2024, 4, 9), (5, 28))].granule.version == "3002"
    with pytest.raises(gcomc.GCOMCCatalogError):
        gcomc.parse_csw_response({"type": "ExceptionReport"})


def test_csw_query_uses_tile_parameters_not_bbox():
    seen = []

    def fake(url, params):
        seen.append(params)
        return json.loads((FIXTURES / "csw_rsrf_sample.json").read_text())

    gcomc.query_csw_records(date(2024, 3, 15), date(2024, 4, 9), 5, 28, fetch_json=fake)
    assert seen[0]["tileHNo"] == "28" and seen[0]["tileVNo"] == "5"
    assert seen[0]["datasetId"] == "10002015" and "bbox" not in seen[0]


def test_sftp_path_normalisation_products_to_product():
    g = _granule("GC1SG1_20240315D01D_T0528_L2SG_RSRFQ_3002")
    want = "/product/Standard/GCOM-C/GCOM-C.SGLI/L2.LAND.RSRF/3/2024/03/15/GC1SG1_20240315D01D_T0528_L2SG_RSRFQ_3002.h5"
    assert gcomc.resolve_sftp_path("product.gportal.jaxa.jp:/products/Standard/GCOM-C/GCOM-C.SGLI/L2.LAND.RSRF/3/2024/03/15/" + g.filename, g) == want
    assert gcomc.resolve_sftp_path("standard/GCOM-C/GCOM-C.SGLI/L2.LAND.RSRF/3/2024/03/15/" + g.filename, g) == want
    with pytest.raises(gcomc.GCOMCPathError):
        gcomc.resolve_sftp_path("product.gportal.jaxa.jp:/products/Standard/GCOM-C/GCOM-C.SGLI/L2.LAND.RSRF/3/2024/03/16/" + g.filename, g)
    with pytest.raises(gcomc.GCOMCPathError):
        gcomc.resolve_sftp_path("/somewhere/else/" + g.filename, g)


# ------------------------------------------------------------------ tile grid

def test_eqa_tile_selection_for_study_regions():
    root = Path(__file__).resolve().parents[1] / "config"
    geom = lambda n: json.loads((root / f"no{n}.geojson").read_text())["features"][0]["geometry"]
    assert gcomc.tiles_for_geometry(ASO_NO5) == [(5, 28), (5, 29)]
    assert gcomc.tiles_for_geometry(geom(1)) == [(5, 28)]
    assert gcomc.tiles_for_geometry(geom(9)) == [(5, 29)]


def test_t0528_t0529_boundary_is_lon_cos_lat_110():
    lat = 33.0
    lon_b = 110.0 / math.cos(math.radians(lat))
    dlat = 0.0001
    assert gcomc.tiles_for_bbox(lon_b - 0.01, lat - dlat, lon_b - 0.001, lat + dlat) == [(5, 28)]
    assert gcomc.tiles_for_bbox(lon_b + 0.001, lat - dlat, lon_b + 0.01, lat + dlat) == [(5, 29)]
    assert gcomc.tiles_for_bbox(lon_b - 0.01, lat - dlat, lon_b + 0.01, lat + dlat) == [(5, 28), (5, 29)]
    # The boundary is curved (moves east towards the north): the same longitude
    # lies in T0528 further north and in T0529 further south.
    assert gcomc.tiles_for_bbox(lon_b - 0.001, 35.0, lon_b + 0.001, 35.001) == [(5, 28)]
    assert gcomc.tiles_for_bbox(lon_b - 0.001, 31.0, lon_b + 0.001, 31.001) == [(5, 29)]
    with pytest.raises(ValueError):
        gcomc.tiles_for_bbox(179.0, 0.0, -179.0, 1.0)  # antimeridian not supported


def test_tile_transform_matches_real_corner_attributes(monkeypatch):
    monkeypatch.setitem(gcomc.GCOMC_TILE_PIXELS, "250m", 4800)
    t = gcomc.eqa_tile_transform(5, 28, "250m")
    x_eq_deg = math.degrees(t.c / gcomc.EQA_SPHERE_RADIUS_M)
    # Real T0528 Geometry_data: Upper_left_longitude 130.5407 at 40N, Lower_left 115.4701 at 30N.
    assert abs(x_eq_deg / math.cos(math.radians(40)) - 130.54073) < 1e-4
    assert abs(x_eq_deg / math.cos(math.radians(30)) - 115.47005) < 1e-4
    # JAXA L2 EQA-tile GeoTIFF definition: ModelPixelScale 231.65635827 m (250 m), Sphere (DatumE_Sphere)
    assert abs(t.a - 231.65635827) < 1e-6


def _jaxa_tool_centre(lin, col, v, h, n):
    """Independent implementation of the JAXA GeoTIFF tool definition (manual v1.2, App. 7.1)."""
    x, y = col + 1.0, lin + 1.0                      # centre of the first pixel = (1, 1)
    lat = 90.0 - 10.0 * v - (y - 0.5) * 10.0 / n
    lon = ((x - 0.5) * 10.0 / n - 180.0 + 10.0 * h) / np.cos(np.radians(lat))
    return lon, lat


def _handbook_centre(lin, col, v, h, n):
    """Independent implementation of the Data Users Handbook 4.1.4.1 formula (NP_i = NINT(NP0*cos(lat)))."""
    d = 180.0 / n / 18.0
    np0 = 2 * np.floor(180.0 / d + 0.5)
    lat = 90.0 - (lin + v * n + 0.5) * d
    npi = np.floor(np0 * np.cos(np.radians(lat)) + 0.5)
    return 360.0 / npi * (col + h * n - np0 / 2 + 0.5), lat, npi


@pytest.mark.parametrize("grid,n", [("250m", 4800), ("1km", 1200)])
@pytest.mark.parametrize("v,h", [(5, 28), (5, 29)])
def test_geotiff_pixel_centres_match_jaxa_tool_definition(monkeypatch, grid, n, v, h):
    """GeoTIFF affine + CRS (inverted by PROJ) == JAXA tool pixel centres; Handbook NINT variant stays within 0.22 px."""
    from rasterio.warp import transform as warp
    monkeypatch.setitem(gcomc.GCOMC_TILE_PIXELS, grid, n)
    idx = np.array([0, 1, n // 2, n - 2, n - 1], dtype=np.float64)
    lin, col = [a.ravel() for a in np.meshgrid(idx, idx, indexing="ij")]
    t = gcomc.eqa_tile_transform(v, h, grid)
    lon_a, lat_a = warp(gcomc.eqa_crs(), "EPSG:4326", list(t.c + (col + 0.5) * t.a), list(t.f + (lin + 0.5) * t.e))
    lon_t, lat_t = _jaxa_tool_centre(lin, col, v, h, n)
    assert np.max(np.abs(np.array(lon_a) - lon_t)) < 1e-9 and np.max(np.abs(np.array(lat_a) - lat_t)) < 1e-9
    lon_h, lat_h, npi = _handbook_centre(lin, col, v, h, n)
    assert np.max(np.abs(lat_h - lat_t)) < 1e-9                      # latitude definitions agree exactly
    assert np.max(np.abs(lon_h - lon_t) * npi / 360.0) < 0.22        # E-W difference bounded (audited max 0.217 px)


# -------------------------------------------------------------- status rules

class FakeSFTP:
    def __init__(self, files: Dict[str, bytes] = None, deny: set = (), fail_after: Optional[int] = None):
        self.files, self.deny, self.fail_after = files or {}, set(deny), fail_after
        self.calls = []

    def _check(self, path):
        if path in self.deny:
            raise PermissionError(errno.EACCES, "Permission denied", path)
        if path not in self.files:
            raise FileNotFoundError(errno.ENOENT, "No such file", path)

    def stat_size(self, path):
        self.calls.append(("stat", path))
        self._check(path)
        return len(self.files[path])

    def read_head(self, path, n=8):
        self.calls.append(("head", path))
        self._check(path)
        return self.files[path][:n]

    def download(self, path, dest, expected_size, chunk=1 << 20):
        import hashlib
        self.calls.append(("download", path))
        data = self.files[path]
        with open(dest, "wb") as fh:
            if self.fail_after is not None:
                fh.write(data[: self.fail_after])
                raise ValueError("simulated non-network failure mid-download")
            fh.write(data)
        return hashlib.sha256(data).hexdigest()


def test_status_available_when_sftp_serves_old_file(tmp_path):
    """Age alone never yields REQUEST_REQUIRED: a readable 2.5+ year old file is AVAILABLE."""
    ident = "GC1SG1_20240315D01D_T0528_L2SG_RSRFQ_3002"
    src = make_rsrf(tmp_path / "src" / f"{ident}.h5", ident)
    rec = _record(ident)
    sftp = FakeSFTP({gcomc.resolve_sftp_path(rec.csw_path, rec.granule): src.read_bytes()})
    assert gcomc.in_request_period(rec.granule.observation_date, TODAY)
    res = gcomc.acquire_product(rec, tmp_path / "raw", lambda: sftp, TODAY)
    assert res.status == gcomc.PRODUCT_AVAILABLE and res.source == "sftp"
    assert res.local_path.exists() and not Path(str(res.local_path) + ".part").exists()


@pytest.mark.parametrize("deny", [False, True])
def test_status_request_required_only_when_sftp_refuses_and_archive_rule_applies(tmp_path, deny):
    rec = _record("GC1SG1_20240315D01D_T0528_L2SG_RSRFQ_3002")
    path = gcomc.resolve_sftp_path(rec.csw_path, rec.granule)
    sftp = FakeSFTP({path: b"x"} if deny else {}, deny={path} if deny else ())
    res = gcomc.acquire_product(rec, tmp_path / "raw", lambda: sftp, TODAY)
    assert res.status == gcomc.PRODUCT_REQUEST_REQUIRED


def test_status_error_when_recent_record_missing_on_sftp(tmp_path):
    rec = _record("GC1SG1_20260901D01D_T0528_L2SG_RSRFQ_3002")
    res = gcomc.acquire_product(rec, tmp_path / "raw", lambda: FakeSFTP({}), TODAY)
    assert res.status == gcomc.PRODUCT_ERROR and "archive rule does not apply" in res.detail


def test_status_error_when_remote_file_is_not_hdf5(tmp_path):
    rec = _record("GC1SG1_20240409D01D_T0528_L2SG_RSRFQ_3002")
    sftp = FakeSFTP({gcomc.resolve_sftp_path(rec.csw_path, rec.granule): b"<html>error</html>"})
    assert gcomc.acquire_product(rec, tmp_path / "raw", lambda: sftp, TODAY).status == gcomc.PRODUCT_ERROR


def test_auth_error_propagates_not_request_required(tmp_path):
    def factory():
        raise gcomc.GCOMCAuthError("publickey rejected")

    with pytest.raises(gcomc.GCOMCAuthError):
        gcomc.acquire_product(_record("GC1SG1_20240315D01D_T0528_L2SG_RSRFQ_3002"), tmp_path / "raw", factory, TODAY)


def test_request_period_boundary_is_30_months():
    assert gcomc.in_request_period(date(2024, 4, 2), date(2026, 10, 3))
    assert not gcomc.in_request_period(date(2024, 4, 3), date(2026, 10, 3))


# ------------------------------------------------- cache reuse / safe download

def test_local_raw_cache_is_reused_without_sftp(tmp_path):
    ident = "GC1SG1_20240409D01D_T0528_L2SG_RSRFQ_3002"
    make_rsrf(gcomc.local_raw_path(tmp_path / "raw", _granule(ident)), ident)

    def factory():
        raise AssertionError("SFTP must not be contacted when a valid local raw file exists")

    res = gcomc.acquire_product(_record(ident), tmp_path / "raw", factory, TODAY)
    assert res.status == gcomc.PRODUCT_AVAILABLE and res.source == "local_cache" and len(res.sha256) == 64


def test_invalid_local_raw_file_is_error_and_left_untouched(tmp_path):
    ident = "GC1SG1_20240409D01D_T0528_L2SG_RSRFQ_3002"
    p = gcomc.local_raw_path(tmp_path / "raw", _granule(ident))
    p.parent.mkdir(parents=True)
    p.write_bytes(b"not hdf5")
    res = gcomc.acquire_product(_record(ident), tmp_path / "raw", lambda: FakeSFTP({}), TODAY)
    assert res.status == gcomc.PRODUCT_ERROR and p.read_bytes() == b"not hdf5"


def test_interrupted_download_leaves_no_final_or_part_file(tmp_path):
    ident = "GC1SG1_20240409D01D_T0528_L2SG_RSRFQ_3002"
    src = make_rsrf(tmp_path / "src" / f"{ident}.h5", ident)
    rec = _record(ident)
    sftp = FakeSFTP({gcomc.resolve_sftp_path(rec.csw_path, rec.granule): src.read_bytes()}, fail_after=100)
    with pytest.raises(ValueError):
        gcomc.acquire_product(rec, tmp_path / "raw", lambda: sftp, TODAY)
    final = gcomc.local_raw_path(tmp_path / "raw", rec.granule)
    assert not final.exists() and not final.with_name(final.name + ".part").exists()


def test_corrupt_download_is_not_renamed(tmp_path):
    ident = "GC1SG1_20240409D01D_T0528_L2SG_RSRFQ_3002"
    rec = _record(ident)
    sftp = FakeSFTP({gcomc.resolve_sftp_path(rec.csw_path, rec.granule): gcomc.HDF5_SIGNATURE + b"garbage" * 10})
    res = gcomc.acquire_product(rec, tmp_path / "raw", lambda: sftp, TODAY)
    final = gcomc.local_raw_path(tmp_path / "raw", rec.granule)
    assert res.status == gcomc.PRODUCT_ERROR and not final.exists() and not final.with_name(final.name + ".part").exists()


# ------------------------------------------------------------- HDF5 / scaling

def test_hdf5_validation_rejects_schema_changes(tmp_path):
    ident = "GC1SG1_20240409D01D_T0528_L2SG_RSRFQ_3002"
    ok = make_rsrf(tmp_path / "a" / f"{ident}.h5", ident)
    meta = gcomc.validate_rsrf_hdf5(ok)
    assert meta["Product_version"] == "3002" and meta["fields"]["SW02"]["data_description"] == "TOA reflectance of SW02"
    with pytest.raises(gcomc.GCOMCSchemaError, match="not found"):
        gcomc.validate_rsrf_hdf5(make_rsrf(tmp_path / "b" / f"{ident}.h5", ident, drop="Image_data/Rs_VN08"))
    with pytest.raises(gcomc.GCOMCSchemaError, match="unknown product version"):
        gcomc.validate_rsrf_hdf5(make_rsrf(tmp_path / "c" / f"{ident}.h5", ident, version="3003"))
    with pytest.raises(gcomc.GCOMCSchemaError, match="Data_description"):
        gcomc.validate_rsrf_hdf5(make_rsrf(tmp_path / "d" / f"{ident}.h5", ident, sw02_desc="Surface reflectance of SW02"))


def test_slope_offset_error_dn_and_valid_range():
    dn = np.array([[0, 1234, 65534, 65535, 60001]], np.uint16)
    out = gcomc.dn_to_reflectance(dn, 9.9999997e-05, 0.0, 65535, 0, 60000)
    assert out.dtype == np.float32
    assert out[0, 0] == np.float32(0.0)
    assert out[0, 1] == np.float32(1234 * 9.9999997e-05)
    assert np.isnan(out[0, 2]) and np.isnan(out[0, 3]) and np.isnan(out[0, 4])
    off = gcomc.dn_to_reflectance(np.array([[100]], np.uint16), 1e-4, -0.2, 65535, 0, 65534)
    assert abs(float(off[0, 0]) - (-0.19)) < 1e-6


# --------------------------------------------- process_date: routing, mosaic, AOI

def _read(path: Path):
    with rasterio.open(path) as r:
        return r.read(1), r.transform, r.tags(), r.dtypes[0], r.nodata


def test_routing_native_grids_qa_raw_and_sw02_type(tmp_path):
    raw, out = tmp_path / "raw", tmp_path / "out"
    prods = {(5, 28): _cached(raw, "GC1SG1_20240409D01D_T0528_L2SG_RSRFQ_3002"),
             (5, 29): _cached(raw, "GC1SG1_20240409D01D_T0529_L2SG_RSRFQ_3002")}
    s = gcomc.process_date(date(2024, 4, 9), prods, CROSS_AOI, out)
    assert s["aoi_status"] == gcomc.AOI_OBSERVED
    vn08 = out / "250m" / "VN08" / "GCOMC_SGLI_RSRF_20240409_D_VN08.tif"
    sw03 = out / "250m" / "SW03" / "GCOMC_SGLI_RSRF_20240409_D_SW03.tif"
    sw02 = out / "1km" / "SW02" / "GCOMC_SGLI_RSRF_20240409_D_SW02.tif"
    qa = out / "qa" / "QA_flag" / "GCOMC_SGLI_RSRF_20240409_D_QA_flag.tif"
    assert vn08.exists() and sw03.exists() and sw02.exists() and qa.exists()
    assert not (out / "250m" / "SW02").exists() and not (out / "1km" / "VN08").exists() and not (out / "250m" / "SW04").exists()
    a250, t250, tags250, dt250, _ = _read(vn08)
    a1k, t1k, tags1k, dt1k, _ = _read(sw02)
    aqa, tqa, tagsqa, dtqa, ndqa = _read(qa)
    assert dt250 == dt1k == "float32"
    assert abs(t1k.a / t250.a - 4.0) < 1e-12              # 1 km kept on its own grid, never upsampled
    assert a1k.shape[0] <= math.ceil(a250.shape[0] / 4) + 1
    assert tags1k["reflectance_type"] == "TOA reflectance" and "TOA" in tags1k["data_description"]
    assert tags250["reflectance_type"] == "surface reflectance"
    assert _read(sw03)[2]["reflectance_type"] == "surface reflectance"
    assert dtqa == "uint16" and tagsqa["scale_offset_applied"] == "False"
    assert set(np.unique(aqa)) <= {2, 1}                  # raw values (2) and Error_DN fill outside bbox; never scaled
    assert np.isclose(np.nanmax(a250), 2000 * 9.9999997e-05, rtol=0, atol=1e-7)
    assert tags250["resampling"] == "none" and tags250["raster_reprojected"] == "False"


def test_mosaic_places_both_tiles_without_resampling(tmp_path):
    raw, out = tmp_path / "raw", tmp_path / "out"
    prods = {(5, 28): _cached(raw, "GC1SG1_20240409D01D_T0528_L2SG_RSRFQ_3002", dn250=1000),
             (5, 29): _cached(raw, "GC1SG1_20240409D01D_T0529_L2SG_RSRFQ_3002", dn250=3000)}
    gcomc.process_date(date(2024, 4, 9), prods, CROSS_AOI, out)
    arr, tr, *_ = _read(out / "250m" / "VN01" / "GCOMC_SGLI_RSRF_20240409_D_VN01.tif")
    vals = set(np.round(np.unique(arr[~np.isnan(arr)]), 6))
    assert vals == {np.float32(0.1).round(6), np.float32(0.3).round(6)}  # exact DN from each tile, nothing blended
    # the column where values switch lies exactly on the global tile edge (h=29 start)
    x0, _ = gcomc._eqa_origin()
    col_global_edge = 29 * N250
    col_in_window = int(round((x0 + col_global_edge * tr.a - tr.c) / tr.a))
    row = arr[arr.shape[0] // 2]
    assert np.all(np.isclose(row[col_in_window - 1], 0.1, atol=1e-6) | np.isnan(row[col_in_window - 1]))
    assert np.isclose(row[col_in_window], 0.3, atol=1e-6)


def test_aoi_no_data_writes_no_geotiff_and_logs(tmp_path, caplog):
    raw, out = tmp_path / "raw", tmp_path / "out"
    ident = "GC1SG1_20240315D01D_T0528_L2SG_RSRFQ_3002"
    prods = {(5, 28): _cached(raw, ident, dn250=None)}
    s = gcomc.process_date(date(2024, 3, 15), prods, CROSS_AOI, out)
    assert s["aoi_status"] == gcomc.AOI_NO_DATA and s["files"] == []
    assert not (out / "250m").exists()


def test_partial_observation_and_missing_tile(tmp_path):
    raw, out = tmp_path / "raw", tmp_path / "out"
    qa = np.full((N250, N250), 2, np.uint16)
    qa[:, 44:] = 1  # easternmost T0528 columns (inside the AOI): bit0 'no available data'
    prods = {(5, 28): _cached(raw, "GC1SG1_20240409D01D_T0528_L2SG_RSRFQ_3002", qa=qa)}
    g29 = gcomc.RSRFGranule("(no record) T0529", date(2024, 4, 9), "D", 5, 29, "", "", "")
    prods[(5, 29)] = gcomc.ProductResult(granule=g29, status=gcomc.PRODUCT_NO_RECORD)
    s = gcomc.process_date(date(2024, 4, 9), prods, CROSS_AOI, out)
    assert s["aoi_status"] == gcomc.AOI_PARTIAL
    assert s["product_status"]["T0529"]["status"] == gcomc.PRODUCT_NO_RECORD


# ------------------------------------------------- orchestration (daily, statuses)

def _run(tmp_path, records, products_raw, sftp=None, caplog=None, start=date(2024, 3, 14), end=date(2024, 3, 16)):
    config = {"surface_reflectance": {"gcomc_raw_dir": str(tmp_path / "raw")}}
    return gcomc._process_gcomc_surface_reflectance(
        config, tmp_path, tmp_path / "output", CROSS_AOI, start, end, today=TODAY,
        csw_query=lambda s, e, v, h: [r for r in records if (r.granule.tile_v, r.granule.tile_h) == (v, h)],
        sftp_factory=(lambda: sftp) if sftp else (lambda: (_ for _ in ()).throw(AssertionError("no SFTP expected"))),
    )


def test_daily_iteration_no_record_and_aoi_no_data_are_distinct(tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="src.gcomc")
    raw = tmp_path / "raw"
    ident = "GC1SG1_20240315D01D_T0528_L2SG_RSRFQ_3002"
    make_rsrf(gcomc.local_raw_path(raw, _granule(ident)), ident, dn250=None)
    s = _run(tmp_path, [_record(ident)], raw)
    assert s["dates_no_record"] == ["2024-03-14", "2024-03-16"]   # every calendar day is checked
    assert s["dates_aoi_no_data"] == ["2024-03-15"]
    assert "GCOM-C/SGLI: no RSRF observation for AOI on 2024-03-15" in caplog.text
    summary = json.loads((tmp_path / "output" / "gcomc" / "rsrf" / "summary" / "GCOMC_SGLI_RSRF_20240315_D.json").read_text())
    assert summary["product_status"]["T0528"]["status"] == "AVAILABLE" and summary["aoi_status"] == "AOI_NO_DATA"
    # second run skips the date (file_exists=skip) without touching CSW products again
    s2 = _run(tmp_path, [_record(ident)], raw)
    assert "2024-03-15" in s2["dates_skipped"]


def test_request_required_date_is_reported_not_processed(tmp_path):
    rec = _record("GC1SG1_20240315D01D_T0528_L2SG_RSRFQ_3002")
    s = _run(tmp_path, [rec], tmp_path / "raw", sftp=FakeSFTP({}), start=date(2024, 3, 15), end=date(2024, 3, 15))
    assert [d["date"] for d in s["dates_request_required"]] == ["2024-03-15"]
    assert not s["dates_processed"] and not s["dates_aoi_no_data"]


def test_auth_failure_is_reported_as_error(tmp_path):
    rec = _record("GC1SG1_20240315D01D_T0528_L2SG_RSRFQ_3002")
    config = {"surface_reflectance": {"gcomc_raw_dir": str(tmp_path / "raw")}}

    def bad():
        raise gcomc.GCOMCAuthError("publickey rejected")

    s = gcomc._process_gcomc_surface_reflectance(config, tmp_path, tmp_path / "o", CROSS_AOI, date(2024, 3, 15), date(2024, 3, 15),
                                                 today=TODAY, csw_query=lambda *a: [rec], sftp_factory=bad)
    assert "publickey rejected" in s["error"] and not s["dates_request_required"]


# --------------------------------------------------------------- config/pipeline

def test_config_accepts_gcomc_opt_in_and_rejects_unknown():
    assert _normalize_satellites(["modis", "viirs", "gcomc"]) == ["modis", "viirs", "gcomc"]
    assert _normalize_satellites("gcomc") == ["gcomc"]
    with pytest.raises(ValueError):
        _normalize_satellites(["gcom-c"])
