# -*- coding: utf-8 -*-
"""QA：variable_map 短码整词匹配防误伤 + family 过滤（design-final.md §7.3）。"""
from __future__ import annotations

from era5tool.config.settings import Settings
from era5tool.nl.variable_map import VariableMap


def _vm(app_state):
    return VariableMap(app_state.settings)


def test_short_code_e_no_false_positive(app_state):
    """'e'（蒸发）不得误伤 'temperature' / 任意单词中的 e。"""
    vm = _vm(app_state)
    assert "evaporation" not in vm.find_all("temperature", "era5-single")
    assert "evaporation" not in vm.find_all("下载温度数据", "era5-single")
    assert "evaporation" not in vm.find_all("weather", "era5-single")


def test_short_code_r_no_false_positive(app_state):
    """'r'（相对湿度）不得误伤普通文本。"""
    vm = _vm(app_state)
    assert "relative_humidity" not in vm.find_all("temperature", "era5-single")
    assert "relative_humidity" not in vm.find_all("下雨", "era5-single")


def test_short_code_tp_whole_word_match(app_state):
    """'tp' 整词匹配降水；单词内部不匹配。"""
    vm = _vm(app_state)
    assert "total_precipitation" in vm.find_all("tp", "era5-single")
    assert "total_precipitation" not in vm.find_all("stp", "era5-single")


def test_short_code_msl_whole_word(app_state):
    vm = _vm(app_state)
    assert "mean_sea_level_pressure" in vm.find_all("msl", "era5-single")
    # 'msl' 不应匹配单词内部（xmsl）；'mslp' 本身是合法同义词应命中
    assert "mean_sea_level_pressure" not in vm.find_all("xmsl", "era5-single")
    assert "mean_sea_level_pressure" in vm.find_all("mslp", "era5-single")


def test_family_filter_land_only_variable(app_state):
    """雪水当量仅 land 适用；海温仅 era5-single 适用。"""
    vm = _vm(app_state)
    assert "snow_depth_water_equivalent" in vm.find_all("雪水当量", "land")
    assert "snow_depth_water_equivalent" not in vm.find_all("雪水当量", "era5-single")
    assert "sea_surface_temperature" in vm.find_all("海温", "era5-single")
    assert "sea_surface_temperature" not in vm.find_all("海温", "land")


def test_monthly_family_inherits_base_vars(app_state):
    """land-monthly 继承 land 变量集；era5-monthly 继承 era5-single。"""
    vm = _vm(app_state)
    assert "snow_depth_water_equivalent" in vm.find_all("雪水当量", "land-monthly")
    assert "mean_sea_level_pressure" in vm.find_all("海平面气压", "era5-monthly")


def test_chinese_multi_char_substring(app_state):
    """中文长词按子串匹配（'土壤湿度' → volumetric_soil_water_layer_1）。"""
    vm = _vm(app_state)
    assert "volumetric_soil_water_layer_1" in vm.find_all("2023年土壤湿度", "land")
