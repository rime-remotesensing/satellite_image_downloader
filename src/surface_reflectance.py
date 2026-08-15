from __future__ import annotations

import json
import logging
import os
import re
import shutil
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import rasterio
from rasterio.features import bounds as geometry_bounds, geometry_mask
from rasterio.io import MemoryFile
from rasterio.merge import merge as rio_merge
from rasterio.transform import Affine
from rasterio.warp import transform_geom
from rasterio.windows import Window, from_bounds
from rasterio.windows import transform as window_transform

from .config import _as_bool, _load_env_kv_file, _resolve_runtime_path
from .constants import (
    MODIS_QA_BANDS,
    MODIS_SDS_NAME_MAP,
    MODIS_SINUSOIDAL_PROJ4,
    MODIS_SINUSOIDAL_X_MIN,
    MODIS_SINUSOIDAL_Y_MAX,
    MODIS_SR_BANDS,
    MODIS_TILE_SIZE_M,
    VIIRS_QA_BANDS,
    VIIRS_SDS_PATH_MAP,
    VIIRS_SR_BANDS_1KM,
    VIIRS_SR_BANDS_500M,
)

LOGGER = logging.getLogger(__name__)

# (field_name, kind) per satellite. kind == "sr" -> scaled float32 reflectance,
# written under a resolution-named folder. kind == "qa" -> raw integer
# QA/bitmask/observation data, written under qa/, never scaled.
_FIELD_SPECS: Dict[str, List[Tuple[str, str]]] = {
    "modis": [(b, "sr") for b in MODIS_SR_BANDS] + [(b, "qa") for b in MODIS_QA_BANDS],
    "viirs": [(b, "sr") for b in VIIRS_SR_BANDS_500M + VIIRS_SR_BANDS_1KM]
    + [(b, "qa") for b in VIIRS_QA_BANDS],
}

_FILENAME_DATE_RE = re.compile(r"\.A(\d{7})\.")
_FILENAME_TILE_RE = re.compile(r"\.(h\d{2}v\d{2})\.")
_TILE_RE = re.compile(r"h(\d{2})v(\d{2})")

_AUTH_CACHE: Dict[str, Any] = {}


# ---------------------------------------------------------------------------
# Earthdata authentication
# ---------------------------------------------------------------------------

def _earthdata_login(config: Dict[str, Any], config_dir: Path) -> Any:
    """Authenticate against NASA Earthdata Login via earthaccess.

    Credential resolution order (never read from config.yaml itself):
      1. EARTHDATA_USERNAME / EARTHDATA_PASSWORD environment variables
         (settable via `docker compose run -e ...` or docker-compose.yml).
      2. The same key-value env file used for the FIRMS API key
         (surface_reflectance.earthdata_env_path, default ./key.env),
         which is already git-ignored.
      3. A netrc file (~/.netrc, or %USERPROFILE%\\_netrc on Windows) with:
         machine urs.earthdata.nasa.gov login <user> password <pass>
    """
    if "auth" in _AUTH_CACHE:
        return _AUTH_CACHE["auth"]

    import earthaccess

    sr_cfg = config.get("surface_reflectance", {}) or {}
    key_env_path_value = str(sr_cfg.get("earthdata_env_path", "key.env")).strip() or "key.env"
    key_env_path = _resolve_runtime_path(key_env_path_value, config_dir, must_exist=False)
    env_values = _load_env_kv_file(key_env_path)

    username = os.environ.get("EARTHDATA_USERNAME") or env_values.get("EARTHDATA_USERNAME")
    password = os.environ.get("EARTHDATA_PASSWORD") or env_values.get("EARTHDATA_PASSWORD")

    auth = None
    if username and password:
        os.environ["EARTHDATA_USERNAME"] = username
        os.environ["EARTHDATA_PASSWORD"] = password
        try:
            auth = earthaccess.login(strategy="environment")
        except Exception as exc:
            LOGGER.debug("earthaccess environment login failed: %s", exc)
            auth = None

    if auth is None or not getattr(auth, "authenticated", False):
        try:
            auth = earthaccess.login(strategy="netrc")
        except Exception as exc:
            LOGGER.debug("earthaccess netrc login failed: %s", exc)
            auth = None

    if auth is None or not getattr(auth, "authenticated", False):
        raise RuntimeError(
            "NASA Earthdata authentication failed. Set EARTHDATA_USERNAME / EARTHDATA_PASSWORD "
            f"as environment variables (or in {key_env_path}), or configure a netrc file "
            "(~/.netrc on Linux/Mac, %USERPROFILE%\\_netrc on Windows) containing: "
            "'machine urs.earthdata.nasa.gov login <user> password <pass>'."
        )

    _AUTH_CACHE["auth"] = auth
    return auth


