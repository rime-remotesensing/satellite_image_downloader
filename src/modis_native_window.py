"""Regenerate MODIS native-window SR/QA GeoTIFFs from original HDF granules.

Given, for each output file, a reference native grid (transform + shape),
this extracts exactly that window from the native Sinusoidal tile mosaic of
the original MOD09GA/MYD09GA .061 HDF granules -- by integer pixel offset,
with no reprojection, resampling, padding or AOI-polygon masking. Sensor
fill stays as fill (NaN for scaled SR).

Output layout and band/dtype/nodata conventions are identical to the
research side's authoritative images:

  <root>/<region>/<year>/surface_reflectance/<platform>/500m/<P>_<platform>_<date>_500m.tif
  <root>/<region>/<year>/surface_reflectance/<platform>/qa/<P>_<platform>_<date>_QC_500m.tif
  <root>/<region>/<year>/surface_reflectance/<platform>/qa/<P>_<platform>_<date>_state_1km.tif

(The official images moved from authoritative_halo_v2/.../surface_reflectance_halo/ to
Aso/MODIS/<region>/<year>/surface_reflectance/ on 2026-10-26; content unchanged.)

  500m: 7 bands sur_refl_b01..b07, float32 reflectance (DN*scale+offset), nodata NaN
  QC_500m: 1 band uint32 raw, nodata unset
  state_1km: 1 band uint16 raw at the native 1 km grid, nodata unset
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np
import rasterio
from rasterio.transform import Affine

from .constants import MODIS_SR_BANDS
from .surface_reflectance import (
    _mosaic_tile_arrays,
    _read_sds,
    _sinusoidal_crs,
    _tile_transform,
    _write_geotiff_group,
)

PRODUCT_BY_PLATFORM = {"terra": "MOD09GA", "aqua": "MYD09GA"}
SUBPIXEL_TOLERANCE = 1e-6
OUTPUT_SUBDIR = "surface_reflectance"  # same as the official Aso/MODIS/<region>/<year>/surface_reflectance/


class NativeWindowError(RuntimeError):
    pass


@dataclass(frozen=True)
class Acquisition:
    region: str
    platform: str
    date: str  # YYYYMMDD

    @property
    def product(self) -> str:
        return PRODUCT_BY_PLATFORM[self.platform]

    @property
    def stem(self) -> str:
        return f"{self.product}_{self.platform}_{self.date}"


def output_paths(root: Path, acq: Acquisition) -> Dict[str, Path]:
    base = Path(root) / acq.region / acq.date[:4] / OUTPUT_SUBDIR / acq.platform
    return {
        "sr": base / "500m" / f"{acq.stem}_500m.tif",
        "qc_500m": base / "qa" / f"{acq.stem}_QC_500m.tif",
        "state_1km": base / "qa" / f"{acq.stem}_state_1km.tif",
    }


def _tile_of(path: Path) -> str:
    return path.name.split(".")[2]


def extract_native_window(
    hdf_paths: Sequence[Path], field_name: str, ref_transform: Affine, ref_shape: Tuple[int, int]
) -> Tuple[np.ndarray, Any, Affine]:
    """Exact integer-offset window of one field from the native tile mosaic.

    Returns (array, sds_meta, transform). Raises NativeWindowError if the
    reference grid is not aligned to the native grid (sub-pixel offset or
    different pixel size) or if the window is not fully inside the mosaic.
    """
    crs = _sinusoidal_crs()
    tiles = []
    meta = None
    for path in hdf_paths:
        sds = _read_sds(Path(path), field_name)
        tiles.append((sds.array, _tile_transform(_tile_of(Path(path)), *sds.array.shape)))
        meta = meta or sds
    mosaic, mt = _mosaic_tile_arrays(tiles, crs=crs, nodata=meta.fill_value)

    if abs(abs(ref_transform.a) - abs(mt.a)) > 1e-9 or abs(abs(ref_transform.e) - abs(mt.e)) > 1e-9:
        raise NativeWindowError(f"{field_name}: reference pixel size {ref_transform.a} != native {mt.a}")
    col_f = (ref_transform.c - mt.c) / mt.a
    row_f = (ref_transform.f - mt.f) / mt.e
    col0, row0 = int(round(col_f)), int(round(row_f))
    if abs(col_f - col0) > SUBPIXEL_TOLERANCE or abs(row_f - row0) > SUBPIXEL_TOLERANCE:
        raise NativeWindowError(f"{field_name}: reference grid has a sub-pixel offset ({col_f}, {row_f})")
    h, w = ref_shape
    if row0 < 0 or col0 < 0 or row0 + h > mosaic.shape[0] or col0 + w > mosaic.shape[1]:
        raise NativeWindowError(
            f"{field_name}: window rows {row0}:{row0 + h}, cols {col0}:{col0 + w} outside mosaic {mosaic.shape}; "
            "a required granule is missing (no padding is applied)"
        )
    out = mosaic[row0:row0 + h, col0:col0 + w]
    out_transform = Affine(mt.a, 0.0, mt.c + col0 * mt.a, 0.0, mt.e, mt.f + row0 * mt.e)
    return out, meta, out_transform


def _grid_of(path: Path) -> Tuple[Affine, Tuple[int, int]]:
    with rasterio.open(path) as ds:
        return ds.transform, (ds.height, ds.width)


def generate_acquisition(
    acq: Acquisition,
    hdf_paths: Sequence[Path],
    reference_grids: Dict[str, Tuple[Affine, Tuple[int, int]]],
    out_root: Path,
    *,
    extra_tags: Optional[Dict[str, Any]] = None,
) -> Dict[str, str]:
    """Write the 3 files for one acquisition. Refuses to overwrite."""
    paths = output_paths(out_root, acq)
    existing = [str(p) for p in paths.values() if p.exists()]
    if existing:
        raise FileExistsError(f"refusing to overwrite: {existing}")

    crs = _sinusoidal_crs()
    granules = ",".join(sorted(Path(p).name for p in hdf_paths))

    def tags(resolution: str, transform: Affine, kind: str) -> Dict[str, Any]:
        t = {
            "product": acq.product, "product_version": "061", "platform": acq.platform,
            "satellite": "modis", "acquisition_date": acq.date, "region": acq.region,
            "source_granules": granules, "native_crs": crs.to_string(),
            "native_transform": list(transform)[:6], "native_pixel_size_m": abs(transform.a),
            "resolution_label": resolution, "kind": kind,
            "spatial_export_mode": "native_rect_exact_reference_grid",
            "raster_reprojected": False, "resampling": "none", "aoi_polygon_mask_applied": False,
            "observation_time_precision": "day_level_only (MOD09GA/MYD09GA has no per-pixel acquisition time)",
        }
        t.update(extra_tags or {})
        return t

    sr_transform, sr_shape = reference_grids["sr"]
    bands, band_tags, transform = [], [], None
    for field in MODIS_SR_BANDS:
        dn, meta, transform = extract_native_window(hdf_paths, field, sr_transform, sr_shape)
        offset = meta.offset if meta.offset is not None else 0.0
        refl = dn.astype(np.float32) * np.float32(meta.scale) + np.float32(offset)
        refl[dn == meta.fill_value] = np.nan
        bands.append((field, refl))
        band_tags.append({"sds_name": meta.sds_path, "scale_factor": meta.scale, "add_offset": offset,
                          "fill_value": meta.fill_value})
    _write_geotiff_group(paths["sr"], bands, crs=crs, transform=transform, dtype="float32",
                         nodata=float("nan"), tags=tags("500m", transform, "sr"), band_tags=band_tags)

    for field, key, resolution in (("QC_500m", "qc_500m", "500m"), ("state_1km", "state_1km", "1km")):
        ref_t, ref_s = reference_grids[key]
        raw, meta, t = extract_native_window(hdf_paths, field, ref_t, ref_s)
        _write_geotiff_group(paths[key], [(field, raw)], crs=crs, transform=t, dtype=str(raw.dtype),
                             nodata=None, tags=tags(resolution, t, "qa"),
                             band_tags=[{"sds_name": meta.sds_path,
                                         "fill_value": "" if meta.fill_value is None else meta.fill_value}])
    return {k: str(v) for k, v in paths.items()}


def compare_files(generated: Path, reference: Path) -> Dict[str, Any]:
    """Pixel/grid comparison. NaN==NaN counts as equal."""
    with rasterio.open(generated) as g, rasterio.open(reference) as r:
        res: Dict[str, Any] = {
            "shape_equal": (g.count, g.height, g.width) == (r.count, r.height, r.width),
            "transform_equal": all(abs(a - b) < 1e-6 for a, b in zip(tuple(g.transform)[:6], tuple(r.transform)[:6])),
            "crs_equal": g.crs == r.crs,
            "dtype_equal": g.dtypes == r.dtypes,
            "nodata_equal": bool((g.nodata == r.nodata) or (g.nodata is not None and r.nodata is not None
                                                             and np.isnan(g.nodata) and np.isnan(r.nodata))),
        }
        if res["shape_equal"]:
            a, b = g.read(), r.read()
            if a.dtype.kind == "f":
                eq = (a == b) | (np.isnan(a) & np.isnan(b))
            else:
                eq = a == b
            res["values_equal"] = bool(eq.all())
            res["mismatch_pixels"] = int((~eq).sum())
        else:
            res["values_equal"] = False
    res["identical"] = all(v for k, v in res.items() if k.endswith("_equal"))
    return res


def reference_grids_for(reference_root: Path, acq: Acquisition) -> Dict[str, Tuple[Affine, Tuple[int, int]]]:
    paths = output_paths(reference_root, acq)
    missing = [str(p) for p in paths.values() if not p.exists()]
    if missing:
        raise FileNotFoundError(f"reference grid files missing: {missing}")
    return {k: _grid_of(p) for k, p in paths.items()}


def reference_source_granules(reference_root: Path, acq: Acquisition) -> List[str]:
    side = output_paths(reference_root, acq)["sr"].with_suffix(".json")
    data = json.loads(side.read_text(encoding="utf-8"))
    granules = [g.strip() for g in str(data.get("source_granules", "")).split(",") if g.strip()]
    if not granules:
        raise NativeWindowError(f"{side}: no source_granules recorded")
    return granules
