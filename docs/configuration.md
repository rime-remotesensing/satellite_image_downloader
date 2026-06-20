# Configuration Reference

All settings are defined in a YAML file (default: `config/config.yaml`) and passed to the pipeline via:

```bash
python run.py --config config/config.yaml
```

---

## Core Inputs

| Key | Type | Description |
|-----|------|-------------|
| `geojson` | string | Path to a GeoJSON file (Polygon or MultiPolygon) defining the area of interest (AOI) |
| `startday` | string \| list | Start date(s) in `YYYYMMDD` format |
| `endday` | string \| list | End date(s) in `YYYYMMDD` format |
| `satellite` | list | Satellites to process. Options: `sentinel2`, `landsat89`, `modis`, `viirs` |
| `output` | string | Root directory for all outputs |

### Single date range

```yaml
startday: "20240101"
endday:   "20240131"
```

### Multiple date ranges (sequential loop)

Provide arrays of equal length. Each pair is processed independently:

```yaml
startday: [20230306, 20230311, 20230410]
endday:   [20230306, 20230311, 20230410]
```

> **Active fire (FIRMS)**: When multiple date ranges are specified, FIRMS data is fetched once for the span from `min(startday)` to `max(endday)`.

---

## Band Selection

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `band` | string | `all` | `all` = all bands; `at` = specific bands listed in `num` |
| `num` | list | `[]` | Band numbers to download when `band: at` (e.g. `[2, 3, 4, 8]`) |

> Bands required by omnicloudmask are always downloaded regardless of `num`.

---

## Cloud Masking

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `cloudmask` | list | `[1, 3]` | Cloud classes to mask. `1` = thick cloud, `2` = thin cloud, `3` = shadow |
| `max_cloud_cover` | int | `80` | Skip scenes with cloud cover above this percentage (STAC metadata filter) |

### omnicloudmask inference options

```yaml
omnicloudmask:
  batch_size: 1
  patch_size: 1000
  patch_overlap: 300
  device: cuda     # "cuda" or "cpu"
```

---

## Snow Masking

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `snowmask.enabled` | bool | `false` | Apply snow masking after cloud masking |
| `snowmask.ndsi_threshold` | float | `0.4` | NDSI threshold for snow detection |
| `snowmask.red_threshold` | float | `0.2` | Red band reflectance threshold for snow detection |

```yaml
snowmask:
  enabled: true
  ndsi_threshold: 0.4
  red_threshold: 0.2
```

---

## Acquisition Metadata

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `metadata.enabled` | bool | `false` | Save acquisition metadata as a GeoJSON file under `img/` |

Saved fields: `Acquisition_Date`, `Image_ID`, `Solar_Azimuth_Angle`, `Solar_Zenith_Angle`.

---

## GEE Compatibility (Sentinel-2)

These options align the output grid with Google Earth Engine exports.

```yaml
gee_compatible:
  enabled: true
  output_crs: EPSG:32652   # Target CRS (UTM zone matching your AOI)
  aoi_as_bbox: true        # Use AOI bounding box (like ee.Geometry.Rectangle)
  snap_grid: true          # Snap output to pixel-aligned coordinates
```

---

## File Handling

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `file_exists` | string | `skip` | What to do if an output file already exists. `skip` = keep existing, `overwrite` = re-process |
| `img_only` | bool | `false` | Only (re)generate `img/` scene stacks and metadata; skip cloud/snow masking |

CLI equivalent of `img_only`:

```bash
python run.py --config config/config.yaml --img-only
```

---

## FIRMS Active Fire Settings

To download active fire data, set `firms.activefire_satellite` **and** the corresponding `firms.product_map` entries. If either is missing, active fire data is not downloaded.

```yaml
firms:
  key_env_path: ./key.env          # Path to the file containing FIRMS_API_KEY
  api_key: ""                       # Alternative: set the key directly (not recommended)
  activefire_satellite:
    - viirs
    - modis
  product_map:
    modis:
      - MODIS_SP                    # Terra + Aqua combined standard product
    viirs:
      - VIIRS_SNPP_SP               # Suomi-NPP
      - VIIRS_NOAA20_SP             # NOAA-20
      - VIIRS_NOAA21_SP             # NOAA-21
```

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `firms.key_env_path` | string | `./key.env` | Path to the `.env` file with `FIRMS_API_KEY=...` |
| `firms.activefire_satellite` | list | — | Satellites to fetch. Options: `modis`, `viirs` |
| `firms.product_map.modis` | string \| list | — | MODIS product(s). `MODIS_SP` covers Terra/Aqua |
| `firms.product_map.viirs` | string \| list | — | VIIRS product(s). Use per-sensor names (e.g. `VIIRS_SNPP_SP`) |
| `firms.days` | int | `5` | Day range per FIRMS API request (1–5). Longer periods are split automatically |
| `firms.bbox_buffer_m` | int | `5000` | Expand the AOI bounding box by this distance (meters) when querying FIRMS |
| `firms.clip_to_aoi` | bool | `false` | Clip FIRMS output to the AOI polygon after download |
| `firms.pixel_tif` | bool | `false` | Also output active fire as a pixel-raster GeoTIFF |
| `firms.pixel_resolution` | int | `10` | Resolution of the pixel raster in meters |
| `firms.pixel_expand_to_detections` | bool | `true` | Expand the raster extent if detections fall outside the AOI grid |
| `firms.period_summary` | bool | `false` | Output one merged Shapefile covering the entire date range |

### Getting a FIRMS API key

1. Register at <https://firms.modaps.eosdis.nasa.gov/api/>
2. Copy your MAP_KEY
3. Create `key.env` in the project root:

```
FIRMS_API_KEY=your_map_key_here
```

> Do **not** put the key directly into `config.yaml`. The `key.env` file is excluded from git by `.gitignore`.

---

## Full Example

```yaml
geojson: ./config/area.geojson
startday: "20240101"
endday:   "20240131"
satellite:
  - sentinel2
  - landsat89
output: ./output

band: all
num: []

cloudmask: [1, 3]
max_cloud_cover: 80
file_exists: skip
img_only: false

gee_compatible:
  enabled: false
  output_crs: EPSG:32654
  aoi_as_bbox: true
  snap_grid: true

omnicloudmask:
  batch_size: 1
  patch_size: 1000
  patch_overlap: 300
  device: cuda

snowmask:
  enabled: false
  ndsi_threshold: 0.4
  red_threshold: 0.2

metadata:
  enabled: true

firms:
  key_env_path: ./key.env
  activefire_satellite:
    - viirs
    - modis
  product_map:
    modis:
      - MODIS_SP
    viirs:
      - VIIRS_SNPP_SP
      - VIIRS_NOAA20_SP
  days: 5
  bbox_buffer_m: 5000
  clip_to_aoi: false
  pixel_tif: false
  pixel_resolution: 10
  pixel_expand_to_detections: true
  period_summary: false
```
