"""The MODIS acquisition plan for the burned-area research, and its frozen manifest.

Two layers:

* ``RUN2_DOWNLOAD_PLAN`` -- the reviewed literal list of 218 (region,
  platform, date) acquisitions (Aqua 111, Terra 107, 7 regions), moved
  verbatim from the old run2.py. Never derived from a date range.
* ``config/modis_run2_acquisitions.json`` -- for each of those 218
  acquisitions, the exact source granules (filename incl. processing
  timestamp, exact LP DAAC URL confirmed via CMR) and the exact native grids
  (transform + shape) of the authoritative 500m / QC_500m / state_1km
  images. Built once from the authoritative image sidecars with
  ``python -m src.modis_plan --build ...`` and then committed, so image
  generation does not need the reference images at all.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from .halo_context import parse_plan_date

PRODUCT_BY_PLATFORM = {"terra": "MOD09GA", "aqua": "MYD09GA"}
VERSION = "061"
MANIFEST_SCHEMA = "modis-run2-acquisitions-v1"
DEFAULT_MANIFEST_PATH = Path(__file__).resolve().parents[1] / "config" / "modis_run2_acquisitions.json"
GRID_KEYS = ("sr", "qc_500m", "state_1km")

# Do not derive this from a range or a statistical filter. This is the
# reviewed C2 TRAIN+VAL selection, in full.
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


def validate_download_plan(plan: Mapping[str, Mapping[str, Sequence[str]]] = RUN2_DOWNLOAD_PLAN) -> Dict[str, int]:
    """Validate the literal plan, returning the expected platform totals."""
    if set(plan) != set(REGION_GEOJSONS):
        raise ValueError("RUN2_DOWNLOAD_PLAN regions must exactly match REGION_GEOJSONS")
    totals = {"aqua": 0, "terra": 0}
    for region, platforms in plan.items():
        if set(platforms) != set(totals):
            raise ValueError(f"{region}: plan must contain exactly aqua and terra")
        for platform, values in platforms.items():
            parsed = [parse_plan_date(v) for v in values]
            if len(parsed) != len(set(parsed)):
                raise ValueError(f"{region}/{platform}: duplicate date in literal plan")
            totals[platform] += len(parsed)
    if totals != {"aqua": 111, "terra": 107} or sum(totals.values()) != 218:
        raise AssertionError(f"Unexpected RUN2 scene totals: {totals}")
    return totals


def plan_acquisitions(regions: Optional[Sequence[str]] = None) -> List[Tuple[str, str, str]]:
    """(region, platform, YYYYMMDD) in a fixed order."""
    validate_download_plan()
    out = []
    for region in sorted(RUN2_DOWNLOAD_PLAN):
        if regions and region not in regions:
            continue
        for platform in ("aqua", "terra"):
            for d in RUN2_DOWNLOAD_PLAN[region][platform]:
                out.append((region, platform, d.replace("-", "")))
    return out


# ---------------------------------------------------------------------------
# Frozen per-acquisition manifest
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class AcquisitionSpec:
    region: str
    platform: str
    date: str  # YYYYMMDD
    granules: Tuple[str, ...]
    grids: Dict[str, Tuple[Tuple[float, ...], Tuple[int, int]]]  # key -> (affine 6, (height, width))


def cmr_exact_lookup(granule_id: str, *, get=None) -> Dict[str, Any]:
    """Exact-name CMR lookup (public metadata, no auth, no data download).

    Requires exactly one granule whose producer_granule_id is the exact
    filename stem and one HTTPS link ending in the exact filename. No
    fallback to other processing timestamps.
    """
    if get is None:
        import requests

        get = requests.get
    product = granule_id.split(".")[0]
    stem = granule_id[:-4] if granule_id.endswith(".hdf") else granule_id
    resp = get("https://cmr.earthdata.nasa.gov/search/granules.json",
               params={"short_name": product, "version": VERSION, "readable_granule_name": stem}, timeout=60)
    resp.raise_for_status()
    entries = resp.json()["feed"]["entry"]
    matches = [e for e in entries if e.get("producer_granule_id") == stem]
    if len(matches) != 1:
        raise RuntimeError(f"{granule_id}: expected exactly one exact CMR match, got {len(matches)}")
    urls = [link["href"] for link in matches[0].get("links", [])
            if link.get("href", "").startswith("https://") and link["href"].endswith("/" + granule_id)]
    if len(urls) != 1:
        raise RuntimeError(f"{granule_id}: expected exactly one exact HTTPS link, got {urls}")
    return {"https_url": urls[0], "cmr_size_mb": matches[0].get("granule_size")}


def build_manifest(reference_root: Path, *, lookup=cmr_exact_lookup) -> Dict[str, Any]:
    """Freeze granules + native grids of the 218 authoritative images (read-only)."""
    import rasterio

    from .modis_native_window import Acquisition, output_paths, reference_source_granules
    from .modis_raw_hdf import parse_granule_id

    acquisitions = []
    regions_by_granule: Dict[str, set] = {}
    platform_by_granule: Dict[str, str] = {}
    for region, platform, d in plan_acquisitions():
        acq = Acquisition(region, platform, d)
        granules = reference_source_granules(reference_root, acq)
        grids = {}
        for key, path in output_paths(reference_root, acq).items():
            with rasterio.open(path) as ds:
                grids[key] = {"transform": list(ds.transform)[:6], "shape": [ds.height, ds.width]}
        acquisitions.append({"region": region, "platform": platform, "date": d,
                             "product": PRODUCT_BY_PLATFORM[platform], "granules": granules, "grids": grids})
        for g in granules:
            regions_by_granule.setdefault(g, set()).add(region)
            platform_by_granule[g] = platform

    granules = []
    for g in sorted(regions_by_granule):
        parsed = parse_granule_id(g)
        granules.append({"granule_id": g, "product": parsed["product"], "version": parsed["version"],
                         "tile": parsed["tile"], "date": parsed["date"], "platform": platform_by_granule[g],
                         "regions": ";".join(sorted(regions_by_granule[g])), **lookup(g)})
    return {"schema": MANIFEST_SCHEMA, "acquisition_count": len(acquisitions), "granule_count": len(granules),
            "grid_source": "authoritative image GeoTIFFs (transform/shape) and 500m sidecar source_granules",
            "url_source": "NASA CMR exact readable_granule_name lookup",
            "acquisitions": acquisitions, "granules": granules}


def load_acquisition_manifest(path: Path = DEFAULT_MANIFEST_PATH):
    """Load and validate the frozen manifest against RUN2_DOWNLOAD_PLAN.

    Returns (acquisitions: List[AcquisitionSpec], granules: List[GranuleRecord]).
    """
    from .modis_raw_hdf import _validate_record

    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if data.get("schema") != MANIFEST_SCHEMA:
        raise ValueError(f"{path}: unexpected schema {data.get('schema')!r}")
    records = [_validate_record(g) for g in data["granules"]]
    known = {r.granule_id for r in records}
    if len(known) != len(records):
        raise ValueError(f"{path}: duplicate granule records")

    acqs = []
    for a in data["acquisitions"]:
        missing = [g for g in a["granules"] if g not in known]
        if missing or not a["granules"]:
            raise ValueError(f"{path}: {a['region']}/{a['platform']}/{a['date']} granules not in manifest: {missing}")
        if set(a["grids"]) != set(GRID_KEYS):
            raise ValueError(f"{path}: {a['region']}/{a['platform']}/{a['date']} must have grids {GRID_KEYS}")
        grids = {k: (tuple(v["transform"]), tuple(v["shape"])) for k, v in a["grids"].items()}
        acqs.append(AcquisitionSpec(a["region"], a["platform"], a["date"], tuple(a["granules"]), grids))

    keys = [(a.region, a.platform, a.date) for a in acqs]
    if sorted(keys) != sorted(plan_acquisitions()):
        raise ValueError(f"{path}: acquisitions do not exactly match RUN2_DOWNLOAD_PLAN (218)")
    return acqs, records


def _main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Maintenance: (re)build the frozen MODIS acquisition manifest.")
    parser.add_argument("--build", action="store_true", required=True)
    parser.add_argument("--reference-root", type=Path, required=True, help="authoritative image root (read-only)")
    parser.add_argument("--output", type=Path, required=True, help="manifest JSON to write (must not exist)")
    args = parser.parse_args(argv)
    if args.output.exists():
        parser.error(f"refusing to overwrite {args.output}")
    data = build_manifest(args.reference_root)
    args.output.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    load_acquisition_manifest(args.output)
    print(f"wrote {args.output}: {data['acquisition_count']} acquisitions, {data['granule_count']} granules")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
