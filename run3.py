"""VIIRS L2 swath images for the research: raw SR/geolocation recovery + 375/750 m grid generation.

Official VIIRS entry point (MODIS = run2.py, SGLI = run4.py). Product: daytime
L2 Surface Reflectance swath VNP09 (S-NPP) / VJ109 (NOAA-20) / VJ209 (NOAA-21),
collection 002, with the exact IMG/MOD geolocation named in each SR InputPointer.
I bands go to the fixed 375 m and M bands / QF1-QF7 / land_water_mask to the fixed
750 m equal-area Sinusoidal analysis grid by surface-distance nearest neighbour
(src/viirs_l2.py, the algorithm the research pipeline ported verbatim). This is NOT
the old Daily L2G (VNP09GA, ~463 m / ~926 m) product.

Plan (no date is inferred): src/viirs_plan.RUN3_DOWNLOAD_PLAN, 141 region-dates x 3 platforms (423 tasks),
frozen per task in config/viirs_run3_acquisitions.json. By default only the 344 tasks the research uses
as PRE/POST (src/viirs_plan.RUN3_RESEARCH_TASKS) are processed; --all-plan-tasks takes all 423.

External TEST (--scope test): src/viirs_plan.RUN3_TEST_PLAN, the 6 SNPP PRE/POST tasks of the three
previously evaluated external cases (region05 2023/2025, hitoyoshi 2025), frozen per task in
config/viirs_run3_test_acquisitions.json; same L2 swath products, grid, QA and 13 km halo. The existing TEST
images (Aso/VIIRS/<region>/<2023|2025>/surface_reflectance/snpp/, Daily L2G VNP09GA 500m/1km) are a different
product on a different grid: --legacy-l2g-root only describes them next to the new products and never treats
them as pixel-comparable.

Official extent: AOI bbox + 13 km halo on the fixed 375/750 m grid -- one window per region, independent
of date, platform and training patches. Official layout (daily products only; per-overpass intermediates
are written only with --keep-overpass):
  <output-root>/<region>/<YYYY>/surface_reflectance/<platform>/{375m,750m,qa,geometry,provenance}/
      <SR>_<platform>_<YYYYMMDD>_daily_<group>.tif
  <output-root>/<region>/<YYYY>/surface_reflectance/<platform>/summary/<SR>_<platform>_<YYYYMMDD>.json

Modes (exactly one; --dry-run is the default):

  --dry-run    Locate every raw file of the 423 tasks (raw root / existing roots).
               No network, no writes. --verify-raw also opens every header
               (schema, DayNightFlag=DAY, InputPointer pairing, orbit/time).
  --download   Fetch only MISSING raw files by exact granule name (CMR exact-name
               lookup, .part -> size + signature check -> rename), then verify them.
               Never overwrites, never deletes. --copy-existing also copies (not
               moves) files found under --existing-root into the raw root.
  --reproduce  Generate overpass + daily products of every ready task into
               --output-root. Existing outputs are left alone.
  --verify     Check --output-root against the grid/QA/band spec; with
               --reference-root compare pixels with another output tree; with
               --research-scenes compare daily products with research scene.npz.

Reports (timestamped JSON, never overwritten): <raw-root>/_run3_reports/ (download),
<output-root>/_run3_reports/ (reproduce, verify).

Examples:
  python run3.py --raw-root <VIIRS_RAW_ROOT> --existing-root <old>/{region}/viirs/_raw_l2
  python run3.py --download --raw-root <VIIRS_RAW_ROOT>
  python run3.py --reproduce --raw-root <VIIRS_RAW_ROOT> --output-root <new dir>
  python run3.py --verify --output-root <dir> [--research-scenes <dataset/scenes>]
  python run3.py --scope test --download --raw-root <VIIRS_RAW_ROOT>
  python run3.py --scope test --reproduce --raw-root <VIIRS_RAW_ROOT> --output-root <new dir>
  python run3.py --scope test --verify --output-root <dir> --legacy-l2g-root <Aso/VIIRS>
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
import shutil
import sys
from collections import Counter
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from src.raster_compare import compare_aligned
from src.regions import REGION_GEOJSONS
from src.viirs_plan import (
    DEFAULT_HALO_M, PLATFORMS, SCOPE_TEST, SCOPES, SR_PRODUCT_BY_PLATFORM, STATUS_NO_GRANULE, TEST_REGION_GEOJSONS,
    load_acquisition_manifest, region_geojson, research_required_tasks, validate_download_plan, validate_test_plan,
)

LOGGER = logging.getLogger("run3")

RAW_ROOT_ENV = "VIIRS_L2_RAW_ROOT"
OUTPUT_ROOT_ENV = "VIIRS_OUTPUT_ROOT"
REPORT_DIR = "_run3_reports"

T_NO_GRANULE = "NO_GRANULE"
T_READY = "READY"
T_MISSING = "MISSING_RAW"
T_SPLIT = "RAW_SPLIT_ACROSS_ROOTS"
T_INVALID = "RAW_INVALID"
T_GENERATED = "GENERATED"
T_ALREADY = "ALREADY_PRESENT"
T_PARTIAL = "PARTIAL_OUTPUT_PRESENT"
T_FAILED = "FAILED"
T_SPEC_OK = "SPEC_OK"
T_SPEC_BAD = "SPEC_MISMATCH"
T_NO_OUTPUT = "OUTPUT_MISSING"

# group -> (grid, band count, dtype); the layout written by src.viirs_l2.write_product
EXPECTED_GROUPS = {
    "375m": ("375m", 3, "float32"), "750m": ("750m", 9, "float32"), "qa": ("750m", 8, "uint8"),
    "qa_375m": ("375m", 1, "uint8"), "geometry_375m": ("375m", 4, "float32"), "geometry_750m": ("750m", 4, "float32"),
    "provenance_375m": ("375m", 5, "float64"), "provenance_750m": ("750m", 5, "float64"),
}
# Research Basic9 channels 0..6 (physical reflectance) -> (our grid, band index); NDVI_01/NBR_01 are research-side.
RESEARCH_BANDS = (("I1", "375m", 0), ("I2", "375m", 1), ("M3", "750m", 2), ("M4", "750m", 3), ("M8", "750m", 6),
                  ("I3", "375m", 2), ("M11", "750m", 8))


class OfflineClient:
    """Stands in for LAADSClient during --reproduce: any network use is a bug."""

    def search(self, *a, **k):
        raise RuntimeError("network access attempted in offline reproduction")

    find_by_name = fetch = search


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%fZ")


def _write_report(directory: Path, mode: str, report: Dict[str, Any]) -> Path:
    path = directory / REPORT_DIR / f"run3_{mode}_{_stamp()}.json"
    if path.exists():
        raise FileExistsError(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return path


def _is_within(child: Path, parent: Path) -> bool:
    try:
        child.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def _region_geometry(region: str) -> Dict[str, Any]:
    from src.halo_context import load_geojson_geometry

    return load_geojson_geometry(Path(__file__).resolve().parent / region_geojson(region))


def _roots_for(region: str, raw_root: Optional[Path], existing: Sequence[str]) -> List[Path]:
    return ([raw_root] if raw_root else []) + [Path(e.replace("{region}", region)) for e in existing]


def locate_task(task, roots: Sequence[Path]) -> Dict[str, Any]:
    """Find every raw file of one task; a task is READY only when one root holds all of them."""
    from src.viirs_l2 import raw_path

    if task.status == STATUS_NO_GRANULE:
        return {"status": T_NO_GRANULE, "raw_dir": None, "missing": []}
    files = task.raw_files()
    for root in roots:
        if all(raw_path(root, n).is_file() for n in files):
            return {"status": T_READY, "raw_dir": str(root), "missing": []}
    found = {n for n in files for root in roots if raw_path(root, n).is_file()}
    missing = [n for n in files if n not in found]
    return {"status": T_MISSING if missing else T_SPLIT, "raw_dir": None, "missing": missing}


def verify_raw_task(task, raw_dir: Path) -> List[str]:
    """Header-level integrity of one task's raw files (no pixel reads)."""
    from pyhdf.SD import SD, SDC

    from src import viirs_l2

    p = viirs_l2.PLATFORMS[task.platform]
    problems = []
    for ov in task.overpasses:
        for name, (img, mod) in zip(ov.granules, ov.geolocation):
            path = viirs_l2.raw_path(raw_dir, name)
            try:
                hdf = SD(str(path), SDC.READ)
                try:
                    flag = str(hdf.attributes().get("DayNightFlag", "")).upper()
                finally:
                    hdf.end()
                if flag != "DAY":
                    problems.append(f"{name}: DayNightFlag {flag!r} != DAY")
                sr = viirs_l2.read_sr_header(path, p)
                if sr.orbit != ov.orbit:
                    problems.append(f"{name}: orbit {sr.orbit} != {ov.orbit}")
                if viirs_l2.pair_geolocation(sr, p, raw_dir) != (img, mod):
                    problems.append(f"{name}: InputPointer geolocation differs from manifest {(img, mod)}")
                viirs_l2.verify_pair(sr, viirs_l2.raw_path(raw_dir, img), viirs_l2.raw_path(raw_dir, mod))
            except Exception as exc:
                problems.append(f"{name}: {type(exc).__name__}: {exc}")
    return problems


