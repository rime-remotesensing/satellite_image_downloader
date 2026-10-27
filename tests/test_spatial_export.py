from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from src.config import _normalize_spatial_export


# ---------------------------------------------------------------------------
# config.surface_reflectance.spatial_export
# ---------------------------------------------------------------------------

def test_omitted_is_pure_noop():
    assert _normalize_spatial_export(None) == {"mode": "aoi_clip", "halo_m": None}


def test_explicit_aoi_clip_mode():
    assert _normalize_spatial_export({"mode": "aoi_clip"}) == {"mode": "aoi_clip", "halo_m": None}


def test_native_rect_halo_with_halo_m():
    assert _normalize_spatial_export({"mode": "native_rect_halo", "halo_m": 16000}) == {
        "mode": "native_rect_halo",
        "halo_m": 16000.0,
    }


def test_unknown_mode_rejected():
    with pytest.raises(ValueError):
        _normalize_spatial_export({"mode": "something_else"})


def test_negative_halo_m_rejected():
    with pytest.raises(ValueError):
        _normalize_spatial_export({"mode": "native_rect_halo", "halo_m": -1})


def test_non_mapping_rejected():
    with pytest.raises(ValueError):
        _normalize_spatial_export("native_rect_halo")


# ---------------------------------------------------------------------------
# MODIS/legacy-VIIRS: _process_date_for_platform mask behavior
#
# Uses a real full-resolution (2400x2400) native tile array and a ~15km AOI
# rectangle -- the same scale as the real reported bug (32x32 patch windows)
# -- because at a too-coarse fake resolution a simple axis-aligned AOI
# rectangle never actually triggers the diagonal-edge masking effect being
# tested for.
# ---------------------------------------------------------------------------

def _run_modis_date(
    tmp_path, monkeypatch, *, aoi, halo_m=0.0, mask_outside_aoi=True, tag="run"
):
    import numpy as np

    from src import surface_reflectance as sr

    array = np.full((2400, 2400), 1000, dtype=np.int16)

    def fake_read_sds(path, field_name):
        return sr._SDSData(
            array=array, scale=0.0001, offset=0.0, fill_value=-28672,
            valid_range=(-100, 16000), sds_path=field_name,
        )

    monkeypatch.setattr(sr, "_read_sds", fake_read_sds)
    monkeypatch.setattr(sr, "_parse_granule_filename", lambda name: (date(2024, 2, 12), "h28v05"))
    monkeypatch.setattr(sr, "_FIELD_SPECS", {"modis": [("sur_refl_b01", "sr")]})

    out_dir = tmp_path / tag
    granule_files = [tmp_path / f"{tag}.hdf"]
    granule_files[0].touch()

    return sr._process_date_for_platform(
        satellite_key="modis",
        platform_key="terra",
        short_name="MOD09GA",
        version="061",
        granule_files=granule_files,
        acq_date=date(2024, 2, 12),
        out_platform_dir=out_dir,
        clip_to_aoi=True,
        aoi_geom_wgs84=aoi,
        file_exists_mode="overwrite",
        halo_m=halo_m,
        mask_outside_aoi=mask_outside_aoi,
    )


# ~15km x 15km AOI near Kyushu, Japan (same scale as the real 32x32-patch bug).
_AOI_15KM = {
    "type": "Polygon",
    "coordinates": [[[131.0, 33.0], [131.15, 33.0], [131.15, 33.15], [131.0, 33.15], [131.0, 33.0]]],
}


def test_aoi_clip_mode_still_masks_outside_aoi(tmp_path, monkeypatch):
    """Default behavior (mode omitted) must be byte-identical to before:
    pixels outside the AOI polygon within the crop window are NaN'd."""
    import numpy as np
    import rasterio

    result = _run_modis_date(tmp_path, monkeypatch, aoi=_AOI_15KM, tag="aoi_clip")

    out_path = Path(result["files"][0])
    with rasterio.open(out_path) as ds:
        data = ds.read(1)
    assert np.isnan(data).any(), "aoi_clip mode must still NaN pixels outside the AOI polygon"


def test_native_rect_halo_mode_keeps_all_native_pixels(tmp_path, monkeypatch):
    """native_rect_halo must not NaN any pixel inside the cropped window just
    because it's outside the AOI polygon -- only the sensor's own fill value
    may produce NaN (there is none in this fake array)."""
    import numpy as np
    import rasterio

    result = _run_modis_date(
        tmp_path, monkeypatch, aoi=_AOI_15KM, halo_m=5000.0, mask_outside_aoi=False, tag="native_halo"
    )

    out_path = Path(result["files"][0])
    with rasterio.open(out_path) as ds:
        data = ds.read(1)
        tags = ds.tags(1)
    assert not np.isnan(data).any(), "native_rect_halo must preserve every real native pixel (no fill value present)"
    assert tags.get("scale_factor") not in (None, "")


def test_halo_m_widens_the_cropped_window(tmp_path, monkeypatch):
    """A larger halo_m must produce a larger (or equal) output raster."""
    import rasterio

    small_result = _run_modis_date(
        tmp_path, monkeypatch, aoi=_AOI_15KM, halo_m=0.0, mask_outside_aoi=False, tag="small"
    )
    large_result = _run_modis_date(
        tmp_path, monkeypatch, aoi=_AOI_15KM, halo_m=8000.0, mask_outside_aoi=False, tag="large"
    )

    with rasterio.open(small_result["files"][0]) as ds:
        small_size = ds.width * ds.height
    with rasterio.open(large_result["files"][0]) as ds:
        large_size = ds.width * ds.height

    assert large_size > small_size
