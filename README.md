# satellite_image_downloader

## 概要

このリポジトリは、設定ファイル駆動で動作する衛星画像処理パイプラインです。主な処理フローは次の通りです。

1. Microsoft Planetary Computer STAC から Sentinel-2 L2A / Landsat 8・9 L2 を検索
2. GeoJSON の AOI で切り出し、スタック済み TIFF（マルチバンド）を保存
3. インストール済みの omnicloudmask パッケージで雲マスクを生成
4. 雲マスク適用、および設定に応じて雪マスク適用
5. FIRMS の active fire データを取得し、Shapefile で保存

## プロジェクト構成

- run.py: パイプライン実行エントリポイント（設定ファイル駆動）
- config/config.yaml: 実行設定
- config/area.geojson: AOI サンプル
- src/pipeline.py: ダウンロード・前処理・出力の統合ロジック

## 入力設定

- geojson: Polygon / MultiPolygon の GeoJSON パス
- startday, endday: 対象期間（YYYYMMDD）
  - 単日/単期間: 文字列で指定（例: "20230307"）
  - 複数期間ループ: 配列で指定（startday/endday は同じ要素数）
    例: startday=[20230306,20230311,20230410], endday=[20230306,20230311,20230410]
  - active fire（FIRMS）は複数期間指定時、最小startday〜最大enddayの全期間を1回で取得
- satellite: 処理対象衛星（sentinel2, landsat89, modis, viirs）
- band: all または at
- num: band が at のときに取得するバンド番号配列
- cloudmask: マスク対象クラス（1, 2, 3）
- snowmask.enabled: 雪マスク処理を行うかどうか（true/false）
- metadata.enabled: 撮影メタデータGeoJSONを保存するかどうか（true/false）
- firms.key_env_path: FIRMS APIキーを保存した `key.env` のパス
- firms.activefire_satellite: active fire の取得対象（modis, viirs）
- firms.product_map.modis: MODISの製品名（文字列または配列）
- firms.product_map.viirs: VIIRSの製品名（文字列または配列）
- firms.pixel_tif: active fire をピクセルベースTIFでも出力するか（true/false）
- firms.pixel_resolution: active fire TIF の解像度[m]（既定: 10）
- firms.pixel_expand_to_detections: 検知が参照グリッド外にある場合にTIF範囲を自動拡張するか（既定: true）
- firms.days: FIRMS area API の取得日数（1..5）
- firms.clip_to_aoi: FIRMS取得後にAOIポリゴンで最終切り抜きするか（true/false）
- firms.bbox_buffer_m: FIRMS取得時にAOI BBOXを上下左右に広げる距離（m）
- firms.period_summary: active fire を期間全体で1ファイルに総まとめ出力するか（true/false）

補足: `firms.activefire_satellite` を未指定の場合は後方互換のため、`satellite` に含まれる modis/viirs を active fire 対象として使います。
推奨: active fire の対象制御は `firms.activefire_satellite` を使って `satellite` から分離してください。
補足: `product_map` は配列対応です。例として VIIRS を SNPP + NOAA-20 の両方で取得できます。
補足: MODIS の `MODIS_SP` は Terra/Aqua を含む統合系です。VIIRS はセンサ別（例: `VIIRS_SNPP_SP`, `VIIRS_NOAA20_SP`）です。

## FIRMS APIキーの取得と設定

FIRMS APIキー（MAP_KEY）は以下で取得してください。

- https://firms.modaps.eosdis.nasa.gov/api/

取得したキーは `config/config.yaml` へ直接書かず、`key.env` に保存してください。

`key.env` の例:

```bash
FIRMS_API_KEY=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
```

`config/config.yaml` では `firms.key_env_path` を指定します（既定値: `./key.env`）。

## 出力

注記: img 以外（masked / snowmasked / cloudmask）は同日コンポジット後の結果を保存します。
注記: metadata.enabled=true の場合、img 配下に撮影メタデータ GeoJSON を保存します。

- Acquisition_Date
- Image_ID
- Solar_Azimuth_Angle
- Solar_Zenith_Angle

- Sentinel-2 出力:
  - output/sentinel2/img: 生データ（スタック、シーン単位）
  - output/sentinel2/img/<start_yyyymmdd>-<end_yyyymmdd>.geojson: 撮影メタデータ
  - output/sentinel2/masked: 雲マスク適用済み（同日コンポジット）
  - output/sentinel2/snowmasked: 雲+雪マスク適用済み（同日コンポジット、snowmask.enabled=true の場合）
  - output/sentinel2/cloudmask: 雲マスクと雪マスク（同日コンポジット）
- Landsat 8/9 出力:
  - output/landsat89/img: 生データ（スタック、シーン単位）
  - output/landsat89/img/<start_yyyymmdd>-<end_yyyymmdd>.geojson: 撮影メタデータ
  - output/landsat89/masked: 雲マスク適用済み（同日コンポジット）
  - output/landsat89/snowmasked: 雲+雪マスク適用済み（同日コンポジット、snowmask.enabled=true の場合）
  - output/landsat89/cloudmask: 雲マスクと雪マスク（同日コンポジット）
- Active fire（Shapefile）:
  - output/modis/activefire/ACFR_yyyymmdd_tttt.shp
  - output/viirs/activefire/ACFR_yyyymmdd_tttt.shp
  - output/modis/activefire/ACFR_<start_yyyymmdd>_<end_yyyymmdd>_tttt.shp
  - output/viirs/activefire/ACFR_<start_yyyymmdd>_<end_yyyymmdd>_tttt.shp
