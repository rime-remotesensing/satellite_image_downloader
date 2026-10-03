# VIIRS L2 swath Surface Reflectance — Phase 0 実データ監査と固定 grid 設計

VIIRS Surface Reflectance を Daily L2G（VNP09GA / VJ109GA / VJ209GA）から
L2 swath（VNP09 / VJ109 / VJ209）へ移行するための、実 granule の監査と解析 grid の設計記録です。
ここに書いた値は 2024 年の実ファイルで確認したものです。確認できていない事項は `UNRESOLVED` と明記しています。

状態: Phase 0 の監査・設計（第1〜8節）に基づき `src/viirs_l2.py` として実装し、実データ統合監査（第11節）を経て
**2026-10-03 に VIIRS の既定を `l2_swath` に切り替えました。** 旧 Daily L2G（VNP09GA / VJ109GA / VJ209GA）は
`surface_reflectance.viirs_product: daily_l2g_legacy` を明示した場合に引き続き利用できます（既存実装は無変更）。

本書でいう出力は「nominal 375-m / 750-m VIIRS L2 observations mapped to a fixed 375-m / 750-m analysis grid」です。
375 m / 750 m は解析 grid の間隔であり、実効的な空間分解能ではありません（阿蘇では I バンドの実 footprint が約 423〜800 m）。
swath から固定 grid への配置は地図 grid への再配置（nearest-neighbour による resampling）であり、
元の swath pixel を一切 resampling していない、という意味ではありません。
避けたいのは L2G への解像度低下（375 m → 約 463 m、750 m → 約 926 m）です。

---

## 1. 対象と取得ファイル

- 日付: 2024-03-10（DOY 070）。SGLI の観測品質一覧で全研究リージョンがほぼ晴天、run2 の TRAIN+VAL でも全 7 リージョンが使用している日
- AOI: `config/no5.geojson`（阿蘇）の最小 bbox + 10 km halo。位置検証には沿岸 box（130.2–130.9°E, 32.6–33.2°N）も使用
- 検索: CMR（collection concept id + temporal + bbox）。昼間の granule のみ
- 取得: Earthdata 認証の HTTPS。`.part` → Content-Length 照合 → SHA-256 → rename。リポジトリ外に保存
  （`../satellite_image_downloader_viirs_data/raw/<product>/<YYYY>/<DDD>/`、manifest は `manifests/phase0_20240310.json`）

| platform | collection（CMR） | L2 SR granule | IMG geolocation | MOD geolocation | orbit | 取得時刻 (UTC) |
|---|---|---|---|---|---|---|
| SNPP | VNP09 v2 / VNP03IMG v2 / VNP03MOD v2 | `VNP09.A2024070.0348.002.2024070133120.hdf` | `VNP03IMG.A2024070.0348.002.2024070104626.nc` | `VNP03MOD.A2024070.0348.002.2024070104626.nc` | 64082 | 03:48–03:54 |
| NOAA-20 | VJ109 v2 / VJ103IMG v2.1 / VJ103MOD v2.1 | `VJ109.A2024070.0436.002.2024070113811.hdf` | `VJ103IMG.A2024070.0436.021.2024070103022.nc` | `VJ103MOD.A2024070.0436.021.2024070103022.nc` | 32737 | 04:36–04:42 |
| NOAA-21 | VJ209 v2 / VJ203IMG v2.1 / VJ203MOD v2.1 | `VJ209.A2024070.0318.002.2025294162600.hdf` | `VJ203IMG.A2024070.0318.021.2024312094153.nc` | `VJ203MOD.A2024070.0318.021.2024312094153.nc` | 6892 | 03:18–03:24 |
| NOAA-21 | 同上 | `VJ209.A2024070.0500.002.2025294162436.hdf` | `VJ203IMG.A2024070.0500.021.2024312093457.nc` | `VJ203MOD.A2024070.0500.021.2024312093457.nc` | 6893 | 05:00–05:06 |

比較用に旧方式の L2G（`VNP09GA` / `VJ109GA` / `VJ209GA`、h28v05 と h29v05、version 002）も同日分を取得しました。

- 阿蘇 AOI はこの日、各 platform の昼間 granule 1 本の内側に収まっており、granule 境界はまたいでいません。
- NOAA-21 は同じ日に 2 回の昼間 overpass（6892 と 6893）があります。
- SR の collection は 002、geolocation は SNPP が 002、NOAA-20/21 が 021 で、番号は一致しません。

