from __future__ import annotations

import argparse
import json
import logging
import sys
import os
from pathlib import Path

from src.config import _load_config
from src.pipeline import run_pipeline, run_pipeline_from_config

# Region to GeoJSON file mapping
BATCH_MODE_REGIONS = [
    ("region01", "config/no1.geojson"),
    ("region02", "config/no2.geojson"),
    ("region03", "config/no3.geojson"),
    ("region04", "config/no4.geojson"),
    ("region05", "config/no5.geojson"),
    ("region06", "config/no6.geojson"),
    ("region07", "config/no7.geojson"),
    ("region08", "config/no8.geojson"),
    ("region09", "config/no9.geojson"),
    ("region10", "config/no10.geojson"),
]

# Current batch download plan: MODIS/VIIRS daily Surface Reflectance +
# FIRMS Active Fire for all regions, over one continuous date range.
BATCH_STARTDAY = "20240201"
BATCH_ENDDAY = "20240410"
BATCH_SATELLITES = ["modis", "viirs"]
# SP = Standard Processing (confirmed/science-quality). This range is well in
# the past, so SP is used rather than NRT (NRT only covers recent data).
BATCH_ACTIVEFIRE = "SP"

BASE_PATH = Path(
    os.environ.get(
        "SATDL_BASE_PATH",
        os.environ.get("SATDL_HOST_DATA_PATH", "/host_data") + "/aoi_rectangle/output",
    )
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Satellite image downloader pipeline (config-driven or batch mode)."
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help="Path to YAML config file. If not specified, runs batch mode for all regions.",
    )
    parser.add_argument(
        "--batch",
        action="store_true",
        help=(
            "Force batch mode: download MODIS/VIIRS Surface Reflectance + FIRMS "
            "Active Fire for all BATCH_MODE_REGIONS over BATCH_STARTDAY..BATCH_ENDDAY."
        ),
    )
    parser.add_argument(
        "--img-only",
        action="store_true",
        help="Regenerate only img/ scene stacks and metadata; skip cloudmask/masked/snowmasked outputs.",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )
    logger = logging.getLogger(__name__)

    # Batch mode: MODIS/VIIRS Surface Reflectance + FIRMS Active Fire for
    # every region in BATCH_MODE_REGIONS, over BATCH_STARTDAY..BATCH_ENDDAY.
    if args.batch or (args.config is None):
        logger.info(
            "Running in batch mode: satellite=%s activefire=%s period=%s-%s",
            BATCH_SATELLITES, BATCH_ACTIVEFIRE, BATCH_STARTDAY, BATCH_ENDDAY,
        )

        all_results = {}
        for region_name, geojson_path in BATCH_MODE_REGIONS:
            logger.info(f"\nProcessing {region_name} ({geojson_path})...")

            region_output_path = str(BASE_PATH / region_name)
            config = {
                "geojson": geojson_path,
                "startday": BATCH_STARTDAY,
                "endday": BATCH_ENDDAY,
                "satellite": BATCH_SATELLITES,
                "activefire": BATCH_ACTIVEFIRE,
                "output": region_output_path,
            }

            try:
                result = run_pipeline(config=config, config_dir=Path.cwd())

                failed_dates = 0
                for key in ("modis_surface_reflectance", "viirs_surface_reflectance"):
                    for platform_summary in (result.get(key) or {}).values():
                        failed_dates += len(platform_summary.get("dates_failed", []))

                all_results[region_name] = {
                    "status": "partial" if failed_dates else "success",
                    "failed_dates": failed_dates,
                    "activefire": result.get("activefire"),
                }
                if failed_dates:
                    logger.warning(
                        "  %s completed with %s failed date(s)", region_name, failed_dates
                    )
                logger.info(f"  {region_name} completed")

            except Exception as exc:
                logger.error(f"  {region_name} failed: {exc}", exc_info=True)
                all_results[region_name] = {
                    "status": "failed",
                    "error": str(exc),
                }

        logger.info("\n" + "="*60)
        logger.info("Batch download completed. Summary:")
        print(json.dumps(all_results, ensure_ascii=False, indent=2))
        return 0

    # Config mode: single config file
    if args.config is None:
        parser.print_help()
        return 1

    try:
        if args.img_only:
            config_path = args.config.resolve()
            config = _load_config(config_path)
            config["img_only"] = True
            config["file_exists"] = "overwrite"
            summary = run_pipeline(config=config, config_dir=config_path.parent)
        else:
            summary = run_pipeline_from_config(args.config)
    except Exception as exc:
        logger.error("Pipeline failed: %s", exc, exc_info=True)
        return 1

    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
