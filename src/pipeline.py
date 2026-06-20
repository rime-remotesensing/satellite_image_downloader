from __future__ import annotations

import logging
from datetime import date
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from .activefire import _process_activefire
from .config import (
    _as_bool,
    _load_config,
    _normalize_activefire_satellites,
    _normalize_date_list,
    _normalize_satellites,
    _resolve_runtime_path,
)
from .constants import SENTINEL_COLLECTION
from .geometry import _bbox_from_geometry, _load_aoi_geometry
from .imagery import _infer_item_output_crs, _process_satellite_imagery, _search_stac_items

LOGGER = logging.getLogger(__name__)


def run_pipeline(
    config: Dict[str, Any],
    config_dir: Path,
    skip_satellite_subdir: bool = False,
) -> Dict[str, Any]:
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
    img_only = _as_bool(config.get("img_only"), default=False)

    activefire_targets: List[str]
    if "activefire_satellite" in firms_cfg:
        activefire_targets = _normalize_activefire_satellites(
            firms_cfg.get("activefire_satellite")
        )
    else:
        activefire_targets = []

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
                "img_only": img_only,
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
                skip_satellite_subdir=skip_satellite_subdir,
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
                skip_satellite_subdir=skip_satellite_subdir,
            )

        if not img_only and include_activefire and activefire_targets:
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

    if not img_only and activefire_targets:
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
            "img_only": img_only,
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
    skip_satellite_subdir: bool = False,
    *,
    batch_mode: bool = False,
    img_only: bool = False,
) -> Dict[str, Any]:
    runtime_config: Dict[str, Any] = {
        "satellite": list(satellite_type) if not isinstance(satellite_type, str) else satellite_type,
        "geojson": geojson_path,
        "startday": sdate,
        "endday": edate,
        "output": output_path,
    }

    config_dir = Path.cwd()
    if config_path:
        base_config_path = Path(config_path).resolve()
        base_config = _load_config(base_config_path)
        base_config.update(runtime_config)
        runtime_config = base_config
        config_dir = base_config_path.parent

    if batch_mode:
        runtime_config["max_cloud_cover"] = None
    if img_only:
        runtime_config["img_only"] = True
        runtime_config["file_exists"] = "overwrite"

    return run_pipeline(config=runtime_config, config_dir=config_dir, skip_satellite_subdir=skip_satellite_subdir)