## 2. SR と geolocation の pairing

L2 SR の global attribute `InputPointer` に、使用した geolocation の完全なファイル名が記録されています（例: NOAA-20）。

```
VJ135_L2...,VJ1AMI_L2...,VJ104_L2...,VJ103IMG.A2024070.0436.021.2024070103022.nc,
VJ102CCIMG...,VJ102CCMOD...,VJ103MOD.A2024070.0436.021.2024070103022.nc
```

pairing は次の全条件で確認します（version 番号の一致では判定しません）。

1. SR の `InputPointer` に、`<geo product>.` で始まる名前がちょうど 1 つある
2. その名前のファイルが raw archive に存在する
3. geolocation の `OrbitNumber`、`StartTime`、`EndTime` が SR と完全に一致する

4 本の SR すべてで、IMG と MOD がそれぞれ 1 対 1 に対応しました。

## 3. ファイル構造（実ファイルが authority）

### 3.1 L2 SR（VNP09 / VJ109 / VJ209、collection 002）

- 形式: **HDF4（HDF-EOS2）**、swath 名 `SurfReflect_VNP`。拡張子 `.hdf`
- 地理位置情報は含まれません（geolocation product が必須）
- 3 platform で SDS の名前・型・属性はすべて同一。違うのは along-track の行数だけです
  （SNPP 6496 / 3248、NOAA-20 6464 / 3232、NOAA-21 6432 / 3216。granule ごとに変わるため固定値にしない）
- 主な global attribute: `ShortName`, `VersionID`=002, `OrbitNumber`, `StartTime`/`EndTime`, `PlatformShortName`
  （SUOMI-NPP / JPSS-1 / JPSS-2）, `InputPointer`, `AlgorithmVersion`=`NPP_PRSRefl 2.0.3`, `PGEVersion`（2.0.10 / 2.0.12）

| SDS 名（完全一致） | 形状 | 型 | scale_factor | add_offset | _FillValue | valid_range |
|---|---|---|---|---|---|---|
| `375m Surface Reflectance Band I1` / `I2` / `I3` | (along_track_375m, 6400) | int16 | 9.9999997e-05 | 0.0 | −28672 | [−100, 16000] |
| `750m Surface Reflectance Band M1` / `M2` / `M3` / `M4` / `M5` / `M7` / `M8` / `M10` / `M11` | (along_track_750m, 3200) | int16 | 9.9999997e-05 | 0.0 | −28672 | [−100, 16000] |
| `QF1 Surface Reflectance` 〜 `QF7 Surface Reflectance` | (along_track_750m, 3200) | uint8 | — | — | 'N/A' | [0, 255] |
| `land_water_mask` | (along_track_750m, 3200) | uint8 | — | — | 'N/A' | [0, 7] |

- I バンドは 375 m、M バンドは 750 m の swath 格子。along-track は常に I = 2 × M、along-scan は 6400 / 3200
- **QF1〜QF7 と land_water_mask は 750 m 格子のみ**です（I バンドの品質 bit も 750 m の QF4・QF6 にあります）
- `land_water_mask`: 0 Shallow_Ocean, 1 Land, 2 Coastline, 3 Shallow_Inland, 4 Ephemeral, 5 Deep_Inland, 6 Continental, 7 Deep_Ocean
- 反射率の特殊値は `_FillValue` −28672 のみ観測されました（valid_range 外の他のコードは無し）

### 3.2 QA bit 定義（`QA index` 属性の原文から。bit 7 = MSB）

