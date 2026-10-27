"""Shared AOI halo geometry on the MODIS/VIIRS native Sinusoidal grid.

Moved unchanged from the old run2.py when run2.py was rebuilt as the MODIS
authoritative-image entry point. Used by run_activefire.py (MODIS FIRMS halo
bbox, per-region FIRMS buffer) and by the VIIRS/SGLI entry points for AOIs.
"""

from __future__ import annotations

import json
import math
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Tuple

from rasterio.crs import CRS
from rasterio.features import bounds as geometry_bounds
from rasterio.transform import array_bounds
from rasterio.warp import transform_geom

from .constants import MODIS_SINUSOIDAL_PROJ4, MODIS_SINUSOIDAL_X_MIN, MODIS_SINUSOIDAL_Y_MAX, MODIS_TILE_SIZE_M

HALO_PIXELS = 18
DEFAULT_HALO_BUFFER_M = 10_000.0
# region01 needs four more native rows to fill its 32 x 32 model input.
REGION_HALO_BUFFER_M = {"region01": 13_000.0}
MODIS_500M_PIXEL_SIZE_M = MODIS_TILE_SIZE_M / 2400


def halo_buffer_m_for_region(region: str) -> float:
    return REGION_HALO_BUFFER_M.get(region, DEFAULT_HALO_BUFFER_M)


def load_geojson_geometry(path: Path) -> Dict[str, Any]:
    with Path(path).open("r", encoding="utf-8") as stream:
        data = json.load(stream)
    if data.get("type") == "FeatureCollection":
        features = data.get("features", [])
        geometry = features[0].get("geometry") if features else None
    elif data.get("type") == "Feature":
        geometry = data.get("geometry")
    else:
        geometry = data
    if not geometry or geometry.get("type") not in {"Polygon", "MultiPolygon"}:
        raise ValueError("GeoJSON must contain Polygon or MultiPolygon geometry")
    return geometry


def bbox_from_geometry(geometry: Dict[str, Any]) -> Tuple[float, float, float, float]:
    bounds = geometry_bounds(geometry)
    return float(bounds[0]), float(bounds[1]), float(bounds[2]), float(bounds[3])


def parse_plan_date(value: str) -> date:
    return datetime.strptime(value, "%Y-%m-%d").date()


def native_grid_extent(bounds: Tuple[float, float, float, float]) -> Tuple[float, float, float, float]:
    """Snap a native MODIS extent outward to whole 500 m grid cells."""
    left, bottom, right, top = bounds
    pixel = MODIS_500M_PIXEL_SIZE_M
    snapped_left = MODIS_SINUSOIDAL_X_MIN + math.floor((left - MODIS_SINUSOIDAL_X_MIN) / pixel) * pixel
    snapped_right = MODIS_SINUSOIDAL_X_MIN + math.ceil((right - MODIS_SINUSOIDAL_X_MIN) / pixel) * pixel
    snapped_top = MODIS_SINUSOIDAL_Y_MAX - math.floor((MODIS_SINUSOIDAL_Y_MAX - top) / pixel) * pixel
    snapped_bottom = MODIS_SINUSOIDAL_Y_MAX - math.ceil((MODIS_SINUSOIDAL_Y_MAX - bottom) / pixel) * pixel
    return snapped_left, snapped_bottom, snapped_right, snapped_top


def rectangle(bounds: Tuple[float, float, float, float]) -> Dict[str, Any]:
    left, bottom, right, top = bounds
    return {
        "type": "Polygon",
        "coordinates": [[[left, bottom], [right, bottom], [right, top], [left, top], [left, bottom]]],
    }


def expand_wgs84_bbox_by_meters(
    bounds: Tuple[float, float, float, float], buffer_m: float = DEFAULT_HALO_BUFFER_M
) -> Tuple[float, float, float, float]:
    """Expand a WGS84 bbox by an approximate metric distance at its centre."""
    west, south, east, north = bounds
    center_lat = max(-89.9999, min(89.9999, (south + north) / 2.0))
    meters_per_degree_lat = 111_320.0
    meters_per_degree_lon = max(1.0, meters_per_degree_lat * math.cos(math.radians(center_lat)))
    return (
        max(-180.0, west - buffer_m / meters_per_degree_lon),
        max(-90.0, south - buffer_m / meters_per_degree_lat),
        min(180.0, east + buffer_m / meters_per_degree_lon),
        min(90.0, north + buffer_m / meters_per_degree_lat),
    )


