from __future__ import annotations

import json
import logging
import math
import re
import shutil
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import planetary_computer
import rasterio
from pystac.item import Item
from pystac_client import Client
from rasterio.enums import Resampling
from rasterio.features import bounds as geometry_bounds
from rasterio.transform import Affine, from_origin
from rasterio.vrt import WarpedVRT
from rasterio.warp import reproject, transform_geom

from omnicloudmask import predict_from_array

from .config import _as_bool
from .geometry import _bbox_from_geometry
from .constants import (
    CLOUDMASK_REQUIRED_BANDS,
    DN_CONVERSION_PRESETS,
    GDAL_HTTP_OPTIONS,
    LANDSAT_BAND_MAP,
    LANDSAT_COLLECTION,
    PC_STAC_URL,
    SENTINEL_BAND_MAP,
    SENTINEL_COLLECTION,
    SNOWMASK_REQUIRED_BANDS,
    TARGET_RESOLUTION,
)

LOGGER = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# DN correction helpers
# ---------------------------------------------------------------------------

def _parse_processing_baseline(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        text = str(value).strip()
        if not text:
            return None
        return float(text)
    except (TypeError, ValueError):
        return None


def _sentinel2_dn_add_offset(item: Item) -> float:
    baseline = _parse_processing_baseline(item.properties.get("s2:processing_baseline"))
    if baseline is None:
        baseline = _parse_processing_baseline(item.properties.get("processing_baseline"))
    if baseline is not None and baseline >= 4.0:
        return 1000.0
    return 0.0


# ---------------------------------------------------------------------------
# Cloud and snow masking
# ---------------------------------------------------------------------------

def _prepare_for_cloudmask(
    image_path: Path,
    custom_band_indices: Tuple[int, int, int],
    *,
    satellite_key: str = "sentinel2",
    dn_add_offset: float = 0.0,
) -> Tuple[np.ndarray, Dict[str, Any]]:
    with rasterio.open(image_path) as src:
        data = src.read(list(custom_band_indices)).astype(np.float32)
        meta = src.meta.copy()

    preset = DN_CONVERSION_PRESETS.get(satellite_key)
    if preset is None:
        raise ValueError(f"Unsupported satellite for DN conversion: {satellite_key}")
    data = data * preset["scale"] + preset["offset"]
    data = np.clip(data, 0.0, 1.0)

    return data, meta


def _apply_cloud_mask_local(
    image_path: Path,
    mask_path: Path,
    output_path: Path,
    mask_classes: List[int],
    satellite_type: str,
    snow_mask_path: Optional[Path] = None,
    dn_add_offset: float = 0.0,
) -> Path:
    with rasterio.open(image_path) as src:
        data = src.read().astype(np.float32)
        meta = src.meta.copy()
        src_nodata = src.nodata
    nodata_mask = data[0] == src_nodata if src_nodata is not None else None
    all_zero_mask = np.all(data == 0, axis=0)

    with rasterio.open(mask_path) as msrc:
        cloud_mask = msrc.read(1)

    if cloud_mask.shape != data.shape[1:]:
        raise ValueError(
            f"Mask shape mismatch. image={data.shape[1:]}, mask={cloud_mask.shape}"
        )

    snow_mask = None
    if snow_mask_path is not None:
        with rasterio.open(snow_mask_path) as ssrc:
            snow_mask = ssrc.read(1)
        if snow_mask.shape != data.shape[1:]:
            raise ValueError(
                f"Snow mask shape mismatch. image={data.shape[1:]}, snow={snow_mask.shape}"
            )

    if satellite_type not in DN_CONVERSION_PRESETS:
        raise ValueError(f"Unsupported satellite type for reflectance conversion: {satellite_type}")
    preset = DN_CONVERSION_PRESETS[satellite_type]
    data = data * preset["scale"] + preset["offset"]
    data = np.clip(data, 0.0, 1.0)

    mask_target = np.isin(cloud_mask, mask_classes)
    if snow_mask is not None:
        mask_target = mask_target | (snow_mask == 1)
    for band_idx in range(data.shape[0]):
        data[band_idx][mask_target] = np.nan

    if nodata_mask is not None:
        for band_idx in range(data.shape[0]):
            data[band_idx][nodata_mask] = np.nan

    # Treat all-zero pixels (e.g. outside swath) as unobserved
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
    dn_add_offset: float = 0.0,
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
        green_ref = np.clip(green / 10000.0, 0.0, 1.0)
        red_ref = np.clip(red / 10000.0, 0.0, 1.0)
        swir_ref = np.clip(swir / 10000.0, 0.0, 1.0)
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


# ---------------------------------------------------------------------------
# Band resolution helpers
# ---------------------------------------------------------------------------

def _resolve_band_request(
    config: Dict[str, Any],
    satellite_key: str,
    *,
    cloudmask_enabled: bool = True,
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

    downloaded_set = set(requested)
    if cloudmask_enabled:
        downloaded_set.update(CLOUDMASK_REQUIRED_BANDS[satellite_key])
    if cloudmask_enabled and snowmask_enabled:
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


# ---------------------------------------------------------------------------
# STAC item / CRS utilities
# ---------------------------------------------------------------------------

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
        with rasterio.Env(**GDAL_HTTP_OPTIONS), rasterio.open(href) as src:
            return _crs_to_string(src.crs)

    return None


# ---------------------------------------------------------------------------
# Metadata helpers
# ---------------------------------------------------------------------------

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


def _build_metadata_feature(
    item: Item,
    *,
    dn_add_offset: Optional[float] = None,
) -> Optional[Dict[str, Any]]:
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
    processing_baseline = item.properties.get("s2:processing_baseline")
    if processing_baseline is None:
        processing_baseline = item.properties.get("processing_baseline")
    if processing_baseline is not None:
        props["Processing_Baseline"] = processing_baseline
    if dn_add_offset is not None:
        props["DN_Add_Offset"] = dn_add_offset

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


# ---------------------------------------------------------------------------
# Grid / download
# ---------------------------------------------------------------------------

def _build_grid_for_item(
    item: Item,
    geometry_wgs84: Dict[str, Any],
    target_resolution: float,
    *,
    output_crs_override: Optional[str] = None,
    use_bbox_extent: bool = False,
    snap_to_resolution_grid: bool = False,
) -> Tuple[Any, Affine, int, int, Dict[str, Any]]:
    first_asset = None
    for asset in item.assets.values():
        if asset.media_type and "image" not in asset.media_type:
            continue
        first_asset = asset
        break

    if first_asset is None:
        raise ValueError(f"No raster assets found in item: {item.id}")

    LOGGER.info("Preparing grid for item %s", item.id)
    href = planetary_computer.sign(first_asset.href)
    LOGGER.debug("Opening asset href=%s for item=%s", href, getattr(item, "id", None))
    with rasterio.Env(**GDAL_HTTP_OPTIONS), rasterio.open(href) as src:
        if src.crs is None:
            raise ValueError(f"Asset has no CRS: {first_asset.href}")

        target_crs = src.crs
        if output_crs_override:
            target_crs = rasterio.crs.CRS.from_string(output_crs_override)

        if use_bbox_extent:
            west, south, east, north = _bbox_from_geometry(geometry_wgs84)
            bbox_geom_wgs84 = {
                "type": "Polygon",
                "coordinates": [[
                    [west, south],
                    [east, south],
                    [east, north],
                    [west, north],
                    [west, south],
                ]],
            }
            geom_in_item_crs = transform_geom(
                "EPSG:4326",
                target_crs.to_string(),
                bbox_geom_wgs84,
                precision=6,
            )
        else:
            geom_in_item_crs = transform_geom(
                "EPSG:4326",
                target_crs.to_string(),
                geometry_wgs84,
                precision=6,
            )

        left, bottom, right, top = geometry_bounds(geom_in_item_crs)

        if snap_to_resolution_grid:
            left = math.floor(left / target_resolution) * target_resolution
            bottom = math.floor(bottom / target_resolution) * target_resolution
            right = math.ceil(right / target_resolution) * target_resolution
            top = math.ceil(top / target_resolution) * target_resolution

        width = max(1, int(math.ceil((right - left) / target_resolution)))
        height = max(1, int(math.ceil((top - bottom) / target_resolution)))
        transform = from_origin(left, top, target_resolution, target_resolution)
        profile_template = {
            "driver": "GTiff",
            "dtype": "float32",
            "crs": target_crs,
            "transform": transform,
            "width": width,
            "height": height,
            "compress": "lzw",
            "nodata": 0.0,
        }

    LOGGER.info("Prepared grid for item %s", item.id)
    return geom_in_item_crs, transform, width, height, profile_template


def _download_item_stack(
    item: Item,
    geometry_wgs84: Dict[str, Any],
    target_resolution: float,
    band_map: Dict[int, Tuple[str, str]],
    download_band_numbers: List[int],
    output_stack_path: Path,
    *,
    conversion_satellite_type: str,
    dn_add_offset: float = 0.0,
    output_crs_override: Optional[str] = None,
    use_bbox_extent: bool = False,
    snap_to_resolution_grid: bool = False,
) -> Dict[str, Any]:
    geom_in_crs, transform, width, height, profile_template = _build_grid_for_item(
        item=item,
        geometry_wgs84=geometry_wgs84,
        target_resolution=target_resolution,
        output_crs_override=output_crs_override,
        use_bbox_extent=use_bbox_extent,
        snap_to_resolution_grid=snap_to_resolution_grid,
    )

    arrays: List[np.ndarray] = []
    band_labels: List[str] = []

    def _read_band(band_number: int) -> Tuple[np.ndarray, str]:
        asset_key, label = band_map[band_number]
        asset = item.assets.get(asset_key)
        if asset is None:
            raise ValueError(f"Missing asset '{asset_key}' for item {item.id}")

        LOGGER.info("Reading band %s (%s) for item %s", band_number, label, item.id)
        href = planetary_computer.sign(asset.href)
        resampling = Resampling.nearest  # Match GEE default (nearest neighbor)

        with rasterio.Env(**GDAL_HTTP_OPTIONS), rasterio.open(href) as src:
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
        LOGGER.info("Finished band %s (%s) for item %s", band_number, label, item.id)

        return arr, label

    max_workers = 1
    if max_workers > 1:
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            for arr, label in executor.map(_read_band, download_band_numbers):
                arrays.append(arr)
                band_labels.append(label)
    else:
        for band_number in download_band_numbers:
            arr, label = _read_band(band_number)
            arrays.append(arr)
            band_labels.append(label)

    stack = np.stack(arrays, axis=0).astype(np.float32)
    valid_stack_mask = stack != 0
    if conversion_satellite_type == "sentinel2":
        stack = stack - dn_add_offset
        stack = np.maximum(stack, 0.0)
    stack[~valid_stack_mask] = 0.0
    output_stack_path.parent.mkdir(parents=True, exist_ok=True)

    profile = profile_template.copy()
    profile.update({"count": stack.shape[0]})
    processing_baseline = item.properties.get("s2:processing_baseline")
    if processing_baseline is None:
        processing_baseline = item.properties.get("processing_baseline")

    with rasterio.open(output_stack_path, "w", **profile) as dst:
        dst.write(stack)
        dst.update_tags(
            data_units="corrected_dn",
            dn_add_offset=dn_add_offset,
            conversion_satellite_type=conversion_satellite_type,
            processing_baseline="" if processing_baseline is None else str(processing_baseline),
        )

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


# ---------------------------------------------------------------------------
# Cloud mask inference
# ---------------------------------------------------------------------------

def _custom_band_indices_for_cloudmask(
    download_band_numbers: List[int],
    satellite_key: str,
) -> Tuple[int, int, int]:
    required = CLOUDMASK_REQUIRED_BANDS[satellite_key]
    positions = []
    for band in required:
        if band not in download_band_numbers:
            raise ValueError(f"Required cloudmask band {band} is not in downloaded bands")
        positions.append(download_band_numbers.index(band) + 1)

    return positions[0], positions[1], positions[2]


def _is_cuda_runtime_error(exc: Exception) -> bool:
    message = str(exc).lower()
    if "cuda" not in message:
        return False

    signatures = (
        "no kernel image is available",
        "not compatible with the current pytorch installation",
        "invalid device function",
        "no cuda gpus are available",
        "cuda driver version is insufficient",
    )
    return any(signature in message for signature in signatures)


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
    conversion_dn_add_offset: float = 0.0,
) -> Path:
    LOGGER.info("_run_cloudmask_and_mask start: stack=%s", getattr(stack_path, 'name', str(stack_path)))
    custom_band_indices = _custom_band_indices_for_cloudmask(download_band_numbers, satellite_key)
    prep_data, prep_meta = _prepare_for_cloudmask(
        image_path=stack_path,
        custom_band_indices=custom_band_indices,
        satellite_key=conversion_satellite_type,
        dn_add_offset=conversion_dn_add_offset,
    )

    predict_kwargs: Dict[str, Any] = {}
    if omnicloudmask_cfg.get("patch_size") is not None:
        predict_kwargs["patch_size"] = int(omnicloudmask_cfg["patch_size"])
    if omnicloudmask_cfg.get("patch_overlap") is not None:
        predict_kwargs["patch_overlap"] = int(omnicloudmask_cfg["patch_overlap"])
    if omnicloudmask_cfg.get("batch_size") is not None:
        predict_kwargs["batch_size"] = int(omnicloudmask_cfg["batch_size"])
    requested_device = str(omnicloudmask_cfg.get("device", "")).strip()
    if requested_device:
        predict_kwargs["inference_device"] = requested_device

    try:
        cloudmask = predict_from_array(input_array=prep_data, **predict_kwargs)
    except RuntimeError as exc:
        if not _is_cuda_runtime_error(exc):
            raise

        retry_kwargs = dict(predict_kwargs)
        if str(retry_kwargs.get("inference_device", "")).lower() == "cpu":
            raise

        LOGGER.warning(
            "OmniCloudMask CUDA inference failed (%s). Retrying on CPU.",
            exc,
        )
        retry_kwargs["inference_device"] = "cpu"
        cloudmask = predict_from_array(input_array=prep_data, **retry_kwargs)

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

    LOGGER.info("Wrote cloudmask to %s", cloudmask_path)

    masked_stack_path.parent.mkdir(parents=True, exist_ok=True)
    result_path = _apply_cloud_mask_local(
        image_path=stack_path,
        mask_path=cloudmask_path,
        output_path=masked_stack_path,
        mask_classes=cloudmask_classes,
        satellite_type=conversion_satellite_type,
        dn_add_offset=conversion_dn_add_offset,
    )

    LOGGER.info("_run_cloudmask_and_mask finished: result=%s", result_path)
    return result_path


# ---------------------------------------------------------------------------
# Compositing
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# STAC search
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Main satellite imagery processing orchestrator
# ---------------------------------------------------------------------------

def _process_satellite_imagery(
    config: Dict[str, Any],
    config_dir: Path,
    output_root: Path,
    geometry_wgs84: Dict[str, Any],
    start_date: date,
    end_date: date,
    satellite_key: str,
    skip_satellite_subdir: bool = False,
) -> Dict[str, Any]:
    max_cloud = config.get("max_cloud_cover", 80)
    file_exists_mode = str(config.get("file_exists", "overwrite")).strip().lower()
    if file_exists_mode not in {"overwrite", "skip"}:
        raise ValueError("config.file_exists must be 'overwrite' or 'skip'")
    cloudmask_classes = [int(v) for v in config.get("cloudmask", [1, 2, 3])]
    omnicloudmask_cfg = config.get("omnicloudmask", {})
    snowmask_cfg = config.get("snowmask", {})
    snowmask_enabled = bool(snowmask_cfg.get("enabled", False))
    ndsi_threshold = float(snowmask_cfg.get("ndsi_threshold", 0.4))
    red_threshold = float(snowmask_cfg.get("red_threshold", 0.2))
    metadata_cfg = config.get("metadata", {})
    metadata_enabled = bool(metadata_cfg.get("enabled", True))
    img_only = _as_bool(config.get("img_only"), default=False)
    if img_only:
        file_exists_mode = "overwrite"

    gee_cfg = config.get("gee_compatible", {})
    gee_enabled = _as_bool(gee_cfg.get("enabled"), default=False)

    output_crs_override: Optional[str] = None
    use_bbox_extent = False
    snap_to_resolution_grid = False
    if satellite_key == "sentinel2" and gee_enabled:
        output_crs_raw = str(gee_cfg.get("output_crs", "")).strip()
        output_crs_override = output_crs_raw or None
        use_bbox_extent = _as_bool(gee_cfg.get("aoi_as_bbox"), default=True)
        snap_to_resolution_grid = _as_bool(gee_cfg.get("snap_grid"), default=True)

    download_band_numbers, _ = _resolve_band_request(
        config,
        satellite_key,
        cloudmask_enabled=not img_only,
        snowmask_enabled=snowmask_enabled,
    )
    target_resolution = TARGET_RESOLUTION[satellite_key]

    if satellite_key == "sentinel2":
        collection = SENTINEL_COLLECTION
        band_map = SENTINEL_BAND_MAP
        sat_subdir = "" if skip_satellite_subdir else "sentinel2"
    else:
        collection = LANDSAT_COLLECTION
        band_map = LANDSAT_BAND_MAP
        sat_subdir = "" if skip_satellite_subdir else "landsat89"

    out_sat_dir = output_root / sat_subdir if sat_subdir else output_root

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
    if not img_only:
        masked_dir.mkdir(parents=True, exist_ok=True)
        snowmasked_dir.mkdir(parents=True, exist_ok=True)
        cloudmask_dir.mkdir(parents=True, exist_ok=True)
        masked_tmp_dir.mkdir(parents=True, exist_ok=True)
        cloudmask_tmp_dir.mkdir(parents=True, exist_ok=True)
    if not img_only and snowmask_enabled:
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
                dn_add_offset = _sentinel2_dn_add_offset(item)
            else:
                prefix, conversion_sat_type = _landsat_prefix(item)
                dn_add_offset = 0.0
            LOGGER.debug(
                "item id=%s s2:processing_baseline=%r dn_add_offset=%s",
                getattr(item, "id", None),
                item.properties.get("s2:processing_baseline"),
                dn_add_offset,
            )

            scene_tag = re.sub(r"[^A-Za-z0-9]", "", item.id)[-24:] or "SCENE"
            scene_stem = f"{prefix}_{date_token}_{scene_tag}"
            img_stack_path = img_dir / f"{scene_stem}.tif"

            if file_exists_mode == "skip" and img_stack_path.exists():
                LOGGER.info(
                    "Skipping existing scene %s for date %s (img exists: %s)",
                    item.id,
                    date_token,
                    img_stack_path,
                )
                continue
            group_key = (prefix, date_token)

            cloudmask_path = cloudmask_tmp_dir / f"{scene_stem}_cloudmask.tif"
            masked_stack_path = masked_tmp_dir / f"{scene_stem}_masked.tif"
            final_masked_path = masked_dir / f"{scene_stem}_masked.tif"
            if not img_only and (masked_stack_path.exists() or final_masked_path.exists()):
                LOGGER.info("Skipping scene %s: masked output already exists", scene_stem)
                grouped_masked[group_key].append(masked_stack_path if masked_stack_path.exists() else final_masked_path)
                if cloudmask_path.exists():
                    grouped_cloudmask[group_key].append(cloudmask_path)
                processed_items += 1
                continue

            LOGGER.info("Starting download for scene %s", scene_stem)
            download_info = _download_item_stack(
                item=item,
                geometry_wgs84=geometry_wgs84,
                target_resolution=target_resolution,
                band_map=band_map,
                download_band_numbers=download_band_numbers,
                output_stack_path=img_stack_path,
                conversion_satellite_type=conversion_sat_type,
                dn_add_offset=dn_add_offset,
                output_crs_override=output_crs_override,
                use_bbox_extent=use_bbox_extent,
                snap_to_resolution_grid=snap_to_resolution_grid,
            )
            LOGGER.info("Finished download for scene %s", scene_stem)

            if reference_raster_path is None:
                reference_raster_path = str(img_stack_path)

            if output_crs is None:
                output_crs = _crs_to_string(download_info["profile"].get("crs"))
                if output_crs is None:
                    output_crs = _infer_item_output_crs(item)

            if metadata_enabled:
                feature = _build_metadata_feature(item, dn_add_offset=dn_add_offset)
                if feature is not None:
                    metadata_features.append(feature)

            group_key = (prefix, date_token)
            processed_items += 1

            if img_only:
                continue

            LOGGER.info("Starting cloudmask for scene %s", scene_stem)
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
                conversion_dn_add_offset=dn_add_offset,
            )
            LOGGER.info("Finished cloudmask for scene %s", scene_stem)
            grouped_masked[group_key].append(masked_stack_path)
            grouped_cloudmask[group_key].append(cloudmask_path)

            if snowmask_enabled:
                LOGGER.info("Starting snowmask for scene %s", scene_stem)
                snowmask_path = snowmask_tmp_dir / f"{scene_stem}_snowmask.tif"
                _create_ndsi_snow_mask_local(
                    image_path=img_stack_path,
                    output_path=snowmask_path,
                    satellite_key=satellite_key,
                    download_band_numbers=download_info["band_numbers"],
                    ndsi_threshold=ndsi_threshold,
                    red_threshold=red_threshold,
                    dn_add_offset=dn_add_offset,
                )

                snowmasked_stack_path = snowmasked_tmp_dir / f"{scene_stem}_snowmasked.tif"
                _apply_cloud_mask_local(
                    image_path=img_stack_path,
                    mask_path=cloudmask_path,
                    output_path=snowmasked_stack_path,
                    mask_classes=cloudmask_classes,
                    satellite_type=conversion_sat_type,
                    snow_mask_path=snowmask_path,
                    dn_add_offset=dn_add_offset,
                )
                LOGGER.info("Finished snowmask for scene %s", scene_stem)
                grouped_snowmasked[group_key].append(snowmasked_stack_path)
                grouped_snowmask[group_key].append(snowmask_path)

        masked_items = 0
        if img_only:
            if metadata_enabled and metadata_features:
                _save_metadata_geojson(metadata_features, metadata_output_path)
            return {
                "searched_items": len(items),
                "processed_items": processed_items,
                "masked_items": 0,
                "snowmasked_items": 0,
                "cloudmask_items": 0,
                "snowmask_enabled": snowmask_enabled,
                "img_only": img_only,
                "output_crs": output_crs,
                "reference_raster": reference_raster_path,
                "metadata_enabled": metadata_enabled,
                "metadata_file": str(metadata_output_path) if metadata_enabled and metadata_features else None,
            }

        LOGGER.info("Starting masked composite for window %s-%s", start_date, end_date)
        for (prefix, date_token), paths in sorted(grouped_masked.items()):
            composite, profile = _composite_stacks(
                paths,
                reducer="min",
                resampling_method=Resampling.nearest,
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
        LOGGER.info("Starting cloudmask composite for window %s-%s", start_date, end_date)
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
            LOGGER.info("Starting snowmasked composite for window %s-%s", start_date, end_date)
            for (prefix, date_token), paths in sorted(grouped_snowmasked.items()):
                composite, profile = _composite_stacks(
                    paths,
                    reducer="min",
                    resampling_method=Resampling.nearest,
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
            "img_only": img_only,
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
