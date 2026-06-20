# Docker ガイド

再現可能な GPU 対応実行環境として Docker Compose を提供しています。Python・GDAL・全依存パッケージがコンテナに含まれています。

---

## 事前準備

- [Docker Desktop](https://www.docker.com/products/docker-desktop/)（Windows/Mac）または Docker Engine（Linux）
- GPU を使う場合: [NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/install-guide.html)

---

## CUDA バージョンの合わせ方（GPU 使用時は必須）

> GPU を使う場合、**Dockerfile の CUDA バージョンをホスト環境に合わせる必要があります**。
> 合っていないと `torch.cuda.is_available()` が `False` になり CPU で動作します。

### ステップ 1 — ホストの CUDA バージョンを確認

```bash
nvidia-smi
```

右上の `CUDA Version: XX.X` を確認します。

### ステップ 2 — [env/Dockerfile](../env/Dockerfile) を編集

```dockerfile
# ① ベースイメージの CUDA バージョンを変える
FROM nvidia/cuda:12.8.1-cudnn8-runtime-ubuntu22.04
#                 ^^^^  ← ここを変える

# ② PyTorch ビルドを合わせる
ARG TORCH_INDEX_URL=https://download.pytorch.org/whl/cu128
#                                                   ^^^^^ ← ここを変える
```

| ホスト CUDA | ① ベースイメージタグ | ② PyTorch サフィックス |
|-------------|--------------------|-----------------------|
| 11.8 | `nvidia/cuda:11.8.0-cudnn8-runtime-ubuntu22.04` | `cu118` |
| 12.1 | `nvidia/cuda:12.1.1-cudnn8-runtime-ubuntu22.04` | `cu121` |
| 12.4 | `nvidia/cuda:12.4.1-cudnn9-runtime-ubuntu22.04` | `cu124` |
| 12.8（デフォルト） | `nvidia/cuda:12.8.1-cudnn8-runtime-ubuntu22.04` | `cu128` |

参考リンク:
- PyTorch ビルド一覧: <https://download.pytorch.org/whl/torch/>
- CUDA ベースイメージ一覧: <https://hub.docker.com/r/nvidia/cuda/tags>

### ステップ 3 — イメージを再ビルド

```bash
docker compose build downloader
```

### GPU 認識の確認

```bash
docker compose run --rm downloader python3 -c "
import torch
print('torch:', torch.__version__)
print('cuda_available:', torch.cuda.is_available())
if torch.cuda.is_available():
    print('device:', torch.cuda.get_device_name(0))
"
```

---

## 基本的な使い方

設定ファイルを指定して実行：

```bash
docker compose run --rm downloader python3 run.py --config config/config.yaml
```

`docker-compose.yml` のデフォルト `command` も同じ設定を指しているため、以下でも動作します：

```bash
docker compose run --rm downloader
```

---

## プロジェクト外のディレクトリに出力する

デフォルトではプロジェクトディレクトリが `/workspace` にマウントされ、`./output` のような相対パスはそこを基準に解決されます。

別のホストディレクトリに出力する場合は、`SATDL_HOST_DATA_PATH` にそのパスを設定します。コンテナ内の `/host_data` にマウントされます。

**Linux / macOS:**

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

`config/config.yaml` ではコンテナ側のパスを指定します：

```yaml
geojson: /host_data/config/area.geojson
output:  /host_data/my_project/output
```

---

## モデルキャッシュ

初回実行時、omnicloudmask が Hugging Face からモデルをダウンロードします（数分かかる場合があります）。
Docker Compose では名前付きボリュームでキャッシュを永続化しているため、2回目以降は再ダウンロードを省略できます。

| ボリューム | 内容 |
|-----------|------|
| `satdl_model_cache` | Hugging Face / PyTorch キャッシュ（`~/.cache`） |
| `satdl_model_data` | omnicloudmask のモデル本体（`~/.local/share`） |

キャッシュをクリアして再取得したい場合：

```bash
docker volume rm satellite_image_downloader_satdl_model_cache
docker volume rm satellite_image_downloader_satdl_model_data
```

---

## バッチモード

複数リージョン・複数日付を一括ダウンロードするバッチモードです。
`run.py` の `BATCH_MODE_REGIONS` と `REGION_DOWNLOAD_DATES` を自分の用途に合わせて編集してから実行します（詳細は [README](../README.md#runpy-のカスタマイズバッチダウンロード) 参照）。

```bash
# ローカル
python run.py --batch

# Docker
docker compose run --rm downloader python3 run.py --batch
```

出力先をバッチモードで変更する場合（`SATDL_BASE_PATH` を環境変数で指定）：

**Linux / macOS:**

```bash
SATDL_HOST_DATA_PATH=/path/to/data \
docker compose run --rm \
  -e SATDL_BASE_PATH=/host_data/output \
  downloader python3 run.py --batch
```

**Windows (PowerShell):**

```powershell
$env:SATDL_HOST_DATA_PATH = "D:\your\data"
docker compose run --rm `
  -e SATDL_BASE_PATH=/host_data/output `
  downloader python3 run.py --batch
```

出力は `<SATDL_BASE_PATH>/<リージョン名>/<年>/` に保存されます。

---

## パスのまとめ

| 実行方法 | config.yaml でのパス指定 |
|---------|------------------------|
| プロジェクト内に出力 | `./output` |
| Docker で別ドライブに出力 | `/host_data/...`（`SATDL_HOST_DATA_PATH` をホストで設定） |
| ローカルで別ドライブに出力 | 絶対パス（例: `/data/output` や `D:\data\output`） |

> Docker 実行時は `config.yaml` 内のパスをコンテナ側のパス（`/host_data/...`）で指定してください。ホスト側のパスはコンテナから見えません。
