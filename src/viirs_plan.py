"""The VIIRS L2 swath acquisition plan and its frozen manifest.

Two layers, like src/modis_plan.py:

* ``RUN3_DOWNLOAD_PLAN`` -- the reviewed literal list of 141 (region, date)
  rows chosen by the research PRE/POST daily-pair audit (127 region-dates of
  the 7 training regions + 14 of region04), each acquired for all 3 platforms
  (423 tasks). ``RUN3_RESEARCH_TASKS`` is the 344-task subset the research
  uses. Both were moved verbatim from config/viirs_required_dates_daily.csv and
  config/viirs_research_required_tasks.csv, which are kept only as records to
  cross-check against (identical to the research's own copies).
* config/viirs_run3_acquisitions.json -- for each of the 423 (region, platform,
  date) tasks: the exact daytime L2 SR granules (VNP09 / VJ109 / VJ209, collection
  002) per orbit, their exact IMG/MOD geolocation pair (from the SR InputPointer),
  or NO_GRANULE when CMR had no SR granule for that day. Built once from the
  acquisition summaries + raw archive with ``python -m src.viirs_plan --build``.

The external TEST cases follow the same two layers, kept separate from the
2024 plan: ``RUN3_TEST_CASES`` / ``RUN3_TEST_PLAN`` (6 SNPP tasks, region05
2023/2025 + hitoyoshi 2025) and config/viirs_run3_test_acquisitions.json
(``--acquire-test`` then ``--build --scope test``).
"""

from __future__ import annotations

import argparse
import csv
import json
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Set, Tuple

from .regions import FORBIDDEN_REGIONS, REGION_GEOJSONS

REQUIRED_DATES_CSV = Path(__file__).resolve().parents[1] / "config" / "viirs_required_dates_daily.csv"
RESEARCH_REQUIRED_CSV = Path(__file__).resolve().parents[1] / "config" / "viirs_research_required_tasks.csv"
DEFAULT_MANIFEST_PATH = Path(__file__).resolve().parents[1] / "config" / "viirs_run3_acquisitions.json"
MANIFEST_SCHEMA = "viirs-run3-acquisitions-v1"

PLATFORMS: Tuple[str, ...] = ("snpp", "noaa20", "noaa21")
SR_PRODUCT_BY_PLATFORM = {"snpp": "VNP09", "noaa20": "VJ109", "noaa21": "VJ209"}
EXPECTED_REQUIRED_REGION_DATE_TASKS = 141
EXPECTED_PLATFORM_REGION_DATE_TASKS = EXPECTED_REQUIRED_REGION_DATE_TASKS * len(PLATFORMS)
EXPECTED_RESEARCH_REQUIRED = 344

# The research VIIRS L2 swath pipeline (configs/sensors/viirs_pipeline*.json) builds
# its scenes with halo_m = 13000 for every region; using the same value gives the
# identical analysis-grid window (same transform and shape).
DEFAULT_HALO_M = 13_000.0

STATUS_GRANULES = "GRANULES"
STATUS_NO_GRANULE = "NO_GRANULE"

