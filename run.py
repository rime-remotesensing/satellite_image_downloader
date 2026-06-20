from __future__ import annotations

import argparse
import json
import logging
import sys
import os
from pathlib import Path

from src.pipeline import _load_config, run_pipeline, run_pipeline_from_config, satellite_image_downloader

# Region to config file mapping with hardcoded dates
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

# Hardcoded download dates extracted from existing snowmasked_terrain files
# Format: region_name -> year -> list of dates (YYYYMMDD)
REGION_DOWNLOAD_DATES = {
    "region01": {
        "2024": [
            "20240116", "20240126", "20240210", "20240220",
            "20240311", "20240316", "20240331", "20240410",
        ]
    },
    "region02": {
        "2024": [
            "20240116", "20240126", "20240220",
            "20240311", "20240316", "20240410",
        ]
    },
    "region03": {
        "2024": [
            "20240116", "20240126", "20240220",
            "20240311", "20240316", "20240410",
        ]
    },
    "region04": {
        "2024": [
            "20240116", "20240126", "20240220",
            "20240311", "20240316", "20240410",
        ]
    },
    "region05": {
        "2023": ["20230121", "20230307"],
        "2024": [
            "20240116", "20240126", "20240220",
            "20240316", "20240410",
        ],
        "2026": [
            "20260301", "20260311", "20260316",
            "20260321", "20260425",
        ],
    },
    "region06": {
        "2024": [
            "20240116", "20240126", "20240210", "20240220",
            "20240311", "20240316", "20240410",
        ]
    },
    "region07": {
        "2024": [
            "20240116", "20240126", "20240210", "20240220",
            "20240311", "20240316", "20240410",
        ]
    },
    "region08": {
        "2024": [
            "20240116", "20240126", "20240210", "20240301",
            "20240311", "20240316", "20240326", "20240410",
        ]
    },
    "region09": {
        "2024": [
            "20240116", "20240126", "20240210",
            "20240311", "20240316", "20240410",
        ]
    },
    "region10": {
        "2024": [
            "20240116", "20240126", "20240220",
            "20240311", "20240316", "20240410",
        ]
    },
}

BASE_PATH = Path(os.environ.get("SATDL_HOST_DATA_PATH", "D:/sugimoto/Aso/Sentinel-2"))
BASE_PATH = Path(
    os.environ.get(
        "SATDL_BASE_PATH",
        os.environ.get("SATDL_HOST_DATA_PATH", "/host_data") + "/Aso/Sentinel-2",
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
        help="Force batch mode (download dates for all regions to their respective directories).",
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

    # Batch mode: use hardcoded dates for all regions
    if args.batch or (args.config is None):
        logger.info("Running in batch mode with hardcoded download dates...")
        
        all_results = {}
        for region_name, geojson_path in BATCH_MODE_REGIONS:
            region_dates = REGION_DOWNLOAD_DATES.get(region_name, {})
            
            if not region_dates:
                logger.warning(f"No download dates configured for {region_name}")
                continue

            logger.info(f"\nProcessing {region_name}:")
            
            # Process each year in the region
            for year in sorted(region_dates.keys()):
                sdates = region_dates[year]
                
                if not sdates:
                    logger.warning(f"  {year}: No dates found")
                    continue

                logger.info(f"  {year}: {len(sdates)} dates (from {sdates[0]} to {sdates[-1]})")
                
                try:
                    # Convert to list for satellite_image_downloader
                    sdate_list = [int(d) for d in sdates]
                    edate_list = [int(d) for d in sdates]
                    
                    logger.info(f"  Downloading {len(sdate_list)} dates for {region_name}/{year}...")
                    
                    # Output to region/year directory
                    region_output_path = str(BASE_PATH / region_name / year)
                    
                    result = satellite_image_downloader(
                        satellite_type=["sentinel2"],
                        geojson_path=geojson_path,
                        sdate=sdate_list,
                        edate=edate_list,
                        output_path=region_output_path,
                        config_path="config/config.yaml",
                        batch_mode=True,
                        img_only=args.img_only,
                        skip_satellite_subdir=True,
                    )
                    
                    result_key = f"{region_name}/{year}"
                    failed_items = int(result.get("failed_items", 0))
                    all_results[result_key] = {
                        "status": "partial" if failed_items else "success",
                        "dates_processed": len(sdate_list),
                        "total_runs": result.get("total_runs", 0),
                        "failed_items": failed_items,
                    }
                    if failed_items:
                        logger.warning(
                            "  %s/%s completed with %s failed scene(s)",
                            region_name,
                            year,
                            failed_items,
                        )
                    logger.info(f"  ✁E{region_name}/{year} completed")
                    
                except Exception as exc:
                    logger.error(f"  ✁E{region_name}/{year} failed: {exc}", exc_info=True)
                    all_results[f"{region_name}/{year}"] = {
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
