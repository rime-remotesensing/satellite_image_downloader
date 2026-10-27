"""MCD64A1 / VNP64A1 monthly burned-area products.

MCD64A1 (MODIS, .061) and VNP64A1 (VIIRS, .002) are NOT daily products --
each granule is a monthly-tiled 500m native Sinusoidal file whose "Burn
Date" SDS stores, per pixel, the ordinal day-of-year (DOY) the pixel burned
within that calendar month (0 = valid land/no burn that month, 1-366 = DOY,
-1 = fill/unmapped, -2 = water; confirmed by direct pyhdf inspection of real
granules -- see src/constants.py). To honor an arbitrary [startday, endday]
request, this module downloads every calendar month the range overlaps,
converts each month's Burn Date DOY values to actual calendar dates, keeps
only pixels whose date falls in [startday, endday], and unions the result
(binary OR) across all months into one period layer per product.

This module reuses -- and does not duplicate -- the MODIS/VIIRS Sinusoidal
grid, mosaic, native-pixel crop, and GeoTIFF-writing logic already in
src/surface_reflectance.py (which is intentionally not modified here).
"""

from __future__ import annotations

import calendar
import logging
import shutil
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import rasterio

from .config import _as_bool, _resolve_runtime_path
from .constants import BURNED_AREA_FILL_VALUE, BURNED_AREA_OUTPUT_NODATA, BURNED_AREA_PRODUCTS, BURNED_AREA_WATER_VALUE
from .network_retry import call_with_network_retry
from .surface_reflectance import (
    _aoi_bbox_geometry_in_crs,
    _apply_window,
    _attr_float,
    _attr_range,
    _crop_window,
    _download_granules,
    _earthdata_login,
    _granule_filename,
    _mosaic_tile_arrays,
    _parse_granule_filename,
    _search_granules,
    _sinusoidal_crs,
    _tile_transform,
    _write_geotiff_group,
)

LOGGER = logging.getLogger(__name__)

_BURN_DATE_FIELD = "Burn Date"


# ---------------------------------------------------------------------------
# Month / DOY arithmetic
# ---------------------------------------------------------------------------

def _months_in_range(start_date: date, end_date: date) -> List[Tuple[int, int]]:
    if end_date < start_date:
        raise ValueError("end_date must be >= start_date")

    months: List[Tuple[int, int]] = []
    year, month = start_date.year, start_date.month
    while (year, month) <= (end_date.year, end_date.month):
        months.append((year, month))
        month += 1
        if month > 12:
            month = 1
            year += 1
    return months


def _month_bounds(year: int, month: int) -> Tuple[date, date]:
    last_day = calendar.monthrange(year, month)[1]
    return date(year, month, 1), date(year, month, last_day)


def _requested_doy_range_for_month(
    year: int, month: int, start_date: date, end_date: date
) -> Tuple[int, int]:
    """The [start_date, end_date] request, clipped to this month, as
    ordinal day-of-year bounds within `year` (leap-year correct via
    Python's own date arithmetic -- never computed by hand)."""
    month_start, month_end = _month_bounds(year, month)
    clipped_start = max(start_date, month_start)
    clipped_end = min(end_date, month_end)
    return clipped_start.timetuple().tm_yday, clipped_end.timetuple().tm_yday


# ---------------------------------------------------------------------------
# HDF4 "Burn Date" SDS read (generic; mirrors surface_reflectance's own
# hdf4 reader but resolves the on-disk SDS name directly, since HDF4 burned
# area products name it identically to the canonical field, unlike the
# suffixed MODIS SR names surface_reflectance.py's own map handles).
# ---------------------------------------------------------------------------

def _read_burned_area_sds(hdf_path: Path, field_name: str) -> Tuple[np.ndarray, Optional[float], Optional[float]]:
    from pyhdf.SD import SD, SDC

    hdf = SD(str(hdf_path), SDC.READ)
    try:
        available = set(hdf.datasets().keys())
        if field_name not in available:
            raise ValueError(
                f"{hdf_path.name}: expected SDS '{field_name}' not found. "
                f"Available datasets: {sorted(available)}"
            )
        sds = hdf.select(field_name)
        try:
            array = sds[:]
            attrs = {str(k): v for k, v in sds.attributes().items()}
        finally:
            sds.endaccess()
    finally:
        hdf.end()

    lower_attrs = {k.lower(): v for k, v in attrs.items()}
    fill_value = _attr_float(lower_attrs, "_fillvalue")
    water_value = _attr_float(lower_attrs, "water")
    return array, fill_value, water_value


# ---------------------------------------------------------------------------
# Per-month processing
# ---------------------------------------------------------------------------

