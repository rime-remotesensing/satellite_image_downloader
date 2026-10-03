# GCOM-C/SGLI L2 RSRF — 実データ監査とG-Portalアクセス記録

GCOM-C/SGLI Level-2 大気補正済み陸域反射率（RSRF）を downloader に追加するための、
実データ監査（Phase 0）と新G-Portalへのアクセス確認の記録です。
ここに記載した値は、実ファイル・実サーバーで確認したものだけです。
確認できていない事項は `UNRESOLVED` と明記しています。

状態: 監査結果に基づき `src/gcomc.py`（downloader 本体）と `src/gcomc_inventory.py`（観測品質の集計ユーティリティ）として実装済み。
使い方は [configuration.md](configuration.md#gcom-csgli-l2-rsrfjaxa-g-portal) を参照。
本書は実装前の監査記録として維持しています（第6節の smoke test は試作スクリプトによるもの）。

---

## 1. G-Portalアクセス仕様（2026-10-02 確認）

新G-Portal は 2026-10-01 に公開されました（旧システムは 2026年10月末停止予定）。
出典: JAXA「G-Portal 改修に伴うご案内」 https://gportal.jaxa.jp/gpr/notice/case/view/1502

| 項目 | 確認結果 | 確認方法 |
|---|---|---|
| CSW（カタログ） | `https://csw.gportal.jaxa.jp/csw` | 実際に検索 |
| RSRF datasetId | `10002015` | CSW検索結果の `gpp.datasetId` |
| SFTP host | `sftp.gportal.jaxa.jp`, port `22` | 実接続 |
| SFTP 認証 | `publickey` のみ（サーバーの allowed auth types） | 実接続 |
| SFTP host key | `ssh-rsa`, SHA256 `pYLmuMNFi9tQYRAZLXERRuyOszorS0qJQEE/u4Co+xM` | 実接続（banner `SSH-2.0-AWS_SFTP_1.2`） |
| ログイン直後のディレクトリ | `/`（直下に `product/`, `provide/`。`provide/` は確認時点で空） | 実接続 |

### 正式なSFTPパス

```
/product/Standard/GCOM-C/GCOM-C.SGLI/L2.LAND.RSRF/3/YYYY/MM/DD/<filename>
```

- `/products/...` はサーバー上に存在しません（`ENOENT`）。
- CSW JSON の `product.fileName`（`product.gportal.jaxa.jp:/products/Standard/...`）は
  **SFTP実パスとしてそのまま使用しないでください**。
  CSW の `/Standard/` 以降の部分だけを取り出し、`/product/Standard/` に連結します。
- 公式サンプルスクリプト（`make_filelist.sh`）の `standard/ → /product/Standard/` 変換と一致します。

### CSW検索の注意

- AOI の bbox 検索は使用しません。CSW のフットプリントはタイル4隅を直線で結んだ近似で、
  EQA の曲線境界付近のタイルを取りこぼします（阿蘇周辺で T0529 の取りこぼしを実際に確認）。
- SGLI タイルグリッドから必要タイルを求め、`datasetId` + 日付 + `tileHNo`/`tileVNo` で検索し、
  identifier の `D01D` / `A01D` で軌道方向を判定します。

### 実装メモ: 2.5年ルールの公式案内と実サーバー挙動の相違

公式案内では、RSRF を含む通常の GCOM-C L2 プロダクトについて、
観測開始日がダウンロード実行日より2.5年以上前の場合は
Web から1ファイルずつダウンロード要求が必要で、その手続きなしには SFTP から取得できない、とされています。
例外は L2 `*.Statistics`、`LTOA.Global`/`LCLR.Global`、L3 で、日次 L2 RSRF は例外に含まれません。

一方、2026-10-02 の実サーバーでは、2.5年より古いファイル（2024-02-21, 2024-03-15 など）も
通常の `/product/Standard/...` 配下に存在し、正規アカウント・公開鍵認証で stat・読み取りができました。
（`provide/` は確認時点で空。Web 要求後の配置先は未確認。）

方針（2026-10-02 決定）: 公式案内の記述は直接取得の禁止ではなく取得可能性の説明と解釈し、
正規SFTPから実際に取得できるファイルは直接取得します。
2.5年より古いという理由だけで `REQUEST_REQUIRED` には分類しません。
サーバー側の挙動は今後変わり得るため、状態は常に実サーバーへのアクセス結果で判定します。

### 取得状態の定義と判定順序

1. CSW record なし → `NO_OBSERVATION`
2. CSW record あり + 正規SFTPパスにファイルあり + stat・read 可能 → `AVAILABLE`
3. CSW record あり + SFTP から明示的に取得不可（ファイルなし / permission denied）+ 2.5年ルール対象 → `REQUEST_REQUIRED`
   （実装では、SFTP 側の明示的な拒否・不在を検出した時点で `REQUEST_REQUIRED_CANDIDATE` として停止・報告）
4. 認証・権限設定・ネットワーク・パス解決・一時的なサーバー障害・ファイル破損 → `ERROR`（`REQUEST_REQUIRED` とは区別）

SFTP 上にファイルがないことを `NO_OBSERVATION` として扱ってはいけません。
また、CSW record があることと、AOI で解析可能な観測であることは別です（第6節）。

---

## 2. 取得対象（研究上の確定方針）

- 期間: 2024-02-20 〜 2024-04-10（51日、毎日走査）
- 軌道: descending（`D01D`）。ascending（`A01D`）は夜間で、日本域では全画素 Error_DN（実ファイルで確認）
- タイル: T0528, T0529（v05 h28 / h29）
- プロダクト: L2 LAND RSRF, version `3002`
- 合計 102 ファイル。新CSW上で 102 件すべての record が存在（`NO_OBSERVATION` は 0 件）

T0529 は除外しません。研究対象（run2.py の C2 TRAIN+VAL リージョン）のポリゴン総面積
1051.57 km² のうち 580.29 km²（55.2%）が T0529 側にあり、region09 は 100% T0529 内です。
run2.py の23日分は取得対象の制限には使わず、後段の解析・学習用 subset として扱います。
MODIS/VIIRS の required-date plan を SGLI の観測品質の代理として使用しません。

### 取得状況（2026-10-02 完了）

| 状態 | 件数 |
|---|---|
| `AVAILABLE`（取得・検証済み） | **102**（T0528 51件 + T0529 51件） |
| `REQUEST_REQUIRED` | 0 |
| `NO_OBSERVATION`（CSW record なし） | 0 |
| `ERROR` | 0 |

全102ファイル（合計 17,359,537,626 bytes）を、次の手順で保存しました:
一時ファイル（`.part`）へダウンロード → SFTP stat サイズとの一致確認 → SHA-256 記録 → HDF5 検証 → atomic rename。
全件 Product_version `3002`。必須 dataset と native shape（250 m 4800×4800 / 1 km 1200×1200）は全件一致。
取得後にディスク上の全ファイルの SHA-256 を再計算し、記録値と102件とも一致しました。
サーバー側にチェックサムファイルは無いため、SHA-256 は取得時の記録値です（サーバー値との照合ではありません）。
2.5年より古い84件も、正規SFTPパスから stat・read・download できました（SFTP からの拒否・不在は0件）。

生データ・manifest はリポジトリ外（`GCOMC_HOST_DATA_PATH`、既定 `../satellite_image_downloader_gcomc_data`）に保存しています:

- `raw/GCOM-C/SGLI/L2.LAND.RSRF/3002/YYYY/MM/DD/<filename>`
- `manifests/rsrf_available_20240402_20240410.csv` — 最初に取得した18件の size / SHA-256 / HDF5 検証結果
- `manifests/rsrf_downloads.csv` — 残り84件の size / SHA-256 / HDF5 検証結果
- `manifests/rsrf_acquisition_status_20240220_20240410.csv` — 102件の状態表（SFTP パス・SHA-256 再検証結果を含む）
- `manifests/gcomc_rsrf_aso_csw_inventory_20240220_20240410.csv` — 新CSWの record 一覧
- `inventory/sgli_rsrf_usable_inventory_by_tile_20240220_20240410.csv` — 日付 × タイル × リージョンの観測品質
- `inventory/sgli_rsrf_usable_inventory_by_region_20240220_20240410.csv` — 日付 × リージョン（両タイル合算）の観測品質
- `logs/` — 取得・検証・集計に使用したスクリプトとログ

### SGLI usable observation inventory

研究対象リージョンのポリゴン（`config/noN.geojson`）内の native 画素（画素中心で判定。250 m / 1 km 各グリッド、resample なし）について、
日付 × タイル × リージョンごとに次を集計しました:
全250 m バンドが有効な画素の割合、Error_DN の割合、QA_flag の各 bit（nodata, cloud, probably cloud, shadow, snow/ice ほか）の割合、
VN01–VN11・SW03 の各バンドの有効率、SW01/SW02/SW04（1 km）の有効率、観測時刻。
画素の除外は行っていません（集計のみ）。参考値として、QA bit6/7/12 が立っておらず全250 m バンドが有効な画素の割合も記録しています。

結果の要点:
- **51日中28日に観測があり、23日は全リージョンで無観測**（QA bit0 no data）。観測日はおおむね「2日観測・2日欠測」の周期。
  観測がある日は、ほぼ全リージョンで有効率がほぼ100%（部分的な被覆は3件のみ）。
- **CSW record があることは AOI で観測があることを意味しない。** 102件すべてに record があるが、AOI で観測があるのは28日分。
- **MODIS/VIIRS の required-date plan は SGLI の代わりにならない。** run2.py の TRAIN+VAL では7リージョンすべてで使われている
  2024-03-15, 03-18, 03-22, 03-29, 04-10 は、SGLI では無観測。
- 観測日のうち、晴天画素（QA bit6/7/12 なし）が50%以上あるのは、TRAIN+VAL の各リージョンで7〜12日。

---

## 3. HDF5構造（Phase 0 監査）

監査ファイル: `GC1SG1_20240221D01D_T0529_L2SG_RSRFQ_3002.h5`
（並行運用期間中に旧G-Portal HTTPS 経由で取得。監査専用で、取得済みデータには含めていません）。
取得済み18ファイルについても、下記の dataset 存在・shape・dtype・属性・SW02 の記述を同じ基準で検証し、全件一致しました。

### Global_attributes

| 属性 | 値 |
|---|---|
| Product_name | Atmospheric corrected surface reflectance |
| Product_version | `3002` |
| Algorithm_version | `3.02` |
| Parameter_version | `002.08` |
| Product_file_name | ファイル名と一致 |
| Image_start_time / Image_end_time | 観測開始・終了時刻（UTC） |

ルートグループ: `Geometry_data`, `Global_attributes`, `Image_data`, `Level_1_attributes`, `Processing_attributes`

### 反射率 dataset

全 dataset 共通: dtype `uint16`、gzip、chunk 256×256、
`Slope` = 9.9999997e-05（float32）、`Offset` = 0.0、`Error_DN` = 65535、
`Minimum_valid_DN` = 0、`Maximum_valid_DN` = 65534。
物理値 = DN × Slope + Offset。Error_DN と有効範囲外は欠損として扱います。
NASA 形式の `scale_factor` / `add_offset` / `_FillValue` 属性はありません。

| HDF5 path | native shape | Spatial_resolution (deg) | 中心波長 (nm) | Data_description |
|---|---|---|---|---|
| `/Image_data/Rs_VN01` | 4800×4800 | 0.0020833334 | 380.03 | Surface reflectance of VN01 |
| `/Image_data/Rs_VN02` | 4800×4800 | 0.0020833334 | 412.51 | Surface reflectance of VN02 |
| `/Image_data/Rs_VN03` | 4800×4800 | 0.0020833334 | 443.24 | Surface reflectance of VN03 |
| `/Image_data/Rs_VN04` | 4800×4800 | 0.0020833334 | 489.85 | Surface reflectance of VN04 |
| `/Image_data/Rs_VN05` | 4800×4800 | 0.0020833334 | 529.64 | Surface reflectance of VN05 |
| `/Image_data/Rs_VN06` | 4800×4800 | 0.0020833334 | 566.16 | Surface reflectance of VN06 |
| `/Image_data/Rs_VN07` | 4800×4800 | 0.0020833334 | 672.00 | Surface reflectance of VN07 |
| `/Image_data/Rs_VN08` | 4800×4800 | 0.0020833334 | 672.10 | Surface reflectance of VN08 |
| `/Image_data/Rs_VN09` | 4800×4800 | 0.0020833334 | 763.07 | Surface reflectance of VN09 |
| `/Image_data/Rs_VN10` | 4800×4800 | 0.0020833334 | 866.76 | Surface reflectance of VN10 |
| `/Image_data/Rs_VN11` | 4800×4800 | 0.0020833334 | 867.12 | Surface reflectance of VN11 |
| `/Image_data/Rs_SW03` | 4800×4800 | 0.0020833334 | 1634.51 | Surface reflectance of SW03 |
| `/Image_data/Rs_SW01` | 1200×1200 | 0.0083333338 | 1054.99 | Surface reflectance of SW01 |
| `/Image_data/Rs_SW02` | 1200×1200 | 0.0083333338 | 1385.35 | **TOA reflectance of SW02** |
| `/Image_data/Rs_SW04` | 1200×1200 | 0.0083333338 | 2209.48 | Surface reflectance of SW04 |

- native 250 m（公称）: VN01–VN11, SW03。グリッド間隔 1/480° ≈ 0.00208333°
- native 1 km（公称）: SW01, SW02, SW04。グリッド間隔 1/120° ≈ 0.00833333°
- **SW02 は TOA reflectance**（実ファイルの `Data_description` と公式フォーマット仕様の両方で確認）。
  他の SW バンドと同じ surface reflectance として扱ってはいけません。
- 1 km バンドを 250 m に upsample しません。250 m / 1 km の統合方法は downloader では決めません。

### QA・補助 dataset

| HDF5 path | dtype | shape | 備考 |
|---|---|---|---|
| `/Image_data/QA_flag` | uint16 | 4800×4800 | `Error_DN` = 1（= bit0 no data）、Slope 1.0。bit 定義は `Data_description` に格納 |
| `/Image_data/Land_water_flag` | uint8 | 4800×4800 | 0(water)–100(land)、`Error_DN` = 255 |
| `/Geometry_data/Obs_time` | int16 | 4800×4800 | 観測時刻（hour）、Slope 0.001、`Error_DN` = -32768 |

QA_flag の bit（`Data_description` 原文）:
Bit00 no available data / Bit01 land / Bit02 coast / Bit03 sunglint flag>0.005 /
Bit04 sunglint mask>0.12 / Bit05 snow or ice / Bit06 cloud / Bit07 probably cloud /
Bit08 high tau-a>0.8 / Bit09 saturation recovery / Bit10 BRF samples<=3 /
Bit11 straylight flag / Bit12 shadow / Bit13 pol cloud or hi-tau /
Bit14 recovery by pre-days / Bit15 recovery (pol)

QA は raw integer のまま保持し、scale しません。

### 実ファイルで判明した注意点

- 1つの日次タイルに複数軌道の画素が混在することがあります
  （T0528 2024-02-21: Obs_time 約 1.16 h と 2.84 h UTC）。
- RSRF には画素ごとの緯度経度はありません。

---

## 4. タイルグリッド（EQA）

- タイル ID `Tvvhh`: vv = 縦方向 00–17（北→南）、hh = 横方向 00–35（西→東）、各10°×10°（赤道上）
- 投影: EQA（sinusoidal equal area、経度0°中心）、WGS84 楕円体の緯度経度
- `Geometry_data` のコーナー属性（例 T0529: UL 143.5948°E / 40°N、LL 127.0171°E / 30°N）は、
  連続 sinusoidal でタイル縁 x = lon·cos(lat) = 110° として計算した値と一致
- T0528 と T0529 の継ぎ目は連続
  （境界をまたぐ差がタイル内の隣接画素差より小さいことを実データで確認）

画素中心の緯度経度の定義と、それを GeoTIFF でどう表すかは第7節の地理位置監査で確定しました
（当初ここに記載していた「INT() により最大約0.36画素ずれ得る（UNRESOLVED）」は、ハンドブックの式の誤読に基づくもので撤回します。
正しくは `NINT` で、差は最大 0.217 px。さらに実データは連続モデルに従うことを確認しました）。

MODIS の sinusoidal 定数や h/v タイル処理は流用していません。

---

## 5. 未解決事項（UNRESOLVED）

1. 公式案内（2.5年ルール）と実サーバー挙動の相違が、移行期間中の暫定状態かどうか
2. Web 要求後のファイルの配置先（`/provide` か別の場所か）
3. 「2.5年」の厳密な境界の定義（月数・日数、境界日を含むか）
（解決済み）EQA 画素位置の定義と GeoTIFF の球半径 → 第7節
（解決済み）Phase 0 監査ファイル（旧HTTPS経由）と新SFTPから取得した同名ファイル（2024-02-21 T0528/T0529）は、
バイト単位で完全に一致しました（2026-10-03 確認）。

---

## 6. End-to-end smoke test（2026-10-02）

処理チェーン: SFTP download → size 照合 → SHA-256 → HDF5 検証 → Slope/Offset スケーリング
（Error_DN・有効範囲外 → NaN）→ native 解像度での抽出（250 m と 1 km を別グリッドのまま保持）
→ AOI（`config/no5.geojson`）の最小 WGS84 bbox を native 画素へ外側スナップして切り出し → GeoTIFF
→ 全出力を読み直して検証。

（試作スクリプト時点の記録）出力 GeoTIFF の CRS は `+proj=sinu +lon_0=0 +R=6378137`、グリッドは EQA タイルの連続アフィン。
250 m 画素 = 231.9156 m、1 km 画素 = 927.6624 m（比は厳密に 4）。
本実装では第7節の監査に基づき、JAXA の GeoTIFF 定義に合わせて R = 6371007.181 m
（250 m 画素 = 231.65635827 m、1 km 画素 = 926.62543306 m）に変更しています。画素中心の緯度経度は R によらず同一です。
出力構成: `gcomc/rsrf/250m/<field>/`, `gcomc/rsrf/1km/<field>/`, `gcomc/rsrf/qa/<field>/`, `gcomc/rsrf/ancillary/Obs_time/`。

| ファイル | 結果 | AOI 内の反射率有効率 |
|---|---|---|
| 2024-03-15 T0528 | 37/37 PASS | 0%（AOI がこの日のスワス外。QA は全画素 bit0 no data） |
| 2024-04-09 T0528 | **38/38 PASS**（AOI 内の有効値必須チェックを含む） | 99.8–100% |
| 2024-04-09 T0529 | **38/38 PASS**（同上） | 99.9–100% |

主な検証項目:
- 反射率は float32 で、値が DN × Slope + Offset と厳密に一致。Error_DN と有効範囲外が NaN と一致
- 1 km バンドは upsample されていない。250 m グリッドと整列し、1 km の切り出し範囲は 250 m の範囲を包含する
- QA_flag（uint16）、Land_water_flag（uint8）、Obs_time（int16）は raw 値のまま HDF5 と完全一致
- SW02 は `reflectance_type=TOA reflectance` のタグ付き
- 出所タグ（product、source filename、version、observation date、tile、native resolution、scale 適用有無）がある
- タイルのコーナー経度が、ファイル属性と 1e-3° 以内で一致
- 地点照合（2024-04-09）: Land_water_flag は阿蘇カルデラ・熊本市 = 100（陸）、有明海・八代海 = 0（水）

### 実データで判明した観測特性

- **阿蘇 AOI は毎日観測されるわけではない。** 2024-04-02〜04-10 では 04-04, 04-05, 04-08, 04-09 に観測があり、
  04-02, 04-03, 04-06, 04-07, 04-10 は T0528・T0529 とも AOI 全体が no data（スワス外）。
  CSW record はこれらの日にも存在するため、「CSW record あり」は「AOI で観測あり」を意味しない。
- **雲画素にも反射率値が入っている。** 雲は QA_flag の bit6（cloud）などで示されるだけで、Error_DN にはならない。
  雲の除外は QA を用いて後段で行う必要がある。
- CSW の `cloudCoverPercentage` はタイル全体の値で、AOI の雲量の代わりにはならない（03-15 T0528 は 8% だが AOI は観測外）。

---

## 7. 地理位置（geolocation）監査（2026-10-03）

250 m 画素内の燃焼面積率を PlanetScope の教師ラベルと対応させるため、画素中心の位置を JAXA 公式の定義と照合しました。

### 7.1 JAXA 公式の画素中心の定義は2通りある

| 出典 | 定義（0 始まりの行 lin・列 col、タイル v/h、1辺の画素数 n） |
|---|---|
| (HB) SHIKISAI Data Users Handbook 4.1.4.1 | d = 180/n/18、NP0 = 2·NINT(180/d)、lat = 90 − (lin + v·n + 0.5)·d、NP_i = **NINT**(NP0·cos(lat))、lon = 360/NP_i·(col + h·n − NP0/2 + 0.5) |
| (TOOL) Map projection & GeoTIFF conversion Tool User's Manual v1.2, App. 7.1 | x = m/10·(lon·cos(lat) + 180 − 10h) + 0.5、y = n/10·(90 − lat − 10v) + 0.5（最初の画素中心 = 1、m = n = 4800 / 1200） |

緯度の定義は両者で同一です。経度は、HB だけが各行の画素数を整数に丸める（NINT）ため、東西方向に行ごとの差が生じます。
（ハンドブックの PDF から抽出したテキストでは数式記号が文字化けしており、当初 `INT` と誤読していました。正しくは `NINT` です。）

### 7.2 GeoTIFF の affine/CRS と公式定義の一致度

出力 GeoTIFF の affine と CRS から求めた画素中心を PROJ（`rasterio.warp.transform`、EQA CRS → EPSG:4326）で緯度経度に戻し、
HB・TOOL を独立に実装した値と比較しました。
対象: T0528 / T0529、250 m / 1 km、タイル全域（1,200 × 1,200 点の格子）、左端・中央・右端 × 上端・中央・下端、
全行の左右端列、阿蘇 AOI 窓、T0528/T0529 境界の各 8 列。
px_x = 東西差を画素幅（その行の 360/NP_i 度）で割った値、px_y = 南北差 / d、距離 = WGS84 測地線距離。

**affine/CRS vs TOOL**（全対象・全集合で最大値）

| grid | px_x 最大 | px_y 最大 | 距離 最大 | \|Δlon\| 最大 | \|Δlat\| 最大 |
|---|---|---|---|---|---|
| 250 m | 4.5e-11 px | 6.8e-12 px | 1.1e-8 m | 1.1e-13° | 1.4e-14° |
| 1 km | 1.1e-11 px | 1.7e-12 px | 1.1e-8 m | 1.1e-13° | 1.4e-14° |

→ 現在の GeoTIFF の affine/CRS は、JAXA ツールの定義を数値誤差の範囲で完全に再現します（R = 6378137 / 6371000 / 最終値 6371007.181 の3通りで検証。最終値での最大差は 250 m で 3.5e-11 px、1 km で 8.8e-12 px。画素中心は R に依存しない）。

**affine/CRS（= TOOL）vs HB**（中央値 / P90 / P99 / 最大）

| grid | tile | 集合 | px_x | 距離 [m] |
|---|---|---|---|---|
| 250 m | T0528 | タイル全域 | 0.093 / 0.161 / 0.183 / 0.199 | 21.6 / 37.3 / 42.6 / 46.2 |
| 250 m | T0528 | 阿蘇 AOI 窓 | 0.087 / 0.172 / 0.179 / 0.180 | 20.3 / 39.9 / 41.6 / 41.7 |
| 250 m | T0528 | 境界側 8 列 | 0.088 / 0.164 / 0.179 / 0.181 | 20.4 / 38.1 / 41.6 / 42.1 |
| 250 m | T0529 | タイル全域 | 0.102 / 0.176 / 0.201 / 0.217 | 23.7 / 40.8 / 46.6 / 50.4 |
| 250 m | T0529 | 阿蘇 AOI 窓 | 0.088 / 0.172 / 0.180 / 0.180 | 20.3 / 40.0 / 41.7 / 41.7 |
| 1 km | T0528 | タイル全域 | 0.085 / 0.157 / 0.182 / 0.196 | 78.8 / 146 / 169 / 182 |
| 1 km | T0528 | 阿蘇 AOI 窓 | 0.097 / 0.158 / 0.167 / 0.167 | 90.3 / 147 / 155 / 155 |
| 1 km | T0529 | タイル全域 | 0.093 / 0.172 / 0.199 / 0.214 | 86.3 / 160 / 185 / 198 |
| 1 km | T0529 | 阿蘇 AOI 窓 | 0.098 / 0.159 / 0.167 / 0.167 | 90.6 / 147 / 155 / 155 |

左端・中央・右端 × 上端・中央・下端の各点も最大 0.217 px 以内です（タイルの東端ほど大きい）。南北方向の差はありません（< 1e-11 px）。
→ HB と TOOL の差は無視できる大きさではありません（250 m で最大 50 m、中央値でも約 0.1 px）。

### 7.3 データ自体はどちらの定義で格子化されているか（実測）

HB と TOOL の差 δ = lon·(NP_i − NP0·cos(lat))/360 [px] は、NINT の端数が1行ごとに約 0.4 進むため、行ごとに疑似ランダムに変わります。
データが HB で格子化されていれば、海岸線などの境界の位置にこの δ の揺れが現れます。
Land_water_flag（0〜100 の陸地率）から各行の海岸線位置を画素以下の精度で推定し、
近傍 4 行で detrend した残差を、同じ処理をした δ の予測値に回帰しました
（傾き ≈ 1 なら HB、≈ 0 なら TOOL）。

| データ | タイル | 傾き ± 1σ | n |
|---|---|---|---|
| Land_water_flag（51日分の合成） | T0528 | −0.024 ± 0.055 | 3,157 |
| Land_water_flag（51日分の合成） | T0529 | 0.043 ± 0.058 | 1,959 |
| VN11 反射率（晴天 12 日分、陸/水の線形混合） | T0528 | −0.091 ± 0.077 | 1,186 |
| VN11 反射率（晴天 12 日分、陸/水の線形混合） | T0529 | 0.017 ± 0.056 | 1,539 |
| 陽性対照: Land_water_flag を HB の幾何に再標本化 | T0528 | 0.555 ± 0.054 | 2,861 |
| 陽性対照: Land_water_flag を HB の幾何に再標本化 | T0529 | 0.811 ± 0.053 | 1,288 |

陽性対照（丸めと補間で減衰するが明瞭に検出）に対し、実データの傾きはすべて 0 と整合し、陽性対照から 7〜10σ 離れています。
→ **RSRF v3002 は TOOL（連続）定義で格子化されており、HB の NINT 定義には従っていません。**

### 7.4 JAXA 公式 GeoTIFF ツールとの独立比較

`SGLI_geo_map_linux.exe`（v1.2、G-Portal 配布、SHA-256 `6ccac383…d9d3cb`）で 2024-04-09 の T0528/T0529 を
緯度経度 GeoTIFF に変換しました（NN、既定の 7.5″ / 30″）。ツールは再投影するため、native ラスタとの画素単位の比較ではなく、
ツール出力の各画素中心の緯度経度を native 格子へ戻し、最近傍の native 値がツールの値と一致するかを調べました。

| データ | タイル | 比較画素数 | affine/CRS で一致 | うち値の境界の画素で一致 | HB で一致（境界画素） | ツールの有効画素がタイル外に置かれる数（affine / HB） |
|---|---|---|---|---|---|---|
| Land_water_flag 250 m | T0528 | 16,859,466 | 100.000% | 100.000%（296,435） | 94.8% | 0 / 378 |
| Land_water_flag 250 m | T0529 | 23,276,801 | 100.000% | 100.000%（202,337） | 93.5% | 0 / 452 |
| Rs_SW01 1 km | T0528 | 252,096 | 100.000% | 100.000% | 91.7% | 0 / 44 |
| Rs_SW01 1 km | T0529 | 355,745 | 100.000% | 100.000% | 90.8% | 0 / 50 |

さらに、pipeline が出力した 2024-04-09 のモザイク GeoTIFF（T0528+T0529）を、阿蘇 AOI ポリゴン内のツール出力画素と照合しました:
Land_water_flag 10,159 画素（T0528 側 8,223 / T0529 側 1,936）、SW01 640 画素（515 / 125）がすべて一致
（SW01 は DN × Slope に換算して比較）。海岸（陸水境界）、T0528/T0529 境界、AOI との位置関係のいずれも JAXA ツールと一致しています。

### 7.5 球半径 R

JAXA の EQA の定義は度単位（x = lon·cos(lat)）で、半径を含みません。R は GeoTIFF でメートル座標として表すための規約です。
JAXA 自身の L2 EQA タイル GeoTIFF の定義（Higher Level Product Format Description の付属シート GeoTIFF Tag List）は
`GeogGeodeticDatumGeoKey = 6035`（DatumE_Sphere）、`PCSCitationGeoKey = "Sphere_Sinusoidal"`、
`ModelPixelScaleTag = 231.65635827 m`（250 m）/ `926.62543306 m`（1 km）です。pixel scale は R = 231.65635827 × 480 × 180/π = **6,371,007.181 m** に対応します
（同じ表の Datum コード 6035 は EPSG 上 R = 6,371,000 m の球を指し、JAXA 仕様内で厳密には整合していません。座標値を決めるのは pixel scale なので、そちらに合わせます）。
当初の実装は WGS84 の長半径 6,378,137 m を便宜的に使っていました（JAXA の定義ではない）。
画素中心の緯度経度は R によらず同一（7.2 で検証）ですが、JAXA の GeoTIFF とメートル座標を一致させるため R = 6,371,007.181 m に変更しました。
この値は MODIS sinusoidal の球半径と同じですが、MODIS の定数は流用せず JAXA のタグ値から独立に導いています。

### 7.6 判定

- 出力 GeoTIFF の affine + CRS（R = 6,371,007.181 m）は、JAXA 公式 GeoTIFF ツールの EQA 画素中心定義を 1e-10 px 未満で再現し、
  データ自体もこの定義に従っていることを実測で確認しました。**現在の実装（単一の affine 変換）を正確な native geolocation として維持します。**
- ハンドブックの NINT 式とは最大 0.217 px（250 m で 50 m）異なりますが、v3002 のデータはその式に従っていません。
  ハンドブックの式で画素位置を計算すると、むしろ最大 0.2 px の誤差を持ち込むことになります。
- 監査スクリプトと結果は raw アーカイブ側の `logs/` に保存しています。
