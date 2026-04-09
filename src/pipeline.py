from __future__ import annotations

import csv
import io
import json
import logging
import math
import os
import re
import shutil
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np
import planetary_computer
import requests
import rasterio
from pystac.item import Item
from pystac_client import Client
from rasterio.enums import MergeAlg, Resampling
from rasterio.features import bounds as geometry_bounds, rasterize
from rasterio.transform import Affine, from_origin
from rasterio.vrt import WarpedVRT
from rasterio.warp import reproject, transform as warp_transform, transform_geom

try:
    import yaml
except ImportError as exc:  # pragma: no cover
    raise RuntimeError("PyYAML is required. Please install pyyaml.") from exc

try:
    import shapefile
except ImportError as exc:  # pragma: no cover
    raise RuntimeError("pyshp is required. Please install pyshp.") from exc


LOGGER = logging.getLogger(__name__)
PROJECT_ROOT = Path(__file__).resolve().parents[1]

PC_STAC_URL = "https://planetarycomputer.microsoft.com/api/stac/v1"

SENTINEL_COLLECTION = "sentinel-2-l2a"
LANDSAT_COLLECTION = "landsat-c2-l2"

SENTINEL_BAND_MAP: Dict[int, Tuple[str, str]] = {
    1: ("B01", "B01"),
    2: ("B02", "B02"),
    3: ("B03", "B03"),
    4: ("B04", "B04"),
    5: ("B05", "B05"),
    6: ("B06", "B06"),
    7: ("B07", "B07"),
    8: ("B08", "B08"),
    9: ("B8A", "B8A"),
    10: ("B09", "B09"),
    11: ("B11", "B11"),
    12: ("B12", "B12"),
}

LANDSAT_BAND_MAP: Dict[int, Tuple[str, str]] = {
    1: ("coastal", "B01"),
    2: ("blue", "B02"),
    3: ("green", "B03"),
    4: ("red", "B04"),
    5: ("nir08", "B05"),
    6: ("swir16", "B06"),
    7: ("swir22", "B07"),
    8: ("qa_aerosol", "B08"),
    9: ("qa_pixel", "B09"),
    10: ("lwir11", "B10"),
    11: ("lwir12", "B11"),
}

CLOUDMASK_REQUIRED_BANDS = {
    "sentinel2": [3, 4, 8],
    "landsat89": [3, 4, 5],
}

SNOWMASK_REQUIRED_BANDS = {
    "sentinel2": [3, 4, 11],
    "landsat89": [3, 4, 6],
}

TARGET_RESOLUTION = {
    "sentinel2": 10.0,
    "landsat89": 30.0,
}

WGS84 = (
    'GEOGCS["WGS 84",DATUM["WGS_1984",SPHEROID["WGS 84",6378137,298.257223563]],'
    'PRIMEM["Greenwich",0],UNIT["degree",0.0174532925199433]]'
)

from omnicloudmask import predict_from_array  # noqa: E402


DN_CONVERSION_PRESETS: Dict[str, Dict[str, float]] = {
    "sentinel2": {"scale": 1 / 10000, "offset": 0.0},
    "landsat8": {"scale": 0.0000275, "offset": -0.2},
    "landsat9": {"scale": 0.0000275, "offset": -0.2},
}


def _prepare_for_cloudmask(
    image_path: Path,
    custom_band_indices: Tuple[int, int, int],
) -> Tuple[np.ndarray, Dict[str, Any]]:
    with rasterio.open(image_path) as src:
        data = src.read(list(custom_band_indices)).astype(np.float32)
        meta = src.meta.copy()
    return data, meta


def _apply_cloud_mask_local(
    image_path: Path,
    mask_path: Path,
    output_path: Path,
    mask_classes: List[int],
    satellite_type: str,
    snow_mask_path: Optional[Path] = None,
) -> Path:
    if satellite_type not in DN_CONVERSION_PRESETS:
        raise ValueError(f"Unsupported satellite type for reflectance conversion: {satellite_type}")

    preset = DN_CONVERSION_PRESETS[satellite_type]

    with rasterio.open(image_path) as src:
        raw_data = src.read().astype(np.float32)
        meta = src.meta.copy()
        src_nodata = src.nodata

    with rasterio.open(mask_path) as msrc:
        cloud_mask = msrc.read(1)

    if cloud_mask.shape != raw_data.shape[1:]:
        raise ValueError(
            f"Mask shape mismatch. image={raw_data.shape[1:]}, mask={cloud_mask.shape}"
        )

    snow_mask = None
    if snow_mask_path is not None:
        with rasterio.open(snow_mask_path) as ssrc:
            snow_mask = ssrc.read(1)
        if snow_mask.shape != raw_data.shape[1:]:
            raise ValueError(
                f"Snow mask shape mismatch. image={raw_data.shape[1:]}, snow={snow_mask.shape}"
            )

    data = raw_data * preset["scale"] + preset["offset"]
    data = np.clip(data, 0.0, 1.0)

    mask_target = np.isin(cloud_mask, mask_classes)
    if snow_mask is not None:
        mask_target = mask_target | (snow_mask == 1)
    for band_idx in range(data.shape[0]):
        data[band_idx][mask_target] = np.nan

    if src_nodata is not None:
        nodata_mask = raw_data[0] == src_nodata
        for band_idx in range(data.shape[0]):
            data[band_idx][nodata_mask] = np.nan

    # Swath外などの全バンド0画素は観測外とみなして除外
    all_zero_mask = np.all(raw_data == 0, axis=0)
    if all_zero_mask.any():
        for band_idx in range(data.shape[0]):
            data[band_idx][all_zero_mask] = np.nan

    output_path.parent.mkdir(parents=True, exist_ok=True)
    meta.update({
        "dtype": "float32",
        "nodata": float("nan"),
        "compress": "lzw",
    })
    with rasterio.open(output_path, "w", **meta) as dst:
        dst.write(data.astype(np.float32))

    return output_path