| SDS | bit | 内容 |
|---|---|---|
| QF1 | 6–7 | sun glint（00 none, 01 geometry, 10 wind speed, 11 both） |
| QF1 | 5 | low sun mask（0 high, 1 low） |
| QF1 | 4 | day/night（0 day, 1 night） |
| QF1 | 2–3 | cloud detection & confidence（00 confident clear, 01 probably clear, 10 probably cloudy, 11 confident cloudy） |
| QF1 | 0–1 | cloud mask quality（00 poor, 01 low, 10 medium, 11 high） |
| QF2 | 7 / 6 | thin cirrus emissive / reflective |
| QF2 | 5 | snow/ice |
| QF2 | 4 | heavy aerosol mask |
| QF2 | 3 | shadow mask（0 no cloud shadow, 1 shadow） |
| QF2 | 0–2 | land/water background（000 land & desert, 001 land no desert, 010 inland water, 011 sea water, 101 coastal） |
| QF3 | 0–7 | bad M1, M2, M3, M4, M5, M7, M8, M10 SDR data（bit 0 = M1 … bit 7 = M10） |
| QF4 | 7 / 6 / 5 / 4 | missing PW / invalid land AM / missing AOT / overall quality of AOT（0 good, 1 bad） |
| QF4 | 3 / 2 / 1 / 0 | bad I3 / I2 / I1 / M11 SDR data |
| QF5 | 7–2 | overall quality M7, M5, M4, M3, M2, M1 SR data（0 good, 1 bad） |
| QF5 | 1 / 0 | missing SP / OZ input data |
| QF6 | 5 / 4 / 3 | overall quality I3 / I2 / I1 SR data |
| QF6 | 2, 2, 0 | 原文では「2 M11」「2 M10」「0 M8」と記載（bit 1 の記載なし。UNRESOLVED） |
| QF7 | 4 | thin cirrus flag |
| QF7 | 2–3 | aerosol quantity（00 climatology, 01 low, 10 average, 11 high） |
| QF7 | 1 / 0 | adjacent to cloud / snow present |

downloader は QF1〜QF7 と land_water_mask を raw の整数のまま保持し、雲画素の SR を NaN にしません。

### 3.3 Geolocation（VNP03IMG/MOD, VJ103IMG/MOD, VJ203IMG/MOD）

- 形式: **NetCDF4（HDF5）**、拡張子 `.nc`。CF-1.6
- IMG（375 m）は SR の I バンドと、MOD（750 m）は SR の M バンドと同じ形状
- global attribute: `OrbitNumber`, `StartTime`/`EndTime`, `startDirection`/`endDirection`（Ascending）, `number_of_filled_scans`, `InputPointer`（L1 の VNP01 等）

| dataset | 型 | scale / offset | _FillValue | 備考 |
|---|---|---|---|---|
| `/geolocation_data/latitude`, `/longitude` | float32 | — | −999.9 | terrain-corrected、画素中心 |
| `/geolocation_data/sensor_zenith`, `/sensor_azimuth` | int16 | 0.01 / 0 | −32768 | 度 |
| `/geolocation_data/solar_zenith`, `/solar_azimuth` | int16 | 0.01 / 0 | −32768 | 度 |
| `/geolocation_data/height` | int16 | 1 / 0 | −32768 | m |
| `/geolocation_data/range` | int16 | 100 / 800000 | −32768 | m |
| `/geolocation_data/land_water_mask` | uint8 | — | 255 | SR と同じクラス定義（IMG は 375 m） |
| `/geolocation_data/quality_flag` | uint8 | — | — | 1 Input_invalid, 2 Pointing_bad, 4 Terrain_bad, 8 SolarAngle_bad |
| `/scan_line_attributes/scan_start_time` ほか | float64 | — | −999.9 | スキャンごとの時刻（TAI93）、`scan_quality` |

監査した granule では、geolocation の緯度経度に fill はありませんでした（bow-tie で削除された画素にも位置が入っています）。

## 4. 実データの性質

### 4.1 bow-tie 削除（on-board deletion）

5 本の SR すべてで、**SR の fill は、次の削除ゾーンと画素単位で完全に一致**しました
（ゾーン外の fill 0、ゾーン内で fill でない画素 0、バンドによって fill だったりなかったりする画素 0）。

| 格子 | スキャン内の行 | 列（fill） |
|---|---|---|
| I（32 行 / scan） | 0, 1, 30, 31 | < 2016 または ≥ 4384 |
| I | 2, 3, 28, 29 | < 1280 または ≥ 5120 |
| M（16 行 / scan） | 0, 15 | < 1008 または ≥ 2192 |
| M | 1, 14 | < 640 または ≥ 2560 |

- 削除された画素は geolocation 側には位置を持つため、有効画素の判定は geolocation ではなく SR の fill で行う必要があります
- 削除された画素の地表は、隣のスキャンの重複部分で観測されています。swath → grid の配置では source から除外し、fill を補間しません
- 昼間の granule では、fill はすべて bow-tie 削除でした。夜間など他の理由の fill は今回観測していません

### 4.2 I と M の幾何関係

swath 上で、2×2 の I 画素中心の平均と M 画素中心の差は、中央値 0.8 m、P99 1.8 m、最大 3.5 m でした（NOAA-20、1,034 万点）。
swath の格子では I と M は 2:1 で入れ子になっています。

