# satellite-image-downloader

[Microsoft Planetary Computer](https://planetarycomputer.microsoft.com/) から Sentinel-2 / Landsat 8・9 衛星画像を、[NASA Earthdata](https://urs.earthdata.nasa.gov/) から MODIS/VIIRS の daily Surface Reflectance を、[JAXA G-Portal](https://www.gportal.jaxa.jp/) から GCOM-C/SGLI の L2 大気補正済み反射率（RSRF）を、[NASA FIRMS](https://firms.modaps.eosdis.nasa.gov/) から熱異常（アクティブファイア）データを自動ダウンロード・前処理する設定ファイル駆動のパイプラインです。

## 機能

- **対応衛星**: Sentinel-2 L2A / Landsat 8・9 L2 / MODIS Terra・Aqua (MOD09GA/MYD09GA) / VIIRS Suomi-NPP・NOAA-20・NOAA-21 (L2 swath VNP09/VJ109/VJ209。旧 VNP09GA ほかも選択可) / GCOM-C SGLI (L2 LAND RSRF)
- **AOI クリッピング**: GeoJSON ポリゴンで任意の領域に切り抜き
- **自動雲マスク**: [omnicloudmask](https://github.com/DPIRD-DMA/OmniCloudMask) による雲・影マスク（Sentinel-2/Landsat）
- **雪マスク**: NDSI ベースの雪マスク（オプション、Sentinel-2/Landsat）
- **同日コンポジット**: 同日の複数シーンを最小値合成で1枚に統合
- **MODIS/VIIRS Surface Reflectance**: NASA Earthdata から native Sinusoidal グリッドのまま直接ダウンロード（再投影・リサンプリング・雲マスクなし、NASA公式QAを保持）
- **VIIRS L2 swath（既定）**: VIIRS は既定で VNP09 / VJ109 / VJ209 の 6-minute L2 swath を使い、I バンドは fixed 375-m analysis grid、M バンドは fixed 750-m analysis grid（MODIS Land と同じ等積 Sinusoidal CRS 上の独自 375 m / 750 m grid。750 m の 1 cell = 375 m の 2×2 cell）へ、地表距離による nearest-neighbour で配置（nominal 375-m / 750-m VIIRS L2 observations mapped to fixed equal-area Sinusoidal analysis grids。raw swath + geolocation が正本で、raster は解析 product。750 m → 375 m の upsample はせず、375 m は grid 間隔で実効分解能ではない）。overpass ごとの出力と、そこから選んだ日次 best observation を保存。旧 Daily L2G（VNP09GA ほか）は `surface_reflectance.viirs_product: daily_l2g_legacy` で利用可能（[設定リファレンス](docs/configuration.md#viirs-l2-swathvnp09--vj109--vj209-固定-375-m--750-m-grid)）
- **GCOM-C/SGLI RSRF**: JAXA G-Portal（公開鍵認証の SFTP）から Level-2 LAND RSRF（version 3002、日次・descending）を取得。native 250 m（VN01–VN11, SW03）と native 1 km（SW01, SW02, SW04）を別グリッドのまま、解像度ごとの multi-band GeoTIFF（250 m: 12 バンド、1 km: 3 バンド）として保存し、1 km → 250 m の upsample や super-resolution は行わない。SW02 は公式定義上 **TOA reflectance**（他の SW バンドは surface reflectance）
- **熱異常（アクティブファイア）データ**: FIRMS MODIS/VIIRS の熱異常検知を point data（Shapefile）として取得（`activefire: SP`/`NRT`）
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
FROM nvidia/cuda:12.8.1-cudnn-runtime-ubuntu22.04
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
| 12.8 (デフォルト) | `nvidia/cuda:12.8.1-cudnn-runtime-ubuntu22.04` | `cu128` |

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

band: all
num: []

cloudmask: [1, 3]
snowmask: false

activefire: none
```

`config.yaml` には実行ごとに変更する可能性が高い項目だけを置いています。それ以外の詳細設定はコード側のデフォルトで動作するため、通常は編集不要です。設定項目の詳細は [設定リファレンス](docs/configuration.md) を参照してください。

### 3. FIRMS API キーを設定する（熱異常データが必要な場合）

<https://firms.modaps.eosdis.nasa.gov/api/> で無料登録し、`key.env` をプロジェクトルートに作成：

```
FIRMS_API_KEY=your_api_key_here
```

> `key.env` は `.gitignore` で除外されているためコミットされません。

### 4. Earthdata 認証情報を設定する（MODIS/VIIRS Surface Reflectance が必要な場合）

<https://urs.earthdata.nasa.gov/> で無料登録し、`key.env` に追記（または環境変数 `EARTHDATA_USERNAME`/`EARTHDATA_PASSWORD`、もしくは netrc ファイルでも可）：

```
EARTHDATA_USERNAME=your_username
EARTHDATA_PASSWORD=your_password
```

詳細は [設定リファレンス](docs/configuration.md#modisviirs-surface-reflectancenasa-earthdata) を参照してください。

### 4b. G-Portal 認証情報を設定する（GCOM-C/SGLI が必要な場合）

[新 G-Portal](https://www.gportal.jaxa.jp/) でユーザー登録し、Web 上で SFTP 用の秘密鍵を発行してリポジトリ外に保存します（SFTP は公開鍵認証のみ）。`key.env` に追記：

```
GPORTAL_USERNAME=your_gportal_account
GPORTAL_PRIVATE_KEY_HOST_PATH=C:/Users/you/.ssh/gportal_privatekey.key
```

秘密鍵は docker compose の `gcomc` サービスでコンテナ内 `/run/secrets/gportal_privatekey.key` に read-only でマウントされ、リポジトリにはコピーされません。`satellite` に `gcomc` を加えて実行します：

```bash
docker compose --env-file key.env --profile gcomc run --rm gcomc python3 run.py --config config/config.yaml
```

詳細は [設定リファレンス](docs/configuration.md#gcom-csgli-l2-rsrfjaxa-g-portal) を参照してください。

### 5. 実行する

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
│   ├── surface_reflectance/
│   │   ├── terra/             # MOD09GA (Terra)
│   │   │   ├── 500m/          # sur_refl_b01-b07 (float32, native Sinusoidal)
│   │   │   └── qa/            # QC_500m, state_1km (raw, unscaled)
│   │   └── aqua/              # MYD09GA (Aqua, Terraとは別ファイル)
│   │       ├── 500m/
│   │       └── qa/
│   └── activefire/       # MODIS 熱異常 Shapefile（point/event data）
└── viirs/
    ├── surface_reflectance/
    │   └── snpp/               # noaa20/, noaa21/ も同じ構成
    │       ├── l2_swath/       # 既定: VNP09 (L2 swath) を固定 Sinusoidal 375/750 m grid へ配置
    │       │   ├── overpass/   # overpass ごと: 375m/(I1-I3) 750m/(M bands) qa/ geometry/ provenance/
    │       │   ├── daily/      # 日次 best observation（overpass 出力からの派生物）
    │       │   └── summary/    # 日ごとの overpass・AOI 状態（JSON）
    │       ├── 500m/           # 旧方式 (viirs_product: daily_l2g_legacy): VNP09GA I1-I3 (~463m)
    │       ├── 1km/            # 旧方式: M1-M5,M7,M8,M10,M11 (~927m)
    │       └── qa/             # 旧方式: QF1-QF7, land_water_mask (raw, unscaled)
    └── activefire/       # VIIRS 熱異常 Shapefile（point/event data）
└── gcomc/
    └── rsrf/
        ├── 250m/              # 1 ファイル 12 バンド: VN01-VN11, SW03（native ~232m, float32 反射率）+ sidecar JSON
        ├── 1km/               # 1 ファイル 3 バンド: SW01, SW02(TOA), SW04（native ~927m, 250mへ upsample しない）
        ├── qa/                # QA_flag, Land_water_flag, Obs_time（raw, unscaled）
        └── summary/           # 日付ごとのプロダクト状態・AOI 観測状態（JSON）
```

> `img/` はシーン単位の生データを保存します。それ以外（`masked` / `snowmasked` / `cloudmask`）は同日コンポジット後の結果です。
> `metadata.enabled: true` にすると、撮影メタデータ GeoJSON が `img/` 配下に保存されます。
> MODIS/VIIRS の `surface_reflectance/` は NASA Earthdata から直接取得したもので、Sentinel-2/Landsat とは独立した処理系です。設定は [設定リファレンス](docs/configuration.md#modisviirs-surface-reflectancenasa-earthdata) を参照してください。
> `activefire/` の主出力は元の point/event data（Shapefile）です。ピクセルラスタ（`activefire_tif/`）はデフォルトでは生成されません（`firms.pixel_tif: true` で再度有効化できます。詳細は [設定リファレンス](docs/configuration.md#firms-熱異常データの詳細設定api-キー) を参照してください）。

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
- **GCOM-C/SGLI の観測頻度**: CSW に毎日 record があっても、AOI がその日のスワス外で全画素 no data のことがあります（阿蘇では概ね2日観測・2日欠測）。この日は `AOI_NO_DATA` として GeoTIFF を作らず、summary JSON にだけ記録します。日付は毎日走査します。
- **GCOM-C/SGLI の雲**: 雲画素にも反射率値が入っています。downloader は雲を除去しません（QA_flag を使った雲除去は後段の役割です）。実データ監査の記録は [docs/gcomc_rsrf_smoke_test.md](docs/gcomc_rsrf_smoke_test.md) を参照してください。
- **モデルキャッシュ**: 初回実行時に omnicloudmask がモデルをダウンロードします。Docker では名前付きボリュームにキャッシュされるため、2回目以降は再ダウンロード不要です。

## ライセンス

MIT License
