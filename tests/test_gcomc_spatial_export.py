from __future__ import annotations

from src.gcomc import _resolve_gcomc_spatial_export


def test_omitted_config_is_default_clip_with_zero_halo():
    assert _resolve_gcomc_spatial_export({}) == (True, 0.0)


def test_explicit_aoi_clip_mode_ignores_halo_m():
    config = {"surface_reflectance": {"spatial_export": {"mode": "aoi_clip", "halo_m": 5000}}}
    assert _resolve_gcomc_spatial_export(config) == (True, 0.0)


def test_native_rect_halo_mode_disables_clip_and_uses_halo_m():
    config = {"surface_reflectance": {"spatial_export": {"mode": "native_rect_halo", "halo_m": 12000}}}
    assert _resolve_gcomc_spatial_export(config) == (False, 12000.0)


def test_native_rect_halo_mode_without_halo_m_is_zero():
    config = {"surface_reflectance": {"spatial_export": {"mode": "native_rect_halo"}}}
    assert _resolve_gcomc_spatial_export(config) == (False, 0.0)
