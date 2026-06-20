from __future__ import annotations

import csv
import io
import logging
import math
import os
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import rasterio
import requests
from rasterio.enums import MergeAlg
from rasterio.features import bounds as geometry_bounds, rasterize
from rasterio.transform import from_origin
from rasterio.warp import transform as warp_transform, transform_geom

try:
    import shapefile
except ImportError as exc:  # pragma: no cover
    raise RuntimeError("pyshp is required. Please install pyshp.") from exc

from .config import _load_env_kv_file, _normalize_firms_products, _resolve_runtime_path
from .constants import WGS84
from .geometry import _expand_bbox_by_meters, _point_in_geometry
from .imagery import _crs_to_string

LOGGER = logging.getLogger(__name__)


def _safe_shp_field_name(name: str, used: set[str]) -> str:
    clean = re.sub(r"[^A-Za-z0-9_]", "_", name)
    if not clean:
        clean = "field"
    clean = clean[:10]

    if clean not in used:
        used.add(clean)
        return clean

    idx = 1
    while True:
        candidate = f"{clean[:8]}{idx:02d}"
        if candidate not in used:
            used.add(candidate)
            return candidate
        idx += 1


def _write_activefire_shapefile(
    output_shp_path: Path,
    rows: List[Dict[str, str]],
    *,
    output_crs: str = "EPSG:4326",
) -> None:
    output_shp_path.parent.mkdir(parents=True, exist_ok=True)

    writer = shapefile.Writer(str(output_shp_path), shapeType=shapefile.POINT)

    target_crs = rasterio.crs.CRS.from_string(output_crs)
    target_crs_text = _crs_to_string(target_crs) or "EPSG:4326"
    needs_reproject = target_crs_text.upper() != "EPSG:4326"

    source_fields = [k for k in rows[0].keys() if k.lower() not in {"latitude", "longitude", "lat", "lon"}]
    field_name_map: Dict[str, str] = {}
    used_names: set[str] = set()

    for field in source_fields:
        shp_field = _safe_shp_field_name(field, used_names)
        field_name_map[field] = shp_field
        writer.field(shp_field, "C", size=254)

    for row in rows:
        lon_raw = row.get("longitude") or row.get("lon")
        lat_raw = row.get("latitude") or row.get("lat")
        if lon_raw is None or lat_raw is None:
            continue

        try:
            lon = float(lon_raw)
            lat = float(lat_raw)
        except ValueError:
            continue

        x, y = lon, lat
        if needs_reproject:
            x_vals, y_vals = warp_transform("EPSG:4326", target_crs_text, [lon], [lat])
            x, y = float(x_vals[0]), float(y_vals[0])

        writer.point(x, y)
        record = [str(row.get(field, ""))[:254] for field in source_fields]
        writer.record(*record)

    writer.close()

    prj_path = output_shp_path.with_suffix(".prj")
    prj_text = target_crs.to_wkt() if target_crs is not None else WGS84
    prj_path.write_text(prj_text, encoding="utf-8")


def _fetch_firms_rows(
    api_key: str,
    product: str,
    bbox: Tuple[float, float, float, float],
    days: int,
    base_url: str,
    start_on: Optional[date] = None,
) -> List[Dict[str, str]]:
    west, south, east, north = bbox
    bbox_token = f"{west:.6f},{south:.6f},{east:.6f},{north:.6f}"
    url = f"{base_url.rstrip('/')}/{api_key}/{product}/{bbox_token}/{days}"
    if start_on is not None:
        url = f"{url}/{start_on.isoformat()}"

    response = requests.get(url, timeout=120)
    try:
        response.raise_for_status()
    except requests.HTTPError as exc:
        detail = response.text.strip().replace("\n", " ")
        raise RuntimeError(
            f"FIRMS request failed ({response.status_code}) for product={product}, "
            f"days={days}, bbox={bbox_token}: {detail}"
        ) from exc

    text = response.text.strip()
    if not text:
        return []

    reader = csv.DictReader(io.StringIO(text))
    return [dict(row) for row in reader]


