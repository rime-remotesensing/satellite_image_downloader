# GCOM-C/SGLI 取得・出力仕様（run4.py、Biomass_Burning_Sentinel-2 連携）

`run4.py` は GCOM-C/SGLI 専用の正式な入口です。旧 `run4.py`（VIIRS Active Fire）の処理は `run_activefire.py` へ移しました。
取得・検証・画像生成はすべて既存の `src/gcomc.py` が行い、`run4.py` は計画・状態の分類・no-overwrite・検証・manifest を担当します。
実データの監査記録は [gcomc_rsrf_smoke_test.md](gcomc_rsrf_smoke_test.md) にあります。

## 1. 製品

| 項目 | 値 |
|---|---|
| 製品 | GCOM-C/SGLI **L2 LAND RSRF**（大気補正済み陸域反射率）、Product_version **3002** |
| 配布 | JAXA G-Portal: CSW `https://csw.gportal.jaxa.jp/csw`（datasetId 10002015）→ SFTP `sftp.gportal.jaxa.jp`（公開鍵認証） |
| SFTP パス | `/product/Standard/GCOM-C/GCOM-C.SGLI/L2.LAND.RSRF/3/YYYY/MM/DD/<file>` |
| 軌道 | **descending（D01D、昼間）のみ**。ascending は日本域で全画素が Error_DN です |
| タイル | EQA 10° タイル。研究リージョン（+13 km halo）では **T0528 + T0529**。region09 は T0529 だけに入ります |
| ファイル名 | `GC1SG1_<YYYYMMDD>D01D_T<vv><hh>_L2SG_RSRFQ_3002.h5`（日付とタイルから一意に決まります） |

タイルは EQA タイル格子から厳密に求めます。CSW の bbox 検索ではタイルの曲線境界を表せず、阿蘇周辺の T0529 を取りこぼすため、使いません。

## 2. バンド・解像度

| ファイル | バンド（順序固定） | 格子 | dtype | 値 |
|---|---|---|---|---|
| `250m/…_250m.tif` | VN01, VN02, …, VN11, SW03（12） | native 250 m（1/480°、231.656 m） | float32 | 反射率 = DN × Slope + Offset。Error_DN（65535）と範囲外の値は NaN |
| `1km/…_1km.tif` | SW01, **SW02**, SW04（3） | native 1 km（1/120°、926.625 m） | float32 | 同上。**SW02 は TOA reflectance**（バンドタグ `reflectance_type`）、他は surface reflectance |
| `qa/QA_flag/…` | QA_flag | 250 m | uint16 | raw bit（nodata = 1 = bit0 no data） |
| `qa/Land_water_flag/…` | Land_water_flag | 250 m | uint8 | 0 = water … 100 = land（nodata 255） |
| `qa/Obs_time/…` | Obs_time | 250 m | int16 | 観測時刻の raw 値（× 0.001 = UTC 時。nodata −32768） |

- 学習入力の SWIR 候補は **SW03（250 m 画像の band 12）と SW04（1 km 画像の band 3）**。SW01・SW02（1 km 画像の band 1・2）は学習入力の対象外（原データとの対応を保つため画像には残す）。
- 1 km バンドを 250 m へ upsample することはありません。250 m と 1 km は別ファイル・別格子のままにします。1 km の窓は 250 m の窓を包含します。
- QA bit: 0 no data / 1 land / 2 coast / 3–4 sunglint / 5 snow・ice / 6 cloud / 7 probably cloud / 8 high tau-a / 9 saturation recovery / 10 BRF samples≤3 / 11 straylight / 12 shadow / 13 pol cloud / 14–15 recovery
- 各バンドのタグに、HDF5 のパス、Slope、Offset、Error_DN、有効範囲、中心波長、reflectance_type を記録します。サイドカー JSON にも同じ内容があります。

## 3. 格子・CRS

