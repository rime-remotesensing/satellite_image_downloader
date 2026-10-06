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
| `satellite` | list | 処理対象。選択肢: `sentinel2`, `landsat89`, `modis`, `viirs`, `gcomc` |
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
  - sentinel2   # / landsat89 / modis / viirs / gcomc

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

### GEE 互換出力グリッド（Sentinel-2）

Google Earth Engine エクスポートとグリッドを合わせるモード（AOI のバウンディングボックスを範囲とし、ピクセルアラインメントにスナップする）は Sentinel-2 で常に有効です。出力 CRS（UTM ゾーン）は **AOI GeoJSON のバウンディングボックス中心から自動判定**されます（`geopandas.estimate_utm_crs()` 等と同じ、bbox 中心の経度からゾーン番号・緯度から南北半球を決める標準的な方式）。`geojson:` を別地域に変更しても、古い UTM ゾーンが残ってしまう心配はありません。

AOI が UTM ゾーン境界（経度6度ごと）を跨ぐ場合でも、bbox 中心を含むゾーンに一意に決定されます。境界を跨ぐ AOI では中心から離れた側の歪みが多少大きくなりますが、これは UTM 投影自体の性質であり、跨ぐ AOI に対して「唯一正しいゾーン」は存在しません。

自動判定を上書きしたい場合や、GEE 互換モード自体を無効化したい場合のみ、従来通り `gee_compatible:` ブロックを追加してください：

```yaml
gee_compatible:
  enabled: true        # デフォルト true。false で無効化
  output_crs: EPSG:32654   # 指定すると自動判定を上書き
  aoi_as_bbox: true     # デフォルト true
  snap_grid: true       # デフォルト true
```

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

#### VIIRS L2 swath（VNP09 / VJ109 / VJ209）→ 固定 375 m / 750 m grid

`surface_reflectance.viirs_product` で VIIRS の取得方式を選びます。**既定は `l2_swath`** です（2026-10-03 に実データ統合監査を経て切り替え）。
`satellite: [viirs]` だけを指定した場合も L2 swath 方式になります。

| 値 | 内容 |
|---|---|
| `l2_swath`（既定） | 6-minute L2 swath（VNP09 / VJ109 / VJ209、collection 002）を固定 analysis grid へ配置 |
| `daily_l2g_legacy` | 上記の Daily L2G（VNP09GA / VJ109GA / VJ209GA、sinusoidal 約 463 m / 926 m）。明示した場合のみ使用 |

```yaml
satellite:
  - viirs
surface_reflectance:
  viirs_platforms: [snpp, noaa20, noaa21]   # 省略時は snpp のみ
  # viirs_product: daily_l2g_legacy        # 旧方式を使う場合のみ明示
```

`l2_swath` の出力は「nominal 375-m / 750-m VIIRS L2 observations mapped to fixed equal-area Sinusoidal analysis grids」です。
元の swath と IMG/MOD geolocation が native の正本であり、Sinusoidal 上の raster は解析用 product です（native swath geometry をそのまま保存した 375 m raster ではありません）。
I バンド（I1–I3）は IMG geolocation から fixed 375-m analysis grid へ、M バンド（M1–M5, M7, M8, M10, M11）は MOD geolocation から fixed 750-m analysis grid へ、
それぞれ独立に nearest-neighbour で配置します。750 m バンドを 375 m へ upsample することはしません
（モデル入力時に 750 m の 1 cell を対応する 375 m の 2×2 cell へ複製するのは、後段の前処理の役割です）。
375 m は解析 grid の間隔であり、実効的な空間分解能ではありません。阿蘇では I バンドの実 source footprint が約 423〜800 m（天頂角による）になることを確認しています。

- **固定 grid（既定 `VIIRS_SINUSOIDAL_GRID`）**: MODIS Land と同じ Sinusoidal CRS（`constants.py` の `MODIS_SINUSOIDAL_PROJ4`、
  球半径 R = 6371007.181 m）を使い、anchor も MODIS タイルグリッドの原点（`MODIS_SINUSOIDAL_X_MIN`, `MODIS_SINUSOIDAL_Y_MAX`）です。
  ただしセルは独自の 375 m / 750 m で、NASA の 500 m / 1 km（約 463 m / 926 m）L2G grid は使いません。
  セルの辺は 375 m が `x = X0 + n×375`, `y = Y0 − m×375`、750 m が `x = X0 + n×750`, `y = Y0 − m×750` で、anchor が共通なので
  750 m の 1 cell は 375 m の 2×2 cell と定義上完全に一致します（許容誤差で合わせるのではありません）。
  AOI の窓は 750 m 単位で外側へスナップし、375 m / 750 m で同じ地理範囲になります。全世界で同じ grid で、地域ごとの定義は不要です。
  原点を granule の範囲から計算することはありません（全日付・全 platform で同じ grid）
