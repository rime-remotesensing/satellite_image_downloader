"""Exact-granule recovery of original MODIS MOD09GA/MYD09GA .061 HDF files.

Separate from the normal download/processing path (run2.py /
surface_reflectance.py): that path skips work whenever processed GeoTIFFs
already exist and deletes raw HDFs afterwards by default. This module only
ever deals with raw HDF files themselves, so it can recover an original
granule even when every derived GeoTIFF is already present.

Rules (matching the research side's recovery plan):
  * The granule list comes from a manifest; nothing is inferred from image
    names or dates.
  * Exact granule only -- the filename, including the processing timestamp,
    must match. There is no fallback to another processing version.
  * Existing files are never overwritten or deleted. A file that is present
    but fails verification is reported and left untouched.
  * Downloads go to <name>.part, are verified, then atomically renamed.
"""

from __future__ import annotations

import csv
import hashlib
import json
import logging
import os
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence

LOGGER = logging.getLogger(__name__)

PRODUCT_BY_PLATFORM = {"terra": "MOD09GA", "aqua": "MYD09GA"}
SUPPORTED_VERSION = "061"
DEFAULT_RECOVERED_SUBDIR = "recovered_modis_061"

# (SDS name, expected native shape) -- the 7 SR bands + 2 QA layers the
# downloader exports (same names as constants.MODIS_SDS_NAME_MAP values).
REQUIRED_SDS: Dict[str, tuple] = {
    **{f"sur_refl_b0{i}_1": (2400, 2400) for i in range(1, 8)},
    "QC_500m_1": (2400, 2400),
    "state_1km_1": (1200, 1200),
}

_GRANULE_RE = re.compile(
    r"^(?P<product>M[OY]D09GA)\.A(?P<year>\d{4})(?P<doy>\d{3})\.(?P<tile>h\d{2}v\d{2})"
    r"\.(?P<version>\d{3})\.(?P<processed>\d{13})\.hdf$"
)

STATUS_PRESENT_VALID = "PRESENT_VALID"
STATUS_PRESENT_ELSEWHERE = "PRESENT_ELSEWHERE_VALID"
STATUS_PRESENT_INVALID = "PRESENT_INVALID"
STATUS_MISSING = "MISSING"
STATUS_DOWNLOADED_VALID = "DOWNLOADED_VALID"
STATUS_DOWNLOAD_FAILED = "DOWNLOAD_FAILED"
STATUS_DOWNLOADED_INVALID = "DOWNLOADED_INVALID"


class ManifestError(ValueError):
    pass


@dataclass(frozen=True)
class GranuleRecord:
    granule_id: str
    product: str
    version: str
    tile: str
    date: str          # YYYY-MM-DD
    platform: str
    regions: tuple
    https_url: str

    def destination(self, raw_root: Path, subdir: str = DEFAULT_RECOVERED_SUBDIR) -> Path:
        return raw_root / subdir / self.granule_id


def parse_granule_id(granule_id: str) -> Dict[str, str]:
    m = _GRANULE_RE.match(granule_id)
    if not m:
        raise ManifestError(f"not an exact MOD09GA/MYD09GA granule filename: {granule_id!r}")
    g = m.groupdict()
    g["date"] = datetime.strptime(g["year"] + g["doy"], "%Y%j").date().isoformat()
    return g


def _validate_record(raw: Dict[str, Any]) -> GranuleRecord:
    granule_id = str(raw.get("granule_id", "")).strip()
    parsed = parse_granule_id(granule_id)

    product = str(raw.get("product") or parsed["product"]).strip()
    version = str(raw.get("version") or parsed["version"]).strip()
    tile = str(raw.get("tile") or parsed["tile"]).strip()
    date_text = str(raw.get("date") or parsed["date"]).strip()
    platform = str(raw.get("platform", "")).strip().lower()
    regions = tuple(r for r in str(raw.get("regions", "")).split(";") if r)
    url = str(raw.get("https_url", "")).strip()

    problems = []
    if product != parsed["product"]:
        problems.append(f"product {product} != filename {parsed['product']}")
    if version != parsed["version"] or version != SUPPORTED_VERSION:
        problems.append(f"version {version} (filename {parsed['version']}, supported {SUPPORTED_VERSION})")
    if tile != parsed["tile"]:
        problems.append(f"tile {tile} != filename {parsed['tile']}")
    if date_text != parsed["date"]:
        problems.append(f"date {date_text} != filename {parsed['date']}")
    if PRODUCT_BY_PLATFORM.get(platform) != parsed["product"]:
        problems.append(f"platform {platform!r} does not match product {parsed['product']}")
    if url and Path(url.split("?")[0]).name != granule_id:
        problems.append("https_url filename does not equal granule_id (no other version is accepted)")
    if problems:
        raise ManifestError(f"{granule_id}: " + "; ".join(problems))

    return GranuleRecord(granule_id, product, version, tile, date_text, platform, regions, url)


