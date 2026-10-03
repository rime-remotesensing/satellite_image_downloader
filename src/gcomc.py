"""GCOM-C/SGLI Level-2 LAND RSRF (atmospheric corrected reflectance) downloader.

Independent of the NASA Earthdata MODIS/VIIRS path in surface_reflectance.py.
Every constant below was verified against real RSRF version 3002 files and the
new JAXA G-Portal (2026-10) -- see docs/gcomc_rsrf_smoke_test.md. Nothing here
searches for "similar" dataset names: an unexpected schema is a hard error.

Three independent questions are answered by three independent sources:
  CSW (catalog)      -> does a product record exist?          (NO_RECORD)
  SFTP               -> can the file actually be retrieved?   (AVAILABLE / REQUEST_REQUIRED / ERROR)
  HDF5 + AOI polygon -> is the AOI actually observed?         (OBSERVED / PARTIAL_OBSERVATION / AOI_NO_DATA)
"""

from __future__ import annotations

import base64
import calendar
import hashlib
import json
import logging
import math
import os
import re
import socket
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

import h5py
import numpy as np
import rasterio
from rasterio.crs import CRS
from rasterio.features import bounds as geometry_bounds, geometry_mask
from rasterio.transform import Affine
from rasterio.warp import transform_geom

from .config import _load_env_kv_file, _resolve_runtime_path
from .network_retry import call_with_network_retry
from .surface_reflectance import _aoi_bbox_geometry_in_crs

LOGGER = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Product / access constants (verified 2026-10-02)
# ---------------------------------------------------------------------------
GCOMC_CSW_URL = "https://csw.gportal.jaxa.jp/csw"
GCOMC_RSRF_DATASET_ID = "10002015"
GCOMC_RSRF_PRODUCT_VERSION = "3002"
GCOMC_ORBIT_DIRECTION = "D"  # descending = daytime; ascending (A) RSRF tiles are all Error_DN

GCOMC_SFTP_HOST = "sftp.gportal.jaxa.jp"
GCOMC_SFTP_PORT = 22
GCOMC_SFTP_HOST_KEY_SHA256 = "pYLmuMNFi9tQYRAZLXERRuyOszorS0qJQEE/u4Co+xM"
# Fixed in-container location of the read-only bind-mounted private key
# (docker-compose.yml, service "gcomc"). The host path is never read by Python.
GCOMC_PRIVATE_KEY_PATH = "/run/secrets/gportal_privatekey.key"
# Real SFTP root. The CSW JSON field "product.fileName" says "/products/..."
# which does NOT exist on the server; only the part after "/Standard/" is used.
GCOMC_SFTP_PRODUCT_ROOT = "/product/Standard/"
GCOMC_RSRF_PRODUCT_DIR = "GCOM-C/GCOM-C.SGLI/L2.LAND.RSRF"
# Official notice: L2 products observed more than 2.5 years before the download
# day need a per-file Web request. Used ONLY to label a file the SFTP server
# actually refuses or lacks; never to skip a file that SFTP serves.
# The exact boundary definition is UNRESOLVED (30 calendar months used).
GCOMC_REQUEST_PERIOD_MONTHS = 30

HDF5_SIGNATURE = b"\x89HDF\r\n\x1a\n"

# ---------------------------------------------------------------------------
# EQA tile grid. 36 x 18 tiles of 10 deg at the equator, sinusoidal equal-area
# centred on lon 0, origin lat 90 / lon -180. Tile id "Tvvhh".
# Pixel geolocation follows the JAXA GeoTIFF conversion tool definition
# (Map projection & GeoTIFF conversion Tool User's Manual v1.2, App. 7.1):
#   x = m/10 * (lon*cos(lat) + 180 - 10h) + 0.5,  y = n/10 * (90 - lat - 10v) + 0.5
# (pixel centre of the first pixel = 1; m = n = 4800 / 1200). A single affine
# on a spherical sinusoidal CRS reproduces it exactly (audited < 1e-10 px).
# The Handbook 4.1.4.1 variant with NP_i = NINT(NP0*cos(lat)) differs by up to
# 0.22 px E-W, but the RSRF v3002 data were shown empirically to follow the
# continuous definition (docs/gcomc_rsrf_smoke_test.md section 7).
# Sphere radius: chosen so the metre coordinates equal those of JAXA's own L2
# EQA-tile GeoTIFFs (Higher Level Product Format Description, Attached Sheet
# GeoTIFF Tag List: PCS "Sphere_Sinusoidal", ModelPixelScale 231.65635827 m
# (250 m) / 926.62543306 m (1 km)  ->  R = 231.65635827 * 480 * 180/pi
# = 6371007.181 m). (The same sheet's GeogGeodeticDatum 6035 "DatumE_Sphere"
# nominally denotes a 6371000 m sphere; the pixel-scale value is followed
# because it defines the coordinates.) Lat/lon of pixel centres do not depend
# on R. Derived from the JAXA tag, not taken from the MODIS constants.
# ---------------------------------------------------------------------------
EQA_TILE_DEG = 10.0
EQA_N_TILES_H = 36
EQA_N_TILES_V = 18
EQA_SPHERE_RADIUS_M = 6371007.181
# Pixels per tile side for each native grid (real files: 4800 / 1200).
GCOMC_TILE_PIXELS: Dict[str, int] = {"250m": 4800, "1km": 1200}

# QA_flag bit definition, exactly as stored in the audited files' Data_description.
GCOMC_QA_FLAG_DESCRIPTION = [
    "Bit00:no available data   ;", "Bit01:land                ;",
    "Bit02:coast               ;", "Bit03:sunglint flag>0.005 ;",
    "Bit04:sunglint mask>0.12  ;", "Bit05:snow or ice         ;",
    "Bit06:cloud               ;", "Bit07:probably cloud      ;",
    "Bit08:high tau-a>0.8      ;", "Bit09:saturation recovery ;",
    "Bit10:BRF samples<=3      ;", "Bit11:straylight flag     ;",
    "Bit12:shadow              ;", "Bit13:pol cloud or hi-tau ;",
    "Bit14:recovery by pre-days;", "Bit15:recovery (pol)      ;",
]
QA_BIT_NO_DATA = 0


@dataclass(frozen=True)
class FieldSpec:
    name: str            # output folder / band name
    hdf5_path: str       # exact dataset path in the RSRF HDF5 file
    grid: str            # native grid: "250m" or "1km"
    group: str           # output group folder: "250m" / "1km" / "qa"
    kind: str            # "reflectance" (scaled float32) or "raw" (integer, never scaled)
    dtype: str           # expected on-disk dtype
    description: str     # expected Data_description (exact)
    reflectance_type: Optional[str] = None


def _refl(band: str, grid: str, rtype: str = "surface reflectance") -> FieldSpec:
    prefix = "TOA reflectance" if rtype == "TOA reflectance" else "Surface reflectance"
    return FieldSpec(band, f"Image_data/Rs_{band}", grid, grid, "reflectance", "uint16", f"{prefix} of {band}", rtype)