# ---------------------------------------------------------------------------
# CMR search / download
# ---------------------------------------------------------------------------

def _granule_filename(granule: Any) -> str:
    links = granule.data_links()
    for link in links:
        name = link.rsplit("/", 1)[-1]
        if name.lower().endswith((".hdf", ".h5", ".hdf5")):
            return name
    if links:
        return links[0].rsplit("/", 1)[-1]
    raise RuntimeError("Granule has no data links")


def _parse_granule_filename(filename: str) -> Tuple[date, str]:
    date_match = _FILENAME_DATE_RE.search(filename)
    tile_match = _FILENAME_TILE_RE.search(filename)
    if not date_match or not tile_match:
        raise ValueError(f"Cannot parse acquisition date/tile from granule filename: {filename}")
    acq_date = datetime.strptime(date_match.group(1), "%Y%j").date()
    return acq_date, tile_match.group(1)


def _search_granules(
    short_name: str,
    version: str,
    bbox: Tuple[float, float, float, float],
    start_date: date,
    end_date: date,
) -> List[Any]:
    import earthaccess

    temporal = (
        f"{start_date.isoformat()}T00:00:00Z",
        f"{(end_date + timedelta(days=1)).isoformat()}T00:00:00Z",
    )
    return list(
        earthaccess.search_data(
            short_name=short_name,
            version=version,
            temporal=temporal,
            bounding_box=bbox,
        )
    )


def _group_granules_by_date(granules: List[Any]) -> Dict[str, List[Any]]:
    grouped: Dict[str, List[Any]] = defaultdict(list)
    for granule in granules:
        try:
            filename = _granule_filename(granule)
            acq_date, _tile = _parse_granule_filename(filename)
        except Exception as exc:
            LOGGER.warning("Skipping granule with unparsable filename: %s", exc)
            continue
        grouped[acq_date.strftime("%Y%m%d")].append(granule)
    return grouped


def _download_granules(granules: List[Any], dest_dir: Path) -> List[Path]:
    import earthaccess

    dest_dir.mkdir(parents=True, exist_ok=True)
    downloaded = earthaccess.download(granules, str(dest_dir))
    paths = [Path(p) for p in downloaded if p]
    if not paths:
        raise RuntimeError("earthaccess.download returned no local files")
    return paths


# ---------------------------------------------------------------------------
# HDF4 (MODIS, via pyhdf) / HDF5 (VIIRS, via h5py) SDS extraction
#
# Both readers resolve field_name -> the actual on-disk SDS name/path using a
# fixed mapping (MODIS_SDS_NAME_MAP / VIIRS_SDS_PATH_MAP in constants.py)
# confirmed against real granules. There is intentionally no tail-matching,
# shape-based search, or "similar name" fallback: a field that isn't at its
# mapped location is a hard error, not something to guess about.
# ---------------------------------------------------------------------------

@dataclass
class _SDSData:
    array: np.ndarray
    scale: Optional[float]
    offset: Optional[float]
    fill_value: Optional[float]
    valid_range: Optional[Tuple[float, float]]
    sds_path: str


