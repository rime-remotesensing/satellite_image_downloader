# satellite-image-downloader

[Microsoft Planetary Computer](https://planetarycomputer.microsoft.com/) から Sentinel-2 / Landsat 8・9 衛星画像を、[NASA FIRMS](https://firms.modaps.eosdis.nasa.gov/) から熱異常（アクティブファイア）データを自動ダウンロード・前処理する設定ファイル駆動のパイプラインです。

## 機能

- **対応衛星**: Sentinel-2 L2A / Landsat 8・9 L2
- **AOI クリッピング**: GeoJSON ポリゴンで任意の領域に切り抜き
- **自動雲マスク**: [omnicloudmask](https://github.com/DPIRD-DMA/OmniCloudMask) による雲・影マスク
- **雪マスク**: NDSI ベースの雪マスク（オプション）
- **同日コンポジット**: 同日の複数シーンを最小値合成で1枚に統合
- **熱異常（アクティブファイア）データ**: FIRMS MODIS/VIIRS の熱異常域を Shapefile + ラスタで取得
- **GPU 対応**: CUDA GPU があれば omnicloudmask の推論を高速化
- **Docker 対応**: 依存関係を含む再現可能な実行環境

---

## セットアップ

### 必要なもの

- [Git](https://git-scm.com/)
- **Docker を使う場合（推奨）**: [Docker Desktop](https://www.docker.com/products/docker-desktop/)（Windows/Mac）または Docker Engine（Linux）
- **ローカル環境を使う場合**: Python 3.10 以上、GDAL

### 1. リポジトリをクローンする

```bash
git clone https://github.com/your-username/satellite-image-downloader.git
cd satellite-image-downloader
```

---

### Docker で実行する場合（推奨）

Docker を使うと、Python・GDAL・GPU 依存パッケージを手動インストールする必要はありません。

#### CUDA バージョンの確認と設定（GPU 使用時）

GPU を使う場合のみ、**Dockerfile の CUDA バージョンをホスト環境に合わせる必要があります**。
CPU のみで動かす場合はこの手順をスキップできます。

**ステップ 1 — ホスト側の CUDA バージョンを確認する**

```bash
nvidia-smi
```

出力の右上に `CUDA Version: XX.X` と表示されます。

**ステップ 2 — [env/Dockerfile](env/Dockerfile) の2行を変更する**

```dockerfile
# ① ベースイメージ: cuda バージョンをホストに合わせる
FROM nvidia/cuda:12.8.1-cudnn8-runtime-ubuntu22.04
#                 ^^^^  ここを変える

# ② PyTorch ビルド: cu128 の部分をホストに合わせる
ARG TORCH_INDEX_URL=https://download.pytorch.org/whl/cu128
#                                                   ^^^^^ ここを変える
```

| ホスト CUDA | ① ベースイメージ | ② PyTorch |
|-------------|-----------------|-----------|
| 11.8 | `nvidia/cuda:11.8.0-cudnn8-runtime-ubuntu22.04` | `cu118` |
| 12.1 | `nvidia/cuda:12.1.1-cudnn8-runtime-ubuntu22.04` | `cu121` |
| 12.4 | `nvidia/cuda:12.4.1-cudnn9-runtime-ubuntu22.04` | `cu124` |
| 12.8 (デフォルト) | `nvidia/cuda:12.8.1-cudnn8-runtime-ubuntu22.04` | `cu128` |

#### Docker イメージをビルドする

```bash
docker compose build downloader
```

**GPU が正しく認識されているか確認**（任意）：

```bash
docker compose run --rm downloader python3 -c "import torch; print(torch.cuda.is_available(), torch.cuda.get_device_name(0) if torch.cuda.is_available() else 'no GPU')"
```

---

### ローカル環境で実行する場合

**① GDAL をインストールする（`pip install` より先に行う必要があります）**

```bash
# Ubuntu/Debian
sudo apt-get install gdal-bin libgdal-dev

# macOS
brew install gdal
```

Windows では [OSGeo4W](https://trac.osgeo.org/osgeo4w/) または conda 環境（`conda install gdal`）を推奨します。

**② 依存パッケージをインストールする**

```bash
pip install -r env/requirements.txt
```

---

## クイックスタート

### 1. AOI ファイルを用意する

対象地域を Polygon または MultiPolygon の GeoJSON で記述し、`config/area.geojson` として保存します。

> [geojson.io](https://geojson.io/) を使うと、地図上で領域を描いてそのまま GeoJSON として保存できます。

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

### 2. 設定ファイルを編集する

`config/config.yaml` を編集します：

```yaml
geojson: ./config/area.geojson
startday: "20240101"
endday:   "20240131"
satellite:
  - sentinel2
output: ./output
```

設定項目の詳細は [設定リファレンス](docs/configuration.md) を参照してください。

### 3. FIRMS API キーを設定する（熱異常データが必要な場合）

<https://firms.modaps.eosdis.nasa.gov/api/> で無料登録し、`key.env` をプロジェクトルートに作成：

```
FIRMS_API_KEY=your_api_key_here
```

> `key.env` は `.gitignore` で除外されているためコミットされません。

### 4. 実行する

```bash
# ローカル実行
python run.py --config config/config.yaml

# Docker 実行
docker compose run --rm downloader python3 run.py --config config/config.yaml
```

---

## Python API

スクリプトから直接呼び出すこともできます：

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

**複数日付をまとめてループ実行する**（`sdate`/`edate` を配列で渡す）：

```python
satellite_image_downloader(
    satellite_type=["sentinel2"],
    geojson_path="config/area.geojson",
    sdate=[20230306, 20230311, 20230410],
    edate=[20230306, 20230311, 20230410],
    output_path="output",
)
```

**複数リージョンをループ実行する**：

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

---

## run.py のカスタマイズ（バッチダウンロード）

`run.py` はファイルを直接編集して自分のダウンロード計画に合わせて使うことを想定しています。
変更する箇所は冒頭の3つの変数です。

### 1. `BATCH_MODE_REGIONS` — リージョンと GeoJSON の対応表

```python
BATCH_MODE_REGIONS = [
    ("region_a", "config/region_a.geojson"),
    ("region_b", "config/region_b.geojson"),
]
```

タプルは `(リージョン名, GeoJSON パス)` です。対象地域ごとに GeoJSON を作成してここに列挙します。

### 2. `REGION_DOWNLOAD_DATES` — 日付の設定

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

各日付は `startday == endday` の1日単位で処理されます。年をキーにしてまとめると管理しやすいです。

### 3. `BASE_PATH` — 出力先ルートディレクトリ

```python
BASE_PATH = Path(
    os.environ.get(
        "SATDL_BASE_PATH",
        os.environ.get("SATDL_HOST_DATA_PATH", "/host_data") + "/your_project/output",
    )
)
```

フォールバックパス（`/host_data/your_project/output` の部分）を自分の出力先に変更するか、環境変数 `SATDL_BASE_PATH` で実行時に指定します。

出力は `<BASE_PATH>/<リージョン名>/<年>/` に保存されます。

### バッチ実行

```bash
# ローカル
python run.py --batch

# Docker
docker compose run --rm downloader python3 run.py --batch
```

---

## 出力ディレクトリ構成

```
output/
├── sentinel2/
│   ├── img/              # 生データ（シーン単位のマルチバンド TIFF）
│   ├── masked/           # 雲マスク適用済み（同日コンポジット）
│   ├── snowmasked/       # 雲+雪マスク適用済み（同日コンポジット）
│   └── cloudmask/        # 雲マスク・雪マスクレイヤ
├── landsat89/
│   ├── img/
│   ├── masked/
│   ├── snowmasked/
│   └── cloudmask/
├── modis/
│   ├── activefire/       # MODIS 熱異常 Shapefile
│   └── activefire_tif/   # MODIS 熱異常ピクセルラスタ
└── viirs/
    ├── activefire/       # VIIRS 熱異常 Shapefile
    └── activefire_tif/   # VIIRS 熱異常ピクセルラスタ
```

> `img/` はシーン単位の生データを保存します。それ以外（`masked` / `snowmasked` / `cloudmask`）は同日コンポジット後の結果です。
> `metadata.enabled: true` にすると、撮影メタデータ GeoJSON が `img/` 配下に保存されます。

---

## Docker での実行

詳細は [Docker ガイド](docs/docker.md) を参照してください（CUDA バージョン変更・外部パスマウント・バッチ実行・モデルキャッシュ）。

## 設定リファレンス

`config/config.yaml` の全オプションは [設定リファレンス](docs/configuration.md) を参照してください。

---

## 補足

- **Sentinel-2 処理基準**: `s2:processing_baseline >= 4.0` のシーンは `RADIO_ADD_OFFSET`（1000 DN）を自動補正します。反射率変換（÷10000）は `masked`/`snowmasked` 等の後段で適用します。
- **FIRMS リクエスト制限**: FIRMS area API は1リクエストあたり最大5日間です。長い期間は内部で自動分割して取得・統合します。
- **熱異常データの CRS**: AOI に対応する Sentinel-2 画像の CRS に合わせます（判定できない場合は EPSG:4326）。
- **モデルキャッシュ**: 初回実行時に omnicloudmask がモデルをダウンロードします。Docker では名前付きボリュームにキャッシュされるため、2回目以降は再ダウンロード不要です。

## ライセンス

MIT License