def _build_activefire_reference_grid_from_raster(
    reference_raster_path: Path,
) -> Optional[Dict[str, Any]]:
    if not reference_raster_path.exists():
        return None

    with rasterio.open(reference_raster_path) as src:
        if src.crs is None:
            return None
        return {
            "crs": _crs_to_string(src.crs) or "EPSG:4326",
            "transform": src.transform,
            "width": int(src.width),
            "height": int(src.height),
            "resolution": abs(float(src.transform.a)) if src.transform.a != 0 else 10.0,
        }


def _build_activefire_reference_grid_from_geometry(
    geometry_wgs84: Dict[str, Any],
    output_crs: str,
    resolution_m: float,
) -> Dict[str, Any]:
    geom_in_target = transform_geom("EPSG:4326", output_crs, geometry_wgs84, precision=6)
    left, bottom, right, top = geometry_bounds(geom_in_target)
    width = max(1, int(math.ceil((right - left) / resolution_m)))
    height = max(1, int(math.ceil((top - bottom) / resolution_m)))
    transform = from_origin(left, top, resolution_m, resolution_m)
    return {
        "crs": output_crs,
        "transform": transform,
        "width": width,
        "height": height,
        "resolution": resolution_m,
    }


def _grid_bounds_from_reference_grid(
    reference_grid: Dict[str, Any],
) -> Tuple[float, float, float, float]:
    transform = reference_grid["transform"]
    width = int(reference_grid["width"])
    height = int(reference_grid["height"])

    left = float(transform.c)
    top = float(transform.f)
    right = left + float(transform.a) * width
    bottom = top + float(transform.e) * height

    min_x = min(left, right)
    max_x = max(left, right)
    min_y = min(bottom, top)
    max_y = max(bottom, top)
    return min_x, min_y, max_x, max_y


def _expand_reference_grid_to_bounds(
    reference_grid: Dict[str, Any],
    include_bounds: Tuple[float, float, float, float],
) -> Dict[str, Any]:
    transform = reference_grid["transform"]
    res_x = abs(float(transform.a))
    res_y = abs(float(transform.e))
    if res_x == 0 or res_y == 0:
        return reference_grid

    current_left, current_bottom, current_right, current_top = _grid_bounds_from_reference_grid(reference_grid)
    inc_left, inc_bottom, inc_right, inc_top = include_bounds

    target_left = min(current_left, inc_left)
    target_bottom = min(current_bottom, inc_bottom)
    target_right = max(current_right, inc_right)
    target_top = max(current_top, inc_top)

    origin_x = float(transform.c)
    origin_y = float(transform.f)

    snap_left = origin_x + math.floor((target_left - origin_x) / res_x) * res_x
    snap_right = origin_x + math.ceil((target_right - origin_x) / res_x) * res_x
    snap_top = origin_y - math.floor((origin_y - target_top) / res_y) * res_y
    snap_bottom = origin_y - math.ceil((origin_y - target_bottom) / res_y) * res_y

    width = max(1, int(math.ceil((snap_right - snap_left) / res_x)))
    height = max(1, int(math.ceil((snap_top - snap_bottom) / res_y)))
    transform_out = from_origin(snap_left, snap_top, res_x, res_y)

    expanded = dict(reference_grid)
    expanded.update({
        "transform": transform_out,
        "width": width,
        "height": height,
    })
    return expanded


