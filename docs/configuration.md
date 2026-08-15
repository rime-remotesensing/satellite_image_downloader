# 設定リファレンス

設定はすべて YAML ファイル（デフォルト: `config/config.yaml`）に記述し、以下で実行します：

```bash
python run.py --config config/config.yaml
```

`config.yaml` には**実行ごとに変更する可能性が高い項目だけ**を置いています。それ以外の詳細設定・内部パラメータはコード側のデフォルトとして保持されており、`config.yaml` に書かなくても従来と同じ動作をします。上書きしたい場合のみ、下記「Internal defaults / advanced behavior」に記載のキーを `config.yaml` に追加してください。

---

## User-facing settings

| キー | 型 | 説明 |
|------|----|------|
| `geojson` | string | AOI を定義する GeoJSON ファイルのパス（Polygon または MultiPolygon） |
| `startday` | string \| list | 開始日（`YYYYMMDD` 形式） |
| `endday` | string \| list | 終了日（`YYYYMMDD` 形式） |
| `satellite` | list | 処理対象。選択肢: `sentinel2`, `landsat89`, `modis`, `viirs` |
| `band` | string | `all` = 全バンド、`at` = `num` で指定したバンドのみ（Sentinel-2/Landsat 8-9 のみ） |
| `num` | list | `band: at` のときに取得するバンド番号（例: `[2, 3, 4, 8]`） |
| `cloudmask` | list | マスク対象の雲クラス。`1` = 厚雲、`2` = 薄雲、`3` = 影（Sentinel-2/Landsat 8-9 のみ） |
| `snowmask` | bool | 雲マスクの後に雪マスク処理を行うか（Sentinel-2/Landsat 8-9 のみ） |
| `activefire` | string | FIRMS 熱異常の取得レベル。`SP` / `NRT` / `none`（詳細は下記） |

### Area・期間

```yaml
geojson: ./config/area.geojson
startday: "20240101"
endday:   "20240131"
```

複数期間を順番にループ実行したい場合は同じ長さの配列で指定します（各ペアが独立して処理されます）：

```yaml
startday: [20230306, 20230311, 20230410]
endday:   [20230306, 20230311, 20230410]
```

> **FIRMS の注意**: 複数期間を指定した場合、FIRMS は `min(startday)` から `max(endday)` の全期間を1回で取得します。

### 衛星・バンド・雲/雪マスク

```yaml
satellite:
  - sentinel2   # / landsat89 / modis / viirs

band: all
num: []

cloudmask: [1, 3]
snowmask: true
```

`satellite` に `modis` / `viirs` を含めると、NASA Earthdata から daily Surface Reflectance をネイティブの Sinusoidal グリッドのまま（再投影・リサンプリング・雲マスクなし）取得します。Google Earth Engine は使用しません。FIRMS 熱異常取得（`activefire`）とは完全に独立した処理系です。詳細は下記「MODIS/VIIRS Surface Reflectance」を参照してください。

> omnicloudmask に必要なバンドは `num` の指定に関わらず常にダウンロードされます。

### FIRMS Active Fire (`activefire`)

```yaml
activefire: SP    # SP / NRT / none
```

| 値 | 意味 |
|----|------|
| `SP` | Standard Processing。確定値。過去データの解析向け |
| `NRT` | Near Real-Time。速報値。直近監視向け |
| `none` | FIRMS 熱異常を取得しない |

大文字小文字は区別しません（`sp`/`SP`/`Sp` はすべて同じ）。それ以外の値（例: `activefire: test`）はエラーになります。

対象プラットフォーム（MODIS Terra+Aqua、VIIRS Suomi-NPP、VIIRS NOAA-20、VIIRS NOAA-21）はコード側で固定されており、選択できません。内部的には以下の NASA FIRMS Area API source にマッピングされます：

