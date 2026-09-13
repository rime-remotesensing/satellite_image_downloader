"""Dedicated C2 MODIS 10 km halo downloader for the current TRAIN+VAL plan.

This module deliberately does not alter ``run.py``, the default config, or
the shared pipeline.  Its scene plan is intentionally literal: each listed
region/platform/date is passed to the existing MODIS per-platform worker as a
single-day request.  That keeps an accidental continuous-date download from
turning this curated 218-scene run into a much larger job.
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
import shutil
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Sequence, Tuple

from rasterio.crs import CRS
from rasterio.features import bounds as geometry_bounds
from rasterio.transform import array_bounds
from rasterio.warp import transform_geom

LOGGER = logging.getLogger(__name__)

HALO_PIXELS = 18
DEFAULT_HALO_BUFFER_M = 10_000.0
# region01 needs four more native rows to fill its 32 x 32 model input.  This
# one-off override deliberately leaves every other region on the 10 km plan.
REGION_HALO_BUFFER_M = {"region01": 13_000.0}
# These fixed native-grid values mirror src.constants. They are kept here so
# --dry-run stays dependency-free: it must not import optional imagery
# packages, authenticate, or access the network.
MODIS_SINUSOIDAL_PROJ4 = "+proj=sinu +lon_0=0 +x_0=0 +y_0=0 +R=6371007.181 +units=m +no_defs"
MODIS_SINUSOIDAL_X_MIN = -20015109.354
MODIS_SINUSOIDAL_Y_MAX = 10007554.677
MODIS_TILE_SIZE_M = (20015109.354 * 2.0) / 36.0
MODIS_500M_PIXEL_SIZE_M = MODIS_TILE_SIZE_M / 2400
RUN2_START_DATE = "2024-02-08"
RUN2_END_DATE = "2024-04-10"
ACTIVEFIRE_SATELLITES = ("modis",)
ACTIVEFIRE_PRODUCT_MAP = {"modis": ["MODIS_SP"], "viirs": []}

REGION_GEOJSONS: Dict[str, str] = {
    "region01": "config/no1.geojson",
    "region03": "config/no3.geojson",
    "region05": "config/no5.geojson",
    "region06": "config/no6.geojson",
    "region07": "config/no7.geojson",
    "region08": "config/no8.geojson",
    "region09": "config/no9.geojson",
}

# Do not derive this from a range or a statistical filter.  This is the
# reviewed C2 TRAIN+VAL selection, in full, and is the source of truth for
# this one-off runner.
RUN2_DOWNLOAD_PLAN: Dict[str, Dict[str, List[str]]] = {
    "region01": {
        "aqua": [
            "2024-02-12", "2024-02-17", "2024-02-18", "2024-02-27",
            "2024-03-07", "2024-03-08", "2024-03-10", "2024-03-13",
            "2024-03-15", "2024-03-16", "2024-03-18", "2024-03-22",
            "2024-03-27", "2024-03-29", "2024-04-01", "2024-04-10",
        ],
        "terra": [
            "2024-02-08", "2024-02-12", "2024-02-13", "2024-02-17",
            "2024-02-18", "2024-03-07", "2024-03-10", "2024-03-11",
            "2024-03-15", "2024-03-16", "2024-03-27", "2024-03-29",
            "2024-04-01", "2024-04-09", "2024-04-10",
        ],
    },
    "region03": {
        "aqua": [
            "2024-02-12", "2024-02-17", "2024-02-18", "2024-03-10",
            "2024-03-13", "2024-03-15", "2024-03-22", "2024-03-27",
            "2024-03-29", "2024-04-01", "2024-04-10",
        ],
        "terra": [
            "2024-02-13", "2024-02-17", "2024-02-18", "2024-02-20",
            "2024-03-14", "2024-03-15", "2024-03-16", "2024-03-18",
            "2024-03-22", "2024-03-27", "2024-03-29", "2024-04-01",
            "2024-04-10",
        ],
    },
    "region05": {
        "aqua": [
            "2024-02-12", "2024-02-16", "2024-02-17", "2024-02-18",
            "2024-02-24", "2024-02-27", "2024-03-09", "2024-03-10",
            "2024-03-13", "2024-03-14", "2024-03-15", "2024-03-16",
            "2024-03-18", "2024-03-22", "2024-03-26", "2024-03-27",
            "2024-03-29", "2024-04-01", "2024-04-10",
        ],
        "terra": [
            "2024-02-12", "2024-02-13", "2024-02-17", "2024-02-18",
            "2024-02-20", "2024-03-03", "2024-03-07", "2024-03-10",
            "2024-03-14", "2024-03-15", "2024-03-16", "2024-03-18",
            "2024-03-22", "2024-03-27", "2024-03-29", "2024-03-31",
            "2024-04-01", "2024-04-09", "2024-04-10",
        ],
    },
    "region06": {
        "aqua": [
            "2024-02-12", "2024-02-17", "2024-02-18", "2024-02-24",
            "2024-02-27", "2024-03-04", "2024-03-10", "2024-03-13",
            "2024-03-15", "2024-03-16", "2024-03-22", "2024-03-27",
            "2024-03-29", "2024-04-01", "2024-04-10",
        ],
        "terra": [
            "2024-02-13", "2024-02-17", "2024-02-18", "2024-03-10",
            "2024-03-15", "2024-03-16", "2024-03-18", "2024-03-22",
            "2024-03-27", "2024-04-09", "2024-04-10",
        ],
    },
    "region07": {
        "aqua": [
            "2024-02-12", "2024-02-16", "2024-02-17", "2024-02-18",
            "2024-02-27", "2024-03-10", "2024-03-13", "2024-03-15",
            "2024-03-16", "2024-03-18", "2024-03-22", "2024-03-27",
            "2024-03-29", "2024-04-01", "2024-04-10",
        ],
        "terra": [
            "2024-02-13", "2024-02-17", "2024-02-18", "2024-02-20",
            "2024-03-07", "2024-03-10", "2024-03-15", "2024-03-16",
            "2024-03-18", "2024-03-22", "2024-03-27", "2024-03-29",
            "2024-03-31", "2024-04-01", "2024-04-09", "2024-04-10",
        ],
    },
    "region08": {
        "aqua": [
            "2024-02-12", "2024-02-16", "2024-02-17", "2024-02-18",
            "2024-02-27", "2024-03-04", "2024-03-08", "2024-03-09",
            "2024-03-10", "2024-03-13", "2024-03-15", "2024-03-16",
            "2024-03-18", "2024-03-22", "2024-03-27", "2024-03-29",
            "2024-04-01", "2024-04-10",
        ],
        "terra": [
            "2024-02-08", "2024-02-12", "2024-02-13", "2024-02-17",
            "2024-02-18", "2024-02-20", "2024-02-24", "2024-03-07",
            "2024-03-10", "2024-03-15", "2024-03-16", "2024-03-18",
            "2024-03-22", "2024-03-27", "2024-03-29", "2024-04-01",
            "2024-04-09", "2024-04-10",
        ],
    },
    "region09": {
        "aqua": [
            "2024-02-12", "2024-02-16", "2024-02-17", "2024-02-18",
            "2024-02-27", "2024-03-04", "2024-03-09", "2024-03-10",
            "2024-03-13", "2024-03-15", "2024-03-16", "2024-03-18",
            "2024-03-22", "2024-03-27", "2024-03-29", "2024-04-01",
            "2024-04-10",
        ],
        "terra": [
            "2024-02-13", "2024-02-17", "2024-02-18", "2024-02-20",
            "2024-03-07", "2024-03-10", "2024-03-13", "2024-03-15",
            "2024-03-16", "2024-03-18", "2024-03-22", "2024-03-27",
            "2024-03-29", "2024-04-01", "2024-04-10",
        ],
    },
}

PRODUCT_BY_PLATFORM = {"terra": "MOD09GA", "aqua": "MYD09GA"}

# Assigned only for a real run. Keeping them lazy makes dry-run useful even
# on a host that has only the lightweight geospatial dependencies installed.
_process_activefire = None
_process_platform = None


def _load_runtime_workers() -> None:
    """Load the existing download implementation only when a real run starts."""
    global _process_activefire, _process_platform
    if _process_platform is not None:
        return
    from src.activefire import _process_activefire as activefire_worker
    from src.surface_reflectance import _process_platform as platform_worker

    _process_activefire = activefire_worker
    _process_platform = platform_worker


def _load_geojson_geometry(path: Path) -> Dict[str, Any]:
    with path.open("r", encoding="utf-8") as stream:
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


def _bbox_from_geometry(geometry: Dict[str, Any]) -> Tuple[float, float, float, float]:
    bounds = geometry_bounds(geometry)
    return float(bounds[0]), float(bounds[1]), float(bounds[2]), float(bounds[3])


def _parse_plan_date(value: str) -> date:
    return datetime.strptime(value, "%Y-%m-%d").date()


def halo_buffer_m_for_region(region: str) -> float:
    if region not in REGION_GEOJSONS:
        raise ValueError(f"Unknown run2 region: {region}")
    return REGION_HALO_BUFFER_M.get(region, DEFAULT_HALO_BUFFER_M)


def validate_download_plan(plan: Mapping[str, Mapping[str, Sequence[str]]] = RUN2_DOWNLOAD_PLAN) -> Dict[str, int]:
    """Validate the literal plan, returning the expected platform totals."""
    if set(plan) != set(REGION_GEOJSONS):
        raise ValueError("RUN2_DOWNLOAD_PLAN regions must exactly match REGION_GEOJSONS")

    totals = {"aqua": 0, "terra": 0}
    for region, platforms in plan.items():
        if set(platforms) != set(totals):
            raise ValueError(f"{region}: plan must contain exactly aqua and terra")
        for platform, values in platforms.items():
            parsed = [_parse_plan_date(value) for value in values]
            if len(parsed) != len(set(parsed)):
                raise ValueError(f"{region}/{platform}: duplicate date in literal plan")
            totals[platform] += len(parsed)

    if totals != {"aqua": 111, "terra": 107}:
        raise AssertionError(f"Unexpected RUN2 scene totals: {totals}")
    if sum(totals.values()) != 218:
        raise AssertionError("RUN2 plan must contain exactly 218 region/platform/date scenes")
    return totals


def _native_grid_extent(bounds: Tuple[float, float, float, float]) -> Tuple[float, float, float, float]:
    """Snap a native MODIS extent outward to whole 500 m grid cells."""
    left, bottom, right, top = bounds
    pixel = MODIS_500M_PIXEL_SIZE_M
    snapped_left = MODIS_SINUSOIDAL_X_MIN + math.floor((left - MODIS_SINUSOIDAL_X_MIN) / pixel) * pixel
    snapped_right = MODIS_SINUSOIDAL_X_MIN + math.ceil((right - MODIS_SINUSOIDAL_X_MIN) / pixel) * pixel
    snapped_top = MODIS_SINUSOIDAL_Y_MAX - math.floor((MODIS_SINUSOIDAL_Y_MAX - top) / pixel) * pixel
    snapped_bottom = MODIS_SINUSOIDAL_Y_MAX - math.ceil((MODIS_SINUSOIDAL_Y_MAX - bottom) / pixel) * pixel
    return snapped_left, snapped_bottom, snapped_right, snapped_top


def _rectangle(bounds: Tuple[float, float, float, float]) -> Dict[str, Any]:
    left, bottom, right, top = bounds
    return {
        "type": "Polygon",
        "coordinates": [[[left, bottom], [right, bottom], [right, top], [left, top], [left, bottom]]],
    }


def _expand_wgs84_bbox_by_meters(
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


def build_halo_context(
    geometry_wgs84: Dict[str, Any], buffer_m: float = DEFAULT_HALO_BUFFER_M
) -> Dict[str, Any]:
    """Build a metric WGS84 halo, snapped outward to native MODIS pixels."""
    if buffer_m < DEFAULT_HALO_BUFFER_M:
        raise ValueError(f"halo buffer must be at least {DEFAULT_HALO_BUFFER_M:.0f} m")
    native_crs = CRS.from_proj4(MODIS_SINUSOIDAL_PROJ4)
    west, south, east, north = _bbox_from_geometry(geometry_wgs84)
    original_bbox_wgs84 = (west, south, east, north)
    expanded_bbox_wgs84 = _expand_wgs84_bbox_by_meters(original_bbox_wgs84, buffer_m)
    # This matches src.surface_reflectance._aoi_bbox_geometry_in_crs. It is
    # repeated solely for dry-run, before the worker is lazily imported.
    segments = 64
    ring: List[List[float]] = []
    expanded_west, expanded_south, expanded_east, expanded_north = expanded_bbox_wgs84
    for index in range(segments + 1):
        ring.append([expanded_west + (expanded_east - expanded_west) * index / segments, expanded_south])
    for index in range(1, segments + 1):
        ring.append([expanded_east, expanded_south + (expanded_north - expanded_south) * index / segments])
    for index in range(1, segments + 1):
        ring.append([expanded_east - (expanded_east - expanded_west) * index / segments, expanded_north])
    for index in range(1, segments + 1):
        ring.append([expanded_west, expanded_north - (expanded_north - expanded_south) * index / segments])
    expanded_native_geom = transform_geom(
        "EPSG:4326", native_crs, {"type": "Polygon", "coordinates": [ring]}, precision=6
    )
    halo_native_bounds = _native_grid_extent(tuple(geometry_bounds(expanded_native_geom)))

    original_native_geom = transform_geom(
        "EPSG:4326", native_crs, _rectangle(original_bbox_wgs84), precision=6
    )
    original_native_bounds = _native_grid_extent(tuple(geometry_bounds(original_native_geom)))
    original_left, original_bottom, original_right, original_top = original_native_bounds
    halo_left, halo_bottom, halo_right, halo_top = halo_native_bounds
    minimum_native_halo_pixels = min(
        (original_left - halo_left) / MODIS_500M_PIXEL_SIZE_M,
        (original_bottom - halo_bottom) / MODIS_500M_PIXEL_SIZE_M,
        (halo_right - original_right) / MODIS_500M_PIXEL_SIZE_M,
        (halo_top - original_top) / MODIS_500M_PIXEL_SIZE_M,
    )
    if minimum_native_halo_pixels < HALO_PIXELS:
        raise AssertionError(
            f"{buffer_m / 1000:g} km halo yielded only {minimum_native_halo_pixels:.3f} native pixels; "
            f"need at least {HALO_PIXELS}"
        )

    # The shared worker accepts WGS84 geometry and performs the final native
    # grid crop/snap itself.  Do not inverse-transform the native rectangle
    # and then take a WGS84 bbox: a sinusoidal rectangle has curved longitude
    # edges, which would turn the intended 10 km context into a much wider box.
    halo_wgs84_geometry = _rectangle(expanded_bbox_wgs84)
    return {
        "original_bbox_wgs84": original_bbox_wgs84,
        "expanded_bbox_wgs84": expanded_bbox_wgs84,
        "original_native_bounds": original_native_bounds,
        "halo_native_bounds": halo_native_bounds,
        "halo_wgs84_geometry": halo_wgs84_geometry,
        "halo_bbox_wgs84": expanded_bbox_wgs84,
        "native_pixel_size_m": MODIS_500M_PIXEL_SIZE_M,
        "halo_pixels": HALO_PIXELS,
        "minimum_native_halo_pixels": minimum_native_halo_pixels,
        "buffer_m": buffer_m,
    }


def default_output_root(region: str | None = None) -> Path:
    host_data = Path(os.environ.get("SATDL_HOST_DATA_PATH", "/host_data"))
    if region == "region01":
        return host_data / "aoi_rectangle" / "output_halo13km_region01"
    return host_data / "aoi_rectangle" / "output_halo10km"


def _runtime_config(region_output: Path) -> Dict[str, Any]:
    return {
        "file_exists": "skip",
        "surface_reflectance": {
            "clip_to_aoi": True,
            "products": {
                "modis": {"terra": "MOD09GA", "aqua": "MYD09GA", "version": "061"},
            },
        },
        # FIRMS gets exactly the halo bbox passed below; do not add the
        # default 5 km buffer.  Its source representation remains unchanged.
        "firms": {"bbox_buffer_m": 0, "clip_to_aoi": False},
        "output": str(region_output),
    }


def _metadata_payload(region: str, geojson_path: Path, halo: Mapping[str, Any], output: Path) -> Dict[str, Any]:
    return {
        "runner": "run2.py",
        "region": region,
        "geojson": str(geojson_path),
        "output": str(output),
        "products": {"terra": "MOD09GA.061", "aqua": "MYD09GA.061"},
        "qa_layers": ["500m Surface Reflectance", "QC_500m", "state_1km"],
        "original_bbox_wgs84": halo["original_bbox_wgs84"],
        "expanded_bbox_wgs84": halo["expanded_bbox_wgs84"],
        "original_native_grid_bounds_m": halo["original_native_bounds"],
        "halo_native_grid_bounds_m": halo["halo_native_bounds"],
        "halo_bbox_wgs84": halo["halo_bbox_wgs84"],
        "native_pixel_size_m": halo["native_pixel_size_m"],
        "halo_pixels_per_side": halo["halo_pixels"],
        "minimum_native_halo_pixels_per_side": halo["minimum_native_halo_pixels"],
        "halo_buffer_m_per_side": halo["buffer_m"],
        "halo_guarantee": (
            f"{halo['buffer_m'] / 1000:g} km WGS84 buffer, snapped outward to "
            f">= {HALO_PIXELS} native MODIS 500 m pixels per side"
        ),
        "activefire": {
            "level": "SP",
            "product": "MODIS_SP",
            "extent": f"{halo['buffer_m'] / 1000:g} km halo",
            "satellites": list(ACTIVEFIRE_SATELLITES),
        },
        "activefire_date_range": [RUN2_START_DATE, RUN2_END_DATE],
        "activefire_event_interval": f"{RUN2_START_DATE}T00:00:00Z/{RUN2_END_DATE}T23:59:59Z",
    }


def validate_halo_raster(
    raster_path: Path,
    original_native_bounds: Tuple[float, float, float, float],
    minimum_pixels: int = HALO_PIXELS,
) -> Dict[str, float]:
    """Assert that a downloaded native GeoTIFF contains real halo pixels.

    This checks the GeoTIFF's actual extent; it neither pads data nor accepts
    a nominal buffer declaration as evidence of context.
    """
    import rasterio

    with rasterio.open(raster_path) as dataset:
        if dataset.crs != CRS.from_proj4(MODIS_SINUSOIDAL_PROJ4):
            raise ValueError(f"{raster_path}: expected the native MODIS Sinusoidal CRS")
        left, bottom, right, top = array_bounds(dataset.height, dataset.width, dataset.transform)

    original_left, original_bottom, original_right, original_top = original_native_bounds
    pixel = MODIS_500M_PIXEL_SIZE_M
    context = {
        "left": (original_left - left) / pixel,
        "bottom": (original_bottom - bottom) / pixel,
        "right": (right - original_right) / pixel,
        "top": (top - original_top) / pixel,
    }
    if min(context.values()) < minimum_pixels - 1e-6:
        raise AssertionError(
            f"{raster_path}: insufficient native context {context}; "
            f"need >= {minimum_pixels} pixels on every side"
        )
    return context


def _validate_platform_result(
    result: Mapping[str, Any], original_native_bounds: Tuple[float, float, float, float]
) -> List[Dict[str, float]]:
    validations: List[Dict[str, float]] = []
    for processed in result.get("dates_processed", []):
        for output_name in processed.get("files", []):
            output_path = Path(output_name)
            if output_path.suffix.lower() == ".tif":
                validations.append(validate_halo_raster(output_path, original_native_bounds))
    return validations


def _publish_activefire_contract(output_root: Path, region: str) -> List[str]:
    """Expose MODIS_SP shapefile sets at the downstream C2 source contract."""
    source_dir = output_root / region / "modis" / "activefire"
    contract_dir = output_root / "MODIS" / region / "2024" / "activefire"
    if not source_dir.exists():
        return []

    published: List[str] = []
    for source_shp in source_dir.glob("ACFR_*.shp"):
        for source_file in source_dir.glob(f"{source_shp.stem}.*"):
            target = contract_dir / source_file.name
            if target.exists():
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source_file, target)
            published.append(str(target))
    return published


def _print_dry_run(regions: Iterable[str]) -> None:
    totals = validate_download_plan()
    print("RUN2 dedicated MODIS C2 region-specific halo download plan")
    print(f"Full literal plan: Aqua={totals['aqua']}, Terra={totals['terra']}, total={sum(totals.values())}")
    print(f"Products: Aqua=MYD09GA.061, Terra=MOD09GA.061; QA=QC_500m,state_1km")
    print(f"Active Fire: FIRMS SP MODIS_SP only, {RUN2_START_DATE}..{RUN2_END_DATE}")

    for region in regions:
        geojson_path = (Path.cwd() / REGION_GEOJSONS[region]).resolve()
        geometry = _load_geojson_geometry(geojson_path)
        buffer_m = halo_buffer_m_for_region(region)
        halo = build_halo_context(geometry, buffer_m)
        region_output_root = default_output_root(region)
        print(f"\n{region}: {geojson_path}")
        print(f"  halo buffer: {buffer_m:.0f} m")
        print(f"  original AOI bbox: {halo['original_bbox_wgs84']}")
        print(f"  expanded {buffer_m / 1000:g} km bbox (pre-snap): {halo['expanded_bbox_wgs84']}")
        print(f"  SR/FIRMS WGS84 bbox: {halo['halo_bbox_wgs84']}")
        print(f"  native bounds: {halo['original_native_bounds']} -> {halo['halo_native_bounds']}")
        print(f"  output: {region_output_root / region}")
        print(f"  activefire contract: {region_output_root / 'MODIS' / region / '2024' / 'activefire'}")
        local_aqua = len(RUN2_DOWNLOAD_PLAN[region]["aqua"])
        local_terra = len(RUN2_DOWNLOAD_PLAN[region]["terra"])
        print(f"  scenes: Aqua={local_aqua}, Terra={local_terra}, Total={local_aqua + local_terra}")
        for platform in ("aqua", "terra"):
            print(f"  {platform} ({PRODUCT_BY_PLATFORM[platform]}): {', '.join(RUN2_DOWNLOAD_PLAN[region][platform])}")


def run(regions: Sequence[str]) -> Dict[str, Any]:
    """Run the literal MODIS plan, followed by one halo-extent FIRMS SP call per region."""
    validate_download_plan()
    _load_runtime_workers()
    all_results: Dict[str, Any] = {}
    for region in regions:
        geojson_path = (Path.cwd() / REGION_GEOJSONS[region]).resolve()
        geometry = _load_geojson_geometry(geojson_path)
        region_output_root = default_output_root(region)
        halo = build_halo_context(geometry, halo_buffer_m_for_region(region))
        region_output = region_output_root / region
        region_output.mkdir(parents=True, exist_ok=True)
        (region_output / "run2_halo_metadata.json").write_text(
            json.dumps(_metadata_payload(region, geojson_path, halo, region_output), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        config = _runtime_config(region_output)
        platform_results: Dict[str, List[Dict[str, Any]]] = {"aqua": [], "terra": []}
        for platform in ("aqua", "terra"):
            short_name = PRODUCT_BY_PLATFORM[platform]
            for date_text in RUN2_DOWNLOAD_PLAN[region][platform]:
                scene_date = _parse_plan_date(date_text)
                LOGGER.info("%s %s %s (%s)", region, platform, date_text, short_name)
                platform_results[platform].append(
                    _process_platform(
                        config=config,
                        config_dir=Path.cwd(),
                        output_root=region_output,
                        geometry_wgs84=halo["halo_wgs84_geometry"],
                        bbox=halo["halo_bbox_wgs84"],
                        start_date=scene_date,
                        end_date=scene_date,
                        satellite_key="modis",
                        platform_key=platform,
                        short_name=short_name,
                        version="061",
                        out_satellite_dir_name="modis",
                    )
                )
                platform_results[platform][-1]["halo_validation"] = _validate_platform_result(
                    platform_results[platform][-1], halo["original_native_bounds"]
                )

        activefire = _process_activefire(
            config=config,
            config_dir=Path.cwd(),
            output_root=region_output,
            bbox=halo["halo_bbox_wgs84"],
            geometry_wgs84=halo["halo_wgs84_geometry"],
            # Preserve activefire.py's normal WGS84 representation; the
            # dedicated runner only narrows source/platform and extent.
            reference_crs=None,
            reference_raster_path=None,
            start_date=_parse_plan_date(RUN2_START_DATE),
            end_date=_parse_plan_date(RUN2_END_DATE),
            satellites=list(ACTIVEFIRE_SATELLITES),
            product_map_override=ACTIVEFIRE_PRODUCT_MAP,
        )
        all_results[region] = {
            "scenes": platform_results,
            "activefire": activefire,
            "activefire_contract_files": _publish_activefire_contract(region_output_root, region),
        }
    return all_results


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="Print the plan without login, network, download, or writes.")
    parser.add_argument("--regions", nargs="+", choices=tuple(REGION_GEOJSONS), help="Optional region subset.")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
    regions = args.regions or list(REGION_GEOJSONS)
    if args.dry_run:
        _print_dry_run(regions)
        return 0

    try:
        print(json.dumps(run(regions), ensure_ascii=False, indent=2, default=str))
    except Exception as exc:
        LOGGER.error("run2 failed: %s", exc, exc_info=True)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