# Do not derive this from a range or a statistical filter. These are the 141
# (region, date) rows of the research PRE/POST daily-pair audit (127 region-dates
# of the 7 training regions + 14 of region04), moved verbatim from
# config/viirs_required_dates_daily.csv; every date is acquired for all 3
# platforms (423 tasks). This is the source of truth for run3.py.
RUN3_DOWNLOAD_PLAN: Dict[str, Dict[str, List[str]]] = {
    "region01": {
        "snpp": [
            "2024-02-12", "2024-02-17", "2024-02-18", "2024-03-02",
            "2024-03-08", "2024-03-10", "2024-03-13", "2024-03-15",
            "2024-03-16", "2024-03-18", "2024-03-22", "2024-03-27",
            "2024-03-29", "2024-04-01", "2024-04-10",
        ],
        "noaa20": [
            "2024-02-12", "2024-02-17", "2024-02-18", "2024-03-02",
            "2024-03-08", "2024-03-10", "2024-03-13", "2024-03-15",
            "2024-03-16", "2024-03-18", "2024-03-22", "2024-03-27",
            "2024-03-29", "2024-04-01", "2024-04-10",
        ],
        "noaa21": [
            "2024-02-12", "2024-02-17", "2024-02-18", "2024-03-02",
            "2024-03-08", "2024-03-10", "2024-03-13", "2024-03-15",
            "2024-03-16", "2024-03-18", "2024-03-22", "2024-03-27",
            "2024-03-29", "2024-04-01", "2024-04-10",
        ],
    },
    "region03": {
        "snpp": [
            "2024-02-12", "2024-02-13", "2024-02-17", "2024-02-18",
            "2024-02-27", "2024-03-08", "2024-03-10", "2024-03-13",
            "2024-03-14", "2024-03-15", "2024-03-16", "2024-03-18",
            "2024-03-22", "2024-03-27", "2024-03-29", "2024-04-01",
            "2024-04-10",
        ],
        "noaa20": [
            "2024-02-12", "2024-02-13", "2024-02-17", "2024-02-18",
            "2024-02-27", "2024-03-08", "2024-03-10", "2024-03-13",
            "2024-03-14", "2024-03-15", "2024-03-16", "2024-03-18",
            "2024-03-22", "2024-03-27", "2024-03-29", "2024-04-01",
            "2024-04-10",
        ],
        "noaa21": [
            "2024-02-12", "2024-02-13", "2024-02-17", "2024-02-18",
            "2024-02-27", "2024-03-08", "2024-03-10", "2024-03-13",
            "2024-03-14", "2024-03-15", "2024-03-16", "2024-03-18",
            "2024-03-22", "2024-03-27", "2024-03-29", "2024-04-01",
            "2024-04-10",
        ],
    },
    "region04": {
        "snpp": [
            "2024-02-12", "2024-02-17", "2024-02-18", "2024-02-27",
            "2024-03-10", "2024-03-14", "2024-03-15", "2024-03-16",
            "2024-03-18", "2024-03-22", "2024-03-27", "2024-03-29",
            "2024-04-01", "2024-04-10",
        ],
        "noaa20": [
            "2024-02-12", "2024-02-17", "2024-02-18", "2024-02-27",
            "2024-03-10", "2024-03-14", "2024-03-15", "2024-03-16",
            "2024-03-18", "2024-03-22", "2024-03-27", "2024-03-29",
            "2024-04-01", "2024-04-10",
        ],
        "noaa21": [
            "2024-02-12", "2024-02-17", "2024-02-18", "2024-02-27",
            "2024-03-10", "2024-03-14", "2024-03-15", "2024-03-16",
            "2024-03-18", "2024-03-22", "2024-03-27", "2024-03-29",
            "2024-04-01", "2024-04-10",
        ],
    },
    "region05": {
        "snpp": [
            "2024-02-11", "2024-02-12", "2024-02-13", "2024-02-16",
            "2024-02-17", "2024-02-18", "2024-02-27", "2024-03-08",
            "2024-03-10", "2024-03-14", "2024-03-15", "2024-03-16",
            "2024-03-18", "2024-03-22", "2024-03-27", "2024-03-29",
            "2024-04-01", "2024-04-09", "2024-04-10",
        ],
        "noaa20": [
            "2024-02-11", "2024-02-12", "2024-02-13", "2024-02-16",
            "2024-02-17", "2024-02-18", "2024-02-27", "2024-03-08",
            "2024-03-10", "2024-03-14", "2024-03-15", "2024-03-16",
            "2024-03-18", "2024-03-22", "2024-03-27", "2024-03-29",
            "2024-04-01", "2024-04-09", "2024-04-10",
        ],
        "noaa21": [
            "2024-02-11", "2024-02-12", "2024-02-13", "2024-02-16",
            "2024-02-17", "2024-02-18", "2024-02-27", "2024-03-08",
            "2024-03-10", "2024-03-14", "2024-03-15", "2024-03-16",
            "2024-03-18", "2024-03-22", "2024-03-27", "2024-03-29",
            "2024-04-01", "2024-04-09", "2024-04-10",
        ],
    },
    "region06": {
        "snpp": [
            "2024-02-12", "2024-02-16", "2024-02-17", "2024-02-18",
            "2024-02-24", "2024-02-27", "2024-03-04", "2024-03-10",
            "2024-03-13", "2024-03-15", "2024-03-16", "2024-03-18",
            "2024-03-22", "2024-03-27", "2024-03-29", "2024-04-01",
            "2024-04-10",
        ],
        "noaa20": [
            "2024-02-12", "2024-02-16", "2024-02-17", "2024-02-18",
            "2024-02-24", "2024-02-27", "2024-03-04", "2024-03-10",
            "2024-03-13", "2024-03-15", "2024-03-16", "2024-03-18",
            "2024-03-22", "2024-03-27", "2024-03-29", "2024-04-01",
            "2024-04-10",
        ],
        "noaa21": [
            "2024-02-12", "2024-02-16", "2024-02-17", "2024-02-18",
            "2024-02-24", "2024-02-27", "2024-03-04", "2024-03-10",
            "2024-03-13", "2024-03-15", "2024-03-16", "2024-03-18",
            "2024-03-22", "2024-03-27", "2024-03-29", "2024-04-01",
            "2024-04-10",
        ],
    },
    "region07": {
        "snpp": [
            "2024-02-12", "2024-02-13", "2024-02-16", "2024-02-17",
            "2024-02-18", "2024-02-24", "2024-02-27", "2024-03-04",
            "2024-03-08", "2024-03-10", "2024-03-13", "2024-03-14",
            "2024-03-15", "2024-03-16", "2024-03-18", "2024-03-22",
            "2024-03-27", "2024-03-29", "2024-04-01", "2024-04-10",
        ],
        "noaa20": [
            "2024-02-12", "2024-02-13", "2024-02-16", "2024-02-17",
            "2024-02-18", "2024-02-24", "2024-02-27", "2024-03-04",
            "2024-03-08", "2024-03-10", "2024-03-13", "2024-03-14",
            "2024-03-15", "2024-03-16", "2024-03-18", "2024-03-22",
            "2024-03-27", "2024-03-29", "2024-04-01", "2024-04-10",
        ],
        "noaa21": [
            "2024-02-12", "2024-02-13", "2024-02-16", "2024-02-17",
            "2024-02-18", "2024-02-24", "2024-02-27", "2024-03-04",
            "2024-03-08", "2024-03-10", "2024-03-13", "2024-03-14",
            "2024-03-15", "2024-03-16", "2024-03-18", "2024-03-22",
            "2024-03-27", "2024-03-29", "2024-04-01", "2024-04-10",
        ],
    },
    "region08": {
        "snpp": [
            "2024-02-12", "2024-02-13", "2024-02-16", "2024-02-17",
            "2024-02-18", "2024-02-24", "2024-02-27", "2024-03-04",
            "2024-03-10", "2024-03-13", "2024-03-14", "2024-03-15",
            "2024-03-16", "2024-03-18", "2024-03-22", "2024-03-27",
            "2024-03-29", "2024-04-01", "2024-04-10",
        ],
        "noaa20": [
            "2024-02-12", "2024-02-13", "2024-02-16", "2024-02-17",
            "2024-02-18", "2024-02-24", "2024-02-27", "2024-03-04",
            "2024-03-10", "2024-03-13", "2024-03-14", "2024-03-15",
            "2024-03-16", "2024-03-18", "2024-03-22", "2024-03-27",
            "2024-03-29", "2024-04-01", "2024-04-10",
        ],
        "noaa21": [
            "2024-02-12", "2024-02-13", "2024-02-16", "2024-02-17",
            "2024-02-18", "2024-02-24", "2024-02-27", "2024-03-04",
            "2024-03-10", "2024-03-13", "2024-03-14", "2024-03-15",
            "2024-03-16", "2024-03-18", "2024-03-22", "2024-03-27",
            "2024-03-29", "2024-04-01", "2024-04-10",
        ],
    },
    "region09": {
        "snpp": [
            "2024-02-08", "2024-02-12", "2024-02-13", "2024-02-16",
            "2024-02-17", "2024-02-18", "2024-02-27", "2024-03-04",
            "2024-03-09", "2024-03-10", "2024-03-13", "2024-03-14",
            "2024-03-15", "2024-03-16", "2024-03-18", "2024-03-22",
            "2024-03-27", "2024-03-29", "2024-04-01", "2024-04-10",
        ],
        "noaa20": [
            "2024-02-08", "2024-02-12", "2024-02-13", "2024-02-16",
            "2024-02-17", "2024-02-18", "2024-02-27", "2024-03-04",
            "2024-03-09", "2024-03-10", "2024-03-13", "2024-03-14",
            "2024-03-15", "2024-03-16", "2024-03-18", "2024-03-22",
            "2024-03-27", "2024-03-29", "2024-04-01", "2024-04-10",
        ],
        "noaa21": [
            "2024-02-08", "2024-02-12", "2024-02-13", "2024-02-16",
            "2024-02-17", "2024-02-18", "2024-02-27", "2024-03-04",
            "2024-03-09", "2024-03-10", "2024-03-13", "2024-03-14",
            "2024-03-15", "2024-03-16", "2024-03-18", "2024-03-22",
            "2024-03-27", "2024-03-29", "2024-04-01", "2024-04-10",
        ],
    },
}