GCOMC_REFLECTANCE_250M = [_refl(f"VN{i:02d}", "250m") for i in range(1, 12)] + [_refl("SW03", "250m")]
GCOMC_REFLECTANCE_1KM = [
    _refl("SW01", "1km"),
    _refl("SW02", "1km", "TOA reflectance"),  # official: TOA, not surface, reflectance
    _refl("SW04", "1km"),
]
GCOMC_RAW_FIELDS = [
    FieldSpec("QA_flag", "Image_data/QA_flag", "250m", "qa", "raw", "uint16", "<QA bit list>"),
    FieldSpec("Land_water_flag", "Image_data/Land_water_flag", "250m", "qa", "raw", "uint8", "Land water flag: 0(water)-100(land)"),
    FieldSpec("Obs_time", "Geometry_data/Obs_time", "250m", "qa", "raw", "int16", "Observation time (hour)"),
]
GCOMC_FIELDS: List[FieldSpec] = GCOMC_REFLECTANCE_250M + GCOMC_REFLECTANCE_1KM + GCOMC_RAW_FIELDS
_QA_FIELD = GCOMC_RAW_FIELDS[0]

_REQUIRED_FIELD_ATTRS = ("Slope", "Offset", "Error_DN", "Minimum_valid_DN", "Maximum_valid_DN", "Data_description")

PRODUCT_AVAILABLE = "AVAILABLE"
PRODUCT_REQUEST_REQUIRED = "REQUEST_REQUIRED"
PRODUCT_NO_RECORD = "NO_RECORD"
PRODUCT_ERROR = "ERROR"
AOI_OBSERVED = "OBSERVED"
AOI_PARTIAL = "PARTIAL_OBSERVATION"
AOI_NO_DATA = "AOI_NO_DATA"


class GCOMCError(RuntimeError):
    """Base class for GCOM-C errors (never interpreted as 'no observation')."""


class GCOMCAuthError(GCOMCError):
    """G-Portal SFTP authentication / host-key / credential problem (hard error)."""


class GCOMCSchemaError(GCOMCError):
    """Unexpected HDF5 content: missing dataset/attribute, unknown version, shape/dtype mismatch."""


class GCOMCPathError(GCOMCError):
    """CSW path and expected SFTP path are inconsistent."""


class GCOMCCatalogError(GCOMCError):
    """Unexpected or inconsistent CSW response."""


# ---------------------------------------------------------------------------
# Filename / identifier parsing
# ---------------------------------------------------------------------------

_IDENTIFIER_RE = re.compile(r"^GC1SG1_(\d{8})([AD])01D_T(\d{2})(\d{2})_L2SG_RSRFQ_(\d)(\d{3})(?:\.h5)?$")


@dataclass(frozen=True)
class RSRFGranule:
    identifier: str
    observation_date: date   # observation start date (UTC) encoded in the granule id
    orbit_direction: str     # "A" or "D"
    tile_v: int
    tile_h: int
    version: str             # e.g. "3002"
    algorithm_version: str   # e.g. "3"
    parameter_version: str   # e.g. "002"

    @property
    def filename(self) -> str:
        return f"{self.identifier}.h5"

    @property
    def tile_id(self) -> str:
        return f"T{self.tile_v:02d}{self.tile_h:02d}"


def parse_rsrf_identifier(text: str) -> RSRFGranule:
    """Parse an RSRF granule id or filename, e.g. GC1SG1_20240315D01D_T0528_L2SG_RSRFQ_3002(.h5)."""
    m = _IDENTIFIER_RE.match(Path(str(text)).name)
    if not m:
        raise ValueError(f"Not a GCOM-C L2 RSRF 250 m daily tile granule id: {text!r}")
    ymd, orbit, vv, hh, alg, par = m.groups()
    obs = datetime.strptime(ymd, "%Y%m%d").date()
    v, h = int(vv), int(hh)
    if not (0 <= v < EQA_N_TILES_V and 0 <= h < EQA_N_TILES_H):
        raise ValueError(f"Tile out of EQA grid range in {text!r}")
    ident = Path(str(text)).name
    if ident.endswith(".h5"):
        ident = ident[:-3]
    return RSRFGranule(ident, obs, orbit, v, h, alg + par, alg, par)


def parse_tile_id(tile: str) -> Tuple[int, int]:
    m = re.fullmatch(r"T(\d{2})(\d{2})", tile)
    if not m:
        raise ValueError(f"Invalid SGLI tile id: {tile!r}")
    return int(m.group(1)), int(m.group(2))


# ---------------------------------------------------------------------------
# EQA tile grid
# ---------------------------------------------------------------------------

def eqa_crs() -> CRS:
    return CRS.from_proj4(f"+proj=sinu +lon_0=0 +x_0=0 +y_0=0 +R={EQA_SPHERE_RADIUS_M} +units=m +no_defs")


def eqa_pixel_size_m(grid: str) -> float:
    return EQA_SPHERE_RADIUS_M * math.radians(EQA_TILE_DEG / GCOMC_TILE_PIXELS[grid])


def _eqa_origin() -> Tuple[float, float]:
    return EQA_SPHERE_RADIUS_M * math.radians(-180.0), EQA_SPHERE_RADIUS_M * math.radians(90.0)


def eqa_tile_transform(tile_v: int, tile_h: int, grid: str) -> Affine:
    px = eqa_pixel_size_m(grid)
    x0, y0 = _eqa_origin()
    n = GCOMC_TILE_PIXELS[grid]
    return Affine(px, 0.0, x0 + tile_h * n * px, 0.0, -px, y0 - tile_v * n * px)


def tiles_for_bbox(west: float, south: float, east: float, north: float) -> List[Tuple[int, int]]:
    """EQA tiles (v, h) intersecting a lon/lat rectangle, computed exactly on the EQA grid.

    In the EQA plane x = lon*cos(lat), y = lat, so for each tile row the x-extent
    of the rectangle is bounded by W*cos(lat) and E*cos(lat) over that row's
    latitude interval (checked at its ends and at the equator if included).
    """
    if not (-180.0 <= west < east <= 180.0) or not (-90.0 <= south < north <= 90.0):
        raise ValueError(f"Unsupported AOI bounds (antimeridian crossing or invalid): {(west, south, east, north)}")
    eps = 1e-12
    v0 = max(0, int(math.floor((90.0 - north) / EQA_TILE_DEG)))
    v1 = min(EQA_N_TILES_V - 1, int(math.ceil((90.0 - south) / EQA_TILE_DEG)) - 1)
    tiles: List[Tuple[int, int]] = []
    for v in range(v0, v1 + 1):
        lat_hi = min(north, 90.0 - EQA_TILE_DEG * v)
        lat_lo = max(south, 90.0 - EQA_TILE_DEG * (v + 1))
        if lat_lo >= lat_hi:
            continue
        lats = [lat_lo, lat_hi] + ([0.0] if lat_lo < 0.0 < lat_hi else [])
        xs = [lon * math.cos(math.radians(lat)) for lon in (west, east) for lat in lats]
        h0 = int(math.floor((min(xs) + 180.0) / EQA_TILE_DEG + eps))
        h1 = int(math.ceil((max(xs) + 180.0) / EQA_TILE_DEG - eps)) - 1
        for h in range(max(0, h0), min(EQA_N_TILES_H - 1, h1) + 1):
            tiles.append((v, h))
    return tiles


