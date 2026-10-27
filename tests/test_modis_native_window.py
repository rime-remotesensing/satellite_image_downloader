from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
from rasterio.transform import Affine

from src import modis_native_window as nw
from src.surface_reflectance import _SDSData, _tile_transform

TILE = "h28v05"
SR_FILL = -28672


def _fake_reader(arrays):
    def read(path, field):
        arr = arrays[field]
        if field.startswith("sur_refl"):
            return _SDSData(arr, 0.0001, 0.0, SR_FILL, (-100, 16000), f"{field}_1")
        return _SDSData(arr, None, None, None, None, f"{field}_1")
    return read


@pytest.fixture
def tile_arrays():
    rng = np.random.default_rng(0)
    a = {f"sur_refl_b0{i}": rng.integers(0, 9000, (40, 40)).astype(np.int16) for i in range(1, 8)}
    a["sur_refl_b06"][5, 7] = SR_FILL
    a["QC_500m"] = rng.integers(0, 2**31, (40, 40)).astype(np.uint32)
    a["state_1km"] = rng.integers(0, 2**15, (20, 20)).astype(np.uint16)
    return a


def _window_transform(n_pixels_side, col0, row0):
    t = _tile_transform(TILE, n_pixels_side, n_pixels_side)
    return Affine(t.a, 0, t.c + col0 * t.a, 0, t.e, t.f + row0 * t.e)


def test_exact_integer_window(monkeypatch, tile_arrays, tmp_path):
    monkeypatch.setattr(nw, "_read_sds", _fake_reader(tile_arrays))
    hdf = tmp_path / f"MOD09GA.A2024039.{TILE}.061.2024041034305.hdf"
    arr, meta, t = nw.extract_native_window([hdf], "sur_refl_b01", _window_transform(40, 6, 4), (5, 9))
    assert np.array_equal(arr, tile_arrays["sur_refl_b01"][4:9, 6:15])
    assert t == _window_transform(40, 6, 4)


def test_subpixel_reference_is_refused(monkeypatch, tile_arrays, tmp_path):
    monkeypatch.setattr(nw, "_read_sds", _fake_reader(tile_arrays))
    hdf = tmp_path / f"MOD09GA.A2024039.{TILE}.061.x.hdf"
    t = _window_transform(40, 6, 4)
    shifted = Affine(t.a, 0, t.c + t.a * 0.3, 0, t.e, t.f)
    with pytest.raises(nw.NativeWindowError):
        nw.extract_native_window([hdf], "sur_refl_b01", shifted, (5, 9))


def test_window_outside_mosaic_is_refused_not_padded(monkeypatch, tile_arrays, tmp_path):
    monkeypatch.setattr(nw, "_read_sds", _fake_reader(tile_arrays))
    hdf = tmp_path / f"MOD09GA.A2024039.{TILE}.061.x.hdf"
    with pytest.raises(nw.NativeWindowError):
        nw.extract_native_window([hdf], "sur_refl_b01", _window_transform(40, 35, 0), (5, 9))


def test_generate_layout_dtypes_and_no_overwrite(monkeypatch, tile_arrays, tmp_path):
    import rasterio

    monkeypatch.setattr(nw, "_read_sds", _fake_reader(tile_arrays))
    acq = nw.Acquisition("region01", "terra", "20240208")
    hdf = tmp_path / f"MOD09GA.A2024039.{TILE}.061.2024041034305.hdf"
    grids = {"sr": (_window_transform(40, 4, 2), (6, 8)), "qc_500m": (_window_transform(40, 4, 2), (6, 8)),
             "state_1km": (_window_transform(20, 2, 1), (3, 4))}
    out = tmp_path / "out"
    paths = nw.generate_acquisition(acq, [hdf], grids, out)
    assert paths["sr"].endswith("region01/2024/surface_reflectance/terra/500m/MOD09GA_terra_20240208_500m.tif")
    assert paths["qc_500m"].endswith("qa/MOD09GA_terra_20240208_QC_500m.tif")
    assert paths["state_1km"].endswith("qa/MOD09GA_terra_20240208_state_1km.tif")

    with rasterio.open(paths["sr"]) as ds:
        assert ds.count == 7 and ds.dtypes[0] == "float32" and np.isnan(ds.nodata)
        np.testing.assert_allclose(ds.read(1), tile_arrays["sur_refl_b01"][2:8, 4:12] * np.float32(0.0001))
        assert np.isnan(ds.read(6)[5 - 2, 7 - 4])  # sensor fill stays NaN, nothing else is masked
        assert np.isfinite(ds.read(1)).all()
    with rasterio.open(paths["qc_500m"]) as ds:
        assert ds.dtypes[0] == "uint32" and ds.nodata is None
        assert np.array_equal(ds.read(1), tile_arrays["QC_500m"][2:8, 4:12])
    with rasterio.open(paths["state_1km"]) as ds:
        assert ds.dtypes[0] == "uint16" and ds.shape == (3, 4)
        assert abs(ds.transform.a - 2 * _window_transform(40, 0, 0).a) < 1e-6
        assert np.array_equal(ds.read(1), tile_arrays["state_1km"][1:4, 2:6])

    for key in paths:
        assert nw.compare_files(Path(paths[key]), Path(paths[key]))["identical"]
    with pytest.raises(FileExistsError):
        nw.generate_acquisition(acq, [hdf], grids, out)


def test_compare_detects_value_difference(monkeypatch, tile_arrays, tmp_path):
    monkeypatch.setattr(nw, "_read_sds", _fake_reader(tile_arrays))
    acq = nw.Acquisition("region01", "terra", "20240208")
    hdf = tmp_path / f"MOD09GA.A2024039.{TILE}.061.2024041034305.hdf"
    grids = {"sr": (_window_transform(40, 4, 2), (6, 8)), "qc_500m": (_window_transform(40, 4, 2), (6, 8)),
             "state_1km": (_window_transform(20, 2, 1), (3, 4))}
    a = nw.generate_acquisition(acq, [hdf], grids, tmp_path / "a")
    tile_arrays["QC_500m"][3, 5] ^= 1
    b = nw.generate_acquisition(acq, [hdf], grids, tmp_path / "b")
    res = nw.compare_files(Path(a["qc_500m"]), Path(b["qc_500m"]))
    assert not res["identical"] and res["mismatch_pixels"] == 1
    assert nw.compare_files(Path(a["sr"]), Path(b["sr"]))["identical"]