# The 344 (region, platform, date) tasks the research uses as PRE or POST
# (viirs_burning/results/manifests/daily_pair_manifest.csv, 9,248 pairs, 7 regions;
# frozen 2026-10-27), moved verbatim from config/viirs_research_required_tasks.csv.
# region04 is not used by the research. run3.py processes only these by default.
RUN3_RESEARCH_TASKS: Dict[str, Dict[str, List[str]]] = {
    "region01": {
        "snpp": [
            "2024-02-12", "2024-02-17", "2024-02-18", "2024-03-02",
            "2024-03-08", "2024-03-10", "2024-03-13", "2024-03-15",
            "2024-03-16", "2024-03-18", "2024-03-22", "2024-03-27",
            "2024-03-29", "2024-04-01", "2024-04-10",
        ],
        "noaa20": [
            "2024-02-12", "2024-02-17", "2024-02-18", "2024-03-08",
            "2024-03-10", "2024-03-15", "2024-03-16", "2024-03-18",
            "2024-03-22", "2024-03-27", "2024-03-29", "2024-04-01",
            "2024-04-10",
        ],
        "noaa21": [
            "2024-02-12", "2024-02-17", "2024-02-18", "2024-03-08",
            "2024-03-10", "2024-03-13", "2024-03-15", "2024-03-16",
            "2024-03-18", "2024-03-22", "2024-03-27", "2024-03-29",
            "2024-04-01", "2024-04-10",
        ],
    },
    "region03": {
        "snpp": [
            "2024-02-12", "2024-02-13", "2024-02-17", "2024-02-18",
            "2024-02-27", "2024-03-08", "2024-03-10", "2024-03-13",
            "2024-03-14", "2024-03-15", "2024-03-16", "2024-03-18",
            "2024-03-22", "2024-03-27", "2024-03-29", "2024-04-01",
            "2024-04-10",
        ],
        "noaa20": [
            "2024-02-12", "2024-02-13", "2024-02-17", "2024-02-18",
            "2024-02-27", "2024-03-10", "2024-03-14", "2024-03-15",
            "2024-03-16", "2024-03-18", "2024-03-22", "2024-03-27",
            "2024-03-29", "2024-04-01", "2024-04-10",
        ],
        "noaa21": [
            "2024-02-12", "2024-02-13", "2024-02-17", "2024-02-18",
            "2024-03-08", "2024-03-10", "2024-03-13", "2024-03-14",
            "2024-03-15", "2024-03-16", "2024-03-18", "2024-03-22",
            "2024-03-27", "2024-03-29", "2024-04-01", "2024-04-10",
        ],
    },
    "region05": {
        "snpp": [
            "2024-02-11", "2024-02-12", "2024-02-13", "2024-02-16",
            "2024-02-17", "2024-02-18", "2024-02-27", "2024-03-08",
            "2024-03-10", "2024-03-14", "2024-03-15", "2024-03-16",
            "2024-03-18", "2024-03-22", "2024-03-27", "2024-03-29",
            "2024-04-01", "2024-04-09", "2024-04-10",
        ],
        "noaa20": [
            "2024-02-11", "2024-02-12", "2024-02-16", "2024-02-17",
            "2024-02-18", "2024-02-27", "2024-03-08", "2024-03-10",
            "2024-03-14", "2024-03-15", "2024-03-16", "2024-03-18",
            "2024-03-22", "2024-03-27", "2024-03-29", "2024-04-01",
            "2024-04-09", "2024-04-10",
        ],
        "noaa21": [
            "2024-02-11", "2024-02-12", "2024-02-16", "2024-02-17",
            "2024-02-18", "2024-03-08", "2024-03-10", "2024-03-14",
            "2024-03-15", "2024-03-16", "2024-03-18", "2024-03-22",
            "2024-03-27", "2024-03-29", "2024-04-01", "2024-04-10",
        ],
    },
    "region06": {
        "snpp": [
            "2024-02-12", "2024-02-16", "2024-02-17", "2024-02-18",
            "2024-02-24", "2024-02-27", "2024-03-04", "2024-03-10",
            "2024-03-13", "2024-03-15", "2024-03-16", "2024-03-18",
            "2024-03-22", "2024-03-27", "2024-03-29", "2024-04-01",
            "2024-04-10",
        ],
        "noaa20": [
            "2024-02-12", "2024-02-16", "2024-02-17", "2024-02-18",
            "2024-02-27", "2024-03-10", "2024-03-15", "2024-03-16",
            "2024-03-18", "2024-03-22", "2024-03-27", "2024-03-29",
            "2024-04-01", "2024-04-10",
        ],
        "noaa21": [
            "2024-02-12", "2024-02-16", "2024-02-17", "2024-02-18",
            "2024-03-04", "2024-03-10", "2024-03-15", "2024-03-16",
            "2024-03-18", "2024-03-22", "2024-03-27", "2024-03-29",
            "2024-04-01", "2024-04-10",
        ],
    },
    "region07": {
        "snpp": [
            "2024-02-12", "2024-02-13", "2024-02-16", "2024-02-17",
            "2024-02-18", "2024-02-24", "2024-02-27", "2024-03-04",
            "2024-03-08", "2024-03-10", "2024-03-13", "2024-03-14",
            "2024-03-15", "2024-03-16", "2024-03-18", "2024-03-22",
            "2024-03-27", "2024-03-29", "2024-04-01", "2024-04-10",
        ],
        "noaa20": [
            "2024-02-12", "2024-02-16", "2024-02-17", "2024-02-18",
            "2024-02-27", "2024-03-04", "2024-03-08", "2024-03-10",
            "2024-03-13", "2024-03-14", "2024-03-15", "2024-03-16",
            "2024-03-18", "2024-03-22", "2024-03-27", "2024-03-29",
            "2024-04-01", "2024-04-10",
        ],
        "noaa21": [
            "2024-02-12", "2024-02-16", "2024-02-17", "2024-02-18",
            "2024-03-04", "2024-03-10", "2024-03-13", "2024-03-14",
            "2024-03-15", "2024-03-16", "2024-03-18", "2024-03-22",
            "2024-03-27", "2024-03-29", "2024-04-01", "2024-04-10",
        ],
    },
    "region08": {
        "snpp": [
            "2024-02-12", "2024-02-13", "2024-02-16", "2024-02-17",
            "2024-02-18", "2024-02-24", "2024-02-27", "2024-03-04",
            "2024-03-10", "2024-03-13", "2024-03-14", "2024-03-15",
            "2024-03-16", "2024-03-18", "2024-03-22", "2024-03-27",
            "2024-03-29", "2024-04-01", "2024-04-10",
        ],
        "noaa20": [
            "2024-02-12", "2024-02-16", "2024-02-17", "2024-02-18",
            "2024-02-27", "2024-03-04", "2024-03-10", "2024-03-14",
            "2024-03-15", "2024-03-16", "2024-03-18", "2024-03-22",
            "2024-03-27", "2024-03-29", "2024-04-01", "2024-04-10",
        ],
        "noaa21": [
            "2024-02-12", "2024-02-16", "2024-02-17", "2024-02-18",
            "2024-03-04", "2024-03-10", "2024-03-14", "2024-03-15",
            "2024-03-18", "2024-03-22", "2024-03-27", "2024-03-29",
            "2024-04-01", "2024-04-10",
        ],
    },
    "region09": {
        "snpp": [
            "2024-02-08", "2024-02-12", "2024-02-13", "2024-02-16",
            "2024-02-17", "2024-02-18", "2024-02-27", "2024-03-04",
            "2024-03-09", "2024-03-10", "2024-03-13", "2024-03-14",
            "2024-03-15", "2024-03-16", "2024-03-18", "2024-03-22",
            "2024-03-27", "2024-03-29", "2024-04-01", "2024-04-10",
        ],
        "noaa20": [
            "2024-02-12", "2024-02-16", "2024-02-17", "2024-02-18",
            "2024-02-27", "2024-03-04", "2024-03-09", "2024-03-10",
            "2024-03-13", "2024-03-14", "2024-03-15", "2024-03-16",
            "2024-03-18", "2024-03-22", "2024-03-27", "2024-03-29",
            "2024-04-01",
        ],
        "noaa21": [
            "2024-02-12", "2024-02-16", "2024-02-17", "2024-02-18",
            "2024-03-04", "2024-03-09", "2024-03-10", "2024-03-13",
            "2024-03-15", "2024-03-16", "2024-03-18", "2024-03-22",
            "2024-03-27", "2024-03-29", "2024-04-01", "2024-04-10",
        ],
    },
}


