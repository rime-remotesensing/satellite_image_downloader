# satellite_image_downloader

## 概要

このリポジトリは、設定ファイル駆動で動作する衛星画像処理パイプラインです。主な処理フローは次の通りです。

1. Microsoft Planetary Computer STAC から Sentinel-2 L2A / Landsat 8・9 L2 を検索
2. GeoJSON の AOI で切り出し、バンド別 TIFF を保存
3. インストール済みの omnicloudmask パッケージで雲マスクを生成
4. 雲マスク適用と同日最小値コンポジットを作成
5. FIRMS の active fire データを取得し、Shapefile で保存

## プロジェクト構成

- run.py: パイプライン実行エントリポイント（設定ファイル駆動）
- config/config.yaml: 実行設定
- config/area.geojson: AOI サンプル
- src/pipeline.py: ダウンロード・前処理・出力の統合ロジック

## 入力設定

- geojson: Polygon / MultiPolygon の GeoJSON パス
- startday, endday: 対象期間（YYYYMMDD）
- satellite: sentinel2, landsat89, modis, viirs から1つ以上
- band: all または at
- num: band が at のときに取得するバンド番号配列
- cloudmask: マスク対象クラス（1, 2, 3）

## 出力

- Sentinel-2 生データ: output/sentinel2/raw/S2C_yyyymmdd_Bxx.tif
- Sentinel-2 前処理済み: output/sentinel2/img/S2C_yyyymmdd_Bxx.tif
- Landsat 8/9 生データ: output/landsat89/raw/L8C_yyyymmdd_Bxx.tif, L9C_yyyymmdd_Bxx.tif
- Landsat 8/9 前処理済み: output/landsat89/img/L8C_yyyymmdd_Bxx.tif, L9C_yyyymmdd_Bxx.tif
- 雲マスク: 各 raw ディレクトリに *_omnicloudmask.tif
- Active fire（Shapefile）:
  - output/modis/activefire/ACFR_yyyymmdd_tttt.shp
  - output/viirs/activefire/ACFR_yyyymmdd_tttt.shp

## ローカル実行

```bash
python run.py --config config/config.yaml
```

## Docker Compose 実行

```bash
docker compose run --rm downloader
```

実行前に config/config.yaml を用途に合わせて編集してください。

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