- **UTM grid（診断用）**: 以前の既定 `KYUSHU_UTM52N_GRID`（EPSG:32652、原点 E = 0, N = 0）は Phase 0 との比較・回帰確認用に残しています。
  grid 定義は `src/viirs_l2.py` の `AnalysisGridDefinition` として交換可能です
- **最近傍の距離**: Sinusoidal は面積を保存しますが距離・角度は保存しない（阿蘇の経度 131° では大きく剪断する）ため、
  最近傍の判定は Sinusoidal 平面上の距離ではなく地表距離で行います。grid の cell 中心を lon/lat に逆投影し、source の lon/lat とともに
  WGS84 楕円体上の 3 次元直交座標へ変換して `scipy.spatial.cKDTree` で最近傍を求め、弦長を地表距離に換算します。
  `source_distance_m` と footprint の局所半対角（隣接 geolocation 画素間の地表距離から計算）も地表距離です。
  つまり「投影 = 面積保存の Sinusoidal」「観測の選択 = 物理的な地表距離」を分けています
- **pairing**: L2 SR の `InputPointer` に記録された geolocation（VNP03IMG/MOD, VJ103IMG/MOD, VJ203IMG/MOD）のファイル名をそのまま使い、
  OrbitNumber・開始/終了時刻の一致を確認します（collection 番号の一致では判定しません）
- **配置**: 画素中心の nearest-neighbour。採用は source 画素の局所的な半対角以内のみ。bow-tie 削除画素は source から除外し、fill を補間しません。
  同一 orbit の隣接 granule は 1 つの KD-tree で mosaic します（書き込み順に依存しない）
- **overpass 出力**: overpass（orbit）ごとに必ず保存。AOI が swath 外の overpass は書き出さず summary に記録
- **daily 出力**: overpass 出力からの派生物（平均・中央値の合成はしない）。正式な選択規則は、750 m cell ごとに
  1. 全 M バンドの SR が有効 2. QF1 の雲の信頼度が低い 3. QF2 の影なし 4. センサー天頂角が小さい 5. 取得時刻が早い
  の順で 1 つの overpass を選び、その 2×2 の 375 m cell も同じ overpass の I バンドを使います（別時刻の I と M を混ぜない）。
  QF6（bit 定義に誤記あり）は選択・有効判定に使いません。
  NASA の L2G（GA）は observation coverage を優先して日次観測を選ぶため、候補が 2 つある日には採用 orbit が GA と 5〜11% の cell で異なります。
  これは意図したアルゴリズムの差で、本方式は GA の日次 composite を再現するものではありません
- **QA**: QF1–QF7 と land_water_mask を raw の uint8 のまま保存。雲画素の反射率を NaN にしません
- **出所の記録**: cell ごとに source granule、swath の行・列、スキャン時刻、source-to-target-centre distance を保存します
  （阿蘇では I バンドの実 footprint が約 423〜800 m あり、grid の 375 m と混同しないため）
- **観測のない cell**: 実観測の source が footprint 内にない cell（例: swath 最外縁で約 0.25%）は、補間・近傍値・人工的な値で埋めず、
  AOI 状態を `PARTIAL_OBSERVATION` として記録します
- **教師データとの対応**: burn-fraction の教師データを 375 m grid で作る前に、教師ラベル（PlanetScope 由来）の CRS を必ず確認してください

出力（`<platform>` = snpp / noaa20 / noaa21）:

```
output/viirs/surface_reflectance/<platform>/l2_swath/
  overpass/{375m,750m,qa,geometry,provenance}/<SR>_<platform>_<YYYYMMDD>_<HHMM>_o<orbit>_<group>.tif
  daily/{375m,750m,qa,geometry,provenance}/<SR>_<platform>_<YYYYMMDD>_daily_<group>.tif
  summary/<SR>_<platform>_<YYYYMMDD>.json
```