def _check_plan_shape(name: str, plan: Mapping[str, Mapping[str, Sequence[str]]]) -> None:
    for region, platforms in plan.items():
        if region in FORBIDDEN_REGIONS:
            raise ValueError(f"{name}: {region} is out of scope (no 2024 burn events)")
        if region not in REGION_GEOJSONS:
            raise ValueError(f"{name}: unknown region '{region}' has no configured GeoJSON")
        if tuple(platforms) != PLATFORMS:
            raise ValueError(f"{name}: {region} must list exactly {PLATFORMS} in that order")
        for platform, values in platforms.items():
            for v in values:
                datetime.strptime(v, "%Y-%m-%d")
            if list(values) != sorted(set(values)):
                raise ValueError(f"{name}: {region}/{platform} dates must be sorted and unique")


def validate_download_plan(plan: Mapping[str, Mapping[str, Sequence[str]]] = RUN3_DOWNLOAD_PLAN) -> Dict[str, int]:
    """Validate the literal plan: 141 region-dates, each for all 3 platforms (423 tasks)."""
    _check_plan_shape("RUN3_DOWNLOAD_PLAN", plan)
    if set(plan) != set(REGION_GEOJSONS):
        raise ValueError(f"RUN3_DOWNLOAD_PLAN regions {sorted(plan)} != {sorted(REGION_GEOJSONS)}")
    for region, platforms in plan.items():
        if len({tuple(v) for v in platforms.values()}) != 1:
            raise ValueError(f"RUN3_DOWNLOAD_PLAN: {region} dates differ between platforms")
    total = sum(len(p[PLATFORMS[0]]) for p in plan.values())
    if total != EXPECTED_REQUIRED_REGION_DATE_TASKS:
        raise AssertionError(f"Expected {EXPECTED_REQUIRED_REGION_DATE_TASKS} region-date tasks, got {total}")
    return {"region_date_tasks": total, "platform_region_date_tasks": total * len(PLATFORMS)}