def load_manifest(path: Path) -> List[GranuleRecord]:
    """Load the missing-HDF manifest (research CSV, or JSON with 'records')."""
    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(f"manifest not found: {path}")
    if path.suffix.lower() == ".json":
        data = json.loads(path.read_text(encoding="utf-8"))
        rows = data["records"] if isinstance(data, dict) else data
    else:
        with path.open(newline="", encoding="utf-8-sig") as f:
            rows = list(csv.DictReader(f))

    records = [_validate_record(r) for r in rows]
    ids = [r.granule_id for r in records]
    dupes = sorted({g for g in ids if ids.count(g) > 1})
    if dupes:
        raise ManifestError(f"duplicate granule_id in manifest: {dupes}")
    return records


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _core_value(core: str, key: str) -> Optional[str]:
    m = re.search(key + r".*?VALUE\s*=\s*(\"[^\"]*\"|[0-9]+)", core, re.S)
    return m.group(1).strip('"') if m else None


def verify_hdf(path: Path, record: GranuleRecord, *, check_filename: bool = True) -> Dict[str, Any]:
    """Open the HDF4 file and check identity + the 9 SDS the exporter needs.

    check_filename=False is only for a not-yet-renamed `.part` download,
    whose final name is fixed by construction; the internal LOCALGRANULEID
    check still enforces the exact granule.

    Returns {"ok": bool, "problems": [...], "size_bytes", "sha256", ...}.
    """
    result: Dict[str, Any] = {"path": str(path), "ok": False, "problems": []}
    if not path.is_file():
        result["problems"].append("file not found")
        return result
    result["size_bytes"] = path.stat().st_size
    if check_filename and path.name != record.granule_id:
        result["problems"].append(f"filename {path.name} != granule_id {record.granule_id}")

    try:
        from pyhdf.SD import SD, SDC

        hdf = SD(str(path), SDC.READ)
    except Exception as exc:  # corrupt / truncated / not HDF4
        result["problems"].append(f"cannot open as HDF4: {exc}")
        return result
    try:
        attrs = hdf.attributes()
        core = str(attrs.get("CoreMetadata.0", ""))
        identity = {
            "LOCALGRANULEID": _core_value(core, "LOCALGRANULEID"),
            "SHORTNAME": _core_value(core, "SHORTNAME"),
            "RANGEBEGINNINGDATE": _core_value(core, "RANGEBEGINNINGDATE"),
            "HORIZONTALTILENUMBER": _core_value(core, "HORIZONTALTILENUMBER"),
            "VERTICALTILENUMBER": _core_value(core, "VERTICALTILENUMBER"),
        }
        result["identity"] = identity
        if identity["LOCALGRANULEID"] != record.granule_id:
            result["problems"].append(f"LOCALGRANULEID {identity['LOCALGRANULEID']!r} != {record.granule_id}")
        if identity["SHORTNAME"] != record.product:
            result["problems"].append(f"SHORTNAME {identity['SHORTNAME']!r} != {record.product}")
        if identity["RANGEBEGINNINGDATE"] != record.date:
            result["problems"].append(f"RANGEBEGINNINGDATE {identity['RANGEBEGINNINGDATE']!r} != {record.date}")
        tile = f"h{int(identity['HORIZONTALTILENUMBER'] or -1):02d}v{int(identity['VERTICALTILENUMBER'] or -1):02d}"
        if tile != record.tile:
            result["problems"].append(f"internal tile {tile} != {record.tile}")

        datasets = hdf.datasets()
        for name, shape in REQUIRED_SDS.items():
            if name not in datasets:
                result["problems"].append(f"missing SDS {name}")
            elif tuple(datasets[name][1]) != shape:
                result["problems"].append(f"SDS {name} shape {tuple(datasets[name][1])} != {shape}")
    except Exception as exc:
        result["problems"].append(f"metadata read failed: {exc}")
    finally:
        hdf.end()

    if not result["problems"]:
        result["sha256"] = sha256_file(path)
        result["ok"] = True
    return result


