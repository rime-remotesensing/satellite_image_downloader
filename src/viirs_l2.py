"""VIIRS L2 swath Surface Reflectance (VNP09 / VJ109 / VJ209) -> fixed UTM 375 m / 750 m analysis grids.

Output = nominal 375-m / 750-m VIIRS L2 observations mapped to a fixed 375-m / 750-m analysis grid.
375 m / 750 m are analysis-grid spacings, not effective resolutions (source footprints at Aso are ~423-800 m for I bands).
The swath -> grid step is a re-gridding (nearest neighbour), not a resampling-free copy.
I bands stay on the 375 m grid and M bands on the 750 m grid; nothing is up- or down-sampled
between them (the 750 -> 375 broadcast belongs to the later deep-learning preprocessing).

Every structural constant was verified against real 2024 granules
(docs/viirs_l2_swath_smoke_test.md). An unexpected schema is a hard error.
The legacy Daily L2G path (VNP09GA/VJ109GA/VJ209GA) in surface_reflectance.py is untouched.
"""

from __future__ import annotations

import calendar
import json
import logging
import math
import os
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

import h5py
import numpy as np
import rasterio
from rasterio.crs import CRS
from rasterio.features import bounds as geometry_bounds, geometry_mask
from rasterio.transform import Affine

from .config import _resolve_runtime_path
from .network_retry import call_with_network_retry

LOGGER = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Fixed analysis grid
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class AnalysisGridDefinition:
    """A fixed analysis grid: CRS, origin, and the nested cell sizes. Cell edges are integer multiples
    of the cell size from the origin; the origin is never derived from a granule extent.
    The coarse cell must be an integer multiple of the fine cell so the grids nest exactly."""
    name: str
    crs: str
    origin_e: float
    origin_n: float
    cell_m: Dict[str, float]
    snap_m: float            # AOI windows snap outward to this (the coarse cell) -> identical 375/750 extents


# Analysis grid for the current study area (Aso, Kyushu; all regions lie in UTM zone 52).
# This is NOT a global rule: another region needs its own definition (e.g. a different UTM zone).
KYUSHU_UTM52N_GRID = AnalysisGridDefinition(
    name="kyushu_utm52n_375_750", crs="EPSG:32652", origin_e=0.0, origin_n=0.0,
    cell_m={"375m": 375.0, "750m": 750.0}, snap_m=750.0,
)
DEFAULT_ANALYSIS_GRID = KYUSHU_UTM52N_GRID
# Back-compatible aliases of the default definition.
GRID_CRS = DEFAULT_ANALYSIS_GRID.crs
GRID_ORIGIN_E = DEFAULT_ANALYSIS_GRID.origin_e
GRID_ORIGIN_N = DEFAULT_ANALYSIS_GRID.origin_n
GRID_CELL_M = DEFAULT_ANALYSIS_GRID.cell_m
GRID_SNAP_M = DEFAULT_ANALYSIS_GRID.snap_m


@dataclass(frozen=True)
class GridWindow:
    """AOI window on a fixed grid; x0/y0/x1/y1 are multiples of grid.snap_m from the grid origin (exactly representable)."""
    x0: float
    y0: float
    x1: float
    y1: float
    grid: AnalysisGridDefinition = DEFAULT_ANALYSIS_GRID

    def shape(self, res: str) -> Tuple[int, int]:
        c = self.grid.cell_m[res]
        return int(round((self.y1 - self.y0) / c)), int(round((self.x1 - self.x0) / c))

    def transform(self, res: str) -> Affine:
        c = self.grid.cell_m[res]
        return Affine(c, 0.0, self.x0, 0.0, -c, self.y1)

    def centres(self, res: str) -> Tuple[np.ndarray, np.ndarray]:
        ny, nx = self.shape(res)
        c = self.grid.cell_m[res]
        xs = self.x0 + (np.arange(nx) + 0.5) * c
        ys = self.y1 - (np.arange(ny) + 0.5) * c
        return np.meshgrid(xs, ys)


def _grid_transformer(crs: str = DEFAULT_ANALYSIS_GRID.crs, inverse: bool = False):
    from pyproj import Transformer
    return Transformer.from_crs(crs, "EPSG:4326", always_xy=True) if inverse else \
        Transformer.from_crs("EPSG:4326", crs, always_xy=True)


def grid_window_for_geometry(geometry_wgs84: Dict[str, Any], halo_m: float = 10000.0,
                             grid: AnalysisGridDefinition = DEFAULT_ANALYSIS_GRID) -> GridWindow:
    """AOI bbox (densified) in the grid CRS + halo, snapped OUTWARD to multiples of grid.snap_m from the fixed origin."""
    w, s, e, n = geometry_bounds(geometry_wgs84)
    k = 65
    lons = np.concatenate([np.linspace(w, e, k), np.full(k, e), np.linspace(e, w, k), np.full(k, w)])
    lats = np.concatenate([np.full(k, s), np.linspace(s, n, k), np.full(k, n), np.linspace(n, s, k)])
    x, y = _grid_transformer(grid.crs).transform(lons, lats)
    snap = grid.snap_m

    def lo(v, o):
        return o + math.floor((v - o) / snap) * snap

    def hi(v, o):
        return o + math.ceil((v - o) / snap) * snap

    return GridWindow(lo(x.min() - halo_m, grid.origin_e), lo(y.min() - halo_m, grid.origin_n),
                      hi(x.max() + halo_m, grid.origin_e), hi(y.max() + halo_m, grid.origin_n), grid)


# ---------------------------------------------------------------------------
# Products (verified against 2024 granules; collection numbers differ between SR and geolocation)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class PlatformProducts:
    key: str
    prefix: str
    platform_short_name: str
    sr: str
    sr_concept_id: str
    img: str
    img_concept_id: str
    mod: str
    mod_concept_id: str


