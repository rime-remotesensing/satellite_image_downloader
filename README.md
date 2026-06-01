# satellite_image_downloader

Microsoft Planetary Computer と NASA FIRMS から、指定した範囲・期間の衛星画像と active fire データを取得する Python パイプラインです。

主な処理は次の通りです。

1. GeoJSON で指定した AOI を読み込む
2. Microsoft Planetary Computer STAC から Sentinel-2 L2A または Landsat 8/9 L2 を検索する
3. AOI に合わせて GeoTIFF を切り出し、複数バンドのスタック画像として保存する
4. omnicloudmask で雲マスクを作成し、雲・影を除いた画像を保存する
5. 任意で NDSI による雪マスクを作成し、雲＋雪を除いた画像を保存する
6. 任意で NASA FIRMS から MODIS / VIIRS active fire を取得し、Shapefile と GeoTIFF で保存する

## できること

- Sentinel-2 L2A のダウンロード
- Landsat 8 / 9 Collection 2 Level-2 のダウンロード
- AOI GeoJSON による切り出し
- バンド指定、または全バンド取得
- 雲マスク、雲除去済み画像の作成
- 雪マスク、雲＋雪除去済み画像の作成
- 撮影メタデータ GeoJSON の保存
- FIRMS active fire の Shapefile / raster GeoTIFF 出力
- 設定ファイル実行、バッチ実行、Python API からの実行
- Docker Compose 実行

## ディレクトリ構成

```text
satellite_image_downloader/
├─ run.py                  # 実行入口
├─ src/pipeline.py          # パイプライン本体
├─ config/config.yaml       # 実行設定
├─ env/requirements.txt     # Python 依存パッケージ
├─ env/Dockerfile           # Docker イメージ定義
├─ docker-compose.yml       # Docker Compose 設定
├─ scripts/                 # 補助スクリプト
└─ data/                    # Docker 実行時などのデータ置き場
```

## まず動かす

### 1. AOI GeoJSON を用意する

`config/config.yaml` の `geojson` に、対象範囲の GeoJSON を指定します。

```yaml
geojson: ./config/no5.geojson
```

GeoJSON は `Polygon` または `MultiPolygon` を想定しています。

### 2. 期間を指定する

1期間だけ実行する場合は、`YYYYMMDD` 形式で指定します。

```yaml
startday: [20260425]
endday:   [20260425]
```

複数日をまとめて処理したい場合は、`startday` と `endday` に同じ数の要素を入れます。

```yaml
startday: [20240116, 20240126, 20240220]
endday:   [20240116, 20240126, 20240220]
```

複数期間を指定した場合、衛星画像は期間ごとに処理されます。FIRMS active fire は、最小 `startday` から最大 `endday` までの全期間をまとめて取得します。

### 3. 対象衛星を指定する

```yaml
satellite:
  - sentinel2
```

指定できる値は次の通りです。

| 値 | 内容 |
| --- | --- |
| `sentinel2` | Sentinel-2 Level-2A |
| `landsat89` | Landsat 8 / 9 Collection 2 Level-2 |

active fire は `satellite` ではなく、`firms.activefire_satellite` で指定します。

### 4. 実行する

```bash
python run.py --config config/config.yaml
```

実行が終わると、処理結果の概要が JSON で表示されます。

## 設定ファイルの主な項目

`config/config.yaml` を編集して、入力、出力、マスク処理、FIRMS 取得条件を変更します。

| 項目 | 例 | 説明 |
| --- | --- | --- |
| `geojson` | `./config/no5.geojson` | AOI GeoJSON のパス |
| `startday` | `[20260425]` | 開始日。`YYYYMMDD` のリスト |
| `endday` | `[20260425]` | 終了日。`startday` と同じ要素数にする |
| `satellite` | `[sentinel2]` | 取得する衛星画像 |
| `output` | `./output` | 出力先ルート |
| `band` | `all` | `all` または `at` |
| `num` | `[1, 2, 3]` | `band: at` のときに取得するバンド番号 |
| `cloudmask` | `[1, 3]` | マスクする omnicloudmask クラス |
| `max_cloud_cover` | `80` | STAC 検索時の雲量上限。不要なら `null` |
| `file_exists` | `skip` | 既存ファイルがある場合の動作。`skip` または `overwrite` |
| `metadata.enabled` | `true` | 撮影メタデータ GeoJSON を保存するか |

