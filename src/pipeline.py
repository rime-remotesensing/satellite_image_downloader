from __future__ import annotations

import csv
import io
import json
import logging
import math
import re
import shutil
from collections import defaultdict
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np
import planetary_computer
import requests
import rasterio
from pystac.item import Item
from pystac_client import Client
from rasterio.enums import Resampling
from rasterio.features import bounds as geometry_bounds
from rasterio.transform import Affine, from_origin
from rasterio.vrt import WarpedVRT
from rasterio.warp import reproject, transform_geom

try:
    import yaml
except ImportError as exc:  # pragma: no cover
    raise RuntimeError("PyYAML is required. Please install pyyaml.") from exc

try:
    import shapefile
except ImportError as exc:  # pragma: no cover
    raise RuntimeError("pyshp is required. Please install pyshp.") from exc


LOGGER = logging.getLogger(__name__)

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

    data = raw_data * preset["scale"] + preset["offset"]
    data = np.clip(data, 0.0, 1.0)

    mask_target = np.isin(cloud_mask, mask_classes)
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


def _load_config(config_path: Path) -> Dict[str, Any]:
    with config_path.open("r", encoding="utf-8") as f:
        loaded = yaml.safe_load(f)
    return loaded or {}


def _to_date(value: str) -> date:
    return datetime.strptime(value, "%Y%m%d").date()


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


def _resolve_band_request(config: Dict[str, Any], satellite_key: str) -> Tuple[List[int], List[int]]:
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
    downloaded = sorted(set(requested + required))
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
            resampling=Resampling.bilinear,
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

    download_band_numbers, requested_band_numbers = _resolve_band_request(config, satellite_key)
    target_resolution = TARGET_RESOLUTION[satellite_key]

    if satellite_key == "sentinel2":
        collection = SENTINEL_COLLECTION
        out_sat_dir = output_root / "sentinel2"
        band_map = SENTINEL_BAND_MAP
    else:
        collection = LANDSAT_COLLECTION
        out_sat_dir = output_root / "landsat89"
        band_map = LANDSAT_BAND_MAP

    raw_dir = out_sat_dir / "raw"
    img_dir = out_sat_dir / "img"
    stack_tmp_dir = out_sat_dir / "_stack_tmp"
    masked_tmp_dir = out_sat_dir / "_masked_tmp"

    raw_dir.mkdir(parents=True, exist_ok=True)
    img_dir.mkdir(parents=True, exist_ok=True)
    stack_tmp_dir.mkdir(parents=True, exist_ok=True)
    masked_tmp_dir.mkdir(parents=True, exist_ok=True)

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
            "composites": 0,
        }

    grouped_masked: Dict[Tuple[str, str], List[Path]] = defaultdict(list)
    processed_items = 0

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
            stack_path = stack_tmp_dir / f"{prefix}_{date_token}_{scene_tag}_stack.tif"

            download_info = _download_item_stack(
                item=item,
                geometry_wgs84=geometry_wgs84,
                target_resolution=target_resolution,
                band_map=band_map,
                download_band_numbers=download_band_numbers,
                output_stack_path=stack_path,
            )

            _write_band_files_from_stack(
                stack_path=stack_path,
                output_dir=raw_dir,
                base_stem=f"{prefix}_{date_token}",
                band_numbers=download_info["band_numbers"],
                requested_band_numbers=download_info["band_numbers"],
                band_map=band_map,
            )

            cloudmask_path = _unique_tif_path(raw_dir, f"{prefix}_{date_token}_omnicloudmask")
            masked_stack_path = masked_tmp_dir / f"{prefix}_{date_token}_{scene_tag}_masked.tif"

            _run_cloudmask_and_mask(
                stack_path=stack_path,
                cloudmask_path=cloudmask_path,
                masked_stack_path=masked_stack_path,
                satellite_key=satellite_key,
                target_resolution=target_resolution,
                download_band_numbers=download_info["band_numbers"],
                cloudmask_classes=cloudmask_classes,
                omnicloudmask_cfg=omnicloudmask_cfg,
                conversion_satellite_type=conversion_sat_type,
            )

            grouped_masked[(prefix, date_token)].append(masked_stack_path)
            processed_items += 1

        composites = 0
        for (prefix, date_token), masked_paths in grouped_masked.items():
            if not masked_paths:
                continue

            composite, profile = _composite_masked_stacks(masked_paths)
            _write_composite_bands(
                composite=composite,
                profile=profile,
                output_dir=img_dir,
                base_stem=f"{prefix}_{date_token}",
                band_numbers=download_band_numbers,
                requested_band_numbers=requested_band_numbers,
                band_map=band_map,
            )
            composites += 1

        return {
            "searched_items": len(items),
            "processed_items": processed_items,
            "composites": composites,
        }
    finally:
        shutil.rmtree(stack_tmp_dir, ignore_errors=True)
        shutil.rmtree(masked_tmp_dir, ignore_errors=True)


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


def _write_activefire_shapefile(output_shp_path: Path, rows: List[Dict[str, str]]) -> None:
    output_shp_path.parent.mkdir(parents=True, exist_ok=True)

    writer = shapefile.Writer(str(output_shp_path), shapeType=shapefile.POINT)

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

        writer.point(lon, lat)
        record = [str(row.get(field, ""))[:254] for field in source_fields]
        writer.record(*record)

    writer.close()

    prj_path = output_shp_path.with_suffix(".prj")
    prj_path.write_text(WGS84, encoding="utf-8")