### 4.3 阿蘇での実際の画素間隔（geolocation の隣接画素中心の距離、中央値）

| platform / orbit | センサー天頂角 | I（scan × track） | M（scan × track） |
|---|---|---|---|
| SNPP 64082 | 41–44° | 455 × 483 m | 911 × 967 m |
| NOAA-20 32737 | 34–37° | 528 × 442 m | 1058 × 885 m |
| NOAA-21 6892 | 62–63° | 503 × 681 m | 1010 × 1362 m |
| NOAA-21 6893 | 59–60° | 423 × 640 m | 849 × 1280 m |

阿蘇ではどの観測も画素の実寸が 375 m / 750 m より大きく、375 m grid への nearest-neighbour 配置では source 画素が複数 cell に複製されます。

## 5. 固定解析 grid の提案

### 5.1 CRS の比較（阿蘇 131.08°E, 33.03°N で 375 m × 375 m のセルが地表で占める形）

| 候補 | 地表でのセル形状 | 評価 |
|---|---|---|
| MODIS/SGLI と同型の sinusoidal（lon_0 = 0） | 375.8 m × 599.6 m の平行四辺形、内角 38.6° | 面積は保存されるが強くせん断される。不採用 |
| **UTM zone 52N（EPSG:32652, WGS84）** | 375.0 m × 375.0 m の正方形、面積比 1.000 | **推奨** |
| 阿蘇中心の LAEA | 375.0 m × 375.0 m の正方形 | 独自 CRS になる |
| 緯度経度 1/300° | 311 m × 370 m | 正方形でない |

### 5.2 提案する grid の定義

- CRS: **EPSG:32652（WGS 84 / UTM zone 52N）**。全日付・全 platform で共通
- 原点: UTM の false origin（E = 0 m, N = 0 m）に固定。granule の範囲から計算しない
- 375 m grid: セルの辺は E, N とも 375 m の整数倍。セル (r, c) の範囲は x ∈ [x0 + 375c, x0 + 375(c+1))、y ∈ (y1 − 375(r+1), y1 − 375r]（pixel is area）、中心はその中点
- 750 m grid: 辺は 750 m の整数倍
- AOI の窓: AOI bbox（+ halo）を UTM へ変換し、**外側へ 750 m の倍数にスナップ**。375 m の窓は同じ範囲を 2 倍の行数・列数で表す
- 結果として、750 m の cell (i, j) は 375 m の cell (2i..2i+1, 2j..2j+1) と幾何学的に完全一致します。
  浮動小数の許容誤差ではなく、すべての座標が 375 の整数倍（float64 で厳密に表現可能）であることで保証されます

PlanetScope のオルソ画像も通常 UTM（WGS84）で配布されるため、教師ラベルの集計を再投影なしで行える利点があります
（使用する教師データの CRS は未確認。下記 UNRESOLVED 参照）。

### 5.3 2:1 alignment の監査結果

3 つの overpass の阿蘇 AOI 窓（UTM 52N, x 669750–719250 m, y 3636000–3675750 m）で:

| 項目 | 値 |
|---|---|
| 375 m 形状 / 750 m 形状 | 106 × 132 / 53 × 66（厳密に 2 倍） |
| origin alignment error | 0.0 m（x, y とも） |
| pixel edge alignment error（全 750 m 列・行） | 0.0 m |
| 750 m 中心 − 2×2 の 375 m 中心の平均 | 0.0 m |
| 原点の UTM (0, 0) からのずれ（x0 mod 750, y1 mod 750） | 0.0 m, 0.0 m |

## 6. swath → grid の配置（Phase 0 試作）

### 6.1 方法（baseline: nearest-neighbour）

1. pairing を確認した SR と geolocation を読み、AOI 周辺（±0.4°）の画素だけを取り出す
2. 画素中心の緯度経度を PROJ で UTM 52N に変換する
3. bow-tie 削除画素（4.1 の SR fill）を source から除外する
4. 同一 orbit の全 granule の source 画素で 1 つの KD-tree を作る（same-orbit mosaic を兼ねる）
5. 各 target cell の中心から最も近い source 画素中心を選ぶ。採用は距離 ≤ その source 画素の局所的な半対角
   （0.5 × √(scan 間隔² + track 間隔²)）の場合のみ。超えた cell は観測なし（swath 外）
