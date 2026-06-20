from __future__ import annotations

import os
import re
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

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


def _normalize_satellites(value: Any) -> List[str]:
    if isinstance(value, str):
        sats = [v.strip().lower() for v in value.split(",") if v.strip()]
    elif isinstance(value, Sequence):
        sats = [str(v).strip().lower() for v in value if str(v).strip()]
    else:
        raise ValueError("config.satellite must be string or list")

    allowed = {"sentinel2", "landsat89", "modis", "viirs"}
    invalid = [s for s in sats if s not in allowed]
    if invalid:
        raise ValueError(f"Unsupported satellites in config: {invalid}")

    deduped: List[str] = []
    for sat in sats:
        if sat not in deduped:
            deduped.append(sat)
    return deduped


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