def _download_task(task, raw_root: Path, client) -> List[str]:
    """Fetch the task's missing raw files into raw_root by exact name. Returns fetched names."""
    from src import viirs_l2

    p = viirs_l2.PLATFORMS[task.platform]
    concept = {"09": p.sr_concept_id, "03IMG": p.img_concept_id, "03MOD": p.mod_concept_id}
    fetched = []
    for name in task.raw_files():
        dest = viirs_l2.raw_path(raw_root, name)
        if dest.exists():
            continue
        url = client.find_by_name(concept[viirs_l2.parse_granule_name(name).kind], name)
        if not url.endswith("/" + name):
            raise viirs_l2.VIIRSPairingError(f"{name}: CMR URL {url} is not the exact granule")
        client.fetch(url, dest)
        fetched.append(name)
    return fetched


def _copy_into(task, src_roots: Sequence[Path], raw_root: Path) -> List[str]:
    from src.viirs_l2 import raw_path

    copied = []
    for name in task.raw_files():
        dest = raw_path(raw_root, name)
        src = next((raw_path(r, name) for r in src_roots if raw_path(r, name).is_file()), None)
        if dest.exists() or src is None:
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        part = dest.with_name(dest.name + ".part")
        shutil.copy2(src, part)
        if part.stat().st_size != src.stat().st_size:
            part.unlink()
            raise OSError(f"copy of {name} is incomplete")
        os.replace(part, dest)
        copied.append(name)
    return copied