def _attr_float(attrs: Dict[str, Any], key: str) -> Optional[float]:
    value = attrs.get(key)
    if value is None:
        return None
    try:
        arr = np.asarray(value).reshape(-1)
        return float(arr[0])
    except (TypeError, ValueError, IndexError):
        return None


def _attr_range(attrs: Dict[str, Any], key: str) -> Optional[Tuple[float, float]]:
    value = attrs.get(key)
    if value is None:
        return None
    try:
        arr = np.asarray(value).reshape(-1)
        if arr.size < 2:
            return None
        return float(arr[0]), float(arr[1])
    except (TypeError, ValueError, IndexError):
        return None


def _read_hdf4_sds(hdf_path: Path, field_name: str) -> _SDSData:
    from pyhdf.SD import SD, SDC

    sds_name = MODIS_SDS_NAME_MAP.get(field_name)
    if sds_name is None:
        raise ValueError(f"No on-disk SDS mapping defined for MODIS field '{field_name}'")

    hdf = SD(str(hdf_path), SDC.READ)
    try:
        available = set(hdf.datasets().keys())
        if sds_name not in available:
            raise ValueError(
                f"{hdf_path.name}: expected SDS '{sds_name}' (for field '{field_name}') not found. "
                f"Available datasets: {sorted(available)}"
            )

        sds = hdf.select(sds_name)
        try:
            array = sds[:]
            attrs = {str(k): v for k, v in sds.attributes().items()}
        finally:
            sds.endaccess()
    finally:
        hdf.end()

    lower_attrs = {k.lower(): v for k, v in attrs.items()}
    scale = _attr_float(lower_attrs, "scale_factor")
    offset = _attr_float(lower_attrs, "add_offset")
    fill_value = _attr_float(lower_attrs, "_fillvalue")
    valid_range = _attr_range(lower_attrs, "valid_range")

    return _SDSData(
        array=array,
        scale=scale,
        offset=offset,
        fill_value=fill_value,
        valid_range=valid_range,
        sds_path=sds_name,
    )


def _read_h5_sds(h5_path: Path, field_name: str) -> _SDSData:
    import h5py

    sds_path = VIIRS_SDS_PATH_MAP.get(field_name)
    if sds_path is None:
        raise ValueError(f"No on-disk SDS path mapping defined for VIIRS field '{field_name}'")

    with h5py.File(h5_path, "r") as f:
        if sds_path not in f:
            raise ValueError(
                f"{h5_path.name}: expected dataset '{sds_path}' (for field '{field_name}') not found."
            )
        ds = f[sds_path]
        if not isinstance(ds, h5py.Dataset):
            raise ValueError(f"{h5_path.name}: '{sds_path}' exists but is not a dataset")
        array = ds[()]
        attrs = {str(k).lower(): v for k, v in ds.attrs.items()}

    scale = _attr_float(attrs, "scale_factor")
    offset = _attr_float(attrs, "add_offset")
    fill_value = _attr_float(attrs, "_fillvalue")
    valid_range = _attr_range(attrs, "valid_range")
    return _SDSData(
        array=array,
        scale=scale,
        offset=offset,
        fill_value=fill_value,
        valid_range=valid_range,
        sds_path=sds_path,
    )


def _read_sds(path: Path, field_name: str) -> _SDSData:
    suffix = path.suffix.lower()
    if suffix == ".hdf":
        return _read_hdf4_sds(path, field_name)
    if suffix in (".h5", ".hdf5"):
        return _read_h5_sds(path, field_name)
    raise ValueError(f"Unsupported granule file extension: {path}")


# ---------------------------------------------------------------------------
# MODIS/VIIRS sinusoidal tile grid
# ---------------------------------------------------------------------------

def _sinusoidal_crs() -> rasterio.crs.CRS:
    return rasterio.crs.CRS.from_proj4(MODIS_SINUSOIDAL_PROJ4)


