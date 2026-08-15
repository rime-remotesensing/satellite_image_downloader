# 設定リファレンス

設定はすべて YAML ファイル（デフォルト: `config/config.yaml`）に記述し、以下で実行します：

```bash
python run.py --config config/config.yaml
```

---

## 基本入力

| キー | 型 | 説明 |
|------|----|------|
| `geojson` | string | AOI を定義する GeoJSON ファイルのパス（Polygon または MultiPolygon） |
| `startday` | string \| list | 開始日（`YYYYMMDD` 形式） |
| `endday` | string \| list | 終了日（`YYYYMMDD` 形式） |
| `satellite` | list | 処理対象の衛星。選択肢: `sentinel2`, `landsat89`, `modis`, `viirs` |
| `output` | string | 出力のルートディレクトリ |

### 単一期間の指定

```yaml
startday: "20240101"
endday:   "20240131"
```

### 複数期間の指定（順番にループ実行）

同じ長さの配列で指定します。各ペアが独立して処理されます：

```yaml
startday: [20230306, 20230311, 20230410]
endday:   [20230306, 20230311, 20230410]
```

> **FIRMS（熱異常データ）の注意**: 複数期間を指定した場合、FIRMS は `min(startday)` から `max(endday)` の全期間を1回で取得します。

---

## バンド選択

| キー | 型 | デフォルト | 説明 |
|------|----|-----------|------|
| `band` | string | `all` | `all` = 全バンド、`at` = `num` で指定したバンドのみ |
| `num` | list | `[]` | `band: at` のときに取得するバンド番号（例: `[2, 3, 4, 8]`） |

> omnicloudmask に必要なバンドは `num` の指定に関わらず常にダウンロードされます。

---

## 雲マスク

| キー | 型 | デフォルト | 説明 |
|------|----|-----------|------|
| `cloudmask` | list | `[1, 3]` | マスク対象の雲クラス。`1` = 厚雲、`2` = 薄雲、`3` = 影 |
| `max_cloud_cover` | int | `80` | この雲被覆率（%）を超えるシーンはスキップ（STAC メタデータによるフィルタ） |

### omnicloudmask の推論設定

```yaml
omnicloudmask:
  batch_size: 1
  patch_size: 1000
  patch_overlap: 300
  device: cuda     # "cuda" または "cpu"
```

---

## 雪マスク

| キー | 型 | デフォルト | 説明 |
|------|----|-----------|------|
| `snowmask.enabled` | bool | `false` | 雲マスクの後に雪マスク処理を行うか |
| `snowmask.ndsi_threshold` | float | `0.4` | 雪と判定する NDSI 閾値 |
| `snowmask.red_threshold` | float | `0.2` | 雪と判定する赤バンド反射率閾値 |

```yaml
snowmask:
  enabled: true
  ndsi_threshold: 0.4
  red_threshold: 0.2
```

---

## 撮影メタデータ

| キー | 型 | デフォルト | 説明 |
|------|----|-----------|------|
| `metadata.enabled` | bool | `false` | 撮影メタデータを `img/` 配下に GeoJSON で保存するか |

保存されるフィールド: `Acquisition_Date`, `Image_ID`, `Solar_Azimuth_Angle`, `Solar_Zenith_Angle`

---

## GEE 互換オプション（Sentinel-2）

Google Earth Engine エクスポートとグリッドを合わせるためのオプションです。

```yaml
gee_compatible:
  enabled: true
  output_crs: EPSG:32652   # 出力 CRS（AOI に合わせた UTM ゾーン）
  aoi_as_bbox: true        # AOI のバウンディングボックスを使う（ee.Geometry.Rectangle 相当）
  snap_grid: true          # ピクセルアラインメントにスナップ
```

---

## ファイル処理オプション

| キー | 型 | デフォルト | 説明 |
|------|----|-----------|------|
| `file_exists` | string | `skip` | 出力ファイルが既にある場合の動作。`skip` = スキップ、`overwrite` = 上書き |
| `img_only` | bool | `false` | `img/` のみ（再）生成し、雲・雪マスクはスキップ |

