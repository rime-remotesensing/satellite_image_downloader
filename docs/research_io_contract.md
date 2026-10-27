# 研究側（Biomass_Burning_Sentinel-2）との入出力契約

このリポジトリは、原データの取得・永続保存と、センサーごとの反射率・QA 画像の生成までを担当します。
指数、教師データ、AF4、patch、学習は研究側の担当です。研究側のコードは変更していません。
対応関係は 2026-10-26 時点で、研究リポジトリ（読み取り専用）と照合して確認しました。

## 1. センサーごとの正式製品

| | MODIS（run2.py） | VIIRS（run3.py） | SGLI（run4.py） |
|---|---|---|---|
| 製品・版 | MOD09GA / MYD09GA .061（L2G 日次） | VNP09 / VJ109 / VJ209 c002 L2 swath + 03IMG/03MOD | GCOM-C L2 LAND RSRF v3002、D01D |
| 研究で使うバンド | B1–B7（Aqua B6 は欠測が多い）+ NDVI/NBR（研究側で計算） | Basic9 = I1, I2, M3, M4, M8, I3, M11 + NDVI_01, NBR_01（研究側で計算） | 未定（研究側の `configs/sensors/sgli.json` は `selected_bands: null`） |
| 解像度・格子 | native Sinusoidal 500 m（463.31 m）、state_1km は 926.6 m | 固定の 375 m（I）/ 750 m（M・QF）等積 Sinusoidal grid（2×2 で入れ子） | native EQA 250 m（231.66 m）/ 1 km（926.63 m） |
| CRS | `+proj=sinu +R=6371007.181` | 同左（原点 −20015109.354, 10007554.677。窓は 750 m の倍数） | `+proj=sinu +R=6371007.181`（JAXA EQA の定義） |
| 反射率 | DN × 0.0001、fill → NaN | DN × 9.9999997e-05、−28672 → NaN | DN × Slope + Offset、Error_DN・範囲外 → NaN |
| QA | QC_500m（uint32）、state_1km（uint16）、raw 値・nodata なし | QF1–QF7・land_water_mask（uint8 raw） | QA_flag（uint16）、Land_water_flag（uint8）、Obs_time（int16）、raw 値 |
| 窓 | AOI bbox + halo（10 km、region01 13 km）、地域ごとに固定（manifest で固定）。旧パッチ範囲は legacy | AOI bbox + 13 km（研究側 `halo_m`）、AOI マスクなし | AOI bbox + 13 km、AOI マスクなし |
| 観測時刻 | 日単位のみ（画素ごとの時刻なし） | 画素ごとの scan_time_unix、overpass の開始・終了 UTC | Obs_time（画素ごとの UTC 時） |
| 原データ ↔ 画像の対応 | サイドカーの `source_granules`、`run2_*` レポート（SHA-256） | タグの `source_granules` / `geolocation_granules`、provenance 層、summary、`run3_*` レポート | タグの `source_filenames`、summary の SHA-256、`run4_*` レポート |

詳しい仕様: [modis_research_interface.md](modis_research_interface.md)、[viirs_research_interface.md](viirs_research_interface.md)、[sgli_research_interface.md](sgli_research_interface.md)

## 2. ディレクトリ構造

```
MODIS  <out>/<region>/<YYYY>/surface_reflectance/<platform>/{500m,qa}/...             (= 研究側 Aso/MODIS/<region>/2024/surface_reflectance/ と同じ規約)
VIIRS  <raw>/<product>/<YYYY>/<DOY>/<granule>                                       (研究側 raw_day() が読む raw_root)
       <out>/<region>/viirs/surface_reflectance/<platform>/l2_swath/{overpass,daily,summary}/...
SGLI   <raw>/GCOM-C/SGLI/L2.LAND.RSRF/3002/YYYY/MM/DD/<file>.h5
       <out>/<region>/gcomc/rsrf/{250m,1km,qa/<field>,summary}/...
AF     <af>/<region>/modis/activefire/ACFR_*.shp      → 研究側 Aso/MODIS/<region>/2024/activefire_halo/
       <af>/<region>/<platform>/viirs/activefire/ACFR_*.shp → 研究側 Aso/VIIRS/<region>/2024/activefire_halo/<platform>/viirs/activefire/
```

## 3. 研究側が必要とする入力と、現在の出力との差分

| センサー | 研究側が読むもの | 現在の出力 | 差分・未対応 |
|---|---|---|---|
| MODIS | `Aso/MODIS/<region>/2024/surface_reflectance/` の 500m / QC_500m / state_1km（AOI + halo 版、2026-10-27 採用） | run2.py の通常出力 | **なし**（新正式 654 TIFF と一致を確認） |
| VIIRS（L2 swath） | 原 SR / IMG / MOD（`raw_root`）→ 研究側の `raw_day()` で Basic9 scene を作る | 原データ（manifest）と、同じアルゴリズムで作った daily 375/750 m 画像 | daily 画像は研究 scene の物理 7 バンド（M は 2×2 複製）・valid mask と**画素単位で一致**（attempt02 の 7 scene すべて）。<br>指数（NDVI_01 / NBR_01）と 750→375 の複製は研究側の処理<br>研究側の pair 設定の `raw_root` はリージョンごとの既存 `_raw_l2` を指している。run3 の共有 raw root（重複なし）を使う場合は、研究側設定の `raw_root` を差し替える必要がある |
| VIIRS（旧 L2G） | `viirs_burning/config/default.yaml` の `surface_reflectance_halo/<platform>/500m/VJ109GA_*`（旧 manifest） | **run3.py では生成しない**（旧方式。375 m 入力と同一視しない） | 旧系統を再生成するには `run.py` の `daily_l2g_legacy` を使う |
| SGLI | 研究側にはまだ reader の枠組みしかない（バンド選択・QA 方針・格子は未定） | native 250 m（12 バンド）/ 1 km（3 バンド）+ raw QA | 研究側で、使うバンド、250 m と 1 km の統合方法、SW02（TOA）の扱いを決める必要がある。**この PC には SGLI の原 HDF5 がなく、実データでの再生成は今回未検証** |
| Active Fire | `EVENT_CATALOG.json`（956 イベント。上記 AF の shapefile から作成） | run_activefire.py が同じ shapefile 規約で出力 | 研究側の source shapefile 384 本が、run_activefire の schema / CRS / 期間 / platform / bbox / SP・NRT 判定をすべて満たす（読み取り専用で確認）。<br>研究側の配置先（`activefire_halo`）へのコピーは研究側の作業 |

## 4. 守っている原則

- 日付・granule を推定や代用で補うことはしません。不足は `MISSING` / `NO_GRANULE` / `NO_RECORD` / `RAW_MISSING` として明示します。
- 観測なし（AOI_NO_DATA、swath 外）と取得失敗（ERROR / REQUEST_REQUIRED / DOWNLOAD_FAILED）を区別します。
- 原データは削除しません。既存の画像は上書きしません（`ALREADY_PRESENT` / `PARTIAL_OUTPUT_PRESENT` として報告します）。
- 認証情報（key.env、G-Portal の鍵）は、コード・manifest・レポートのどこにも書き出しません。
