"""MODIS official images: raw HDF recovery + native-window generation (AOI + halo).

This is the official entry point for the MODIS MOD09GA/MYD09GA .061 images
used by the burned-area deep-learning research (218 acquisitions = Aqua 111
+ Terra 107, 7 regions). Everything it needs is in the repository:

* src/modis_plan.py                          RUN2_DOWNLOAD_PLAN (218 literal acquisitions)
* config/modis_run2_acquisitions.json        per acquisition: exact source granules; per granule:
                                             exact LP DAAC URL (frozen, validated against the plan)
* config/modis_run2_full_scene_manifest.json the official image windows (AOI + halo; adopted
                                             2026-10-27, SHA-256 9510...a861)

Official extent (default): AOI bbox + halo (10 km, region01 13 km) snapped outward to the native
500 m grid -- one fixed window per region, independent of date, platform and training patches.
The earlier patch-support windows (union of the TRAIN/DEV 32 x 32 patches using each file; the grids
stored in config/modis_run2_acquisitions.json) are reproducible only with --legacy-patch-extent.

Modes (exactly one; --dry-run is the default):

  --dry-run    Classify all 88 raw HDFs (present / present elsewhere / missing /
               invalid) and the 218 acquisitions (ready / blocked). No network,
               no writes.
  --download   Download only MISSING raw HDFs, from their exact manifest URL,
               into <raw-root>/recovered_modis_061/ (.part -> HDF4 verify ->
               atomic rename). Never overwrites, never deletes. The only mode
               that uses the network.
  --reproduce  Generate the 500m / QC_500m / state_1km GeoTIFFs of every ready
               acquisition into --output-root on the official windows, and record
               them in <output-root>/full_scene_manifest.json. Existing outputs are
               left alone. Raw HDFs are never deleted.
  --verify     Check --output-root: spec (grid, dtype, bands, nodata, granules), AOI and
               halo coverage, one fixed grid per region; with --reference-root, pixel and
               SHA-256 identity with the official images; with --legacy-reference-root,
               that each legacy patch-extent image lies inside and matches over its whole
               extent (matched by transform / global pixel position, not by image corner).

Every mode except --dry-run writes a timestamped JSON report (granule <->
raw HDF <-> image correspondence, SHA-256, statuses) under
<raw-root>/recovered_modis_061/_run2_reports/ (download) or
<output-root>/_run2_reports/ (reproduce, verify).

Images are written to <output-root>/<region>/<YYYY>/surface_reflectance/<platform>/{500m,qa}/,
the layout of the official images (Aso/MODIS/<region>/2024/surface_reflectance/).

Examples:
  python run2.py --raw-root <RAW_HDF_ROOT> [--existing-root <dir>]...
  python run2.py --download --raw-root <RAW_HDF_ROOT>
  python run2.py --reproduce --raw-root <RAW_HDF_ROOT> --output-root <new dir>
  python run2.py --verify --output-root <dir> --reference-root <Aso/MODIS> [--legacy-reference-root <old dir>]
  python run2.py --reproduce --legacy-patch-extent --raw-root <RAW_HDF_ROOT> --output-root <new dir>
  python run2.py --verify --legacy-patch-extent --output-root <dir> [--reference-root <old patch images>]
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

LOGGER = logging.getLogger("run2")

RAW_ROOT_ENV = "MODIS_RAW_HDF_ROOT"
OUTPUT_ROOT_ENV = "MODIS_OUTPUT_ROOT"
REPORT_DIR = "_run2_reports"

ACQ_READY = "READY"
ACQ_BLOCKED = "BLOCKED_MISSING_HDF"
ACQ_GENERATED = "GENERATED"
ACQ_ALREADY = "ALREADY_PRESENT"
ACQ_PARTIAL = "PARTIAL_OUTPUT_PRESENT"
ACQ_FAILED = "FAILED"
ACQ_IDENTICAL = "IDENTICAL"
ACQ_DIFFERS = "DIFFERS"
ACQ_SPEC_OK = "SPEC_OK"
ACQ_SPEC_BAD = "SPEC_MISMATCH"
ACQ_NO_OUTPUT = "OUTPUT_MISSING"


def _is_within(child: Path, parent: Path) -> bool:
    try:
        child.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _write_report(directory: Path, mode: str, report: Dict[str, Any]) -> Path:
    path = directory / REPORT_DIR / f"run2_{mode}_{_stamp()}.json"
    if path.exists():
        raise FileExistsError(f"report already exists: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return path


def _session_factory(config_dir: Path):
    def make():
        import earthaccess

        from src.surface_reflectance import _earthdata_login

        _earthdata_login({}, config_dir)
        return earthaccess.get_requests_https_session()

    return make


def _check_research_manifest(path: Path, records) -> List[str]:
    """A research-side manifest may only restrict scope; every record must equal ours."""
    from src.modis_raw_hdf import load_manifest

    ours = {r.granule_id: r for r in records}
    theirs = load_manifest(path)
    bad = [r.granule_id for r in theirs if ours.get(r.granule_id) != r]
    if bad:
        raise SystemExit(f"{path}: records differ from the frozen run2 manifest: {bad}")
    return [r.granule_id for r in theirs]


def _hdf_paths_by_granule(statuses) -> Dict[str, Path]:
    from src.modis_raw_hdf import STATUS_DOWNLOADED_VALID, STATUS_PRESENT_ELSEWHERE, STATUS_PRESENT_VALID

    usable = {STATUS_PRESENT_VALID, STATUS_PRESENT_ELSEWHERE, STATUS_DOWNLOADED_VALID}
    return {s.granule_id: Path(s.detail["path"]) for s in statuses if s.status in usable}


def _copy_existing(statuses, records, raw_root: Path, subdir: str) -> None:
    """Copy (never move) valid HDFs found under --existing-root into the raw root."""
    from src.modis_raw_hdf import STATUS_PRESENT_ELSEWHERE, STATUS_PRESENT_VALID, verify_hdf

    by_id = {r.granule_id: r for r in records}
    for st in statuses:
        if st.status != STATUS_PRESENT_ELSEWHERE:
            continue
        dest = Path(st.destination)
        if dest.exists():
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        part = dest.with_name(dest.name + ".part")
        shutil.copy2(st.detail["path"], part)
        v = verify_hdf(part, by_id[st.granule_id], check_filename=False)
        if not v["ok"] or v["sha256"] != st.detail["sha256"]:
            part.unlink(missing_ok=True)
            raise RuntimeError(f"copy of {st.granule_id} failed verification: {v['problems']}")
        os.replace(part, dest)
        v["path"] = str(dest)
        v["copied_from"] = st.detail["path"]
        st.status, st.detail = STATUS_PRESENT_VALID, v


def _acquisition_rows(acqs, hdf_paths: Dict[str, Path], url_by_granule: Dict[str, str]) -> List[Dict[str, Any]]:
    rows = []
    for a in acqs:
        missing = [g for g in a.granules if g not in hdf_paths]
        rows.append({
            "region": a.region, "platform": a.platform, "date": a.date,
            "status": ACQ_BLOCKED if missing else ACQ_READY,
            "source_granules": [{"granule_id": g, "https_url": url_by_granule[g],
                                 "hdf_path": str(hdf_paths[g]) if g in hdf_paths else None} for g in a.granules],
            "missing_hdf": missing,
        })
    return rows


def _sha256(path: Path) -> str:
    from src.modis_raw_hdf import sha256_file

    return sha256_file(path)


def _reproduce(acqs, rows, hdf_paths, output_root: Path, extra_tags: Optional[Dict[tuple, Dict[str, Any]]] = None) -> None:
    from src.modis_native_window import Acquisition, generate_acquisition, output_paths
    from rasterio.transform import Affine

    for a, row in zip(acqs, rows):
        acq = Acquisition(a.region, a.platform, a.date)
        paths = output_paths(output_root, acq)
        present = [p.exists() for p in paths.values()]
        if all(present):
            row["status"] = ACQ_ALREADY
        elif any(present):
            row["status"] = ACQ_PARTIAL
            row["error"] = "some outputs exist; not overwriting -- inspect and remove the partial set manually"
            continue
        elif row["status"] == ACQ_BLOCKED:
            continue
        else:
            grids = {k: (Affine(*t), s) for k, (t, s) in a.grids.items()}
            try:
                tags = {"source_granule_urls": ",".join(s["https_url"] for s in row["source_granules"])}
                tags.update((extra_tags or {}).get((a.region, a.platform, a.date), {}))
                generate_acquisition(acq, [hdf_paths[g] for g in a.granules], grids, output_root, extra_tags=tags)
                row["status"] = ACQ_GENERATED
            except Exception as exc:
                LOGGER.error("%s %s %s: %s", a.region, a.platform, a.date, exc)
                row.update(status=ACQ_FAILED, error=str(exc))
                continue
        row["outputs"] = {k: {"path": str(p), "sha256": _sha256(p)} for k, p in paths.items()}


def check_output_spec(spec, paths: Dict[str, Path]) -> List[str]:
    """Frozen-spec check of one acquisition's 3 files (no reference images needed)."""
    import math

    import rasterio

    expected = {"sr": (7, "float32", "nan"), "qc_500m": (1, "uint32", None), "state_1km": (1, "uint16", None)}
    problems = []
    for key, path in paths.items():
        count, dtype, nodata = expected[key]
        transform, shape = spec.grids[key]
        with rasterio.open(path) as ds:
            if ds.count != count or set(ds.dtypes) != {dtype}:
                problems.append(f"{key}: {ds.count} bands {ds.dtypes}, expected {count} x {dtype}")
            if (ds.height, ds.width) != tuple(shape):
                problems.append(f"{key}: shape {(ds.height, ds.width)} != {tuple(shape)}")
            if any(abs(x - y) > 1e-6 for x, y in zip(tuple(ds.transform)[:6], transform)):
                problems.append(f"{key}: transform {tuple(ds.transform)[:6]} != {transform}")
            if "+proj=sinu" not in ds.crs.to_proj4():
                problems.append(f"{key}: CRS is not MODIS Sinusoidal")
            ok_nodata = (ds.nodata is None) if nodata is None else (ds.nodata is not None and math.isnan(ds.nodata))
            if not ok_nodata:
                problems.append(f"{key}: nodata {ds.nodata!r}, expected {nodata!r}")
            if key == "sr":
                tagged = sorted(g for g in ds.tags().get("source_granules", "").split(",") if g)
                if tagged and tagged != sorted(spec.granules):
                    problems.append(f"sr: source_granules tag {tagged} != {sorted(spec.granules)}")
    return problems


