"""The VIIRS L2 swath acquisition plan and its frozen manifest.

Two layers, like src/modis_plan.py:

* config/viirs_required_dates_daily.csv -- the 141 (region, date) rows chosen by
  the research PRE/POST daily-pair audit (127 region-dates of the 7 training
  regions + 14 of region04). Every date is acquired for all 3 platforms.
* config/viirs_run3_acquisitions.json -- for each of the 423 (region, platform,
  date) tasks: the exact daytime L2 SR granules (VNP09 / VJ109 / VJ209, collection
  002) per orbit, their exact IMG/MOD geolocation pair (from the SR InputPointer),
  or NO_GRANULE when CMR had no SR granule for that day. Built once from the
  acquisition summaries + raw archive with ``python -m src.viirs_plan --build``.
"""

from __future__ import annotations

import argparse
import csv
import json
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from .regions import FORBIDDEN_REGIONS, REGION_GEOJSONS

REQUIRED_DATES_CSV = Path(__file__).resolve().parents[1] / "config" / "viirs_required_dates_daily.csv"
DEFAULT_MANIFEST_PATH = Path(__file__).resolve().parents[1] / "config" / "viirs_run3_acquisitions.json"
MANIFEST_SCHEMA = "viirs-run3-acquisitions-v1"

PLATFORMS: Tuple[str, ...] = ("snpp", "noaa20", "noaa21")
SR_PRODUCT_BY_PLATFORM = {"snpp": "VNP09", "noaa20": "VJ109", "noaa21": "VJ209"}
EXPECTED_REQUIRED_REGION_DATE_TASKS = 141
EXPECTED_PLATFORM_REGION_DATE_TASKS = EXPECTED_REQUIRED_REGION_DATE_TASKS * len(PLATFORMS)

# The research VIIRS L2 swath pipeline (configs/sensors/viirs_pipeline*.json) builds
# its scenes with halo_m = 13000 for every region; using the same value gives the
# identical analysis-grid window (same transform and shape).
DEFAULT_HALO_M = 13_000.0

STATUS_GRANULES = "GRANULES"
STATUS_NO_GRANULE = "NO_GRANULE"


def load_required_tasks(csv_path: Path = REQUIRED_DATES_CSV) -> Dict[str, List[str]]:
    """Read the date authority. Returns {region: [YYYY-MM-DD, ...]} sorted; never infers dates."""
    csv_path = Path(csv_path)
    if not csv_path.is_file():
        raise FileNotFoundError(f"Authoritative VIIRS required-date plan not found: {csv_path}")
    with csv_path.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames != ["region", "date"]:
            raise ValueError(f"{csv_path}: expected header 'region,date', got {reader.fieldnames}")
        rows = list(reader)

    seen: set = set()
    tasks: Dict[str, List[str]] = defaultdict(list)
    for row in rows:
        region, date_text = row["region"].strip(), row["date"].strip()
        datetime.strptime(date_text, "%Y-%m-%d")
        if region in FORBIDDEN_REGIONS:
            raise ValueError(f"{csv_path}: {region} is out of scope (no 2024 burn events)")
        if region not in REGION_GEOJSONS:
            raise ValueError(f"{csv_path}: unknown region '{region}' has no configured GeoJSON")
        if (region, date_text) in seen:
            raise ValueError(f"{csv_path}: duplicate (region, date) row: {(region, date_text)}")
        seen.add((region, date_text))
        tasks[region].append(date_text)
    return {r: sorted(d) for r, d in tasks.items()}


def validate_required_tasks(tasks: Mapping[str, Sequence[str]]) -> Dict[str, int]:
    if set(tasks) != set(REGION_GEOJSONS):
        raise AssertionError(f"plan regions {sorted(tasks)} != {sorted(REGION_GEOJSONS)}")
    total = sum(len(d) for d in tasks.values())
    if total != EXPECTED_REQUIRED_REGION_DATE_TASKS:
        raise AssertionError(f"Expected {EXPECTED_REQUIRED_REGION_DATE_TASKS} region-date tasks, got {total}")
    return {"region_date_tasks": total, "platform_region_date_tasks": total * len(PLATFORMS)}


RESEARCH_REQUIRED_CSV = Path(__file__).resolve().parents[1] / "config" / "viirs_research_required_tasks.csv"
EXPECTED_RESEARCH_REQUIRED = 344


def load_research_required_tasks(csv_path: Path = RESEARCH_REQUIRED_CSV) -> set:
    """(region, platform, date) used as PRE or POST by the research VIIRS daily-pair manifest
    (viirs_burning/results/manifests/daily_pair_manifest.csv, 9,248 pairs, 7 regions; frozen 2026-10-27).
    Must be a subset of the plan; region04 is not used by the research."""
    with Path(csv_path).open(newline="", encoding="utf-8") as f:
        rows = {(r["region"], r["platform"], r["date"]) for r in csv.DictReader(f)}
    if len(rows) != EXPECTED_RESEARCH_REQUIRED:
        raise ValueError(f"{csv_path}: expected {EXPECTED_RESEARCH_REQUIRED} tasks, got {len(rows)}")
    outside = rows - set(plan_tasks())
    if outside:
        raise ValueError(f"{csv_path}: tasks outside the plan: {sorted(outside)[:5]}")
    return rows


def plan_tasks(regions: Optional[Sequence[str]] = None) -> List[Tuple[str, str, str]]:
    """(region, platform, YYYY-MM-DD) in a fixed order."""
    tasks = load_required_tasks()
    validate_required_tasks(tasks)
    return [(r, p, d) for r in sorted(tasks) if not regions or r in regions for d in tasks[r] for p in PLATFORMS]


