"""Full-scene MODIS windows: AOI + halo on the native 500 m grid, independent of training patches.

Since 2026-10-27 these are the official MODIS image windows (frozen in config/modis_run2_full_scene_manifest.json).
The earlier official images used the grids of config/modis_run2_acquisitions.json -- training-patch support
windows (the union of the 32 x 32 TRAIN/DEV patches using each file, so 21 did not cover the AOI); run2.py
keeps them as --legacy-patch-extent. A full-scene window is derived from the AOI polygon and the native grid only:

  AOI window  = projected AOI bbox (densified edges) snapped outward to whole 500 m pixels
  halo window = AOI bbox + halo (default 10 km, region01 13 km; src.halo_context) snapped outward,
                >= HALO_PIXELS (18) native pixels on every side
  state_1km   = the halo window snapped outward to whole 1 km pixels (contains the 500 m window)

Same tiles, extraction, reflectance scaling, QA and NoData as the official images (src.modis_native_window);
only the window differs. Every window is recorded with its affine transform and its offset in the global
MODIS Sinusoidal pixel grid, so pixels are matched by coordinates -- never by "offset from the image corner".
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .constants import MODIS_SINUSOIDAL_X_MIN, MODIS_SINUSOIDAL_Y_MAX, MODIS_TILE_SIZE_M
from .halo_context import HALO_PIXELS, build_halo_context, halo_buffer_m_for_region, load_geojson_geometry

MANIFEST_NAME = "full_scene_manifest.json"
MANIFEST_SCHEMA = "modis-run2-full-scene-v1"
PX = {"500m": MODIS_TILE_SIZE_M / 2400, "1km": MODIS_TILE_SIZE_M / 1200}
_REPO = Path(__file__).resolve().parents[1]
# The windows of the official images (Aso/MODIS/<region>/2024/surface_reflectance/, adopted 2026-10-27):
# a byte copy of the full_scene_manifest.json the research side adopted.
FROZEN_MANIFEST_PATH = _REPO / "config" / "modis_run2_full_scene_manifest.json"
FROZEN_MANIFEST_SHA256 = "95100815ede6558b7d9454cec567d07712ae6687a9149d86d7b1db521281a861"
# The external TEST windows (region05 2023/2025, hitoyoshi 2025): the same AOI + halo rule, frozen separately
# (python -m src.modis_full_scene --build-test). The existing TEST images were AOI-clipped bbox crops; their
# grids appear here as "patch_support_window" (the earlier extent), which must lie inside the new window.
TEST_FROZEN_MANIFEST_PATH = _REPO / "config" / "modis_run2_test_full_scene_manifest.json"

Window = Tuple[int, int, int, int]  # global pixel indices: row0, row1, col0, col1 (half-open)


def window_from_bounds(bounds: Sequence[float], grid: str) -> Window:
    """Whole native pixels covering (left, bottom, right, top); bounds already on the lattice stay exact."""
    left, bottom, right, top = bounds
    px = PX[grid]
    snap = lambda v: round(v, 6)  # noqa: E731  (absorb float noise of lattice-aligned bounds)
    return (math.floor(snap((MODIS_SINUSOIDAL_Y_MAX - top) / px)), math.ceil(snap((MODIS_SINUSOIDAL_Y_MAX - bottom) / px)),
            math.floor(snap((left - MODIS_SINUSOIDAL_X_MIN) / px)), math.ceil(snap((right - MODIS_SINUSOIDAL_X_MIN) / px)))


def window_from_grid(transform: Sequence[float], shape: Sequence[int], grid: str) -> Window:
    px = PX[grid]
    r0 = round((MODIS_SINUSOIDAL_Y_MAX - transform[5]) / px)
    c0 = round((transform[2] - MODIS_SINUSOIDAL_X_MIN) / px)
    return r0, r0 + int(shape[0]), c0, c0 + int(shape[1])


def transform_of(win: Window, grid: str) -> Tuple[float, ...]:
    px = PX[grid]
    return (px, 0.0, MODIS_SINUSOIDAL_X_MIN + win[2] * px, 0.0, -px, MODIS_SINUSOIDAL_Y_MAX - win[0] * px)


def shape_of(win: Window) -> Tuple[int, int]:
    return win[1] - win[0], win[3] - win[2]


def to_1km(win: Window) -> Window:
    return win[0] // 2, -(-win[1] // 2), win[2] // 2, -(-win[3] // 2)


def contains(outer: Window, inner: Window) -> bool:
    return outer[0] <= inner[0] and outer[2] <= inner[2] and outer[1] >= inner[1] and outer[3] >= inner[3]


def margins(outer: Window, inner: Window) -> Dict[str, int]:
    """Pixels of outer beyond inner on each side (negative = inner sticks out)."""
    return {"top": inner[0] - outer[0], "bottom": outer[1] - inner[1], "left": inner[2] - outer[2], "right": outer[3] - inner[3]}


def in_image(image: Window, inner: Window) -> Dict[str, int]:
    """inner as (row_off, col_off, height, width) inside image -- informational; match pixels by transform."""
    return {"row_off": inner[0] - image[0], "col_off": inner[2] - image[2], "height": inner[1] - inner[0], "width": inner[3] - inner[2]}


def full_scene_spec(spec, halo_m: Optional[float] = None):
    """(AcquisitionSpec with full-scene grids, window record) for one frozen acquisition."""
    from .modis_plan import AcquisitionSpec, region_geojson

    buffer_m = halo_buffer_m_for_region(spec.region) if halo_m is None else float(halo_m)
    halo = build_halo_context(load_geojson_geometry(_REPO / region_geojson(spec.region)), buffer_m)
    w_aoi = window_from_bounds(halo["original_native_bounds"], "500m")
    w500 = window_from_bounds(halo["halo_native_bounds"], "500m")
    w1k = to_1km(w500)
    t_off, s_off = spec.grids["sr"]
    w_patch = window_from_grid(t_off, s_off, "500m")
    grids = {"sr": (transform_of(w500, "500m"), shape_of(w500)), "qc_500m": (transform_of(w500, "500m"), shape_of(w500)),
             "state_1km": (transform_of(w1k, "1km"), shape_of(w1k))}
    m = margins(w500, w_aoi)
    record = {
        "region": spec.region, "platform": spec.platform, "date": spec.date, "granules": list(spec.granules),
        "halo_m_requested": buffer_m, "halo_pixels_required": HALO_PIXELS,
        "aoi_bbox_wgs84": list(halo["original_bbox_wgs84"]), "halo_bbox_wgs84": list(halo["halo_bbox_wgs84"]),
        "grids": {k: {"transform": list(t), "shape": list(s), "crs": "MODIS Sinusoidal +R=6371007.181",
                      "global_pixel_window": dict(zip(("row0", "row1", "col0", "col1"), w1k if k == "state_1km" else w500)),
                      "bounds_m": _bounds(t, s)} for k, (t, s) in grids.items()},
        "aoi_window": {"global_pixel_window": dict(zip(("row0", "row1", "col0", "col1"), w_aoi)),
                       "in_image": in_image(w500, w_aoi)},
        "halo_achieved_px": m, "halo_achieved_px_min": min(m.values()),
        "patch_support_window": {"global_pixel_window": dict(zip(("row0", "row1", "col0", "col1"), w_patch)),
                                 "in_image": in_image(w500, w_patch), "contained": contains(w500, w_patch)},
    }
    return AcquisitionSpec(spec.region, spec.platform, spec.date, spec.granules, grids), record


def load_frozen_full_scene(acqs, path: Path = FROZEN_MANIFEST_PATH):
    """Official windows: (AcquisitionSpec list with frozen full-scene grids, records), in the order of acqs.

    The manifest must hold exactly these acquisitions with the same granules; windows are taken as stored
    (not recomputed), so the official extent cannot drift with code changes.
    """
    from .modis_plan import AcquisitionSpec

    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if data.get("schema") != MANIFEST_SCHEMA:
        raise ValueError(f"{path}: unexpected schema {data.get('schema')!r}")
    by_key = {(r["region"], r["platform"], r["date"]): r for r in data["acquisitions"]}
    specs, records = [], []
    for a in acqs:
        rec = by_key.get((a.region, a.platform, a.date))
        if rec is None or tuple(rec["granules"]) != tuple(a.granules):
            raise ValueError(f"{path}: {a.region}/{a.platform}/{a.date} missing or granules differ")
        grids = {k: (tuple(v["transform"]), tuple(v["shape"])) for k, v in rec["grids"].items()}
        specs.append(AcquisitionSpec(a.region, a.platform, a.date, a.granules, grids))
        records.append(rec)
    return specs, records


def file_sha256(path: Path) -> str:
    import hashlib

    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _bounds(t, s) -> List[float]:
    left, top = t[2], t[5]
    return [left, top - s[0] * abs(t[4]), left + s[1] * t[0], top]


def tags_for(record: Dict[str, Any]) -> Dict[str, Any]:
    """GeoTIFF tags so a reader can place every pixel by coordinates."""
    g = record["grids"]["sr"]["global_pixel_window"]
    return {
        "spatial_export_mode": "full_scene_aoi_halo",
        "extent_definition": "AOI bbox + halo snapped outward to the native 500 m grid; independent of training patches",
        "halo_m_requested": record["halo_m_requested"], "halo_achieved_px": record["halo_achieved_px"],
        "aoi_window_in_image": record["aoi_window"]["in_image"],
        "global_pixel_origin_500m": f"row={g['row0']},col={g['col0']} in the global MODIS Sinusoidal 500 m grid "
                                    f"(x0={MODIS_SINUSOIDAL_X_MIN}, y0={MODIS_SINUSOIDAL_Y_MAX}, pixel={PX['500m']})",
        "pixel_mapping": "pixel (row, col) centre: x = transform.c + (col + 0.5) * transform.a, "
                         "y = transform.f + (row + 0.5) * transform.e; match images by coordinates, not by corner offsets",
    }


def manifest_document(records: List[Dict[str, Any]]) -> Dict[str, Any]:
    return {"schema": MANIFEST_SCHEMA, "acquisition_count": len(records),
            "grid": {"crs": "+proj=sinu +lon_0=0 +x_0=0 +y_0=0 +R=6371007.181 +units=m +no_defs",
                     "origin_x": MODIS_SINUSOIDAL_X_MIN, "origin_y": MODIS_SINUSOIDAL_Y_MAX,
                     "pixel_500m": PX["500m"], "pixel_1km": PX["1km"]},
            "acquisitions": records}


def write_or_check_manifest(output_root: Path, records: List[Dict[str, Any]]) -> Path:
    """Write <output_root>/full_scene_manifest.json; if it exists, the windows must be identical (never rewritten)."""
    path = Path(output_root) / MANIFEST_NAME
    key = lambda r: (r["region"], r["platform"], r["date"])  # noqa: E731
    if path.exists():
        old = {key(r): r for r in json.loads(path.read_text(encoding="utf-8"))["acquisitions"]}
        for r in records:
            if key(r) in old and old[key(r)]["grids"] != json.loads(json.dumps(r["grids"])):
                raise ValueError(f"{path}: windows for {key(r)} differ from the existing manifest")
        missing = [r for r in records if key(r) not in old]
        if missing:
            raise ValueError(f"{path} exists without {len(missing)} acquisitions; use a new output root")
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest_document(records), ensure_ascii=False, indent=1), encoding="utf-8")
    return path


def _main(argv=None) -> int:
    import argparse

    from .modis_plan import SCOPE_TEST, load_acquisition_manifest

    parser = argparse.ArgumentParser(description="Maintenance: freeze the external TEST full-scene windows.")
    parser.add_argument("--build-test", action="store_true", required=True)
    parser.add_argument("--output", type=Path, required=True, help="manifest JSON to write (must not exist)")
    args = parser.parse_args(argv)
    if args.output.exists():
        parser.error(f"refusing to overwrite {args.output}")
    acqs, _ = load_acquisition_manifest(scope=SCOPE_TEST)
    records = [full_scene_spec(a)[1] for a in acqs]
    args.output.write_text(json.dumps(manifest_document(records), ensure_ascii=False, indent=1), encoding="utf-8")
    load_frozen_full_scene(acqs, args.output)
    print(f"wrote {args.output}: {len(records)} acquisitions")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