def _tile_transform(tile: str, height: int, width: int) -> Affine:
    match = _TILE_RE.fullmatch(tile)
    if not match:
        raise ValueError(f"Invalid MODIS/VIIRS sinusoidal tile id: {tile}")
    h = int(match.group(1))
    v = int(match.group(2))
    x_min = MODIS_SINUSOIDAL_X_MIN + h * MODIS_TILE_SIZE_M
    y_max = MODIS_SINUSOIDAL_Y_MAX - v * MODIS_TILE_SIZE_M
    pixel_size_x = MODIS_TILE_SIZE_M / width
    pixel_size_y = MODIS_TILE_SIZE_M / height
    return Affine(pixel_size_x, 0.0, x_min, 0.0, -pixel_size_y, y_max)


def _resolution_label(pixel_size_m: float) -> str:
    if pixel_size_m < 700:
        return "500m"
    if pixel_size_m < 1500:
        return "1km"
    return f"{int(round(pixel_size_m))}m"


# ---------------------------------------------------------------------------
# Mosaic / AOI clip
# ---------------------------------------------------------------------------

def _mosaic_tile_arrays(
    tiles: List[Tuple[np.ndarray, Affine]],
    crs: rasterio.crs.CRS,
    nodata: Optional[float],
) -> Tuple[np.ndarray, Affine]:
    if len(tiles) == 1:
        return tiles[0]

    datasets = []
    memfiles = []
    try:
        for array, transform in tiles:
            memfile = MemoryFile()
            profile: Dict[str, Any] = {
                "driver": "GTiff",
                "height": array.shape[0],
                "width": array.shape[1],
                "count": 1,
                "dtype": array.dtype,
                "crs": crs,
                "transform": transform,
            }
            if nodata is not None:
                profile["nodata"] = nodata
            dataset = memfile.open(**profile)
            dataset.write(array, 1)
            memfiles.append(memfile)
            datasets.append(dataset)

        merge_kwargs: Dict[str, Any] = {"method": "first"}
        if nodata is not None:
            merge_kwargs["nodata"] = nodata
        mosaic_array, mosaic_transform = rio_merge(datasets, **merge_kwargs)
        return mosaic_array[0], mosaic_transform
    finally:
        for dataset in datasets:
            dataset.close()
        for memfile in memfiles:
            memfile.close()


def _aoi_geometry_in_crs(geometry_wgs84: Dict[str, Any], crs: rasterio.crs.CRS) -> Dict[str, Any]:
    return transform_geom("EPSG:4326", crs, geometry_wgs84, precision=6)


def _crop_window(
    transform: Affine, height: int, width: int, aoi_geom: Dict[str, Any]
) -> Tuple[Window, Affine]:
    minx, miny, maxx, maxy = geometry_bounds(aoi_geom)
    left = transform.c
    top = transform.f
    right = left + transform.a * width
    bottom = top + transform.e * height

    ix_min = max(minx, min(left, right))
    ix_max = min(maxx, max(left, right))
    iy_min = max(miny, min(bottom, top))
    iy_max = min(maxy, max(bottom, top))
    if ix_min >= ix_max or iy_min >= iy_max:
        raise ValueError("AOI does not intersect the downloaded tile mosaic extent")

    window = from_bounds(ix_min, iy_min, ix_max, iy_max, transform=transform)
    window = window.round_offsets(op="floor").round_lengths(op="ceil")
    row_off = max(0, int(window.row_off))
    col_off = max(0, int(window.col_off))
    row_end = min(height, row_off + int(window.height))
    col_end = min(width, col_off + int(window.width))
    clipped_window = Window(col_off=col_off, row_off=row_off, width=col_end - col_off, height=row_end - row_off)
    out_transform = window_transform(clipped_window, transform)
    return clipped_window, out_transform


def _apply_window(array: np.ndarray, window: Window) -> np.ndarray:
    row_off = int(window.row_off)
    col_off = int(window.col_off)
    row_end = row_off + int(window.height)
    col_end = col_off + int(window.width)
    return array[row_off:row_end, col_off:col_end]


