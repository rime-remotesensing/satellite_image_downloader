from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from src.pipeline import run_pipeline

GEOJSON = Path("config/no1.geojson")


def _base_config(**overrides):
    config = {
        "geojson": str(GEOJSON),
        "startday": "20240212",
        "endday": "20240212",
        "satellite": ["modis"],
    }
    config.update(overrides)
    return config


def test_buffer_km_omitted_matches_zero_exactly():
    captured = {}

    def fake_modis(**kwargs):
        captured["omitted"] = (kwargs["geometry_wgs84"], kwargs["bbox"])
        return {}

    with patch("src.pipeline._process_modis_surface_reflectance", side_effect=fake_modis):
        run_pipeline(config=_base_config(), config_dir=Path.cwd())
    geom_omitted, bbox_omitted = captured["omitted"]

    with patch("src.pipeline._process_modis_surface_reflectance", side_effect=fake_modis):
        run_pipeline(config=_base_config(buffer_km=0), config_dir=Path.cwd())
    geom_zero, bbox_zero = captured["omitted"]

    assert geom_omitted == geom_zero
    assert bbox_omitted == bbox_zero


def test_buffer_km_zero_uses_original_geometry_object_unchanged():
    from src.geometry import _load_aoi_geometry, _bbox_from_geometry

    original_geometry = _load_aoi_geometry(GEOJSON)
    original_bbox = _bbox_from_geometry(original_geometry)

    captured = {}

    def fake_modis(**kwargs):
        captured["geometry"] = kwargs["geometry_wgs84"]
        captured["bbox"] = kwargs["bbox"]
        return {}

    with patch("src.pipeline._process_modis_surface_reflectance", side_effect=fake_modis):
        run_pipeline(config=_base_config(), config_dir=Path.cwd())

    assert captured["geometry"] == original_geometry
    assert captured["bbox"] == pytest.approx(original_bbox)


def test_buffer_km_10_expands_bbox_passed_to_each_product():
    from src.geometry import _load_aoi_geometry, _bbox_from_geometry, _expand_bbox_by_meters

    original_geometry = _load_aoi_geometry(GEOJSON)
    original_bbox = _bbox_from_geometry(original_geometry)
    expected_bbox = _expand_bbox_by_meters(original_bbox, 10_000.0)

    captured = {}

    def fake_modis(**kwargs):
        captured["bbox"] = kwargs["bbox"]
        captured["geometry"] = kwargs["geometry_wgs84"]
        return {}

    with patch("src.pipeline._process_modis_surface_reflectance", side_effect=fake_modis):
        result = run_pipeline(config=_base_config(buffer_km=10), config_dir=Path.cwd())

    assert captured["bbox"] == pytest.approx(expected_bbox)
    assert captured["bbox"] != pytest.approx(original_bbox)
    # effective geometry is a rectangle built from the buffered bbox, not the
    # original (possibly non-rectangular) AOI polygon reused verbatim.
    from src.geometry import _bbox_from_geometry as bbox_of

    assert bbox_of(captured["geometry"]) == pytest.approx(expected_bbox)

    assert result["config"]["buffer_km"] == 10.0
    assert result["config"]["original_bbox"] == pytest.approx(list(original_bbox))
    assert result["config"]["effective_bbox"] == pytest.approx(list(expected_bbox))


def test_negative_buffer_km_raises():
    with pytest.raises(ValueError):
        run_pipeline(config=_base_config(buffer_km=-5), config_dir=Path.cwd())


def test_activefire_keeps_using_original_geometry_even_with_buffer_km(monkeypatch):
    from src.geometry import _load_aoi_geometry, _bbox_from_geometry

    original_geometry = _load_aoi_geometry(GEOJSON)
    original_bbox = _bbox_from_geometry(original_geometry)

    captured = {}

    def fake_activefire(**kwargs):
        captured["geometry"] = kwargs["geometry_wgs84"]
        captured["bbox"] = kwargs["bbox"]
        return {}

    config = _base_config(buffer_km=10, satellite=[], activefire="SP")
    with patch("src.pipeline._process_activefire", side_effect=fake_activefire):
        run_pipeline(config=config, config_dir=Path.cwd())

    assert captured["geometry"] == original_geometry
    assert captured["bbox"] == pytest.approx(original_bbox)


def test_burned_area_unset_does_not_invoke_worker():
    with patch("src.pipeline._process_burned_area") as worker:
        run_pipeline(config=_base_config(satellite=[]), config_dir=Path.cwd())
    assert not worker.called


def test_burned_area_uses_combined_range_not_per_window_for_multiple_date_windows():
    """startday/endday given as parallel arrays (multiple windows) must
    produce ONE burned-area call spanning min(startday)..max(endday), not a
    separate call per window (each window is a single day here)."""
    calls = []

    def fake_burned_area(**kwargs):
        calls.append((kwargs["start_date"], kwargs["end_date"]))
        return {}

    config = _base_config(
        satellite=[],
        burned_area=["MCD64A1"],
        startday=["20230121", "20230308"],
        endday=["20230121", "20230308"],
    )
    with patch("src.pipeline._process_burned_area", side_effect=fake_burned_area):
        result = run_pipeline(config=config, config_dir=Path.cwd())

    from datetime import date

    assert calls == [(date(2023, 1, 21), date(2023, 3, 8))]
    assert result["burned_area_range"] == {"startday": "20230121", "endday": "20230308"}


def test_burned_area_receives_effective_geometry_and_buffer_km():
    captured = {}

    def fake_burned_area(**kwargs):
        captured.update(kwargs)
        return {}

    config = _base_config(satellite=[], burned_area=["MCD64A1"], buffer_km=10)
    with patch("src.pipeline._process_burned_area", side_effect=fake_burned_area):
        run_pipeline(config=config, config_dir=Path.cwd())

    assert captured["products"] == ["MCD64A1"]
    assert captured["buffer_km"] == 10.0
