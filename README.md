# satellite-image-downloader

A config-driven pipeline for downloading and preprocessing satellite imagery from [Microsoft Planetary Computer](https://planetarycomputer.microsoft.com/) and active fire data from [NASA FIRMS](https://firms.modaps.eosdis.nasa.gov/).

## Features

- **Satellite support**: Sentinel-2 L2A and Landsat 8/9 L2
- **AOI-based clipping**: Provide a GeoJSON polygon to clip imagery to your area of interest
- **Automatic cloud masking**: Uses [omnicloudmask](https://github.com/DPIRD-DMA/OmniCloudMask) for accurate cloud and shadow detection
- **Snow masking**: Optional NDSI-based snow masking
- **Daily compositing**: Merges multiple scenes from the same date into one image
- **Active fire data**: Downloads FIRMS MODIS/VIIRS active fire detections as Shapefiles and pixel rasters
- **GPU acceleration**: Cloud masking runs on CUDA GPU when available
- **Docker support**: Fully containerized environment for reproducible execution

## Requirements

- Python 3.10+
- GDAL (system library)
- CUDA-capable NVIDIA GPU (optional, recommended for cloud masking)
- Docker + NVIDIA Container Toolkit (for Docker-based execution)

## Installation

```bash
git clone https://github.com/your-username/satellite-image-downloader.git
cd satellite-image-downloader
pip install -r env/requirements.txt
```

> **Note**: GDAL must be installed on your system before installing Python packages.
> - Ubuntu/Debian: `sudo apt-get install gdal-bin libgdal-dev`
> - macOS: `brew install gdal`
> - Windows: Use [OSGeo4W](https://trac.osgeo.org/osgeo4w/) or a conda environment.

## Quick Start

### 1. Prepare your AOI

Create a GeoJSON file containing your area of interest (Polygon or MultiPolygon) and save it as `config/area.geojson`:

```json
{
  "type": "FeatureCollection",
  "features": [
    {
      "type": "Feature",
      "geometry": {
        "type": "Polygon",
        "coordinates": [[[139.5, 35.5], [140.0, 35.5], [140.0, 36.0], [139.5, 36.0], [139.5, 35.5]]]
      },
      "properties": {}
    }
  ]
}
```

### 2. Edit the config file

Edit `config/config.yaml`:

```yaml
geojson: ./config/area.geojson
startday: "20240101"
endday:   "20240131"
satellite:
  - sentinel2
output: ./output
```

See [Configuration Reference](docs/configuration.md) for all available options.

### 3. Set up your FIRMS API key (for active fire data)

Register for a free key at <https://firms.modaps.eosdis.nasa.gov/api/> and create `key.env` in the project root:

```
FIRMS_API_KEY=your_api_key_here
```

> `key.env` is excluded from git by `.gitignore` — your key will not be committed.

### 4. Run

```bash
python run.py --config config/config.yaml
```

## Python API

You can call the pipeline directly from Python for programmatic use:

```python
from src.pipeline import satellite_image_downloader

satellite_image_downloader(
    satellite_type=["sentinel2", "landsat89"],
    geojson_path="config/area.geojson",
    sdate="20240101",
    edate="20240131",
    output_path="output",
)
```

### Multiple date ranges

Pass lists to `sdate` / `edate` to process several date windows in sequence:

```python
satellite_image_downloader(
    satellite_type=["sentinel2"],
    geojson_path="config/area.geojson",
    sdate=[20230306, 20230311, 20230410],
    edate=[20230306, 20230311, 20230410],
    output_path="output",
)
```

### Multiple regions

Loop over a list of regions to download each one in turn:

```python
regions = [
    {"geojson": "config/region_a.geojson", "output": "output/region_a"},
    {"geojson": "config/region_b.geojson", "output": "output/region_b"},
]

for r in regions:
    satellite_image_downloader(
        satellite_type=["sentinel2"],
        geojson_path=r["geojson"],
        sdate="20240101",
        edate="20240131",
        output_path=r["output"],
    )
```

You can also edit `BATCH_MODE_REGIONS` and `REGION_DOWNLOAD_DATES` in `run.py` and run:

```bash
python run.py --batch
```

## Customizing run.py for batch downloads

`run.py` is designed to be edited directly for your own data download plan.
The three variables to configure are at the top of the file:

### 1. `BATCH_MODE_REGIONS` — list of regions and their GeoJSON files

```python
BATCH_MODE_REGIONS = [
    ("region_a", "config/region_a.geojson"),
    ("region_b", "config/region_b.geojson"),
]
```

Each tuple is `(region_name, path_to_geojson)`. Create one GeoJSON per area of interest and list them here.

### 2. `REGION_DOWNLOAD_DATES` — dates to download per region

```python
REGION_DOWNLOAD_DATES = {
    "region_a": {
        "2024": ["20240101", "20240115", "20240201"],
    },
    "region_b": {
        "2023": ["20230301", "20230401"],
        "2024": ["20240101"],
    },
}
```

Each date is processed as a single-day window (`startday == endday`). Add as many years and dates as needed.

### 3. `BASE_PATH` — root output directory

```python
BASE_PATH = Path(
    os.environ.get(
        "SATDL_BASE_PATH",
        os.environ.get("SATDL_HOST_DATA_PATH", "/host_data") + "/your_project/output",
    )
)
```

Change the fallback path (`/host_data/your_project/output`) to your preferred output location, or set it at runtime via the `SATDL_BASE_PATH` environment variable (see [Docker Guide](docs/docker.md)).

Output is saved to `<BASE_PATH>/<region_name>/<year>/`.

### Running batch mode

```bash
# Local
python run.py --batch

# Docker
docker compose run --rm downloader python3 run.py --batch

# Regenerate img/ only (skip re-masking)
python run.py --batch --img-only
```

## Output Structure

```
output/
├── sentinel2/
│   ├── img/              # Raw scene stacks (one multi-band TIFF per scene)
│   ├── masked/           # Cloud-masked daily composites
│   ├── snowmasked/       # Cloud + snow masked daily composites
│   └── cloudmask/        # Cloud and snow mask layers
├── landsat89/
│   ├── img/
│   ├── masked/
│   ├── snowmasked/
│   └── cloudmask/
├── modis/
│   ├── activefire/       # MODIS active fire Shapefiles
│   └── activefire_tif/   # MODIS active fire pixel rasters
└── viirs/
    ├── activefire/       # VIIRS active fire Shapefiles
    └── activefire_tif/   # VIIRS active fire pixel rasters
```

> `img/` stores raw per-scene data. All other directories store daily composites (scenes from the same date are merged).
> When `metadata.enabled: true`, a GeoJSON file with acquisition metadata is also saved under `img/`.

## Docker

See the [Docker Guide](docs/docker.md) for containerized execution, GPU setup, and batch processing.

## Configuration Reference

See the [Configuration Reference](docs/configuration.md) for every available option.

## Technical Notes

- **Sentinel-2 processing baseline**: Scenes with `s2:processing_baseline >= 4.0` are automatically corrected by subtracting the `RADIO_ADD_OFFSET` (1000 DN). Reflectance conversion (÷10000) is applied in the masked/snowmasked outputs.
- **FIRMS request limits**: The FIRMS area API allows a maximum day range of 1–5 per request. Longer periods are automatically split and merged internally.
- **Active fire CRS**: Output CRS matches the Sentinel-2 image CRS for the AOI (falls back to EPSG:4326 if not available).
- **Model caching**: On first run, omnicloudmask downloads its model weights from Hugging Face. Subsequent runs use the cached weights (see [Docker Guide](docs/docker.md) for volume caching).

## License

MIT License — see [LICENSE](LICENSE) for details.
