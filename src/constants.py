from __future__ import annotations

from typing import Any, Dict, Optional, Tuple

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

# ---------------------------------------------------------------------------
# MODIS/VIIRS Sinusoidal Tile Grid
# ---------------------------------------------------------------------------
# Reference: NASA MODIS Land team, "MODIS Grid Home"
# (https://modis-land.gsfc.nasa.gov/MODLAND_grid.html). VIIRS L2G/L3 gridded
# products (VNP09GA) intentionally reuse this identical sinusoidal tile grid
# (36 x 18 tiles, h00-h35 / v00-v17) so MODIS and VIIRS tiles co-register
# pixel-for-pixel. These are fixed projection/grid constants, not per-scene
# radiometric values, so they are safe to define here rather than reading
# them from each granule.
MODIS_SINUSOIDAL_SPHERE_RADIUS_M = 6371007.181
MODIS_SINUSOIDAL_PROJ4 = (
    f"+proj=sinu +lon_0=0 +x_0=0 +y_0=0 +R={MODIS_SINUSOIDAL_SPHERE_RADIUS_M} +units=m +no_defs"
)
MODIS_SINUSOIDAL_X_MIN = -20015109.354
MODIS_SINUSOIDAL_Y_MAX = 10007554.677
MODIS_TILE_SIZE_M = (20015109.354 * 2.0) / 36.0  # 1111950.5197665554 m per 10-degree tile

EARTHDATA_URS_HOST = "urs.earthdata.nasa.gov"

SURFACE_REFLECTANCE_DEFAULT_PRODUCTS: Dict[str, Dict[str, str]] = {
    "modis": {
        "terra": "MOD09GA",
        "aqua": "MYD09GA",
        "version": "061",
    },
    "viirs": {
        "snpp": "VNP09GA",
        "version": "002",
    },
}

# MOD09GA/MYD09GA.061: 500 m surface reflectance bands + required QA/state layers.
MODIS_SR_BANDS = [f"sur_refl_b0{i}" for i in range(1, 8)]
MODIS_QA_BANDS = ["QC_500m", "state_1km"]

# VNP09GA.002: I-bands (nominal 500 m) and M-bands (nominal 1 km) surface
# reflectance, plus required quality-flag / land-water-mask layers.
VIIRS_SR_BANDS_500M = ["I1", "I2", "I3"]
VIIRS_SR_BANDS_1KM = ["M1", "M2", "M3", "M4", "M5", "M7", "M8", "M10", "M11"]
VIIRS_QA_BANDS = ["QF1", "QF2", "QF3", "QF4", "QF5", "QF6", "QF7", "land_water_mask"]

# ---------------------------------------------------------------------------
# Explicit on-disk SDS identifiers (verified against real granules)
# ---------------------------------------------------------------------------
# These map our canonical field_name (used for output grouping/filenames,
# unchanged) to the ACTUAL dataset name/path confirmed by directly inspecting
# real downloaded MOD09GA.061 / MYD09GA.061 / VNP09GA.002 granules. No
# fallback, tail-matching, or shape-based search is used to resolve these —
# an unrecognized/missing entry is a hard error, not a guess.
#
# VNP09GA.002 stores gridded (HDFEOS Grid) 2-D fields under
# HDFEOS/GRIDS/VIIRS_Grid_{500m,1km}_2D/Data Fields/ with a "_1" suffix
# (confirmed via h5py inspection of a real granule on 2023-03-05). The file
# also contains unrelated flat 1-D "_c" arrays at the top level with similar
# names; those are intentionally never read.
VIIRS_SDS_PATH_MAP: Dict[str, str] = {
    "I1": "HDFEOS/GRIDS/VIIRS_Grid_500m_2D/Data Fields/SurfReflect_I1_1",
    "I2": "HDFEOS/GRIDS/VIIRS_Grid_500m_2D/Data Fields/SurfReflect_I2_1",
    "I3": "HDFEOS/GRIDS/VIIRS_Grid_500m_2D/Data Fields/SurfReflect_I3_1",
    "M1": "HDFEOS/GRIDS/VIIRS_Grid_1km_2D/Data Fields/SurfReflect_M1_1",
    "M2": "HDFEOS/GRIDS/VIIRS_Grid_1km_2D/Data Fields/SurfReflect_M2_1",
    "M3": "HDFEOS/GRIDS/VIIRS_Grid_1km_2D/Data Fields/SurfReflect_M3_1",
    "M4": "HDFEOS/GRIDS/VIIRS_Grid_1km_2D/Data Fields/SurfReflect_M4_1",
    "M5": "HDFEOS/GRIDS/VIIRS_Grid_1km_2D/Data Fields/SurfReflect_M5_1",
    "M7": "HDFEOS/GRIDS/VIIRS_Grid_1km_2D/Data Fields/SurfReflect_M7_1",
    "M8": "HDFEOS/GRIDS/VIIRS_Grid_1km_2D/Data Fields/SurfReflect_M8_1",
    "M10": "HDFEOS/GRIDS/VIIRS_Grid_1km_2D/Data Fields/SurfReflect_M10_1",
    "M11": "HDFEOS/GRIDS/VIIRS_Grid_1km_2D/Data Fields/SurfReflect_M11_1",
    "QF1": "HDFEOS/GRIDS/VIIRS_Grid_1km_2D/Data Fields/SurfReflect_QF1_1",
    "QF2": "HDFEOS/GRIDS/VIIRS_Grid_1km_2D/Data Fields/SurfReflect_QF2_1",
    "QF3": "HDFEOS/GRIDS/VIIRS_Grid_1km_2D/Data Fields/SurfReflect_QF3_1",
    "QF4": "HDFEOS/GRIDS/VIIRS_Grid_1km_2D/Data Fields/SurfReflect_QF4_1",
    "QF5": "HDFEOS/GRIDS/VIIRS_Grid_1km_2D/Data Fields/SurfReflect_QF5_1",
    "QF6": "HDFEOS/GRIDS/VIIRS_Grid_1km_2D/Data Fields/SurfReflect_QF6_1",
    "QF7": "HDFEOS/GRIDS/VIIRS_Grid_1km_2D/Data Fields/SurfReflect_QF7_1",
    "land_water_mask": "HDFEOS/GRIDS/VIIRS_Grid_1km_2D/Data Fields/land_water_mask_1",
}

