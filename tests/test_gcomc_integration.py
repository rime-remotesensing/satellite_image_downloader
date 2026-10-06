"""Real-data integration tests for GCOM-C/SGLI RSRF (opt-in).

Local-archive cases (Case A/B/C) run only when GCOMC_RAW_DIR points at the raw
archive containing the audited files (e.g. inside the "gcomc" compose service).
They never touch the network: an SFTP factory that fails if called proves the
local raw cache is reused.

The network case runs only with GCOMC_RUN_NETWORK_TESTS=1 plus G-Portal
credentials; it queries CSW and stats/reads 8 bytes on SFTP (no download).
"""
from __future__ import annotations

import json
import math
import os
from datetime import date
from pathlib import Path

import h5py
import numpy as np
import pytest
import rasterio
from rasterio.warp import transform as warp_transform

from src import gcomc

ROOT = Path(__file__).resolve().parents[1]
RAW = Path(os.environ.get("GCOMC_RAW_DIR", "/nonexistent"))
ASO = json.loads((ROOT / "config" / "no5.geojson").read_text())["features"][0]["geometry"]
CASE_FILES = [
    "GC1SG1_20240315D01D_T0528_L2SG_RSRFQ_3002", "GC1SG1_20240315D01D_T0529_L2SG_RSRFQ_3002",
    "GC1SG1_20240409D01D_T0528_L2SG_RSRFQ_3002", "GC1SG1_20240409D01D_T0529_L2SG_RSRFQ_3002",
]
have_archive = all(gcomc.local_raw_path(RAW, gcomc.parse_rsrf_identifier(i)).exists() for i in CASE_FILES)
local = pytest.mark.skipif(not have_archive, reason="GCOMC_RAW_DIR with the audited RSRF files not available")
TODAY = date(2026, 10, 3)


def _no_sftp():
    raise AssertionError("integration cases must reuse the local raw archive, not SFTP")


def _products(*idents):
    out = {}
    for ident in idents:
        g = gcomc.parse_rsrf_identifier(ident)
        rec = gcomc.CSWRecord(g, "standard/" + gcomc.expected_sftp_path(g).split("/Standard/", 1)[1], None, "", "", None)
        res = gcomc.acquire_product(rec, RAW, _no_sftp, TODAY)
        assert res.status == gcomc.PRODUCT_AVAILABLE and res.source == "local_cache", res.detail
        out[(g.tile_v, g.tile_h)] = res
    return out


def _value_at(path, lon, lat):
    with rasterio.open(path) as r:
        xs, ys = warp_transform("EPSG:4326", r.crs, [lon], [lat])
        row, col = r.index(xs[0], ys[0])
        return r.read(1)[row, col], r


@local
def test_case_a_20240315_t0528_available_but_aoi_no_data(tmp_path):
    s = gcomc.process_date(date(2024, 3, 15), _products(CASE_FILES[0]), ASO, tmp_path)
    assert s["product_status"]["T0528"]["status"] == "AVAILABLE"
    assert s["aoi_status"] == gcomc.AOI_NO_DATA and s["files"] == []
    both = gcomc.process_date(date(2024, 3, 15), _products(*CASE_FILES[:2]), ASO, tmp_path / "both")
    assert both["aoi_status"] == gcomc.AOI_NO_DATA