def _shapes_bounds(
    shapes: List[Tuple[Dict[str, Any], int]],
) -> Optional[Tuple[float, float, float, float]]:
    min_x: Optional[float] = None
    min_y: Optional[float] = None
    max_x: Optional[float] = None
    max_y: Optional[float] = None

    for geom, _ in shapes:
        gtype = geom.get("type")
        coords = geom.get("coordinates")
        if gtype != "Polygon" or not coords:
            continue

        for ring in coords:
            for pt in ring:
                if len(pt) < 2:
                    continue
                x = float(pt[0])
                y = float(pt[1])
                min_x = x if min_x is None else min(min_x, x)
                min_y = y if min_y is None else min(min_y, y)
                max_x = x if max_x is None else max(max_x, x)
                max_y = y if max_y is None else max(max_y, y)

    if min_x is None or min_y is None or max_x is None or max_y is None:
        return None
    return min_x, min_y, max_x, max_y


def _activefire_footprint_polygon(
    row: Dict[str, str],
    output_crs: str,
    default_pixel_size: float,
) -> Optional[Dict[str, Any]]:
    lon_raw = row.get("longitude") or row.get("lon")
    lat_raw = row.get("latitude") or row.get("lat")
    if lon_raw is None or lat_raw is None:
        return None

    try:
        lon = float(lon_raw)
        lat = float(lat_raw)
    except ValueError:
        return None

    try:
        scan_km = float(row.get("scan", ""))
    except (TypeError, ValueError):
        scan_km = default_pixel_size / 1000.0

    try:
        track_km = float(row.get("track", ""))
    except (TypeError, ValueError):
        track_km = default_pixel_size / 1000.0

    half_w_m = max(default_pixel_size, scan_km * 1000.0) / 2.0
    half_h_m = max(default_pixel_size, track_km * 1000.0) / 2.0

    if output_crs.upper() == "EPSG:4326":
        meters_per_degree_lat = 111320.0
        meters_per_degree_lon = max(1.0, 111320.0 * math.cos(math.radians(lat)))
        dx = half_w_m / meters_per_degree_lon
        dy = half_h_m / meters_per_degree_lat
        x, y = lon, lat
    else:
        x_vals, y_vals = warp_transform("EPSG:4326", output_crs, [lon], [lat])
        x, y = float(x_vals[0]), float(y_vals[0])
        dx = half_w_m
        dy = half_h_m

    return {
        "type": "Polygon",
        "coordinates": [[
            [x - dx, y - dy],
            [x + dx, y - dy],
            [x + dx, y + dy],
            [x - dx, y + dy],
            [x - dx, y - dy],
        ]],
    }


def _write_activefire_raster(
    output_tif_path: Path,
    rows: List[Dict[str, str]],
    *,
    output_crs: str,
    reference_grid: Dict[str, Any],
    expand_grid_to_detections: bool,
) -> None:
    output_tif_path.parent.mkdir(parents=True, exist_ok=True)

    grid = dict(reference_grid)
    resolution = float(grid.get("resolution", 10.0))

    shapes: List[Tuple[Dict[str, Any], int]] = []
    for row in rows:
        geom = _activefire_footprint_polygon(
            row,
            output_crs=output_crs,
            default_pixel_size=resolution,
        )
        if geom is not None:
            shapes.append((geom, 1))

    if expand_grid_to_detections and shapes:
        bounds = _shapes_bounds(shapes)
        if bounds is not None:
            grid = _expand_reference_grid_to_bounds(grid, bounds)

    transform = grid["transform"]
    width = int(grid["width"])
    height = int(grid["height"])

    raster = np.zeros((height, width), dtype=np.uint16)
    if shapes:
        raster = rasterize(
            shapes=shapes,
            out_shape=(height, width),
            transform=transform,
            fill=0,
            all_touched=True,
            merge_alg=MergeAlg.add,
            dtype="uint16",
        )

    profile = {
        "driver": "GTiff",
        "dtype": "uint16",
        "count": 1,
        "crs": output_crs,
        "transform": transform,
        "width": width,
        "height": height,
        "nodata": 0,
        "compress": "lzw",
    }
    with rasterio.open(output_tif_path, "w", **profile) as dst:
        dst.write(raster, 1)