def tiles_for_geometry(geometry_wgs84: Dict[str, Any]) -> List[Tuple[int, int]]:
    return tiles_for_bbox(*geometry_bounds(geometry_wgs84))


def _densify_geometry(geometry: Dict[str, Any], max_step_deg: float = 0.0005) -> Dict[str, Any]:
    def ring(coords: Sequence[Sequence[float]]) -> List[List[float]]:
        out: List[List[float]] = []
        for (x0, y0), (x1, y1) in zip(coords[:-1], coords[1:]):
            n = max(1, int(math.ceil(max(abs(x1 - x0), abs(y1 - y0)) / max_step_deg)))
            out.extend([[x0 + (x1 - x0) * i / n, y0 + (y1 - y0) * i / n] for i in range(n)])
        out.append(list(coords[-1]))
        return out

    if geometry["type"] == "Polygon":
        return {"type": "Polygon", "coordinates": [ring(r) for r in geometry["coordinates"]]}
    if geometry["type"] == "MultiPolygon":
        return {"type": "MultiPolygon", "coordinates": [[ring(r) for r in poly] for poly in geometry["coordinates"]]}
    raise ValueError(f"Unsupported AOI geometry type: {geometry['type']}")


@dataclass(frozen=True)
class GlobalWindow:
    """Half-open window [row0,row1) x [col0,col1) in global EQA pixel indices of one grid."""
    grid: str
    row0: int
    row1: int
    col0: int
    col1: int

    @property
    def shape(self) -> Tuple[int, int]:
        return self.row1 - self.row0, self.col1 - self.col0

    @property
    def transform(self) -> Affine:
        px = eqa_pixel_size_m(self.grid)
        x0, y0 = _eqa_origin()
        return Affine(px, 0.0, x0 + self.col0 * px, 0.0, -px, y0 - self.row0 * px)

    def tile_slices(self, tile_v: int, tile_h: int) -> Optional[Tuple[slice, slice, slice, slice]]:
        """(tile rows, tile cols, window rows, window cols) of the overlap with one tile, or None."""
        n = GCOMC_TILE_PIXELS[self.grid]
        r0, r1 = max(self.row0, tile_v * n), min(self.row1, (tile_v + 1) * n)
        c0, c1 = max(self.col0, tile_h * n), min(self.col1, (tile_h + 1) * n)
        if r0 >= r1 or c0 >= c1:
            return None
        return (slice(r0 - tile_v * n, r1 - tile_v * n), slice(c0 - tile_h * n, c1 - tile_h * n),
                slice(r0 - self.row0, r1 - self.row0), slice(c0 - self.col0, c1 - self.col0))


def native_window_for_bounds(bounds_native: Tuple[float, float, float, float], grid: str) -> GlobalWindow:
    """Smallest window of whole native pixels covering the bounds (snapped outward)."""
    minx, miny, maxx, maxy = bounds_native
    px = eqa_pixel_size_m(grid)
    x0, y0 = _eqa_origin()
    n_rows = GCOMC_TILE_PIXELS[grid] * EQA_N_TILES_V
    n_cols = GCOMC_TILE_PIXELS[grid] * EQA_N_TILES_H
    c0 = max(0, int(math.floor((minx - x0) / px)))
    c1 = min(n_cols, int(math.ceil((maxx - x0) / px)))
    r0 = max(0, int(math.floor((y0 - maxy) / px)))
    r1 = min(n_rows, int(math.ceil((y0 - miny) / px)))
    if c0 >= c1 or r0 >= r1:
        raise ValueError("AOI bounds do not intersect the EQA grid")
    return GlobalWindow(grid, r0, r1, c0, c1)


# ---------------------------------------------------------------------------
# CSW catalog
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class CSWRecord:
    granule: RSRFGranule
    csw_path: str
    csw_size_bytes: Optional[int]
    begin_position: str
    end_position: str
    cloud_cover_pct: Optional[float]


def parse_csw_response(payload: Dict[str, Any]) -> List[CSWRecord]:
    """Parse a CSW GetRecords GeoJSON response into RSRF records (raises on inconsistent records)."""
    if not isinstance(payload, dict) or payload.get("type") != "FeatureCollection":
        raise GCOMCCatalogError("CSW response is not a GeoJSON FeatureCollection")
    records: List[CSWRecord] = []
    for feature in payload.get("features") or []:
        props = feature.get("properties") or {}
        gpp = props.get("gpp") or {}
        ident = props.get("identifier", "")
        granule = parse_rsrf_identifier(ident)
        if str(gpp.get("datasetId", GCOMC_RSRF_DATASET_ID)) != GCOMC_RSRF_DATASET_ID:
            raise GCOMCCatalogError(f"{ident}: unexpected datasetId {gpp.get('datasetId')!r}")
        if gpp.get("tileHNo") is not None and int(gpp["tileHNo"]) != granule.tile_h:
            raise GCOMCCatalogError(f"{ident}: tileHNo {gpp['tileHNo']} disagrees with identifier")
        if gpp.get("tileVNo") is not None and int(gpp["tileVNo"]) != granule.tile_v:
            raise GCOMCCatalogError(f"{ident}: tileVNo {gpp['tileVNo']} disagrees with identifier")
        direction = gpp.get("orbitDirection")
        if direction and {"Ascending": "A", "Descending": "D"}.get(direction) != granule.orbit_direction:
            raise GCOMCCatalogError(f"{ident}: orbitDirection {direction!r} disagrees with identifier")
        product = props.get("product") or {}
        path = product.get("fileName") or ""
        if not path:
            raise GCOMCCatalogError(f"{ident}: CSW record has no product.fileName")
        size = product.get("size")
        cloud = gpp.get("cloudCoverPercentage")
        records.append(CSWRecord(
            granule=granule,
            csw_path=path,
            csw_size_bytes=int(size) if str(size or "").isdigit() else None,
            begin_position=str(props.get("beginPosition", "")),
            end_position=str(props.get("endPosition", "")),
            cloud_cover_pct=float(cloud) if cloud not in (None, "") else None,
        ))
    return records


def build_csw_params(start: date, end: date, tile_v: int, tile_h: int, *, start_index: int = 1, count: int = 1000) -> Dict[str, str]:
    return {
        "service": "CSW",
        "version": "3.0.0",
        "request": "GetRecords",
        "outputFormat": "application/json",
        "datasetId": GCOMC_RSRF_DATASET_ID,
        "startTime": f"{start.isoformat()}T00:00:00",
        "endTime": f"{end.isoformat()}T23:59:59",
        "tileHNo": str(tile_h),
        "tileVNo": str(tile_v),
        "count": str(count),
        "startIndex": str(start_index),
    }


def _http_get_json(url: str, params: Dict[str, str]) -> Dict[str, Any]:
    import requests

    response = requests.get(url, params=params, timeout=(30, 300))
    response.raise_for_status()
    try:
        return response.json()
    except ValueError as exc:
        raise GCOMCCatalogError(f"CSW returned non-JSON content: {response.text[:200]!r}") from exc