def _create_ndsi_snow_mask_local(
    image_path: Path,
    output_path: Path,
    satellite_key: str,
    download_band_numbers: List[int],
    ndsi_threshold: float = 0.4,
    red_threshold: float = 0.2,
) -> Path:
    required = SNOWMASK_REQUIRED_BANDS[satellite_key]
    missing = [b for b in required if b not in download_band_numbers]
    if missing:
        raise ValueError(
            f"Snow mask requires bands {required} for {satellite_key}, missing={missing}"
        )

    green_idx = download_band_numbers.index(required[0]) + 1
    red_idx = download_band_numbers.index(required[1]) + 1
    swir_idx = download_band_numbers.index(required[2]) + 1

    with rasterio.open(image_path) as src:
        green = src.read(green_idx).astype(np.float32)
        red = src.read(red_idx).astype(np.float32)
        swir = src.read(swir_idx).astype(np.float32)
        meta = src.meta.copy()

    if satellite_key == "sentinel2":
        green_ref = green / 10000.0
        red_ref = red / 10000.0
        swir_ref = swir / 10000.0
    else:
        green_ref = np.clip(green * 0.0000275 - 0.2, 0.0, 1.0)
        red_ref = np.clip(red * 0.0000275 - 0.2, 0.0, 1.0)
        swir_ref = np.clip(swir * 0.0000275 - 0.2, 0.0, 1.0)

    denom = green_ref + swir_ref
    ndsi = np.where(denom != 0, (green_ref - swir_ref) / denom, np.nan)

    snow_condition = (ndsi > ndsi_threshold) & (red_ref > red_threshold)
    snow_mask = np.where(snow_condition, 1, 0).astype(np.uint8)
    snow_mask[np.isnan(ndsi)] = 255

    meta.update({
        "count": 1,
        "dtype": "uint8",
        "nodata": 255,
        "compress": "lzw",
    })

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with rasterio.open(output_path, "w", **meta) as dst:
        dst.write(snow_mask, 1)

    return output_path


def _load_config(config_path: Path) -> Dict[str, Any]:
    with config_path.open("r", encoding="utf-8") as f:
        loaded = yaml.safe_load(f)
    return loaded or {}


def _load_env_kv_file(env_path: Path) -> Dict[str, str]:
    values: Dict[str, str] = {}
    if not env_path.exists():
        return values

    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            values[key] = value
    return values


def _resolve_runtime_path(
    path_value: str,
    config_dir: Path,
    *,
    must_exist: bool = False,
) -> Path:
    """Resolve runtime path with project-root first fallback.

    Relative paths are interpreted from PROJECT_ROOT first, then config_dir.
    This allows values like "./config/no5.geojson" in config/config.yaml
    without accidentally resolving to config/config/no5.geojson.
    """
    p = Path(str(path_value))
    if p.is_absolute():
        return p

    candidates = [
        (PROJECT_ROOT / p).resolve(),
        (config_dir / p).resolve(),
    ]

    if must_exist:
        for candidate in candidates:
            if candidate.exists():
                return candidate

    return candidates[0]


def _to_date(value: str) -> date:
    return datetime.strptime(value, "%Y%m%d").date()


def _normalize_date_list(value: Any, field_name: str) -> List[date]:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        values = list(value)
    else:
        values = [value]

    if not values:
        raise ValueError(f"config.{field_name} must not be empty")

    dates: List[date] = []
    for raw in values:
        text = str(raw).strip()
        if not re.fullmatch(r"\d{8}", text):
            raise ValueError(
                f"config.{field_name} must be YYYYMMDD or list of YYYYMMDD values"
            )
        dates.append(_to_date(text))

    return dates


def _normalize_satellites(value: Any) -> List[str]:
    if isinstance(value, str):
        sats = [v.strip().lower() for v in value.split(",") if v.strip()]
    elif isinstance(value, Sequence):
        sats = [str(v).strip().lower() for v in value if str(v).strip()]
    else:
        raise ValueError("config.satellite must be string or list")

    allowed = {"sentinel2", "landsat89", "modis", "viirs"}
    invalid = [s for s in sats if s not in allowed]
    if invalid:
        raise ValueError(f"Unsupported satellites in config: {invalid}")

    deduped: List[str] = []
    for sat in sats:
        if sat not in deduped:
            deduped.append(sat)
    return deduped


def _normalize_activefire_satellites(value: Any) -> List[str]:
    if isinstance(value, str):
        sats = [v.strip().lower() for v in value.split(",") if v.strip()]
    elif isinstance(value, Sequence):
        sats = [str(v).strip().lower() for v in value if str(v).strip()]
    else:
        raise ValueError("config.firms.activefire_satellite must be string or list")

    allowed = {"modis", "viirs"}
    invalid = [s for s in sats if s not in allowed]
    if invalid:
        raise ValueError(
            f"Unsupported satellites in config.firms.activefire_satellite: {invalid}"
        )

    deduped: List[str] = []
    for sat in sats:
        if sat not in deduped:
            deduped.append(sat)
    return deduped


def _normalize_firms_products(
    value: Any,
    *,
    default_products: List[str],
    field_name: str,
) -> List[str]:
    if value is None:
        raw = list(default_products)
    elif isinstance(value, str):
        raw = [v.strip() for v in value.split(",") if v.strip()]
    elif isinstance(value, Sequence):
        raw = [str(v).strip() for v in value if str(v).strip()]
    else:
        raise ValueError(f"{field_name} must be string or list")

    if not raw:
        raw = list(default_products)

    deduped: List[str] = []
    for product in raw:
        if product not in deduped:
            deduped.append(product)
    return deduped


def _load_aoi_geometry(geojson_path: Path) -> Dict[str, Any]:
    with geojson_path.open("r", encoding="utf-8") as f:
        data = json.load(f)

    if data.get("type") == "FeatureCollection":
        features = data.get("features", [])
        if not features:
            raise ValueError("GeoJSON FeatureCollection has no features")
        geom = features[0].get("geometry")
    elif data.get("type") == "Feature":
        geom = data.get("geometry")
    else:
        geom = data

    if not geom or geom.get("type") not in {"Polygon", "MultiPolygon"}:
        raise ValueError("GeoJSON must contain Polygon or MultiPolygon geometry")

    return geom


def _bbox_from_geometry(geometry: Dict[str, Any]) -> Tuple[float, float, float, float]:
    bounds = geometry_bounds(geometry)
    return float(bounds[0]), float(bounds[1]), float(bounds[2]), float(bounds[3])