def _process_activefire(
    config: Dict[str, Any],
    config_dir: Path,
    output_root: Path,
    bbox: Tuple[float, float, float, float],
    geometry_wgs84: Dict[str, Any],
    reference_crs: Optional[str],
    reference_raster_path: Optional[str],
    start_date: date,
    end_date: date,
    satellites: List[str],
) -> Dict[str, Any]:
    firms_cfg = config.get("firms", {})

    key_env_path_value = str(firms_cfg.get("key_env_path", "key.env")).strip() or "key.env"
    key_env_path = _resolve_runtime_path(key_env_path_value, config_dir, must_exist=False)
    env_values = _load_env_kv_file(key_env_path)

    api_key = str(firms_cfg.get("api_key", "")).strip()
    if not api_key:
        for key_name in ("FIRMS_API_KEY", "MAP_KEY", "FIRMS_MAP_KEY"):
            candidate = str(env_values.get(key_name, "")).strip()
            if candidate:
                api_key = candidate
                break

    if not api_key:
        for key_name in ("FIRMS_API_KEY", "MAP_KEY", "FIRMS_MAP_KEY"):
            candidate = str(os.getenv(key_name, "")).strip()
            if candidate:
                api_key = candidate
                break

    if not api_key:
        LOGGER.warning(
            "FIRMS api_key is not set. Set firms.key_env_path (%s) with FIRMS_API_KEY=... (or MAP_KEY=...). Active fire download is skipped.",
            key_env_path,
        )
        return {"modis": 0, "viirs": 0}

    bbox_buffer_m = max(0.0, float(firms_cfg.get("bbox_buffer_m", 0)))
    if bbox_buffer_m > 0:
        original_bbox = bbox
        bbox = _expand_bbox_by_meters(bbox, bbox_buffer_m)
        LOGGER.info(
            "Expanded FIRMS bbox by %.1fm: %s -> %s",
            bbox_buffer_m,
            original_bbox,
            bbox,
        )

    base_url = str(
        firms_cfg.get(
            "base_url",
            "https://firms.modaps.eosdis.nasa.gov/api/area/csv",
        )
    )

    product_map = firms_cfg.get("product_map", {})
    modis_products = _normalize_firms_products(
        product_map.get("modis"),
        default_products=[],
        field_name="config.firms.product_map.modis",
    )
    viirs_products = _normalize_firms_products(
        product_map.get("viirs"),
        default_products=[],
        field_name="config.firms.product_map.viirs",
    )
    products_by_sat = {
        "modis": modis_products,
        "viirs": viirs_products,
    }

    requested_days = int(firms_cfg.get("days", 5))
    days = max(1, min(requested_days, 5))
    if requested_days != days:
        LOGGER.warning(
            "FIRMS area API accepts days in [1..5]. requested=%s, using=%s",
            requested_days,
            days,
        )

    period_summary_enabled = bool(firms_cfg.get("period_summary", True))
    clip_to_aoi = bool(firms_cfg.get("clip_to_aoi", True))
    pixel_tif_enabled = bool(firms_cfg.get("pixel_tif", True))
    pixel_resolution = max(1.0, float(firms_cfg.get("pixel_resolution", 10.0)))
    pixel_expand_to_detections = bool(firms_cfg.get("pixel_expand_to_detections", True))

    activefire_output_crs = reference_crs or "EPSG:4326"
    LOGGER.info("Activefire output CRS (fixed): %s", activefire_output_crs)

    reference_grid: Optional[Dict[str, Any]] = None
    if pixel_tif_enabled and reference_raster_path:
        reference_grid = _build_activefire_reference_grid_from_raster(Path(reference_raster_path))
        if reference_grid is not None and reference_grid.get("crs") != activefire_output_crs:
            reference_grid = None

    if pixel_tif_enabled and reference_grid is None:
        reference_grid = _build_activefire_reference_grid_from_geometry(
            geometry_wgs84=geometry_wgs84,
            output_crs=activefire_output_crs,
            resolution_m=pixel_resolution,
        )

    utc_token = datetime.now(timezone.utc).strftime("%H%M")
    summary = {
        "modis": 0,
        "viirs": 0,
        "period_summary": {"modis": 0, "viirs": 0},
        "raster": {"modis": 0, "viirs": 0},
        "output_crs": activefire_output_crs,
        "products": products_by_sat,
    }

    for sat in satellites:
        products = products_by_sat.get(sat, [])
        out_dir = output_root / sat / "activefire"
        out_raster_dir = output_root / sat / "activefire_tif"

        rows: List[Dict[str, str]] = []
        cursor = start_date
        while cursor <= end_date:
            window_end = min(cursor + timedelta(days=days - 1), end_date)
            window_days = (window_end - cursor).days + 1
            for product in products:
                try:
                    part = _fetch_firms_rows(
                        api_key=api_key,
                        product=product,
                        bbox=bbox,
                        days=window_days,
                        base_url=base_url,
                        start_on=cursor,
                    )
                    rows.extend(part)
                except Exception as exc:
                    LOGGER.warning(
                        "FIRMS fetch failed for %s (%s) window %s..%s: %s",
                        sat,
                        product,
                        cursor,
                        window_end,
                        exc,
                    )
            cursor = window_end + timedelta(days=1)

        filtered: Dict[str, List[Dict[str, str]]] = {}
        for row in rows:
            acq_date_raw = row.get("acq_date")
            if not acq_date_raw:
                continue
            try:
                acq_date = datetime.strptime(acq_date_raw, "%Y-%m-%d").date()
            except ValueError:
                continue
            if start_date <= acq_date <= end_date:
                if clip_to_aoi:
                    lon_raw = row.get("longitude") or row.get("lon")
                    lat_raw = row.get("latitude") or row.get("lat")
                    if lon_raw is None or lat_raw is None:
                        continue
                    try:
                        lon = float(lon_raw)
                        lat = float(lat_raw)
                    except ValueError:
                        continue
                    if not _point_in_geometry(lon, lat, geometry_wgs84):
                        continue
                key = acq_date.strftime("%Y%m%d")
                if key not in filtered:
                    filtered[key] = []
                filtered[key].append(row)

        for date_token, rows_for_day in filtered.items():
            shp_path = out_dir / f"ACFR_{date_token}_{utc_token}.shp"
            _write_activefire_shapefile(
                shp_path,
                rows_for_day,
                output_crs=activefire_output_crs,
            )

            if pixel_tif_enabled and reference_grid is not None:
                tif_path = out_raster_dir / f"ACFR_{date_token}_{utc_token}.tif"
                _write_activefire_raster(
                    tif_path,
                    rows_for_day,
                    output_crs=activefire_output_crs,
                    reference_grid=reference_grid,
                    expand_grid_to_detections=pixel_expand_to_detections,
                )
                summary["raster"][sat] += 1

            summary[sat] += 1

        if period_summary_enabled:
            merged_rows: List[Dict[str, str]] = []
            for date_token in sorted(filtered.keys()):
                merged_rows.extend(filtered[date_token])

            if merged_rows:
                summary_path = out_dir / (
                    f"ACFR_{start_date.strftime('%Y%m%d')}_{end_date.strftime('%Y%m%d')}_{utc_token}.shp"
                )
                _write_activefire_shapefile(
                    summary_path,
                    merged_rows,
                    output_crs=activefire_output_crs,
                )

                if pixel_tif_enabled and reference_grid is not None:
                    summary_tif_path = out_raster_dir / (
                        f"ACFR_{start_date.strftime('%Y%m%d')}_{end_date.strftime('%Y%m%d')}_{utc_token}.tif"
                    )
                    _write_activefire_raster(
                        summary_tif_path,
                        merged_rows,
                        output_crs=activefire_output_crs,
                        reference_grid=reference_grid,
                        expand_grid_to_detections=pixel_expand_to_detections,
                    )

                summary["period_summary"][sat] += 1

    return summary