def query_csw_records(
    start: date,
    end: date,
    tile_v: int,
    tile_h: int,
    *,
    fetch_json: Callable[[str, Dict[str, str]], Dict[str, Any]] = _http_get_json,
    page_size: int = 1000,
) -> List[CSWRecord]:
    """All RSRF CSW records for one EQA tile and date range (tile query, never bbox)."""
    records: List[CSWRecord] = []
    start_index = 1
    while True:
        params = build_csw_params(start, end, tile_v, tile_h, start_index=start_index, count=page_size)
        payload = call_with_network_retry(lambda: fetch_json(GCOMC_CSW_URL, params), service="G-Portal-CSW")
        page = parse_csw_response(payload)
        records.extend(page)
        meta = payload.get("properties") or {}
        matched = int(meta.get("numberOfRecordsMatched", len(records)))
        if not page or len(records) >= matched:
            break
        start_index += len(page)
    return records


def select_daily_records(records: Iterable[CSWRecord], tiles: Sequence[Tuple[int, int]]) -> Dict[Tuple[date, Tuple[int, int]], CSWRecord]:
    """Pick the descending, version-3002 record per (date, tile). Unknown-only versions and duplicates are errors."""
    wanted = set(tiles)
    by_key: Dict[Tuple[date, Tuple[int, int]], List[CSWRecord]] = {}
    for rec in records:
        g = rec.granule
        if g.orbit_direction != GCOMC_ORBIT_DIRECTION or (g.tile_v, g.tile_h) not in wanted:
            continue
        by_key.setdefault((g.observation_date, (g.tile_v, g.tile_h)), []).append(rec)
    selected: Dict[Tuple[date, Tuple[int, int]], CSWRecord] = {}
    for key, recs in by_key.items():
        current = [r for r in recs if r.granule.version == GCOMC_RSRF_PRODUCT_VERSION]
        if not current:
            versions = sorted({r.granule.version for r in recs})
            raise GCOMCCatalogError(
                f"{key[0]} T{key[1][0]:02d}{key[1][1]:02d}: only unknown RSRF product version(s) {versions}; "
                f"expected {GCOMC_RSRF_PRODUCT_VERSION} (re-audit before supporting a new version)"
            )
        if len({r.granule.identifier for r in current}) > 1:
            raise GCOMCCatalogError(f"{key}: duplicate RSRF records {[r.granule.identifier for r in current]}")
        selected[key] = current[0]
    return selected


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

def expected_sftp_path(granule: RSRFGranule) -> str:
    d = granule.observation_date
    return (f"{GCOMC_SFTP_PRODUCT_ROOT}{GCOMC_RSRF_PRODUCT_DIR}/{granule.algorithm_version}/"
            f"{d:%Y}/{d:%m}/{d:%d}/{granule.filename}")


def resolve_sftp_path(csw_path: str, granule: RSRFGranule) -> str:
    """Map a CSW path ('product.gportal.jaxa.jp:/products/Standard/...' or 'standard/...')
    onto the real SFTP path '/product/Standard/...', checking it against the granule id."""
    text = csw_path.split(":", 1)[1] if re.match(r"^[A-Za-z0-9.-]+:/", csw_path) else csw_path
    marker = re.search(r"(?:^|/)[Ss]tandard/", text)
    if not marker:
        raise GCOMCPathError(f"CSW path has no 'Standard/' component: {csw_path!r}")
    resolved = GCOMC_SFTP_PRODUCT_ROOT + text[marker.end():]
    expected = expected_sftp_path(granule)
    if resolved != expected:
        raise GCOMCPathError(f"CSW path {csw_path!r} resolves to {resolved!r}, expected {expected!r}")
    return resolved


def local_raw_path(raw_dir: Path, granule: RSRFGranule) -> Path:
    d = granule.observation_date
    return Path(raw_dir) / "GCOM-C" / "SGLI" / "L2.LAND.RSRF" / granule.version / f"{d:%Y}" / f"{d:%m}" / f"{d:%d}" / granule.filename


def in_request_period(observation_date: date, today: date) -> bool:
    """True if the official archive/Web-request rule applies (obs date > 30 months before today)."""
    y, m = today.year, today.month - GCOMC_REQUEST_PERIOD_MONTHS
    while m <= 0:
        y, m = y - 1, m + 12
    cutoff = date(y, m, min(today.day, calendar.monthrange(y, m)[1]))
    return observation_date < cutoff


# ---------------------------------------------------------------------------
# HDF5 validation and reading
# ---------------------------------------------------------------------------

def _scalar(value: Any) -> Any:
    v = np.asarray(value).reshape(-1)[0]
    return v.decode() if isinstance(v, bytes) else v


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", str(text)).strip(" ;")


def _check_field(ds: Any, spec: FieldSpec, filename: str) -> Dict[str, Any]:
    n = GCOMC_TILE_PIXELS[spec.grid]
    if not isinstance(ds, h5py.Dataset):
        raise GCOMCSchemaError(f"{filename}: '{spec.hdf5_path}' is not a dataset")
    if tuple(ds.shape) != (n, n):
        raise GCOMCSchemaError(f"{filename}: {spec.hdf5_path} shape {ds.shape}, expected ({n}, {n})")
    if str(ds.dtype) != spec.dtype:
        raise GCOMCSchemaError(f"{filename}: {spec.hdf5_path} dtype {ds.dtype}, expected {spec.dtype}")
    attrs = {k: ds.attrs[k] for k in ds.attrs.keys()}
    missing = [a for a in _REQUIRED_FIELD_ATTRS if a not in attrs]
    if missing:
        raise GCOMCSchemaError(f"{filename}: {spec.hdf5_path} missing attributes {missing}")
    if spec.name == "QA_flag":
        desc = [_norm(x.decode() if isinstance(x, bytes) else x) for x in np.asarray(attrs["Data_description"]).reshape(-1)]
        if desc != [_norm(x) for x in GCOMC_QA_FLAG_DESCRIPTION]:
            raise GCOMCSchemaError(f"{filename}: QA_flag bit definition differs from the audited definition")
    elif _scalar(attrs["Data_description"]) != spec.description:
        raise GCOMCSchemaError(
            f"{filename}: {spec.hdf5_path} Data_description {_scalar(attrs['Data_description'])!r}, expected {spec.description!r}"
        )
    meta = {
        "slope": float(_scalar(attrs["Slope"])),
        "offset": float(_scalar(attrs["Offset"])),
        "error_dn": int(_scalar(attrs["Error_DN"])),
        "min_valid_dn": int(_scalar(attrs["Minimum_valid_DN"])),
        "max_valid_dn": int(_scalar(attrs["Maximum_valid_DN"])),
        "data_description": spec.description if spec.name != "QA_flag" else "QA_flag bits " + " ".join(_norm(x) for x in GCOMC_QA_FLAG_DESCRIPTION),
    }
    if not (math.isfinite(meta["slope"]) and meta["slope"] > 0 and math.isfinite(meta["offset"])):
        raise GCOMCSchemaError(f"{filename}: {spec.hdf5_path} invalid Slope/Offset {meta['slope']}/{meta['offset']}")
    if "Spatial_resolution" in attrs:
        res_deg = float(_scalar(attrs["Spatial_resolution"]))
        if abs(res_deg * n - EQA_TILE_DEG) > 1e-4:
            raise GCOMCSchemaError(f"{filename}: {spec.hdf5_path} Spatial_resolution {res_deg} inconsistent with {n} px/tile")
        meta["spatial_resolution_deg"] = res_deg
    elif spec.kind == "reflectance":
        raise GCOMCSchemaError(f"{filename}: {spec.hdf5_path} missing Spatial_resolution")
    if "Center_wavelength" in attrs:
        meta["center_wavelength_nm"] = float(_scalar(attrs["Center_wavelength"]))
    return meta