def _outside_aoi_mask(transform: Affine, height: int, width: int, aoi_geom: Dict[str, Any]) -> np.ndarray:
    return geometry_mask([aoi_geom], out_shape=(height, width), transform=transform, invert=False)


def _apply_scale_offset(
    array: np.ndarray, scale: float, offset: float, fill_value: Optional[float]
) -> np.ndarray:
    result = array.astype(np.float32) * np.float32(scale) + np.float32(offset)
    if fill_value is not None:
        result[array == fill_value] = np.nan
    return result


# ---------------------------------------------------------------------------
# GeoTIFF output
# ---------------------------------------------------------------------------

def _write_geotiff_group(
    output_path: Path,
    bands: List[Tuple[str, np.ndarray]],
    *,
    crs: rasterio.crs.CRS,
    transform: Affine,
    dtype: str,
    nodata: Optional[float],
    tags: Dict[str, Any],
    band_tags: List[Dict[str, Any]],
) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    height, width = bands[0][1].shape

    profile: Dict[str, Any] = {
        "driver": "GTiff",
        "height": height,
        "width": width,
        "count": len(bands),
        "dtype": dtype,
        "crs": crs,
        "transform": transform,
        "compress": "lzw",
    }
    if nodata is not None:
        profile["nodata"] = nodata

    with rasterio.open(output_path, "w", **profile) as dst:
        for idx, (name, array) in enumerate(bands, start=1):
            dst.write(array.astype(dtype), idx)
            dst.set_band_description(idx, name)
            dst.update_tags(idx, **{k: str(v) for k, v in band_tags[idx - 1].items()})
        dst.update_tags(**{k: str(v) for k, v in tags.items()})

    sidecar_path = output_path.with_suffix(".json")
    sidecar = dict(tags)
    sidecar["bands"] = [
        {"band_index": idx, "name": name, **band_tags[idx - 1]}
        for idx, (name, _array) in enumerate(bands, start=1)
    ]
    with sidecar_path.open("w", encoding="utf-8") as f:
        json.dump(sidecar, f, ensure_ascii=False, indent=2, default=str)


# ---------------------------------------------------------------------------
# Per-date processing
# ---------------------------------------------------------------------------