6. 反射率・QA・geometry・時刻はすべて同じ source 画素から取る（QA は nearest のみ。bilinear / cubic は使わない）

nearest-neighbour は面積を保存する処理ではありません。footprint を考慮した面積加重の配置と比較できるよう、
source の行・列・granule・距離を cell ごとに保持する設計にします。

### 6.2 結果（阿蘇 AOI 窓）

| overpass | 格子 | 配置できた割合 | 有効 SR | NN 距離 中央値 / P99 / 最大 | cell あたりの source 中心数（0 / 1 / 2 / 3） | 同距離のタイ |
|---|---|---|---|---|---|---|
| SNPP 64082 | 375 m | 99.99% | 99.99% | 179 / 310 / 353 m | 4501 / 8795 / 695 / 1 | 0 |
| SNPP 64082 | 750 m | 100% | 100% | 362 / 619 / 671 m | 1164 / 2121 / 213 / 0 | 0 |
| NOAA-20 32737 | 375 m | 99.99% | 99.99% | 186 / 328 / 370 m | 5121 / 8050 / 821 / 0 | 0 |
| NOAA-20 32737 | 750 m | 100% | 100% | 365 / 664 / 722 m | 1224 / 2126 / 148 / 0 | 0 |
| NOAA-21 6892 | 375 m | 99.99% | 99.99% | 206 / 391 / 494 m | 6725 / 6712 / 553 / 2 | 0 |
| NOAA-21 6893 | 375 m | 99.98% | 99.98% | 191 / 357 / 452 m | 5614 / 7209 / 1154 / 15 | 0 |

- 未配置の cell（0.01〜0.02%）は距離の上限を超えた AOI 窓の端の cell です
- 雲（QF1 bits 2–3 が confident clear の割合、750 m）: SNPP 98.9%、NOAA-20 98.7%、NOAA-21 98.8% / 97.9%。この日は晴天で、雲を多く含むケースは未検証です
- 375 m の cell に選ばれた I 画素が、親の 750 m cell に選ばれた M 画素の子（swath 上で行・列を 2 で割ったものが一致）になっている割合:
  SNPP 61.0% / NOAA-20 60.3% / NOAA-21 61.4%・57.5%。375 m と 750 m で独立に nearest を取ると、約 4 割の cell で I と M が swath 上の隣接する別画素から来ます（UNRESOLVED 参照）

### 6.3 同一 orbit の mosaic（granule 境界）

同一 platform・同一 orbit の granule は、grid へ配置する前に source 画素を合わせて 1 つの KD-tree にします。
重複部分では距離が最も近い source が選ばれ、書き込み順には依存しません。
距離が完全に同じ場合は、センサー天頂角が小さいほう → スキャン時刻が早いほうを採る規則とします（今回の試作で同距離のタイは 0 件）。
この日は AOI が granule 境界をまたがないため、境界での動作は実データでは未検証です。

### 6.4 日次の best observation（派生物）

overpass ごとの出力を必ず保存し、日次の選択はその派生物とします。数値の平均・中央値による合成は行いません。
試作の規則（750 m cell ごと、決定的）:

1. SR が有効（fill でない）
2. QF1 の cloud confidence が低い（confident clear を優先）
3. cloud shadow がない（QF2 bit 3）
4. センサー天頂角が小さい
5. 取得時刻が早い

375 m の cell は親の 750 m cell と同じ overpass を使います（I と M を同じ観測からそろえるため）。
選んだ overpass の orbit・時刻を cell ごとに記録します。
NASA の L2G の日次選択（observation coverage と quality による）を参考にしていますが、同じアルゴリズムを再現したものではありません。

## 7. 旧 L2G（GA）との比較（同日・同 platform）

GA の値は、提案 grid の 750 m cell 中心を含む GA の 1 km 層の画素から取りました。
GA の `orbit_pnt_1` は、ルート属性 `OrbitNumber.1..N`（互いに異なる orbit、0 始まり）を指します
（granule ごとの `OrbitNumberArray` ではありません）。

| platform | 候補 orbit | 自作の選択 | GA の選択 | orbit 一致率 | センサー天頂角の差（中央値） | cloud confidence 一致率 | M7 相関 | M7 平均（自作 / GA） |
|---|---|---|---|---|---|---|---|---|
| SNPP | 64082 | 64082（全 cell） | 64082 | 100% | 0.00° | 99.1% | 0.849 | 0.1664 / 0.1662 |
| NOAA-20 | 32737 | 32737（全 cell） | 32737 | 100% | 0.00° | 98.7% | 0.828 | 0.2363 / 0.2360 |
| NOAA-21 | 6892, 6893 | 6893: 3,433 / 6892: 65 | 6893: 3,267 / 6892: 231 | 92.7% | 0.01° | 98.7% | 0.728 | 0.2601 / 0.2554 |

