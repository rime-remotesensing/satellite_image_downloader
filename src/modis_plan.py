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

The external TEST cases follow the same two layers, kept separate from the
2024 plan: ``RUN2_TEST_CASES`` / ``RUN2_TEST_PLAN`` (6 acquisitions, region05
2023/2025 + hitoyoshi 2025) and config/modis_run2_test_acquisitions.json
(built with ``--scope test`` from the sidecars of the existing TEST images).
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


def plan_acquisitions(regions: Optional[Sequence[str]] = None, scope: str = "train_dev") -> List[Tuple[str, str, str]]:
    """(region, platform, YYYYMMDD) in a fixed order; scope "train_dev" (218) or "test" (6)."""
    plan = RUN2_TEST_PLAN if scope == SCOPE_TEST else RUN2_DOWNLOAD_PLAN
    validate_test_plan() if scope == SCOPE_TEST else validate_download_plan()
    out = []
    for region in sorted(plan):
        if regions and region not in regions:
            continue
        for platform in ("aqua", "terra"):
            for d in plan[region][platform]:
                out.append((region, platform, d.replace("-", "")))
    return out


# ---------------------------------------------------------------------------
# External TEST (never used for training or development)
# ---------------------------------------------------------------------------

SCOPE_TRAIN_DEV = "train_dev"
SCOPE_TEST = "test"
SCOPES = (SCOPE_TRAIN_DEV, SCOPE_TEST)

TEST_REGION_GEOJSONS: Dict[str, str] = {
    "region05": "config/no5.geojson",
    "hitoyoshi": "config/hitoyoshi.geojson",
}

# The three previously evaluated external TEST cases (case, region, platform, PRE, POST), verbatim from the
# research's authoritative pairing (results/final_trainval_retraining/modis_native_residual_experiment/
# evaluation/external_eval.py CASES; asserted again by modis_burning/scripts/g5_external_contract_preflight.py)
# and identical to the existing images Aso/MODIS/{region05/2023,region05/2025,hitoyoshi/2025}. Not inferred.
RUN2_TEST_CASES: Tuple[Tuple[str, str, str, str, str], ...] = (
    ("region05_2023", "region05", "terra", "2023-01-11", "2023-03-08"),
    ("region05_2025", "region05", "aqua", "2025-02-14", "2025-03-22"),
    ("hitoyoshi_2025", "hitoyoshi", "terra", "2025-02-25", "2025-03-20"),
)

# Do not derive this from a range: the PRE/POST dates of RUN2_TEST_CASES, in full (6 acquisitions).
RUN2_TEST_PLAN: Dict[str, Dict[str, List[str]]] = {
    "hitoyoshi": {
        "aqua": [],
        "terra": ["2025-02-25", "2025-03-20"],
    },
    "region05": {
        "aqua": ["2025-02-14", "2025-03-22"],
        "terra": ["2023-01-11", "2023-03-08"],
    },
}

TEST_MANIFEST_PATH = Path(__file__).resolve().parents[1] / "config" / "modis_run2_test_acquisitions.json"


def validate_test_plan(plan: Mapping[str, Mapping[str, Sequence[str]]] = RUN2_TEST_PLAN) -> Dict[str, int]:
    """The TEST plan must be exactly the PRE/POST dates of RUN2_TEST_CASES (aqua 2 + terra 4)."""
    if set(plan) != set(TEST_REGION_GEOJSONS):
        raise ValueError("RUN2_TEST_PLAN regions must exactly match TEST_REGION_GEOJSONS")
    from_cases: Dict[Tuple[str, str], set] = {}
    for _, region, platform, pre, post in RUN2_TEST_CASES:
        from_cases.setdefault((region, platform), set()).update({pre, post})
    totals = {"aqua": 0, "terra": 0}
    for region, platforms in plan.items():
        if set(platforms) != set(totals):
            raise ValueError(f"{region}: test plan must contain exactly aqua and terra")
        for platform, values in platforms.items():
            parsed = [parse_plan_date(v) for v in values]
            if len(parsed) != len(set(parsed)):
                raise ValueError(f"{region}/{platform}: duplicate date in test plan")
            if set(values) != from_cases.get((region, platform), set()):
                raise ValueError(f"{region}/{platform}: test plan differs from RUN2_TEST_CASES")
            totals[platform] += len(parsed)
    if totals != {"aqua": 2, "terra": 4}:
        raise AssertionError(f"Unexpected RUN2 TEST totals: {totals}")
    return totals


def region_geojson(region: str) -> str:
    """AOI GeoJSON of a TRAIN/DEV or TEST region (region05 is the same file in both)."""
    return {**REGION_GEOJSONS, **TEST_REGION_GEOJSONS}[region]


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


def build_manifest(reference_root: Path, *, lookup=cmr_exact_lookup, scope: str = "train_dev") -> Dict[str, Any]:
    """Freeze granules + native grids of the authoritative images of one scope (read-only).

    train_dev: the 218 official 2024 images (patch-support grids); test: the 6 existing external TEST
    images (their AOI-clipped bbox grids), whose 500m sidecars record the exact source granules."""
    import rasterio

    from .modis_native_window import Acquisition, output_paths, reference_source_granules
    from .modis_raw_hdf import parse_granule_id

    acquisitions = []
    regions_by_granule: Dict[str, set] = {}
    platform_by_granule: Dict[str, str] = {}
    for region, platform, d in plan_acquisitions(scope=scope):
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


def load_acquisition_manifest(path: Optional[Path] = None, scope: str = "train_dev"):
    """Load and validate the frozen manifest against RUN2_DOWNLOAD_PLAN (or RUN2_TEST_PLAN for scope="test").

    Returns (acquisitions: List[AcquisitionSpec], granules: List[GranuleRecord]).
    """
    from .modis_raw_hdf import _validate_record

    if path is None:
        path = TEST_MANIFEST_PATH if scope == SCOPE_TEST else DEFAULT_MANIFEST_PATH
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
    if sorted(keys) != sorted(plan_acquisitions(scope=scope)):
        plan_name = "RUN2_TEST_PLAN (6)" if scope == SCOPE_TEST else "RUN2_DOWNLOAD_PLAN (218)"
        raise ValueError(f"{path}: acquisitions do not exactly match {plan_name}")
    return acqs, records


def _main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Maintenance: (re)build the frozen MODIS acquisition manifest.")
    parser.add_argument("--build", action="store_true", required=True)
    parser.add_argument("--reference-root", type=Path, required=True, help="authoritative image root (read-only)")
    parser.add_argument("--output", type=Path, required=True, help="manifest JSON to write (must not exist)")
    parser.add_argument("--scope", choices=SCOPES, default=SCOPE_TRAIN_DEV)
    args = parser.parse_args(argv)
    if args.output.exists():
        parser.error(f"refusing to overwrite {args.output}")
    data = build_manifest(args.reference_root, scope=args.scope)
    args.output.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    load_acquisition_manifest(args.output, scope=args.scope)
    print(f"wrote {args.output}: {data['acquisition_count']} acquisitions, {data['granule_count']} granules")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