### バンド指定

全バンドを取得する場合:

```yaml
band: all
num: []
```

特定バンドだけを出力したい場合:

```yaml
band: at
num: [2, 3, 4, 8]
```

注意: 雲マスクや雪マスクに必要なバンドは、`num` に含まれていなくても内部処理のために自動でダウンロードされます。ただし、最終的に `img/` に個別出力されるのは `num` で指定したバンドです。

### 雲マスク

```yaml
cloudmask: [1, 3]
```

omnicloudmask のクラスのうち、どれを無効値にするかを指定します。

| クラス | 意味 |
| --- | --- |
| `1` | thick cloud |
| `2` | thin cloud |
| `3` | shadow |

通常は `[1, 3]` または `[1, 2, 3]` を使います。

### 雪マスク

```yaml
snowmask:
  enabled: true
  ndsi_threshold: 0.4
  red_threshold: 0.2
```

`enabled: true` の場合、NDSI による雪マスクを作り、`snowmasked/` に雲＋雪マスク済み画像を出力します。

### GEE 互換設定

Sentinel-2 で Google Earth Engine のエクスポートに近い格子に合わせたい場合に使います。

```yaml
gee_compatible:
  enabled: true
  output_crs: EPSG:32652
  aoi_as_bbox: true
  snap_grid: true
```

| 項目 | 説明 |
| --- | --- |
| `enabled` | GEE 互換設定を使うか |
| `output_crs` | 出力 CRS |
| `aoi_as_bbox` | AOI の外接矩形で切り出すか |
| `snap_grid` | ピクセルサイズに合わせてグリッドを丸めるか |

## FIRMS active fire を使う場合

FIRMS active fire を取得するには、NASA FIRMS の API キーが必要です。

API キーは次のページから取得します。

https://firms.modaps.eosdis.nasa.gov/api/

取得したキーは、リポジトリ直下などに `key.env` として保存します。

```bash
FIRMS_API_KEY=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
```

`config/config.yaml` では、次のように指定します。

```yaml
firms:
  activefire_satellite:
    - viirs
    - modis
  key_env_path: ./key.env
  pixel_tif: true
  period_summary: true
  product_map:
    modis:
      - MODIS_NRT
    viirs:
      - VIIRS_SNPP_NRT
      - VIIRS_NOAA20_NRT
      - VIIRS_NOAA21_NRT
```

| 項目 | 説明 |
| --- | --- |
| `activefire_satellite` | `modis`、`viirs` のどちらを取得するか |
| `key_env_path` | API キーを書いた `.env` ファイル |
| `api_key` | 直接 API キーを書く場合の項目。通常は空でよい |
| `clip_to_aoi` | 取得後に AOI ポリゴンで絞り込むか |
| `bbox_buffer_m` | FIRMS 取得用 BBOX を AOI から何 m 広げるか |
| `pixel_tif` | active fire を raster GeoTIFF でも出力するか |
| `pixel_resolution` | active fire GeoTIFF の解像度 m |
| `pixel_expand_to_detections` | 検知点がグリッド外に出た場合に出力範囲を広げるか |
| `days` | FIRMS API の1リクエストあたり日数。1から5 |
| `period_summary` | 期間全体をまとめた Shapefile / GeoTIFF を出すか |
| `product_map.modis` | MODIS の FIRMS product |
| `product_map.viirs` | VIIRS の FIRMS product |

API キーが見つからない場合、active fire の取得はスキップされます。衛星画像の処理は継続します。

## 出力

`output` に指定したディレクトリの下に、衛星またはデータ種別ごとのフォルダが作られます。

通常の設定ファイル実行では、次のような構成です。