def _expand_bbox_by_meters(
    bbox: Tuple[float, float, float, float],
    buffer_m: float,
) -> Tuple[float, float, float, float]:
    if buffer_m <= 0:
        return bbox

    west, south, east, north = bbox
    center_lat = max(-89.9999, min(89.9999, (south + north) / 2.0))

    meters_per_degree_lat = 111320.0
    meters_per_degree_lon = max(1.0, 111320.0 * math.cos(math.radians(center_lat)))

    dlat = buffer_m / meters_per_degree_lat
    dlon = buffer_m / meters_per_degree_lon

    expanded = (
        max(-180.0, west - dlon),
        max(-90.0, south - dlat),
        min(180.0, east + dlon),
        min(90.0, north + dlat),
    )
    return expanded


def _point_on_segment(
    x: float,
    y: float,
    x1: float,
    y1: float,
    x2: float,
    y2: float,
    eps: float = 1e-12,
) -> bool:
    # Collinearity check
    cross = (x - x1) * (y2 - y1) - (y - y1) * (x2 - x1)
    if abs(cross) > eps:
        return False

    # Bounding box check
    if x < min(x1, x2) - eps or x > max(x1, x2) + eps:
        return False
    if y < min(y1, y2) - eps or y > max(y1, y2) + eps:
        return False
    return True


def _point_in_ring(lon: float, lat: float, ring: List[List[float]]) -> bool:
    if len(ring) < 3:
        return False

    inside = False
    n = len(ring)
    for i in range(n):
        x1, y1 = ring[i]
        x2, y2 = ring[(i + 1) % n]

        if _point_on_segment(lon, lat, x1, y1, x2, y2):
            return True

        intersects = ((y1 > lat) != (y2 > lat)) and (
            lon < (x2 - x1) * (lat - y1) / ((y2 - y1) + 1e-300) + x1
        )
        if intersects:
            inside = not inside
    return inside


def _point_in_geometry(lon: float, lat: float, geometry: Dict[str, Any]) -> bool:
    gtype = geometry.get("type")
    coords = geometry.get("coordinates")
    if not coords:
        return False

    def in_polygon(poly_coords: List[List[List[float]]]) -> bool:
        outer = poly_coords[0]
        holes = poly_coords[1:]
        if not _point_in_ring(lon, lat, outer):
            return False
        for hole in holes:
            if _point_in_ring(lon, lat, hole):
                return False
        return True

    if gtype == "Polygon":
        return in_polygon(coords)
    if gtype == "MultiPolygon":
        return any(in_polygon(poly) for poly in coords)
    return False


def _resolve_band_request(
    config: Dict[str, Any],
    satellite_key: str,
    *,
    snowmask_enabled: bool = False,
) -> Tuple[List[int], List[int]]:
    if satellite_key == "sentinel2":
        max_band = max(SENTINEL_BAND_MAP.keys())
    else:
        max_band = max(LANDSAT_BAND_MAP.keys())

    band_cfg = config.get("band", "all")
    if isinstance(band_cfg, dict):
        mode = str(band_cfg.get("mode", "all")).lower()
        selected = band_cfg.get("num", [])
    else:
        mode = str(band_cfg).lower()
        selected = config.get("num", [])

    if mode == "all":
        requested = list(range(1, max_band + 1))
    elif mode == "at":
        if not selected:
            raise ValueError("band=at requires num: [..]")
        requested = sorted({int(v) for v in selected})
    else:
        raise ValueError("band must be 'all' or 'at'")

    invalid = [b for b in requested if b < 1 or b > max_band]
    if invalid:
        raise ValueError(f"Invalid band numbers for {satellite_key}: {invalid}")

    required = CLOUDMASK_REQUIRED_BANDS[satellite_key]
    downloaded_set = set(requested + required)
    if snowmask_enabled:
        downloaded_set.update(SNOWMASK_REQUIRED_BANDS[satellite_key])
    downloaded = sorted(downloaded_set)
    return downloaded, requested


def _unique_tif_path(output_dir: Path, stem: str) -> Path:
    candidate = output_dir / f"{stem}.tif"
    if not candidate.exists():
        return candidate

    index = 2
    while True:
        candidate = output_dir / f"{stem}_{index:02d}.tif"
        if not candidate.exists():
            return candidate
        index += 1


def _item_datetime(item: Item) -> datetime:
    raw = item.properties.get("datetime") or item.properties.get("start_datetime")
    if not raw:
        raise ValueError(f"Item {item.id} has no datetime")
    return datetime.fromisoformat(raw.replace("Z", "+00:00"))


def _crs_to_string(crs_obj: Any) -> Optional[str]:
    if crs_obj is None:
        return None
    if hasattr(crs_obj, "to_string"):
        try:
            return str(crs_obj.to_string())
        except Exception:
            pass
    text = str(crs_obj).strip()
    return text or None


def _infer_item_output_crs(item: Item) -> Optional[str]:
    epsg = item.properties.get("proj:epsg")
    if epsg is not None:
        try:
            return f"EPSG:{int(epsg)}"
        except (TypeError, ValueError):
            pass

    for asset in item.assets.values():
        asset_epsg = asset.extra_fields.get("proj:epsg") if asset.extra_fields else None
        if asset_epsg is not None:
            try:
                return f"EPSG:{int(asset_epsg)}"
            except (TypeError, ValueError):
                continue

    for asset in item.assets.values():
        if asset.media_type and "image" not in asset.media_type:
            continue
        href = planetary_computer.sign(asset.href)
        with rasterio.open(href) as src:
            return _crs_to_string(src.crs)

    return None


def _get_first_float(properties: Dict[str, Any], keys: List[str]) -> Optional[float]:
    for key in keys:
        value = properties.get(key)
        if value is None:
            continue
        try:
            return float(value)
        except (TypeError, ValueError):
            continue
    return None


def _extract_solar_angles(item: Item) -> Tuple[Optional[float], Optional[float]]:
    props = item.properties

    solar_azimuth = _get_first_float(
        props,
        [
            "view:sun_azimuth",
            "s2:mean_solar_azimuth",
            "solar_azimuth",
            "sun_azimuth",
        ],
    )

    solar_zenith = _get_first_float(
        props,
        [
            "view:sun_zenith",
            "s2:mean_solar_zenith",
            "solar_zenith",
            "sun_zenith",
        ],
    )

    if solar_zenith is None:
        solar_elevation = _get_first_float(
            props,
            [
                "view:sun_elevation",
                "sun_elevation",
                "solar_elevation",
            ],
        )
        if solar_elevation is not None:
            solar_zenith = 90.0 - solar_elevation

    return solar_azimuth, solar_zenith


