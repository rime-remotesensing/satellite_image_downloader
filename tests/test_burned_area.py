from __future__ import annotations

from datetime import date

import numpy as np
import pytest

from src.burned_area import (
    _month_bounds,
    _months_in_range,
    _month_layer_to_period_layer,
    _requested_doy_range_for_month,
    _union_period_layers,
)
from src.config import _normalize_buffer_km, _normalize_burned_area_products
from src.constants import BURNED_AREA_OUTPUT_NODATA


# ---------------------------------------------------------------------------
# buffer_km
# ---------------------------------------------------------------------------

def test_buffer_km_omitted_is_zero():
    assert _normalize_buffer_km(None) == 0.0


def test_buffer_km_zero_matches_omitted():
    assert _normalize_buffer_km(0) == _normalize_buffer_km(None) == 0.0


def test_buffer_km_positive_float_accepted():
    assert _normalize_buffer_km(10) == 10.0
    assert _normalize_buffer_km(2.5) == 2.5


def test_buffer_km_negative_raises():
    with pytest.raises(ValueError):
        _normalize_buffer_km(-1)


def test_buffer_km_expands_bbox_by_10km(monkeypatch=None):
    from src.geometry import _expand_bbox_by_meters

    original_bbox = (131.0, 33.0, 131.02, 33.02)
    buffer_km = _normalize_buffer_km(10)
    expanded = _expand_bbox_by_meters(original_bbox, buffer_km * 1000.0)

    assert expanded[0] < original_bbox[0]
    assert expanded[1] < original_bbox[1]
    assert expanded[2] > original_bbox[2]
    assert expanded[3] > original_bbox[3]

    # ~10km in degrees at this latitude
    meters_per_degree_lat = 111320.0
    expected_dlat = 10_000 / meters_per_degree_lat
    assert expanded[1] == pytest.approx(original_bbox[1] - expected_dlat, rel=1e-6)


# ---------------------------------------------------------------------------
# burned_area product list
# ---------------------------------------------------------------------------

def test_burned_area_unset_or_empty_is_noop():
    assert _normalize_burned_area_products(None) == []
    assert _normalize_burned_area_products([]) == []


def test_burned_area_case_insensitive_normalization():
    assert _normalize_burned_area_products(["mcd64a1", "vnp64a1"]) == ["MCD64A1", "VNP64A1"]


def test_burned_area_unknown_product_rejected():
    with pytest.raises(ValueError):
        _normalize_burned_area_products(["MCD12Q1"])


# ---------------------------------------------------------------------------
# Month enumeration
# ---------------------------------------------------------------------------

def test_single_month_period():
    months = _months_in_range(date(2024, 2, 12), date(2024, 2, 20))
    assert months == [(2024, 2)]


def test_cross_month_period():
    months = _months_in_range(date(2024, 2, 12), date(2024, 3, 13))
    assert months == [(2024, 2), (2024, 3)]


def test_cross_year_period():
    months = _months_in_range(date(2023, 12, 20), date(2024, 1, 10))
    assert months == [(2023, 12), (2024, 1)]


def test_leap_year_february_has_29_days():
    assert _month_bounds(2024, 2) == (date(2024, 2, 1), date(2024, 2, 29))
    assert _month_bounds(2023, 2) == (date(2023, 2, 1), date(2023, 2, 28))


# ---------------------------------------------------------------------------
# DOY range clipping
# ---------------------------------------------------------------------------

def test_doy_range_within_single_month():
    # Feb 12 2024 is DOY 43 (leap year: Jan=31 days); Feb 29 is DOY 60.
    lo, hi = _requested_doy_range_for_month(2024, 2, date(2024, 2, 12), date(2024, 3, 13))
    assert lo == date(2024, 2, 12).timetuple().tm_yday
    assert hi == date(2024, 2, 29).timetuple().tm_yday  # clipped to month end


def test_doy_range_second_month_starts_at_month_start():
    lo, hi = _requested_doy_range_for_month(2024, 3, date(2024, 2, 12), date(2024, 3, 13))
    assert lo == date(2024, 3, 1).timetuple().tm_yday
    assert hi == date(2024, 3, 13).timetuple().tm_yday


def test_doy_range_cross_year_second_year_starts_at_jan_1():
    lo, hi = _requested_doy_range_for_month(2024, 1, date(2023, 12, 20), date(2024, 1, 10))
    assert lo == 1  # Jan 1
    assert hi == date(2024, 1, 10).timetuple().tm_yday


def test_leap_year_doy_after_feb29_is_correct():
    # March 1 in a leap year is DOY 61, not 60.
    assert date(2024, 3, 1).timetuple().tm_yday == 61
    assert date(2023, 3, 1).timetuple().tm_yday == 60


# ---------------------------------------------------------------------------
# Month layer -> period layer (Burn Date DOY filtering)
# ---------------------------------------------------------------------------

