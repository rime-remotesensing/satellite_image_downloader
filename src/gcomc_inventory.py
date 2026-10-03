"""SGLI-specific usable-observation inventory (standalone utility, not part of normal downloads).

Reads already-downloaded RSRF HDF5 files from the local raw archive (no network)
and reports, per date x region (both tiles combined on the native grids):
product status, AOI status, per-band valid fraction, Error_DN fraction, main
QA_flag bit fractions, clear fraction and observation time.

Usage (inside the "gcomc" compose service):
  python3 -m src.gcomc_inventory --start 2024-02-20 --end 2024-04-10 \
      --region region01=config/no1.geojson --region region09=config/no9.geojson \
      --out /gcomc_data/inventory/sgli_rsrf_usable_inventory.csv
"""

from __future__ import annotations

import argparse
import csv
import json
import os
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np

from . import gcomc

# Official QA_flag bits (audited Data_description) summarised in the inventory.
INVENTORY_QA_BITS: Dict[int, str] = {
    0: "no_available_data", 5: "snow_or_ice", 6: "cloud", 7: "probably_cloud", 8: "high_tau_a",
    9: "saturation_recovery", 11: "straylight", 12: "shadow", 13: "pol_cloud_or_hi_tau",
    14: "recovery_by_pre_days", 15: "recovery_pol",
}
# "clear" = observed and none of cloud(6) / probably cloud(7) / shadow(12) set; informational only.
CLEAR_EXCLUDE_BITS = (6, 7, 12)


def _load_geometry(path: Path) -> Dict[str, Any]:
    gj = json.loads(Path(path).read_text(encoding="utf-8"))
    if gj.get("type") == "FeatureCollection":
        return gj["features"][0]["geometry"]
    if gj.get("type") == "Feature":
        return gj["geometry"]
    return gj


def _local_products(raw_dir: Path, obs: date, tiles: Sequence[Tuple[int, int]]) -> Dict[Tuple[int, int], gcomc.ProductResult]:
    out: Dict[Tuple[int, int], gcomc.ProductResult] = {}
    for v, h in tiles:
        ident = f"GC1SG1_{obs:%Y%m%d}{gcomc.GCOMC_ORBIT_DIRECTION}01D_T{v:02d}{h:02d}_L2SG_RSRFQ_{gcomc.GCOMC_RSRF_PRODUCT_VERSION}"
        g = gcomc.parse_rsrf_identifier(ident)
        path = gcomc.local_raw_path(raw_dir, g)
        if path.exists():
            out[(v, h)] = gcomc.ProductResult(granule=g, status=gcomc.PRODUCT_AVAILABLE, source="local_cache", local_path=path)
        else:
            out[(v, h)] = gcomc.ProductResult(granule=g, status="NOT_IN_LOCAL_ARCHIVE", local_path=path)
    return out