def _fetch_firms_rows(
    api_key: str,
    product: str,
    bbox: Tuple[float, float, float, float],
    days: int,
    base_url: str,
) -> List[Dict[str, str]]:
    west, south, east, north = bbox
    bbox_token = f"{west:.6f},{south:.6f},{east:.6f},{north:.6f}"
    url = f"{base_url.rstrip('/')}/{api_key}/{product}/{bbox_token}/{days}"

    response = requests.get(url, timeout=120)
    response.raise_for_status()

    text = response.text.strip()
    if not text:
        return []

    reader = csv.DictReader(io.StringIO(text))
    return [dict(row) for row in reader]


def _process_activefire(
    config: Dict[str, Any],
    output_root: Path,
    bbox: Tuple[float, float, float, float],
    start_date: date,
    end_date: date,
    satellites: List[str],
) -> Dict[str, Any]:
    firms_cfg = config.get("firms", {})
    api_key = str(firms_cfg.get("api_key", "")).strip()
    if not api_key:
        LOGGER.warning("FIRMS api_key is not set. Active fire download is skipped.")
        return {"modis": 0, "viirs": 0}

    base_url = str(
        firms_cfg.get(
            "base_url",
            "https://firms.modaps.eosdis.nasa.gov/api/area/csv",
        )
    )

    product_map = firms_cfg.get("product_map", {})
    modis_product = str(product_map.get("modis", "MODIS_NRT"))
    viirs_product = str(product_map.get("viirs", "VIIRS_SNPP_NRT"))

    days = int(firms_cfg.get("days", (end_date - start_date).days + 1))
    days = max(1, min(days, 365))

    utc_token = datetime.now(timezone.utc).strftime("%H%M")
    summary = {"modis": 0, "viirs": 0}

    for sat in satellites:
        product = modis_product if sat == "modis" else viirs_product
        out_dir = output_root / sat / "activefire"

        rows = _fetch_firms_rows(
            api_key=api_key,
            product=product,
            bbox=bbox,
            days=days,
            base_url=base_url,
        )

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
                filtered[acq_date.strftime("%Y%m%d")].append(row)

        for date_token, rows_for_day in filtered.items():
            shp_path = out_dir / f"ACFR_{date_token}_{utc_token}.shp"
            _write_activefire_shapefile(shp_path, rows_for_day)
            summary[sat] += 1

    return summary


def run_pipeline(config: Dict[str, Any], config_dir: Path) -> Dict[str, Any]:
    if "geojson" not in config:
        raise ValueError("config.geojson is required")
    if "startday" not in config or "endday" not in config:
        raise ValueError("config.startday and config.endday are required")
    if "satellite" not in config:
        raise ValueError("config.satellite is required")

    geojson_path = Path(config["geojson"])
    if not geojson_path.is_absolute():
        geojson_path = (config_dir / geojson_path).resolve()

    output_root = Path(config.get("output", "output"))
    if not output_root.is_absolute():
        output_root = (config_dir / output_root).resolve()

    start_date = _to_date(str(config["startday"]))
    end_date = _to_date(str(config["endday"]))
    if end_date < start_date:
        raise ValueError("endday must be greater than or equal to startday")

    satellites = _normalize_satellites(config["satellite"])
    geometry_wgs84 = _load_aoi_geometry(geojson_path)
    bbox = _bbox_from_geometry(geometry_wgs84)

    output_root.mkdir(parents=True, exist_ok=True)

    summary: Dict[str, Any] = {
        "config": {
            "geojson": str(geojson_path),
            "startday": start_date.strftime("%Y%m%d"),
            "endday": end_date.strftime("%Y%m%d"),
            "satellite": satellites,
            "output": str(output_root),
        }
    }

    if "sentinel2" in satellites:
        LOGGER.info("Processing Sentinel-2 imagery...")
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
        LOGGER.info("Processing Landsat 8/9 imagery...")
        summary["landsat89"] = _process_satellite_imagery(
            config=config,
            config_dir=config_dir,
            output_root=output_root,
            geometry_wgs84=geometry_wgs84,
            start_date=start_date,
            end_date=end_date,
            satellite_key="landsat89",
        )

    activefire_targets = [s for s in satellites if s in {"modis", "viirs"}]
    if activefire_targets:
        LOGGER.info("Processing FIRMS active fire data...")
        summary["activefire"] = _process_activefire(
            config=config,
            output_root=output_root,
            bbox=bbox,
            start_date=start_date,
            end_date=end_date,
            satellites=activefire_targets,
        )

    return summary


def run_pipeline_from_config(config_path: Path) -> Dict[str, Any]:
    config_path = config_path.resolve()
    if not config_path.exists():
        raise FileNotFoundError(f"Config file does not exist: {config_path}")

    config = _load_config(config_path)
    return run_pipeline(config=config, config_dir=config_path.parent)


def satellite_image_downloader(
    satellite_type: Sequence[str] | str,
    geojson_path: str,
    sdate: str,
    edate: str,
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