def validate_rsrf_hdf5(path: Path, expected_filename: Optional[str] = None) -> Dict[str, Any]:
    """Open and validate an RSRF file against the audited schema. Returns metadata; raises GCOMCSchemaError."""
    filename = expected_filename or Path(path).name
    try:
        f = h5py.File(path, "r")
    except OSError as exc:
        raise GCOMCSchemaError(f"{filename}: cannot be opened as HDF5 ({exc})") from exc
    with f:
        if "Global_attributes" not in f:
            raise GCOMCSchemaError(f"{filename}: missing Global_attributes")
        ga = f["Global_attributes"].attrs
        for key in ("Product_version", "Product_file_name", "Algorithm_version", "Parameter_version", "Image_start_time", "Image_end_time"):
            if key not in ga:
                raise GCOMCSchemaError(f"{filename}: missing Global_attributes/{key}")
        meta: Dict[str, Any] = {k: str(_scalar(ga[k])) for k in (
            "Product_version", "Product_file_name", "Algorithm_version", "Parameter_version", "Image_start_time", "Image_end_time")}
        if meta["Product_version"] != GCOMC_RSRF_PRODUCT_VERSION:
            raise GCOMCSchemaError(f"{filename}: unknown product version {meta['Product_version']!r} (expected {GCOMC_RSRF_PRODUCT_VERSION})")
        if meta["Product_file_name"] != filename:
            raise GCOMCSchemaError(f"{filename}: Product_file_name is {meta['Product_file_name']!r}")
        fields: Dict[str, Dict[str, Any]] = {}
        for spec in GCOMC_FIELDS:
            if spec.hdf5_path not in f:
                raise GCOMCSchemaError(f"{filename}: expected dataset '{spec.hdf5_path}' not found")
            fields[spec.name] = _check_field(f[spec.hdf5_path], spec, filename)
        meta["fields"] = fields
    return meta


def dn_to_reflectance(dn: np.ndarray, slope: float, offset: float, error_dn: int, min_valid_dn: int, max_valid_dn: int) -> np.ndarray:
    """physical = DN * Slope + Offset as float32; Error_DN and DN outside the valid range -> NaN."""
    out = (dn.astype(np.float64) * slope + offset).astype(np.float32)
    out[(dn == error_dn) | (dn < min_valid_dn) | (dn > max_valid_dn)] = np.nan
    return out


# ---------------------------------------------------------------------------
# SFTP
# ---------------------------------------------------------------------------

class GPortalSFTP:
    """Lazy, public-key-only SFTP session to the new G-Portal (host key pinned)."""

    def __init__(self, username: str, private_key_path: str = GCOMC_PRIVATE_KEY_PATH, *,
                 host: str = GCOMC_SFTP_HOST, port: int = GCOMC_SFTP_PORT, host_key_sha256: str = GCOMC_SFTP_HOST_KEY_SHA256):
        if not username:
            raise GCOMCAuthError("GPORTAL_USERNAME is not set")
        if not os.path.isfile(private_key_path):
            raise GCOMCAuthError(f"G-Portal private key not found at {private_key_path} (mount it read-only via docker compose)")
        self._username = username
        self._key_path = private_key_path
        self._host, self._port, self._pin = host, port, host_key_sha256
        self._transport = None
        self._sftp = None

    def _connect_once(self) -> None:
        import paramiko

        try:
            transport = paramiko.Transport((self._host, self._port))
            transport.start_client(timeout=60)
        except (socket.error, EOFError, paramiko.SSHException) as exc:
            if isinstance(exc, paramiko.AuthenticationException):
                raise
            raise ConnectionError(f"G-Portal SFTP connection failed: {exc}") from exc
        key = transport.get_remote_server_key()
        fingerprint = base64.b64encode(hashlib.sha256(key.asbytes()).digest()).decode().rstrip("=")
        if fingerprint != self._pin:
            transport.close()
            raise GCOMCAuthError(f"G-Portal SFTP host key mismatch (got SHA256:{fingerprint})")
        try:
            pkey = paramiko.PKey.from_path(self._key_path)  # key type auto-detected (G-Portal issues ed25519)
        except Exception:
            transport.close()
            # Deliberately no exception text: it must never echo key material.
            raise GCOMCAuthError("G-Portal private key could not be loaded (passphrase-protected or unsupported)") from None
        try:
            transport.auth_publickey(self._username, pkey)
        except paramiko.AuthenticationException as exc:
            transport.close()
            raise GCOMCAuthError(f"G-Portal SFTP public-key authentication failed ({type(exc).__name__})") from exc
        transport.set_keepalive(30)
        self._transport = transport
        self._sftp = paramiko.SFTPClient.from_transport(transport)

    def _client(self):
        if self._sftp is None:
            call_with_network_retry(self._connect_once, service="G-Portal-SFTP")
        return self._sftp

    def reset(self) -> None:
        self.close()

    def close(self) -> None:
        for obj in (self._sftp, self._transport):
            try:
                if obj is not None:
                    obj.close()
            except Exception:
                pass
        self._sftp = self._transport = None

    def __enter__(self) -> "GPortalSFTP":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def stat_size(self, path: str) -> int:
        return int(self._client().stat(path).st_size)

    def read_head(self, path: str, n: int = 8) -> bytes:
        with self._client().open(path, "rb") as fh:
            return fh.read(n)

    def download(self, path: str, dest: Path, expected_size: int, chunk: int = 4 << 20) -> str:
        """Stream the remote file into dest (caller handles .part naming). Returns SHA-256 hex."""
        h = hashlib.sha256()
        with self._client().open(path, "rb") as src, open(dest, "wb") as dst:
            src.prefetch(expected_size)
            while True:
                block = src.read(chunk)
                if not block:
                    break
                dst.write(block)
                h.update(block)
            dst.flush()
            os.fsync(dst.fileno())
        return h.hexdigest()


def _transient(exc: BaseException) -> bool:
    try:
        import paramiko
        ssh_types: Tuple[type, ...] = (paramiko.SSHException,)
        auth_types: Tuple[type, ...] = (paramiko.AuthenticationException,)
    except ImportError:  # pragma: no cover
        ssh_types, auth_types = (), ()
    if isinstance(exc, (GCOMCError,) + auth_types):
        return False
    return isinstance(exc, (socket.error, EOFError, ConnectionError, TimeoutError) + ssh_types) and not isinstance(
        exc, (FileNotFoundError, PermissionError))


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(8 << 20), b""):
            h.update(block)
    return h.hexdigest()