def plan_tasks(regions: Optional[Sequence[str]] = None) -> List[Tuple[str, str, str]]:
    """(region, platform, YYYY-MM-DD) in a fixed order."""
    validate_download_plan()
    out = []
    for region in sorted(RUN3_DOWNLOAD_PLAN):
        if regions and region not in regions:
            continue
        for d in RUN3_DOWNLOAD_PLAN[region][PLATFORMS[0]]:
            for platform in PLATFORMS:
                out.append((region, platform, d))
    return out


# ---------------------------------------------------------------------------
# External TEST (never used for training or development)
# ---------------------------------------------------------------------------

SCOPE_TRAIN_DEV = "train_dev"
SCOPE_TEST = "test"
SCOPES = (SCOPE_TRAIN_DEV, SCOPE_TEST)

TEST_REGION_GEOJSONS: Dict[str, str] = {
    "region05": "config/no5.geojson",
    "hitoyoshi": "config/hitoyoshi.geojson",
}

# The three previously evaluated external TEST cases (case, region, platform, PRE, POST). The research used
# SNPP only. region05_2023 / hitoyoshi_2025: viirs_burning/scripts/final_area_comparison_maps.py; all three:
# the existing images Aso/VIIRS/<region>/<year>/surface_reflectance/snpp/ (legacy Daily L2G VNP09GA) and their
# VNP64A1_<PRE>_<POST> / ACFR_<PRE>_<POST> companions. Not inferred. Only region05 2025-03-22 is also a MODIS TEST date.
RUN3_TEST_CASES: Tuple[Tuple[str, str, str, str, str], ...] = (
    ("region05_2023", "region05", "snpp", "2023-01-21", "2023-03-07"),
    ("region05_2025", "region05", "snpp", "2025-02-27", "2025-03-22"),
    ("hitoyoshi_2025", "hitoyoshi", "snpp", "2025-03-07", "2025-03-22"),
)