@dataclass
class RecordStatus:
    granule_id: str
    platform: str
    date: str
    tile: str
    regions: List[str]
    destination: str
    status: str
    detail: Dict[str, Any] = field(default_factory=dict)


def plan_recovery(
    records: Sequence[GranuleRecord],
    raw_root: Path,
    *,
    subdir: str = DEFAULT_RECOVERED_SUBDIR,
    existing_roots: Iterable[Path] = (),
    verify: Callable[[Path, GranuleRecord], Dict[str, Any]] = verify_hdf,
) -> List[RecordStatus]:
    """Classify each record without any network access or file writes."""
    existing_roots = [Path(p) for p in existing_roots]
    out: List[RecordStatus] = []
    for rec in records:
        dest = rec.destination(raw_root, subdir)
        base = dict(granule_id=rec.granule_id, platform=rec.platform, date=rec.date, tile=rec.tile,
                    regions=list(rec.regions), destination=str(dest))
        if dest.exists():
            v = verify(dest, rec)
            status = STATUS_PRESENT_VALID if v["ok"] else STATUS_PRESENT_INVALID
            out.append(RecordStatus(**base, status=status, detail=v))
            continue
        found = None
        for root in existing_roots:
            candidate = root / rec.granule_id
            if candidate.exists():
                v = verify(candidate, rec)
                if v["ok"]:
                    found = v
                    break
        if found:
            out.append(RecordStatus(**base, status=STATUS_PRESENT_ELSEWHERE, detail=found))
        else:
            out.append(RecordStatus(**base, status=STATUS_MISSING, detail={"https_url": rec.https_url}))
    return out


def _stream_download(url: str, part: Path, session: Any, timeout: int = 300) -> None:
    with session.get(url, stream=True, allow_redirects=True, timeout=timeout) as r:
        r.raise_for_status()
        with part.open("wb") as f:
            for chunk in r.iter_content(chunk_size=1 << 20):
                if chunk:
                    f.write(chunk)


def download_missing(
    statuses: List[RecordStatus],
    records: Sequence[GranuleRecord],
    *,
    session_factory: Callable[[], Any],
    downloader: Callable[[str, Path, Any], None] = _stream_download,
    verify: Callable[[Path, GranuleRecord], Dict[str, Any]] = verify_hdf,
    retry: Callable[..., Any] = None,
) -> List[RecordStatus]:
    """Download only MISSING records, by their exact URL, into their destination.

    Never touches PRESENT_* records. Never overwrites an existing destination.
    A failed or unverifiable download leaves no final file behind.
    """
    if retry is None:
        from .network_retry import call_with_network_retry as retry

    by_id = {r.granule_id: r for r in records}
    session = None
    for st in statuses:
        if st.status != STATUS_MISSING:
            continue
        rec = by_id[st.granule_id]
        if not rec.https_url:
            st.status = STATUS_DOWNLOAD_FAILED
            st.detail = {"error": "manifest has no exact https_url; not searching for substitutes"}
            continue
        dest = Path(st.destination)
        if dest.exists():  # appeared concurrently -- do not overwrite
            v = verify(dest, rec)
            st.status = STATUS_PRESENT_VALID if v["ok"] else STATUS_PRESENT_INVALID
            st.detail = v
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        part = dest.with_name(dest.name + ".part")
        try:
            if session is None:
                session = session_factory()
            retry(lambda: downloader(rec.https_url, part, session), service="Earthdata-download")
            v = verify(part, rec, check_filename=False)
            if not v["ok"]:
                st.status = STATUS_DOWNLOADED_INVALID
                st.detail = v
                part.unlink(missing_ok=True)
                continue
            os.replace(part, dest)
            v["path"] = str(dest)
            st.status = STATUS_DOWNLOADED_VALID
            st.detail = v
        except Exception as exc:
            LOGGER.error("download failed for %s: %s", rec.granule_id, exc)
            part.unlink(missing_ok=True)
            st.status = STATUS_DOWNLOAD_FAILED
            st.detail = {"error": str(exc)}
    return statuses


def summarize(statuses: Sequence[RecordStatus]) -> Dict[str, Any]:
    counts: Dict[str, int] = {}
    for s in statuses:
        counts[s.status] = counts.get(s.status, 0) + 1
    return {
        "total": len(statuses),
        "counts": counts,
        "terra": sum(s.platform == "terra" for s in statuses),
        "aqua": sum(s.platform == "aqua" for s in statuses),
        "records": [asdict(s) for s in statuses],
    }
