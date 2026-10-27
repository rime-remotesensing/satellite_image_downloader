"""Pixel comparison of two GeoTIFFs on the same grid (shared by run3.py / run4.py --verify)."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

import numpy as np
import rasterio


def compare_aligned(a: Path, b: Path) -> Dict[str, Any]:
    """Compare a and b band by band. Same CRS and cell size are required; windows that differ but lie on the
    same lattice are compared on their overlap. NaN == NaN counts as equal."""
    with rasterio.open(a) as x, rasterio.open(b) as y:
        res: Dict[str, Any] = {
            "crs_equal": x.crs == y.crs,
            "cell_equal": x.transform.a == y.transform.a and x.transform.e == y.transform.e,
            "band_count_equal": x.count == y.count, "dtype_equal": x.dtypes == y.dtypes,
            "nodata_equal": (x.nodata == y.nodata) or (x.nodata is not None and y.nodata is not None
                                                       and np.isnan(x.nodata) and np.isnan(y.nodata)),
            "window_equal": (tuple(x.transform)[:6], x.shape) == (tuple(y.transform)[:6], y.shape),
        }
        if not (res["crs_equal"] and res["cell_equal"] and res["band_count_equal"]):
            res["identical"] = False
            return res
        cx = (y.transform.c - x.transform.c) / x.transform.a
        cy = (y.transform.f - x.transform.f) / x.transform.e
        if abs(cx - round(cx)) > 1e-6 or abs(cy - round(cy)) > 1e-6:
            res.update(identical=False, lattice_aligned=False)
            return res
        cx, cy = round(cx), round(cy)
        r0, c0, r1, c1 = max(0, cy), max(0, cx), min(x.height, cy + y.height), min(x.width, cx + y.width)
        if r1 <= r0 or c1 <= c0:
            res.update(identical=False, overlap_pixels=0)
            return res
        va = x.read()[:, r0:r1, c0:c1]
        vb = y.read()[:, r0 - cy:r1 - cy, c0 - cx:c1 - cx]
        eq = (va == vb) | (np.isnan(va) & np.isnan(vb)) if va.dtype.kind == "f" else va == vb
        res.update(overlap_pixels=int((r1 - r0) * (c1 - c0)), mismatch_values=int((~eq).sum()))
        res["identical"] = res["mismatch_values"] == 0 and res["dtype_equal"] and bool(res["nodata_equal"])
    return res