def _build_metadata_feature(item: Item) -> Optional[Dict[str, Any]]:
    if item.geometry is None:
        return None

    acquired = _item_datetime(item).strftime("%Y-%m-%d %H:%M:%S")
    solar_azimuth, solar_zenith = _extract_solar_angles(item)

    props: Dict[str, Any] = {
        "Acquisition_Date": acquired,
        "Image_ID": item.id,
        "Solar_Azimuth_Angle": solar_azimuth,
        "Solar_Zenith_Angle": solar_zenith,
    }

    return {
        "type": "Feature",
        "geometry": item.geometry,
        "id": item.id,
        "properties": props,
    }


def _save_metadata_geojson(
    metadata_features: List[Dict[str, Any]],
    output_path: Path,
) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "type": "FeatureCollection",
        "features": metadata_features,
    }
    with output_path.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, separators=(",", ":"))


def _build_grid_for_item(item: Item, geometry_wgs84: Dict[str, Any], target_resolution: float) -> Tuple[Any, Affine, int, int, Dict[str, Any]]:
    first_asset = None
    for asset in item.assets.values():
        if asset.media_type and "image" not in asset.media_type:
            continue
        first_asset = asset
        break

    if first_asset is None:
        raise ValueError(f"No raster assets found in item: {item.id}")

    href = planetary_computer.sign(first_asset.href)
    with rasterio.open(href) as src:
        if src.crs is None:
            raise ValueError(f"Asset has no CRS: {first_asset.href}")

        geom_in_item_crs = transform_geom("EPSG:4326", src.crs.to_string(), geometry_wgs84, precision=6)
        left, bottom, right, top = geometry_bounds(geom_in_item_crs)
        width = max(1, int(math.ceil((right - left) / target_resolution)))
        height = max(1, int(math.ceil((top - bottom) / target_resolution)))
        transform = from_origin(left, top, target_resolution, target_resolution)
        profile_template = {
            "driver": "GTiff",
            "dtype": "float32",
            "crs": src.crs,
            "transform": transform,
            "width": width,
            "height": height,
            "compress": "lzw",
            "nodata": 0.0,
        }

    return geom_in_item_crs, transform, width, height, profile_template


def _download_item_stack(
    item: Item,
    geometry_wgs84: Dict[str, Any],
    target_resolution: float,
    band_map: Dict[int, Tuple[str, str]],
    download_band_numbers: List[int],
    output_stack_path: Path,
) -> Dict[str, Any]:
    geom_in_crs, transform, width, height, profile_template = _build_grid_for_item(
        item=item,
        geometry_wgs84=geometry_wgs84,
        target_resolution=target_resolution,
    )

    arrays: List[np.ndarray] = []
    band_labels: List[str] = []

    for band_number in download_band_numbers:
        asset_key, label = band_map[band_number]
        asset = item.assets.get(asset_key)
        if asset is None:
            raise ValueError(f"Missing asset '{asset_key}' for item {item.id}")

        href = planetary_computer.sign(asset.href)
        resampling = Resampling.nearest if "qa" in asset_key else Resampling.bilinear

        with rasterio.open(href) as src:
            with WarpedVRT(
                src,
                crs=profile_template["crs"],
                transform=transform,
                width=width,
                height=height,
                resampling=resampling,
                nodata=0,
            ) as vrt:
                arr = vrt.read(1).astype(np.float32)
                valid_mask = vrt.read_masks(1) > 0
                arr[~valid_mask] = 0.0

        arrays.append(arr)
        band_labels.append(label)

    stack = np.stack(arrays, axis=0)
    output_stack_path.parent.mkdir(parents=True, exist_ok=True)

    profile = profile_template.copy()
    profile.update({"count": stack.shape[0]})

    with rasterio.open(output_stack_path, "w", **profile) as dst:
        dst.write(stack)

    return {
        "stack_path": output_stack_path,
        "band_numbers": list(download_band_numbers),
        "band_labels": band_labels,
        "profile": profile,
        "geom_in_item_crs": geom_in_crs,
    }


def _write_band_files_from_stack(
    stack_path: Path,
    output_dir: Path,
    base_stem: str,
    band_numbers: List[int],
    requested_band_numbers: List[int],
    band_map: Dict[int, Tuple[str, str]],
) -> None:
    requested = set(requested_band_numbers)
    output_dir.mkdir(parents=True, exist_ok=True)

    with rasterio.open(stack_path) as src:
        stack = src.read().astype(np.float32)
        profile = src.profile.copy()

    for idx, band_number in enumerate(band_numbers):
        if band_number not in requested:
            continue

        _, label = band_map[band_number]
        out_path = _unique_tif_path(output_dir, f"{base_stem}_{label}")

        band_profile = profile.copy()
        band_profile.update({"count": 1, "dtype": "float32", "compress": "lzw"})

        with rasterio.open(out_path, "w", **band_profile) as dst:
            dst.write(stack[idx], 1)


def _custom_band_indices_for_cloudmask(download_band_numbers: List[int], satellite_key: str) -> Tuple[int, int, int]:
    required = CLOUDMASK_REQUIRED_BANDS[satellite_key]
    positions = []
    for band in required:
        if band not in download_band_numbers:
            raise ValueError(f"Required cloudmask band {band} is not in downloaded bands")
        positions.append(download_band_numbers.index(band) + 1)

    return positions[0], positions[1], positions[2]