def output_dir(output_root: Path, region: str, platform: str, year: str) -> Path:
    """Official layout VIIRS/<region>/<year>/surface_reflectance/<platform>/ (daily products directly below)."""
    return output_root / region / year / "surface_reflectance" / platform


def _task_dir(output_root: Path, task) -> Path:
    return output_dir(output_root, task.region, task.platform, task.date[:4])


def _summary_path(output_root: Path, task) -> Path:
    sr = SR_PRODUCT_BY_PLATFORM[task.platform]
    return _task_dir(output_root, task) / "summary" / f"{sr}_{task.platform}_{task.date.replace('-', '')}.json"


def _existing_outputs(output_root: Path, task) -> List[Path]:
    token = f"_{task.platform}_{task.date.replace('-', '')}_"
    base = _task_dir(output_root, task)
    return list(base.rglob(f"*{token}*.tif")) if base.exists() else []


def reproduce_task(task, raw_dir: Path, output_root: Path, halo_m: float, keep_overpass: bool = False) -> Dict[str, Any]:
    from src import viirs_l2

    geometry = _region_geometry(task.region)
    window = viirs_l2.grid_window_for_geometry(geometry, halo_m)
    entries = [(name, "", "DAY") for ov in task.overpasses for name in ov.granules]
    summary = viirs_l2.process_platform_day(viirs_l2.PLATFORMS[task.platform], date.fromisoformat(task.date), entries,
                                            raw_dir, OfflineClient(), geometry, window, output_root / task.region,
                                            product_dir=_task_dir(output_root, task), write_overpass=keep_overpass)
    got = sorted((o["orbit"], tuple(o["granules"])) for o in summary["overpasses"])
    want = sorted((ov.orbit, ov.granules) for ov in task.overpasses)
    if got != want:
        raise RuntimeError(f"overpasses {got} differ from the frozen manifest {want}")
    return summary


