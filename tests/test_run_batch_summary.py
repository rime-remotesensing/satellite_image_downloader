"""run.py batch summary: failed dates are counted from the real run_pipeline() result for both VIIRS modes.

Results are produced by the real pipeline code; only network access is faked (LAADS/CMR client for L2,
Earthdata login/search/download for the legacy GA path). No network.
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

import run
from src import pipeline, viirs_l2
from tests.test_viirs_l2 import AOI, make_granule

DAY = "20240310"


class _Session:
    """HTTP session returning an HTML error page instead of the data file."""

    class _Resp:
        headers = {"Content-Length": "6"}

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def raise_for_status(self):
            pass

        def iter_content(self, n):
            yield b"<html>"

    def get(self, *a, **k):
        return self._Resp()


class FakeLAADS:
    """CMR search returns the given entries; any download yields a non-HDF payload (-> VIIRSSchemaError)."""

    def __init__(self, entries):
        self.entries = entries

    def search(self, concept_id, start, end, bbox):
        return list(self.entries.get(concept_id, []))

    def fetch(self, url, dest):
        viirs_l2._download(url, dest, _Session())

    def find_by_name(self, concept_id, name):
        raise AssertionError("geolocation is cached in these tests")


def _config(tmp_path, platforms, viirs_product=None, raw=None):
    aoi_path = tmp_path / "aoi.geojson"
    aoi_path.write_text(json.dumps({"type": "FeatureCollection", "features": [{"type": "Feature", "properties": {}, "geometry": AOI}]}))
    sr = {"viirs_platforms": platforms, "viirs_l2_raw_dir": str(raw or tmp_path / "raw")}
    if viirs_product:
        sr["viirs_product"] = viirs_product
    return {"geojson": str(aoi_path), "startday": DAY, "endday": DAY, "satellite": ["viirs"], "activefire": "none",
            "output": str(tmp_path / "out"), "surface_reflectance": sr}


def _run_l2(tmp_path, platforms, entries, viirs_product=None):
    with patch.object(viirs_l2, "LAADSClient", lambda: FakeLAADS(entries)):
        return pipeline.run_pipeline(_config(tmp_path, platforms, viirs_product), tmp_path)


def _noaa20_entry(srp):
    return {viirs_l2.PLATFORMS["noaa20"].sr_concept_id: [(srp.name, "https://example.invalid/" + srp.name, "DAY")]}


def _bad_snpp_entry():
    name = "VNP09.A2024070.0348.002.2024070133120.hdf"
    return {viirs_l2.PLATFORMS["snpp"].sr_concept_id: [(name, "https://example.invalid/" + name, "DAY")]}


def _statuses(result, platform):
    return [o["aoi_status"] for d in result["viirs_l2_swath"][platform]["days"] for o in d.get("overpasses", [])]


def test_default_l2_swath_success(tmp_path):
    srp = make_granule(tmp_path / "raw")
    result = _run_l2(tmp_path, ["noaa20"], _noaa20_entry(srp))
    assert "viirs_l2_swath" in result and "viirs_surface_reflectance" not in result     # default product = l2_swath
    assert _statuses(result, "noaa20") == ["OBSERVED"]
    assert run.count_failed_dates(result) == 0
    assert run.summarize_region_result(result)["status"] == "success"


def test_default_l2_swath_failure_is_counted(tmp_path):
    result = _run_l2(tmp_path, ["snpp"], _bad_snpp_entry())
    assert result["viirs_l2_swath"]["snpp"]["failed"][0]["date"] == "2024-03-10"
    s = run.summarize_region_result(result)
    assert s["failed_dates"] == 1 and s["status"] == "partial"


def test_explicit_l2_swath_failure_is_counted(tmp_path):
    result = _run_l2(tmp_path, ["snpp"], _bad_snpp_entry(), viirs_product="l2_swath")
    assert run.count_failed_dates(result) == 1


def test_legacy_ga_failure_is_counted_as_before(tmp_path):
    class Granule:
        def data_links(self):
            return ["https://example.invalid/VNP09GA.A2024070.h28v05.002.2024071135325.h5"]

    with patch("src.surface_reflectance._earthdata_login", return_value=None), \
         patch("src.surface_reflectance._search_granules", return_value=[Granule()]), \
         patch("src.surface_reflectance._download_granules", side_effect=RuntimeError("download failed")):
        result = pipeline.run_pipeline(_config(tmp_path, ["snpp"], "daily_l2g_legacy"), tmp_path)
    assert "viirs_surface_reflectance" in result and "viirs_l2_swath" not in result
    assert result["viirs_surface_reflectance"]["snpp"]["dates_failed"][0]["date"] == DAY
    s = run.summarize_region_result(result)
    assert s["failed_dates"] == 1 and s["status"] == "partial"


def test_aoi_outside_swath_is_not_a_failure(tmp_path):
    srp = make_granule(tmp_path / "raw", lat0=34.5)        # swath far north of the AOI
    result = _run_l2(tmp_path, ["noaa20"], _noaa20_entry(srp))
    assert _statuses(result, "noaa20") == ["AOI_OUTSIDE_SWATH"]
    assert run.count_failed_dates(result) == 0 and run.summarize_region_result(result)["status"] == "success"


def test_partial_observation_is_not_a_failure(tmp_path):
    srp = make_granule(tmp_path / "raw", lat0=33.0)        # one scan covers only part of the AOI
    result = _run_l2(tmp_path, ["noaa20"], _noaa20_entry(srp))
    assert _statuses(result, "noaa20") == ["PARTIAL_OBSERVATION"]
    assert run.count_failed_dates(result) == 0 and run.summarize_region_result(result)["status"] == "success"


def test_one_platform_failing_makes_region_partial(tmp_path):
    """Current semantics: failed dates are summed over all platforms; any failure -> region status 'partial'."""
    srp = make_granule(tmp_path / "raw")
    entries = {**_noaa20_entry(srp), **_bad_snpp_entry()}
    result = _run_l2(tmp_path, ["snpp", "noaa20"], entries)
    assert result["viirs_l2_swath"]["noaa20"]["failed"] == [] and len(result["viirs_l2_swath"]["snpp"]["failed"]) == 1
    assert _statuses(result, "noaa20") == ["OBSERVED"]
    s = run.summarize_region_result(result)
    assert s == {"status": "partial", "failed_dates": 1, "activefire": None}


def test_modis_counting_unchanged_and_non_platform_keys_ignored():
    result = {"modis_surface_reflectance": {"terra": {"dates_failed": [{"date": "20240310"}]}, "aqua": {"dates_failed": []}},
              "viirs_l2_swath": {"grid": {"crs": "EPSG:32652"}, "raw_dir": "/x", "noaa21": {"days": [], "failed": [{"date": "2024-03-10"}]}},
              "gcomc_surface_reflectance": {"dates_failed": [{"date": "2024-03-10"}]}}
    assert run.count_failed_dates(result) == 2      # MODIS 1 + L2 1; GCOM-C not part of the batch count (unchanged)