def _run_cloudmask_and_mask(
    stack_path: Path,
    cloudmask_path: Path,
    masked_stack_path: Path,
    satellite_key: str,
    target_resolution: float,
    download_band_numbers: List[int],
    cloudmask_classes: List[int],
    omnicloudmask_cfg: Dict[str, Any],
    conversion_satellite_type: str,
) -> Path:
    custom_band_indices = _custom_band_indices_for_cloudmask(download_band_numbers, satellite_key)
    prep_data, prep_meta = _prepare_for_cloudmask(
        image_path=stack_path,
        custom_band_indices=custom_band_indices,
    )

    predict_kwargs: Dict[str, Any] = {}
    if omnicloudmask_cfg.get("patch_size") is not None:
        predict_kwargs["patch_size"] = int(omnicloudmask_cfg["patch_size"])
    if omnicloudmask_cfg.get("patch_overlap") is not None:
        predict_kwargs["patch_overlap"] = int(omnicloudmask_cfg["patch_overlap"])
    if omnicloudmask_cfg.get("batch_size") is not None:
        predict_kwargs["batch_size"] = int(omnicloudmask_cfg["batch_size"])
    if omnicloudmask_cfg.get("device"):
        predict_kwargs["inference_device"] = str(omnicloudmask_cfg["device"])

    cloudmask = predict_from_array(input_array=prep_data, **predict_kwargs)
    if cloudmask.ndim == 2:
        cloudmask = cloudmask[np.newaxis, :, :]

    cloudmask_path.parent.mkdir(parents=True, exist_ok=True)
    mask_profile = prep_meta.copy()
    mask_profile.update(
        {
            "count": cloudmask.shape[0],
            "dtype": str(cloudmask.dtype),
            "compress": "lzw",
            "nodata": None,
        }
    )

    with rasterio.open(cloudmask_path, "w", **mask_profile) as dst:
        dst.write(cloudmask)

    masked_stack_path.parent.mkdir(parents=True, exist_ok=True)
    result_path = _apply_cloud_mask_local(
        image_path=stack_path,
        mask_path=cloudmask_path,
        output_path=masked_stack_path,
        mask_classes=cloudmask_classes,
        satellite_type=conversion_satellite_type,
    )

    return result_path


def _read_stack_with_nan(path: Path) -> Tuple[np.ndarray, Dict[str, Any]]:
    with rasterio.open(path) as src:
        arr = src.read().astype(np.float32)
        profile = src.profile.copy()
        nodata = src.nodata

    if nodata is not None:
        arr[arr == nodata] = np.nan

    return arr, profile


def _reproject_to_reference(
    src_arr: np.ndarray,
    src_profile: Dict[str, Any],
    ref_profile: Dict[str, Any],
    *,
    resampling_method: Resampling = Resampling.bilinear,
) -> np.ndarray:
    if (
        src_profile["crs"] == ref_profile["crs"]
        and src_profile["transform"] == ref_profile["transform"]
        and src_profile["width"] == ref_profile["width"]
        and src_profile["height"] == ref_profile["height"]
    ):
        return src_arr

    dst = np.full((src_arr.shape[0], ref_profile["height"], ref_profile["width"]), np.nan, dtype=np.float32)
    sentinel_nodata = -9999.0

    for b in range(src_arr.shape[0]):
        source = np.where(np.isnan(src_arr[b]), sentinel_nodata, src_arr[b]).astype(np.float32)
        reproject(
            source=source,
            destination=dst[b],
            src_transform=src_profile["transform"],
            src_crs=src_profile["crs"],
            dst_transform=ref_profile["transform"],
            dst_crs=ref_profile["crs"],
            src_nodata=sentinel_nodata,
            dst_nodata=np.nan,
            resampling=resampling_method,
        )

    return dst


def _composite_masked_stacks(masked_stack_paths: List[Path]) -> Tuple[np.ndarray, Dict[str, Any]]:
    first_arr, first_profile = _read_stack_with_nan(masked_stack_paths[0])
    aligned = [first_arr]

    for path in masked_stack_paths[1:]:
        arr, profile = _read_stack_with_nan(path)
        aligned_arr = _reproject_to_reference(arr, profile, first_profile)
        aligned.append(aligned_arr)

    stack = np.stack(aligned, axis=0)
    with np.errstate(all="ignore"):
        composite = np.nanmin(stack, axis=0)

    out_profile = first_profile.copy()
    out_profile.update({
        "dtype": "float32",
        "nodata": float("nan"),
        "count": composite.shape[0],
        "compress": "lzw",
    })

    return composite.astype(np.float32), out_profile


def _composite_stacks(
    stack_paths: List[Path],
    *,
    reducer: str,
    resampling_method: Resampling,
) -> Tuple[np.ndarray, Dict[str, Any]]:
    if not stack_paths:
        raise ValueError("stack_paths is empty")
    if reducer not in {"min", "max"}:
        raise ValueError("reducer must be 'min' or 'max'")

    first_arr, first_profile = _read_stack_with_nan(stack_paths[0])
    aligned = [first_arr]

    for path in stack_paths[1:]:
        arr, profile = _read_stack_with_nan(path)
        aligned_arr = _reproject_to_reference(
            arr,
            profile,
            first_profile,
            resampling_method=resampling_method,
        )
        aligned.append(aligned_arr)

    stack = np.stack(aligned, axis=0)
    with np.errstate(all="ignore"):
        if reducer == "min":
            composite = np.nanmin(stack, axis=0)
        else:
            composite = np.nanmax(stack, axis=0)

    out_profile = first_profile.copy()
    out_profile.update({"count": composite.shape[0], "compress": "lzw"})
    return composite, out_profile


def _write_composite_bands(
    composite: np.ndarray,
    profile: Dict[str, Any],
    output_dir: Path,
    base_stem: str,
    band_numbers: List[int],
    requested_band_numbers: List[int],
    band_map: Dict[int, Tuple[str, str]],
) -> None:
    requested = set(requested_band_numbers)
    output_dir.mkdir(parents=True, exist_ok=True)

    for idx, band_number in enumerate(band_numbers):
        if band_number not in requested:
            continue

        _, label = band_map[band_number]
        out_path = _unique_tif_path(output_dir, f"{base_stem}_{label}")
        out_profile = profile.copy()
        out_profile.update({"count": 1})

        with rasterio.open(out_path, "w", **out_profile) as dst:
            dst.write(composite[idx], 1)


def _landsat_prefix(item: Item) -> Tuple[str, str]:
    platform = str(item.properties.get("platform", "")).lower()
    if "landsat-9" in platform:
        return "L9C", "landsat9"
    return "L8C", "landsat8"


def _search_stac_items(
    collection: str,
    geometry: Dict[str, Any],
    start_date: date,
    end_date: date,
    max_cloud_cover: Optional[float],
) -> List[Item]:
    client = Client.open(PC_STAC_URL, modifier=planetary_computer.sign_inplace)
    query: Dict[str, Any] = {}
    if max_cloud_cover is not None:
        query["eo:cloud_cover"] = {"lt": float(max_cloud_cover)}

    search = client.search(
        collections=[collection],
        intersects=geometry,
        datetime=f"{start_date.isoformat()}/{end_date.isoformat()}",
        query=query or None,
    )

    return sorted(list(search.items()), key=_item_datetime)