def _month_result(burn_date: np.ndarray, fill=-1, water=-2, outside=None):
    if outside is None:
        outside = np.zeros(burn_date.shape, dtype=bool)
    return {"burn_date": burn_date, "fill_value": fill, "water_value": water, "outside_bbox_mask": outside}


def test_burn_date_within_requested_range_is_1():
    burn_date = np.array([[43, 50, 60]], dtype=np.int16)  # DOYs
    result = _month_result(burn_date)
    layer = _month_layer_to_period_layer(result, doy_lo=43, doy_hi=50)
    assert layer.tolist() == [[1, 1, 0]]


def test_burn_date_outside_requested_range_is_excluded():
    burn_date = np.array([[10, 43, 100]], dtype=np.int16)
    result = _month_result(burn_date)
    layer = _month_layer_to_period_layer(result, doy_lo=43, doy_hi=43)
    assert layer.tolist() == [[0, 1, 0]]


def test_fill_and_water_pixels_are_nodata():
    burn_date = np.array([[-1, -2, 43]], dtype=np.int16)
    result = _month_result(burn_date)
    layer = _month_layer_to_period_layer(result, doy_lo=43, doy_hi=43)
    assert layer.tolist() == [[BURNED_AREA_OUTPUT_NODATA, BURNED_AREA_OUTPUT_NODATA, 1]]


def test_outside_aoi_bbox_is_nodata_even_if_burned_in_range():
    burn_date = np.array([[43]], dtype=np.int16)
    outside = np.array([[True]])
    result = _month_result(burn_date, outside=outside)
    layer = _month_layer_to_period_layer(result, doy_lo=43, doy_hi=43)
    assert layer.tolist() == [[BURNED_AREA_OUTPUT_NODATA]]


# ---------------------------------------------------------------------------
# Union across months
# ---------------------------------------------------------------------------

def test_union_same_pixel_burned_in_multiple_months_stays_1():
    feb_layer = np.array([[1, 0, 255]], dtype=np.uint8)
    mar_layer = np.array([[1, 1, 255]], dtype=np.uint8)
    result = _union_period_layers([feb_layer, mar_layer])
    assert result.tolist() == [[1, 1, 255]]


def test_union_one_valid_observation_beats_nodata():
    feb_layer = np.array([[255, 0]], dtype=np.uint8)
    mar_layer = np.array([[0, 255]], dtype=np.uint8)
    result = _union_period_layers([feb_layer, mar_layer])
    assert result.tolist() == [[0, 0]]


def test_union_burn_wins_over_no_burn():
    feb_layer = np.array([[0]], dtype=np.uint8)
    mar_layer = np.array([[1]], dtype=np.uint8)
    result = _union_period_layers([feb_layer, mar_layer])
    assert result.tolist() == [[1]]


def test_union_all_nodata_stays_nodata():
    feb_layer = np.array([[255]], dtype=np.uint8)
    mar_layer = np.array([[255]], dtype=np.uint8)
    result = _union_period_layers([feb_layer, mar_layer])
    assert result.tolist() == [[255]]


def test_fetch_month_granules_discards_cmr_temporal_overlap_from_other_months(monkeypatch):
    """Regression test: a real CMR query for a Feb-1 monthly granule was
    confirmed to also return the Jan-1 granule (temporal overlap in CMR's
    own indexing), which would silently contaminate the mosaic with the
    wrong month's Burn Date values if not filtered out."""
    from src.burned_area import _fetch_month_granules

    class _FakeGranule:
        def __init__(self, filename):
            self._filename = filename

        def data_links(self):
            return [f"https://example.com/{self._filename}"]

    jan_granule = _FakeGranule("VNP64A1.A2024001.h28v05.002.2024065194431.hdf")
    feb_granule = _FakeGranule("VNP64A1.A2024032.h28v05.002.2024115125438.hdf")

    monkeypatch.setattr("src.burned_area._search_granules", lambda **kwargs: [jan_granule, feb_granule])
    monkeypatch.setattr("src.burned_area.call_with_network_retry", lambda fn, service=None: fn())

    matched = _fetch_month_granules("VNP64A1", "002", (0, 0, 1, 1), 2024, 2)
    assert len(matched) == 1
    assert matched[0] is feb_granule


def test_mcd64a1_and_vnp64a1_are_processed_independently(monkeypatch):
    from src.burned_area import _process_burned_area

    calls = []

    def fake_product(**kwargs):
        calls.append(kwargs["product_key"])
        return {"product": kwargs["product_key"], "status": "success"}

    monkeypatch.setattr("src.burned_area._process_burned_area_product", fake_product)

    result = _process_burned_area(
        config={},
        config_dir=None,
        output_root=None,
        geometry_wgs84={},
        start_date=date(2024, 2, 1),
        end_date=date(2024, 2, 29),
        products=["MCD64A1", "VNP64A1"],
    )

    assert calls == ["MCD64A1", "VNP64A1"]
    assert set(result.keys()) == {"MCD64A1", "VNP64A1"}
    assert result["MCD64A1"] != result["VNP64A1"] or result["MCD64A1"]["product"] != result["VNP64A1"]["product"]
