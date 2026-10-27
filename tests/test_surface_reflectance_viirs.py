from __future__ import annotations

import pytest

from src.constants import VIIRS_ALL_PLATFORMS, VIIRS_DEFAULT_PLATFORMS, VIIRS_SDS_PATH_MAP
from src.surface_reflectance import _normalize_viirs_platforms, _process_viirs_surface_reflectance


def test_default_platform_selection_is_snpp_only_for_backward_compatibility():
    assert VIIRS_DEFAULT_PLATFORMS == ["snpp"]
    assert _normalize_viirs_platforms(None) == ["snpp"]


def test_explicit_platform_selection_list():
    assert _normalize_viirs_platforms(["snpp", "noaa20", "noaa21"]) == ["snpp", "noaa20", "noaa21"]
    assert _normalize_viirs_platforms(["NOAA20"]) == ["noaa20"]
    assert _normalize_viirs_platforms("snpp,noaa21") == ["snpp", "noaa21"]


def test_unknown_platform_is_rejected():
    with pytest.raises(ValueError):
        _normalize_viirs_platforms(["noaa19"])


def test_empty_platform_list_is_rejected():
    with pytest.raises(ValueError):
        _normalize_viirs_platforms([])


def test_duplicate_platforms_are_deduped():
    assert _normalize_viirs_platforms(["snpp", "snpp"]) == ["snpp"]


def test_all_platforms_share_one_sds_path_map():
    # Verified via real h5py inspection of VNP09GA/VJ109GA/VJ209GA granules
    # (see src/constants.py comment): all three platforms use the identical
    # on-disk dataset layout, so there is intentionally one shared map, not
    # per-platform copies.
    for field in ("I1", "I2", "I3", "M1", "M11", "QF7", "land_water_mask"):
        assert field in VIIRS_SDS_PATH_MAP
    assert set(VIIRS_ALL_PLATFORMS) == {"snpp", "noaa20", "noaa21"}


def test_old_snpp_only_config_dispatches_exactly_one_platform_call(monkeypatch):
    calls = []

    def fake_process_platform(**kwargs):
        calls.append(kwargs["platform_key"])
        return {"platform": kwargs["platform_key"], "short_name": kwargs["short_name"]}

    monkeypatch.setattr("src.surface_reflectance._process_platform", fake_process_platform)

    result = _process_viirs_surface_reflectance(
        config={},  # no surface_reflectance block at all -- the old-style config
        config_dir=None,
        output_root=None,
        geometry_wgs84={},
        bbox=(0, 0, 1, 1),
        start_date=None,
        end_date=None,
    )

    assert calls == ["snpp"]
    assert set(result.keys()) == {"snpp"}
    assert result["snpp"]["short_name"] == "VNP09GA"


def test_explicit_three_platform_config_dispatches_correct_products(monkeypatch):
    calls = {}

    def fake_process_platform(**kwargs):
        calls[kwargs["platform_key"]] = kwargs["short_name"]
        return {}

    monkeypatch.setattr("src.surface_reflectance._process_platform", fake_process_platform)

    config = {
        "surface_reflectance": {
            "viirs_platforms": ["snpp", "noaa20", "noaa21"],
        }
    }
    _process_viirs_surface_reflectance(
        config=config,
        config_dir=None,
        output_root=None,
        geometry_wgs84={},
        bbox=(0, 0, 1, 1),
        start_date=None,
        end_date=None,
    )

    assert calls == {"snpp": "VNP09GA", "noaa20": "VJ109GA", "noaa21": "VJ209GA"}
