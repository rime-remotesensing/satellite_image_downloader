"""Pipeline wiring / regression tests for the GCOM-C opt-in (no network)."""
from __future__ import annotations

import json
import subprocess
import sys
import textwrap
from datetime import date
from pathlib import Path
from unittest.mock import patch

import pytest

from src import pipeline

ROOT = Path(__file__).resolve().parents[1]
GEOJSON = str(ROOT / "config" / "no5.geojson")


def _config(tmp_path, satellites):
    return {"geojson": GEOJSON, "startday": "20240409", "endday": "20240409", "satellite": satellites,
            "output": str(tmp_path / "out"), "activefire": "none"}


@pytest.fixture
def mocked_processors():
    with patch.object(pipeline, "_process_satellite_imagery", return_value={"ok": "img"}) as img, \
         patch.object(pipeline, "_process_modis_surface_reflectance", return_value={"ok": "modis"}) as modis, \
         patch.object(pipeline, "_process_viirs_surface_reflectance", return_value={"ok": "viirs"}) as viirs, \
         patch("src.gcomc._process_gcomc_surface_reflectance", return_value={"ok": "gcomc"}) as gcomc:
        yield {"img": img, "modis": modis, "viirs": viirs, "gcomc": gcomc}


def test_existing_satellites_dispatch_unchanged_and_gcomc_not_called(tmp_path, mocked_processors):
    m = mocked_processors
    result = pipeline.run_pipeline(_config(tmp_path, ["sentinel2", "landsat89", "modis", "viirs"]), ROOT)
    assert m["gcomc"].call_count == 0
    assert "gcomc_surface_reflectance" not in result
    assert [c.kwargs["satellite_key"] for c in m["img"].call_args_list] == ["sentinel2", "landsat89"]
    for key in ("modis", "viirs"):
        kwargs = m[key].call_args.kwargs
        assert set(kwargs) == {"config", "config_dir", "output_root", "geometry_wgs84", "bbox", "start_date", "end_date"}
        assert kwargs["start_date"] == kwargs["end_date"] == date(2024, 4, 9)
    assert result["modis_surface_reflectance"] == {"ok": "modis"}
    assert result["viirs_surface_reflectance"] == {"ok": "viirs"}


def test_gcomc_opt_in_alongside_modis_viirs(tmp_path, mocked_processors):
    m = mocked_processors
    result = pipeline.run_pipeline(_config(tmp_path, ["modis", "viirs", "gcomc"]), ROOT)
    assert m["modis"].call_count == m["viirs"].call_count == m["gcomc"].call_count == 1
    kwargs = m["gcomc"].call_args.kwargs
    assert set(kwargs) == {"config", "config_dir", "output_root", "geometry_wgs84", "start_date", "end_date"}
    assert result["gcomc_surface_reflectance"] == {"ok": "gcomc"}


def test_runs_without_gcomc_never_import_the_module(tmp_path):
    """Lazy import: a MODIS/VIIRS-only run must not load src.gcomc at all."""
    script = textwrap.dedent(f"""
        import sys
        from unittest.mock import patch
        from src import pipeline
        with patch.object(pipeline, "_process_modis_surface_reflectance", return_value={{}}), \\
             patch.object(pipeline, "_process_viirs_surface_reflectance", return_value={{}}):
            pipeline.run_pipeline({json.dumps(_config(tmp_path, ["modis", "viirs"]))}, __import__("pathlib").Path({str(ROOT)!r}))
        print("GCOMC_LOADED" if "src.gcomc" in sys.modules else "GCOMC_NOT_LOADED")
    """)
    out = subprocess.run([sys.executable, "-c", script], cwd=ROOT, capture_output=True, text=True, check=True)
    assert out.stdout.strip().splitlines()[-1] == "GCOMC_NOT_LOADED"