CLI で `img_only` を指定する場合：

```bash
python run.py --config config/config.yaml --img-only
```

---

## MODIS/VIIRS Surface Reflectance 設定（NASA Earthdata）

`satellite` に `modis` / `viirs` を含めると、NASA Earthdata から daily Surface Reflectance を
ネイティブの Sinusoidal グリッドのまま（再投影・リサンプリングなし）ローカルへ直接ダウンロードします。
Google Earth Engine は使用しません。FIRMS の熱異常取得（`firms.activefire_satellite`）とは完全に独立した処理系です。

```yaml
satellite:
  - modis
  - viirs

surface_reflectance:
  products:
    modis:
      terra: MOD09GA   # Terra
      aqua: MYD09GA    # Aqua（Terraとは合成せず、常に別ファイル）
      version: "061"
    viirs:
      snpp: VNP09GA    # Suomi-NPP
      version: "002"
  clip_to_aoi: true          # true: AOIで切り抜き（native grid alignmentは変えない）
  earthdata_env_path: ./key.env  # 認証情報を読み込むファイル（firms.key_env_pathと同じ形式）
  keep_native_files: false   # true: ダウンロードしたHDF/HDF5を処理後も残す（デバッグ用）
```

| キー | 型 | デフォルト | 説明 |
|------|----|-----------|------|
| `surface_reflectance.products.modis.terra` | string | `MOD09GA` | Terra の短縮名 |
| `surface_reflectance.products.modis.aqua` | string | `MYD09GA` | Aqua の短縮名 |
| `surface_reflectance.products.modis.version` | string | `061` | MODIS プロダクトバージョン |
| `surface_reflectance.products.viirs.snpp` | string | `VNP09GA` | Suomi-NPP の短縮名 |
| `surface_reflectance.products.viirs.version` | string | `002` | VIIRS プロダクトバージョン |
| `surface_reflectance.clip_to_aoi` | bool | `true` | AOI で切り抜くか（native pixel gridは維持） |
| `surface_reflectance.earthdata_env_path` | string | `./key.env` | Earthdata 認証情報を読み込むファイル |
| `surface_reflectance.keep_native_files` | bool | `false` | ダウンロードした HDF/HDF5 を残すか |

### Earthdata 認証

`config.yaml` やソースコードに認証情報を直接書かないでください。以下のいずれかで設定します（優先順）：

1. 環境変数 `EARTHDATA_USERNAME` / `EARTHDATA_PASSWORD`（Docker では `-e` または `docker-compose.yml` の `environment` で渡せます）
2. `surface_reflectance.earthdata_env_path`（デフォルト `./key.env`）に FIRMS キーと同様の形式で追記：
   ```
   EARTHDATA_USERNAME=your_username
   EARTHDATA_PASSWORD=your_password
   ```
   （`key.env` は `.gitignore` 済みでコミットされません）
3. netrc ファイル（Linux/Mac: `~/.netrc`、Windows: `%USERPROFILE%\_netrc`）：
   ```
   machine urs.earthdata.nasa.gov
       login your_username
       password your_password
   ```

Earthdata アカウントは <https://urs.earthdata.nasa.gov/> で無料登録できます。

### 座標系・出力について

- ダウンロード・SR 生成段階では再投影・リサンプリングを一切行わず、NASA native Sinusoidal グリッド（CRS・affine transform・pixel size）をそのまま保持します。
- AOI が複数タイルに跨る場合は、同日・同プロダクト・同解像度のタイルを native grid 上で mosaic してから AOI で切り抜きます。
- Surface Reflectance（`sur_refl_b01`–`b07`, VIIRS `I1`–`I3`/`M1`–`M11`）は HDF 内の scale factor / offset / fill value を読み取り、float32 の物理反射率として保存します。
- QA（MODIS `QC_500m` / `state_1km`、VIIRS `QF1`–`QF7` / `land_water_mask`）は整数値のまま保持し、スケーリングは行いません。雲マスク処理（omnicloudmask 含む）は適用されません。
- MODIS Terra/Aqua、VIIRS I-band(500m)/M-band(1km) は互いに合成せず、常に別ファイルとして出力します。