- 自作の日次選択は、GA と同じ orbit（NOAA-21 では天頂角の小さい 05:00 の orbit）を主に選んでいます。
  NOAA-21 の約 7% の不一致は、GA が observation coverage を優先するためと考えられます
- 相関が 0.73〜0.85 にとどまるのは、格子（750 m UTM と 926 m sinusoidal）の違いによる空間的な不一致を含むためです。
  値の画素単位の一致を求める比較ではありません
- GA の範囲（h28v05 / h29v05）は AOI 窓を 100% カバーしており、自作の配置も 100% でした

## 8. 位置精度の独立検証（沿岸 box）

UTM 375 m grid に配置した I2（近赤外では水が暗い）と land_water_mask を、平行移動しながら独立の参照と比べ、
相関が最大になるずれ（25 m 刻み、±750 m）を求めました。

| platform | 参照 | 最適なずれ | 375 m 画素に対する比 | 相関（ずれ 0 → 最適） |
|---|---|---|---|---|
| NOAA-20 | SGLI Land_water_flag（陸地率） vs VIIRS I2 | 50 m | 0.13 | 0.9330 → 0.9331 |
| NOAA-20 | SGLI Land_water_flag vs VIIRS land_water_mask | 56 m | 0.15 | 0.9675 → 0.9677 |
| NOAA-20 | NASA GA I2 vs VIIRS I2 | 25 m | 0.07 | 0.9729 → 0.9730 |
| SNPP | SGLI Land_water_flag vs VIIRS I2 | 75 m（−x） | 0.20 | 0.8868 → 0.8875 |
| SNPP | SGLI Land_water_flag vs VIIRS land_water_mask | 75 m（+x） | 0.20 | 0.9674 → 0.9678 |
| SNPP | NASA GA I2 vs VIIRS I2 | 35 m | 0.09 | 0.9667 → 0.9672 |

SGLI の Land_water_flag は、JAXA の GeoTIFF ツールとの照合で位置の正しさを確認済みの独立データです
（docs/gcomc_rsrf_smoke_test.md 第7節）。ずれ 0 と最適値で相関はほとんど変わらず、
SNPP では 2 つの指標で向きが逆です。375 m grid と nearest の量子化を超える系統的な位置ずれは検出されませんでした
（この方法の分解能は概ね 0.1〜0.2 px）。

## 9. 未解決事項（UNRESOLVED）と決定事項

決定事項（2026-10-03）:
- VIIRS の既定を `l2_swath` に切り替え。旧方式は `viirs_product: daily_l2g_legacy` で利用可能
- 日次 best observation の正式な選択規則: 1 全 M バンドの SR が有効 2 QF1 の雲の信頼度が低い 3 QF2 の影なし 4 センサー天頂角が小さい 5 取得時刻が早い。
  NASA の L2G（GA）は observation coverage を優先するため、候補が 2 つある日には採用 orbit が GA と 5〜11% の cell で異なる（第11.2節）。
  これは意図したアルゴリズムの差であり、本方式は GA の日次 composite を再現するものではない
- 実観測の source が footprint 内にない cell（swath 最外縁の約 0.25% など）は補間・近傍値・人工値で埋めず、`PARTIAL_OBSERVATION` として記録
- EPSG:32652 は現在の研究対象地域（zone 52 内）用の analysis grid として採用。global な規則ではない。
  grid 定義は `AnalysisGridDefinition`（既定 `KYUSHU_UTM52N_GRID`）として交換可能な構造にしている
- 教師ラベル（PlanetScope 由来）の CRS 確認は downloader の既定切替を妨げないが、burn-fraction の教師データを 375 m grid に変更する前に必ず確認する
- 固定 grid は EPSG:32652、375 m / 750 m とも原点 E = 0, N = 0（第5節の提案どおり）
- I と M は同一 overpass 内でそれぞれ独立に nearest（I → IMG → 375 m、M → MOD → 750 m）。
  swath 上の親子関係（第6.2節の約 6 割）は要件にしない。footprint 整合方式は採用しない（試作もしていない）
