# 研究用データの取得と再生成（run2.py / run3.py / run4.py / run_activefire.py）

焼失面積の深層学習研究（Biomass_Burning_Sentinel-2）の入力データを、固定された取得計画どおりに取得・再生成するための手順です。
一般的な使い方（自分の地域・期間でダウンロードする）は [README](../README.md) を参照してください。

## 準備

README のセットアップ（clone、Docker のビルド、`key.env`）に加えて、研究対象地域の AOI を元の PC から同じ名前で `config/` にコピーします（`*.geojson` は Git 管理外）。

| ファイル | 用途 |
|---|---|
| `config/no1.geojson`, `no3`〜`no9.geojson` | 2024 年 TRAIN/DEV |
| `config/no5.geojson`, `config/hitoyoshi.geojson` | 外部 TEST（研究側 `Aso/PlanetScope/<region>/` と同一ファイル） |

## 入口と共通ルール

| 入口 | 衛星・製品 | 仕様書 |
|---|---|---|
| `run2.py` | MODIS MOD09GA / MYD09GA v061（native Sinusoidal 500 m、SR + QC_500m + state_1km） | [modis_research_interface.md](modis_research_interface.md) |
| `run3.py` | VIIRS L2 swath VNP09 / VJ109 / VJ209 c002（固定 375 m / 750 m 格子、QA・観測幾何・provenance 付き） | [viirs_research_interface.md](viirs_research_interface.md) |
| `run4.py` | GCOM-C / SGLI L2 LAND RSRF v3002（native 250 m / 1 km） | [sgli_research_interface.md](sgli_research_interface.md) |
| `run_activefire.py` | FIRMS Active Fire（MODIS・VIIRS） | スクリプト冒頭の docstring |

どの入口も次の 4 モードで動きます。

| モード | 動作 |
|---|---|
| （指定なし）= `--dry-run` | 計画と原データの有無を表示するだけ。ネットワークも書き込みもなし |
| `--download` | 足りない原データだけを取得する |
| `--reproduce` | 原データから画像を生成する |
| `--verify` | 生成した画像を仕様・既存画像と照合する |

- **既存のデータは上書きも削除もしません。** 新しい画像は必ず新しい `--output-root` に作ってください。
- 保存先は引数か環境変数で指定します。

  | 入口 | 原データ | 出力 |
  |---|---|---|
  | run2.py | `--raw-root` / `MODIS_RAW_HDF_ROOT` | `--output-root` / `MODIS_OUTPUT_ROOT` |
  | run3.py | `--raw-root` / `VIIRS_L2_RAW_ROOT` | `--output-root` / `VIIRS_OUTPUT_ROOT` |
  | run4.py | `--raw-root` / `GCOMC_RAW_DIR` | `--output-root` / `GCOMC_OUTPUT_ROOT` |
  | run_activefire.py | — | `--output-root` / `ACTIVEFIRE_OUTPUT_ROOT` |

- 別の場所にある既存の原データは `--existing-root` で読み取り専用で再利用できます（`--download --copy-existing` で原データの保存先へ複製）。
- ディレクトリ構造・CRS・QA・NoData の取り決めは [research_io_contract.md](research_io_contract.md) にあります。

## 2024 年 TRAIN/DEV

`--scope` を付けないときはこの計画です。

| センサー | 取得計画（Python） | 固定 manifest | 規模 |
|---|---|---|---|
| MODIS | `src/modis_plan.py` の `RUN2_DOWNLOAD_PLAN` | `config/modis_run2_acquisitions.json`、範囲は `config/modis_run2_full_scene_manifest.json` | 218 取得 / 88 granule |
| VIIRS | `src/viirs_plan.py` の `RUN3_DOWNLOAD_PLAN` | `config/viirs_run3_acquisitions.json` | 423 タスク（既定は研究で使う 344 タスク `RUN3_RESEARCH_TASKS`） |
| SGLI | `run4.py` | — | 2024-02-20〜04-10 の毎日 × タイル T0528 / T0529 |
| Active Fire | `run_activefire.py` | — | 2024-02-08〜04-10 |

画像の範囲は地域ごとに固定で、AOI 全体 + 周囲の余白（halo）です。MODIS は 10 km（region01 だけ 13 km）、VIIRS は 13 km です。

```bash
# MODIS（計画確認 → 不足分の取得 → 生成 → 正式画像との照合）
docker compose run --rm downloader python3 run2.py --raw-root /host_data/modis_raw
docker compose --env-file key.env run --rm downloader python3 run2.py --download --raw-root /host_data/modis_raw
docker compose run --rm downloader python3 run2.py --reproduce --raw-root /host_data/modis_raw --output-root /host_data/MODIS
docker compose run --rm downloader python3 run2.py --verify --output-root /host_data/MODIS --reference-root <Aso/MODIS>

# VIIRS
docker compose run --rm downloader python3 run3.py --raw-root /host_data/viirs_l2_raw
docker compose --env-file key.env run --rm downloader python3 run3.py --download --raw-root /host_data/viirs_l2_raw
docker compose run --rm downloader python3 run3.py --reproduce --raw-root /host_data/viirs_l2_raw --output-root /host_data/VIIRS

# GCOM-C / SGLI（gcomc プロファイル）
docker compose --env-file key.env --profile gcomc run --rm gcomc python3 run4.py --download --raw-root /gcomc_data/raw
docker compose --env-file key.env --profile gcomc run --rm gcomc python3 run4.py --reproduce --raw-root /gcomc_data/raw --output-root /gcomc_data/GCOMC

# Active Fire
docker compose --env-file key.env run --rm downloader python3 run_activefire.py --download --output-root /host_data/activefire
```