出力例：

```
output/modis/surface_reflectance/terra/500m/MOD09GA_terra_20230301_500m.tif
output/modis/surface_reflectance/terra/qa/MOD09GA_terra_20230301_QC_500m.tif
output/modis/surface_reflectance/terra/qa/MOD09GA_terra_20230301_state_1km.tif
output/modis/surface_reflectance/aqua/500m/MYD09GA_aqua_20230301_500m.tif
output/viirs/surface_reflectance/snpp/500m/VNP09GA_snpp_20230301_500m.tif
output/viirs/surface_reflectance/snpp/1km/VNP09GA_snpp_20230301_1km.tif
output/viirs/surface_reflectance/snpp/qa/VNP09GA_snpp_20230301_QA_1km.tif
```

各 GeoTIFF には元の SDS 名・scale・offset・fill value・product/platform/date/tile 情報がバンドタグ（および同名の `.json` サイドカー）として保存されます。

---

## FIRMS 熱異常データ設定

熱異常データをダウンロードするには `firms.activefire_satellite` と `firms.product_map` の両方を設定してください。どちらかが未設定の場合はダウンロードされません。

```yaml
firms:
  key_env_path: ./key.env          # FIRMS_API_KEY を記載したファイルのパス
  api_key: ""                       # 直接書く場合（非推奨）
  activefire_satellite:
    - viirs
    - modis
  product_map:
    modis:
      - MODIS_SP                    # Terra + Aqua 統合標準プロダクト
    viirs:
      - VIIRS_SNPP_SP               # Suomi-NPP
      - VIIRS_NOAA20_SP             # NOAA-20
      - VIIRS_NOAA21_SP             # NOAA-21
```

| キー | 型 | デフォルト | 説明 |
|------|----|-----------|------|
| `firms.key_env_path` | string | `./key.env` | `FIRMS_API_KEY=...` を記載した `.env` ファイルのパス |
| `firms.activefire_satellite` | list | — | 取得対象衛星。`modis`, `viirs` から選択 |
| `firms.product_map.modis` | string \| list | — | MODIS プロダクト。`MODIS_SP` は Terra/Aqua 統合 |
| `firms.product_map.viirs` | string \| list | — | VIIRS プロダクト。センサ別に指定（例: `VIIRS_SNPP_SP`） |
| `firms.days` | int | `5` | 1リクエストあたりの日数（1〜5）。長い期間は自動分割 |
| `firms.bbox_buffer_m` | int | `5000` | FIRMS 取得時に AOI バウンディングボックスを広げる距離（m） |
| `firms.clip_to_aoi` | bool | `false` | 取得後に AOI ポリゴンでクリップするか |
| `firms.pixel_tif` | bool | `false` | 火事データをピクセルラスタ GeoTIFF でも出力するか |
| `firms.pixel_resolution` | int | `10` | ピクセルラスタの解像度（m） |
| `firms.pixel_expand_to_detections` | bool | `true` | 検知点がグリッド外に出る場合にラスタ範囲を自動拡張するか |
| `firms.period_summary` | bool | `false` | 期間全体をまとめた1つの Shapefile も出力するか |

### FIRMS API キーの取得

1. <https://firms.modaps.eosdis.nasa.gov/api/> で無料登録
2. MAP_KEY を取得
3. プロジェクトルートに `key.env` を作成：

```
FIRMS_API_KEY=your_map_key_here
```

> `key.env` は `.gitignore` に含まれているためリポジトリにコミットされません。`config.yaml` には直接書かないでください。

---

## 設定ファイルの全例

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