- 日次では 750 m cell で選んだ overpass を、その 2×2 の 375 m cell にも使う（別時刻の I と M を混ぜない）
- 750 m → 375 m の 2×2 複製はモデル入力時の前処理でのみ行い、downloader では行わない
- scipy・pyproj を正式な依存に追加

未解決:
1. QF6 の bit 定義の原文に誤記（bit 2 が 2 回、bit 1 の記載なし）。raw のまま保存し、日次選択・有効判定には使わない
2. 阿蘇では source 画素の実寸（423–681 m、swath 端では最大約 800 m）が 375 m より大きく、nearest では同じ source が複数 cell に複製される
   （cell あたり平均 1.35〜2.33 回）。面積加重の配置との比較は今後の課題
3. swath 最外縁（センサー天頂角 約 67°）では、bow-tie 削除画素の位置を隣のスキャンの画素中心が footprint 内で覆いきれず、
   AOI の 0.25% の cell が観測なしになる（第11節）。補間はしない
4. UTM 52N は 126–132°E（zone 52）の範囲で有効。全研究リージョンは zone 52 内だが、他地域へ広げる場合の規則は未決定
5. 教師ラベル（PlanetScope 由来）の CRS は未確認（375 m の教師データ作成前に要確認）
6. 夜間 granule や機器異常など、bow-tie 以外の理由による SR fill は観測していない
7. 日次選択と GA の採用 orbit の不一致（候補が 2 つある日で 5〜11%）。GA は observation coverage を優先していると考えられ、
   本実装はセンサー天頂角を優先する。NASA の L2G アルゴリズムを再現したものではない

## 10. 監査に使ったスクリプトと中間結果

リポジトリ外（`../satellite_image_downloader_viirs_data/phase0/`）に保存しています。

- `scripts/fetch_phase0.py` — CMR の結果からの取得（.part → サイズ照合 → SHA-256 → rename）
- `scripts/audit_structure.py`, `audit_*.txt` — SR / geolocation の全構造・全属性
- `scripts/audit_values.py` — fill・bow-tie・画素間隔・I/M の入れ子
- `scripts/phase0_map.py` — swath → UTM 375/750 m grid の nearest 配置（試作）
- `scripts/ga_compare.py` — 日次選択の試作と GA との比較
- `scripts/geoloc_check.py` — 位置精度の独立検証
- `scripts/qa_definitions.txt` — QF1〜QF7 の原文
- `VNP_2024070.*`, `VJ1_2024070.*`, `VJ2_2024070.*`（AOI）、`*_coast.*`（沿岸 box）— 配置結果と診断値

---

## 11. 本実装の統合テスト監査（2026-10-03、default 切替前）

本番コード（`src/viirs_l2.py`）を `run_pipeline`（`viirs_product: l2_swath`）経由で実データに対して実行しました。
SR は CMR 検索から、geolocation は SR の `InputPointer` に記録された名前で CMR を検索して取得しています（実ネットワーク）。
その後、raw キャッシュのみ（ネットワーク遮断）で再処理し、以下を集計しました。AOI は `config/no5.geojson`（阿蘇）+ 10 km halo。

### 11.1 ケースと overpass ごとの結果

| ケース | platform / 日付 | orbit（開始 UTC） | granule | AOI 状態 | 天頂角（中央値） | 雲（QF1 confident cloudy） | 375 m 配置 | NN 距離 375 m 中央値 / P99 / 最大 |
|---|---|---|---|---|---|---|---|---|
| granule 境界・雲 | NOAA-20 2024-02-21 | 32480（03:36） | 0336 | OBSERVED | 54.1° | 100% | 13992 / 13992 | 169 / 311 / 358 m |
| 〃 | 〃 | 32481（05:12） | **0512 + 0518** | PARTIAL（0.9975） | 67.0° | 100% | 13969 / 13992 | 248 / 479 / 772 m |
| swath 外 | NOAA-20 2024-02-21 | 32481 | 0512 のみ | **AOI_OUTSIDE_SWATH**（出力なし） | — | — | — | — |
| 雲・複数候補 | NOAA-21 2024-02-23 | 6665（03:18） | 0318 | OBSERVED | 62.6° | 100% | 13991 / 13992 | 209 / 396 / 534 m |
| 〃 | 〃 | 6666（05:00） | 0500 | OBSERVED | 59.4° | 100% | 13990 / 13992 | 190 / 359 / 510 m |
| 複数候補 | NOAA-21 2024-03-10 | 6892（03:18） | 0318 | OBSERVED | 62.4° | 0.8% | 13990 / 13992 | 206 / 391 / 494 m |
| 〃 | 〃 | 6893（05:00） | 0500 | OBSERVED | 59.7° | 1.7% | 13989 / 13992 | 191 / 357 / 452 m |
| 単一 | SNPP 2024-03-10 | 64082（03:48） | 0348 | OBSERVED | 42.5° | 0.9% | 13991 / 13992 | 179 / 310 / 353 m |
| 単一 | NOAA-20 2024-03-10 | 32737（04:36） | 0436 | OBSERVED | 35.2° | 1.1% | 13991 / 13992 | 186 / 328 / 370 m |