| プラットフォーム | SP | NRT |
|---|---|---|
| MODIS (Terra+Aqua) | `MODIS_SP` | `MODIS_NRT` |
| VIIRS Suomi-NPP | `VIIRS_SNPP_SP` | `VIIRS_SNPP_NRT` |
| VIIRS NOAA-20 | `VIIRS_NOAA20_SP` | `VIIRS_NOAA20_NRT` |
| VIIRS NOAA-21 | *(利用不可)* | `VIIRS_NOAA21_NRT` |

> **NOAA-21 の Standard Processing (SP) source は現時点で NASA FIRMS に存在しません**（NRT のみ提供、[FIRMS API](https://firms.modaps.eosdis.nasa.gov/api/area/) / [data availability](https://firms.modaps.eosdis.nasa.gov/api/data_availability/) で確認）。`activefire: SP` を指定した場合、NOAA-21 は明確な warning ログを出したうえでスキップされ、NRT へ自動フォールバックすることはありません。

FIRMS の主出力は元の point/event data（Shapefile: 緯度経度・取得日時・confidence・FRP・scan/track 等）です。ピクセルラスタ（`activefire_tif/`）はデフォルトでは生成されません（下記参照）。

### GEE 互換出力 CRS（Sentinel-2、AOI 依存）

```yaml
gee_compatible:
  enabled: true
  output_crs: EPSG:32652   # AOI に合わせた UTM ゾーン
  aoi_as_bbox: true
  snap_grid: true
```

Google Earth Engine エクスポートとグリッドを合わせるためのオプションです。`output_crs` は AOI の地域ごとに異なる UTM ゾーンを指定する必要があるため、他の詳細設定と異なりコード側の共通デフォルトを持たせていません。GEE 互換出力が不要な場合は `enabled: false` にするか、このブロックごと `config.yaml` から削除してください。

---

## Internal defaults / advanced behavior

以下は通常変更しない設定です。`config.yaml` に書かなくても、下表のデフォルト値で今まで通り動作します。上書きしたい場合のみ、該当のキーをそのまま `config.yaml` に追加してください（既存の書式のままです）。

| キー | デフォルト | 説明 |
|------|-----------|------|
| `output` | `output` | 出力のルートディレクトリ |
| `max_cloud_cover` | `80` | この雲被覆率（%）を超えるシーンはスキップ（STAC メタデータによるフィルタ） |
| `file_exists` | `skip` | 出力ファイルが既にある場合の動作。`skip` = スキップ、`overwrite` = 上書き |
| `img_only` | `false` | `img/` のみ（再）生成し、雲・雪マスクはスキップ（CLI `--img-only` でも指定可） |
| `metadata.enabled` | `true` | 撮影メタデータを `img/` 配下に GeoJSON で保存するか |

### omnicloudmask の推論設定

```yaml
omnicloudmask:
  batch_size: 1
  patch_size: 1000
  patch_overlap: 300
  device: cuda     # "cuda" または "cpu"
```

上記が内部デフォルトです。一部のキーだけ書いた場合は残りがデフォルト値で補われます。

### 雪マスクの閾値

`snowmask: true/false` は基本設定側で指定します。閾値を変更したい場合は従来通りの辞書形式も使えます（`snowmask: true/false` の代わりに指定）：

```yaml
snowmask:
  enabled: true
  ndsi_threshold: 0.4   # デフォルト
  red_threshold: 0.2    # デフォルト
```

### MODIS/VIIRS Surface Reflectance（NASA Earthdata）

product/version・AOI クリップ・Earthdata 認証情報のパス・ネイティブ HDF/HDF5 の保持有無は、以下の内容がコード側デフォルトとして組み込まれており、通常は `config.yaml` に書く必要はありません：

| 項目 | デフォルト |
|------|-----------|
| MODIS Terra | `MOD09GA.061` |
| MODIS Aqua | `MYD09GA.061` |
| VIIRS Suomi-NPP | `VNP09GA.002` |
| AOI クリップ | 有効（native grid alignment は維持） |
| Earthdata 認証情報ファイル | `./key.env` |
| ダウンロード済み HDF/HDF5 の保持 | 無効（処理後に削除） |

上書きが必要な場合のみ、従来通り `surface_reflectance:` ブロックを追加してください：

```yaml
surface_reflectance:
  products:
    modis:
      terra: MOD09GA
      aqua: MYD09GA
      version: "061"
    viirs:
      snpp: VNP09GA
      version: "002"
  clip_to_aoi: true
  earthdata_env_path: ./key.env
  keep_native_files: false
```

#### Earthdata 認証

`config.yaml` やソースコードに認証情報を直接書かないでください。以下のいずれかで設定します（優先順）：

1. 環境変数 `EARTHDATA_USERNAME` / `EARTHDATA_PASSWORD`（Docker では `-e` または `docker-compose.yml` の `environment` で渡せます）
2. `key.env`（デフォルト、FIRMS キーと同じ形式で追記）：
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

#### 座標系・出力について

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

### FIRMS 熱異常データの詳細設定・API キー

`activefire: SP/NRT` で自動的に有効になります。以下は内部デフォルトで、通常は変更不要です：

| キー | デフォルト | 説明 |
|------|-----------|------|
| `firms.key_env_path` | `key.env` | `FIRMS_API_KEY=...` を記載した `.env` ファイルのパス |
| `firms.base_url` | `https://firms.modaps.eosdis.nasa.gov/api/area/csv` | FIRMS Area API のベース URL |
| `firms.bbox_buffer_m` | `5000` | FIRMS 取得用の BBOX を AOI から上下左右に広げる距離 (m) |
| `firms.clip_to_aoi` | `false` | 取得後に AOI ポリゴンで切り抜くか |
| `firms.days` | `5` | 1リクエストあたりの日数（1〜5）。長い期間は自動分割 |
| `firms.period_summary` | `true` | `startday`–`endday` 全期間の総まとめ Shapefile も1つ出力するか |
| `firms.pixel_tif` | `false` | 熱異常をピクセルラスタ GeoTIFF（`activefire_tif/`）でも出力するか。**デフォルトで無効**（主出力は point/event data） |
| `firms.pixel_resolution` | `10` | `pixel_tif: true` のときのラスタ解像度 (m) |
| `firms.pixel_expand_to_detections` | `true` | 検知点がグリッド外に出る場合にラスタ範囲を自動拡張するか |

上書き例：

```yaml
firms:
  bbox_buffer_m: 10000
  pixel_tif: true   # activefire_tif/ を再度生成したい場合
```

#### FIRMS API キーの取得

1. <https://firms.modaps.eosdis.nasa.gov/api/> で無料登録
2. MAP_KEY を取得
3. プロジェクトルートに `key.env` を作成：

```
FIRMS_API_KEY=your_map_key_here
```

> `key.env` は `.gitignore` に含まれているためリポジトリにコミットされません。`config.yaml` には直接書かないでください。

### FIRMS の旧設定（`firms.activefire_satellite` / `firms.product_map`）

`activefire` キーが `config.yaml` に存在しない場合に限り、以前の設定形式も引き続き読み取れます（下位互換）。`activefire` が設定されている場合は常にそちらが優先されます。

```yaml
firms:
  activefire_satellite:
    - viirs
    - modis
  product_map:
    modis:
      - MODIS_SP
    viirs:
      - VIIRS_SNPP_SP
      - VIIRS_NOAA20_SP
```

新しい設定では `activefire: SP` がこれと同等です（ただし存在しない `VIIRS_NOAA21_SP` は要求しません）。可能な場合は新設定への移行を推奨します。

---

## 設定ファイルの全例

```yaml
geojson: ./config/area.geojson
startday: "20240101"
endday:   "20240131"

satellite:
  - sentinel2
  - landsat89

band: all
num: []

cloudmask: [1, 3]
snowmask: false

activefire: none

gee_compatible:
  enabled: false
  output_crs: EPSG:32654
  aoi_as_bbox: true
  snap_grid: true
```