def _process_date_for_platform(
    *,
    satellite_key: str,
    platform_key: str,
    short_name: str,
    version: str,
    granule_files: List[Path],
    acq_date: date,
    out_platform_dir: Path,
    clip_to_aoi: bool,
    aoi_geom_wgs84: Dict[str, Any],
    file_exists_mode: str,
) -> Dict[str, Any]:
    field_specs = _FIELD_SPECS[satellite_key]
    crs = _sinusoidal_crs()
    aoi_geom_native = _aoi_geometry_in_crs(aoi_geom_wgs84, crs) if clip_to_aoi else None

    date_token = acq_date.strftime("%Y%m%d")
    tiles_used = sorted({_parse_granule_filename(f.name)[1] for f in granule_files})
    source_granules = sorted({f.name for f in granule_files})

    groups: Dict[Tuple[str, str], List[Dict[str, Any]]] = defaultdict(list)

    for field_name, kind in field_specs:
        tile_entries: List[Tuple[np.ndarray, Affine]] = []
        sds_meta: Optional[_SDSData] = None

        for gfile in granule_files:
            _, tile = _parse_granule_filename(gfile.name)
            sds = _read_sds(gfile, field_name)
            transform = _tile_transform(tile, sds.array.shape[0], sds.array.shape[1])
            tile_entries.append((sds.array, transform))
            if sds_meta is None:
                sds_meta = sds

        if not tile_entries or sds_meta is None:
            continue

        mosaic_array, mosaic_transform = _mosaic_tile_arrays(
            tile_entries, crs=crs, nodata=sds_meta.fill_value
        )
        height, width = mosaic_array.shape
        pixel_size = abs(mosaic_transform.a)

        if clip_to_aoi and aoi_geom_native is not None:
            window, out_transform = _crop_window(mosaic_transform, height, width, aoi_geom_native)
            mosaic_array = _apply_window(mosaic_array, window)
            out_height, out_width = mosaic_array.shape
            outside_mask = _outside_aoi_mask(out_transform, out_height, out_width, aoi_geom_native)
        else:
            out_transform = mosaic_transform
            outside_mask = None

        resolution_label = _resolution_label(pixel_size)

        if kind == "sr":
            if sds_meta.scale is None:
                raise ValueError(
                    f"{short_name} field '{field_name}': scale_factor attribute is missing in the "
                    "source file; refusing to assume a value."
                )
            offset = sds_meta.offset if sds_meta.offset is not None else 0.0
            processed = _apply_scale_offset(mosaic_array, sds_meta.scale, offset, sds_meta.fill_value)
            if outside_mask is not None:
                processed[outside_mask] = np.nan
        else:
            offset = None
            processed = mosaic_array
            if outside_mask is not None and sds_meta.fill_value is not None:
                processed = processed.copy()
                processed[outside_mask] = sds_meta.fill_value

        groups[(kind, resolution_label)].append(
            {
                "field_name": field_name,
                "array": processed,
                "transform": out_transform,
                "scale": sds_meta.scale if kind == "sr" else None,
                "offset": offset if kind == "sr" else None,
                "fill_value": sds_meta.fill_value,
                "valid_range": sds_meta.valid_range,
                "sds_path": sds_meta.sds_path,
                "premask_shape": (height, width),
            }
        )

    written_files: List[str] = []

    for (kind, resolution_label), entries in groups.items():
        shapes = {e["array"].shape for e in entries}
        if len(shapes) > 1:
            raise RuntimeError(
                f"{short_name} {platform_key} {date_token}: inconsistent array shapes within "
                f"{kind}/{resolution_label} group: {shapes}"
            )

        if kind == "sr":
            out_dir = out_platform_dir / resolution_label
            suffix = entries[0]["field_name"] if len(entries) == 1 else resolution_label
            common_dtype = "float32"
            common_nodata: Optional[float] = float("nan")
        else:
            out_dir = out_platform_dir / "qa"
            suffix = entries[0]["field_name"] if len(entries) == 1 else f"QA_{resolution_label}"
            dtypes = [e["array"].dtype for e in entries]
            # QA/bit-field/observation-count data must never be narrowed from its
            # source SDS dtype (e.g. MOD09GA QC_500m is uint32). np.result_type()
            # only ever widens to a common supertype, but we assert it explicitly
            # here so a future change can't silently introduce a narrowing cast.
            common_dtype = str(np.result_type(*dtypes))
            for e in entries:
                if not np.can_cast(e["array"].dtype, common_dtype, casting="safe"):
                    raise RuntimeError(
                        f"{short_name} {platform_key} {date_token}: refusing to narrow QA field "
                        f"'{e['field_name']}' from {e['array'].dtype} to {common_dtype}"
                    )
            fill_values = {e["fill_value"] for e in entries}
            common_nodata = entries[0]["fill_value"] if len(fill_values) == 1 else None

        out_path = out_dir / f"{short_name}_{platform_key}_{date_token}_{suffix}.tif"

        if file_exists_mode == "skip" and out_path.exists():
            written_files.append(str(out_path))
            continue

        bands = [(e["field_name"], e["array"]) for e in entries]
        band_tags = [
            {
                "sds_name": e["sds_path"],
                "scale_factor": "" if e["scale"] is None else e["scale"],
                "add_offset": "" if e["offset"] is None else e["offset"],
                "fill_value": "" if e["fill_value"] is None else e["fill_value"],
                "valid_range": "" if e["valid_range"] is None else list(e["valid_range"]),
                "premask_shape": list(e["premask_shape"]),
            }
            for e in entries
        ]
        dataset_tags = {
            "product": short_name,
            "product_version": version,
            "platform": platform_key,
            "satellite": satellite_key,
            "acquisition_date": date_token,
            "tiles": ",".join(tiles_used),
            "source_granules": ",".join(source_granules),
            "kind": kind,
            "resolution_label": resolution_label,
            "clip_to_aoi": clip_to_aoi,
        }

        _write_geotiff_group(
            out_path,
            bands,
            crs=crs,
            transform=entries[0]["transform"],
            dtype=common_dtype,
            nodata=common_nodata,
            tags=dataset_tags,
            band_tags=band_tags,
        )
        written_files.append(str(out_path))

    return {"date": date_token, "tiles": tiles_used, "files": written_files}