- MODIS の出力先は `<output-root>/<region>/<YYYY>/surface_reflectance/<platform>/{500m,qa}/` です（正式画像と同じ構造）。
- 2026-10-26 までの学習パッチ範囲は `run2.py --reproduce --legacy-patch-extent` でだけ再現できます。

## 外部 TEST（2023 阿蘇・2025 阿蘇・2025 人吉）

`--scope test` を付けると、研究で評価済みの外部 TEST 3 ケースを 2024 年と同じ方法で作ります。
TEST の計画は TRAIN/DEV と別で、dry-run の出力の `scope` 欄で区別できます。

| ケース | 地域 | MODIS（PRE → POST） | VIIRS（SNPP、PRE → POST） |
|---|---|---|---|
| 阿蘇 2023 | `region05` | Terra 2023-01-11 → 2023-03-08 | 2023-01-21 → 2023-03-07 |
| 阿蘇 2025 | `region05` | Aqua 2025-02-14 → 2025-03-22 | 2025-02-27 → 2025-03-22 |
| 人吉 2025 | `hitoyoshi` | Terra 2025-02-25 → 2025-03-20 | 2025-03-07 → 2025-03-22 |
| **合計** | | **6 取得 / HDF 10 本** | **6 タスク / 原ファイル 24 本** |

| センサー | 取得計画（Python） | 固定 manifest |
|---|---|---|
| MODIS | `src/modis_plan.py` の `RUN2_TEST_CASES` / `RUN2_TEST_PLAN` | `config/modis_run2_test_acquisitions.json`（granule）、`config/modis_run2_test_full_scene_manifest.json`（範囲） |
| VIIRS | `src/viirs_plan.py` の `RUN3_TEST_CASES` / `RUN3_TEST_PLAN` | `config/viirs_run3_test_acquisitions.json`（granule・orbit・geolocation の対応） |

```bash
# MODIS TEST（地域を絞るなら --regions region05 / --regions hitoyoshi）
docker compose run --rm downloader python3 run2.py --scope test --raw-root /host_data/modis_raw
docker compose --env-file key.env run --rm downloader python3 run2.py --scope test --download --raw-root /host_data/modis_raw
docker compose run --rm downloader python3 run2.py --scope test --reproduce --raw-root /host_data/modis_raw --output-root /host_data/TEST/MODIS
docker compose run --rm downloader python3 run2.py --scope test --verify --output-root /host_data/TEST/MODIS

# VIIRS TEST（日付を絞るなら --dates 2023-01-21 2023-03-07 など）
docker compose run --rm downloader python3 run3.py --scope test --raw-root /host_data/viirs_l2_raw
docker compose --env-file key.env run --rm downloader python3 run3.py --scope test --download --raw-root /host_data/viirs_l2_raw
docker compose run --rm downloader python3 run3.py --scope test --reproduce --raw-root /host_data/viirs_l2_raw --output-root /host_data/TEST/VIIRS
docker compose run --rm downloader python3 run3.py --scope test --verify --output-root /host_data/TEST/VIIRS
```

既存の TEST 画像と比べる場合は、研究側の `Aso` を読み取り専用でマウントして `--verify` にオプションを足します（既存画像は読むだけです）。

```bash
docker compose run --rm -v <Aso>:/aso:ro downloader python3 run2.py --scope test --verify --output-root /host_data/TEST/MODIS --legacy-reference-root /aso/MODIS
docker compose run --rm -v <Aso>:/aso:ro downloader python3 run3.py --scope test --verify --output-root /host_data/TEST/VIIRS --legacy-l2g-root /aso/VIIRS
```

注意点：

- **範囲**：2024 年と同じ規則です（MODIS は AOI + 10 km、VIIRS は AOI + 13 km）。region05 の範囲は 2024 年の region05 と同じです。
- **MODIS の比較**：既存の TEST 画像は AOI で切り抜いた旧方式なので、既存画像に値がある画素だけを位置合わせして比べます。
  人吉の既存画像は新しい範囲より広い（AOI + 約 18〜20 km）ので、はみ出した部分は比べずに件数だけ報告します。
- **VIIRS の比較**：既存の TEST 画像は旧 Daily L2G（VNP09GA、約 463 / 926 m のタイル格子）で、新しい L2 swath（VNP09、375 / 750 m）とは製品も格子も違います。
  `--legacy-l2g-root` は両者の格子と範囲を並べて記録するだけで、画素の比較や一致の判定はしません。
- MODIS と VIIRS は観測日が違う（共通は region05 2025-03-22 だけ）ため、計画はそれぞれ独立しています。
- 日付の根拠や詳細は [MODIS 仕様書 12 節](modis_research_interface.md) と [VIIRS 仕様書 7 節](viirs_research_interface.md) にあります。
