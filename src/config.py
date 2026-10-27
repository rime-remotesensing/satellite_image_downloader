from __future__ import annotations

import os
import re
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

try:
    import yaml
except ImportError as exc:  # pragma: no cover
    raise RuntimeError("PyYAML is required. Please install pyyaml.") from exc

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _load_config(config_path: Path) -> Dict[str, Any]:
    with config_path.open("r", encoding="utf-8") as f:
        loaded = yaml.safe_load(f)
    return loaded or {}


def _load_env_kv_file(env_path: Path) -> Dict[str, str]:
    values: Dict[str, str] = {}
    if not env_path.exists():
        return values

    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            values[key] = value
    return values


def _resolve_runtime_path(
    path_value: str,
    config_dir: Path,
    *,
    must_exist: bool = False,
) -> Path:
    """Resolve a config path with PROJECT_ROOT as the first candidate.

    Relative paths are tried from PROJECT_ROOT first, then config_dir.
    This allows values like "./config/area.geojson" in config/config.yaml
    without accidentally resolving to config/config/area.geojson.
    """
    p = Path(str(path_value))
    if p.is_absolute():
        return p

    candidates = [
        (PROJECT_ROOT / p).resolve(),
        (config_dir / p).resolve(),
    ]

    if must_exist:
        for candidate in candidates:
            if candidate.exists():
                return candidate

    return candidates[0]


def _to_date(value: str) -> date:
    return datetime.strptime(value, "%Y%m%d").date()


def _normalize_date_list(value: Any, field_name: str) -> List[date]:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        values = list(value)
    else:
        values = [value]

    if not values:
        raise ValueError(f"config.{field_name} must not be empty")

    dates: List[date] = []
    for raw in values:
        text = str(raw).strip()
        if not re.fullmatch(r"\d{8}", text):
            raise ValueError(
                f"config.{field_name} must be YYYYMMDD or list of YYYYMMDD values"
            )
        dates.append(_to_date(text))

    return dates


_SATELLITE_ALLOWED_FAMILIES = {"sentinel2", "landsat89", "modis", "viirs", "gcomc"}
_SATELLITE_FAMILY_PLATFORMS: Dict[str, set] = {
    "modis": {"terra", "aqua"},
    "viirs": {"snpp", "noaa20", "noaa21"},
}


def _parse_satellite_tokens(value: Any) -> List[Tuple[str, Optional[str]]]:
    """Parse config.satellite entries, allowing an optional '-platform'
    qualifier (e.g. 'modis-aqua', 'viirs-noaa20') alongside plain family
    names (e.g. 'modis', 'viirs'). Returns (family, platform_or_None) pairs
    in input order; platform is None for unqualified entries.
    """
    if isinstance(value, str):
        raw = [v.strip() for v in value.split(",") if v.strip()]
    elif isinstance(value, Sequence):
        raw = [str(v).strip() for v in value if str(v).strip()]
    else:
        raise ValueError("config.satellite must be string or list")

    parsed: List[Tuple[str, Optional[str]]] = []
    for token in raw:
        lowered = token.strip().lower()
        family, sep, platform = lowered.partition("-")
        if not sep:
            platform = None

        if family not in _SATELLITE_ALLOWED_FAMILIES:
            raise ValueError(f"Unsupported satellite in config: {token!r}")

        if platform is not None:
            allowed_platforms = _SATELLITE_FAMILY_PLATFORMS.get(family)
            if not allowed_platforms or platform not in allowed_platforms:
                raise ValueError(
                    f"Unsupported platform in config.satellite entry {token!r}; "
                    f"{family} supports: {sorted(allowed_platforms or [])}"
                )
        parsed.append((family, platform))
    return parsed


def _normalize_satellites(value: Any) -> List[str]:
    deduped: List[str] = []
    for family, _platform in _parse_satellite_tokens(value):
        if family not in deduped:
            deduped.append(family)
    return deduped


def _extract_satellite_platform_overrides(value: Any) -> Dict[str, List[str]]:
    """Collect explicit per-family platform selections from qualified
    config.satellite entries (e.g. ['modis-aqua', 'viirs-noaa20'] ->
    {'modis': ['aqua'], 'viirs': ['noaa20']}). A family with only
    unqualified entries (e.g. plain 'modis') contributes no override, so its
    platform list falls back to surface_reflectance.{family}_platforms or
    that family's own default.
    """
    overrides: Dict[str, List[str]] = {}
    for family, platform in _parse_satellite_tokens(value):
        if platform is None:
            continue
        overrides.setdefault(family, [])
        if platform not in overrides[family]:
            overrides[family].append(platform)
    return overrides


_SPATIAL_EXPORT_MODES = {"aoi_clip", "native_rect_halo"}