| group | grid | 内容 |
|---|---|---|
| `375m` | 375 m | I1, I2, I3（float32 反射率） |
| `750m` | 750 m | M1, M2, M3, M4, M5, M7, M8, M10, M11（float32 反射率） |
| `qa` / `qa_375m` | 750 m / 375 m | QF1–QF7, land_water_mask（raw uint8）/ IMG geolocation の land_water_mask |
| `geometry_375m` / `geometry_750m` | 375 m / 750 m | sensor_zenith, sensor_azimuth, solar_zenith, solar_azimuth（度） |
| `provenance_375m` / `provenance_750m` | 375 m / 750 m | source_granule, source_row, source_col, scan_time_unix, source_distance_m（daily は selected_orbit も） |

| キー | 既定値 | 説明 |
|---|---|---|
| `surface_reflectance.viirs_product` | `l2_swath` | `daily_l2g_legacy` で旧 Daily L2G 方式 |
| `surface_reflectance.viirs_l2_raw_dir` | 環境変数 `VIIRS_L2_RAW_DIR`、なければ `<output>/viirs/_raw_l2` | L2 SR と geolocation の raw archive。有効なファイルは再利用し、削除しません |

実データ監査は [viirs_l2_swath_smoke_test.md](viirs_l2_swath_smoke_test.md) を参照してください。

### GCOM-C/SGLI L2 RSRF（JAXA G-Portal）

`satellite` に `gcomc` を含めると、JAXA G-Portal から GCOM-C/SGLI Level-2 LAND の大気補正済み反射率（RSRF、
version 3002、日次 250 m タイル、descending）を取得します。MODIS/VIIRS とは独立した処理系（`src/gcomc.py`）で、
`gcomc` を指定しない実行では読み込まれません。実データ監査の記録は [gcomc_rsrf_smoke_test.md](gcomc_rsrf_smoke_test.md) を参照してください。

```yaml
satellite:
  - modis
  - viirs
  - gcomc
```

#### 取得の流れと状態

| 情報源 | 判定すること | 状態 |
|---|---|---|
| CSW（`https://csw.gportal.jaxa.jp/csw`、datasetId `10002015`） | プロダクトの record があるか | なし → `NO_RECORD` |
| SFTP（`sftp.gportal.jaxa.jp:22`、公開鍵認証） | 実際に取得できるか | `AVAILABLE` / `REQUEST_REQUIRED` / `ERROR` |
| HDF5 + AOI ポリゴン | AOI に実観測があるか | `OBSERVED` / `PARTIAL_OBSERVATION` / `AOI_NO_DATA` |

- 必要なタイルは AOI から SGLI EQA グリッド上で計算し、CSW はタイル番号（`tileHNo`/`tileVNo`）で検索します（bbox 検索は使いません）。
- SFTP の実パスは `/product/Standard/GCOM-C/GCOM-C.SGLI/L2.LAND.RSRF/3/YYYY/MM/DD/<filename>` です。
  CSW の `product.fileName`（`/products/...`）は `/Standard/` 以降だけを使って変換します。
- 観測から2.5年より古いことだけを理由に `REQUEST_REQUIRED` にはしません。SFTP が実際にファイルを返せば `AVAILABLE` です。
  SFTP 側でファイルなし・権限拒否となり、かつ2.5年ルール（30か月）に該当する場合のみ `REQUEST_REQUIRED` とし、
  その日は処理せずに報告します。Web からの個別要求は自動化しません。
- 認証失敗・ネットワーク障害・パス不整合・HDF5 破損・想定外のスキーマ（dataset 欠損、未知の version など）は `ERROR` です。
- **CSW record があっても、AOI がその日のスワス外で全画素 no data のことがあります**（阿蘇では概ね2日観測・2日欠測）。
  AOI 状態は QA_flag の bit0（no available data）で判定し、`AOI_NO_DATA` の日は
  `INFO GCOM-C/SGLI: no RSRF observation for AOI on YYYY-MM-DD` を出力して GeoTIFF を作りません（summary JSON のみ）。
- 日付範囲は毎日走査します。

#### G-Portal 認証（公開鍵）

新 G-Portal の SFTP は公開鍵認証のみです。新 G-Portal の Web で秘密鍵を発行し、リポジトリ外に保存してください。
`key.env`（git 管理外）に次を設定します。

```
GPORTAL_USERNAME=<G-Portal のアカウント名>
GPORTAL_PRIVATE_KEY_HOST_PATH=C:/Users/<you>/.ssh/<privatekey>
```

