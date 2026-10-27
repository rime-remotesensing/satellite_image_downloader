from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from src.config import _extract_satellite_platform_overrides, _normalize_satellites
from src.surface_reflectance import _normalize_modis_platforms


# ---------------------------------------------------------------------------
# config.satellite qualified-token parsing
# ---------------------------------------------------------------------------

def test_bare_satellite_names_unaffected():
    assert _normalize_satellites(["modis", "viirs"]) == ["modis", "viirs"]
    assert _extract_satellite_platform_overrides(["modis", "viirs"]) == {}


def test_qualified_tokens_reduce_to_family_list():
    assert _normalize_satellites(["modis-aqua", "viirs-noaa20"]) == ["modis", "viirs"]


def test_qualified_tokens_produce_platform_overrides():
    overrides = _extract_satellite_platform_overrides(["modis-aqua", "viirs-noaa20"])
    assert overrides == {"modis": ["aqua"], "viirs": ["noaa20"]}


def test_multiple_platforms_for_same_family_accumulate():
    overrides = _extract_satellite_platform_overrides(["viirs-snpp", "viirs-noaa21"])
    assert overrides == {"viirs": ["snpp", "noaa21"]}
    assert _normalize_satellites(["viirs-snpp", "viirs-noaa21"]) == ["viirs"]


def test_mixed_bare_and_qualified_tokens():
    assert _normalize_satellites(["sentinel2", "modis-terra"]) == ["sentinel2", "modis"]
    assert _extract_satellite_platform_overrides(["sentinel2", "modis-terra"]) == {"modis": ["terra"]}


def test_unknown_family_rejected():
    with pytest.raises(ValueError):
        _normalize_satellites(["landsat9000"])


def test_unknown_platform_for_known_family_rejected():
    with pytest.raises(ValueError):
        _normalize_satellites(["viirs-noaa19"])
    with pytest.raises(ValueError):
        _normalize_satellites(["modis-suomi"])


def test_case_insensitive():
    assert _normalize_satellites(["MODIS-AQUA"]) == ["modis"]
    assert _extract_satellite_platform_overrides(["MODIS-AQUA"]) == {"modis": ["aqua"]}


def test_string_form_comma_separated():
    assert _normalize_satellites("modis-aqua,viirs-noaa20") == ["modis", "viirs"]
    assert _extract_satellite_platform_overrides("modis-aqua,viirs-noaa20") == {
        "modis": ["aqua"],
        "viirs": ["noaa20"],
    }


# ---------------------------------------------------------------------------
# MODIS platform selection (mirrors VIIRS's existing mechanism)
# ---------------------------------------------------------------------------

def test_modis_platforms_default_is_both_unconditionally():
    assert _normalize_modis_platforms(None) == ["terra", "aqua"]


def test_modis_platforms_explicit_subset():
    assert _normalize_modis_platforms(["aqua"]) == ["aqua"]


def test_modis_platforms_invalid_rejected():
    with pytest.raises(ValueError):
        _normalize_modis_platforms(["terra", "snpp"])


def test_modis_platforms_empty_rejected():
    with pytest.raises(ValueError):
        _normalize_modis_platforms([])


# ---------------------------------------------------------------------------
# Pipeline wiring: config.satellite qualifiers actually reach the workers
# ---------------------------------------------------------------------------

GEOJSON = Path("config/no1.geojson")


def _base_config(**overrides):
    config = {
        "geojson": str(GEOJSON),
        "startday": "20240212",
        "endday": "20240212",
    }
    config.update(overrides)
    return config


def test_pipeline_modis_aqua_qualifier_only_processes_aqua():
    from src.pipeline import run_pipeline

    captured = {}

    def fake_process_platform(**kwargs):
        captured.setdefault("platforms", []).append(kwargs["platform_key"])
        return {}

    with patch("src.surface_reflectance._process_platform", side_effect=fake_process_platform):
        run_pipeline(config=_base_config(satellite=["modis-aqua"]), config_dir=Path.cwd())

    assert captured["platforms"] == ["aqua"]


def test_pipeline_viirs_noaa20_qualifier_only_processes_noaa20_legacy_l2g():
    """surface_reflectance.viirs_product: daily_l2g_legacy routes through the
    legacy _process_platform worker; the qualifier override must still reach it."""
    from src.pipeline import run_pipeline

    captured = {}

    def fake_process_platform(**kwargs):
        captured.setdefault("platforms", []).append(kwargs["platform_key"])
        return {}

    config = _base_config(
        satellite=["viirs-noaa20"],
        surface_reflectance={"viirs_product": "daily_l2g_legacy"},
    )
    with patch("src.surface_reflectance._process_platform", side_effect=fake_process_platform):
        run_pipeline(config=config, config_dir=Path.cwd())

    assert captured["platforms"] == ["noaa20"]


def test_pipeline_viirs_noaa20_qualifier_reaches_default_l2_swath_path():
    """Default viirs_product is l2_swath; the qualifier override must reach
    _process_viirs_l2_swath via surface_reflectance.viirs_platforms, same as
    the legacy path."""
    from src.pipeline import run_pipeline

    captured = {}

    def fake_process_viirs_l2_swath(**kwargs):
        captured["sr_cfg"] = kwargs["config"].get("surface_reflectance", {})
        return {}

    with patch("src.viirs_l2._process_viirs_l2_swath", side_effect=fake_process_viirs_l2_swath):
        run_pipeline(config=_base_config(satellite=["viirs-noaa20"]), config_dir=Path.cwd())

    assert captured["sr_cfg"].get("viirs_platforms") == ["noaa20"]


def test_pipeline_bare_modis_still_processes_both_platforms():
    from src.pipeline import run_pipeline

    captured = {}

    def fake_process_platform(**kwargs):
        captured.setdefault("platforms", []).append(kwargs["platform_key"])
        return {}

    with patch("src.surface_reflectance._process_platform", side_effect=fake_process_platform):
        run_pipeline(config=_base_config(satellite=["modis"]), config_dir=Path.cwd())

    assert sorted(captured["platforms"]) == ["aqua", "terra"]