PLATFORMS: Dict[str, PlatformProducts] = {
    "snpp": PlatformProducts("snpp", "VNP", "SUOMI-NPP", "VNP09", "C2849291562-LAADS",
                             "VNP03IMG", "C2105092163-LAADS", "VNP03MOD", "C2105092427-LAADS"),
    "noaa20": PlatformProducts("noaa20", "VJ1", "JPSS-1", "VJ109", "C2849305562-LAADS",
                               "VJ103IMG", "C2105086226-LAADS", "VJ103MOD", "C2105084593-LAADS"),
    "noaa21": PlatformProducts("noaa21", "VJ2", "JPSS-2", "VJ209", "C4075706276-LAADS",
                               "VJ203IMG", "C3478476050-LAADS", "VJ203MOD", "C3478458790-LAADS"),
}
SR_COLLECTION = "002"

I_BANDS = ["I1", "I2", "I3"]
M_BANDS = ["M1", "M2", "M3", "M4", "M5", "M7", "M8", "M10", "M11"]
QA_SDS = [f"QF{i} Surface Reflectance" for i in range(1, 8)] + ["land_water_mask"]
QA_NAMES = [f"QF{i}" for i in range(1, 8)] + ["land_water_mask"]
SR_FILL = -28672
SR_VALID = (-100, 16000)
SR_SCALE = 9.999999747378752e-05
N_SCAN_COLS = {"375m": 6400, "750m": 3200}
ROWS_PER_SCAN = {"375m": 32, "750m": 16}
GEOMETRY = ["sensor_zenith", "sensor_azimuth", "solar_zenith", "solar_azimuth"]

# On-board bow-tie deletion zones (row within scan -> deleted columns <a or >=b).
# Measured: SR fill == these zones exactly in all audited granules.
BOWTIE_ZONES = {
    "375m": [((0, 1, 30, 31), 2016, 4384), ((2, 3, 28, 29), 1280, 5120)],
    "750m": [((0, 15), 1008, 2192), ((1, 14), 640, 2560)],
}

# TAI93 epoch (1993-01-01T00:00:00Z) as Unix seconds.
TAI93_UNIX = 725846400.0


def sr_sds_name(band: str) -> str:
    return ("375m Surface Reflectance Band " if band.startswith("I") else "750m Surface Reflectance Band ") + band


class VIIRSL2Error(RuntimeError):
    """Base class (never interpreted as 'no observation')."""


class VIIRSSchemaError(VIIRSL2Error):
    pass


class VIIRSPairingError(VIIRSL2Error):
    pass


# ---------------------------------------------------------------------------
# Filenames
# ---------------------------------------------------------------------------

_NAME_RE = re.compile(r"^(VNP|VJ1|VJ2)(09|03IMG|03MOD)\.A(\d{4})(\d{3})\.(\d{2})(\d{2})\.(\d{3})\.(\d{13})\.(hdf|nc)$")


@dataclass(frozen=True)
class GranuleName:
    name: str
    prefix: str
    kind: str          # "09", "03IMG", "03MOD"
    acq_date: date
    hhmm: str
    collection: str
    production: str

    @property
    def product(self) -> str:
        return self.prefix + self.kind

    @property
    def start(self) -> datetime:
        return datetime(self.acq_date.year, self.acq_date.month, self.acq_date.day,
                        int(self.hhmm[:2]), int(self.hhmm[2:]), tzinfo=timezone.utc)


def parse_granule_name(name: str) -> GranuleName:
    m = _NAME_RE.match(Path(name).name)
    if not m:
        raise ValueError(f"Not a VIIRS L2 SR / geolocation granule name: {name!r}")
    pre, kind, y, doy, hh, mm, coll, prod, _ = m.groups()
    d = date(int(y), 1, 1) + timedelta(days=int(doy) - 1)
    if d.year != int(y):
        raise ValueError(f"Invalid day of year in {name!r}")
    return GranuleName(Path(name).name, pre, kind, d, hh + mm, coll, prod)


def raw_path(raw_dir: Path, name: str) -> Path:
    g = parse_granule_name(name)
    return Path(raw_dir) / g.product / f"{g.acq_date:%Y}" / f"{g.acq_date.timetuple().tm_yday:03d}" / g.name


# ---------------------------------------------------------------------------
# Reading / validation
# ---------------------------------------------------------------------------

def _attr(v: Any) -> Any:
    if isinstance(v, bytes):
        return v.decode()
    a = np.asarray(v)
    if a.dtype.kind in "SU":
        x = a.reshape(-1)[0]
        return x.decode() if isinstance(x, bytes) else str(x)
    return a.reshape(-1)[0].item() if a.size == 1 else a.reshape(-1).tolist()


@dataclass
class SRGranule:
    path: Path
    name: GranuleName
    orbit: int
    start: str
    end: str
    input_pointer: List[str]
    rows_750: int