def region_day_stats(obs: date, region: str, geometry: Dict[str, Any], raw_dir: Path) -> Dict[str, Any]:
    tiles = gcomc.tiles_for_geometry(geometry)
    products = _local_products(raw_dir, obs, tiles)
    row: Dict[str, Any] = {
        "observation_date": obs.isoformat(), "region": region,
        "tiles": "+".join(sorted(p.granule.tile_id for p in products.values())),
        "product_status": ";".join(f"{p.granule.tile_id}:{p.status}" for p in products.values()),
    }
    files = {k: p.local_path for k, p in products.items() if p.status == gcomc.PRODUCT_AVAILABLE}
    if not files:
        row["aoi_status"] = "NOT_EVALUATED"
        return row
    bbox = gcomc._aoi_bbox_geometry_in_crs(geometry, gcomc.eqa_crs())
    bounds = gcomc.geometry_bounds(bbox)
    windows = {grid: gcomc.native_window_for_bounds(bounds, grid) for grid in gcomc.GCOMC_TILE_PIXELS}
    masks = {grid: gcomc.aoi_pixel_mask(win, geometry) for grid, win in windows.items()}
    qa_window = gcomc.read_mosaic(windows["250m"], gcomc._QA_FIELD, files)
    status, n_obs, n = gcomc.classify_aoi(qa_window, masks["250m"])
    qa = qa_window[masks["250m"]]
    row.update(aoi_status=status, aoi_pixels_250m=n, aoi_pixels_1km=int(masks["1km"].sum()),
               observed_fraction_250m=round(n_obs / n, 4))
    all_valid = np.ones(n, dtype=bool)
    any_error = np.zeros(n, dtype=bool)
    with_meta = None
    for spec in gcomc.GCOMC_REFLECTANCE_250M + gcomc.GCOMC_REFLECTANCE_1KM:
        dn = gcomc.read_mosaic(windows[spec.grid], spec, files)[masks[spec.grid]]
        if with_meta is None:
            with_meta = gcomc.validate_rsrf_hdf5(next(iter(files.values())))["fields"]
        m = with_meta[spec.name]
        ok = (dn != m["error_dn"]) & (dn >= m["min_valid_dn"]) & (dn <= m["max_valid_dn"])
        row[f"valid_fraction_{spec.name}"] = round(float(ok.mean()), 4)
        if spec.grid == "250m":
            all_valid &= ok
            any_error |= dn == m["error_dn"]
    row["valid_fraction_all_250m_bands"] = round(float(all_valid.mean()), 4)
    row["error_dn_fraction_any_250m_band"] = round(float(any_error.mean()), 4)
    row["band_availability_250m"] = all(row[f"valid_fraction_{s.name}"] > 0 for s in gcomc.GCOMC_REFLECTANCE_250M)
    row["band_availability_1km"] = all(row[f"valid_fraction_{s.name}"] > 0 for s in gcomc.GCOMC_REFLECTANCE_1KM)
    for bit, name in INVENTORY_QA_BITS.items():
        row[f"qa_{name}_fraction"] = round(float(np.mean((qa >> bit) & 1)), 4)
    clear = all_valid.copy()
    for bit in CLEAR_EXCLUDE_BITS:
        clear &= ((qa >> bit) & 1) == 0
    row["clear_fraction_250m"] = round(float(clear.mean()), 4)
    obs_spec = next(s for s in gcomc.GCOMC_RAW_FIELDS if s.name == "Obs_time")
    ot = gcomc.read_mosaic(windows["250m"], obs_spec, files)[masks["250m"]]
    ot_meta = with_meta["Obs_time"]
    ot_h = ot[ot != ot_meta["error_dn"]].astype(np.float64) * ot_meta["slope"] + ot_meta["offset"]
    row["obs_time_utc_hour_min"] = round(float(ot_h.min()), 3) if ot_h.size else ""
    row["obs_time_utc_hour_max"] = round(float(ot_h.max()), 3) if ot_h.size else ""
    return row


def build_inventory(start: date, end: date, regions: Dict[str, Path], raw_dir: Path) -> List[Dict[str, Any]]:
    geoms = {name: _load_geometry(path) for name, path in regions.items()}
    rows = []
    d = start
    while d <= end:
        for name, geom in geoms.items():
            rows.append(region_day_stats(d, name, geom, raw_dir))
        d += timedelta(days=1)
    return rows


def write_csv(rows: List[Dict[str, Any]], out: Path) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    fields = list(dict.fromkeys(k for r in rows for k in r))
    with open(out, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fields)
        w.writeheader()
        w.writerows(rows)


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--start", required=True)
    ap.add_argument("--end", required=True)
    ap.add_argument("--region", action="append", required=True, help="NAME=path/to/aoi.geojson (repeatable)")
    ap.add_argument("--raw-dir", default=os.environ.get("GCOMC_RAW_DIR"))
    ap.add_argument("--out", required=True)
    a = ap.parse_args(argv)
    if not a.raw_dir:
        ap.error("--raw-dir (or GCOMC_RAW_DIR) is required")
    regions = dict(item.split("=", 1) for item in a.region)
    rows = build_inventory(datetime.strptime(a.start, "%Y-%m-%d").date(), datetime.strptime(a.end, "%Y-%m-%d").date(),
                           {k: Path(v) for k, v in regions.items()}, Path(a.raw_dir))
    write_csv(rows, Path(a.out))
    print(f"wrote {len(rows)} rows to {a.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