- **mosaic**: 2024-02-21 の orbit 32481 では、AOI 窓の cell が 0512 と 0518 の両 granule から配置されました（provenance の source_granule = 0, 1）
- **fill / bow-tie**: 全 overpass で bow-tie 削除画素を source から除外（375 m で 412〜6,107 画素）。距離が完全に同じタイは全ケース 0
- **swath 外**: 0512 単独では AOI 内に source が 1 つもなく、`AOI_OUTSIDE_SWATH` として summary にのみ記録され、GeoTIFF は書かれません
- **swath 最外縁の空白**: 32481（天頂角 67°）で AOI の 23 cell（0.25%）が観測なし。19 cell では最も近い geolocation 画素が bow-tie 削除画素で、
  残った画素の中心が footprint の範囲内にない実際の空白です（補間で埋めません）
- **雲**: 2024-02-21 と 2024-02-23 は AOI の全 cell が confident cloudy。雲画素の反射率は NaN にされず、QF1–QF7 は raw のまま保存されます
- **375 / 750 の整合**: 全出力で 375 m と 750 m の原点が一致し、x0 mod 750 = y1 mod 750 = 0、画素 375.0 / 750.0 m
- **出所の記録**: 全 cell に source granule、swath の行・列、スキャン時刻、source_distance_m が記録されています

### 11.2 日次 best observation と旧 GA（L2G）との比較

| ケース | 日次の選択（cell 数） | GA の選択（cell 数） | orbit 一致率 | 天頂角の差（同じ orbit の cell、中央値） | 雲判定一致率 | M7 相関 | M7 平均（本実装 / GA） |
|---|---|---|---|---|---|---|---|
| NOAA-20 2024-02-21 | 32480: 3498 | 32480: 3331 / 32481: 167 | 95.2% | 0.02° | 100% | 0.938 | 0.730 / 0.725 |
| NOAA-21 2024-02-23 | 6666: 3498 | 6665: 381 / 6666: 3117 | 89.1% | 0.00° | 100% | 0.646 | 0.714 / 0.692 |
| NOAA-21 2024-03-10 | 6892: 171 / 6893: 3327 | 6892: 231 / 6893: 3267 | 89.9% | 0.00° | 98.7% | 0.669 | 0.258 / 0.255 |
| SNPP 2024-03-10 | 64082: 3498 | 64082: 3498 | 100% | 0.00° | 99.1% | 0.849 | 0.166 / 0.166 |
| NOAA-20 2024-03-10 | 32737: 3498 | 32737: 3498 | 100% | 0.00° | 98.7% | 0.828 | 0.236 / 0.236 |

- 全ケースで、日次の 375 m cell は親の 750 m cell と同じ overpass を使っています（I と M が別時刻にならない）
- 日次選択は GA と同じ orbit を主に選び、同じ orbit を選んだ cell ではセンサー天頂角が一致しています（同じ観測であることを示す）
- 相関は格子（UTM 750 m と sinusoidal 926 m）の違いによる空間的な不一致を含みます。値の画素単位の一致を求める比較ではありません
- GA は I バンドを約 463 m、M バンドを約 926 m の格子に格納しますが、本実装は 375 m / 750 m の grid に配置しています

### 11.3 テスト

- 単体テスト（`tests/test_viirs_l2.py`、ネットワーク遮断）: 18 件
- 実データ統合テスト（`tests/test_viirs_l2_integration.py`、`VIIRS_L2_RAW_DIR` 指定時のみ、ネットワーク遮断）: 8 件
  （granule 境界の mosaic、swath 外、雲、複数 overpass と日次の一貫性、GA との比較 4 ケース）