- CRS: `+proj=sinu +lon_0=0 +R=6371007.181`（JAXA の GeoTIFF 定義の pixel scale から導いた値）。affine は JAXA 公式ツールの画素中心定義を 1e-10 px 未満の誤差で再現します（監査記録 7 節）。
- 窓: AOI の bbox に 13 km を足し、native 画素へ外側にスナップした矩形です（`native_rect_halo`）。**AOI ポリゴンによるマスクはしません。** 再投影・リサンプリングもしません。

## 4. 日付と観測状態

対象期間 2024-02-20 〜 2024-04-10 の **全 51 日** を処理します。MODIS や VIIRS の取得日には限定しません。リージョン・日ごとに次の状態を区別して記録します。

| 状態 | 意味 |
|---|---|
| `OBSERVED` / `PARTIAL_OBSERVATION` | AOI に観測があった（QA bit0 で判定）。`observation.label` = `USABLE`（晴天率 ≥ 0.5）／`CLOUDY` |
| `AOI_NO_DATA` | 原ファイルはあるが、その日の swath 外（画像は出力せず summary だけ残す）。label = `NO_OBSERVATION` |
| `NO_RECORD` | CSW に record がない |
| `RAW_MISSING` | record はあるが未取得（原ファイルがない）。**観測なしとは区別します** |
| `REQUEST_REQUIRED` / `ERROR` | 取得できなかった（2.5 年ルール、または認証・ネットワーク・破損）。観測なしとは扱いません |

晴天率は QA bit 6・7・12 が立っておらず、全 250 m バンドが有効な AOI 画素の割合です（`src/gcomc_inventory.region_day_stats`）。
閾値 0.5 は研究側の `min_clear_fraction` に合わせた参考ラベルで、画素を除外する処理ではありません。

## 5. 原データの保存と再利用

- 保存先: `<GCOMC_RAW_DIR>/GCOM-C/SGLI/L2.LAND.RSRF/3002/YYYY/MM/DD/<file>`
- 手順: `.part` に保存 → SFTP の stat サイズと照合 → HDF5 のスキーマ検証 → atomic rename。有効なファイルが手元にあれば再取得しません。
- 検証に失敗した既存ファイルには触れません（`ERROR` / `RAW_INVALID` として報告します）。
- `--reproduce` は手元の原データだけを使います（ネットワークは使いません）。タイルが 1 枚でも欠けている日は `RAW_MISSING` とし、部分的な mosaic は作りません。

## 6. 出力構造

正式配置（2026-10-27〜。研究側 `GCOMC/regionXX/2024/surface_reflectance/`）:

```
<out>/<region>/<YYYY>/surface_reflectance/250m/GCOMC_SGLI_RSRF_<YYYYMMDD>_D_250m.tif (+ .json)
<out>/<region>/<YYYY>/surface_reflectance/1km/GCOMC_SGLI_RSRF_<YYYYMMDD>_D_1km.tif   (+ .json)
<out>/<region>/<YYYY>/surface_reflectance/qa/{QA_flag,Land_water_flag,Obs_time}/GCOMC_SGLI_RSRF_<YYYYMMDD>_D_<field>.tif
<out>/<region>/<YYYY>/surface_reflectance/summary/GCOMC_SGLI_RSRF_<YYYYMMDD>_D.json
     (タイルごとの原ファイル名・SHA-256・状態、AOI 状態、観測率・晴天率・観測時刻、バンドごとの有効率)
<out>/_run4_reports/run4_<mode>_<UTC>.json
```

## 7. コマンド

```bash
python run4.py --raw-root <GCOMC_RAW_DIR> [--verify-raw]                 # dry-run: 日付 × タイルの原データの有無
python run4.py --download --raw-root <GCOMC_RAW_DIR>                    # CSW + SFTP（G-Portal 鍵が必要）
python run4.py --reproduce --raw-root <GCOMC_RAW_DIR> --output-root <OUT>
python run4.py --verify --output-root <OUT> --raw-root <GCOMC_RAW_DIR> [--reference-root <別の出力>]
```

Docker では `gcomc` プロファイルを使います（G-Portal の鍵を読み取り専用でマウント）:
`docker compose --env-file key.env --profile gcomc run --rm gcomc python3 run4.py ...`