def _fetch_month_granules(
    short_name: str, version: str, bbox: Tuple[float, float, float, float], year: int, month: int
) -> List[Any]:
    """CMR search for exactly one monthly granule-date.

    CMR's temporal overlap match is not exact here: a single-day search
    window for e.g. 2024-02-01 was confirmed (via a real query) to also
    return the neighboring 2024-01-01 monthly granule, presumably because
    the granule's own indexed temporal extent spans past the single-day
    query window. So this never trusts "whatever CMR returned" as this
    month's granules -- it always re-parses each granule's own acquisition
    date from its filename and keeps only exact matches for `month_start`.
    """
    month_start, _month_end = _month_bounds(year, month)
    granules = call_with_network_retry(
        lambda: _search_granules(
            short_name=short_name,
            version=version,
            bbox=bbox,
            start_date=month_start,
            end_date=month_start,
        ),
        service="CMR",
    )

    matched = []
    for granule in granules:
        try:
            acq_date, _tile = _parse_granule_filename(_granule_filename(granule))
        except Exception as exc:
            LOGGER.warning("Skipping granule with unparsable filename: %s", exc)
            continue
        if acq_date == month_start:
            matched.append(granule)
        else:
            LOGGER.debug(
                "Discarding granule dated %s returned by a %s search (CMR temporal overlap, not this month)",
                acq_date,
                month_start,
            )
    return matched


def _process_month(
    *,
    short_name: str,
    granule_files: List[Path],
    year: int,
    month: int,
    crs: rasterio.crs.CRS,
    aoi_bbox_native: Dict[str, Any],
) -> Dict[str, Any]:
    """Mosaic + crop one month's Burn Date tiles; return a uint8 in-period
    layer for that month alone (1=burned in requested window, 0=valid land
    not burned in window, 255=fill/water/outside-AOI) plus its transform."""
    tile_entries: List[Tuple[np.ndarray, rasterio.Affine]] = []
    fill_value: Optional[float] = None
    water_value: Optional[float] = None

    for gfile in granule_files:
        _, tile = _parse_granule_filename(gfile.name)
        array, fill_value, water_value = _read_burned_area_sds(gfile, _BURN_DATE_FIELD)
        transform = _tile_transform(tile, array.shape[0], array.shape[1])
        tile_entries.append((array, transform))

    if fill_value is None:
        fill_value = float(BURNED_AREA_FILL_VALUE)
    if water_value is None:
        water_value = float(BURNED_AREA_WATER_VALUE)

    mosaic_array, mosaic_transform = _mosaic_tile_arrays(tile_entries, crs=crs, nodata=fill_value)
    height, width = mosaic_array.shape

    window, out_transform = _crop_window(mosaic_transform, height, width, aoi_bbox_native)
    burn_date = _apply_window(mosaic_array, window)

    outside_bbox_mask = _outside_bbox_mask(out_transform, burn_date.shape, aoi_bbox_native)

    return {
        "burn_date": burn_date,
        "transform": out_transform,
        "fill_value": fill_value,
        "water_value": water_value,
        "outside_bbox_mask": outside_bbox_mask,
    }


def _outside_bbox_mask(
    transform: rasterio.Affine, shape: Tuple[int, int], aoi_bbox_native: Dict[str, Any]
) -> np.ndarray:
    from rasterio.features import geometry_mask

    return geometry_mask(
        [aoi_bbox_native],
        out_shape=shape,
        transform=transform,
        invert=False,
        all_touched=True,
    )


def _month_layer_to_period_layer(
    month_result: Dict[str, Any], doy_lo: int, doy_hi: int
) -> np.ndarray:
    burn_date = month_result["burn_date"]
    fill_value = month_result["fill_value"]
    water_value = month_result["water_value"]
    outside_bbox = month_result["outside_bbox_mask"]

    layer = np.full(burn_date.shape, BURNED_AREA_OUTPUT_NODATA, dtype=np.uint8)

    valid_land = (burn_date != fill_value) & (burn_date != water_value) & ~outside_bbox
    burned_in_period = valid_land & (burn_date >= doy_lo) & (burn_date <= doy_hi)

    layer[valid_land] = 0
    layer[burned_in_period] = 1
    return layer


def _union_period_layers(layers: List[np.ndarray]) -> np.ndarray:
    """Binary OR across months: 1 wins over 0, 0 wins over 255 (nodata)."""
    shapes = {layer.shape for layer in layers}
    if len(shapes) > 1:
        raise RuntimeError(f"Inconsistent per-month burned-area layer shapes: {shapes}")

    result = np.full(layers[0].shape, BURNED_AREA_OUTPUT_NODATA, dtype=np.uint8)
    for layer in layers:
        result = np.where(result == 1, 1, np.where(layer == 1, 1, np.where((result == 0) | (layer == 0), 0, result)))
    return result.astype(np.uint8)


# ---------------------------------------------------------------------------
# Per-product orchestration
# ---------------------------------------------------------------------------