```text
output/
├─ sentinel2/
│  ├─ img/
│  ├─ masked/
│  ├─ snowmasked/
│  └─ cloudmask/
├─ landsat89/
│  ├─ img/
│  ├─ masked/
│  ├─ snowmasked/
│  └─ cloudmask/
├─ modis/
│  ├─ activefire/
│  └─ activefire_tif/
└─ viirs/
   ├─ activefire/
   └─ activefire_tif/
```

### 衛星画像

| フォルダ | 内容 |
| --- | --- |
| `img/` | AOI で切り出したスタック GeoTIFF。撮影シーン単位 |
| `masked/` | 雲・影をマスクした日別コンポジット GeoTIFF |
| `snowmasked/` | 雲・影・雪をマスクした日別コンポジット GeoTIFF |
| `cloudmask/` | 日別コンポジットの雲マスク、雪マスク |

主なファイル名の例:

```text
output/sentinel2/img/S2C_20260425_<scene_id>.tif
output/sentinel2/masked/S2C_20260425_masked.tif
output/sentinel2/snowmasked/S2C_20260425_snowmasked.tif
output/sentinel2/cloudmask/S2C_20260425_cloudmask.tif
output/sentinel2/cloudmask/S2C_20260425_snowmask.tif
```

`metadata.enabled: true` の場合、`img/` の下に撮影メタデータ GeoJSON も保存されます。

```text
output/sentinel2/img/20260425-20260425.geojson
```

メタデータには主に次の情報が入ります。

- `Acquisition_Date`
- `Image_ID`
- `Solar_Azimuth_Angle`
- `Solar_Zenith_Angle`

### Active Fire

```text
output/modis/activefire/ACFR_20260425_1234.shp
output/viirs/activefire/ACFR_20260425_1234.shp
output/modis/activefire_tif/ACFR_20260425_1234.tif
output/viirs/activefire_tif/ACFR_20260425_1234.tif
```

`period_summary: true` の場合、期間全体をまとめたファイルも出力されます。

```text
output/viirs/activefire/ACFR_20260401_20260425_1234.shp
output/viirs/activefire_tif/ACFR_20260401_20260425_1234.tif
```

末尾の `1234` は実行時刻 UTC の `HHMM` です。

## 実行方法

### 設定ファイルで実行

最も基本的な実行方法です。

```bash
python run.py --config config/config.yaml
```

### バッチ実行

`run.py` に定義されている `region01` から `region10` と、ハードコード済みの日付リストを使って Sentinel-2 を一括取得します。

```bash
python run.py --batch
```

`--config` を指定せずに実行した場合も、現在の実装ではバッチ実行になります。

```bash
python run.py
```

バッチ実行で使われる AOI と日付は、`run.py` の次の変数で定義されています。

- `BATCH_MODE_REGIONS`
- `REGION_DOWNLOAD_DATES`

出力先の基準パスは `SATDL_BASE_PATH` または `SATDL_HOST_DATA_PATH` で変更できます。指定しない場合、Docker 内では `/host_data/Aso/Sentinel-2` が使われます。

PowerShell の例:

```powershell
$env:SATDL_BASE_PATH = "D:/sugimoto/Aso/Sentinel-2"
python run.py --batch
```

## Docker Compose で実行

ローカル Python 環境を作らずに実行したい場合は Docker Compose を使います。

### 1. イメージをビルド

```bash
docker compose build downloader
```

### 2. 設定ファイルで実行

```bash
docker compose run --rm downloader python3 run.py --config config/config.yaml
```

または、`docker-compose.yml` の `command` を使って実行します。

```bash
docker compose run --rm downloader
```

### 3. バッチ実行

```bash
docker compose run --rm downloader python3 run.py --batch
```

### ホスト側のデータフォルダを指定する

Docker コンテナ内では、ホストのフォルダが `/host_data` にマウントされます。ホスト側のフォルダは `SATDL_HOST_DATA_PATH` で指定できます。

PowerShell の例:

```powershell
$env:SATDL_HOST_DATA_PATH = "D:/sugimoto"
docker compose run --rm -e SATDL_BASE_PATH=/host_data/Aso/Sentinel-2 downloader python3 run.py --batch
```