# ---------------------------------------------------------------------------
# Per-platform orchestration
# ---------------------------------------------------------------------------

def _date_range(start_date: date, end_date: date) -> List[date]:
    days = (end_date - start_date).days
    return [start_date + timedelta(days=i) for i in range(days + 1)]


def _expected_primary_outputs(
    satellite_key: str, short_name: str, platform_key: str, out_platform_dir: Path, date_token: str
) -> List[Path]:
    if satellite_key == "modis":
        return [out_platform_dir / "500m" / f"{short_name}_{platform_key}_{date_token}_500m.tif"]
    if satellite_key == "viirs":
        return [
            out_platform_dir / "500m" / f"{short_name}_{platform_key}_{date_token}_500m.tif",
            out_platform_dir / "1km" / f"{short_name}_{platform_key}_{date_token}_1km.tif",
        ]
    raise ValueError(f"Unsupported satellite_key: {satellite_key}")


def _process_platform(
    *,
    config: Dict[str, Any],
    config_dir: Path,
    output_root: Path,
    geometry_wgs84: Dict[str, Any],
    bbox: Tuple[float, float, float, float],
    start_date: date,
    end_date: date,
    satellite_key: str,
    platform_key: str,
    short_name: str,
    version: str,
    out_satellite_dir_name: str,
) -> Dict[str, Any]:
    sr_cfg = config.get("surface_reflectance", {}) or {}
    clip_to_aoi = _as_bool(sr_cfg.get("clip_to_aoi"), default=True)
    keep_native_files = _as_bool(sr_cfg.get("keep_native_files"), default=False)

    file_exists_mode = str(config.get("file_exists", "skip")).strip().lower()
    if file_exists_mode not in {"overwrite", "skip"}:
        raise ValueError("config.file_exists must be 'overwrite' or 'skip'")

    out_platform_dir = output_root / out_satellite_dir_name / "surface_reflectance" / platform_key

    summary: Dict[str, Any] = {
        "short_name": short_name,
        "version": version,
        "platform": platform_key,
        "dates_processed": [],
        "dates_skipped": [],
        "dates_failed": [],
    }

    dates_to_process: List[date] = []
    for current_date in _date_range(start_date, end_date):
        date_token = current_date.strftime("%Y%m%d")
        expected = _expected_primary_outputs(
            satellite_key, short_name, platform_key, out_platform_dir, date_token
        )
        if file_exists_mode == "skip" and all(p.exists() for p in expected):
            LOGGER.info("Skipping %s %s %s: outputs already exist", short_name, platform_key, date_token)
            summary["dates_skipped"].append(date_token)
        else:
            dates_to_process.append(current_date)

    if not dates_to_process:
        return summary

    try:
        _earthdata_login(config, config_dir)
    except Exception as exc:
        LOGGER.error("Earthdata authentication failed: %s", exc)
        summary["error"] = str(exc)
        return summary

    try:
        granules = _search_granules(
            short_name=short_name,
            version=version,
            bbox=bbox,
            start_date=dates_to_process[0],
            end_date=dates_to_process[-1],
        )
    except Exception as exc:
        LOGGER.error("CMR search failed for %s: %s", short_name, exc, exc_info=True)
        summary["error"] = str(exc)
        return summary

    granules_by_date = _group_granules_by_date(granules)

    tmp_root = output_root / out_satellite_dir_name / "surface_reflectance" / "_tmp" / platform_key
    tmp_root.mkdir(parents=True, exist_ok=True)

    try:
        for current_date in dates_to_process:
            date_token = current_date.strftime("%Y%m%d")
            day_granules = granules_by_date.get(date_token, [])
            if not day_granules:
                LOGGER.warning("No %s granules found for %s on %s", short_name, platform_key, current_date)
                summary["dates_skipped"].append(date_token)
                continue

            tmp_dir = tmp_root / date_token
            try:
                local_files = _download_granules(day_granules, tmp_dir)
                result = _process_date_for_platform(
                    satellite_key=satellite_key,
                    platform_key=platform_key,
                    short_name=short_name,
                    version=version,
                    granule_files=local_files,
                    acq_date=current_date,
                    out_platform_dir=out_platform_dir,
                    clip_to_aoi=clip_to_aoi,
                    aoi_geom_wgs84=geometry_wgs84,
                    file_exists_mode=file_exists_mode,
                )
                summary["dates_processed"].append(result)
            except Exception as exc:
                LOGGER.error(
                    "Failed to process %s %s on %s: %s",
                    short_name,
                    platform_key,
                    current_date,
                    exc,
                    exc_info=True,
                )
                summary["dates_failed"].append({"date": date_token, "error": str(exc)})
            finally:
                if not keep_native_files:
                    shutil.rmtree(tmp_dir, ignore_errors=True)
    finally:
        if not keep_native_files:
            shutil.rmtree(tmp_root, ignore_errors=True)

    return summary


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------