@dataclass(frozen=True)
class Overpass:
    orbit: int
    granules: Tuple[str, ...]                 # SR file names, in start-time order
    geolocation: Tuple[Tuple[str, str], ...]  # (IMG, MOD) per SR granule
    aoi_status: str                           # at acquisition time (informational)


@dataclass(frozen=True)
class Task:
    region: str
    platform: str
    date: str  # YYYY-MM-DD
    status: str
    overpasses: Tuple[Overpass, ...]

    def raw_files(self) -> List[str]:
        out = []
        for ov in self.overpasses:
            for sr, (img, mod) in zip(ov.granules, ov.geolocation):
                out += [sr, img, mod]
        return out


def build_manifest(acquisition_root: Path) -> Dict[str, Any]:
    """Freeze tasks from an earlier acquisition (summaries + raw archive under acquisition_root/<region>/viirs/).

    A day without a summary is NO_GRANULE (CMR returned no SR granule). The SR
    granules of each summary must equal the daytime SR files in that day's raw
    directory -- the set the research pipeline reads -- otherwise the build fails.
    """
    from .viirs_l2 import parse_granule_name

    tasks = []
    for region, platform, d in plan_tasks():
        sr = SR_PRODUCT_BY_PLATFORM[platform]
        base = Path(acquisition_root) / region / "viirs"
        summary = base / "surface_reflectance" / platform / "l2_swath" / "summary" / f"{sr}_{platform}_{d.replace('-', '')}.json"
        if not summary.exists():
            tasks.append({"region": region, "platform": platform, "date": d, "status": STATUS_NO_GRANULE, "overpasses": []})
            continue
        s = json.loads(summary.read_text(encoding="utf-8"))
        ovs = [{"orbit": o["orbit"], "granules": o["granules"], "geolocation": o["geolocation"],
                "aoi_status": o["aoi_status"]} for o in s["overpasses"]]
        listed = sorted(g for o in ovs for g in o["granules"])
        doy = datetime.strptime(d, "%Y-%m-%d").timetuple().tm_yday
        on_disk = sorted(p.name for p in (base / "_raw_l2" / sr / d[:4] / f"{doy:03d}").glob("*.hdf"))
        if listed != on_disk:
            raise ValueError(f"{region}/{platform}/{d}: summary granules {listed} != raw SR files {on_disk}")
        for g in listed:
            if parse_granule_name(g).acq_date.isoformat() != d:
                raise ValueError(f"{g}: acquisition date is not {d}")
        tasks.append({"region": region, "platform": platform, "date": d, "status": STATUS_GRANULES, "overpasses": ovs})
    unique = {f for t in tasks for o in t["overpasses"] for g, geo in zip(o["granules"], o["geolocation"]) for f in [g, *geo]}
    return {"schema": MANIFEST_SCHEMA, "product_collection": "002", "halo_m": DEFAULT_HALO_M,
            "task_count": len(tasks), "unique_raw_files": len(unique),
            "source": "acquisition summaries (CMR DayNightFlag=DAY, SR InputPointer geolocation pairing) + raw archive listing",
            "tasks": tasks}


def load_acquisition_manifest(path: Path = DEFAULT_MANIFEST_PATH) -> List[Task]:
    """Load and validate against the CSV plan (exactly the 423 tasks)."""
    from .viirs_l2 import parse_granule_name

    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if data.get("schema") != MANIFEST_SCHEMA:
        raise ValueError(f"{path}: unexpected schema {data.get('schema')!r}")
    tasks = []
    for t in data["tasks"]:
        ovs = []
        for o in t["overpasses"]:
            if len(o["granules"]) != len(o["geolocation"]) or not o["granules"]:
                raise ValueError(f"{path}: {t['region']}/{t['platform']}/{t['date']} orbit {o['orbit']} is malformed")
            for g, (img, mod) in zip(o["granules"], o["geolocation"]):
                names = [parse_granule_name(n) for n in (g, img, mod)]
                if names[0].product != SR_PRODUCT_BY_PLATFORM[t["platform"]] or names[0].acq_date.isoformat() != t["date"]:
                    raise ValueError(f"{path}: {g} does not belong to {t['platform']} {t['date']}")
            ovs.append(Overpass(int(o["orbit"]), tuple(o["granules"]), tuple(tuple(x) for x in o["geolocation"]), o["aoi_status"]))
        if (t["status"] == STATUS_NO_GRANULE) != (not ovs):
            raise ValueError(f"{path}: {t['region']}/{t['platform']}/{t['date']} status {t['status']} inconsistent")
        tasks.append(Task(t["region"], t["platform"], t["date"], t["status"], tuple(ovs)))
    if [(t.region, t.platform, t.date) for t in tasks] != plan_tasks():
        raise ValueError(f"{path}: tasks do not exactly match {REQUIRED_DATES_CSV.name} x {PLATFORMS}")
    return tasks


def _main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Maintenance: (re)build the frozen VIIRS acquisition manifest.")
    parser.add_argument("--build", action="store_true", required=True)
    parser.add_argument("--acquisition-root", type=Path, required=True,
                        help="root holding <region>/viirs/{_raw_l2,surface_reflectance} of an earlier acquisition (read-only)")
    parser.add_argument("--output", type=Path, required=True, help="manifest JSON to write (must not exist)")
    args = parser.parse_args(argv)
    if args.output.exists():
        parser.error(f"refusing to overwrite {args.output}")
    data = build_manifest(args.acquisition_root)
    args.output.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    tasks = load_acquisition_manifest(args.output)
    print(f"wrote {args.output}: {len(tasks)} tasks, {data['unique_raw_files']} unique raw files")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