この例では、コンテナ内の `/host_data/Aso/Sentinel-2` が、ホスト側の `D:/sugimoto/Aso/Sentinel-2` に対応します。

### Docker のキャッシュ

初回実行時は、omnicloudmask や PyTorch 関連のモデル取得に時間がかかることがあります。このリポジトリでは Docker volume にキャッシュを保存します。

- `satdl_model_cache`: Hugging Face や torch などのキャッシュ
- `satdl_model_data`: omnicloudmask のローカルデータ

キャッシュを消して再取得したい場合:

```bash
docker volume rm satellite_image_downloader_satdl_model_cache
docker volume rm satellite_image_downloader_satdl_model_data
```

### GPU の確認

Docker 内で GPU が見えているか確認する例です。

```bash
docker compose run --rm downloader python3 -c "import torch; print('torch', torch.__version__); print('cuda_available', torch.cuda.is_available()); print('device', torch.cuda.get_device_name(0) if torch.cuda.is_available() else 'none')"
```

## Python から使う

`src.pipeline.satellite_image_downloader` を直接呼び出すこともできます。

```python
from src.pipeline import satellite_image_downloader

satellite_image_downloader(
    satellite_type=["sentinel2", "landsat89"],
    geojson_path="config/area.geojson",
    sdate="20240101",
    edate="20240131",
    output_path="output",
    config_path="config/config.yaml",
)
```

複数日をまとめて処理する例:

```python
from src.pipeline import satellite_image_downloader

satellite_image_downloader(
    satellite_type=["sentinel2"],
    geojson_path="config/area.geojson",
    sdate=[20230306, 20230311, 20230410],
    edate=[20230306, 20230311, 20230410],
    output_path="output",
    config_path="config/config.yaml",
)
```

`config_path` を渡すと、`config/config.yaml` の詳細設定を読み込んだうえで、`satellite_type`、`geojson_path`、`sdate`、`edate`、`output_path` が上書きされます。

## パス指定の注意

相対パスは、まずプロジェクトルート（`run.py` があるディレクトリ）を基準に解決されます。ファイルが存在しない場合は、設定ファイルがあるディレクトリも候補になります。

```yaml
geojson: ./config/no5.geojson
output: ./output
```

Docker 実行では、コンテナから見えるパスを指定してください。ホスト側の任意フォルダを使いたい場合は、`SATDL_HOST_DATA_PATH` で `/host_data` にマウントしてから、設定ファイルでは `/host_data/...` のように指定します。

例:

```yaml
geojson: /host_data/config/no5.geojson
output: /host_data/output
```

## よくあるトラブル

### active fire が出力されない

- `key.env` に `FIRMS_API_KEY=...` が書かれているか確認してください。
- `firms.key_env_path` が正しいか確認してください。
- `firms.activefire_satellite` と `firms.product_map` の両方が設定されているか確認してください。
- API キーがない場合、active fire はスキップされます。

### 出力が見つからない

- `config/config.yaml` の `output` を確認してください。
- Docker 実行時は、ホスト側パスではなくコンテナ側パスで出力している可能性があります。
- バッチ実行時は `SATDL_BASE_PATH` または `SATDL_HOST_DATA_PATH` の設定を確認してください。

### 既存ファイルがあるのに再処理される、または処理されない

`file_exists` を確認してください。

```yaml
file_exists: skip       # 既存の img があればスキップ
file_exists: overwrite  # 再作成
```

### CUDA エラーが出る

`omnicloudmask.device` を `cpu` に変更すると、GPU なしでも実行できます。ただし処理は遅くなります。

```yaml
omnicloudmask:
  device: cpu
```

## 実装上の補足

- Sentinel-2 は `s2:processing_baseline >= 4.0` のシーンで DN offset を考慮します。
- Sentinel-2 の標準解像度は 10 m、Landsat 8/9 は 30 m として処理します。
- 同じ日に複数シーンがある場合、雲マスク後の画像は日別にコンポジットされます。
- active fire の CRS は、可能な場合 Sentinel-2 出力 CRS に合わせます。推定できない場合は `EPSG:4326` を使います。