# Do not derive this from a range: the PRE/POST dates of RUN3_TEST_CASES, in full (6 tasks).
RUN3_TEST_PLAN: Dict[str, Dict[str, List[str]]] = {
    "hitoyoshi": {
        "snpp": ["2025-03-07", "2025-03-22"],
        "noaa20": [],
        "noaa21": [],
    },
    "region05": {
        "snpp": ["2023-01-21", "2023-03-07", "2025-02-27", "2025-03-22"],
        "noaa20": [],
        "noaa21": [],
    },
}
EXPECTED_TEST_TASKS = 6

TEST_MANIFEST_PATH = Path(__file__).resolve().parents[1] / "config" / "viirs_run3_test_acquisitions.json"


def validate_test_plan(plan: Mapping[str, Mapping[str, Sequence[str]]] = RUN3_TEST_PLAN) -> Dict[str, int]:
    """The TEST plan must be exactly the PRE/POST dates of RUN3_TEST_CASES (6 SNPP tasks)."""
    if set(plan) != set(TEST_REGION_GEOJSONS):
        raise ValueError("RUN3_TEST_PLAN regions must exactly match TEST_REGION_GEOJSONS")
    from_cases: Dict[Tuple[str, str], set] = {}
    for _, region, platform, pre, post in RUN3_TEST_CASES:
        from_cases.setdefault((region, platform), set()).update({pre, post})
    total = 0
    for region, platforms in plan.items():
        if tuple(platforms) != PLATFORMS:
            raise ValueError(f"RUN3_TEST_PLAN: {region} must list exactly {PLATFORMS} in that order")
        for platform, values in platforms.items():
            for v in values:
                datetime.strptime(v, "%Y-%m-%d")
            if list(values) != sorted(set(values)):
                raise ValueError(f"RUN3_TEST_PLAN: {region}/{platform} dates must be sorted and unique")
            if set(values) != from_cases.get((region, platform), set()):
                raise ValueError(f"RUN3_TEST_PLAN: {region}/{platform} differs from RUN3_TEST_CASES")
            total += len(values)
    if total != EXPECTED_TEST_TASKS:
        raise AssertionError(f"Expected {EXPECTED_TEST_TASKS} TEST tasks, got {total}")
    return {"test_tasks": total}


def test_plan_tasks(regions: Optional[Sequence[str]] = None) -> List[Tuple[str, str, str]]:
    """(region, platform, YYYY-MM-DD) of RUN3_TEST_PLAN in a fixed order."""
    validate_test_plan()
    return [(r, p, d) for r in sorted(RUN3_TEST_PLAN) if not regions or r in regions
            for p in PLATFORMS for d in RUN3_TEST_PLAN[r][p]]


def region_geojson(region: str) -> str:
    """AOI GeoJSON of a TRAIN/DEV or TEST region (region05 is the same file in both)."""
    return {**REGION_GEOJSONS, **TEST_REGION_GEOJSONS}[region]


def research_required_tasks() -> Set[Tuple[str, str, str]]:
    """(region, platform, date) of RUN3_RESEARCH_TASKS; must be 344 tasks inside the plan."""
    _check_plan_shape("RUN3_RESEARCH_TASKS", RUN3_RESEARCH_TASKS)
    rows = {(r, p, d) for r, platforms in RUN3_RESEARCH_TASKS.items() for p, ds in platforms.items() for d in ds}
    if len(rows) != EXPECTED_RESEARCH_REQUIRED:
        raise AssertionError(f"Expected {EXPECTED_RESEARCH_REQUIRED} research tasks, got {len(rows)}")
    outside = rows - set(plan_tasks())
    if outside:
        raise ValueError(f"RUN3_RESEARCH_TASKS: tasks outside the plan: {sorted(outside)[:5]}")
    return rows


# ---------------------------------------------------------------------------
# Cross-check records (not the plan): the CSVs the literals were moved from
# ---------------------------------------------------------------------------

def load_required_dates_csv(csv_path: Path = REQUIRED_DATES_CSV) -> Dict[str, List[str]]:
    """Read the region,date record. Returns {region: [YYYY-MM-DD, ...]} sorted; never infers dates."""
    csv_path = Path(csv_path)
    with csv_path.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames != ["region", "date"]:
            raise ValueError(f"{csv_path}: expected header 'region,date', got {reader.fieldnames}")
        rows = list(reader)

    seen: set = set()
    tasks: Dict[str, List[str]] = defaultdict(list)
    for row in rows:
        region, date_text = row["region"].strip(), row["date"].strip()
        datetime.strptime(date_text, "%Y-%m-%d")
        if region in FORBIDDEN_REGIONS:
            raise ValueError(f"{csv_path}: {region} is out of scope (no 2024 burn events)")
        if region not in REGION_GEOJSONS:
            raise ValueError(f"{csv_path}: unknown region '{region}' has no configured GeoJSON")
        if (region, date_text) in seen:
            raise ValueError(f"{csv_path}: duplicate (region, date) row: {(region, date_text)}")
        seen.add((region, date_text))
        tasks[region].append(date_text)
    return {r: sorted(d) for r, d in tasks.items()}