@dataclass
class ProductResult:
    granule: RSRFGranule
    status: str
    source: str = ""                 # "local_cache" or "sftp"
    local_path: Optional[Path] = None
    sftp_path: str = ""
    size_bytes: Optional[int] = None
    sha256: str = ""
    detail: str = ""
    metadata: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "identifier": self.granule.identifier, "tile": self.granule.tile_id, "status": self.status,
            "source": self.source, "sftp_path": self.sftp_path, "size_bytes": self.size_bytes,
            "sha256": self.sha256, "detail": self.detail,
            "local_path": str(self.local_path) if self.local_path else "",
        }


def acquire_product(
    record: CSWRecord,
    raw_dir: Path,
    sftp_factory: Callable[[], Any],
    today: date,
) -> ProductResult:
    """Return the product status for one CSW record, reusing a valid local raw file or downloading it.

    Local raw file present -> validated and reused (no network). Otherwise the
    real SFTP state decides: readable -> download (.part -> size check -> HDF5
    validation -> atomic rename) -> AVAILABLE; explicitly missing/denied ->
    REQUEST_REQUIRED if the official archive rule applies, else ERROR.
    """
    g = record.granule
    sftp_path = resolve_sftp_path(record.csw_path, g)
    final = local_raw_path(raw_dir, g)
    result = ProductResult(granule=g, status=PRODUCT_ERROR, sftp_path=sftp_path, local_path=final)

    if final.exists():
        try:
            result.metadata = validate_rsrf_hdf5(final, g.filename)
        except GCOMCSchemaError as exc:
            # Never overwrite or delete a file in the research raw archive automatically.
            result.detail = f"local raw file failed validation (left untouched): {exc}"
            return result
        result.status, result.source = PRODUCT_AVAILABLE, "local_cache"
        result.size_bytes = final.stat().st_size
        result.sha256 = _sha256_file(final)
        return result

    sftp = sftp_factory()
    try:
        size = call_with_network_retry(lambda: _retry_wrap(sftp, lambda: sftp.stat_size(sftp_path)), service="G-Portal-SFTP")
        head = call_with_network_retry(lambda: _retry_wrap(sftp, lambda: sftp.read_head(sftp_path, len(HDF5_SIGNATURE))), service="G-Portal-SFTP")
    except (FileNotFoundError, PermissionError) as exc:
        reason = "file not found" if isinstance(exc, FileNotFoundError) else "permission denied"
        if in_request_period(g.observation_date, today):
            result.status = PRODUCT_REQUEST_REQUIRED
            result.detail = f"SFTP {reason} at {sftp_path}; observation older than {GCOMC_REQUEST_PERIOD_MONTHS} months -> Web download request required"
        else:
            result.detail = f"SFTP {reason} at {sftp_path} although the CSW record exists and the archive rule does not apply"
        return result
    if head != HDF5_SIGNATURE:
        result.detail = "remote file is readable but does not start with the HDF5 signature"
        return result

    final.parent.mkdir(parents=True, exist_ok=True)
    part = final.with_name(final.name + ".part")

    def _download() -> str:
        try:
            return _retry_wrap(sftp, lambda: sftp.download(sftp_path, part, size))
        except BaseException:
            if part.exists():
                part.unlink()
            raise

    sha = call_with_network_retry(_download, service="G-Portal-SFTP")
    local_size = part.stat().st_size
    if local_size != size:
        part.unlink()
        result.detail = f"size mismatch after download: local {local_size} != remote {size}"
        return result
    try:
        result.metadata = validate_rsrf_hdf5(part, g.filename)
    except GCOMCSchemaError as exc:
        part.unlink()
        result.detail = f"downloaded file failed HDF5 validation: {exc}"
        return result
    os.replace(part, final)  # atomic rename only after validation
    result.status, result.source = PRODUCT_AVAILABLE, "sftp"
    result.size_bytes, result.sha256 = size, sha
    return result


def _retry_wrap(sftp: Any, fn: Callable[[], Any]) -> Any:
    """Turn transient SSH/socket failures into ConnectionError (retried by network_retry) and reset the session."""
    try:
        return fn()
    except Exception as exc:
        if _transient(exc):
            if hasattr(sftp, "reset"):
                sftp.reset()
            raise ConnectionError(f"G-Portal SFTP transient failure: {type(exc).__name__}: {exc}") from exc
        raise


# ---------------------------------------------------------------------------
# Mosaic, AOI status, GeoTIFF output
# ---------------------------------------------------------------------------

def read_mosaic(window: GlobalWindow, spec: FieldSpec, tile_files: Dict[Tuple[int, int], Path]) -> np.ndarray:
    """Place native pixels of every available tile into the window by integer index (no resampling).
    Pixels not covered by an available tile keep the field's Error_DN."""
    fill = None
    out: Optional[np.ndarray] = None
    for (v, h), path in sorted(tile_files.items()):
        sl = window.tile_slices(v, h)
        with h5py.File(path, "r") as f:
            ds = f[spec.hdf5_path]
            err = int(_scalar(ds.attrs["Error_DN"]))
            if out is None:
                fill = err
                out = np.full(window.shape, err, dtype=ds.dtype)
            elif err != fill or ds.dtype != out.dtype:
                raise GCOMCSchemaError(f"{spec.hdf5_path}: Error_DN/dtype differ between tiles")
            if sl is not None:
                tr, tc, wr, wc = sl
                out[wr, wc] = ds[tr, tc]
    if out is None:
        raise GCOMCError("read_mosaic called without any tile file")
    return out


def aoi_pixel_mask(window: GlobalWindow, geometry_wgs84: Dict[str, Any]) -> np.ndarray:
    """Boolean mask of window pixels whose centre lies in the AOI polygon (all_touched fallback for tiny AOIs)."""
    geom = transform_geom("EPSG:4326", eqa_crs(), _densify_geometry(geometry_wgs84))
    mask = geometry_mask([geom], out_shape=window.shape, transform=window.transform, invert=True, all_touched=False)
    if not mask.any():
        mask = geometry_mask([geom], out_shape=window.shape, transform=window.transform, invert=True, all_touched=True)
    return mask


def classify_aoi(qa: np.ndarray, aoi_mask: np.ndarray) -> Tuple[str, int, int]:
    """AOI status from QA_flag bit0 (official 'no available data') over AOI pixels."""
    n = int(aoi_mask.sum())
    if n == 0:
        raise GCOMCError("AOI covers no native pixel")
    observed = int(np.sum(((qa[aoi_mask] >> QA_BIT_NO_DATA) & 1) == 0))
    if observed == 0:
        return AOI_NO_DATA, observed, n
    if observed == n:
        return AOI_OBSERVED, observed, n
    return AOI_PARTIAL, observed, n