def read_sr_header(path: Path, platform: PlatformProducts) -> SRGranule:
    """Validate an L2 SR file (HDF4) against the audited schema; return header info. Raises VIIRSSchemaError."""
    from pyhdf.SD import SD, SDC
    name = parse_granule_name(path.name)
    if name.prefix != platform.prefix or name.kind != "09":
        raise VIIRSSchemaError(f"{path.name}: not a {platform.sr} granule")
    try:
        sd = SD(str(path), SDC.READ)
    except Exception as exc:
        raise VIIRSSchemaError(f"{path.name}: cannot be opened as HDF4 ({exc})") from exc
    try:
        ga = sd.attributes()
        for key in ("ShortName", "VersionID", "OrbitNumber", "StartTime", "EndTime", "InputPointer", "PlatformShortName", "DayNightFlag"):
            if key not in ga:
                raise VIIRSSchemaError(f"{path.name}: missing global attribute {key}")
        if ga["ShortName"] != platform.sr or ga["VersionID"] != SR_COLLECTION or ga["PlatformShortName"] != platform.platform_short_name:
            raise VIIRSSchemaError(f"{path.name}: unexpected ShortName/VersionID/Platform "
                                   f"{ga['ShortName']}/{ga['VersionID']}/{ga['PlatformShortName']}")
        datasets = sd.datasets()
        rows_750 = None
        for band in I_BANDS + M_BANDS:
            sds = sr_sds_name(band)
            if sds not in datasets:
                raise VIIRSSchemaError(f"{path.name}: expected SDS '{sds}' not found")
            dims, shape, htype, _ = datasets[sds]
            res = "375m" if band.startswith("I") else "750m"
            if htype != 22 or len(shape) != 2 or shape[1] != N_SCAN_COLS[res] or shape[0] % ROWS_PER_SCAN[res]:
                raise VIIRSSchemaError(f"{path.name}: {sds} shape/type {shape}/{htype} unexpected")
            r750 = shape[0] if res == "750m" else shape[0] // 2
            if rows_750 is None:
                rows_750 = r750
            elif r750 != rows_750:
                raise VIIRSSchemaError(f"{path.name}: I/M along-track rows inconsistent")
            a = sd.select(sds).attributes()
            if (abs(float(a.get("scale_factor", -1)) - SR_SCALE) > 1e-12 or float(a.get("add_offset", 1)) != 0.0
                    or int(a.get("_FillValue", 0)) != SR_FILL or list(a.get("valid_range", [])) != list(SR_VALID)):
                raise VIIRSSchemaError(f"{path.name}: {sds} scale/offset/fill/valid_range differ from the audited schema")
        for q in QA_SDS:
            if q not in datasets:
                raise VIIRSSchemaError(f"{path.name}: expected SDS '{q}' not found")
            _, shape, htype, _ = datasets[q]
            if htype != 21 or tuple(shape) != (rows_750, N_SCAN_COLS["750m"]):
                raise VIIRSSchemaError(f"{path.name}: {q} shape/type {shape}/{htype} unexpected")
        return SRGranule(path, name, int(ga["OrbitNumber"]), ga["StartTime"], ga["EndTime"],
                         [p.strip() for p in ga["InputPointer"].split(",") if p.strip()], rows_750)
    finally:
        sd.end()


def read_geo_header(path: Path) -> Dict[str, Any]:
    try:
        f = h5py.File(path, "r")
    except OSError as exc:
        raise VIIRSSchemaError(f"{path.name}: cannot be opened as NetCDF4/HDF5 ({exc})") from exc
    with f:
        out = {k: _attr(f.attrs[k]) for k in ("OrbitNumber", "StartTime", "EndTime", "ShortName") if k in f.attrs}
        for ds in ("latitude", "longitude") + tuple(GEOMETRY) + ("land_water_mask", "quality_flag"):
            if f"geolocation_data/{ds}" not in f:
                raise VIIRSSchemaError(f"{path.name}: missing /geolocation_data/{ds}")
        if "scan_line_attributes/scan_start_time" not in f:
            raise VIIRSSchemaError(f"{path.name}: missing /scan_line_attributes/scan_start_time")
        out["shape"] = tuple(f["geolocation_data/latitude"].shape)
        out["leapseconds"] = int(_attr(f.attrs["TAI93_leapseconds"])) if "TAI93_leapseconds" in f.attrs else None
        for ds in GEOMETRY:
            a = f[f"geolocation_data/{ds}"].attrs
            if abs(float(_attr(a["scale_factor"])) - 0.01) > 1e-7 or float(_attr(a["add_offset"])) != 0.0:
                raise VIIRSSchemaError(f"{path.name}: {ds} scale/offset unexpected")
    return out


@dataclass
class PairedGranule:
    sr: SRGranule
    img: Path
    mod: Path
    leapseconds: int


def pair_geolocation(sr: SRGranule, platform: PlatformProducts, raw_dir: Path) -> Tuple[str, str]:
    """Geolocation names exactly as recorded in the SR InputPointer (never inferred from version numbers)."""
    names = {}
    for kind, prod in (("img", platform.img), ("mod", platform.mod)):
        hits = [p for p in sr.input_pointer if p.startswith(prod + ".")]
        if len(hits) != 1:
            raise VIIRSPairingError(f"{sr.path.name}: InputPointer lists {len(hits)} {prod} granules")
        g = parse_granule_name(hits[0])
        if g.acq_date != sr.name.acq_date or g.hhmm != sr.name.hhmm:
            raise VIIRSPairingError(f"{sr.path.name}: {hits[0]} acquisition differs (A{g.acq_date} {g.hhmm})")
        names[kind] = hits[0]
    return names["img"], names["mod"]


def verify_pair(sr: SRGranule, img: Path, mod: Path) -> PairedGranule:
    leaps = None
    for path, res in ((img, "375m"), (mod, "750m")):
        h = read_geo_header(path)
        if int(h.get("OrbitNumber", -1)) != sr.orbit or h.get("StartTime") != sr.start or h.get("EndTime") != sr.end:
            raise VIIRSPairingError(f"{sr.path.name} vs {path.name}: orbit/start/end differ "
                                    f"({h.get('OrbitNumber')}, {h.get('StartTime')}, {h.get('EndTime')})")
        want = (sr.rows_750 * (2 if res == "375m" else 1), N_SCAN_COLS[res])
        if tuple(h["shape"]) != want:
            raise VIIRSPairingError(f"{path.name}: geolocation shape {h['shape']} != SR {want}")
        leaps = h["leapseconds"]
    if leaps is None:
        raise VIIRSSchemaError(f"{img.name}: missing TAI93_leapseconds")
    return PairedGranule(sr, img, mod, leaps)


def bowtie_zone(rows: int, res: str) -> np.ndarray:
    """Boolean (rows, cols) mask of the on-board deletion zone."""
    ncol = N_SCAN_COLS[res]
    rin = np.arange(rows) % ROWS_PER_SCAN[res]
    cols = np.arange(ncol)
    z = np.zeros((rows, ncol), bool)
    for scan_rows, a, b in BOWTIE_ZONES[res]:
        z |= np.isin(rin, scan_rows)[:, None] & ((cols < a) | (cols >= b))[None, :]
    return z