def _process_satellite_imagery(
    config: Dict[str, Any],
    config_dir: Path,
    output_root: Path,
    geometry_wgs84: Dict[str, Any],
    start_date: date,
    end_date: date,
    satellite_key: str,
) -> Dict[str, Any]:
    max_cloud = config.get("max_cloud_cover", 80)
    cloudmask_classes = [int(v) for v in config.get("cloudmask", [1, 2, 3])]
    omnicloudmask_cfg = config.get("omnicloudmask", {})
    snowmask_cfg = config.get("snowmask", {})
    snowmask_enabled = bool(snowmask_cfg.get("enabled", False))
    ndsi_threshold = float(snowmask_cfg.get("ndsi_threshold", 0.4))
    red_threshold = float(snowmask_cfg.get("red_threshold", 0.2))
    metadata_cfg = config.get("metadata", {})
    metadata_enabled = bool(metadata_cfg.get("enabled", True))

    download_band_numbers, _ = _resolve_band_request(
        config,
        satellite_key,
        snowmask_enabled=snowmask_enabled,
    )
    target_resolution = TARGET_RESOLUTION[satellite_key]

    if satellite_key == "sentinel2":
        collection = SENTINEL_COLLECTION
        out_sat_dir = output_root / "sentinel2"
        band_map = SENTINEL_BAND_MAP
    else:
        collection = LANDSAT_COLLECTION
        out_sat_dir = output_root / "landsat89"
        band_map = LANDSAT_BAND_MAP

    img_dir = out_sat_dir / "img"
    masked_dir = out_sat_dir / "masked"
    snowmasked_dir = out_sat_dir / "snowmasked"
    cloudmask_dir = out_sat_dir / "cloudmask"
    masked_tmp_dir = out_sat_dir / "_masked_tmp"
    snowmasked_tmp_dir = out_sat_dir / "_snowmasked_tmp"
    cloudmask_tmp_dir = out_sat_dir / "_cloudmask_tmp"
    snowmask_tmp_dir = out_sat_dir / "_snowmask_tmp"

    metadata_filename = f"{start_date.strftime('%Y%m%d')}-{end_date.strftime('%Y%m%d')}.geojson"
    metadata_output_path = img_dir / metadata_filename

    img_dir.mkdir(parents=True, exist_ok=True)
    masked_dir.mkdir(parents=True, exist_ok=True)
    snowmasked_dir.mkdir(parents=True, exist_ok=True)
    cloudmask_dir.mkdir(parents=True, exist_ok=True)
    masked_tmp_dir.mkdir(parents=True, exist_ok=True)
    cloudmask_tmp_dir.mkdir(parents=True, exist_ok=True)
    if snowmask_enabled:
        snowmasked_tmp_dir.mkdir(parents=True, exist_ok=True)
        snowmask_tmp_dir.mkdir(parents=True, exist_ok=True)

    items = _search_stac_items(
        collection=collection,
        geometry=geometry_wgs84,
        start_date=start_date,
        end_date=end_date,
        max_cloud_cover=max_cloud,
    )

    if satellite_key == "landsat89":
        items = [
            item
            for item in items
            if str(item.properties.get("platform", "")).lower() in {"landsat-8", "landsat-9"}
        ]

    if not items:
        LOGGER.warning("No STAC items found for %s", satellite_key)
        return {
            "searched_items": 0,
            "processed_items": 0,
            "masked_items": 0,
            "snowmasked_items": 0,
            "cloudmask_items": 0,
        }

    processed_items = 0
    output_crs: Optional[str] = None
    metadata_features: List[Dict[str, Any]] = []
    grouped_masked: Dict[Tuple[str, str], List[Path]] = defaultdict(list)
    grouped_cloudmask: Dict[Tuple[str, str], List[Path]] = defaultdict(list)
    grouped_snowmasked: Dict[Tuple[str, str], List[Path]] = defaultdict(list)
    grouped_snowmask: Dict[Tuple[str, str], List[Path]] = defaultdict(list)
    reference_raster_path: Optional[str] = None

    try:
        for item in items:
            dt = _item_datetime(item)
            date_token = dt.strftime("%Y%m%d")

            if satellite_key == "sentinel2":
                prefix = "S2C"
                conversion_sat_type = "sentinel2"
            else:
                prefix, conversion_sat_type = _landsat_prefix(item)

            scene_tag = re.sub(r"[^A-Za-z0-9]", "", item.id)[-24:] or "SCENE"
            scene_stem = f"{prefix}_{date_token}_{scene_tag}"
            img_stack_path = img_dir / f"{scene_stem}.tif"

            download_info = _download_item_stack(
                item=item,
                geometry_wgs84=geometry_wgs84,
                target_resolution=target_resolution,
                band_map=band_map,
                download_band_numbers=download_band_numbers,
                output_stack_path=img_stack_path,
            )

            if reference_raster_path is None:
                reference_raster_path = str(img_stack_path)

            if output_crs is None:
                output_crs = _crs_to_string(download_info["profile"].get("crs"))
                if output_crs is None:
                    output_crs = _infer_item_output_crs(item)

            if metadata_enabled:
                feature = _build_metadata_feature(item)
                if feature is not None:
                    metadata_features.append(feature)

            group_key = (prefix, date_token)

            cloudmask_path = cloudmask_tmp_dir / f"{scene_stem}_cloudmask.tif"
            masked_stack_path = masked_tmp_dir / f"{scene_stem}_masked.tif"

            _run_cloudmask_and_mask(
                stack_path=img_stack_path,
                cloudmask_path=cloudmask_path,
                masked_stack_path=masked_stack_path,
                satellite_key=satellite_key,
                target_resolution=target_resolution,
                download_band_numbers=download_info["band_numbers"],
                cloudmask_classes=cloudmask_classes,
                omnicloudmask_cfg=omnicloudmask_cfg,
                conversion_satellite_type=conversion_sat_type,
            )
            grouped_masked[group_key].append(masked_stack_path)
            grouped_cloudmask[group_key].append(cloudmask_path)

            if snowmask_enabled:
                snowmask_path = snowmask_tmp_dir / f"{scene_stem}_snowmask.tif"
                _create_ndsi_snow_mask_local(
                    image_path=img_stack_path,
                    output_path=snowmask_path,
                    satellite_key=satellite_key,
                    download_band_numbers=download_info["band_numbers"],
                    ndsi_threshold=ndsi_threshold,
                    red_threshold=red_threshold,
                )

                snowmasked_stack_path = snowmasked_tmp_dir / f"{scene_stem}_snowmasked.tif"
                _apply_cloud_mask_local(
                    image_path=img_stack_path,
                    mask_path=cloudmask_path,
                    output_path=snowmasked_stack_path,
                    mask_classes=cloudmask_classes,
                    satellite_type=conversion_sat_type,
                    snow_mask_path=snowmask_path,
                )
                grouped_snowmasked[group_key].append(snowmasked_stack_path)
                grouped_snowmask[group_key].append(snowmask_path)

            processed_items += 1

        masked_items = 0
        for (prefix, date_token), paths in sorted(grouped_masked.items()):
            composite, profile = _composite_stacks(
                paths,
                reducer="min",
                resampling_method=Resampling.bilinear,
            )
            out_path = masked_dir / f"{prefix}_{date_token}_masked.tif"
            profile.update({
                "dtype": "float32",
                "nodata": float("nan"),
                "compress": "lzw",
            })
            with rasterio.open(out_path, "w", **profile) as dst:
                dst.write(composite.astype(np.float32))
            masked_items += 1

        cloudmask_items = 0
        for (prefix, date_token), paths in sorted(grouped_cloudmask.items()):
            composite, profile = _composite_stacks(
                paths,
                reducer="min",
                resampling_method=Resampling.nearest,
            )
            out_path = cloudmask_dir / f"{prefix}_{date_token}_cloudmask.tif"
            composite_uint8 = np.where(np.isnan(composite), 0, composite).astype(np.uint8)
            profile.update({
                "count": composite_uint8.shape[0],
                "dtype": "uint8",
                "nodata": None,
                "compress": "lzw",
            })
            with rasterio.open(out_path, "w", **profile) as dst:
                dst.write(composite_uint8)
            cloudmask_items += 1

        snowmasked_items = 0
        if snowmask_enabled:
            for (prefix, date_token), paths in sorted(grouped_snowmasked.items()):
                composite, profile = _composite_stacks(
                    paths,
                    reducer="min",
                    resampling_method=Resampling.bilinear,
                )
                out_path = snowmasked_dir / f"{prefix}_{date_token}_snowmasked.tif"
                profile.update({
                    "dtype": "float32",
                    "nodata": float("nan"),
                    "compress": "lzw",
                })
                with rasterio.open(out_path, "w", **profile) as dst:
                    dst.write(composite.astype(np.float32))
                snowmasked_items += 1

            for (prefix, date_token), paths in sorted(grouped_snowmask.items()):
                composite, profile = _composite_stacks(
                    paths,
                    reducer="max",
                    resampling_method=Resampling.nearest,
                )
                out_path = cloudmask_dir / f"{prefix}_{date_token}_snowmask.tif"
                composite_uint8 = np.where(np.isnan(composite), 255, composite).astype(np.uint8)
                profile.update({
                    "count": composite_uint8.shape[0],
                    "dtype": "uint8",
                    "nodata": 255,
                    "compress": "lzw",
                })
                with rasterio.open(out_path, "w", **profile) as dst:
                    dst.write(composite_uint8)

        if metadata_enabled and metadata_features:
            _save_metadata_geojson(metadata_features, metadata_output_path)

        return {
            "searched_items": len(items),
            "processed_items": processed_items,
            "masked_items": masked_items,
            "snowmasked_items": snowmasked_items,
            "cloudmask_items": cloudmask_items,
            "snowmask_enabled": snowmask_enabled,
            "output_crs": output_crs,
            "reference_raster": reference_raster_path,
            "metadata_enabled": metadata_enabled,
            "metadata_file": str(metadata_output_path) if metadata_enabled and metadata_features else None,
        }
    finally:
        shutil.rmtree(masked_tmp_dir, ignore_errors=True)
        shutil.rmtree(cloudmask_tmp_dir, ignore_errors=True)
        shutil.rmtree(snowmasked_tmp_dir, ignore_errors=True)
        shutil.rmtree(snowmask_tmp_dir, ignore_errors=True)


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