def _grid_problems(ds, grid: str) -> List[str]:
    from rasterio.crs import CRS

    from src.constants import MODIS_SINUSOIDAL_X_MIN, MODIS_SINUSOIDAL_Y_MAX
    from src.viirs_l2 import GRID_CELL_M, GRID_CRS, GRID_SNAP_M

    cell, t, out = GRID_CELL_M[grid], ds.transform, []
    if abs(t.a - cell) > 1e-9 or abs(t.e + cell) > 1e-9 or t.b or t.d:
        out.append(f"pixel size {t.a}/{t.e} != {cell}")
    for k in ((t.c - MODIS_SINUSOIDAL_X_MIN) / GRID_SNAP_M, (MODIS_SINUSOIDAL_Y_MAX - t.f) / GRID_SNAP_M):
        if abs(k - round(k)) > 1e-6:
            out.append(f"window origin ({t.c}, {t.f}) is not on the 750 m anchor lattice")
    if ds.crs != CRS.from_string(GRID_CRS):
        out.append(f"CRS {ds.crs.to_proj4()} != {GRID_CRS}")
    return out


def check_product_spec(files: Dict[str, Path], task) -> List[str]:
    """Spec check of one daily/overpass product set (8 group files)."""
    import numpy as np
    import rasterio

    from src.viirs_l2 import I_BANDS, M_BANDS, QA_NAMES

    names = {"375m": I_BANDS, "750m": M_BANDS, "qa": QA_NAMES}
    allowed = {g for ov in task.overpasses for g in ov.granules}
    problems, extents = [], {}
    for group, (grid, count, dtype) in EXPECTED_GROUPS.items():
        path = files[group]
        if not path.exists():
            problems.append(f"{group}: missing")
            continue
        with rasterio.open(path) as ds:
            extra = 1 if group.startswith("provenance") and ds.count == count + 1 else 0  # daily: selected_orbit
            if ds.count - extra != count or set(ds.dtypes) != {dtype}:
                problems.append(f"{group}: {ds.count} x {set(ds.dtypes)} != {count} x {dtype}")
            if group in names and list(ds.descriptions) != list(names[group]):
                problems.append(f"{group}: band order {ds.descriptions} != {names[group]}")
            if dtype.startswith("float") and not (ds.nodata is not None and math.isnan(ds.nodata)):
                problems.append(f"{group}: nodata {ds.nodata} is not NaN")
            problems += [f"{group}: {p}" for p in _grid_problems(ds, grid)]
            extents.setdefault(grid, (ds.transform.c, ds.transform.f, ds.width * ds.transform.a, ds.height * ds.transform.a))
            tags = ds.tags()
            if tags.get("platform") != task.platform:
                problems.append(f"{group}: platform tag {tags.get('platform')} != {task.platform}")
            if not set(json.loads(tags.get("source_granules", "[]"))) <= allowed:
                problems.append(f"{group}: source_granules {tags.get('source_granules')} not in the manifest")
            if group in ("375m", "750m"):
                a = ds.read()
                finite = a[np.isfinite(a)]
                if finite.size and (finite.min() < -0.0101 or finite.max() > 1.6001):
                    problems.append(f"{group}: reflectance outside [-0.01, 1.6]")
    if len(extents) == 2 and any(abs(x - y) > 1e-6 for x, y in zip(extents["375m"], extents["750m"])):
        problems.append(f"375 m and 750 m windows differ: {extents}")
    return problems