def _process_burned_area_product(
    *,
    config: Dict[str, Any],
    config_dir: Path,
    output_root: Path,
    geometry_wgs84: Dict[str, Any],
    start_date: date,
    end_date: date,
    product_key: str,
    buffer_km: float,
) -> Dict[str, Any]:
    spec = BURNED_AREA_PRODUCTS[product_key]
    short_name = spec["short_name"]
    version = spec["version"]
    satellite_key = spec["satellite_key"]

    ba_cfg = config.get("burned_area_options", {}) or {}
    keep_native_files = _as_bool(ba_cfg.get("keep_native_files"), default=False)

    out_dir = output_root / satellite_key / "burned_area"
    out_path = out_dir / f"{short_name}_{start_date.strftime('%Y%m%d')}_{end_date.strftime('%Y%m%d')}_burned.tif"

    file_exists_mode = str(config.get("file_exists", "skip")).strip().lower()
    if file_exists_mode == "skip" and out_path.exists():
        LOGGER.info("Skipping %s: output already exists", out_path)
        return {"product": short_name, "output": str(out_path), "status": "skipped"}

    try:
        _earthdata_login(config, config_dir)
    except Exception as exc:
        LOGGER.error("Earthdata authentication failed: %s", exc)
        return {"product": short_name, "error": str(exc)}

    crs = _sinusoidal_crs()
    aoi_bbox_native = _aoi_bbox_geometry_in_crs(geometry_wgs84, crs)
    from .geometry import _bbox_from_geometry

    bbox = _bbox_from_geometry(geometry_wgs84)

    months = _months_in_range(start_date, end_date)
    tmp_root = output_root / satellite_key / "burned_area" / "_tmp" / short_name
    tmp_root.mkdir(parents=True, exist_ok=True)

    month_layers: List[np.ndarray] = []
    tiles_used: set = set()
    source_granules: set = set()
    months_used: List[str] = []
    result_transform = None

    try:
        for year, month in months:
            try:
                granules = _fetch_month_granules(short_name, version, bbox, year, month)
            except Exception as exc:
                LOGGER.error("CMR search failed for %s %04d-%02d: %s", short_name, year, month, exc, exc_info=True)
                return {"product": short_name, "error": str(exc)}

            if not granules:
                LOGGER.warning("No %s granules found for %04d-%02d", short_name, year, month)
                continue

            tmp_dir = tmp_root / f"{year:04d}{month:02d}"
            try:
                local_files = call_with_network_retry(
                    lambda: _download_granules(granules, tmp_dir),
                    service="Earthdata-download",
                )
                for f in local_files:
                    source_granules.add(f.name)
                    _, tile = _parse_granule_filename(f.name)
                    tiles_used.add(tile)

                month_result = _process_month(
                    short_name=short_name,
                    granule_files=local_files,
                    year=year,
                    month=month,
                    crs=crs,
                    aoi_bbox_native=aoi_bbox_native,
                )
                doy_lo, doy_hi = _requested_doy_range_for_month(year, month, start_date, end_date)
                layer = _month_layer_to_period_layer(month_result, doy_lo, doy_hi)
                month_layers.append(layer)
                months_used.append(f"{year:04d}-{month:02d}")
                result_transform = month_result["transform"]
            finally:
                if not keep_native_files:
                    shutil.rmtree(tmp_dir, ignore_errors=True)
    finally:
        if not keep_native_files:
            shutil.rmtree(tmp_root, ignore_errors=True)

    if not month_layers:
        return {"product": short_name, "error": "No granules found for any month in the requested range"}

    period_layer = _union_period_layers(month_layers)
    burned_pixel_count = int(np.count_nonzero(period_layer == 1))

    dataset_tags = {
        "product": short_name,
        "version": version,
        "start_date": start_date.strftime("%Y%m%d"),
        "end_date": end_date.strftime("%Y%m%d"),
        "buffer_km": buffer_km,
        "months_used": ",".join(months_used),
        "tiles_used": ",".join(sorted(tiles_used)),
        "source_granules": ",".join(sorted(source_granules)),
        "burned_pixel_count": burned_pixel_count,
        "native_crs": crs.to_string(),
        "native_pixel_size_m": abs(result_transform.a),
        "raster_reprojected": False,
        "resampling": "none",
    }

    _write_geotiff_group(
        out_path,
        [("burned_period", period_layer)],
        crs=crs,
        transform=result_transform,
        dtype="uint8",
        nodata=float(BURNED_AREA_OUTPUT_NODATA),
        tags=dataset_tags,
        band_tags=[{"native_transform": list(result_transform)[:6]}],
    )

    return {
        "product": short_name,
        "output": str(out_path),
        "status": "success",
        "months_used": months_used,
        "tiles_used": sorted(tiles_used),
        "burned_pixel_count": burned_pixel_count,
    }


def _process_burned_area(
    config: Dict[str, Any],
    config_dir: Path,
    output_root: Path,
    geometry_wgs84: Dict[str, Any],
    start_date: date,
    end_date: date,
    products: List[str],
    buffer_km: float = 0.0,
) -> Dict[str, Any]:
    """Process each requested burned-area product independently.

    MCD64A1 and VNP64A1 are never merged/OR'd together -- each gets its own
    output GeoTIFF, returned here under its own key.
    """
    result: Dict[str, Any] = {}
    for product_key in products:
        LOGGER.info("Processing burned area %s (%s .. %s)...", product_key, start_date, end_date)
        result[product_key] = _process_burned_area_product(
            config=config,
            config_dir=config_dir,
            output_root=output_root,
            geometry_wgs84=geometry_wgs84,
            start_date=start_date,
            end_date=end_date,
            product_key=product_key,
            buffer_km=buffer_km,
        )
    return result