def _write_geotiff(path: Path, array: np.ndarray, *, transform: Affine, nodata: Optional[float], tags: Dict[str, Any],
                   band_name: str, footprint: Optional[np.ndarray]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".part.tif")
    profile: Dict[str, Any] = {
        "driver": "GTiff", "height": array.shape[0], "width": array.shape[1], "count": 1,
        "dtype": str(array.dtype), "crs": eqa_crs(), "transform": transform, "compress": "lzw",
    }
    if nodata is not None:
        profile["nodata"] = nodata
    with rasterio.open(tmp, "w", **profile) as dst:
        dst.write(array, 1)
        dst.set_band_description(1, band_name)
        if footprint is not None:
            dst.write_mask(footprint.astype(np.uint8) * np.uint8(255))
        dst.update_tags(**{k: str(v) for k, v in tags.items()})
    os.replace(tmp, path)


def output_path(out_dir: Path, spec: FieldSpec, obs: date) -> Path:
    return out_dir / spec.group / spec.name / f"GCOMC_SGLI_RSRF_{obs:%Y%m%d}_{GCOMC_ORBIT_DIRECTION}_{spec.name}.tif"


def summary_path(out_dir: Path, obs: date) -> Path:
    return out_dir / "summary" / f"GCOMC_SGLI_RSRF_{obs:%Y%m%d}_{GCOMC_ORBIT_DIRECTION}.json"


def process_date(
    obs: date,
    products: Dict[Tuple[int, int], ProductResult],
    geometry_wgs84: Dict[str, Any],
    out_dir: Path,
    *,
    clip_to_aoi: bool = True,
) -> Dict[str, Any]:
    """Mosaic available tiles per native grid, crop to the AOI bbox, classify AOI status and write GeoTIFFs.

    Writes nothing but the date summary when the AOI has no observed pixel.
    """
    available = {k: p for k, p in products.items() if p.status == PRODUCT_AVAILABLE}
    tile_files = {k: p.local_path for k, p in available.items()}
    crs = eqa_crs()
    bbox_geom = _aoi_bbox_geometry_in_crs(geometry_wgs84, crs)
    bbox_bounds = geometry_bounds(bbox_geom)
    windows = {grid: native_window_for_bounds(bbox_bounds, grid) for grid in GCOMC_TILE_PIXELS}

    qa = read_mosaic(windows["250m"], _QA_FIELD, tile_files)
    aoi_mask_250 = aoi_pixel_mask(windows["250m"], geometry_wgs84)
    aoi_status, n_obs, n_aoi = classify_aoi(qa, aoi_mask_250)
    first_meta = next(iter(available.values())).metadata or {}

    summary: Dict[str, Any] = {
        "observation_date": obs.isoformat(),
        "orbit_direction": GCOMC_ORBIT_DIRECTION,
        "product_status": {p.granule.tile_id: p.to_dict() for p in products.values()},
        "aoi_status": aoi_status,
        "aoi_observed_pixels_250m": n_obs,
        "aoi_pixels_250m": n_aoi,
        "aoi_observed_fraction_250m": round(n_obs / n_aoi, 6),
        "files": [],
        "band_valid_fraction_in_aoi": {},
    }
    if aoi_status == AOI_NO_DATA:
        return summary

    footprints: Dict[str, Optional[np.ndarray]] = {}
    aoi_masks = {"250m": aoi_mask_250, "1km": aoi_pixel_mask(windows["1km"], geometry_wgs84)}
    for grid, win in windows.items():
        footprints[grid] = (geometry_mask([bbox_geom], out_shape=win.shape, transform=win.transform,
                                          invert=True, all_touched=True) if clip_to_aoi else None)

    common = {
        "source_product": "GCOM-C/SGLI L2 LAND RSRF (Atmospheric corrected surface reflectance)",
        "source_dataset_id": GCOMC_RSRF_DATASET_ID,
        "product_version": GCOMC_RSRF_PRODUCT_VERSION,
        "algorithm_version": first_meta.get("Algorithm_version", ""),
        "parameter_version": first_meta.get("Parameter_version", ""),
        "observation_date": obs.isoformat(),
        "orbit_direction": GCOMC_ORBIT_DIRECTION,
        "tiles": ",".join(sorted(p.granule.tile_id for p in available.values())),
        "source_filenames": ",".join(sorted(p.granule.filename for p in available.values())),
        "missing_tiles": ",".join(sorted(p.granule.tile_id for k, p in products.items() if k not in available)),
        "aoi_status": aoi_status,
        "crop_definition": "minimum_wgs84_bbox_touching_native_pixels",
        "raster_reprojected": False,
        "resampling": "none",
        "grid": f"GCOM-C EQA tile grid (sinusoidal equal-area, lon_0=0), sphere R={EQA_SPHERE_RADIUS_M} m",
        "geolocation_model": "JAXA SGLI GeoTIFF tool (manual v1.2 App.7.1) continuous EQA definition; "
                             "affine reproduces it to <1e-10 px; data verified to follow it (not the Handbook NINT variant)",
    }
    for spec in GCOMC_FIELDS:
        win = windows[spec.grid]
        dn = read_mosaic(win, spec, tile_files)
        meta = first_meta.get("fields", {}).get(spec.name, {})
        for p in available.values():
            other = (p.metadata or {}).get("fields", {}).get(spec.name, {})
            for key in ("slope", "offset", "error_dn", "min_valid_dn", "max_valid_dn"):
                if other.get(key) != meta.get(key):
                    raise GCOMCSchemaError(f"{spec.hdf5_path}: {key} differs between tiles ({other.get(key)} vs {meta.get(key)})")
        footprint = footprints[spec.grid]
        tags = dict(common)
        tags.update(
            field=spec.name, hdf5_path=f"/{spec.hdf5_path}", native_grid=spec.grid,
            native_tile_pixels=GCOMC_TILE_PIXELS[spec.grid], native_pixel_size_m=f"{win.transform.a:.6f}",
            native_resolution_deg=f"{EQA_TILE_DEG / GCOMC_TILE_PIXELS[spec.grid]:.10f}",
            data_description=meta.get("data_description", ""), slope=meta.get("slope"), offset=meta.get("offset"),
            error_dn=meta.get("error_dn"), valid_dn_range=f"{meta.get('min_valid_dn')}..{meta.get('max_valid_dn')}",
        )
        if spec.kind == "reflectance":
            arr = dn_to_reflectance(dn, meta["slope"], meta["offset"], meta["error_dn"], meta["min_valid_dn"], meta["max_valid_dn"])
            if footprint is not None:
                arr[~footprint] = np.nan
            nodata: Optional[float] = float("nan")
            tags.update(scale_offset_applied=True, units="reflectance (unitless)", reflectance_type=spec.reflectance_type,
                        center_wavelength_nm=meta.get("center_wavelength_nm", ""))
            valid = ~np.isnan(arr)
        else:
            arr = dn.copy()
            if footprint is not None:
                arr[~footprint] = meta["error_dn"]
            nodata = float(meta["error_dn"])
            tags.update(scale_offset_applied=False, raw_integer=True,
                        note="raw integer; Slope/Offset NOT applied")
            valid = arr != meta["error_dn"]
        summary["band_valid_fraction_in_aoi"][spec.name] = round(float(valid[aoi_masks[spec.grid]].mean()), 6)
        path = output_path(out_dir, spec, obs)
        _write_geotiff(path, arr, transform=win.transform, nodata=nodata, tags=tags, band_name=spec.name, footprint=footprint)
        summary["files"].append(str(path))
    return summary


