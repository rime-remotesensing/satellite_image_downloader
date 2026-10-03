"""VIIRS default routing (l2_swath) / explicit legacy (daily_l2g_legacy) and output separation (no network)."""
from __future__ import annotations

from datetime import date
from pathlib import Path
from unittest.mock import patch

import pytest

from src import pipeline, viirs_l2
from src import surface_reflectance as sr

ROOT = Path(__file__).resolve().parents[1]
GEOJSON = str(ROOT / "config" / "no5.geojson")


def _cfg(tmp_path, satellites, viirs_product=None):
    cfg = {"geojson": GEOJSON, "startday": "20240310", "endday": "20240310", "satellite": satellites,
           "output": str(tmp_path / "out"), "activefire": "none"}
    if viirs_product is not None:
        cfg["surface_reflectance"] = {"viirs_product": viirs_product}
    return cfg


@pytest.fixture
def mocks():
    with patch.object(pipeline, "_process_modis_surface_reflectance", return_value={"modis": 1}) as modis, \
         patch.object(pipeline, "_process_viirs_surface_reflectance", return_value={"legacy": 1}) as legacy, \
         patch("src.viirs_l2._process_viirs_l2_swath", return_value={"l2": 1}) as l2, \
         patch("src.gcomc._process_gcomc_surface_reflectance", return_value={"gcomc": 1}) as gcomc:
        yield {"modis": modis, "legacy": legacy, "l2": l2, "gcomc": gcomc}


def test_default_viirs_product_is_l2_swath():
    assert pipeline.VIIRS_DEFAULT_PRODUCT == "l2_swath"
    assert pipeline._viirs_product({}) == "l2_swath"
    assert pipeline._viirs_product({"surface_reflectance": {}}) == "l2_swath"
    assert pipeline._viirs_product({"surface_reflectance": {"viirs_product": "daily_l2g_legacy"}}) == "daily_l2g_legacy"
    with pytest.raises(ValueError):
        pipeline._viirs_product({"surface_reflectance": {"viirs_product": "VNP09GA"}})


def test_viirs_without_product_routes_to_l2_swath(tmp_path, mocks):
    r = pipeline.run_pipeline(_cfg(tmp_path, ["viirs"]), ROOT)
    assert mocks["l2"].call_count == 1 and mocks["legacy"].call_count == 0
    assert r["viirs_l2_swath"] == {"l2": 1} and "viirs_surface_reflectance" not in r
    kw = mocks["l2"].call_args.kwargs
    assert set(kw) == {"config", "config_dir", "output_root", "geometry_wgs84", "bbox", "start_date", "end_date"}
    assert kw["start_date"] == kw["end_date"] == date(2024, 3, 10)


def test_explicit_legacy_routes_to_existing_ga_pipeline(tmp_path, mocks):
    r = pipeline.run_pipeline(_cfg(tmp_path, ["viirs"], "daily_l2g_legacy"), ROOT)
    assert mocks["legacy"].call_count == 1 and mocks["l2"].call_count == 0
    assert r["viirs_surface_reflectance"] == {"legacy": 1} and "viirs_l2_swath" not in r
    kw = mocks["legacy"].call_args.kwargs
    assert set(kw) == {"config", "config_dir", "output_root", "geometry_wgs84", "bbox", "start_date", "end_date"}


@pytest.mark.parametrize("viirs_product", [None, "daily_l2g_legacy", "l2_swath"])
def test_modis_and_gcomc_routing_unaffected_by_viirs_default(tmp_path, mocks, viirs_product):
    r = pipeline.run_pipeline(_cfg(tmp_path, ["modis", "gcomc"], viirs_product), ROOT)
    assert mocks["modis"].call_count == 1 and mocks["gcomc"].call_count == 1
    assert mocks["l2"].call_count == 0 and mocks["legacy"].call_count == 0          # no VIIRS requested -> none called
    assert r["modis_surface_reflectance"] == {"modis": 1} and r["gcomc_surface_reflectance"] == {"gcomc": 1}
    mk = mocks["modis"].call_args.kwargs
    assert set(mk) == {"config", "config_dir", "output_root", "geometry_wgs84", "bbox", "start_date", "end_date"}


def test_l2_swath_and_legacy_outputs_never_collide(tmp_path):
    """Legacy GA files live in <platform>/{500m,1km,qa}; L2 swath files live under <platform>/l2_swath/ only."""
    out = tmp_path / "out"
    from tests.test_viirs_l2 import AOI, NoNet, make_granule
    raw = tmp_path / "raw"
    srp = make_granule(raw)
    s = viirs_l2.process_platform_day(viirs_l2.PLATFORMS["noaa20"], date(2024, 3, 10), [(srp.name, "", "DAY")], raw, NoNet(), AOI,
                                      viirs_l2.grid_window_for_geometry(AOI, halo_m=0.0), out)
    l2_files = {Path(f) for f in s["daily"]["files"] + s["overpasses"][0]["files"]}
    l2_files |= set((out / "viirs" / "surface_reflectance" / "noaa20" / "l2_swath" / "summary").glob("*.json"))
    platform_dir = out / "viirs" / "surface_reflectance" / "noaa20"
    assert l2_files and all(platform_dir / "l2_swath" in f.parents for f in l2_files)
    legacy = set()
    for short, plat in (("VNP09GA", "snpp"), ("VJ109GA", "noaa20"), ("VJ209GA", "noaa21")):
        pdir = out / "viirs" / "surface_reflectance" / plat
        legacy |= set(sr._expected_primary_outputs("viirs", short, plat, pdir, "20240310"))
    assert legacy and not any("l2_swath" in f.parts for f in legacy)
    assert not (l2_files & legacy)
    assert {f.name for f in l2_files}.isdisjoint({f.name for f in legacy})
    # raw caches are separate too: L2 default raw dir vs the legacy temporary download dir
    l2_raw_default = out / "viirs" / "_raw_l2"
    legacy_tmp = out / "viirs" / "surface_reflectance" / "_tmp"
    assert l2_raw_default not in legacy_tmp.parents and legacy_tmp not in l2_raw_default.parents


def test_grid_definition_is_replaceable():
    g = viirs_l2.AnalysisGridDefinition("test_zone53", "EPSG:32653", 0.0, 0.0, {"375m": 375.0, "750m": 750.0}, 750.0)
    aoi = {"type": "Polygon", "coordinates": [[[135.70, 34.88], [135.80, 34.88], [135.80, 34.95], [135.70, 34.95], [135.70, 34.88]]]}
    w = viirs_l2.grid_window_for_geometry(aoi, grid=g)
    assert w.grid is g and w.x0 % 750 == 0 and w.y1 % 750 == 0
    assert viirs_l2.grid_window_for_geometry(aoi).grid is viirs_l2.DEFAULT_ANALYSIS_GRID
    assert viirs_l2.DEFAULT_ANALYSIS_GRID.crs == "EPSG:32652"
