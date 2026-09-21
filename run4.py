"""Continuous-coverage VIIRS Active Fire (FIRMS) downloader for S-NPP,
NOAA-20 and NOAA-21, for the 8 study regions used by run3.py.

This module does not touch Surface Reflectance code (src/surface_reflectance.py,
run3.py) or run2.py at all. It calls the existing, unmodified
src.activefire._process_activefire() once per (region, platform), so all
FIRMS fetch/date-window-chunking/AOI-clip/shapefile-writing behavior is
exactly what the rest of the project already relies on -- this module only
supplies the platform-specific product name, continuous date range, and a
platform-separated output_root per call.

Source policy (confirmed live against FIRMS's own /api/data_availability/
catalog, the FIRMS Archive Download page, and its site JS bundles -- see
conversation record; there is currently no VIIRS_NOAA21_SP source anywhere
in FIRMS, only VIIRS_NOAA21_NRT since 2024-01-17):

    S-NPP   -> VIIRS_SNPP_SP     (science-quality)
    NOAA-20 -> VIIRS_NOAA20_SP   (science-quality)
    NOAA-21 -> VIIRS_NOAA21_NRT  (NRT -- no SP source exists; explicitly
                                   labeled as NRT everywhere, never presented
                                   as science-quality)
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Sequence

try:
    import shapefile
except ImportError as exc:  # pragma: no cover
    raise RuntimeError("pyshp is required. Please install pyshp.") from exc

from run3 import REGION_GEOJSONS  # 8-region set, unchanged; does not import run3's SR workers
import run2  # reused only for its region halo-buffer policy constants, unmodified

LOGGER = logging.getLogger(__name__)

DATE_START = "2024-02-08"
DATE_END = "2024-04-10"

PLATFORM_PRODUCT: Dict[str, str] = {
    "snpp": "VIIRS_SNPP_SP",
    "noaa20": "VIIRS_NOAA20_SP",
    "noaa21": "VIIRS_NOAA21_NRT",
}
PLATFORM_PROCESSING_LEVEL: Dict[str, str] = {
    "snpp": "SP",
    "noaa20": "SP",
    "noaa21": "NRT",
}
PLATFORMS: tuple = ("snpp", "noaa20", "noaa21")

# FIRMS's blanket 5km default bbox buffer (FIRMS_DEFAULT_BBOX_BUFFER_M) was
# confirmed too narrow for these small per-region AOIs: a real test against a
# MODIS-confirmed region01 burn date (2024-03-10) returned zero VIIRS
# detections at 5km but real detections at 13km. Per user decision, reuse
# run2.py's already-established per-region halo buffer policy instead
# (run2.py itself is not modified; only its constants are read).
def _bbox_buffer_m_for_region(region: str) -> float:
    return run2.REGION_HALO_BUFFER_M.get(region, run2.DEFAULT_HALO_BUFFER_M)

_DAILY_SHP_RE = re.compile(r"^ACFR_(\d{8})_\d{4}\.shp$")
_SUMMARY_SHP_RE = re.compile(r"^ACFR_(\d{8})_(\d{8})_\d{4}\.shp$")

# Assigned lazily so --dry-run stays dependency-free of the heavier stack
# (rasterio/requests aren't heavy, but this keeps the same lazy-import
# convention as run2.py/run3.py).
_process_activefire = None
_load_aoi_geometry = None
_bbox_from_geometry = None


def _load_runtime_workers() -> None:
    global _process_activefire, _load_aoi_geometry, _bbox_from_geometry
    if _process_activefire is not None:
        return
    from src.activefire import _process_activefire as activefire_worker
    from src.geometry import _bbox_from_geometry as bbox_worker
    from src.geometry import _load_aoi_geometry as geom_worker

    _process_activefire = activefire_worker
    _load_aoi_geometry = geom_worker
    _bbox_from_geometry = bbox_worker


def default_output_root() -> Path:
    import os

    host_data = Path(os.environ.get("SATDL_HOST_DATA_PATH", "/host_data"))
    return host_data / "aoi_rectangle" / "output_viirs_activefire"


def _parse_date(value: str) -> date:
    return datetime.strptime(value, "%Y-%m-%d").date()


def _print_dry_run(regions: Sequence[str]) -> None:
    print(f"Continuous VIIRS Active Fire acquisition: {DATE_START} .. {DATE_END}")
    print(f"Regions: {', '.join(regions)}")
    print("Platform -> FIRMS source (processing level):")
    for platform in PLATFORMS:
        print(f"  {platform:8s} -> {PLATFORM_PRODUCT[platform]} ({PLATFORM_PROCESSING_LEVEL[platform]})")
    print()
    for region in regions:
        geojson_path = (Path.cwd() / REGION_GEOJSONS[region]).resolve()
        region_output = default_output_root() / region
        print(f"{region}: {geojson_path} (bbox buffer: {_bbox_buffer_m_for_region(region):.0f} m)")
        for platform in PLATFORMS:
            print(f"  {platform}: {region_output / platform / 'viirs' / 'activefire'}")


def _count_shapefile_records(shp_path: Path) -> int:
    if not shp_path.exists():
        return 0
    with shapefile.Reader(str(shp_path)) as reader:
        return reader.numRecords


def _daily_dates_covered(activefire_dir: Path) -> Dict[str, int]:
    """Map YYYY-MM-DD -> event count for every daily shapefile actually written."""
    covered: Dict[str, int] = {}
    if not activefire_dir.exists():
        return covered
    for shp_path in sorted(activefire_dir.glob("ACFR_*.shp")):
        match = _DAILY_SHP_RE.match(shp_path.name)
        if not match:
            continue
        token = match.group(1)
        date_text = f"{token[0:4]}-{token[4:6]}-{token[6:8]}"
        covered[date_text] = _count_shapefile_records(shp_path)
    return covered


def _all_dates_in_range(start: date, end: date) -> List[str]:
    from datetime import timedelta

    n = (end - start).days
    return [(start + timedelta(days=i)).isoformat() for i in range(n + 1)]


def run_smoke_test(region: str, smoke_start: str | None = None, smoke_end: str | None = None) -> Dict[str, Any]:
    """Small historical sample for all 3 platforms.

    Defaults to a 5-day window at the start of the continuous range; callers
    may pass an explicit window already known to contain detections to
    validate schema parsing against non-empty results.
    """
    _load_runtime_workers()
    geojson_path = (Path.cwd() / REGION_GEOJSONS[region]).resolve()
    geometry_wgs84 = _load_aoi_geometry(geojson_path)
    bbox = _bbox_from_geometry(geometry_wgs84)

    from datetime import timedelta

    start_date = _parse_date(smoke_start) if smoke_start else _parse_date(DATE_START)
    end_date = _parse_date(smoke_end) if smoke_end else (start_date + timedelta(days=4))

    bbox_buffer_m = _bbox_buffer_m_for_region(region)
    results: Dict[str, Any] = {}
    for platform in PLATFORMS:
        product = PLATFORM_PRODUCT[platform]
        region_output = default_output_root() / region / platform
        region_output.mkdir(parents=True, exist_ok=True)
        summary = _process_activefire(
            config={"firms": {"bbox_buffer_m": bbox_buffer_m}},
            config_dir=Path.cwd(),
            output_root=region_output,
            bbox=bbox,
            geometry_wgs84=geometry_wgs84,
            reference_crs=None,
            reference_raster_path=None,
            start_date=start_date,
            end_date=end_date,
            satellites=["viirs"],
            product_map_override={"modis": [], "viirs": [product]},
        )
        activefire_dir = region_output / "viirs" / "activefire"
        daily = _daily_dates_covered(activefire_dir)
        sample_rows = []
        for shp_path in sorted(activefire_dir.glob("ACFR_*.shp"))[:1]:
            with shapefile.Reader(str(shp_path)) as reader:
                field_names = [f[0] for f in reader.fields[1:]]
                for shape_rec in reader.iterShapeRecords():
                    sample_rows.append(dict(zip(field_names, shape_rec.record)))
                    if len(sample_rows) >= 3:
                        break
        results[platform] = {
            "product": product,
            "processing_level": PLATFORM_PROCESSING_LEVEL[platform],
            "worker_summary": summary,
            "daily_dates_written": daily,
            "sample_rows": sample_rows,
            "output_dir": str(activefire_dir),
        }
    return results


def run_full_acquisition(regions: Sequence[str]) -> Dict[str, Any]:
    """Continuous DATE_START..DATE_END VIIRS AF acquisition for all platforms."""
    _load_runtime_workers()
    start_date = _parse_date(DATE_START)
    end_date = _parse_date(DATE_END)
    full_range = _all_dates_in_range(start_date, end_date)

    manifest: List[Dict[str, Any]] = []
    region_totals: Dict[str, Dict[str, int]] = {}

    for region in regions:
        geojson_path = (Path.cwd() / REGION_GEOJSONS[region]).resolve()
        geometry_wgs84 = _load_aoi_geometry(geojson_path)
        bbox = _bbox_from_geometry(geometry_wgs84)
        bbox_buffer_m = _bbox_buffer_m_for_region(region)
        region_totals[region] = {}

        for platform in PLATFORMS:
            product = PLATFORM_PRODUCT[platform]
            region_output = default_output_root() / region / platform
            region_output.mkdir(parents=True, exist_ok=True)
            entry: Dict[str, Any] = {
                "region": region,
                "platform": platform,
                "date_start": DATE_START,
                "date_end": DATE_END,
                "source": product,
                "processing_level": PLATFORM_PROCESSING_LEVEL[platform],
                "bbox_buffer_m": bbox_buffer_m,
            }
            try:
                LOGGER.info("%s %s (%s, %s)", region, platform, product, PLATFORM_PROCESSING_LEVEL[platform])
                _process_activefire(
                    config={"firms": {"bbox_buffer_m": bbox_buffer_m}},
                    config_dir=Path.cwd(),
                    output_root=region_output,
                    bbox=bbox,
                    geometry_wgs84=geometry_wgs84,
                    reference_crs=None,
                    reference_raster_path=None,
                    start_date=start_date,
                    end_date=end_date,
                    satellites=["viirs"],
                    product_map_override={"modis": [], "viirs": [product]},
                )
                activefire_dir = region_output / "viirs" / "activefire"
                daily = _daily_dates_covered(activefire_dir)
                missing = [d for d in full_range if d not in daily]
                event_count = sum(daily.values())
                entry.update(
                    {
                        "status": "success",
                        "event_count": event_count,
                        "dates_with_detections": len(daily),
                        "missing_dates": missing,
                        "reason_if_missing": None,
                    }
                )
                region_totals[region][platform] = event_count
            except Exception as exc:
                LOGGER.error("%s %s failed: %s", region, platform, exc, exc_info=True)
                entry.update(
                    {
                        "status": "failed",
                        "event_count": 0,
                        "dates_with_detections": 0,
                        "missing_dates": full_range,
                        "reason_if_missing": str(exc),
                    }
                )
                region_totals[region][platform] = 0
            manifest.append(entry)

        region_totals[region]["combined"] = sum(
            v for k, v in region_totals[region].items() if k != "combined"
        )

    output_root = default_output_root()
    output_root.mkdir(parents=True, exist_ok=True)
    manifest_path = output_root / "run4_activefire_manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2, default=str), encoding="utf-8")

    combined_paths = _write_combined_views(regions)

    return {
        "manifest": manifest,
        "manifest_path": str(manifest_path),
        "region_totals": region_totals,
        "combined_views": combined_paths,
    }


def _write_combined_views(regions: Sequence[str]) -> Dict[str, str]:
    """Deterministic combined per-region CSV index, tagging each already-written
    event with its source platform -- built by reading back the per-platform
    shapefiles already on disk (no re-download), so platform-specific
    originals are never touched or overwritten."""
    import csv

    combined_paths: Dict[str, str] = {}
    for region in regions:
        region_root = default_output_root() / region
        combined_rows: List[Dict[str, Any]] = []
        for platform in PLATFORMS:
            activefire_dir = region_root / platform / "viirs" / "activefire"
            if not activefire_dir.exists():
                continue
            for shp_path in sorted(activefire_dir.glob("ACFR_*.shp")):
                if _SUMMARY_SHP_RE.match(shp_path.name):
                    continue  # skip the period-summary file to avoid double-counting daily events
                with shapefile.Reader(str(shp_path)) as reader:
                    field_names = [f[0] for f in reader.fields[1:]]
                    for shape_rec in reader.iterShapeRecords():
                        row = dict(zip(field_names, shape_rec.record))
                        row["platform"] = platform
                        row["source_product"] = PLATFORM_PRODUCT[platform]
                        row["processing_level"] = PLATFORM_PROCESSING_LEVEL[platform]
                        combined_rows.append(row)

        if not combined_rows:
            continue
        combined_dir = region_root / "combined"
        combined_dir.mkdir(parents=True, exist_ok=True)
        combined_path = combined_dir / "activefire_combined.csv"
        fieldnames = list(dict.fromkeys(k for row in combined_rows for k in row.keys()))
        with combined_path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(combined_rows)
        combined_paths[region] = str(combined_path)
    return combined_paths


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--smoke-test", metavar="REGION")
    parser.add_argument("--smoke-start", metavar="YYYY-MM-DD")
    parser.add_argument("--smoke-end", metavar="YYYY-MM-DD")
    parser.add_argument("--full", action="store_true")
    parser.add_argument("--regions", nargs="+", choices=tuple(REGION_GEOJSONS))
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
    regions = args.regions or sorted(REGION_GEOJSONS)

    if args.dry_run:
        _print_dry_run(regions)
        return 0

    if args.smoke_test:
        if args.smoke_test not in REGION_GEOJSONS:
            parser.error(f"Unknown region: {args.smoke_test}")
        results = run_smoke_test(args.smoke_test, args.smoke_start, args.smoke_end)
        print(json.dumps(results, ensure_ascii=False, indent=2, default=str))
        return 0

    if args.full:
        summary = run_full_acquisition(regions)
        print(json.dumps(summary, ensure_ascii=False, indent=2, default=str))
        return 0

    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