秘密鍵は docker compose の `gcomc` サービスで、コンテナ内 `/run/secrets/gportal_privatekey.key` に read-only で bind mount されます。
Python は Windows のホストパスを直接読みません。実行例：

```bash
docker compose --env-file key.env --profile gcomc run --rm gcomc python3 run.py --config config/config.yaml
```

`GPORTAL_USERNAME` は環境変数、または `surface_reflectance.gportal_env_path`（既定 `key.env`）から読み込みます。

#### 内部デフォルト・高度な設定

| キー | 既定値 | 説明 |
|---|---|---|
| `surface_reflectance.gcomc_raw_dir` | 環境変数 `GCOMC_RAW_DIR`、なければ `<output>/gcomc/_raw` | 生 HDF5 のキャッシュ（アーカイブ）。有効なファイルがあれば検証して再利用し、再ダウンロードしません |
| `surface_reflectance.gportal_env_path` | `key.env` | `GPORTAL_USERNAME` を読む env ファイル |

docker compose の `gcomc` サービスでは `GCOMC_RAW_DIR=/gcomc_data/raw` です。ホスト側の場所は `GCOMC_HOST_DATA_PATH`
（既定 `../satellite_image_downloader_gcomc_data`、リポジトリ外）で指定します。

生ファイルのダウンロードは、一時ファイル（`.part`）→ サイズ照合 → HDF5 検証 → atomic rename の順に行います。
キャッシュ内のファイルが検証に失敗した場合は、上書き・削除せずに `ERROR` とします。

#### 出力

```
output/gcomc/rsrf/250m/VN01 … VN11, SW03/   GCOMC_SGLI_RSRF_YYYYMMDD_D_<band>.tif（float32 反射率）
output/gcomc/rsrf/1km/SW01, SW02, SW04/       GCOMC_SGLI_RSRF_YYYYMMDD_D_<band>.tif（float32 反射率）
output/gcomc/rsrf/qa/QA_flag, Land_water_flag, Obs_time/   raw 整数（scale なし）
output/gcomc/rsrf/summary/GCOMC_SGLI_RSRF_YYYYMMDD_D.json  プロダクト状態・AOI 状態・バンド別有効率
```

- 250 m（実際の画素 231.66 m）と 1 km（926.63 m）は別グリッドのまま出力し、1 km バンドを 250 m に upsample しません。
  250 m と 1 km を同じラスタに統合することもしません（統合方法は後段の前処理で決める前提です）。
- AOI が T0528/T0529 のように複数タイルに跨る場合は、同じ native グリッド上で整数インデックスによりモザイクしてから
  AOI の最小 bbox で切り出します（resampling なし）。日付・バンドごとに1枚です。
- 反射率は HDF5 属性の `Slope`/`Offset` を使って `DN × Slope + Offset` を float32 で保存し、`Error_DN` と有効範囲外は NaN にします。
- **SW02 は公式定義上 TOA reflectance** です。GeoTIFF タグ `reflectance_type=TOA reflectance` で区別しています。
- 雲画素にも反射率値は入っています。downloader は雲を NaN にしません（QA_flag を使った雲除去は後段の役割です）。
- CRS は EQA タイルグリッドの sinusoidal（`+proj=sinu +lon_0=0 +R=6371007.181`）で、メートル座標は JAXA の L2 EQA タイル GeoTIFF 定義
  （Sphere_Sinusoidal、pixel scale 231.65635827 m）と同じです。画素中心は JAXA 公式 GeoTIFF ツールの定義と一致し、
  実データもこの定義に従うことを確認しています（[監査記録 第7節](gcomc_rsrf_smoke_test.md)）。

#### SGLI usable observation inventory（任意のユーティリティ）

ローカルの raw アーカイブから、日付 × リージョンごとの観測品質（プロダクト状態、AOI 状態、バンド別有効率、Error_DN 割合、
QA 各 bit の割合、clear 割合、観測時刻）を CSV にまとめます。通常のダウンロード処理には含まれません。

```bash
docker compose --env-file key.env --profile gcomc run --rm gcomc python3 -m src.gcomc_inventory \
  --start 2024-02-20 --end 2024-04-10 \
  --region region05=config/no5.geojson --region region09=config/no9.geojson \
  --out /gcomc_data/inventory/sgli_rsrf_usable_inventory.csv
```

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
```