- Active fire（Pixel TIF, firms.pixel_tif=true の場合）:
  - output/modis/activefire_tif/ACFR_yyyymmdd_tttt.tif
  - output/viirs/activefire_tif/ACFR_yyyymmdd_tttt.tif
  - output/modis/activefire_tif/ACFR_<start_yyyymmdd>_<end_yyyymmdd>_tttt.tif
  - output/viirs/activefire_tif/ACFR_<start_yyyymmdd>_<end_yyyymmdd>_tttt.tif

注記: FIRMS area API は1リクエストの DAY_RANGE が 1..5 です。期間が6日以上の場合は内部で5日以下に分割して取得し、期間全体を集約します。
注記: FIRMSはAPI仕様上BBOXで取得します。`firms.bbox_buffer_m` で広めに取得し、`firms.clip_to_aoi=true` の場合は最後にAOIポリゴンで切り抜きます。
注記: active fire出力のCRSは固定で「Sentinel-2画像のCRSに合わせる」動作です（推定できない場合は EPSG:4326 にフォールバック）。
注記: active fireのTIFは、検知点(lat/lon)とFIRMS属性(scan/track)からフットプリント矩形を作成してラスタ化しています。
注記: `firms.clip_to_aoi=false` かつ BBOX拡張を使う場合でも、`firms.pixel_expand_to_detections=true` ならTIFが空になりにくいよう範囲を自動拡張します。

## ローカル実行

```bash
python run.py --config config/config.yaml
```

## Docker Compose 実行

```bash
docker compose run --rm downloader
```

実行前に config/config.yaml を用途に合わせて編集してください。

補足（モデルダウンロード高速化）:

- 初回実行時のみ、omnicloudmask のモデルを Hugging Face からダウンロードするため時間がかかります。
- このリポジトリの docker-compose は次を named volume で永続化しています。
  - /root/.cache （HF キャッシュなど）
  - /root/.local/share （omnicloudmask の実モデル保存先）
- そのため、2回目以降はモデル再ダウンロードを回避できます。
- キャッシュを消して再取得したい場合は以下を実行します。

```bash
docker volume rm satellite_image_downloader_satdl_model_cache
docker volume rm satellite_image_downloader_satdl_model_data
```

## パス可搬性（重要）

絶対パスはローカル実行ではそのまま利用できます。

- ローカル実行（python run.py）: 絶対パス利用可
- Docker 実行: コンテナから見えるパスのみ利用可

本プロジェクトでは、固定ドライブ文字依存を避けるため、任意ホストパスをバインドできるようにしています。

- ホスト側パス（設定可能）: ${SATDL_HOST_DATA_PATH}
- コンテナ側パス: /host_data

例（Windows PowerShell）:

```bash
$env:SATDL_HOST_DATA_PATH = "D:/sat_data"
docker compose run --rm downloader
```

この場合、config/config.yaml ではコンテナ側パスで指定します。

- geojson: /host_data/config/area.geojson
- output: /host_data/output

注意:

- geojson の場所や output の保存先が作業ディレクトリ外にある場合は、
  必ず config/config.yaml（または関数引数）でそのパスを明示指定してください。
- 省略すると、既定の相対パス（./config/... や ./output）が使われるため、
  意図しない場所を参照・出力してしまう可能性があります。
- config/config.yaml を使う場合、相対パスは基本的にプロジェクトルート
  （run.py があるディレクトリ）基準で解決されます。

データをリポジトリ配下に置く運用なら、./config/area.geojson と ./output のような相対パスが最も簡単で可搬性が高いです。

## Python API

Python から直接呼び出すこともできます。

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

`sdate` と `edate` も配列で渡せます。以下は3回ループ実行されます。

```python
from src.pipeline import satellite_image_downloader

satellite_image_downloader(
  satellite_type=["sentinel2"],
  geojson_path="config/area.geojson",
  sdate=[20230306, 20230311, 20230410],
  edate=[20230306, 20230311, 20230410],
  output_path="output",
)
```

## run.py を編集してループ実行する方法

ご認識の通り、for 文の中で satellite_image_downloader を呼べば、
対象領域だけを変えて同じ条件で一括実行できます。

実装の考え方:

1. run.py で satellite_image_downloader を import する
2. 領域ごとの geojson パスや出力先を配列で定義する
3. for 文で順番に呼び出す

run.py の最小例:

```python
from src.pipeline import satellite_image_downloader

targets = [
  {
    "name": "region01",
    "geojson": "config/region01.geojson",
    "output": "output/region01",
  },
  {
    "name": "region02",
    "geojson": "config/region02.geojson",
    "output": "output/region02",
  },
]

for t in targets:
  print(f"start: {t['name']}")
  satellite_image_downloader(
    satellite_type=["sentinel2", "landsat89"],
    geojson_path=t["geojson"],
    sdate="20240101",
    edate="20240131",
    output_path=t["output"],
  )
  print(f"done: {t['name']}")
```

補足:

- Docker 実行時に作業ディレクトリ外のデータを使う場合は、
  geojson_path と output_path を /host_data/... のような
  コンテナ側パスで指定してください。
- ローカル実行時に作業ディレクトリ外を使う場合は、
  geojson_path と output_path に絶対パスを指定してください。
- run.py をシンプルに保ちたい場合は、同じ内容を別ファイル
  （例: scripts/batch_run.py）として作る運用でも問題ありません。