def _grid_bounds_from_reference_grid(reference_grid: Dict[str, Any]) -> Tuple[float, float, float, float]:
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


def _shapes_bounds(shapes: List[Tuple[Dict[str, Any], int]]) -> Optional[Tuple[float, float, float, float]]:
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
        default_products=["MODIS_NRT"],
        field_name="config.firms.product_map.modis",
    )
    viirs_products = _normalize_firms_products(
        product_map.get("viirs"),
        default_products=["VIIRS_SNPP_NRT"],
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

        filtered: Dict[str, List[Dict[str, str]]] = defaultdict(list)
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
                filtered[acq_date.strftime("%Y%m%d")].append(row)

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


def run_pipeline(config: Dict[str, Any], config_dir: Path) -> Dict[str, Any]:
    if "geojson" not in config:
        raise ValueError("config.geojson is required")
    if "startday" not in config or "endday" not in config:
        raise ValueError("config.startday and config.endday are required")
    if "satellite" not in config:
        raise ValueError("config.satellite is required")

    geojson_path = _resolve_runtime_path(
        str(config["geojson"]),
        config_dir,
        must_exist=True,
    )

    output_root = _resolve_runtime_path(
        str(config.get("output", "output")),
        config_dir,
        must_exist=False,
    )

    start_dates = _normalize_date_list(config["startday"], "startday")
    end_dates = _normalize_date_list(config["endday"], "endday")

    if len(start_dates) != len(end_dates):
        raise ValueError(
            "config.startday and config.endday must have the same number of values"
        )

    satellites = _normalize_satellites(config["satellite"])
    firms_cfg = config.get("firms", {})

    activefire_targets: List[str]
    if "activefire_satellite" in firms_cfg:
        activefire_targets = _normalize_activefire_satellites(
            firms_cfg.get("activefire_satellite")
        )
    else:
        # Backward compatibility: if not specified, use legacy behavior.
        activefire_targets = [s for s in satellites if s in {"modis", "viirs"}]
    geometry_wgs84 = _load_aoi_geometry(geojson_path)
    bbox = _bbox_from_geometry(geometry_wgs84)

    output_root.mkdir(parents=True, exist_ok=True)

    def _run_single_window(
        start_date: date,
        end_date: date,
        *,
        include_activefire: bool = True,
    ) -> Dict[str, Any]:
        if end_date < start_date:
            raise ValueError("endday must be greater than or equal to startday")

        summary: Dict[str, Any] = {
            "config": {
                "geojson": str(geojson_path),
                "startday": start_date.strftime("%Y%m%d"),
                "endday": end_date.strftime("%Y%m%d"),
                "satellite": satellites,
                "activefire_satellite": activefire_targets,
                "output": str(output_root),
            }
        }

        if "sentinel2" in satellites:
            LOGGER.info("Processing Sentinel-2 imagery... (%s-%s)", start_date, end_date)
            summary["sentinel2"] = _process_satellite_imagery(
                config=config,
                config_dir=config_dir,
                output_root=output_root,
                geometry_wgs84=geometry_wgs84,
                start_date=start_date,
                end_date=end_date,
                satellite_key="sentinel2",
            )

        if "landsat89" in satellites:
            LOGGER.info("Processing Landsat 8/9 imagery... (%s-%s)", start_date, end_date)
            summary["landsat89"] = _process_satellite_imagery(
                config=config,
                config_dir=config_dir,
                output_root=output_root,
                geometry_wgs84=geometry_wgs84,
                start_date=start_date,
                end_date=end_date,
                satellite_key="landsat89",
            )

        if include_activefire and activefire_targets:
            activefire_crs_ref = None
            activefire_reference_raster = None
            if "sentinel2" in summary and isinstance(summary["sentinel2"], dict):
                activefire_crs_ref = summary["sentinel2"].get("output_crs")
                activefire_reference_raster = summary["sentinel2"].get("reference_raster")

            if not activefire_crs_ref:
                try:
                    s2_items = _search_stac_items(
                        collection=SENTINEL_COLLECTION,
                        geometry=geometry_wgs84,
                        start_date=start_date,
                        end_date=end_date,
                        max_cloud_cover=config.get("max_cloud_cover", 80),
                    )
                    if s2_items:
                        activefire_crs_ref = _infer_item_output_crs(s2_items[0])
                except Exception as exc:
                    LOGGER.warning("Failed to infer Sentinel-2 CRS for activefire output: %s", exc)

            LOGGER.info("Processing FIRMS active fire data... (%s-%s)", start_date, end_date)
            summary["activefire"] = _process_activefire(
                config=config,
                config_dir=config_dir,
                output_root=output_root,
                bbox=bbox,
                geometry_wgs84=geometry_wgs84,
                reference_crs=activefire_crs_ref,
                reference_raster_path=activefire_reference_raster,
                start_date=start_date,
                end_date=end_date,
                satellites=activefire_targets,
            )

        return summary

    windows = list(zip(start_dates, end_dates))
    if len(windows) == 1:
        return _run_single_window(windows[0][0], windows[0][1])

    LOGGER.info("Running %d date windows from config", len(windows))
    runs: List[Dict[str, Any]] = []
    activefire_crs_ref: Optional[str] = None
    activefire_reference_raster: Optional[str] = None
    for idx, (start_date, end_date) in enumerate(windows, start=1):
        LOGGER.info("Date window %d/%d: %s-%s", idx, len(windows), start_date, end_date)
        run_summary = _run_single_window(
            start_date,
            end_date,
            include_activefire=False,
        )
        runs.append(run_summary)

        if "sentinel2" in run_summary and isinstance(run_summary["sentinel2"], dict):
            if not activefire_crs_ref:
                activefire_crs_ref = run_summary["sentinel2"].get("output_crs")
            if not activefire_reference_raster:
                activefire_reference_raster = run_summary["sentinel2"].get("reference_raster")

    combined_start = min(start_dates)
    combined_end = max(end_dates)
    activefire_summary: Optional[Dict[str, Any]] = None

    if activefire_targets:
        if not activefire_crs_ref:
            try:
                s2_items = _search_stac_items(
                    collection=SENTINEL_COLLECTION,
                    geometry=geometry_wgs84,
                    start_date=combined_start,
                    end_date=combined_end,
                    max_cloud_cover=config.get("max_cloud_cover", 80),
                )
                if s2_items:
                    activefire_crs_ref = _infer_item_output_crs(s2_items[0])
            except Exception as exc:
                LOGGER.warning("Failed to infer Sentinel-2 CRS for activefire output: %s", exc)

        LOGGER.info(
            "Processing FIRMS active fire data for combined range... (%s-%s)",
            combined_start,
            combined_end,
        )
        activefire_summary = _process_activefire(
            config=config,
            config_dir=config_dir,
            output_root=output_root,
            bbox=bbox,
            geometry_wgs84=geometry_wgs84,
            reference_crs=activefire_crs_ref,
            reference_raster_path=activefire_reference_raster,
            start_date=combined_start,
            end_date=combined_end,
            satellites=activefire_targets,
        )

    result: Dict[str, Any] = {
        "config": {
            "geojson": str(geojson_path),
            "satellite": satellites,
            "activefire_satellite": activefire_targets,
            "output": str(output_root),
        },
        "total_runs": len(runs),
        "activefire_range": {
            "startday": combined_start.strftime("%Y%m%d"),
            "endday": combined_end.strftime("%Y%m%d"),
        },
        "runs": runs,
    }

    if activefire_summary is not None:
        result["activefire"] = activefire_summary

    return result


def run_pipeline_from_config(config_path: Path) -> Dict[str, Any]:
    config_path = config_path.resolve()
    if not config_path.exists():
        raise FileNotFoundError(f"Config file does not exist: {config_path}")

    config = _load_config(config_path)
    return run_pipeline(config=config, config_dir=config_path.parent)


def satellite_image_downloader(
    satellite_type: Sequence[str] | str,
    geojson_path: str,
    sdate: str | Sequence[str] | Sequence[int],
    edate: str | Sequence[str] | Sequence[int],
    output_path: str,
    config_path: Optional[str] = None,
) -> Dict[str, Any]:
    runtime_config: Dict[str, Any] = {
        "satellite": list(satellite_type) if not isinstance(satellite_type, str) else satellite_type,
        "geojson": geojson_path,
        "startday": sdate,
        "endday": edate,
        "output": output_path,
        "band": "all",
        "cloudmask": [1, 2, 3],
    }

    config_dir = Path.cwd()
    if config_path:
        base_config_path = Path(config_path).resolve()
        base_config = _load_config(base_config_path)
        base_config.update(runtime_config)
        runtime_config = base_config
        config_dir = base_config_path.parent

    return run_pipeline(config=runtime_config, config_dir=config_dir)
