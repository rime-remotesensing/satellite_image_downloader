from __future__ import annotations

import logging

from src.activefire import _resolve_firms_product_map
from src.constants import FIRMS_SOURCE_BY_LEVEL


def test_sp_sources_match_documented_platform_support():
    sp = FIRMS_SOURCE_BY_LEVEL["sp"]
    assert sp["viirs"]["snpp"] == "VIIRS_SNPP_SP"
    assert sp["viirs"]["noaa20"] == "VIIRS_NOAA20_SP"
    assert sp["viirs"]["noaa21"] is None  # NOAA-21 SP does not exist at FIRMS
    assert sp["modis"]["modis"] == "MODIS_SP"


def test_nrt_sources_cover_all_three_viirs_platforms():
    nrt = FIRMS_SOURCE_BY_LEVEL["nrt"]
    assert nrt["viirs"]["snpp"] == "VIIRS_SNPP_NRT"
    assert nrt["viirs"]["noaa20"] == "VIIRS_NOAA20_NRT"
    assert nrt["viirs"]["noaa21"] == "VIIRS_NOAA21_NRT"


def test_sp_product_map_skips_noaa21_with_warning_and_no_fallback(caplog):
    with caplog.at_level(logging.WARNING):
        product_map = _resolve_firms_product_map("sp")

    assert "VIIRS_NOAA20_SP" in product_map["viirs"]
    assert "VIIRS_SNPP_SP" in product_map["viirs"]
    # NOAA-21 must be absent, not silently replaced by VIIRS_NOAA21_NRT.
    assert "VIIRS_NOAA21_NRT" not in product_map["viirs"]
    assert not any(src.startswith("VIIRS_NOAA21") for src in product_map["viirs"])
    assert any("NOAA-21" in message or "noaa21" in message for message in caplog.messages)


def test_nrt_product_map_includes_all_three_viirs_platforms():
    product_map = _resolve_firms_product_map("nrt")
    assert set(product_map["viirs"]) == {"VIIRS_SNPP_NRT", "VIIRS_NOAA20_NRT", "VIIRS_NOAA21_NRT"}


def test_sp_product_map_has_no_silent_nrt_fallback_for_any_platform(caplog):
    with caplog.at_level(logging.WARNING):
        product_map = _resolve_firms_product_map("sp")
    for source in product_map["viirs"]:
        assert not source.endswith("_NRT")
