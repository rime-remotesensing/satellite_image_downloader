"""Selective VIIRS Surface Reflectance downloader driven by the authoritative
required-date plan (config/viirs_required_dates_daily.csv).

This module deliberately does not alter run.py, run2.py, the default
config, or the shared pipeline. Its input is a fixed CSV of
(region, date) rows produced by an external production daily-pair audit;
run3.py never infers, expands, or substitutes dates on its own. Each
required (region, date) row is processed as an exact single-day request per
platform -- there is no min(date)..max(date) range download.
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path
from typing import Any, Dict, List, Mapping, Sequence, Tuple

# Reuse run2.py's already-verified MODIS/VIIRS sinusoidal halo geometry
# unchanged (VIIRS VNP09GA/VJ109GA/VJ209GA share the exact same sinusoidal
# grid and 500 m native pixel size as MODIS -- see src/constants.py). This
# module must not modify run2.py.
import run2

LOGGER = logging.getLogger(__name__)

REQUIRED_DATES_CSV = Path("config/viirs_required_dates_daily.csv")

REGION_GEOJSONS: Dict[str, str] = {
    "region01": "config/no1.geojson",
    "region03": "config/no3.geojson",
    "region04": "config/no4.geojson",
    "region05": "config/no5.geojson",
    "region06": "config/no6.geojson",
    "region07": "config/no7.geojson",
    "region08": "config/no8.geojson",
    "region09": "config/no9.geojson",
}
FORBIDDEN_REGIONS = ("region02", "region10")

PLATFORMS: Tuple[str, ...] = ("snpp", "noaa20", "noaa21")
PRODUCT_BY_PLATFORM: Dict[str, str] = {
    "snpp": "VNP09GA",
    "noaa20": "VJ109GA",
    "noaa21": "VJ209GA",
}
PRODUCT_VERSION = "002"

# Same native-pixel halo requirement as the MODIS run2.py plan, applied to
# VIIRS's identical sinusoidal 500 m grid (per user decision: reuse the
# MODIS value rather than inventing a new one).
HALO_PIXELS = run2.HALO_PIXELS
DEFAULT_HALO_BUFFER_M = run2.DEFAULT_HALO_BUFFER_M

EXPECTED_REQUIRED_REGION_DATE_TASKS = 141
EXPECTED_PLATFORM_REGION_DATE_TASKS = EXPECTED_REQUIRED_REGION_DATE_TASKS * len(PLATFORMS)

# Assigned lazily so --dry-run and CSV validation stay dependency-free.
_process_platform = None


def _load_runtime_worker() -> None:
    global _process_platform
    if _process_platform is not None:
        return
    from src.surface_reflectance import _process_platform as platform_worker

    _process_platform = platform_worker


def load_required_tasks(csv_path: Path = REQUIRED_DATES_CSV) -> Dict[str, List[str]]:
    """Read config/viirs_required_dates_daily.csv: the sole date authority.

    Returns {region: [date, ...]} (dates as "YYYY-MM-DD" strings, sorted).
    Raises if the file is missing, malformed, contains a forbidden region,
    or contains a duplicate (region, date) pair. Never infers dates from
    anything else.
    """
    if not csv_path.is_file():
        raise FileNotFoundError(
            f"Authoritative VIIRS required-date plan not found: {csv_path}. "
            "This file must be copied in from the daily-pair audit before run3.py can run."
        )

    with csv_path.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames != ["region", "date"]:
            raise ValueError(
                f"{csv_path}: expected header 'region,date', got {reader.fieldnames}"
            )
        rows = list(reader)

    seen: set[Tuple[str, str]] = set()
    tasks: Dict[str, List[str]] = defaultdict(list)
    for row in rows:
        region = row["region"].strip()
        date_text = row["date"].strip()
        run2._parse_plan_date(date_text)  # raises ValueError if malformed

        if region in FORBIDDEN_REGIONS:
            raise ValueError(
                f"{csv_path}: {region} is explicitly out of scope for this run "
                "(no 2024 burn events) but appears in the required-date plan"
            )
        if region not in REGION_GEOJSONS:
            raise ValueError(f"{csv_path}: unknown region '{region}' has no configured GeoJSON")

        key = (region, date_text)
        if key in seen:
            raise ValueError(f"{csv_path}: duplicate (region, date) row: {key}")
        seen.add(key)
        tasks[region].append(date_text)

    for region in tasks:
        tasks[region].sort()

    return dict(tasks)


def validate_required_tasks(tasks: Mapping[str, Sequence[str]]) -> Dict[str, int]:
    """Assert the required-date plan matches the audited totals exactly."""
    if set(tasks) != set(REGION_GEOJSONS):
        raise AssertionError(
            f"Required-date plan regions {sorted(tasks)} do not match the expected "
            f"8-region set {sorted(REGION_GEOJSONS)}"
        )

    total = sum(len(dates) for dates in tasks.values())
    if total != EXPECTED_REQUIRED_REGION_DATE_TASKS:
        raise AssertionError(
            f"Expected {EXPECTED_REQUIRED_REGION_DATE_TASKS} region-date tasks, got {total}"
        )

    platform_total = total * len(PLATFORMS)
    if platform_total != EXPECTED_PLATFORM_REGION_DATE_TASKS:
        raise AssertionError(
            f"Expected {EXPECTED_PLATFORM_REGION_DATE_TASKS} platform-region-date tasks, "
            f"got {platform_total}"
        )

    return {"region_date_tasks": total, "platform_region_date_tasks": platform_total}


def default_output_root() -> Path:
    import os

    host_data = Path(os.environ.get("SATDL_HOST_DATA_PATH", "/host_data"))
    return host_data / "aoi_rectangle" / "output_viirs_selective"


def _runtime_config() -> Dict[str, Any]:
    return {
        "file_exists": "skip",
        "surface_reflectance": {
            "clip_to_aoi": True,
            "products": {"viirs": {"version": PRODUCT_VERSION}},
        },
    }


def _region_halo(region: str) -> Dict[str, Any]:
    geojson_path = (Path.cwd() / REGION_GEOJSONS[region]).resolve()
    geometry = run2._load_geojson_geometry(geojson_path)
    return run2.build_halo_context(geometry, DEFAULT_HALO_BUFFER_M)


def _print_pre_download_report(tasks: Dict[str, List[str]]) -> None:
    totals = validate_required_tasks(tasks)
    print("REQUIRED_DATE_FILE:", REQUIRED_DATES_CSV)
    print("REQUIRED_REGION_DATE_TASKS =", totals["region_date_tasks"])
    print("PLATFORMS =", ",".join(PLATFORMS))
    print("EXPECTED_PLATFORM_REGION_DATE_TASKS =", totals["platform_region_date_tasks"])
    print()
    for region in sorted(tasks):
        dates = tasks[region]
        print(f"{region} ({len(dates)}): {', '.join(dates)}")


def _print_dry_run(tasks: Dict[str, List[str]], regions: Sequence[str]) -> None:
    _print_pre_download_report(tasks)
    print()
    print("Halo contract (per region, native VIIRS 500m sinusoidal grid):")
    for region in regions:
        halo = _region_halo(region)
        print(f"\n{region}: {REGION_GEOJSONS[region]}")
        print(f"  halo buffer: {DEFAULT_HALO_BUFFER_M:.0f} m, halo_pixels >= {HALO_PIXELS}")
        print(f"  original AOI bbox (WGS84): {halo['original_bbox_wgs84']}")
        print(f"  buffered source bbox (WGS84, sent to CMR): {halo['halo_bbox_wgs84']}")
        print(f"  native bounds: {halo['original_native_bounds']} -> {halo['halo_native_bounds']}")
        print(f"  minimum native halo pixels achieved: {halo['minimum_native_halo_pixels']:.3f}")
        print(f"  dates ({len(tasks[region])}): {', '.join(tasks[region])}")
        print(f"  platforms: {', '.join(PLATFORMS)}")
        print(f"  output: {default_output_root() / region / 'viirs' / 'surface_reflectance'}")


def _extra_tags(halo: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "aoi_bounds_wgs84": list(halo["original_bbox_wgs84"]),
        "buffered_source_bounds_wgs84": list(halo["halo_bbox_wgs84"]),
        "halo_pixels": halo["halo_pixels"],
        "halo_m": halo["buffer_m"],
    }


def _run_one_task(
    *, region: str, date_text: str, platform: str, halo: Dict[str, Any], region_output: Path
) -> Dict[str, Any]:
    _load_runtime_worker()
    scene_date = run2._parse_plan_date(date_text)
    short_name = PRODUCT_BY_PLATFORM[platform]
    config = _runtime_config()

    result = _process_platform(
        config=config,
        config_dir=Path.cwd(),
        output_root=region_output,
        geometry_wgs84=halo["halo_wgs84_geometry"],
        bbox=halo["halo_bbox_wgs84"],
        start_date=scene_date,
        end_date=scene_date,
        satellite_key="viirs",
        platform_key=platform,
        short_name=short_name,
        version=PRODUCT_VERSION,
        out_satellite_dir_name="viirs",
        extra_tags=_extra_tags(halo),
    )

    date_token = scene_date.strftime("%Y%m%d")
    if result.get("error"):
        return {
            "region": region,
            "date": date_text,
            "platform": platform,
            "product": short_name,
            "status": "failed",
            "reason": result["error"],
            "category": "network_or_auth_error",
        }
    for failed in result.get("dates_failed", []):
        if failed["date"] == date_token:
            return {
                "region": region,
                "date": date_text,
                "platform": platform,
                "product": short_name,
                "status": "failed",
                "reason": failed["error"],
                "category": "processing_error",
            }
    for processed in result.get("dates_processed", []):
        if processed["date"] == date_token:
            return {
                "region": region,
                "date": date_text,
                "platform": platform,
                "product": short_name,
                "status": "success",
                "files": processed["files"],
            }
    if date_token in result.get("dates_skipped", []):
        return {
            "region": region,
            "date": date_text,
            "platform": platform,
            "product": short_name,
            "status": "missing_product",
            "reason": "No CMR granules found for this exact date/platform; no nearest-date substitution performed",
            "category": "missing_product",
        }
    return {
        "region": region,
        "date": date_text,
        "platform": platform,
        "product": short_name,
        "status": "unknown",
        "reason": f"Task not reflected in worker summary: {result}",
        "category": "unknown",
    }


def run_smoke_test(region: str, date_text: str) -> List[Dict[str, Any]]:
    """Real download of exactly one region x one required date, all 3 platforms."""
    halo = _region_halo(region)
    region_output = default_output_root() / region
    region_output.mkdir(parents=True, exist_ok=True)
    return [
        _run_one_task(region=region, date_text=date_text, platform=platform, halo=halo, region_output=region_output)
        for platform in PLATFORMS
    ]


def run_full_download(tasks: Dict[str, List[str]], regions: Sequence[str]) -> Dict[str, Any]:
    """Process every required (region, date) x platform task exactly once.

    Single coordinated process: tasks run sequentially region-by-region,
    date-by-date, platform-by-platform. No range expansion is ever
    performed -- each call requests exactly one (region, date) pair.

    `tasks` is expected to already be validated (see validate_required_tasks);
    `regions` may be the full set or an explicit subset for a partial run.
    """
    manifest: List[Dict[str, Any]] = []
    for region in regions:
        halo = _region_halo(region)
        region_output = default_output_root() / region
        region_output.mkdir(parents=True, exist_ok=True)
        (region_output / "run3_halo_metadata.json").write_text(
            json.dumps(_extra_tags(halo), ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        for date_text in tasks[region]:
            for platform in PLATFORMS:
                LOGGER.info("%s %s %s (%s)", region, date_text, platform, PRODUCT_BY_PLATFORM[platform])
                manifest.append(
                    _run_one_task(
                        region=region, date_text=date_text, platform=platform, halo=halo, region_output=region_output
                    )
                )

    status_counts: Dict[str, int] = defaultdict(int)
    for entry in manifest:
        status_counts[entry["status"]] += 1

    output_root = default_output_root()
    output_root.mkdir(parents=True, exist_ok=True)
    manifest_path = output_root / "run3_failure_manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2, default=str), encoding="utf-8")

    return {
        "total_tasks": len(manifest),
        "status_counts": dict(status_counts),
        "manifest_path": str(manifest_path),
        "failed_or_missing": [e for e in manifest if e["status"] not in ("success",)],
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="Print the plan without login, network, or writes.")
    parser.add_argument(
        "--pre-download-report", action="store_true", help="Print and assert required-task totals only."
    )
    parser.add_argument(
        "--smoke-test",
        nargs=2,
        metavar=("REGION", "DATE"),
        help="Real download of one region x one required date across all 3 platforms.",
    )
    parser.add_argument("--full", action="store_true", help="Run the full selective download (real, long-running).")
    parser.add_argument("--regions", nargs="+", choices=tuple(REGION_GEOJSONS), help="Optional region subset.")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")

    tasks = load_required_tasks()
    regions = args.regions or sorted(tasks)

    if args.pre_download_report:
        _print_pre_download_report(tasks)
        return 0

    if args.dry_run:
        _print_dry_run(tasks, regions)
        return 0

    if args.smoke_test:
        region, date_text = args.smoke_test
        if region not in REGION_GEOJSONS:
            parser.error(f"Unknown region for --smoke-test: {region}")
        if date_text not in tasks.get(region, []):
            parser.error(f"{date_text} is not a required date for {region} in {REQUIRED_DATES_CSV}")
        results = run_smoke_test(region, date_text)
        print(json.dumps(results, ensure_ascii=False, indent=2, default=str))
        return 0 if all(r["status"] == "success" for r in results) else 1

    if args.full:
        validate_required_tasks(tasks)
        summary = run_full_download(tasks, regions)
        print(json.dumps(summary, ensure_ascii=False, indent=2, default=str))
        return 0

    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
