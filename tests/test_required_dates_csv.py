"""config/viirs_required_dates_daily.csv: the record RUN3_DOWNLOAD_PLAN was moved from.

It is no longer the plan (src/viirs_plan.RUN3_DOWNLOAD_PLAN is); it is kept unchanged, byte-identical to
the research's viirs_burning/deep_learning/data/manifests/viirs_required_dates_daily.csv, to cross-check.
"""
from __future__ import annotations

import csv
from collections import Counter
from datetime import datetime
from pathlib import Path

from src import viirs_plan

CSV_PATH = Path(__file__).resolve().parents[1] / "config" / "viirs_required_dates_daily.csv"

EXPECTED_REGIONS = {
    "region01", "region03", "region04", "region05",
    "region06", "region07", "region08", "region09",
}


def _load_rows():
    with CSV_PATH.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def test_csv_exists():
    assert CSV_PATH.is_file()


def test_schema_is_region_date():
    rows = _load_rows()
    with CSV_PATH.open(newline="", encoding="utf-8") as f:
        fieldnames = csv.DictReader(f).fieldnames
    assert fieldnames == ["region", "date"]
    assert len(rows) == 141


def test_regions_are_exactly_the_expected_eight():
    rows = _load_rows()
    regions = {row["region"] for row in rows}
    assert regions == EXPECTED_REGIONS
    assert "region02" not in regions
    assert "region10" not in regions


def test_no_duplicate_region_date_pairs():
    rows = _load_rows()
    pairs = [(row["region"], row["date"]) for row in rows]
    dupes = {pair: count for pair, count in Counter(pairs).items() if count > 1}
    assert dupes == {}


def test_all_dates_are_valid_and_within_2024_study_period():
    rows = _load_rows()
    dates = [datetime.strptime(row["date"], "%Y-%m-%d").date() for row in rows]
    assert min(dates).year == 2024
    assert max(dates).year == 2024
    assert min(dates).isoformat() == "2024-02-08"
    assert max(dates).isoformat() == "2024-04-10"


def test_per_region_task_counts_sum_to_141():
    rows = _load_rows()
    counts = Counter(row["region"] for row in rows)
    assert sum(counts.values()) == 141
    assert set(counts) == EXPECTED_REGIONS


def test_expected_platform_region_date_task_total_is_423():
    rows = _load_rows()
    platforms = ("snpp", "noaa20", "noaa21")
    assert len(rows) * len(platforms) == 423


def test_csv_record_equals_the_python_plan():
    rows = _load_rows()
    assert [(r["region"], r["date"]) for r in rows] == [
        (region, d) for region in sorted(viirs_plan.RUN3_DOWNLOAD_PLAN) for d in viirs_plan.RUN3_DOWNLOAD_PLAN[region]["snpp"]]