# ---------------------------------------------------------------------------
# Pipeline entry point
# ---------------------------------------------------------------------------

def _date_range(start: date, end: date) -> List[date]:
    return [start + timedelta(days=i) for i in range((end - start).days + 1)]


def _resolve_raw_dir(config: Dict[str, Any], config_dir: Path, output_root: Path) -> Path:
    sr_cfg = config.get("surface_reflectance", {}) or {}
    value = sr_cfg.get("gcomc_raw_dir") or os.environ.get("GCOMC_RAW_DIR")
    if value:
        return _resolve_runtime_path(str(value), config_dir, must_exist=False)
    return output_root / "gcomc" / "_raw"


def _gportal_username(config: Dict[str, Any], config_dir: Path) -> str:
    sr_cfg = config.get("surface_reflectance", {}) or {}
    env_path = _resolve_runtime_path(str(sr_cfg.get("gportal_env_path", "key.env")), config_dir, must_exist=False)
    return os.environ.get("GPORTAL_USERNAME") or _load_env_kv_file(env_path).get("GPORTAL_USERNAME", "")


def _process_gcomc_surface_reflectance(
    config: Dict[str, Any],
    config_dir: Path,
    output_root: Path,
    geometry_wgs84: Dict[str, Any],
    start_date: date,
    end_date: date,
    *,
    today: Optional[date] = None,
    csw_query: Callable[..., List[CSWRecord]] = query_csw_records,
    sftp_factory: Optional[Callable[[], Any]] = None,
) -> Dict[str, Any]:
    """Daily GCOM-C/SGLI RSRF acquisition + native-grid processing for one date window."""
    today = today or datetime.now(timezone.utc).date()
    file_exists_mode = str(config.get("file_exists", "skip")).strip().lower()
    if file_exists_mode not in {"overwrite", "skip"}:
        raise ValueError("config.file_exists must be 'overwrite' or 'skip'")
    out_dir = output_root / "gcomc" / "rsrf"
    raw_dir = _resolve_raw_dir(config, config_dir, output_root)
    tiles = tiles_for_geometry(geometry_wgs84)
    summary: Dict[str, Any] = {
        "product": "L2.LAND.RSRF", "version": GCOMC_RSRF_PRODUCT_VERSION, "orbit_direction": GCOMC_ORBIT_DIRECTION,
        "tiles": [f"T{v:02d}{h:02d}" for v, h in tiles], "raw_dir": str(raw_dir),
        "dates_processed": [], "dates_aoi_no_data": [], "dates_no_record": [],
        "dates_request_required": [], "dates_failed": [], "dates_skipped": [],
    }

    todo: List[date] = []
    for d in _date_range(start_date, end_date):
        sp = summary_path(out_dir, d)
        if file_exists_mode == "skip" and sp.exists():
            try:
                prev = json.loads(sp.read_text(encoding="utf-8"))
                if all(Path(p).exists() for p in prev.get("files", [])):
                    LOGGER.info("GCOM-C/SGLI: skipping %s, outputs already exist", d)
                    summary["dates_skipped"].append(d.isoformat())
                    continue
            except (OSError, ValueError):
                pass
        todo.append(d)
    if not todo:
        return summary

    try:
        records: List[CSWRecord] = []
        for v, h in tiles:
            records.extend(csw_query(todo[0], todo[-1], v, h))
        selected = select_daily_records(records, tiles)
    except Exception as exc:
        LOGGER.error("GCOM-C/SGLI CSW query failed: %s", exc, exc_info=True)
        summary["error"] = f"CSW: {exc}"
        return summary

    session: Dict[str, Any] = {}

    def _factory() -> Any:
        if "sftp" not in session:
            session["sftp"] = sftp_factory() if sftp_factory else GPortalSFTP(_gportal_username(config, config_dir))
        return session["sftp"]

    try:
        for d in todo:
            token = d.isoformat()
            recs = {t: selected[(d, t)] for t in tiles if (d, t) in selected}
            if not recs:
                LOGGER.info("GCOM-C/SGLI: no RSRF record for AOI tiles on %s", token)
                summary["dates_no_record"].append(token)
                continue
            try:
                products = {t: acquire_product(r, raw_dir, _factory, today) for t, r in recs.items()}
            except GCOMCAuthError:
                raise
            except GCOMCError as exc:
                LOGGER.error("GCOM-C/SGLI %s: %s", token, exc)
                summary["dates_failed"].append({"date": token, "error": str(exc)})
                continue
            for t in tiles:
                if t not in products:
                    g = RSRFGranule(f"(no record) T{t[0]:02d}{t[1]:02d}", d, GCOMC_ORBIT_DIRECTION, t[0], t[1], "", "", "")
                    products[t] = ProductResult(granule=g, status=PRODUCT_NO_RECORD)
            statuses = {p.granule.tile_id: p.status for p in products.values()}
            if any(s == PRODUCT_ERROR for s in statuses.values()):
                details = {p.granule.tile_id: p.detail for p in products.values() if p.status == PRODUCT_ERROR}
                LOGGER.error("GCOM-C/SGLI %s: product error %s", token, details)
                summary["dates_failed"].append({"date": token, "error": details})
                continue
            if any(s == PRODUCT_REQUEST_REQUIRED for s in statuses.values()):
                LOGGER.warning("GCOM-C/SGLI %s: Web download request required for %s", token,
                               [p.granule.filename for p in products.values() if p.status == PRODUCT_REQUEST_REQUIRED])
                summary["dates_request_required"].append({"date": token, "products": {k: v.to_dict() for k, v in products.items()}})
                continue
            try:
                day = process_date(d, products, geometry_wgs84, out_dir)
            except GCOMCError as exc:
                LOGGER.error("GCOM-C/SGLI %s: %s", token, exc)
                summary["dates_failed"].append({"date": token, "error": str(exc)})
                continue
            sp = summary_path(out_dir, d)
            sp.parent.mkdir(parents=True, exist_ok=True)
            tmp = sp.with_name(sp.name + ".part")
            tmp.write_text(json.dumps(day, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, sp)
            if day["aoi_status"] == AOI_NO_DATA:
                LOGGER.info("GCOM-C/SGLI: no RSRF observation for AOI on %s", token)
                summary["dates_aoi_no_data"].append(token)
            else:
                summary["dates_processed"].append({"date": token, "aoi_status": day["aoi_status"],
                                                   "aoi_observed_fraction_250m": day["aoi_observed_fraction_250m"],
                                                   "files": len(day["files"])})
    except GCOMCAuthError as exc:
        LOGGER.error("GCOM-C/SGLI authentication failed: %s", exc)
        summary["error"] = str(exc)
    finally:
        if "sftp" in session and hasattr(session["sftp"], "close"):
            session["sftp"].close()
    return summary