# ---------------------------------------------------------------------------
# Source extraction and nearest-neighbour mapping
# ---------------------------------------------------------------------------

@dataclass
class Sources:
    x: np.ndarray
    y: np.ndarray
    half_diag: np.ndarray
    granule: np.ndarray
    row: np.ndarray
    col: np.ndarray
    time_unix: np.ndarray
    data: Dict[str, np.ndarray] = field(default_factory=dict)
    n_bowtie_excluded: int = 0
    n_invalid_geo_excluded: int = 0


def extract_sources(pg: PairedGranule, granule_index: int, res: str, lonlat_box: Tuple[float, float, float, float],
                    crs: str = DEFAULT_ANALYSIS_GRID.crs) -> Sources:
    """Source pixels of one granule inside a lon/lat box, projected to the fixed grid CRS.
    Bow-tie-deleted pixels and pixels without valid geolocation are excluded; other SR fill is kept as fill."""
    from pyhdf.SD import SD, SDC
    bands = I_BANDS if res == "375m" else M_BANDS
    sd = SD(str(pg.sr.path), SDC.READ)
    try:
        sr = {b: sd.select(sr_sds_name(b))[:] for b in bands}
        qa = {n: sd.select(q)[:] for n, q in zip(QA_NAMES, QA_SDS)} if res == "750m" else {}
    finally:
        sd.end()
    with h5py.File(pg.img if res == "375m" else pg.mod, "r") as f:
        g = f["geolocation_data"]
        lat, lon = g["latitude"][()], g["longitude"][()]
        geo_q = g["quality_flag"][()]
        geom = {k: g[k][()] for k in GEOMETRY}
        geo_lwm = g["land_water_mask"][()]
        st = f["scan_line_attributes/scan_start_time"][()]
    rows_n = lat.shape[0]
    fill_all = np.all(np.stack([sr[b] == SR_FILL for b in bands]), axis=0)
    deleted = fill_all & bowtie_zone(rows_n, res)
    bad_geo = (lat < -90) | (lat > 90) | (lon < -180) | (lon > 180) | ((geo_q & 1) != 0)
    w, s, e, n = lonlat_box
    near = (lon >= w) & (lon <= e) & (lat >= s) & (lat <= n)
    keep = near & ~deleted & ~bad_geo
    r, c = np.nonzero(keep)
    tf = _grid_transformer(crs)
    x, y = tf.transform(lon[r, c].astype(np.float64), lat[r, c].astype(np.float64))
    # neighbour for the local footprint size: next pixel, or the previous one on the last row/column of the granule
    # (a clipped "next" pixel would be the pixel itself and collapse the footprint at granule boundaries)
    r1 = np.where(r + 1 < rows_n, r + 1, r - 1)
    c1 = np.where(c + 1 < lat.shape[1], c + 1, c - 1)
    xa, ya = tf.transform(lon[r, c1].astype(np.float64), lat[r, c1].astype(np.float64))
    xt, yt = tf.transform(lon[r1, c].astype(np.float64), lat[r1, c].astype(np.float64))
    half_diag = 0.5 * np.hypot(np.hypot(xa - x, ya - y), np.hypot(xt - x, yt - y))
    scan_idx = np.minimum(r // ROWS_PER_SCAN[res], len(st) - 1)
    t_unix = TAI93_UNIX + st[scan_idx] - pg.leapseconds
    data = {f"sr_{b}": sr[b][r, c] for b in bands}
    data.update({f"qa_{k}": v[r, c] for k, v in qa.items()})
    data.update({f"geom_{k}": geom[k][r, c] for k in GEOMETRY})
    data["geo_land_water_mask"] = geo_lwm[r, c]
    return Sources(x, y, half_diag, np.full(len(r), granule_index, np.int32), r.astype(np.int32), c.astype(np.int32), t_unix,
                   data, int((near & deleted).sum()), int((near & bad_geo & ~deleted).sum()))


def concat_sources(parts: Sequence[Sources]) -> Sources:
    keys = parts[0].data.keys()
    return Sources(*(np.concatenate([getattr(p, a) for p in parts]) for a in ("x", "y", "half_diag", "granule", "row", "col", "time_unix")),
                   data={k: np.concatenate([p.data[k] for p in parts]) for k in keys},
                   n_bowtie_excluded=sum(p.n_bowtie_excluded for p in parts),
                   n_invalid_geo_excluded=sum(p.n_invalid_geo_excluded for p in parts))


@dataclass
class Mapping:
    res: str
    src: np.ndarray        # (ny, nx) index into Sources, -1 = no observation (outside swath / beyond footprint)
    dist: np.ndarray       # source-to-target-centre distance [m], NaN where unmapped
    ties_resolved: int


def nn_map(src: Sources, window: GridWindow, res: str) -> Mapping:
    """Nearest source centre for each target cell centre; accepted only within half the local footprint diagonal.
    Exact distance ties -> lower sensor zenith -> earlier scan time -> lower granule/row/col index (deterministic)."""
    from scipy.spatial import cKDTree
    X, Y = window.centres(res)
    shape = X.shape
    if len(src.x) == 0:
        return Mapping(res, np.full(shape, -1, np.int64), np.full(shape, np.nan), 0)
    tree = cKDTree(np.c_[src.x, src.y])
    k = min(2, len(src.x))
    dist, idx = tree.query(np.c_[X.ravel(), Y.ravel()], k=k)
    if k == 1:
        dist, idx = dist[:, None], idx[:, None]
    best = idx[:, 0].copy()
    ties = 0
    if k == 2:
        tie = np.abs(dist[:, 1] - dist[:, 0]) <= 1e-6
        for t in np.flatnonzero(tie):
            a, b = idx[t, 0], idx[t, 1]
            ka = (src.data["geom_sensor_zenith"][a], src.time_unix[a], src.granule[a], src.row[a], src.col[a])
            kb = (src.data["geom_sensor_zenith"][b], src.time_unix[b], src.granule[b], src.row[b], src.col[b])
            best[t] = a if ka <= kb else b
        ties = int(tie.sum())
    d0 = dist[:, 0]
    ok = d0 <= src.half_diag[best] + 1e-9
    return Mapping(res, np.where(ok, best, -1).reshape(shape), np.where(ok, d0, np.nan).reshape(shape), ties)


# ---------------------------------------------------------------------------
# Overpass products (same-orbit mosaic)
# ---------------------------------------------------------------------------

@dataclass
class Overpass:
    platform: str
    orbit: int
    granules: List[PairedGranule]
    layers: Dict[str, Dict[str, np.ndarray]]     # res -> name -> array on the grid
    stats: Dict[str, Any]

    @property
    def start(self) -> str:
        return min(g.sr.start for g in self.granules)


def _sample(src: Sources, m: Mapping, key: str, fill: Any, dtype: Any) -> np.ndarray:
    if len(src.x) == 0:  # no source pixel near the window (swath elsewhere): every cell is unmapped
        return np.full(m.src.shape, fill, dtype=dtype)
    s = np.maximum(m.src, 0)
    vals = src.data[key][s] if key in src.data else getattr(src, key)[s]
    return np.where(m.src >= 0, vals, fill).astype(dtype)


def build_overpass(platform: str, orbit: int, granules: List[PairedGranule], window: GridWindow,
                   lonlat_box: Tuple[float, float, float, float]) -> Overpass:
    layers: Dict[str, Dict[str, np.ndarray]] = {}
    stats: Dict[str, Any] = {}
    for res in ("375m", "750m"):
        src = concat_sources([extract_sources(g, i, res, lonlat_box, window.grid.crs) for i, g in enumerate(granules)])
        m = nn_map(src, window, res)
        L: Dict[str, np.ndarray] = {}
        for b in (I_BANDS if res == "375m" else M_BANDS):
            dn = _sample(src, m, f"sr_{b}", SR_FILL, np.int16)
            refl = (dn.astype(np.float64) * SR_SCALE).astype(np.float32)
            refl[(dn == SR_FILL) | (dn < SR_VALID[0]) | (dn > SR_VALID[1])] = np.nan
            L[b] = refl
        if res == "750m":
            for q in QA_NAMES:
                L[q] = _sample(src, m, f"qa_{q}", 0, np.uint8)
        L["geo_land_water_mask"] = _sample(src, m, "geo_land_water_mask", 255, np.uint8)
        for g_ in GEOMETRY:
            raw = _sample(src, m, f"geom_{g_}", -32768, np.int16).astype(np.float32)
            L[g_] = np.where(raw == -32768, np.nan, raw * np.float32(0.01))
        L["source_granule"] = _sample(src, m, "granule", -1, np.int32)
        L["source_row"] = _sample(src, m, "row", -1, np.int32)
        L["source_col"] = _sample(src, m, "col", -1, np.int32)
        L["scan_time_unix"] = np.where(m.src >= 0, _sample(src, m, "time_unix", np.nan, np.float64), np.nan)
        L["source_distance_m"] = m.dist.astype(np.float64)
        L["mapped"] = m.src >= 0
        layers[res] = L
        band0 = "I1" if res == "375m" else "M5"
        valid = L["mapped"] & np.isfinite(L[band0])
        uniq, cnt = np.unique(m.src[m.src >= 0], return_counts=True)
        stats[res] = {
            "cells": int(L["mapped"].size), "mapped": int(L["mapped"].sum()), "valid_sr": int(valid.sum()),
            "fill_sr_mapped": int((L["mapped"] & ~np.isfinite(L[band0])).sum()),
            "sources_in_box": int(len(src.x)), "bowtie_excluded": src.n_bowtie_excluded,
            "invalid_geolocation_excluded": src.n_invalid_geo_excluded, "exact_distance_ties": m.ties_resolved,
            "source_distance_m_median_p99_max": [float(np.nanmedian(m.dist)), float(np.nanpercentile(m.dist, 99)), float(np.nanmax(m.dist))] if valid.any() else None,
            "cells_per_used_source_mean": float(cnt.mean()) if cnt.size else None,
        }
    return Overpass(platform, orbit, granules, layers, stats)


def aoi_cell_mask(geometry_wgs84: Dict[str, Any], window: GridWindow, res: str) -> np.ndarray:
    from rasterio.warp import transform_geom
    g = transform_geom("EPSG:4326", window.grid.crs, geometry_wgs84)
    shape = window.shape(res)
    m = geometry_mask([g], out_shape=shape, transform=window.transform(res), invert=True, all_touched=False)
    if not m.any():
        m = geometry_mask([g], out_shape=shape, transform=window.transform(res), invert=True, all_touched=True)
    return m


def aoi_status(ov: Overpass, aoi375: np.ndarray) -> Tuple[str, float]:
    """OBSERVED / PARTIAL_OBSERVATION / AOI_OUTSIDE_SWATH from valid I-band SR inside the AOI polygon (375 m)."""
    valid = ov.layers["375m"]["mapped"] & np.isfinite(ov.layers["375m"]["I1"])
    frac = float(valid[aoi375].mean())
    if frac == 0.0:
        return "AOI_OUTSIDE_SWATH", frac
    return ("OBSERVED" if frac == 1.0 else "PARTIAL_OBSERVATION"), frac


# ---------------------------------------------------------------------------
# Daily best observation (derived from overpass products; never averaged)
# ---------------------------------------------------------------------------

def daily_selection(overpasses: Sequence[Overpass]) -> np.ndarray:
    """Per 750 m cell: index of the chosen overpass (-1 = none), by the deterministic rule
    1 all M bands valid  2 lower QF1 cloud confidence (bits 2-3)  3 no cloud shadow (QF2 bit 3)
    4 lower sensor zenith  5 earlier scan time. QF6 (ambiguous bit definition) is not used."""
    keys = []
    for ov in overpasses:
        L = ov.layers["750m"]
        valid = L["mapped"] & np.all(np.stack([np.isfinite(L[b]) for b in M_BANDS]), axis=0)
        cloud = ((L["QF1"] >> 2) & 3).astype(np.float64)
        shadow = ((L["QF2"] >> 3) & 1).astype(np.float64)
        keys.append(np.stack([np.where(valid, 0.0, 1.0), np.where(valid, cloud, 9.0), np.where(valid, shadow, 9.0),
                              np.where(valid, L["sensor_zenith"], 999.0), np.where(valid, L["scan_time_unix"], np.inf)]))
    K = np.stack(keys)                                   # (n_overpass, 5, ny, nx)
    order = np.lexsort(tuple(K[:, k] for k in range(4, -1, -1)), axis=0)  # last key = primary
    best = order[0]
    none = np.all(K[:, 0] == 1.0, axis=0)
    return np.where(none, -1, best)


def daily_layers(overpasses: Sequence[Overpass], sel750: np.ndarray) -> Dict[str, Dict[str, np.ndarray]]:
    """Assemble the daily product: each 750 m cell and its 2x2 375 m cells take the SAME overpass."""
    sel375 = np.repeat(np.repeat(sel750, 2, axis=0), 2, axis=1)
    out: Dict[str, Dict[str, np.ndarray]] = {}
    for res, sel in (("375m", sel375), ("750m", sel750)):
        names = overpasses[0].layers[res].keys()
        L = {}
        for nme in names:
            stack = np.stack([ov.layers[res][nme] for ov in overpasses])
            pick = np.take_along_axis(stack, np.maximum(sel, 0)[None], axis=0)[0]
            if pick.dtype.kind == "f":
                pick = np.where(sel >= 0, pick, np.nan)
            elif nme == "mapped":
                pick = np.where(sel >= 0, pick, False)
            else:
                pick = np.where(sel >= 0, pick, {np.dtype("uint8"): 0, np.dtype("int16"): SR_FILL}.get(pick.dtype, -1))
            L[nme] = pick.astype(stack.dtype)
        L["selected_orbit"] = np.where(sel >= 0, np.array([ov.orbit for ov in overpasses])[np.maximum(sel, 0)], -1).astype(np.int32)
        out[res] = L
    return out


# ---------------------------------------------------------------------------
# GeoTIFF output
# ---------------------------------------------------------------------------

GROUPS = {
    "375m": ("375m", I_BANDS, "float32"),
    "750m": ("750m", M_BANDS, "float32"),
    "qa": ("750m", QA_NAMES, "uint8"),
    "qa_375m": ("375m", ["geo_land_water_mask"], "uint8"),
    "geometry_375m": ("375m", GEOMETRY, "float32"),
    "geometry_750m": ("750m", GEOMETRY, "float32"),
    "provenance_375m": ("375m", ["source_granule", "source_row", "source_col", "scan_time_unix", "source_distance_m"], "float64"),
    "provenance_750m": ("750m", ["source_granule", "source_row", "source_col", "scan_time_unix", "source_distance_m"], "float64"),
}
GROUP_DIR = {"375m": "375m", "750m": "750m", "qa": "qa", "qa_375m": "qa", "geometry_375m": "geometry",
             "geometry_750m": "geometry", "provenance_375m": "provenance", "provenance_750m": "provenance"}


def _write(path: Path, arrays: List[np.ndarray], names: List[str], dtype: str, window: GridWindow, res: str,
           mapped: np.ndarray, tags: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    ny, nx = window.shape(res)
    prof: Dict[str, Any] = {"driver": "GTiff", "height": ny, "width": nx, "count": len(arrays), "dtype": dtype,
                            "crs": CRS.from_string(window.grid.crs), "transform": window.transform(res), "compress": "lzw"}
    if dtype.startswith("float"):
        prof["nodata"] = float("nan")
    tmp = path.with_name(path.name + ".part.tif")
    with rasterio.open(tmp, "w", **prof) as dst:
        for i, (a, n) in enumerate(zip(arrays, names), start=1):
            if dtype.startswith("float"):
                a = np.where(mapped, a, np.nan)
            dst.write(a.astype(dtype), i)
            dst.set_band_description(i, n)
        dst.write_mask(mapped.astype(np.uint8) * np.uint8(255))
        dst.update_tags(**{k: (json.dumps(v) if isinstance(v, (list, dict)) else str(v)) for k, v in tags.items()})
    os.replace(tmp, path)


def write_product(out_dir: Path, stem: str, layers: Dict[str, Dict[str, np.ndarray]], window: GridWindow,
                  tags: Dict[str, Any], extra_prov: bool = False) -> List[str]:
    written = []
    for group, (res, names, dtype) in GROUPS.items():
        names = list(names) + (["selected_orbit"] if extra_prov and group.startswith("provenance") else [])
        L = layers[res]
        path = out_dir / GROUP_DIR[group] / f"{stem}_{group}.tif"
        t = dict(tags)
        g = window.grid
        t.update(group=group, analysis_grid=res, grid_cell_m=g.cell_m[res], grid_name=g.name,
                 grid_definition=f"{g.crs}, origin E={g.origin_e} N={g.origin_n}, edges at multiples of {g.cell_m[res]:.0f} m",
                 scale_applied=(dtype == "float32" and group in ("375m", "750m")),
                 nodata_meaning="dataset mask 0 = no source observation within the footprint (outside swath)")
        if group in ("375m", "750m"):
            t["reflectance_type"] = "surface reflectance"
            t["band_source"] = ("nominal 375 m I bands (IMG geolocation) mapped to the fixed 375-m analysis grid" if res == "375m"
                                else "nominal 750 m M bands (MOD geolocation) mapped to the fixed 750-m analysis grid")
            t["note_resolution"] = "grid spacing is not the effective resolution; see source_distance_m and sensor_zenith"
        if group == "qa":
            t["note"] = "raw QF1-QF7 bytes and land_water_mask, unscaled; QF6 bit definition partly UNRESOLVED (file text duplicates bit 2)"
        _write(path, [L[n] for n in names], names, dtype, window, res, L["mapped"], t)
        written.append(str(path))
    return written


# ---------------------------------------------------------------------------
# Acquisition (CMR via earthaccess, LAADS HTTPS) with raw cache
# ---------------------------------------------------------------------------

def _download(url: str, dest: Path, session: Any) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")

    def once() -> None:
        try:
            with session.get(url, stream=True, timeout=(30, 600)) as r:
                r.raise_for_status()
                expected = int(r.headers.get("Content-Length", -1))
                with open(part, "wb") as fh:
                    for chunk in r.iter_content(8 << 20):
                        fh.write(chunk)
            got = part.stat().st_size
            if expected >= 0 and got != expected:
                raise ConnectionError(f"incomplete download {dest.name}: {got} != {expected}")
        except BaseException:
            if part.exists():
                part.unlink()
            raise

    call_with_network_retry(once, service="LAADS-download")
    if dest.suffix == ".hdf":
        with open(part, "rb") as fh:
            sig = fh.read(4)
        if sig != b"\x0e\x03\x13\x01":
            part.unlink()
            raise VIIRSSchemaError(f"{dest.name}: downloaded file is not HDF4")
    else:
        with open(part, "rb") as fh:
            if fh.read(8) != b"\x89HDF\r\n\x1a\n":
                part.unlink()
                raise VIIRSSchemaError(f"{dest.name}: downloaded file is not HDF5/NetCDF4")
    os.replace(part, dest)


class LAADSClient:
    """Thin wrapper over earthaccess for CMR search and authenticated HTTPS download."""

    def __init__(self) -> None:
        import earthaccess
        self._ea = earthaccess
        self._session = None

    def session(self):
        if self._session is None:
            self._session = self._ea.get_requests_https_session()
        return self._session

    def search(self, concept_id: str, start: date, end: date, bbox: Tuple[float, float, float, float]) -> List[Tuple[str, str, str]]:
        """[(granule name, https url, day/night flag)]"""
        res = call_with_network_retry(lambda: self._ea.search_data(
            concept_id=concept_id, temporal=(f"{start}T00:00:00Z", f"{end}T23:59:59Z"), bounding_box=bbox), service="CMR")
        out = []
        for g in res:
            urls = [u for u in g.data_links() if u.startswith("https")]
            name = urls[0].rsplit("/", 1)[1]
            out.append((name, urls[0], g["umm"].get("DataGranule", {}).get("DayNightFlag", "")))
        return out

    def find_by_name(self, concept_id: str, name: str) -> str:
        res = call_with_network_retry(lambda: self._ea.search_data(concept_id=concept_id, granule_name=name), service="CMR")
        urls = [u for g in res for u in g.data_links() if u.startswith("https") and u.endswith(name)]
        if len(urls) != 1:
            raise VIIRSPairingError(f"CMR returned {len(urls)} URLs for geolocation granule {name}")
        return urls[0]

    def fetch(self, url: str, dest: Path) -> None:
        _download(url, dest, self.session())


def _ensure(raw_dir: Path, name: str, url_fn: Callable[[], str], client: Any) -> Path:
    dest = raw_path(raw_dir, name)
    if not dest.exists():
        client.fetch(url_fn(), dest)
    return dest


# ---------------------------------------------------------------------------
# Pipeline entry point
# ---------------------------------------------------------------------------

def _date_range(a: date, b: date) -> List[date]:
    return [a + timedelta(days=i) for i in range((b - a).days + 1)]


def process_platform_day(platform: PlatformProducts, day: date, sr_entries: List[Tuple[str, str, str]], raw_dir: Path,
                         client: Any, geometry_wgs84: Dict[str, Any], window: GridWindow, out_root: Path,
                         file_exists_mode: str = "skip") -> Dict[str, Any]:
    """All daytime overpasses of one platform/day -> overpass products + daily best product."""
    tf_inv = _grid_transformer(window.grid.crs, inverse=True)
    lo, la = tf_inv.transform([window.x0, window.x1, window.x0, window.x1], [window.y0, window.y0, window.y1, window.y1])
    margin = 0.05
    box = (min(lo) - margin, min(la) - margin, max(lo) + margin, max(la) + margin)
    aoi375 = aoi_cell_mask(geometry_wgs84, window, "375m")
    summary: Dict[str, Any] = {"date": day.isoformat(), "platform": platform.key, "overpasses": [], "daily": None}
    paired: List[PairedGranule] = []
    for name, url, dn in sorted(sr_entries):
        if dn and dn.upper() != "DAY":
            continue
        sr_path = _ensure(raw_dir, name, lambda u=url: u, client)
        sr = read_sr_header(sr_path, platform)
        img_name, mod_name = pair_geolocation(sr, platform, raw_dir)
        img = _ensure(raw_dir, img_name, lambda n=img_name: client.find_by_name(platform.img_concept_id, n), client)
        mod = _ensure(raw_dir, mod_name, lambda n=mod_name: client.find_by_name(platform.mod_concept_id, n), client)
        paired.append(verify_pair(sr, img, mod))
    by_orbit: Dict[int, List[PairedGranule]] = {}
    for p in paired:
        by_orbit.setdefault(p.sr.orbit, []).append(p)
    overpasses: List[Overpass] = []
    base = out_root / "viirs" / "surface_reflectance" / platform.key / "l2_swath"
    for orbit, gs in sorted(by_orbit.items()):
        gs = sorted(gs, key=lambda p: p.sr.start)
        ov = build_overpass(platform.key, orbit, gs, window, box)
        status, frac = aoi_status(ov, aoi375)
        rec = {"orbit": orbit, "granules": [g.sr.path.name for g in gs],
               "geolocation": [[g.img.name, g.mod.name] for g in gs],
               "start_utc": gs[0].sr.start, "end_utc": gs[-1].sr.end, "aoi_status": status,
               "aoi_valid_fraction_375m": frac, "stats": ov.stats, "files": []}
        if status != "AOI_OUTSIDE_SWATH":
            stem = f"{platform.sr}_{platform.key}_{day:%Y%m%d}_{gs[0].sr.name.hhmm}_o{orbit}"
            tags = {"product": platform.sr, "collection": SR_COLLECTION, "platform": platform.key, "orbit": orbit,
                    "acquisition_start_utc": gs[0].sr.start, "acquisition_end_utc": gs[-1].sr.end,
                    "source_granules": [g.sr.path.name for g in gs], "geolocation_granules": [[g.img.name, g.mod.name] for g in gs],
                    "mapping": "nearest neighbour of source pixel centres (terrain-corrected IMG/MOD geolocation) to fixed-grid cell centres; "
                               "accepted within half the local source footprint diagonal; bow-tie-deleted pixels excluded; same-orbit granules mosaicked in one KD-tree",
                    "description": "nominal 375-m / 750-m VIIRS L2 observations mapped to a fixed 375-m / 750-m analysis grid"}
            rec["files"] = write_product(base / "overpass", stem, ov.layers, window, tags)
            overpasses.append(ov)
        else:
            LOGGER.info("VIIRS L2 %s orbit %s on %s: AOI outside swath", platform.key, orbit, day)
        summary["overpasses"].append(rec)
    if overpasses:
        sel = daily_selection(overpasses)
        layers = daily_layers(overpasses, sel)
        stem = f"{platform.sr}_{platform.key}_{day:%Y%m%d}_daily"
        tags = {"product": platform.sr, "platform": platform.key, "date": day.isoformat(),
                "candidate_orbits": [ov.orbit for ov in overpasses],
                "selection_rule": "per 750 m cell: all M bands valid > lower QF1 cloud confidence > no QF2 shadow > lower sensor zenith > earlier scan; "
                                  "the 2x2 375 m cells use the same overpass; no averaging",
                "description": "daily best observation derived from overpass products"}
        files = write_product(base / "daily", stem, layers, window, tags, extra_prov=True)
        u, c = np.unique(sel[sel >= 0], return_counts=True)
        summary["daily"] = {"files": files, "selected_orbits": {str(overpasses[i].orbit): int(n) for i, n in zip(u, c)}}
    else:
        LOGGER.info("VIIRS L2 %s: no observation for AOI on %s", platform.key, day)
    jp = base / "summary" / f"{platform.sr}_{platform.key}_{day:%Y%m%d}.json"
    jp.parent.mkdir(parents=True, exist_ok=True)
    tmp = jp.with_name(jp.name + ".part")
    tmp.write_text(json.dumps(summary, indent=1, default=str), encoding="utf-8")
    os.replace(tmp, jp)
    return summary


def _process_viirs_l2_swath(config: Dict[str, Any], config_dir: Path, output_root: Path, geometry_wgs84: Dict[str, Any],
                            bbox: Tuple[float, float, float, float], start_date: date, end_date: date,
                            *, client: Any = None) -> Dict[str, Any]:
    from .surface_reflectance import _normalize_viirs_platforms
    sr_cfg = config.get("surface_reflectance", {}) or {}
    platforms = _normalize_viirs_platforms(sr_cfg.get("viirs_platforms"))
    raw_value = sr_cfg.get("viirs_l2_raw_dir") or os.environ.get("VIIRS_L2_RAW_DIR")
    raw_dir = _resolve_runtime_path(str(raw_value), config_dir) if raw_value else output_root / "viirs" / "_raw_l2"
    file_exists_mode = str(config.get("file_exists", "skip")).strip().lower()
    window = grid_window_for_geometry(geometry_wgs84)
    tf_inv = _grid_transformer(window.grid.crs, inverse=True)
    lo, la = tf_inv.transform([window.x0, window.x1, window.x0, window.x1], [window.y0, window.y0, window.y1, window.y1])
    search_bbox = (min(lo), min(la), max(lo), max(la))
    result: Dict[str, Any] = {"grid": {"name": window.grid.name, "crs": window.grid.crs, "origin": [window.grid.origin_e, window.grid.origin_n],
                                       "window": [window.x0, window.y0, window.x1, window.y1],
                                       "shape_375m": window.shape("375m"), "shape_750m": window.shape("750m")},
                              "raw_dir": str(raw_dir)}
    client = client or LAADSClient()
    for key in platforms:
        p = PLATFORMS[key]
        res: Dict[str, Any] = {"days": [], "failed": []}
        try:
            entries = client.search(p.sr_concept_id, start_date, end_date, search_bbox)
        except Exception as exc:
            LOGGER.error("CMR search failed for %s: %s", p.sr, exc, exc_info=True)
            res["error"] = str(exc)
            result[key] = res
            continue
        by_day: Dict[date, List[Tuple[str, str, str]]] = {}
        for e in entries:
            by_day.setdefault(parse_granule_name(e[0]).acq_date, []).append(e)
        for d in _date_range(start_date, end_date):
            jp = output_root / "viirs" / "surface_reflectance" / key / "l2_swath" / "summary" / f"{p.sr}_{key}_{d:%Y%m%d}.json"
            if file_exists_mode == "skip" and jp.exists():
                res["days"].append({"date": d.isoformat(), "skipped": True})
                continue
            if not by_day.get(d):
                LOGGER.info("VIIRS L2 %s: no L2 SR granule for AOI on %s", key, d)
                res["days"].append({"date": d.isoformat(), "no_granule": True})
                continue
            try:
                res["days"].append(process_platform_day(p, d, by_day[d], raw_dir, client, geometry_wgs84, window, output_root, file_exists_mode))
            except VIIRSL2Error as exc:
                LOGGER.error("VIIRS L2 %s %s: %s", key, d, exc)
                res["failed"].append({"date": d.isoformat(), "error": str(exc)})
        result[key] = res
    return result
