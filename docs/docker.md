# Docker Guide

This project includes a Docker setup for reproducible, GPU-accelerated execution. The container bundles Python, GDAL, and all Python dependencies.

## Prerequisites

- [Docker Desktop](https://www.docker.com/products/docker-desktop/) (Windows/Mac) or Docker Engine (Linux)
- [NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/install-guide.html) for GPU support

## Matching the CUDA version to your GPU

> **Important**: The Dockerfile is pinned to CUDA 12.8 and the matching PyTorch build.
> If your host GPU driver supports a different CUDA version, you must update two lines in [env/Dockerfile](../env/Dockerfile) before building.

Open `env/Dockerfile` and change these two lines to match your installed CUDA version:

```dockerfile
# Line 1 — base image: change "12.8.1-cudnn8-runtime-ubuntu22.04" to match your driver
FROM nvidia/cuda:12.8.1-cudnn8-runtime-ubuntu22.04

# Line 2 — PyTorch index: change "cu128" to match (e.g. cu118, cu121, cu124, cu128)
ARG TORCH_INDEX_URL=https://download.pytorch.org/whl/cu128
```

**How to check your supported CUDA version:**

```bash
nvidia-smi
```

Look for `CUDA Version: XX.X` in the output. Then:

| Host CUDA version | Base image tag | PyTorch index suffix |
|-------------------|----------------|----------------------|
| 11.8 | `nvidia/cuda:11.8.0-cudnn8-runtime-ubuntu22.04` | `cu118` |
| 12.1 | `nvidia/cuda:12.1.1-cudnn8-runtime-ubuntu22.04` | `cu121` |
| 12.4 | `nvidia/cuda:12.4.1-cudnn9-runtime-ubuntu22.04` | `cu124` |
| 12.8 (default) | `nvidia/cuda:12.8.1-cudnn8-runtime-ubuntu22.04` | `cu128` |

Available PyTorch builds: <https://download.pytorch.org/whl/torch/>
Available CUDA base images: <https://hub.docker.com/r/nvidia/cuda/tags>

After editing, rebuild the image:

```bash
docker compose build downloader
```

## Build the image

Run this once (or after updating `env/Dockerfile` or `env/requirements.txt`):

```bash
docker compose build downloader
```

## Basic usage

Run with a config file:

```bash
docker compose run --rm downloader python3 run.py --config config/config.yaml
```

The default `command` in `docker-compose.yml` also points to this config, so you can just run:

```bash
docker compose run --rm downloader
```

## Verify GPU access

```bash
docker compose run --rm downloader python3 -c "
import torch
print('torch:', torch.__version__)
print('cuda_available:', torch.cuda.is_available())
if torch.cuda.is_available():
    print('device:', torch.cuda.get_device_name(0))
"
```

## Output to a directory outside the project

By default, the project directory is mounted at `/workspace` inside the container, and relative paths like `./output` in your config resolve to `/workspace/output` on the host.

To write output to a different host directory, set `SATDL_HOST_DATA_PATH` to that path. It will be mounted at `/host_data` inside the container.

**Linux / macOS (bash):**

```bash
SATDL_HOST_DATA_PATH=/path/to/your/data \
docker compose run --rm \
  -e SATDL_BASE_PATH=/host_data/my_project/output \
  downloader python3 run.py --config config/config.yaml
```

**Windows (PowerShell):**

```powershell
$env:SATDL_HOST_DATA_PATH = "D:\your\data"
docker compose run --rm `
  -e SATDL_BASE_PATH=/host_data/my_project/output `
  downloader python3 run.py --config config/config.yaml
```

In your `config/config.yaml`, reference the container-side path:

```yaml
geojson: /host_data/config/area.geojson
output:  /host_data/my_project/output
```

## Model caching

On the first run, omnicloudmask downloads its model weights from Hugging Face. This can take a few minutes. The Docker Compose file persists these caches in named volumes so subsequent runs start immediately:

| Volume | Contents |
|--------|----------|
| `satdl_model_cache` | Hugging Face and PyTorch caches (`~/.cache`) |
| `satdl_model_data` | omnicloudmask model weights (`~/.local/share`) |

To force a re-download (clears both volumes):

```bash
docker volume rm satellite_image_downloader_satdl_model_cache
docker volume rm satellite_image_downloader_satdl_model_data
```

## Batch mode

Batch mode downloads imagery for multiple regions and date ranges defined in `run.py`.

Edit `BATCH_MODE_REGIONS` and `REGION_DOWNLOAD_DATES` in `run.py` to match your regions and dates:

```python
BATCH_MODE_REGIONS = [
    ("region_a", "config/region_a.geojson"),
    ("region_b", "config/region_b.geojson"),
]

REGION_DOWNLOAD_DATES = {
    "region_a": {
        "2024": ["20240101", "20240115", "20240201"],
    },
    "region_b": {
        "2024": ["20240101", "20240201"],
    },
}
```

Then run:

```bash
# Local
python run.py --batch

# Docker
docker compose run --rm downloader python3 run.py --batch
```

To control the output root directory in batch mode, set `SATDL_BASE_PATH`:

```bash
# Linux / macOS
SATDL_HOST_DATA_PATH=/path/to/data \
docker compose run --rm \
  -e SATDL_BASE_PATH=/host_data/output \
  downloader python3 run.py --batch

# Windows PowerShell
$env:SATDL_HOST_DATA_PATH = "D:\your\data"
docker compose run --rm `
  -e SATDL_BASE_PATH=/host_data/output `
  downloader python3 run.py --batch
```

Output is saved to `<SATDL_BASE_PATH>/<region_name>/<year>/`.

## Path summary

| Scenario | Path in config.yaml |
|----------|---------------------|
| Output within project directory | `./output` |
| Output on a different host drive (Docker) | `/host_data/...` (set `SATDL_HOST_DATA_PATH` on host) |
| Output on a different host drive (local) | Absolute path, e.g. `/data/output` or `D:\data\output` |

> When using Docker, paths in `config.yaml` must use the **container-side** path (`/host_data/...`), not the host path.
