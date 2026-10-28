# satellite-image-downloader

自分の対象地域（GeoJSON）と期間を指定するだけで、衛星画像を自動でダウンロードし、切り抜き・雲マスクなどの前処理までまとめて行うツールです。

| 取得できるデータ | `satellite:` の指定 | 取得元 | 必要な登録 |
|---|---|---|---|
| Sentinel-2 L2A | `sentinel2` | [Microsoft Planetary Computer](https://planetarycomputer.microsoft.com/) | 不要 |
| Landsat 8・9 L2 | `landsat89` | Microsoft Planetary Computer | 不要 |
| MODIS 地表面反射率（Terra / Aqua） | `modis` | [NASA Earthdata](https://urs.earthdata.nasa.gov/) | Earthdata |
| VIIRS 地表面反射率（Suomi-NPP / NOAA-20 / NOAA-21） | `viirs` | NASA Earthdata | Earthdata |
| GCOM-C / SGLI 地表面反射率 | `gcomc` | [JAXA G-Portal](https://www.gportal.jaxa.jp/) | G-Portal |
| 熱異常（Active Fire） | `activefire:` で指定 | [NASA FIRMS](https://firms.modaps.eosdis.nasa.gov/) | FIRMS API キー |
| 焼失域（MCD64A1 / VNP64A1） | `burned_area:` で指定 | NASA Earthdata | Earthdata |

## 目次

1. [インストール](#1-インストール)
2. [認証情報を設定する](#2-認証情報を設定する)
3. [対象地域を用意する](#3-対象地域を用意する)
4. [設定ファイルを書く](#4-設定ファイルを書く)
5. [実行する](#5-実行する)
6. [出力](#6-出力)
7. [応用：複数の地域・日付をまとめて取得する](#7-応用複数の地域日付をまとめて取得する)
8. [補足](#補足)

---

## 1. インストール

Docker の利用を推奨します（Python・GDAL・GPU 関連を手動で入れる必要がありません）。

```bash
git clone https://github.com/rime-remotesensing/satellite_image_downloader.git
cd satellite_image_downloader
docker compose build downloader
```

- **GPU を使う場合**：`nvidia-smi` でホストの CUDA バージョンを確認し、`env/Dockerfile` を合わせてからビルドします。手順は [Docker ガイド](docs/docker.md#cuda-バージョンの合わせ方gpu-使用時は必須) にあります。CPU だけなら変更は不要です。
- **Docker を使わない場合**：Python 3.10 以上で、GDAL を先に入れてから依存パッケージを入れます。

  ```bash
  sudo apt-get install gdal-bin libgdal-dev   # macOS は brew install gdal、Windows は conda install gdal
  pip install -r env/requirements.txt
  ```

## 2. 認証情報を設定する

Sentinel-2 と Landsat だけなら、この手順は不要です。

それ以外のデータを使う場合は、プロジェクトのルートに `key.env` を作り、使うものだけ書きます（`key.env` は Git の管理外なのでコミットされません）。

```
# MODIS・VIIRS・焼失域：https://urs.earthdata.nasa.gov/ で無料登録
EARTHDATA_USERNAME=your_username
EARTHDATA_PASSWORD=your_password

# 熱異常：https://firms.modaps.eosdis.nasa.gov/api/ で無料登録
FIRMS_API_KEY=your_api_key

# GCOM-C/SGLI：G-Portal で登録し、Web 上で SFTP 用の秘密鍵を発行（秘密鍵はリポジトリの外に保存）
GPORTAL_USERNAME=your_account
GPORTAL_PRIVATE_KEY_HOST_PATH=C:/Users/you/.ssh/gportal_privatekey.key
```

## 3. 対象地域を用意する

対象地域を Polygon または MultiPolygon の GeoJSON で書き、`config/` に保存します（例：`config/area.geojson`。サンプルを同梱しています）。
[geojson.io](https://geojson.io/) を使うと、地図上で範囲を描いてそのまま保存できます。

```json
{
  "type": "FeatureCollection",
  "features": [{
    "type": "Feature",
    "properties": {},
    "geometry": {
      "type": "Polygon",
      "coordinates": [[[139.5, 35.5], [140.0, 35.5], [140.0, 36.0], [139.5, 36.0], [139.5, 35.5]]]
    }
  }]
}
```

## 4. 設定ファイルを書く

`config/config.yaml` を編集します。

```yaml
geojson: ./config/area.geojson   # 3 で作った対象地域
startday: "20240101"             # 期間（YYYYMMDD）
endday:   "20240131"

satellite:                       # 取得する衛星（複数可）
  - sentinel2
  - landsat89

band: all                        # all = 全バンド / at = num で指定したバンドだけ
num: []                          # 例：[2, 3, 4, 8]

cloudmask: [1, 3]                # マスクする雲の種類：1 = 厚い雲、2 = 薄い雲、3 = 影
snowmask: false                  # 雪もマスクするか

activefire: none                 # 熱異常：SP（確定値）/ NRT（速報値）/ none
buffer_km: 0                     # 対象地域を上下左右に広げる距離（km）
burned_area: []                  # 焼失域：[MCD64A1] / [VNP64A1]
```

よく使う指定：

| やりたいこと | 書き方 |
|---|---|
| 複数の期間を順番に取得する | `startday: [20230306, 20230410]` と `endday: [20230306, 20230410]`（同じ要素数） |
| MODIS・VIIRS の衛星を絞る | `modis-terra` / `modis-aqua` / `viirs-snpp` / `viirs-noaa20` / `viirs-noaa21`（無指定なら MODIS は両方、VIIRS は Suomi-NPP） |
| 対象地域より少し広く取る | `buffer_km: 10` |

雲・雪マスクとバンドの指定は Sentinel-2 / Landsat にだけ効きます。
すべての項目は [設定リファレンス](docs/configuration.md) にあります。

## 5. 実行する

```bash
# Docker（認証が必要なデータを含む場合は --env-file key.env を付ける）
docker compose --env-file key.env run --rm downloader python3 run.py --config config/config.yaml

# GCOM-C/SGLI を含む場合は gcomc サービスで実行（秘密鍵が読み取り専用でマウントされる）
docker compose --env-file key.env --profile gcomc run --rm gcomc python3 run.py --config config/config.yaml

# Docker を使わない場合
python run.py --config config/config.yaml
```

出力先をプロジェクトの外にしたい場合は、[Docker ガイド](docs/docker.md#プロジェクト外のディレクトリに出力する) を参照してください。

## 6. 出力

```
output/
├── sentinel2/ , landsat89/
│   ├── img/              # シーンごとの元データ（マルチバンド TIFF）
│   ├── masked/           # 雲マスク済み（同じ日の複数シーンを 1 枚に合成）
│   ├── snowmasked/       # 雲 + 雪マスク済み
│   └── cloudmask/        # 雲・雪マスクのレイヤー
├── modis/
│   ├── surface_reflectance/{terra,aqua}/
│   │   ├── 500m/         # 反射率 バンド 1–7
│   │   └── qa/           # 品質フラグ（QC_500m, state_1km）
│   ├── burned_area/      # 期間内に焼けた画素（1 = 焼失、0 = なし、255 = nodata）
│   └── activefire/       # 熱異常の検知点（Shapefile）
├── viirs/
│   ├── surface_reflectance/<snpp|noaa20|noaa21>/l2_swath/
│   │   ├── daily/        # 日ごとの最良観測（375 m / 750 m、QA・観測角度つき）
│   │   └── summary/      # 日ごとの観測状況（JSON）
│   ├── burned_area/
│   └── activefire/
└── gcomc/rsrf/
    ├── 250m/             # 12 バンド（VN01–VN11, SW03）
    ├── 1km/              # 3 バンド（SW01, SW02, SW04）
    ├── qa/
    └── summary/
```

データの性質：

- **Sentinel-2 / Landsat**：対象地域で切り抜き、雲・影は [omnicloudmask](https://github.com/DPIRD-DMA/OmniCloudMask)、雪は NDSI でマスクします。GPU があればマスク処理が速くなります。
- **MODIS / VIIRS**：NASA の元の格子（Sinusoidal 図法）のまま保存します。再投影・リサンプリング・雲マスクはしません（雲は QA で判定できます）。
- **GCOM-C / SGLI**：250 m と 1 km のバンドを別々のファイルで保存します（1 km を 250 m に拡大はしません）。SW02 だけは大気上端（TOA）の反射率です。
- **焼失域**：月次製品から、指定期間内に焼けた画素だけを 1 枚の 2 値画像にまとめます。
- **熱異常**：FIRMS の検知点を Shapefile で保存します。

## 7. 応用：複数の地域をまとめて取得する

`run.py` 冒頭の変数を自分の地域・期間に書き換えると、複数の地域を同じ期間で一括処理できます。

```python
BATCH_MODE_REGIONS = [                 # (地域名, GeoJSON) の一覧
    ("region_a", "config/region_a.geojson"),
    ("region_b", "config/region_b.geojson"),
]
BATCH_STARTDAY = "20240201"            # 期間（全地域共通）
BATCH_ENDDAY = "20240410"
BATCH_SATELLITES = ["modis", "viirs"]  # 取得する衛星
BATCH_ACTIVEFIRE = "SP"                # 熱異常：SP / NRT / none
# BASE_PATH：出力先（環境変数 SATDL_BASE_PATH でも指定可）。出力は <BASE_PATH>/<地域名>/
```

```bash
docker compose --env-file key.env run --rm downloader python3 run.py --batch
```

Python から直接呼ぶこともできます（`sdate` / `edate` にリストを渡すと複数の日付を処理）。

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

---

## 補足

- **SGLI の欠測日**：観測幅の外で対象地域が写らない日があります（日本付近ではおよそ 2 日おき）。その日は画像を作らず、summary JSON に `AOI_NO_DATA` と記録します。
- **FIRMS の取得期間**：1 回のリクエストは最大 5 日間ですが、長い期間は自動で分割します。
- **初回実行**：omnicloudmask のモデルをダウンロードします（Docker では保存されるので 2 回目以降は不要）。
- **テスト**：`docker compose run --rm -T downloader sh -c "pip install -q pytest && python3 -m pytest -q tests"`
- **ドキュメント**：[設定リファレンス](docs/configuration.md) / [Docker ガイド](docs/docker.md)
- **研究用データの再現**（開発者向け）：[docs/research_reproduction.md](docs/research_reproduction.md)

## ライセンス

MIT License