def load_research_required_csv(csv_path: Path = RESEARCH_REQUIRED_CSV) -> Set[Tuple[str, str, str]]:
    """Read the region,platform,date record of the research tasks."""
    with Path(csv_path).open(newline="", encoding="utf-8") as f:
        return {(r["region"], r["platform"], r["date"]) for r in csv.DictReader(f)}


@dataclass(frozen=True)
class Overpass:
    orbit: int
    granules: Tuple[str, ...]                 # SR file names, in start-time order
    geolocation: Tuple[Tuple[str, str], ...]  # (IMG, MOD) per SR granule
    aoi_status: str                           # at acquisition time (informational)


@dataclass(frozen=True)
class Task:
    region: str
    platform: str
    date: str  # YYYY-MM-DD
    status: str
    overpasses: Tuple[Overpass, ...]

    def raw_files(self) -> List[str]:
        out = []
        for ov in self.overpasses:
            for sr, (img, mod) in zip(ov.granules, ov.geolocation):
                out += [sr, img, mod]
        return out


def build_manifest(acquisition_root: Path, scope: str = SCOPE_TRAIN_DEV) -> Dict[str, Any]:
    """Freeze tasks from an earlier acquisition (summaries + raw archive under acquisition_root/<region>/viirs/).

    scope "test" freezes RUN3_TEST_PLAN from an acquisition made with acquire_test().

    A day without a summary is NO_GRANULE (CMR returned no SR granule). The SR
    granules of each summary must equal the daytime SR files in that day's raw
    directory -- the set the research pipeline reads -- otherwise the build fails.
    """
    from .viirs_l2 import parse_granule_name

    tasks = []
    for region, platform, d in (test_plan_tasks() if scope == SCOPE_TEST else plan_tasks()):
        sr = SR_PRODUCT_BY_PLATFORM[platform]
        base = Path(acquisition_root) / region / "viirs"
        summary = base / "surface_reflectance" / platform / "l2_swath" / "summary" / f"{sr}_{platform}_{d.replace('-', '')}.json"
        if not summary.exists():
            tasks.append({"region": region, "platform": platform, "date": d, "status": STATUS_NO_GRANULE, "overpasses": []})
            continue
        s = json.loads(summary.read_text(encoding="utf-8"))
        ovs = [{"orbit": o["orbit"], "granules": o["granules"], "geolocation": o["geolocation"],
                "aoi_status": o["aoi_status"]} for o in s["overpasses"]]
        listed = sorted(g for o in ovs for g in o["granules"])
        doy = datetime.strptime(d, "%Y-%m-%d").timetuple().tm_yday
        on_disk = sorted(p.name for p in (base / "_raw_l2" / sr / d[:4] / f"{doy:03d}").glob("*.hdf"))
        if listed != on_disk:
            raise ValueError(f"{region}/{platform}/{d}: summary granules {listed} != raw SR files {on_disk}")
        for g in listed:
            if parse_granule_name(g).acq_date.isoformat() != d:
                raise ValueError(f"{g}: acquisition date is not {d}")
        tasks.append({"region": region, "platform": platform, "date": d, "status": STATUS_GRANULES, "overpasses": ovs})
    unique = {f for t in tasks for o in t["overpasses"] for g, geo in zip(o["granules"], o["geolocation"]) for f in [g, *geo]}
    data = {"schema": MANIFEST_SCHEMA, "product_collection": "002", "halo_m": DEFAULT_HALO_M,
            "task_count": len(tasks), "unique_raw_files": len(unique),
            "source": "acquisition summaries (CMR DayNightFlag=DAY, SR InputPointer geolocation pairing) + raw archive listing",
            "tasks": tasks}
    if scope == SCOPE_TEST:
        data["scope"] = "external TEST (RUN3_TEST_PLAN); CMR search over the AOI + 13 km halo analysis window"
    return data