def _verify(acqs, output_root: Path, reference_root: Optional[Path]) -> List[Dict[str, Any]]:
    from src.modis_native_window import Acquisition, compare_files, output_paths

    rows = []
    for a in acqs:
        acq = Acquisition(a.region, a.platform, a.date)
        paths = output_paths(output_root, acq)
        row: Dict[str, Any] = {"region": a.region, "platform": a.platform, "date": a.date,
                               "source_granules": list(a.granules)}
        if not all(p.exists() for p in paths.values()):
            row["status"] = ACQ_NO_OUTPUT
            rows.append(row)
            continue
        row["outputs"] = {k: {"path": str(p), "sha256": _sha256(p)} for k, p in paths.items()}
        row["spec_problems"] = check_output_spec(a, paths)
        row["status"] = ACQ_SPEC_OK if not row["spec_problems"] else ACQ_SPEC_BAD
        if reference_root is not None and not row["spec_problems"]:
            ref = output_paths(reference_root, acq)
            if all(p.exists() for p in ref.values()):
                cmp = {k: compare_files(paths[k], ref[k]) for k in paths}
                row["comparison"] = cmp
                row["status"] = ACQ_IDENTICAL if all(c["identical"] for c in cmp.values()) else ACQ_DIFFERS
            else:
                row["reference"] = "missing"
        rows.append(row)
    return rows