@local
def test_case_b_20240409_t0528_scaling_geolocation_qa_geotiff(tmp_path):
    prods = _products(CASE_FILES[2])
    s = gcomc.process_date(date(2024, 4, 9), prods, ASO, tmp_path)
    assert s["aoi_status"] == gcomc.AOI_PARTIAL  # Aso AOI also extends into T0529, which is not supplied here
    f250 = tmp_path / "250m" / "GCOMC_SGLI_RSRF_20240409_D_250m.tif"   # multi-band: VN01..VN11, SW03
    f1k = tmp_path / "1km" / "GCOMC_SGLI_RSRF_20240409_D_1km.tif"      # multi-band: SW01, SW02, SW04
    lwf = tmp_path / "qa" / "Land_water_flag" / "GCOMC_SGLI_RSRF_20240409_D_Land_water_flag.tif"
    qa = tmp_path / "qa" / "QA_flag" / "GCOMC_SGLI_RSRF_20240409_D_QA_flag.tif"
    with rasterio.open(f250) as r250, rasterio.open(f1k) as r1k:
        assert r250.descriptions[7] == "VN08" and r1k.descriptions[0] == "SW01"
        # JAXA L2 EQA-tile GeoTIFF pixel scale (231.65635827 m / 926.62543306 m)
        assert abs(r250.transform.a - 231.65635827) < 1e-6 and abs(r1k.transform.a - 926.62543306) < 1e-6
        assert r250.crs == r1k.crs == gcomc.eqa_crs()
        a250, t250 = r250.read(8), r250.transform                 # band 8 = VN08
    # exact scaling against the HDF5 DN at the same native pixel
    path = prods[(5, 28)].local_path
    x0, y0 = gcomc._eqa_origin()
    col0 = int(round((t250.c - x0) / t250.a)) - 28 * 4800
    row0 = int(round((y0 - t250.f) / t250.a)) - 5 * 4800
    with h5py.File(path, "r") as f:
        ds = f["Image_data/Rs_VN08"]
        h, w = a250.shape
        dn = ds[row0:row0 + h, col0:min(col0 + w, 4800)]
        slope = float(ds.attrs["Slope"][0])
        qa_dn = f["Image_data/QA_flag"][row0:row0 + h, col0:min(col0 + w, 4800)]
    sub = a250[:, : dn.shape[1]]
    ok = (dn <= 65534) & ~np.isnan(sub)
    assert ok.sum() > 1000
    assert np.array_equal(sub[ok], (dn[ok].astype(np.float64) * slope).astype(np.float32))
    with rasterio.open(qa) as rq:
        assert rq.dtypes[0] == "uint16" and rq.tags()["scale_offset_applied"] == "False"
        qa_out = rq.read(1)[:, : dn.shape[1]]
        inside = rq.read_masks(1)[:, : dn.shape[1]] > 0
        assert np.array_equal(qa_out[inside], qa_dn[inside])
    # geolocation: Aso caldera is land, Ariake Sea (outside crop) not testable -> use land inside the crop
    v, _ = _value_at(lwf, 131.05, 32.95)
    assert v == 100


@local
def test_case_c_20240409_t0528_t0529_boundary_mosaic(tmp_path):
    prods = _products(*CASE_FILES[2:])
    s = gcomc.process_date(date(2024, 4, 9), prods, ASO, tmp_path)
    assert s["aoi_status"] == gcomc.AOI_OBSERVED
    assert s["band_valid_fraction_in_aoi"]["VN08"] > 0.99 and s["band_valid_fraction_in_aoi"]["SW02"] > 0.99
    tif = tmp_path / "250m" / "GCOMC_SGLI_RSRF_20240409_D_250m.tif"
    assert len(list((tmp_path / "250m").glob("*.tif"))) == 1  # one 250 m raster per date
    with rasterio.open(tif) as r:
        assert r.descriptions[7] == "VN08"
        arr, t = r.read(8), r.transform
        assert r.tags()["tiles"] == "T0528,T0529"
    x0, y0 = gcomc._eqa_origin()
    edge = int(round((x0 + 29 * 4800 * t.a - t.c) / t.a))  # window column where T0529 starts
    assert 0 < edge < arr.shape[1]
    row0 = int(round((y0 - t.f) / t.a)) - 5 * 4800
    with h5py.File(prods[(5, 28)].local_path, "r") as a, h5py.File(prods[(5, 29)].local_path, "r") as b:
        slope = float(a["Image_data/Rs_VN08"].attrs["Slope"][0])
        last28 = a["Image_data/Rs_VN08"][row0:row0 + arr.shape[0], 4799]
        first29 = b["Image_data/Rs_VN08"][row0:row0 + arr.shape[0], 0]
    for col, src in ((edge - 1, last28), (edge, first29)):
        ok = (src <= 65534) & ~np.isnan(arr[:, col])
        assert ok.sum() > 10
        assert np.array_equal(arr[ok, col], (src[ok].astype(np.float64) * slope).astype(np.float32))
    v, _ = _value_at(tmp_path / "qa" / "Land_water_flag" / "GCOMC_SGLI_RSRF_20240409_D_Land_water_flag.tif", 131.20, 32.97)
    assert v == 100  # Aso eastern part (T0529 side) is land


network = pytest.mark.skipif(
    os.environ.get("GCOMC_RUN_NETWORK_TESTS") != "1" or not os.environ.get("GPORTAL_USERNAME")
    or not os.path.isfile(gcomc.GCOMC_PRIVATE_KEY_PATH),
    reason="set GCOMC_RUN_NETWORK_TESTS=1 with G-Portal credentials to run",
)


@network
def test_network_csw_record_and_sftp_readability():
    recs = gcomc.query_csw_records(date(2024, 3, 15), date(2024, 3, 15), 5, 28)
    sel = gcomc.select_daily_records(recs, [(5, 28)])
    rec = sel[(date(2024, 3, 15), (5, 28))]
    path = gcomc.resolve_sftp_path(rec.csw_path, rec.granule)
    assert path.startswith("/product/Standard/")
    with gcomc.GPortalSFTP(os.environ["GPORTAL_USERNAME"]) as sftp:
        assert sftp.stat_size(path) > 0
        assert sftp.read_head(path) == gcomc.HDF5_SIGNATURE