def _task_products(output_root: Path, task) -> Dict[str, Dict[str, Path]]:
    """'<kind>:<stem>' -> {group: file} for every product listed in the task's summary."""
    from src.viirs_l2 import GROUP_DIR

    s = json.loads(_summary_path(output_root, task).read_text(encoding="utf-8"))
    base = _task_dir(output_root, task)
    listed = [f for o in s["overpasses"] for f in o["files"]] + (s["daily"]["files"] if s.get("daily") else [])
    out: Dict[str, Dict[str, Path]] = {}
    for f in map(Path, listed):
        # daily: <platform>/<group dir>/<file>; overpass (only with --keep-overpass): <platform>/overpass/<group dir>/<file>
        kind = "overpass" if f.parents[1].name == "overpass" else "daily"
        root = base / "overpass" if kind == "overpass" else base
        group = next(g for g in sorted(EXPECTED_GROUPS, key=len, reverse=True) if f.name.endswith(f"_{g}.tif"))
        stem = f.name[: -len(f"_{group}.tif")]
        out.setdefault(f"{kind}:{stem}", {g: root / GROUP_DIR[g] / f"{stem}_{g}.tif" for g in EXPECTED_GROUPS})
    return out


def compare_research_scene(daily: Dict[str, Path], scene_dir: Path) -> Dict[str, Any]:
    """Our daily 375/750 m products vs a research scene.npz (Basic9; its M bands are 750 m values repeated 2x2)."""
    import numpy as np
    import rasterio

    npz = np.load(scene_dir / "scene.npz", allow_pickle=False)
    values, valid = npz["values"], npz["valid"]
    with rasterio.open(daily["375m"]) as f, rasterio.open(daily["750m"]) as c:
        fine, coarse, t375 = f.read(), c.read(), tuple(f.transform)[:6]
    res = {"transform_equal": all(abs(p - q) < 1e-6 for p, q in zip(t375, npz["transform"])),
           "shape_equal": tuple(fine.shape[1:]) == tuple(values.shape[1:])}
    if not (res["transform_equal"] and res["shape_equal"]):
        res["identical"] = False
        return res
    h, w = fine.shape[1:]
    ours = np.stack([fine[i] if grid == "375m" else np.repeat(np.repeat(coarse[i], 2, 0), 2, 1)[:h, :w]
                     for _, grid, i in RESEARCH_BANDS]).astype(np.float32)
    our_valid = np.isfinite(ours).all(0)
    res.update(valid_pixels_research=int(valid.sum()), valid_pixels_ours=int(our_valid.sum()),
               valid_mask_equal=bool((our_valid == valid).all()),
               mismatch_values_on_valid=int((ours[:, valid] != values[:7, valid]).sum()))
    res["identical"] = res["valid_mask_equal"] and res["mismatch_values_on_valid"] == 0
    return res


def _window_problems(files: Dict[str, Path], task, halo_m: float) -> List[str]:
    """The product must sit exactly on the region's AOI + halo analysis window."""
    import rasterio

    from src import viirs_l2

    window = viirs_l2.grid_window_for_geometry(_region_geometry(task.region), halo_m)
    problems = []
    for group, grid in (("375m", "375m"), ("750m", "750m")):
        with rasterio.open(files[group]) as ds:
            got = (round(ds.transform.c, 6), round(ds.transform.f, 6), ds.height, ds.width)
        want = (round(window.x0, 6), round(window.y1, 6), *window.shape(grid))
        if got != want:
            problems.append(f"{group}: window {got} != AOI + {halo_m:g} m halo window {want}")
    return problems


def describe_legacy_l2g(legacy_root: Path, task, daily: Optional[Dict[str, Path]]) -> Dict[str, Any]:
    """The existing TEST image of the same task (legacy Daily L2G VNP09GA) next to the new L2 swath product.

    Different product (gridded daily L2G vs swath L2), grid (MODIS-tile Sinusoidal ~463/926 m vs fixed
    375/750 m analysis grid) and band set, so they are never pixel-compared or declared identical."""
    import rasterio

    stem = f"VNP09GA_{task.platform}_{task.date.replace('-', '')}"
    base = legacy_root / task.region / task.date[:4] / "surface_reflectance" / task.platform
    out: Dict[str, Any] = {"comparable": False, "identical": None,
                           "reason": "legacy Daily L2G VNP09GA (~463/926 m tile grid) vs L2 swath VNP09 (375/750 m analysis grid)"}
    for label, path in (("legacy_500m", base / "500m" / f"{stem}_500m.tif"), ("legacy_1km", base / "1km" / f"{stem}_1km.tif"),
                        ("new_375m", (daily or {}).get("375m")), ("new_750m", (daily or {}).get("750m"))):
        if path is None or not Path(path).exists():
            out[label] = None
            continue
        with rasterio.open(path) as ds:
            out[label] = {"path": str(path), "pixel_m": [ds.transform.a, -ds.transform.e], "shape": [ds.height, ds.width],
                          "bands": ds.count, "crs": ds.crs.to_proj4(),
                          "bounds": [round(v, 1) for v in ds.bounds]}
    return out