def acquire_test(acquisition_root: Path, client: Any = None, halo_m: float = DEFAULT_HALO_M) -> List[Dict[str, Any]]:
    """Maintenance (network): find and fetch the TEST tasks' L2 swath granules the way the 2024 acquisition did.

    Per task: CMR search of the SR collection over the AOI + halo analysis window (lon/lat bbox) for that
    day, then src.viirs_l2.process_platform_day (DayNightFlag=DAY, SR InputPointer -> exact IMG/MOD pair,
    orbit grouping, AOI status) with raw files under acquisition_root/<region>/viirs/_raw_l2/. The summaries it
    writes are what build_manifest(scope="test") freezes. Files already present are reused, never re-fetched.
    """
    from datetime import date as _date

    from .halo_context import load_geojson_geometry
    from .viirs_l2 import PLATFORMS as L2_PLATFORMS
    from .viirs_l2 import LAADSClient, _grid_transformer, grid_window_for_geometry, parse_granule_name, process_platform_day

    client = client or LAADSClient()
    repo = Path(__file__).resolve().parents[1]
    log = []
    for region, platform, d in test_plan_tasks():
        p = L2_PLATFORMS[platform]
        day = _date.fromisoformat(d)
        geometry = load_geojson_geometry(repo / region_geojson(region))
        window = grid_window_for_geometry(geometry, halo_m)
        tf_inv = _grid_transformer(window.grid.crs, inverse=True)
        lo, la = tf_inv.transform([window.x0, window.x1, window.x0, window.x1], [window.y0, window.y0, window.y1, window.y1])
        bbox = (min(lo), min(la), max(lo), max(la))
        entries = [e for e in client.search(p.sr_concept_id, day, day, bbox) if parse_granule_name(e[0]).acq_date == day]
        out_root = Path(acquisition_root) / region
        if not entries:
            log.append({"region": region, "platform": platform, "date": d, "status": STATUS_NO_GRANULE, "search_bbox": bbox})
            continue
        s = process_platform_day(p, day, entries, out_root / "viirs" / "_raw_l2", client, geometry, window, out_root)
        log.append({"region": region, "platform": platform, "date": d, "status": STATUS_GRANULES, "search_bbox": bbox,
                    "cmr_entries": [list(e) for e in entries],
                    "overpasses": [{k: o[k] for k in ("orbit", "granules", "geolocation", "aoi_status")} for o in s["overpasses"]]})
    return log


def load_acquisition_manifest(path: Optional[Path] = None, scope: str = SCOPE_TRAIN_DEV) -> List[Task]:
    """Load and validate the frozen manifest against RUN3_DOWNLOAD_PLAN (exactly the 423 tasks), or against
    RUN3_TEST_PLAN (the 6 TEST tasks) for scope="test"."""
    from .viirs_l2 import parse_granule_name

    if path is None:
        path = TEST_MANIFEST_PATH if scope == SCOPE_TEST else DEFAULT_MANIFEST_PATH
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if data.get("schema") != MANIFEST_SCHEMA:
        raise ValueError(f"{path}: unexpected schema {data.get('schema')!r}")
    tasks = []
    for t in data["tasks"]:
        ovs = []
        for o in t["overpasses"]:
            if len(o["granules"]) != len(o["geolocation"]) or not o["granules"]:
                raise ValueError(f"{path}: {t['region']}/{t['platform']}/{t['date']} orbit {o['orbit']} is malformed")
            for g, (img, mod) in zip(o["granules"], o["geolocation"]):
                names = [parse_granule_name(n) for n in (g, img, mod)]
                if names[0].product != SR_PRODUCT_BY_PLATFORM[t["platform"]] or names[0].acq_date.isoformat() != t["date"]:
                    raise ValueError(f"{path}: {g} does not belong to {t['platform']} {t['date']}")
            ovs.append(Overpass(int(o["orbit"]), tuple(o["granules"]), tuple(tuple(x) for x in o["geolocation"]), o["aoi_status"]))
        if (t["status"] == STATUS_NO_GRANULE) != (not ovs):
            raise ValueError(f"{path}: {t['region']}/{t['platform']}/{t['date']} status {t['status']} inconsistent")
        tasks.append(Task(t["region"], t["platform"], t["date"], t["status"], tuple(ovs)))
    if scope == SCOPE_TEST:
        if [(t.region, t.platform, t.date) for t in tasks] != test_plan_tasks():
            raise ValueError(f"{path}: tasks do not exactly match RUN3_TEST_PLAN ({EXPECTED_TEST_TASKS})")
    elif [(t.region, t.platform, t.date) for t in tasks] != plan_tasks():
        raise ValueError(f"{path}: tasks do not exactly match RUN3_DOWNLOAD_PLAN ({EXPECTED_PLATFORM_REGION_DATE_TASKS})")
    return tasks


def _main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Maintenance: (re)build the frozen VIIRS acquisition manifest.")
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--build", action="store_true")
    action.add_argument("--acquire-test", action="store_true",
                        help="network: search + fetch the TEST tasks into --acquisition-root (input of --build --scope test)")
    parser.add_argument("--acquisition-root", type=Path, required=True,
                        help="root holding <region>/viirs/{_raw_l2,surface_reflectance} of an earlier acquisition (read-only)")
    parser.add_argument("--output", type=Path, required=True,
                        help="manifest JSON (--build) or acquisition log (--acquire-test) to write (must not exist)")
    parser.add_argument("--scope", choices=SCOPES, default=SCOPE_TRAIN_DEV)
    args = parser.parse_args(argv)
    if args.output.exists():
        parser.error(f"refusing to overwrite {args.output}")
    if args.acquire_test:
        from .surface_reflectance import _earthdata_login

        _earthdata_login({}, Path.cwd())
        log = acquire_test(args.acquisition_root)
        args.output.write_text(json.dumps(log, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"acquired {len(log)} TEST tasks into {args.acquisition_root}; log {args.output}")
        return 0
    data = build_manifest(args.acquisition_root, args.scope)
    args.output.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    tasks = load_acquisition_manifest(args.output, args.scope)
    print(f"wrote {args.output}: {len(tasks)} tasks, {data['unique_raw_files']} unique raw files")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