def _verify_full_scene(acqs, records, output_root: Path, reference_root: Optional[Path],
                       legacy_root: Optional[Path] = None) -> List[Dict[str, Any]]:
    """Official (AOI + halo) outputs: spec on the frozen grids, AOI + halo coverage; with reference_root,
    pixel and SHA-256 identity with the official images; with legacy_root, the legacy patch-extent image
    lies inside and matches over its whole extent (overlap located by transform)."""
    import rasterio

    from src.halo_context import HALO_PIXELS
    from src.modis_full_scene import contains, file_sha256, margins, window_from_grid
    from src.modis_native_window import Acquisition, compare_files, output_paths
    from src.raster_compare import compare_aligned

    rows = []
    for a, rec in zip(acqs, records):
        acq = Acquisition(a.region, a.platform, a.date)
        paths = output_paths(output_root, acq)
        row: Dict[str, Any] = {"region": a.region, "platform": a.platform, "date": a.date, "source_granules": list(a.granules)}
        rows.append(row)
        if not all(p.exists() for p in paths.values()):
            row["status"] = ACQ_NO_OUTPUT
            continue
        row["outputs"] = {k: {"path": str(p), "sha256": _sha256(p)} for k, p in paths.items()}
        problems = check_output_spec(a, paths)
        with rasterio.open(paths["sr"]) as ds:
            img = window_from_grid(tuple(ds.transform)[:6], ds.shape, "500m")
        row["grid_500m"] = list(img)
        aoi = tuple(rec["aoi_window"]["global_pixel_window"].values())
        row["aoi_contained"] = contains(img, aoi)
        row["halo_achieved_px"] = margins(img, aoi)
        if not row["aoi_contained"]:
            problems.append("image does not contain the AOI window")
        if min(row["halo_achieved_px"].values()) < HALO_PIXELS:
            problems.append(f"halo {row['halo_achieved_px']} < {HALO_PIXELS} px on some side")
        if reference_root is not None:
            ref = output_paths(reference_root, acq)
            cmp = {}
            for k in paths:
                if not ref[k].exists():
                    cmp[k] = {"identical": False, "reference": "missing"}
                    continue
                c = compare_files(paths[k], ref[k])
                c["sha256_equal"] = row["outputs"][k]["sha256"] == file_sha256(ref[k])
                side, ref_side = paths[k].with_suffix(".json"), ref[k].with_suffix(".json")
                c["sidecar_sha256_equal"] = side.exists() and ref_side.exists() and file_sha256(side) == file_sha256(ref_side)
                cmp[k] = c
            row["official_comparison"] = cmp
            if not all(c["identical"] for c in cmp.values()):
                problems.append("differs from the official image")
        if legacy_root is not None:
            old = output_paths(legacy_root, acq)
            cmp = {}
            for k in paths:
                if not old[k].exists():
                    cmp[k] = {"identical": False, "reference": "missing"}
                    continue
                c = compare_aligned(paths[k], old[k])
                with rasterio.open(old[k]) as r:
                    c["legacy_fully_inside"] = c.get("overlap_pixels") == r.width * r.height
                c["identical"] = bool(c["identical"] and c["legacy_fully_inside"])
                cmp[k] = c
            row["legacy_comparison"] = cmp
            if not all(c["identical"] for c in cmp.values()):
                problems.append("legacy patch-extent image is not contained / differs on the overlap")
        row["spec_problems"] = problems
        row["status"] = ACQ_SPEC_BAD if problems else ACQ_IDENTICAL if reference_root is not None else ACQ_SPEC_OK
    # one fixed grid per region, independent of date and platform
    grids: Dict[str, set] = {}
    for r in rows:
        if "grid_500m" in r:
            grids.setdefault(r["region"], set()).add(tuple(r["grid_500m"]))
    for r in rows:
        if len(grids.get(r["region"], ())) > 1:
            r["spec_problems"].append("region grid differs between acquisitions")
            r["status"] = ACQ_SPEC_BAD
    return rows


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="classify raw HDFs and acquisitions (default)")
    mode.add_argument("--download", action="store_true", help="download only MISSING raw HDFs by exact URL")
    mode.add_argument("--reproduce", action="store_true", help="generate images of every ready acquisition")
    mode.add_argument("--verify", action="store_true", help="check --output-root (and compare to --reference-root)")
    parser.add_argument("--raw-root", type=Path, default=None,
                        help=f"raw HDF root (or env {RAW_ROOT_ENV}); HDFs live in <raw-root>/<subdir>/")
    parser.add_argument("--subdir", default=None, help="raw HDF subdirectory (default: recovered_modis_061)")
    parser.add_argument("--existing-root", type=Path, action="append", default=[],
                        help="extra read-only directory holding already-downloaded HDFs; repeatable")
    parser.add_argument("--copy-existing", action="store_true",
                        help="with --download: also copy (not move) valid --existing-root HDFs into the raw root")
    parser.add_argument("--output-root", type=Path, default=None, help=f"image output root (or env {OUTPUT_ROOT_ENV})")
    parser.add_argument("--reference-root", type=Path, default=None, help="read-only reference images for --verify")
    parser.add_argument("--manifest", type=Path, default=None, help="frozen acquisition manifest (default: config/)")
    parser.add_argument("--research-manifest", type=Path, default=None,
                        help="research MISSING_HDF_DOWNLOAD_MANIFEST.csv: restrict raw HDF scope to it (must agree)")
    parser.add_argument("--regions", nargs="+", default=None)
    parser.add_argument("--legacy-patch-extent", action="store_true",
                        help="legacy windows: the training-patch support grids of config/modis_run2_acquisitions.json "
                             "(the official images before 2026-10-27)")
    parser.add_argument("--legacy-reference-root", type=Path, default=None,
                        help="--verify (official extent): read-only legacy patch-extent images that must lie inside "
                             "the new images and match over their whole extent")
    parser.add_argument("--halo-m", type=float, default=None,
                        help="non-official experiment: recompute AOI + halo windows with this halo instead of the frozen "
                             "official windows (default halo 10000 m, region01 13000 m)")
    args = parser.parse_args(argv)
    mode_name = "download" if args.download else "reproduce" if args.reproduce else "verify" if args.verify else "dry_run"

    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")

    from src.modis_plan import DEFAULT_MANIFEST_PATH, load_acquisition_manifest, validate_download_plan
    from src.modis_raw_hdf import DEFAULT_RECOVERED_SUBDIR, download_missing, plan_recovery, summarize

    totals = validate_download_plan()
    acqs, records = load_acquisition_manifest(args.manifest or DEFAULT_MANIFEST_PATH)
    if args.regions:
        unknown = set(args.regions) - {a.region for a in acqs}
        if unknown:
            parser.error(f"unknown regions: {sorted(unknown)}")
        acqs = [a for a in acqs if a.region in args.regions]
    url_by_granule = {r.granule_id: r.https_url for r in records}

    raw_root = args.raw_root or (Path(os.environ[RAW_ROOT_ENV]) if os.environ.get(RAW_ROOT_ENV) else None)
    output_root = args.output_root or (Path(os.environ[OUTPUT_ROOT_ENV]) if os.environ.get(OUTPUT_ROOT_ENV) else None)
    subdir = args.subdir or DEFAULT_RECOVERED_SUBDIR
    if raw_root is None and not args.verify:
        parser.error(f"--raw-root or {RAW_ROOT_ENV} is required (no default data root is assumed)")
    if (args.reproduce or args.verify) and output_root is None:
        parser.error(f"--output-root or {OUTPUT_ROOT_ENV} is required for --{mode_name}")
    if args.reference_root and output_root and (
            _is_within(output_root, args.reference_root) or _is_within(args.reference_root, output_root)):
        parser.error("output root must be completely separate from the reference root")
    if args.copy_existing and not args.download:
        parser.error("--copy-existing is only valid with --download")
    if args.legacy_patch_extent and (args.halo_m is not None or args.legacy_reference_root):
        parser.error("--halo-m / --legacy-reference-root apply to the official AOI + halo extent, not --legacy-patch-extent")
    for root in filter(None, [args.legacy_reference_root]):
        if output_root and (_is_within(output_root, root) or _is_within(root, output_root)):
            parser.error("output root must be completely separate from the legacy reference root")
    fs_records = None
    if not args.legacy_patch_extent:
        from src.modis_full_scene import full_scene_spec, load_frozen_full_scene, tags_for

        if args.halo_m is None:
            acqs, fs_records = load_frozen_full_scene(acqs)
        else:
            pairs = [full_scene_spec(a, args.halo_m) for a in acqs]
            acqs, fs_records = [p[0] for p in pairs], [p[1] for p in pairs]

    report: Dict[str, Any] = {
        "mode": mode_name, "plan_totals": totals, "plan_acquisitions": sum(totals.values()),
        "acquisitions_in_scope": len(acqs), "regions": args.regions or "all",
        "raw_root": str(raw_root) if raw_root else None, "raw_hdf_dir": str(raw_root / subdir) if raw_root else None,
        "existing_roots": [str(p) for p in args.existing_root],
        "output_root": str(output_root) if output_root else None,
        "reference_root": str(args.reference_root) if args.reference_root else None,
        "network_used": bool(args.download),
        "extent": ("legacy_training_patch_support" if args.legacy_patch_extent
                   else "full_scene_aoi_halo" + ("" if args.halo_m is None else f" (non-official halo {args.halo_m:g} m)")),
    }

    if args.verify:
        rows = (_verify(acqs, output_root, args.reference_root) if args.legacy_patch_extent
                else _verify_full_scene(acqs, fs_records, output_root, args.reference_root, args.legacy_reference_root))
        report["acquisition_status_counts"] = dict(Counter(r["status"] for r in rows))
        report["acquisitions"] = rows
        path = _write_report(output_root, mode_name, report)
        print(json.dumps({k: v for k, v in report.items() if k != "acquisitions"}, ensure_ascii=False, indent=2))
        print("report:", path)
        bad = {ACQ_SPEC_BAD, ACQ_DIFFERS}
        return 1 if any(r["status"] in bad for r in rows) else 0

    needed = {g for a in acqs for g in a.granules}
    scope = [r for r in records if r.granule_id in needed]
    statuses = plan_recovery(scope, raw_root, subdir=subdir, existing_roots=args.existing_root)
    # A research manifest only narrows what --download may fetch; readiness is always judged on all HDFs.
    targets = statuses
    if args.research_manifest:
        wanted = set(_check_research_manifest(args.research_manifest, records))
        targets = [s for s in statuses if s.granule_id in wanted]
        report["research_manifest"] = {"path": str(args.research_manifest), "granules": len(wanted),
                                       "counts": dict(Counter(s.status for s in targets))}
    if args.download:
        download_missing(targets, scope, session_factory=_session_factory(Path.cwd()))
        if args.copy_existing:
            _copy_existing(statuses, scope, raw_root, subdir)
    hdf_summary = summarize(statuses)
    report["raw_hdf"] = {k: v for k, v in hdf_summary.items() if k != "records"}

    hdf_paths = _hdf_paths_by_granule(statuses)
    rows = _acquisition_rows(acqs, hdf_paths, url_by_granule)
    if args.reproduce:
        output_root.mkdir(parents=True, exist_ok=True)
        if fs_records is not None:
            from src.modis_full_scene import write_or_check_manifest

            report["full_scene_manifest"] = str(write_or_check_manifest(output_root, fs_records))
            _reproduce(acqs, rows, hdf_paths, output_root,
                       {(r["region"], r["platform"], r["date"]): tags_for(r) for r in fs_records})
        else:
            _reproduce(acqs, rows, hdf_paths, output_root)
    if fs_records is not None:
        report["full_scene_windows"] = fs_records
    report["acquisition_status_counts"] = dict(Counter(r["status"] for r in rows))
    report["missing_hdf"] = sorted({g for r in rows for g in r["missing_hdf"]})
    report["raw_hdf_records"] = hdf_summary["records"]
    report["acquisitions"] = rows

    path = None
    if args.download:
        path = _write_report(raw_root / subdir, mode_name, report)
    elif args.reproduce:
        path = _write_report(output_root, mode_name, report)

    print(json.dumps({k: v for k, v in report.items() if k not in {"acquisitions", "raw_hdf_records"}},
                     ensure_ascii=False, indent=2))
    for rec in hdf_summary["records"]:
        if rec["status"] != "PRESENT_VALID":
            print(f"  {rec['status']:<24} {rec['platform']:<5} {rec['date']} {rec['tile']} {rec['granule_id']}")
    if path:
        print("report:", path)

    bad_hdf = {"PRESENT_INVALID", "DOWNLOAD_FAILED", "DOWNLOADED_INVALID"}
    if any(r["status"] in bad_hdf for r in hdf_summary["records"]):
        return 1
    if args.download and any(s.status == "MISSING" for s in targets):
        return 1
    if args.reproduce and any(r["status"] in {ACQ_FAILED, ACQ_PARTIAL} for r in rows):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
