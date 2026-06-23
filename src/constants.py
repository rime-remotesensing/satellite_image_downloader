from __future__ import annotations

from typing import Dict, Tuple

PC_STAC_URL = "https://planetarycomputer.microsoft.com/api/stac/v1"

SENTINEL_COLLECTION = "sentinel-2-l2a"
LANDSAT_COLLECTION = "landsat-c2-l2"

SENTINEL_BAND_MAP: Dict[int, Tuple[str, str]] = {
    1: ("B01", "B01"),
    2: ("B02", "B02"),
    3: ("B03", "B03"),
    4: ("B04", "B04"),
    5: ("B05", "B05"),
    6: ("B06", "B06"),
    7: ("B07", "B07"),
    8: ("B08", "B08"),
    9: ("B8A", "B8A"),
    10: ("B09", "B09"),
    11: ("B11", "B11"),
    12: ("B12", "B12"),
}

LANDSAT_BAND_MAP: Dict[int, Tuple[str, str]] = {
    1: ("coastal", "B01"),
    2: ("blue", "B02"),
    3: ("green", "B03"),
    4: ("red", "B04"),
    5: ("nir08", "B05"),
    6: ("swir16", "B06"),
    7: ("swir22", "B07"),
    8: ("qa_aerosol", "B08"),
    9: ("qa_pixel", "B09"),
    10: ("lwir11", "B10"),
    11: ("lwir12", "B11"),
}

CLOUDMASK_REQUIRED_BANDS = {
    "sentinel2": [3, 4, 8],
    "landsat89": [3, 4, 5],
}

SNOWMASK_REQUIRED_BANDS = {
    "sentinel2": [3, 4, 11],
    "landsat89": [3, 4, 6],
}

TARGET_RESOLUTION = {
    "sentinel2": 10.0,
    "landsat89": 30.0,
}

WGS84 = (
    'GEOGCS["WGS 84",DATUM["WGS_1984",SPHEROID["WGS 84",6378137,298.257223563]],'
    'PRIMEM["Greenwich",0],UNIT["degree",0.0174532925199433]]'
)

GDAL_HTTP_OPTIONS = {
    "GDAL_HTTP_CONNECTTIMEOUT": "30",
    "GDAL_HTTP_TIMEOUT": "120",
    "GDAL_HTTP_MAX_RETRY": "5",
    "GDAL_HTTP_RETRY_DELAY": "10",
    "GDAL_HTTP_LOW_SPEED_TIME": "30",
    "GDAL_HTTP_LOW_SPEED_LIMIT": "10240",
}

DN_CONVERSION_PRESETS: Dict[str, Dict[str, float]] = {
    "sentinel2": {"scale": 1 / 10000, "offset": 0.0},
    "landsat8": {"scale": 0.0000275, "offset": -0.2},
    "landsat9": {"scale": 0.0000275, "offset": -0.2},
}