def _verify(tasks, output_root: Path, reference_root: Optional[Path], research_scenes: Optional[Path],
            halo_m: Optional[float] = None, legacy_l2g_root: Optional[Path] = None) -> List[Dict[str, Any]]:
    rows = []
    for t in tasks:
        row: Dict[str, Any] = {"region": t.region, "platform": t.platform, "date": t.date}
        rows.append(row)
        if t.status == STATUS_NO_GRANULE:
            row["status"] = T_NO_GRANULE
            continue
        if not _summary_path(output_root, t).exists():
            row["status"] = T_NO_OUTPUT
            continue
        products = _task_products(output_root, t)
        row["products"] = {k: check_product_spec(v, t) for k, v in products.items()}
        if halo_m is not None:
            for k, v in products.items():
                row["products"][k] += _window_problems(v, t, halo_m)
        row["status"] = T_SPEC_OK if not any(row["products"].values()) else T_SPEC_BAD
        if legacy_l2g_root:
            daily = next((v for k, v in products.items() if k.startswith("daily:")), None)
            row["legacy_l2g"] = describe_legacy_l2g(legacy_l2g_root, t, daily)
        if reference_root:
            cmp = {}
            for k, files in products.items():
                for g, p in files.items():
                    ref = reference_root / p.relative_to(output_root)
                    cmp[f"{k}:{g}"] = compare_aligned(p, ref) if ref.exists() else {"identical": False, "reference": "missing"}
            row["reference"] = {"files": len(cmp), "identical": sum(c["identical"] for c in cmp.values()),
                                "window_equal": sum(bool(c.get("window_equal")) for c in cmp.values()),
                                "differs": {k: c for k, c in cmp.items() if not c["identical"]}}
        if research_scenes:
            scene = research_scenes / f"{t.region}_{t.platform}_{t.date}"
            daily = [v for k, v in products.items() if k.startswith("daily:")]
            if scene.is_dir() and daily:
                row["research_scene"] = compare_research_scene(daily[0], scene)
    return rows


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="locate raw files of every task (default)")
    mode.add_argument("--download", action="store_true", help="fetch only MISSING raw files by exact name")
    mode.add_argument("--reproduce", action="store_true", help="generate products of every ready task")
    mode.add_argument("--verify", action="store_true", help="check --output-root")
    parser.add_argument("--raw-root", type=Path, default=None,
                        help=f"persistent raw root <product>/<YYYY>/<DOY>/<file> (or env {RAW_ROOT_ENV})")
    parser.add_argument("--existing-root", action="append", default=[],
                        help="extra read-only raw root, same layout; '{region}' is replaced by the task region; repeatable")
    parser.add_argument("--copy-existing", action="store_true", help="with --download: copy (not move) existing files into --raw-root")
    parser.add_argument("--verify-raw", action="store_true", help="with --dry-run: open every raw header and check it")
    parser.add_argument("--output-root", type=Path, default=None, help=f"product root (or env {OUTPUT_ROOT_ENV})")
    parser.add_argument("--reference-root", type=Path, default=None, help="--verify: read-only output tree to compare pixels with")
    parser.add_argument("--research-scenes", type=Path, default=None,
                        help="--verify: read-only research dataset/scenes dir (<region>_<platform>_<date>/scene.npz)")
    parser.add_argument("--halo-m", type=float, default=DEFAULT_HALO_M, help="analysis window halo (default: research 13000 m)")
    parser.add_argument("--manifest", type=Path, default=None)
    parser.add_argument("--regions", nargs="+", choices=tuple(dict.fromkeys((*REGION_GEOJSONS, *TEST_REGION_GEOJSONS))))
    parser.add_argument("--scope", choices=SCOPES, default="train_dev",
                        help="train_dev: the 2024 plan (default); test: the external TEST plan (6 SNPP tasks)")
    parser.add_argument("--legacy-l2g-root", type=Path, default=None,
                        help="--verify --scope test: read-only existing TEST tree (Daily L2G); described, never pixel-compared")
    parser.add_argument("--platforms", nargs="+", choices=PLATFORMS)
    parser.add_argument("--dates", nargs="+", metavar="YYYY-MM-DD", help="restrict to these plan dates")
    parser.add_argument("--all-plan-tasks", action="store_true",
                        help="all 423 plan tasks (incl. region04) instead of the research-required tasks "
                             "(src/viirs_plan.RUN3_RESEARCH_TASKS, 344)")
    parser.add_argument("--keep-overpass", action="store_true",
                        help="--reproduce: also write the per-overpass products (default: daily products only)")
    args = parser.parse_args(argv)
    mode_name = "download" if args.download else "reproduce" if args.reproduce else "verify" if args.verify else "dry_run"
    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")

    test_scope = args.scope == SCOPE_TEST
    if test_scope and args.all_plan_tasks:
        parser.error("--all-plan-tasks selects the 2024 plan; --scope test always takes all 6 TEST tasks")
    if args.legacy_l2g_root and not (test_scope and args.verify):
        parser.error("--legacy-l2g-root is only valid with --verify --scope test")
    valid_regions = TEST_REGION_GEOJSONS if test_scope else REGION_GEOJSONS
    if args.regions and set(args.regions) - set(valid_regions):
        parser.error(f"regions {sorted(set(args.regions) - set(valid_regions))} are not in the {args.scope} plan")
    totals = validate_test_plan() if test_scope else validate_download_plan()
    tasks = [t for t in load_acquisition_manifest(args.manifest, scope=args.scope)
             if (not args.regions or t.region in args.regions) and (not args.platforms or t.platform in args.platforms)
             and (not args.dates or t.date in args.dates)]
    if not args.all_plan_tasks and not test_scope:
        required = research_required_tasks()
        tasks = [t for t in tasks if (t.region, t.platform, t.date) in required]
    raw_root = args.raw_root or (Path(os.environ[RAW_ROOT_ENV]) if os.environ.get(RAW_ROOT_ENV) else None)
    output_root = args.output_root or (Path(os.environ[OUTPUT_ROOT_ENV]) if os.environ.get(OUTPUT_ROOT_ENV) else None)
    if args.download and raw_root is None:
        parser.error(f"--download needs --raw-root or {RAW_ROOT_ENV}")
    if not args.verify and raw_root is None and not args.existing_root:
        parser.error(f"--raw-root (or {RAW_ROOT_ENV}) or --existing-root is required; no default data root is assumed")
    if (args.reproduce or args.verify) and output_root is None:
        parser.error(f"--output-root or {OUTPUT_ROOT_ENV} is required for --{mode_name}")
    others = [args.reference_root, args.research_scenes, args.legacy_l2g_root, raw_root] + [Path(e.split("{region}")[0]) for e in args.existing_root]
    for other in filter(None, others):
        if output_root and (args.reproduce or args.verify) and (_is_within(output_root, other) or _is_within(other, output_root)):
            parser.error(f"output root must be separate from {other}")
    if args.copy_existing and not args.download:
        parser.error("--copy-existing is only valid with --download")

    report: Dict[str, Any] = {
        "mode": mode_name, "plan": totals, "tasks_in_scope": len(tasks), "halo_m": args.halo_m,
        "scope": ("test (src/viirs_plan.RUN3_TEST_PLAN, external TEST)" if test_scope else
                  "all_plan_tasks" if args.all_plan_tasks else "research_required (src/viirs_plan.RUN3_RESEARCH_TASKS)"),
        "layout": "<output-root>/<region>/<YYYY>/surface_reflectance/<platform>/{375m,750m,qa,geometry,provenance,summary}",
        "product": "VIIRS L2 swath SR VNP09/VJ109/VJ209 collection 002 + VNP03/VJ103/VJ203 IMG/MOD geolocation",
        "raw_root": str(raw_root) if raw_root else None, "existing_roots": args.existing_root,
        "output_root": str(output_root) if output_root else None, "network_used": bool(args.download),
    }

    if args.verify:
        rows = _verify(tasks, output_root, args.reference_root, args.research_scenes,
                       args.halo_m if test_scope else None, args.legacy_l2g_root)
        report["status_counts"] = dict(Counter(r["status"] for r in rows))
        if args.reference_root:
            refd = [r["reference"] for r in rows if "reference" in r]
            report["reference_compare"] = {k: sum(r[k] for r in refd) for k in ("files", "identical", "window_equal")}
        scenes = [r["research_scene"] for r in rows if "research_scene" in r]
        if args.research_scenes:
            report["research_scene_compare"] = {"scenes": len(scenes), "identical": sum(s["identical"] for s in scenes)}
        report["tasks"] = rows
        path = _write_report(output_root, mode_name, report)
        print(json.dumps({k: v for k, v in report.items() if k != "tasks"}, ensure_ascii=False, indent=2))
        print("report:", path)
        return 1 if any(r["status"] == T_SPEC_BAD for r in rows) or any(not s["identical"] for s in scenes) else 0

    rows: List[Dict[str, Any]] = []
    client = None
    for t in tasks:
        roots = _roots_for(t.region, raw_root, args.existing_root)
        row = {"region": t.region, "platform": t.platform, "date": t.date, "orbits": [o.orbit for o in t.overpasses],
               "raw_files": t.raw_files(), **locate_task(t, roots)}
        rows.append(row)
        try:
            if args.download and args.copy_existing and row["status"] != T_NO_GRANULE:
                row["copied"] = _copy_into(t, roots[1:], raw_root)
            if args.download and row["status"] in (T_MISSING, T_SPLIT):
                if client is None:
                    from src.surface_reflectance import _earthdata_login
                    from src.viirs_l2 import LAADSClient

                    _earthdata_login({}, Path.cwd())
                    client = LAADSClient()
                row["downloaded"] = _download_task(t, raw_root, client)
            if args.download and (row.get("copied") or row.get("downloaded")):
                row.update(locate_task(t, [raw_root]))
                if row["status"] == T_READY:
                    row["raw_problems"] = verify_raw_task(t, raw_root)
            if (args.verify_raw or args.reproduce) and row["status"] == T_READY:
                row["raw_problems"] = verify_raw_task(t, Path(row["raw_dir"]))
            if row.get("raw_problems"):
                row["status"] = T_INVALID
        except Exception as exc:
            LOGGER.error("%s %s %s: %s", t.region, t.platform, t.date, exc)
            row.update(status=T_FAILED, error=str(exc))
            continue

        if not args.reproduce or row["status"] != T_READY:
            continue
        if _summary_path(output_root, t).exists():
            row["status"] = T_ALREADY
            continue
        if _existing_outputs(output_root, t):
            row["status"] = T_PARTIAL
            continue
        try:
            s = reproduce_task(t, Path(row["raw_dir"]), output_root, args.halo_m, args.keep_overpass)
            row["status"] = T_GENERATED
            row["overpasses"] = [{k: o[k] for k in ("orbit", "granules", "geolocation", "start_utc", "end_utc", "aoi_status",
                                                     "aoi_valid_fraction_375m", "files")} for o in s["overpasses"]]
            row["daily"] = s["daily"]
        except Exception as exc:
            LOGGER.error("%s %s %s: %s", t.region, t.platform, t.date, exc, exc_info=True)
            row.update(status=T_FAILED, error=str(exc))

    report["status_counts"] = dict(Counter(r["status"] for r in rows))
    report["missing_raw_files"] = sorted({m for r in rows for m in r.get("missing", [])})
    report["tasks"] = rows
    path = None
    if args.download:
        path = _write_report(raw_root, mode_name, report)
    elif args.reproduce:
        path = _write_report(output_root, mode_name, report)
    print(json.dumps({k: v for k, v in report.items() if k not in ("tasks", "missing_raw_files")}, ensure_ascii=False, indent=2))
    print("missing raw files:", len(report["missing_raw_files"]))
    if path:
        print("report:", path)
    bad = {T_FAILED, T_INVALID, T_PARTIAL} | ({T_MISSING, T_SPLIT} if args.download else set())
    return 1 if any(r["status"] in bad for r in rows) else 0


if __name__ == "__main__":
    sys.exit(main())