# Expected native pixel dimensions for a standard (non-polar-edge) sinusoidal
# tile, used only to record/validate what was actually read — never to
# search for or select a dataset.
VIIRS_EXPECTED_SHAPE_500M = (2400, 2400)
VIIRS_EXPECTED_SHAPE_1KM = (1200, 1200)
MODIS_EXPECTED_SHAPE_500M = (2400, 2400)
MODIS_EXPECTED_SHAPE_1KM = (1200, 1200)

# MOD09GA.061 / MYD09GA.061: confirmed via pyhdf.SD.datasets() against real
# granules (see smoke-test report). "_1" denotes the first-layer 2-D SDS;
# "_c"/"_f" ancillary layers are intentionally never read.
MODIS_SDS_NAME_MAP: Dict[str, str] = {
    "sur_refl_b01": "sur_refl_b01_1",
    "sur_refl_b02": "sur_refl_b02_1",
    "sur_refl_b03": "sur_refl_b03_1",
    "sur_refl_b04": "sur_refl_b04_1",
    "sur_refl_b05": "sur_refl_b05_1",
    "sur_refl_b06": "sur_refl_b06_1",
    "sur_refl_b07": "sur_refl_b07_1",
    "QC_500m": "QC_500m_1",
    "state_1km": "state_1km_1",
}

# ---------------------------------------------------------------------------
# FIRMS activefire level -> source mapping
# ---------------------------------------------------------------------------
# Confirmed against the official NASA FIRMS API docs
# (https://firms.modaps.eosdis.nasa.gov/api/area/,
# https://firms.modaps.eosdis.nasa.gov/api/data_availability/). VIIRS
# NOAA-21 Standard Processing (SP) does not exist as a source -- NOAA-21 is
# NRT-only -- so its "sp" entry is None. Callers must skip + warn on None,
# never silently substitute the NRT source.
FIRMS_SOURCE_BY_LEVEL: Dict[str, Dict[str, Dict[str, Optional[str]]]] = {
    "sp": {
        "modis": {"modis": "MODIS_SP"},
        "viirs": {"snpp": "VIIRS_SNPP_SP", "noaa20": "VIIRS_NOAA20_SP", "noaa21": None},
    },
    "nrt": {
        "modis": {"modis": "MODIS_NRT"},
        "viirs": {
            "snpp": "VIIRS_SNPP_NRT",
            "noaa20": "VIIRS_NOAA20_NRT",
            "noaa21": "VIIRS_NOAA21_NRT",
        },
    },
}

# ---------------------------------------------------------------------------
# Internal defaults for settings no longer exposed in config.yaml
# ---------------------------------------------------------------------------
# These preserve the exact effective behavior that the project's config.yaml
# used to pin explicitly, now that config.yaml only exposes user-facing keys
# (see docs/configuration.md "Internal defaults / advanced behavior").
DEFAULT_OUTPUT_DIR = "output"
DEFAULT_FILE_EXISTS = "skip"
DEFAULT_MAX_CLOUD_COVER = 80

OMNICLOUDMASK_DEFAULTS: Dict[str, Any] = {
    "batch_size": 1,
    "patch_size": 1000,
    "patch_overlap": 300,
    "device": "cuda",
}

SNOWMASK_DEFAULT_NDSI_THRESHOLD = 0.4
SNOWMASK_DEFAULT_RED_THRESHOLD = 0.2

FIRMS_DEFAULT_KEY_ENV_PATH = "key.env"
FIRMS_DEFAULT_BASE_URL = "https://firms.modaps.eosdis.nasa.gov/api/area/csv"
FIRMS_DEFAULT_BBOX_BUFFER_M = 5000.0
FIRMS_DEFAULT_CLIP_TO_AOI = False
FIRMS_DEFAULT_DAYS = 5
FIRMS_DEFAULT_PERIOD_SUMMARY = True
# Active fire pixel-raster (activefire_tif/) output is opt-in only now; the
# point/event data (Shapefile) remains the primary FIRMS output.
FIRMS_DEFAULT_PIXEL_TIF = False
FIRMS_DEFAULT_PIXEL_RESOLUTION = 10.0
FIRMS_DEFAULT_PIXEL_EXPAND_TO_DETECTIONS = True