def build_halo_context(geometry_wgs84: Dict[str, Any], buffer_m: float = DEFAULT_HALO_BUFFER_M) -> Dict[str, Any]:
    """Build a metric WGS84 halo, snapped outward to native MODIS pixels."""
    if buffer_m < DEFAULT_HALO_BUFFER_M:
        raise ValueError(f"halo buffer must be at least {DEFAULT_HALO_BUFFER_M:.0f} m")
    native_crs = CRS.from_proj4(MODIS_SINUSOIDAL_PROJ4)
    original_bbox_wgs84 = bbox_from_geometry(geometry_wgs84)
    expanded_bbox_wgs84 = expand_wgs84_bbox_by_meters(original_bbox_wgs84, buffer_m)
    # Same densified-edge transform as surface_reflectance._aoi_bbox_geometry_in_crs.
    segments = 64
    ring: List[List[float]] = []
    w, s, e, n = expanded_bbox_wgs84
    for i in range(segments + 1):
        ring.append([w + (e - w) * i / segments, s])
    for i in range(1, segments + 1):
        ring.append([e, s + (n - s) * i / segments])
    for i in range(1, segments + 1):
        ring.append([e - (e - w) * i / segments, n])
    for i in range(1, segments + 1):
        ring.append([w, n - (n - s) * i / segments])
    expanded_native_geom = transform_geom("EPSG:4326", native_crs, {"type": "Polygon", "coordinates": [ring]}, precision=6)
    halo_native_bounds = native_grid_extent(tuple(geometry_bounds(expanded_native_geom)))

    original_native_geom = transform_geom("EPSG:4326", native_crs, rectangle(original_bbox_wgs84), precision=6)
    original_native_bounds = native_grid_extent(tuple(geometry_bounds(original_native_geom)))
    ol, ob, orr, ot = original_native_bounds
    hl, hb, hr, ht = halo_native_bounds
    minimum_native_halo_pixels = min(
        (ol - hl) / MODIS_500M_PIXEL_SIZE_M,
        (ob - hb) / MODIS_500M_PIXEL_SIZE_M,
        (hr - orr) / MODIS_500M_PIXEL_SIZE_M,
        (ht - ot) / MODIS_500M_PIXEL_SIZE_M,
    )
    if minimum_native_halo_pixels < HALO_PIXELS:
        raise AssertionError(
            f"{buffer_m / 1000:g} km halo yielded only {minimum_native_halo_pixels:.3f} native pixels; "
            f"need at least {HALO_PIXELS}"
        )
    return {
        "original_bbox_wgs84": original_bbox_wgs84,
        "expanded_bbox_wgs84": expanded_bbox_wgs84,
        "original_native_bounds": original_native_bounds,
        "halo_native_bounds": halo_native_bounds,
        # The workers take WGS84 geometry and do the final native snap; do not
        # inverse-transform the native rectangle (curved edges would widen it).
        "halo_wgs84_geometry": rectangle(expanded_bbox_wgs84),
        "halo_bbox_wgs84": expanded_bbox_wgs84,
        "native_pixel_size_m": MODIS_500M_PIXEL_SIZE_M,
        "halo_pixels": HALO_PIXELS,
        "minimum_native_halo_pixels": minimum_native_halo_pixels,
        "buffer_m": buffer_m,
    }


def validate_halo_raster(raster_path: Path, original_native_bounds, minimum_pixels: int = HALO_PIXELS) -> Dict[str, float]:
    """Assert that a native GeoTIFF's actual extent has >= minimum_pixels of context per side."""
    import rasterio

    with rasterio.open(raster_path) as dataset:
        if dataset.crs != CRS.from_proj4(MODIS_SINUSOIDAL_PROJ4):
            raise ValueError(f"{raster_path}: expected the native MODIS Sinusoidal CRS")
        left, bottom, right, top = array_bounds(dataset.height, dataset.width, dataset.transform)
    ol, ob, orr, ot = original_native_bounds
    pixel = MODIS_500M_PIXEL_SIZE_M
    context = {"left": (ol - left) / pixel, "bottom": (ob - bottom) / pixel,
               "right": (right - orr) / pixel, "top": (top - ot) / pixel}
    if min(context.values()) < minimum_pixels - 1e-6:
        raise AssertionError(f"{raster_path}: insufficient native context {context}; need >= {minimum_pixels} pixels on every side")
    return context
