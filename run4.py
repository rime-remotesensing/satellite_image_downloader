"""GCOM-C/SGLI L2 LAND RSRF images: raw HDF5 recovery from JAXA G-Portal + native EQA generation.

Official SGLI entry point (MODIS = run2.py, VIIRS = run3.py; Active Fire moved to
run_activefire.py). Product: L2 LAND RSRF (atmospherically corrected land surface
reflectance) version 3002, descending (D01D, daytime) daily 10 deg EQA tiles. All
acquisition/validation/processing is the existing src/gcomc.py:

  * tiles from the exact EQA tile grid of each region (+halo) -- never a CSW bbox
    search (that misses T0529 near Aso); here T0528 + T0529
  * CSW record (datasetId 10002015) -> SFTP /product/Standard/... -> .part -> size ->
    HDF5 schema validation -> atomic rename; a valid local file is reused, never re-fetched
  * 250 m GeoTIFF: VN01-VN11, SW03 (12 bands); 1 km GeoTIFF: SW01, SW02, SW04 (3 bands),
    never upsampled; SW02 is TOA reflectance (band tag reflectance_type);
    float32 = DN x Slope + Offset, Error_DN / out-of-range DN -> NaN
  * QA_flag (uint16), Land_water_flag (uint8), Obs_time (int16) stay raw integers
  * native EQA grid (sinusoidal, R = 6371007.181 m), no reprojection / resampling

Every day of the period is processed (SGLI is not limited to MODIS/VIIRS dates);
each region-day is one of: OBSERVED / PARTIAL_OBSERVATION (with clear fraction),
AOI_NO_DATA (outside the swath that day), NO_RECORD (no CSW record),
RAW_MISSING (record not yet downloaded), REQUEST_REQUIRED / ERROR (acquisition).

Modes (exactly one; --dry-run is the default):
  --dry-run    raw HDF5 presence per date x tile (no network, no writes; --verify-raw validates)
  --download   CSW + SFTP for every date x tile not already valid locally (G-Portal key required)
  --reproduce  generate region-day products into --output-root from local raw files only
  --verify     check --output-root (bands, dtypes, SW02 TOA, QA, grid, raw provenance);
               --reference-root compares pixels with another output tree

Examples:
  python run4.py --raw-root <GCOMC_RAW_ROOT>
  python run4.py --download --raw-root <GCOMC_RAW_ROOT>
  python run4.py --reproduce --raw-root <GCOMC_RAW_ROOT> --output-root <new dir>
  python run4.py --verify --output-root <dir> --raw-root <GCOMC_RAW_ROOT>
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
import sys
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from src.regions import REGION_GEOJSONS

LOGGER = logging.getLogger("run4")

RAW_ROOT_ENV = "GCOMC_RAW_DIR"
OUTPUT_ROOT_ENV = "GCOMC_OUTPUT_ROOT"
REPORT_DIR = "_run4_reports"
PERIOD_START = "2024-02-01"
PERIOD_END = "2024-04-10"
# Same native context as the research VIIRS input (13 km); never AOI-polygon-masked.
DEFAULT_HALO_M = 13_000.0
# research min_clear_fraction (configs/sensors/viirs_pipeline*.json), used only to label region-days
USABLE_CLEAR_FRACTION = 0.5

D_NO_DATA = "AOI_NO_DATA"
D_NO_RECORD = "NO_RECORD"
D_RAW_MISSING = "RAW_MISSING"
D_RAW_INVALID = "RAW_INVALID"
D_ALREADY = "ALREADY_PRESENT"
D_PARTIAL_OUT = "PARTIAL_OUTPUT_PRESENT"
D_FAILED = "FAILED"
D_SPEC_OK = "SPEC_OK"
D_SPEC_BAD = "SPEC_MISMATCH"
D_NO_OUTPUT = "OUTPUT_MISSING"


def _dates(start: str, end: str) -> List[date]:
    a, b = date.fromisoformat(start), date.fromisoformat(end)
    return [a + timedelta(days=i) for i in range((b - a).days + 1)]


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%fZ")


def _is_within(child: Path, parent: Path) -> bool:
    try:
        child.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def region_geometry(region: str) -> Dict[str, Any]:
    from src.halo_context import load_geojson_geometry

    return load_geojson_geometry(Path(__file__).resolve().parent / REGION_GEOJSONS[region])


def region_tiles(region: str, halo_m: float) -> List[Tuple[int, int]]:
    """EQA tiles of the AOI bbox + halo (the same tile search src/gcomc.py does for native_rect_halo)."""
    from src import gcomc
    from src.geometry import _bbox_to_rectangle_geometry, _expand_bbox_by_meters

    geom = region_geometry(region)
    if halo_m > 0:
        geom = _bbox_to_rectangle_geometry(_expand_bbox_by_meters(gcomc.geometry_bounds(geom), halo_m))
    return gcomc.tiles_for_geometry(geom)


def granule_for(obs: date, tile: Tuple[int, int]):
    from src import gcomc

    v, h = tile
    return gcomc.parse_rsrf_identifier(
        f"GC1SG1_{obs:%Y%m%d}{gcomc.GCOMC_ORBIT_DIRECTION}01D_T{v:02d}{h:02d}_L2SG_RSRFQ_{gcomc.GCOMC_RSRF_PRODUCT_VERSION}")


def local_products(raw_root: Path, obs: date, tiles: Sequence[Tuple[int, int]], *, validate: bool):
    """{tile: ProductResult} from the local archive only (no network). Missing -> RAW_MISSING; invalid -> ERROR."""
    from src import gcomc

    out = {}
    for t in tiles:
        g = granule_for(obs, t)
        path = gcomc.local_raw_path(raw_root, g)
        p = gcomc.ProductResult(granule=g, status=D_RAW_MISSING, local_path=path)
        if path.exists():
            p.status, p.source, p.size_bytes = gcomc.PRODUCT_AVAILABLE, "local_cache", path.stat().st_size
            if validate:
                try:
                    p.metadata = gcomc.validate_rsrf_hdf5(path, g.filename)
                except gcomc.GCOMCSchemaError as exc:
                    p.status, p.detail = gcomc.PRODUCT_ERROR, f"local raw file failed validation (left untouched): {exc}"
        out[t] = p
    return out


def out_dir(output_root: Path, region: str, obs: date) -> Path:
    """Official layout GCOMC/<region>/<year>/surface_reflectance/{250m,1km,qa/<field>,summary}/."""
    return output_root / region / f"{obs:%Y}" / "surface_reflectance"


def expected_outputs(output_root: Path, region: str, obs: date) -> List[Path]:
    from src import gcomc

    d = out_dir(output_root, region, obs)
    return ([gcomc.reflectance_output_path(d, g, obs) for g in gcomc.GCOMC_REFLECTANCE_GROUPS]
            + [gcomc.output_path(d, s, obs) for s in gcomc.GCOMC_RAW_FIELDS])


def observation_label(aoi_status: str, clear_fraction: Optional[float]) -> str:
    if aoi_status == D_NO_DATA:
        return "NO_OBSERVATION"
    if clear_fraction is None:
        return "OBSERVED_UNRATED"
    return "USABLE" if clear_fraction >= USABLE_CLEAR_FRACTION else "CLOUDY"


def reproduce_region_day(region: str, obs: date, raw_root: Path, output_root: Path, halo_m: float) -> Dict[str, Any]:
    """One region-day from local raw files. All tiles of the region must be present and valid (no partial mosaic)."""
    from src import gcomc, gcomc_inventory
    from src.modis_raw_hdf import sha256_file

    row: Dict[str, Any] = {"region": region, "date": obs.isoformat()}
    products = local_products(raw_root, obs, region_tiles(region, halo_m), validate=True)
    row["source_granules"] = {p.granule.tile_id: {"file": p.granule.filename, "status": p.status, "local_path": str(p.local_path)}
                              for p in products.values()}
    if any(p.status == D_RAW_MISSING for p in products.values()):
        row["status"] = D_RAW_MISSING
        return row
    if any(p.status != gcomc.PRODUCT_AVAILABLE for p in products.values()):
        row.update(status=D_RAW_INVALID, detail={p.granule.tile_id: p.detail for p in products.values() if p.detail})
        return row
    sp = gcomc.summary_path(out_dir(output_root, region, obs), obs)
    if sp.exists():
        row["status"] = D_ALREADY
        return row
    if any(p.exists() for p in expected_outputs(output_root, region, obs)):
        row["status"] = D_PARTIAL_OUT
        return row
    geometry = region_geometry(region)
    day = gcomc.process_date(obs, products, geometry, out_dir(output_root, region, obs), clip_to_aoi=False, halo_m=halo_m)
    stats = gcomc_inventory.region_day_stats(obs, region, geometry, raw_root) if day["aoi_status"] != D_NO_DATA else {}
    day["observation"] = {"clear_fraction_250m": stats.get("clear_fraction_250m"),
                          "qa_cloud_fraction": stats.get("qa_cloud_fraction"),
                          "obs_time_utc_hour_range": [stats.get("obs_time_utc_hour_min"), stats.get("obs_time_utc_hour_max")],
                          "label": observation_label(day["aoi_status"], stats.get("clear_fraction_250m"))}
    for p in products.values():
        day["product_status"][p.granule.tile_id]["sha256"] = sha256_file(p.local_path)
    day["spatial_export"] = {"mode": "native_rect_halo", "halo_m": halo_m, "aoi_polygon_mask_applied": False}
    sp.parent.mkdir(parents=True, exist_ok=True)
    tmp = sp.with_name(sp.name + ".part")
    tmp.write_text(json.dumps(day, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, sp)
    row.update(status=day["aoi_status"], observation=day["observation"], files=day["files"])
    return row


def check_region_day(output_root: Path, region: str, obs: date, raw_root: Optional[Path]) -> List[str]:
    """Spec check of one region-day's GeoTIFFs against src/gcomc.py's contract."""
    import numpy as np
    import rasterio

    from src import gcomc

    summary = json.loads(gcomc.summary_path(out_dir(output_root, region, obs), obs).read_text(encoding="utf-8"))
    if summary["aoi_status"] == D_NO_DATA:
        return [f"{p.name} exists for an AOI_NO_DATA day" for p in expected_outputs(output_root, region, obs) if p.exists()]
    problems: List[str] = []
    windows = {}
    d = out_dir(output_root, region, obs)
    x0, y0 = gcomc._eqa_origin()
    for grid, specs in gcomc.GCOMC_REFLECTANCE_GROUPS.items():
        path = gcomc.reflectance_output_path(d, grid, obs)
        with rasterio.open(path) as ds:
            windows[grid] = (ds.transform, ds.shape)
            if ds.count != len(specs) or set(ds.dtypes) != {"float32"} or not math.isnan(ds.nodata):
                problems.append(f"{path.name}: {ds.count} x {ds.dtypes[0]} nodata {ds.nodata}")
            if list(ds.descriptions) != [s.name for s in specs]:
                problems.append(f"{path.name}: band order {ds.descriptions}")
            for i, s in enumerate(specs, start=1):
                t = ds.tags(i)
                if t.get("reflectance_type") != s.reflectance_type:
                    problems.append(f"{path.name} {s.name}: reflectance_type {t.get('reflectance_type')} != {s.reflectance_type}")
                if t.get("native_grid") != grid:
                    problems.append(f"{path.name} {s.name}: native_grid {t.get('native_grid')}")
            a = ds.read()
            finite = a[np.isfinite(a)]
            if finite.size and (finite.min() < 0 or finite.max() > 6.5535):
                problems.append(f"{path.name}: reflectance outside the valid DN range x Slope")
            tags = ds.tags()
            if tags.get("product_version") != gcomc.GCOMC_RSRF_PRODUCT_VERSION or tags.get("orbit_direction") != "D":
                problems.append(f"{path.name}: product_version/orbit tags {tags.get('product_version')}/{tags.get('orbit_direction')}")
            if raw_root is not None:
                for name in filter(None, tags.get("source_filenames", "").split(",")):
                    if not gcomc.local_raw_path(raw_root, gcomc.parse_rsrf_identifier(name)).exists():
                        problems.append(f"{path.name}: source {name} not in the raw archive")
            if ds.crs != gcomc.eqa_crs():
                problems.append(f"{path.name}: CRS {ds.crs.to_proj4()}")
            if abs(ds.transform.a - gcomc.eqa_pixel_size_m(grid)) > 1e-6:
                problems.append(f"{path.name}: pixel size {ds.transform.a}")
            for k in ((ds.transform.c - x0) / ds.transform.a, (y0 - ds.transform.f) / ds.transform.a):
                if abs(k - round(k)) > 1e-6:
                    problems.append(f"{path.name}: window not on the native EQA pixel lattice")
    for spec in gcomc.GCOMC_RAW_FIELDS:
        path = gcomc.output_path(d, spec, obs)
        with rasterio.open(path) as ds:
            if ds.dtypes[0] != spec.dtype or ds.count != 1:
                problems.append(f"{path.name}: dtype {ds.dtypes[0]} != {spec.dtype}")
            if (ds.transform, ds.shape) != windows["250m"]:
                problems.append(f"{path.name}: grid differs from the 250 m reflectance")
            if ds.tags().get("scale_offset_applied") != "False":
                problems.append(f"{path.name}: raw field must not be scaled")
    (t250, s250), (t1k, s1k) = windows["250m"], windows["1km"]
    if abs(t1k.a - 4 * t250.a) > 1e-6:
        problems.append("1 km pixel is not exactly 4 x 250 m")
    if not (t1k.c <= t250.c + 1e-6 and t1k.f >= t250.f - 1e-6
            and t1k.c + s1k[1] * t1k.a >= t250.c + s250[1] * t250.a - 1e-6
            and t1k.f - s1k[0] * t1k.a <= t250.f - s250[0] * t250.a + 1e-6):
        problems.append("1 km window does not contain the 250 m window")
    return problems


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="raw presence per date x tile (default)")
    mode.add_argument("--download", action="store_true", help="CSW + SFTP for every date x tile not valid locally")
    mode.add_argument("--reproduce", action="store_true", help="generate region-day products from local raw files")
    mode.add_argument("--verify", action="store_true", help="check --output-root")
    parser.add_argument("--raw-root", type=Path, default=None,
                        help=f"raw archive <root>/GCOM-C/SGLI/L2.LAND.RSRF/3002/YYYY/MM/DD/ (or env {RAW_ROOT_ENV})")
    parser.add_argument("--verify-raw", action="store_true", help="with --dry-run: open and validate every local HDF5")
    parser.add_argument("--output-root", type=Path, default=None, help=f"product root (or env {OUTPUT_ROOT_ENV})")
    parser.add_argument("--reference-root", type=Path, default=None, help="--verify: read-only output tree to compare pixels with")
    parser.add_argument("--halo-m", type=float, default=DEFAULT_HALO_M)
    parser.add_argument("--sftp-without-csw", action="store_true",
                        help="--download while the G-Portal CSW catalogue is unavailable: use the exact SFTP path of each "
                             "granule id (same validation; NO_RECORD cannot be detected)")
    parser.add_argument("--regions", nargs="+", choices=tuple(REGION_GEOJSONS))
    parser.add_argument("--start", default=PERIOD_START)
    parser.add_argument("--end", default=PERIOD_END)
    args = parser.parse_args(argv)
    if args.sftp_without_csw and not args.download:
        parser.error("--sftp-without-csw is only valid with --download")
    mode_name = "download" if args.download else "reproduce" if args.reproduce else "verify" if args.verify else "dry_run"
    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")

    from src import gcomc

    raw_root = args.raw_root or (Path(os.environ[RAW_ROOT_ENV]) if os.environ.get(RAW_ROOT_ENV) else None)
    output_root = args.output_root or (Path(os.environ[OUTPUT_ROOT_ENV]) if os.environ.get(OUTPUT_ROOT_ENV) else None)
    if raw_root is None and not args.verify:
        parser.error(f"--raw-root or {RAW_ROOT_ENV} is required; no default data root is assumed")
    if (args.reproduce or args.verify) and output_root is None:
        parser.error(f"--output-root or {OUTPUT_ROOT_ENV} is required for --{mode_name}")
    for other in filter(None, (raw_root, args.reference_root)):
        if output_root and (args.reproduce or args.verify) and (_is_within(output_root, other) or _is_within(other, output_root)):
            parser.error(f"output root must be separate from {other}")
    regions = list(args.regions or REGION_GEOJSONS)
    days = _dates(args.start, args.end)
    tiles_by_region = {r: region_tiles(r, args.halo_m) for r in regions}
    tiles = sorted({t for ts in tiles_by_region.values() for t in ts})
    tile_id = lambda t: f"T{t[0]:02d}{t[1]:02d}"  # noqa: E731
    report: Dict[str, Any] = {
        "mode": mode_name, "product": "GCOM-C/SGLI L2.LAND.RSRF", "version": gcomc.GCOMC_RSRF_PRODUCT_VERSION,
        "orbit_direction": gcomc.GCOMC_ORBIT_DIRECTION, "period": [args.start, args.end], "days": len(days),
        "tiles": [tile_id(t) for t in tiles], "tiles_by_region": {r: [tile_id(t) for t in ts] for r, ts in tiles_by_region.items()},
        "halo_m": args.halo_m, "raw_root": str(raw_root) if raw_root else None,
        "output_root": str(output_root) if output_root else None, "network_used": bool(args.download),
    }
    rows: List[Dict[str, Any]] = []

    if args.download:
        from src.gcomc import GPortalSFTP, _gportal_username, query_csw_records, select_daily_records

        today = datetime.now(timezone.utc).date()
        session: Dict[str, Any] = {}

        def factory():
            if "sftp" not in session:
                session["sftp"] = GPortalSFTP(_gportal_username({}, Path.cwd()))
            return session["sftp"]

        try:
            if args.sftp_without_csw:
                # CSW unavailable: locate each granule at its exact SFTP path (fixed by the granule id); the
                # SFTP state then decides AVAILABLE / REQUEST_REQUIRED / ERROR exactly as with a CSW record.
                # NO_RECORD cannot be determined this way and is never reported.
                selected = {(d, t): gcomc.CSWRecord(granule_for(d, t), "product.gportal.jaxa.jp:/products/Standard/"
                                                    + gcomc.expected_sftp_path(granule_for(d, t)).split("/Standard/", 1)[1],
                                                    None, "", "", None) for d in days for t in tiles}
                report["catalog"] = "CSW not used (--sftp-without-csw): exact SFTP path derived from the granule id"
            else:
                selected = select_daily_records([r for v, h in tiles for r in query_csw_records(days[0], days[-1], v, h)], tiles)
                report["catalog"] = "CSW records (datasetId " + gcomc.GCOMC_RSRF_DATASET_ID + ")"
            for d in days:
                for t in tiles:
                    g = granule_for(d, t)
                    row = {"date": d.isoformat(), "tile": g.tile_id, "file": g.filename}
                    rows.append(row)
                    rec = selected.get((d, t))
                    if rec is None:
                        row["status"] = D_NO_RECORD
                        continue
                    try:
                        row.update(gcomc.acquire_product(rec, raw_root, factory, today).to_dict())
                    except gcomc.GCOMCAuthError:
                        raise
                    except Exception as exc:
                        row.update(status=gcomc.PRODUCT_ERROR, detail=str(exc))
        finally:
            if "sftp" in session:
                session["sftp"].close()
    elif args.reproduce:
        for region in regions:
            for d in days:
                try:
                    rows.append(reproduce_region_day(region, d, raw_root, output_root, args.halo_m))
                except Exception as exc:
                    LOGGER.error("%s %s: %s", region, d, exc, exc_info=True)
                    rows.append({"region": region, "date": d.isoformat(), "status": D_FAILED, "error": str(exc)})
    elif args.verify:
        from src.raster_compare import compare_aligned

        for region in regions:
            for d in days:
                row: Dict[str, Any] = {"region": region, "date": d.isoformat()}
                rows.append(row)
                if not gcomc.summary_path(out_dir(output_root, region, d), d).exists():
                    row["status"] = D_NO_OUTPUT
                    continue
                row["problems"] = check_region_day(output_root, region, d, raw_root)
                row["status"] = D_SPEC_OK if not row["problems"] else D_SPEC_BAD
                if args.reference_root:
                    pairs = [(p, args.reference_root / p.relative_to(output_root)) for p in expected_outputs(output_root, region, d)]
                    cmp = {p.name: compare_aligned(p, r) for p, r in pairs if p.exists() and r.exists()}
                    row["reference"] = {"files": len(cmp), "identical": sum(c["identical"] for c in cmp.values())}
    else:
        for d in days:
            for p in local_products(raw_root, d, tiles, validate=args.verify_raw).values():
                status = p.status if p.status != gcomc.PRODUCT_AVAILABLE else "PRESENT_VALID" if args.verify_raw else "PRESENT"
                rows.append({"date": d.isoformat(), "tile": p.granule.tile_id, "file": p.granule.filename,
                             "status": status, "detail": p.detail})

    report["status_counts"] = dict(Counter(r["status"] for r in rows))
    if args.reproduce:
        report["observation_labels"] = dict(Counter(r["observation"]["label"] for r in rows if r.get("observation")))
    if args.verify and args.reference_root:
        refd = [r["reference"] for r in rows if "reference" in r]
        report["reference_compare"] = {k: sum(r[k] for r in refd) for k in ("files", "identical")}
    report["rows"] = rows
    target = raw_root if args.download else output_root if (args.reproduce or args.verify) else None
    if target is not None:
        path = target / REPORT_DIR / f"run4_{mode_name}_{_stamp()}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        report["report"] = str(path)
    print(json.dumps({k: v for k, v in report.items() if k != "rows"}, ensure_ascii=False, indent=2, default=str))
    bad = {D_FAILED, D_RAW_INVALID, D_PARTIAL_OUT, D_SPEC_BAD, gcomc.PRODUCT_ERROR}
    return 1 if any(r["status"] in bad for r in rows) else 0


if __name__ == "__main__":
    sys.exit(main())