def _normalize_spatial_export(value: Any) -> Dict[str, Any]:
    """Validate surface_reflectance.spatial_export (shared by MODIS, VIIRS --
    both daily_l2g_legacy and l2_swath -- and GCOM-C).

    Unset -> {"mode": "aoi_clip", "halo_m": None}, which is a pure no-op:
    every sensor's own hardcoded/omitted-default halo behavior is preserved
    exactly (e.g. VIIRS L2 swath's own 10 km default halo is untouched when
    halo_m is None, not silently reset to 0).

    mode:
      - "aoi_clip" (default): unchanged existing behavior for every sensor.
      - "native_rect_halo": crop to AOI bbox (+halo_m if given) without
        AOI-polygon NaN-masking -- real native pixels outside the AOI
        polygon but inside the halo-expanded rectangle are kept.
    halo_m: extra meters to expand the AOI bbox by before cropping/searching
      (only meaningful together with native_rect_halo, or for VIIRS L2
      swath where it always applies). Must be >= 0.
    """
    if value is None:
        return {"mode": "aoi_clip", "halo_m": None}
    if not isinstance(value, dict):
        raise ValueError("config.surface_reflectance.spatial_export must be a mapping")

    mode = str(value.get("mode", "aoi_clip")).strip().lower()
    if mode not in _SPATIAL_EXPORT_MODES:
        raise ValueError(
            f"surface_reflectance.spatial_export.mode must be one of {sorted(_SPATIAL_EXPORT_MODES)} "
            f"(got {mode!r})"
        )

    halo_raw = value.get("halo_m")
    halo_m: Optional[float] = None
    if halo_raw is not None:
        try:
            halo_m = float(halo_raw)
        except (TypeError, ValueError):
            raise ValueError("surface_reflectance.spatial_export.halo_m must be a number")
        if halo_m < 0:
            raise ValueError("surface_reflectance.spatial_export.halo_m must not be negative")

    return {"mode": mode, "halo_m": halo_m}


def _normalize_activefire_satellites(value: Any) -> List[str]:
    if isinstance(value, str):
        sats = [v.strip().lower() for v in value.split(",") if v.strip()]
    elif isinstance(value, Sequence):
        sats = [str(v).strip().lower() for v in value if str(v).strip()]
    else:
        raise ValueError("config.firms.activefire_satellite must be string or list")

    allowed = {"modis", "viirs"}
    invalid = [s for s in sats if s not in allowed]
    if invalid:
        raise ValueError(
            f"Unsupported satellites in config.firms.activefire_satellite: {invalid}"
        )

    deduped: List[str] = []
    for sat in sats:
        if sat not in deduped:
            deduped.append(sat)
    return deduped


def _normalize_activefire_level(value: Any) -> str:
    """Validate the simplified top-level `activefire` setting.

    Returns 'sp', 'nrt', or 'none'. Raises ValueError on anything else --
    an unrecognized value must fail loudly, not silently fall back.
    """
    text = str(value).strip().lower()
    if text in {"none", "off", "disabled", ""}:
        return "none"
    if text == "sp":
        return "sp"
    if text == "nrt":
        return "nrt"
    raise ValueError(
        f"config.activefire must be one of 'SP', 'NRT', 'none' (got: {value!r})"
    )


def _normalize_firms_products(
    value: Any,
    *,
    default_products: List[str],
    field_name: str,
) -> List[str]:
    if value is None:
        raw = list(default_products)
    elif isinstance(value, str):
        raw = [v.strip() for v in value.split(",") if v.strip()]
    elif isinstance(value, Sequence):
        raw = [str(v).strip() for v in value if str(v).strip()]
    else:
        raise ValueError(f"{field_name} must be string or list")

    if not raw:
        raw = list(default_products)

    deduped: List[str] = []
    for product in raw:
        if product not in deduped:
            deduped.append(product)
    return deduped


def _normalize_buffer_km(value: Any) -> float:
    """Validate the top-level `buffer_km` setting.

    None/omitted -> 0.0 (exact pre-existing behavior). Negative values are
    rejected outright rather than silently clamped to 0, since a negative
    buffer is a config mistake, not a valid "no buffer" request.
    """
    if value is None:
        return 0.0
    try:
        km = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"config.buffer_km must be a number (got: {value!r})")
    if km < 0:
        raise ValueError(f"config.buffer_km must not be negative (got: {km})")
    return km


def _normalize_burned_area_products(value: Any) -> List[str]:
    """Validate the top-level `burned_area` setting.

    None/[]/omitted -> [] (no burned-area processing; exact pre-existing
    behavior for configs that don't set this key at all).
    """
    if value is None:
        return []
    if isinstance(value, str):
        raw = [v.strip() for v in value.split(",") if v.strip()]
    elif isinstance(value, Sequence):
        raw = [str(v).strip() for v in value if str(v).strip()]
    else:
        raise ValueError("config.burned_area must be string or list")

    allowed = {"MCD64A1", "VNP64A1"}
    normalized: List[str] = []
    for product in raw:
        upper = product.strip().upper()
        if upper not in allowed:
            raise ValueError(
                f"Unsupported burned_area product: {product!r}; supported: {sorted(allowed)}"
            )
        if upper not in normalized:
            normalized.append(upper)
    return normalized


def _as_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0

    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on", "y"}:
        return True
    if text in {"0", "false", "no", "off", "n"}:
        return False
    return default
