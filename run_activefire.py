"""FIRMS Active Fire acquisition for the research Binary AF4 input (MODIS + VIIRS).

Moved here unchanged in behaviour from the old run2.py (MODIS) and the old
run4.py (VIIRS) when run2/run3/run4 became the per-sensor reflectance entry
points. Every fetch goes through the existing src.activefire._process_activefire().

  MODIS  Terra+Aqua  MODIS_SP           (science-quality); FIRMS bbox = the region's
                                         native-snapped MODIS halo bbox (10 km, region01 13 km),
                                         no extra buffer, no AOI clip
  VIIRS  S-NPP       VIIRS_SNPP_SP      (science-quality)
         NOAA-20     VIIRS_NOAA20_SP    (science-quality)
         NOAA-21     VIIRS_NOAA21_NRT   (NRT -- FIRMS has no NOAA-21 SP source; always labelled NRT)
                     FIRMS bbox = AOI bbox + the region halo (10 km, region01 13 km)

Period: 2024-02-08 .. 2024-04-10, continuous (not limited to reflectance dates).
Output: ESRI shapefiles in WGS84, one per UTC acquisition date plus one period
summary, FIRMS columns unchanged (acq_date / acq_time UTC, satellite, confidence,
version, frp, daynight, ...):

  <root>/<region>/modis/activefire/ACFR_<YYYYMMDD>_<HHMM>.shp       (+ C2 contract copy:
  <root>/MODIS/<region>/2024/activefire/ACFR_*)                      research Aso/MODIS/<region>/2024/activefire_halo/)
  <root>/<region>/<platform>/viirs/activefire/ACFR_*.shp             (research Aso/VIIRS/<region>/2024/activefire_halo/<platform>/viirs/activefire/)
  <root>/<region>/combined/activefire_combined.csv                   (VIIRS events tagged with platform/source/level)

Modes: --dry-run (default; no network), --download (FIRMS; a region/source whose
output already holds shapefiles is skipped, never overwritten), --reproduce
(rebuild the combined CSV and the C2 contract copies from the shapefiles on disk,
no network), --verify (schema / CRS / dates / platform / bbox of every shapefile).
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import re
import shutil
import sys
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from src.halo_context import build_halo_context, halo_buffer_m_for_region, load_geojson_geometry
from src.modis_plan import REGION_GEOJSONS as MODIS_REGION_GEOJSONS
from src.regions import REGION_GEOJSONS as VIIRS_REGION_GEOJSONS

LOGGER = logging.getLogger("run_activefire")

OUTPUT_ROOT_ENV = "ACTIVEFIRE_OUTPUT_ROOT"
REPORT_DIR = "_activefire_reports"
DATE_START = "2024-02-08"
DATE_END = "2024-04-10"

MODIS_PRODUCT = "MODIS_SP"
VIIRS_PLATFORM_PRODUCT = {"snpp": "VIIRS_SNPP_SP", "noaa20": "VIIRS_NOAA20_SP", "noaa21": "VIIRS_NOAA21_NRT"}
PROCESSING_LEVEL = {"MODIS_SP": "SP", "VIIRS_SNPP_SP": "SP", "VIIRS_NOAA20_SP": "SP", "VIIRS_NOAA21_NRT": "NRT"}
VIIRS_PLATFORMS = ("snpp", "noaa20", "noaa21")
# FIRMS 'satellite' column values per source
SATELLITE_VALUES = {"MODIS_SP": {"Terra", "Aqua"}, "VIIRS_SNPP_SP": {"N"}, "VIIRS_NOAA20_SP": {"N20"}, "VIIRS_NOAA21_NRT": {"N21"}}
# FIRMS columns per source, as in the shapefiles the research AF4 catalogue was built from:
# SP archives carry 'type'; the NOAA-21 NRT feed does not, and its version ends in "NRT".
_MODIS_FIELDS = ["brightness", "scan", "track", "acq_date", "acq_time", "satellite", "instrument", "confidence",
                 "version", "bright_t31", "frp", "daynight", "type"]
_VIIRS_FIELDS = ["bright_ti4", "scan", "track", "acq_date", "acq_time", "satellite", "instrument", "confidence",
                 "version", "bright_ti5", "frp", "daynight"]
SCHEMA_FIELDS = {"MODIS_SP": _MODIS_FIELDS, "VIIRS_SNPP_SP": _VIIRS_FIELDS + ["type"],
                 "VIIRS_NOAA20_SP": _VIIRS_FIELDS + ["type"], "VIIRS_NOAA21_NRT": _VIIRS_FIELDS}

_DAILY_SHP_RE = re.compile(r"^ACFR_(\d{8})_\d{4}\.shp$")
_SUMMARY_SHP_RE = re.compile(r"^ACFR_(\d{8})_(\d{8})_\d{4}\.shp$")

_process_activefire = None


def _load_worker() -> None:
    global _process_activefire
    if _process_activefire is None:
        from src.activefire import _process_activefire as worker

        _process_activefire = worker


def _parse_date(value: str) -> date:
    return datetime.strptime(value, "%Y-%m-%d").date()


def _geometry(region: str, table: Dict[str, str]) -> Dict[str, Any]:
    return load_geojson_geometry(Path(__file__).resolve().parent / table[region])


def modis_request(region: str) -> Dict[str, Any]:
    """FIRMS call arguments for MODIS: exactly the native-snapped MODIS halo bbox, no extra buffer (old run2.py)."""
    halo = build_halo_context(_geometry(region, MODIS_REGION_GEOJSONS), halo_buffer_m_for_region(region))
    return {"config": {"firms": {"bbox_buffer_m": 0, "clip_to_aoi": False}}, "bbox": halo["halo_bbox_wgs84"],
            "geometry_wgs84": halo["halo_wgs84_geometry"], "satellites": ["modis"],
            "product_map_override": {"modis": [MODIS_PRODUCT], "viirs": []}}


def viirs_request(region: str, platform: str) -> Dict[str, Any]:
    """FIRMS call arguments for one VIIRS platform: AOI bbox + region halo as FIRMS bbox buffer (old run4.py)."""
    from src.geometry import _bbox_from_geometry

    geometry = _geometry(region, VIIRS_REGION_GEOJSONS)
    return {"config": {"firms": {"bbox_buffer_m": halo_buffer_m_for_region(region)}}, "bbox": _bbox_from_geometry(geometry),
            "geometry_wgs84": geometry, "satellites": ["viirs"],
            "product_map_override": {"modis": [], "viirs": [VIIRS_PLATFORM_PRODUCT[platform]]}}


def jobs(regions_modis: Sequence[str], regions_viirs: Sequence[str]) -> List[Tuple[str, str, str, Path]]:
    """(region, sensor, source product, relative activefire dir)."""
    out = [(r, "modis", MODIS_PRODUCT, Path(r) / "modis" / "activefire") for r in regions_modis]
    out += [(r, "viirs", VIIRS_PLATFORM_PRODUCT[p], Path(r) / p / "viirs" / "activefire") for r in regions_viirs for p in VIIRS_PLATFORMS]
    return out


def _worker_root(output_root: Path, rel: Path) -> Path:
    return output_root / rel.parent.parent  # _process_activefire appends <sensor>/activefire


def daily_dates_covered(activefire_dir: Path) -> Dict[str, int]:
    """YYYY-MM-DD -> record count for every daily shapefile."""
    import shapefile

    covered: Dict[str, int] = {}
    for shp in sorted(activefire_dir.glob("ACFR_*.shp")) if activefire_dir.exists() else []:
        m = _DAILY_SHP_RE.match(shp.name)
        if m:
            t = m.group(1)
            with shapefile.Reader(str(shp)) as r:
                covered[f"{t[:4]}-{t[4:6]}-{t[6:]}"] = covered.get(f"{t[:4]}-{t[4:6]}-{t[6:]}", 0) + r.numRecords
    return covered


def publish_modis_contract(output_root: Path, region: str) -> List[str]:
    """Copy MODIS_SP shapefile sets to the downstream C2 contract path (never overwrites)."""
    source_dir = output_root / region / "modis" / "activefire"
    contract_dir = output_root / "MODIS" / region / "2024" / "activefire"
    published: List[str] = []
    for shp in sorted(source_dir.glob("ACFR_*.shp")) if source_dir.exists() else []:
        for f in source_dir.glob(f"{shp.stem}.*"):
            target = contract_dir / f.name
            if not target.exists():
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(f, target)
                published.append(str(target))
    return published


def write_combined_viirs(output_root: Path, region: str) -> Optional[str]:
    """Per-region CSV of every VIIRS daily event tagged with platform/source/level (period summaries skipped)."""
    import shapefile

    rows: List[Dict[str, Any]] = []
    for platform in VIIRS_PLATFORMS:
        d = output_root / region / platform / "viirs" / "activefire"
        for shp in sorted(d.glob("ACFR_*.shp")) if d.exists() else []:
            if _SUMMARY_SHP_RE.match(shp.name):
                continue
            with shapefile.Reader(str(shp)) as r:
                names = [f[0] for f in r.fields[1:]]
                for rec in r.iterShapeRecords():
                    row = dict(zip(names, rec.record))
                    product = VIIRS_PLATFORM_PRODUCT[platform]
                    row.update(platform=platform, source_product=product, processing_level=PROCESSING_LEVEL[product])
                    rows.append(row)
    if not rows:
        return None
    path = output_root / region / "combined" / "activefire_combined.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".part")
    with tmp.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(dict.fromkeys(k for r in rows for k in r)))
        w.writeheader()
        w.writerows(rows)
    os.replace(tmp, path)  # derived index: rebuilt from the shapefiles, never the shapefiles themselves
    return str(path)


def verify_shapefile(shp: Path, product: str, bbox: Tuple[float, float, float, float],
                     start: date, end: date) -> List[str]:
    """Schema, CRS, date range, platform and spatial checks of one FIRMS shapefile."""
    import shapefile

    problems = []
    prj = shp.with_suffix(".prj")
    if not prj.exists() or not prj.read_text().startswith('GEOGCS["WGS 84"'):
        problems.append("CRS is not WGS84 (EPSG:4326)")
    m_daily, m_sum = _DAILY_SHP_RE.match(shp.name), _SUMMARY_SHP_RE.match(shp.name)
    with shapefile.Reader(str(shp)) as r:
        names = [f[0] for f in r.fields[1:]]
        if names != SCHEMA_FIELDS[product]:
            problems.append(f"fields {names} != {SCHEMA_FIELDS[product]}")
            return problems
        if r.shapeType != shapefile.POINT:
            problems.append(f"shape type {r.shapeType} is not POINT")
        w, s, e, n = bbox
        for rec in r.iterShapeRecords():
            row = dict(zip(names, rec.record))
            d = str(row["acq_date"])
            if not (start.isoformat() <= d <= end.isoformat()):
                problems.append(f"acq_date {d} outside {start}..{end}")
            if m_daily and d.replace("-", "") != m_daily.group(1):
                problems.append(f"acq_date {d} in daily file {shp.name}")
            if str(row["satellite"]) not in SATELLITE_VALUES[product]:
                problems.append(f"satellite {row['satellite']} is not a {product} platform")
            if str(row["version"]).upper().endswith("NRT") != (PROCESSING_LEVEL[product] == "NRT"):
                problems.append(f"version {row['version']} does not match processing level {PROCESSING_LEVEL[product]}")
            if not str(row["acq_time"]).strip().isdigit():
                problems.append(f"acq_time {row['acq_time']!r} is not HHMM UTC")
            x, y = rec.shape.points[0]
            if not (w - 1e-6 <= x <= e + 1e-6 and s - 1e-6 <= y <= n + 1e-6):
                problems.append(f"point {x},{y} outside the request bbox")
            if len(problems) > 20:
                break
    if not (m_daily or m_sum):
        problems.append("file name is not ACFR_<date>_<HHMM> or ACFR_<start>_<end>_<HHMM>")
    return problems


def request_bbox(region: str, sensor: str) -> Tuple[float, float, float, float]:
    if sensor == "modis":
        return tuple(modis_request(region)["bbox"])
    from src.geometry import _expand_bbox_by_meters

    req = viirs_request(region, "snpp")
    return tuple(_expand_bbox_by_meters(req["bbox"], req["config"]["firms"]["bbox_buffer_m"]))


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%fZ")


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="print requests and existing outputs (default)")
    mode.add_argument("--download", action="store_true", help="fetch FIRMS for every source without existing shapefiles")
    mode.add_argument("--reproduce", action="store_true", help="rebuild combined CSV and C2 contract copies (offline)")
    mode.add_argument("--verify", action="store_true", help="check every shapefile under --output-root")
    parser.add_argument("--output-root", type=Path, default=None, help=f"AF output root (or env {OUTPUT_ROOT_ENV})")
    parser.add_argument("--sensors", nargs="+", choices=("modis", "viirs"), default=["modis", "viirs"])
    parser.add_argument("--regions", nargs="+", choices=tuple(VIIRS_REGION_GEOJSONS))
    parser.add_argument("--start", default=DATE_START)
    parser.add_argument("--end", default=DATE_END)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")

    output_root = args.output_root or (Path(os.environ[OUTPUT_ROOT_ENV]) if os.environ.get(OUTPUT_ROOT_ENV) else None)
    if output_root is None:
        parser.error(f"--output-root or {OUTPUT_ROOT_ENV} is required; no default data root is assumed")
    start, end = _parse_date(args.start), _parse_date(args.end)
    regions_modis = [r for r in (args.regions or MODIS_REGION_GEOJSONS) if r in MODIS_REGION_GEOJSONS] if "modis" in args.sensors else []
    regions_viirs = list(args.regions or VIIRS_REGION_GEOJSONS) if "viirs" in args.sensors else []
    mode_name = "download" if args.download else "reproduce" if args.reproduce else "verify" if args.verify else "dry_run"

    rows: List[Dict[str, Any]] = []
    for region, sensor, product, rel in jobs(regions_modis, regions_viirs):
        d = output_root / rel
        existing = sorted(p.name for p in d.glob("ACFR_*.shp")) if d.exists() else []
        row: Dict[str, Any] = {"region": region, "sensor": sensor, "source": product,
                               "processing_level": PROCESSING_LEVEL[product], "dir": str(d),
                               "existing_shapefiles": len(existing), "request_bbox_wgs84": request_bbox(region, sensor)}
        rows.append(row)
        if args.download:
            if existing:
                row["status"] = "ALREADY_PRESENT"
                continue
            _load_worker()
            req = modis_request(region) if sensor == "modis" else viirs_request(region, rel.parts[1])
            try:
                row["worker_summary"] = _process_activefire(config_dir=Path.cwd(), output_root=_worker_root(output_root, rel),
                                                            reference_crs=None, reference_raster_path=None,
                                                            start_date=start, end_date=end, **req)
                row["status"] = "DOWNLOADED"
            except Exception as exc:
                LOGGER.error("%s %s %s: %s", region, sensor, product, exc, exc_info=True)
                row.update(status="FAILED", error=str(exc))
                continue
        elif args.verify:
            problems = {p.name: verify_shapefile(d / p, product, row["request_bbox_wgs84"], start, end) for p in
                        map(Path, existing)}
            row["problems"] = {k: v for k, v in problems.items() if v}
            row["status"] = "NO_OUTPUT" if not existing else "SPEC_OK" if not row["problems"] else "SPEC_MISMATCH"
        else:
            row["status"] = "PRESENT" if existing else "NOT_DOWNLOADED"
        if args.download or args.verify or args.reproduce:
            daily = daily_dates_covered(d)
            row["dates_with_detections"] = len(daily)
            row["event_count"] = sum(daily.values())

    report: Dict[str, Any] = {"mode": mode_name, "period": [start.isoformat(), end.isoformat()],
                              "output_root": str(output_root), "network_used": bool(args.download)}
    if args.download or args.reproduce:
        report["modis_contract_files"] = {r: publish_modis_contract(output_root, r) for r in regions_modis}
        report["viirs_combined_csv"] = {r: write_combined_viirs(output_root, r) for r in regions_viirs}
    report["status_counts"] = dict(Counter(r["status"] for r in rows))
    report["jobs"] = rows
    if mode_name != "dry_run":
        path = output_root / REPORT_DIR / f"activefire_{mode_name}_{_stamp()}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        report["report"] = str(path)
    print(json.dumps({k: v for k, v in report.items() if k != "jobs"}, ensure_ascii=False, indent=2, default=str))
    for r in rows:
        print(f"  {r['status']:<16} {r['region']} {r['sensor']:<5} {r['source']:<17} ({r['processing_level']}) {r['dir']}")
    return 1 if any(r["status"] in ("FAILED", "SPEC_MISMATCH") for r in rows) else 0


if __name__ == "__main__":
    sys.exit(main())