def _process_modis_surface_reflectance(
    config: Dict[str, Any],
    config_dir: Path,
    output_root: Path,
    geometry_wgs84: Dict[str, Any],
    bbox: Tuple[float, float, float, float],
    start_date: date,
    end_date: date,
) -> Dict[str, Any]:
    sr_cfg = config.get("surface_reflectance", {}) or {}
    products_cfg = (sr_cfg.get("products", {}) or {}).get("modis", {}) or {}
    version = str(products_cfg.get("version", "061"))
    terra_short_name = str(products_cfg.get("terra", "MOD09GA"))
    aqua_short_name = str(products_cfg.get("aqua", "MYD09GA"))

    result: Dict[str, Any] = {}
    for platform_key, short_name in (("terra", terra_short_name), ("aqua", aqua_short_name)):
        LOGGER.info("Processing MODIS %s surface reflectance (%s)...", platform_key, short_name)
        result[platform_key] = _process_platform(
            config=config,
            config_dir=config_dir,
            output_root=output_root,
            geometry_wgs84=geometry_wgs84,
            bbox=bbox,
            start_date=start_date,
            end_date=end_date,
            satellite_key="modis",
            platform_key=platform_key,
            short_name=short_name,
            version=version,
            out_satellite_dir_name="modis",
        )
    return result


def _process_viirs_surface_reflectance(
    config: Dict[str, Any],
    config_dir: Path,
    output_root: Path,
    geometry_wgs84: Dict[str, Any],
    bbox: Tuple[float, float, float, float],
    start_date: date,
    end_date: date,
) -> Dict[str, Any]:
    sr_cfg = config.get("surface_reflectance", {}) or {}
    products_cfg = (sr_cfg.get("products", {}) or {}).get("viirs", {}) or {}
    version = str(products_cfg.get("version", "002"))
    snpp_short_name = str(products_cfg.get("snpp", "VNP09GA"))

    LOGGER.info("Processing VIIRS SNPP surface reflectance (%s)...", snpp_short_name)
    return {
        "snpp": _process_platform(
            config=config,
            config_dir=config_dir,
            output_root=output_root,
            geometry_wgs84=geometry_wgs84,
            bbox=bbox,
            start_date=start_date,
            end_date=end_date,
            satellite_key="viirs",
            platform_key="snpp",
            short_name=snpp_short_name,
            version=version,
            out_satellite_dir_name="viirs",
        )
    }
