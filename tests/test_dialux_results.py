from pathlib import Path

import pytest

from lighting_agent.dialux_runner.artifacts import RunError
from lighting_agent.dialux_runner.results import parse_summary


@pytest.fixture
def expected():
    return {'building': 'IFC bridge fixture', 'floor': '1F', 'room': '101 - Bridge test room',
            'scene': '灯光场景 1', 'surface': '工作面 (101 - Bridge test room)', 'surface_index': 'WP1',
            'floor_area_m2': 24., 'workplane_height_m': .8, 'edge_margin_m': .5,
            'maintenance_factor': .8, 'mounting_height_m': 2.5, 'ceiling_reflectance': .7,
            'wall_reflectance': .5, 'floor_reflectance': .2}


@pytest.fixture
def summary_text():
    return (Path(__file__).parent / 'fixtures/phase0/room-summary-5.14-zh.txt').read_text(encoding='utf-8')


def parse(text, expected):
    return parse_summary(text, expected=expected, pdf_sha256='a' * 64, source_name='report.pdf')


def test_real_summary_does_not_confuse_targets_scopes_or_glare_method(summary_text, expected):
    result = parse(summary_text, expected)
    metrics = result['metrics']
    assert metrics['average_illuminance_lx']['value'] == 23.2
    assert metrics['uniformity_u0']['value'] == .46
    assert metrics['workplane_lpd_w_m2']['value'] == .67
    assert metrics['room_lpd_w_m2']['value'] == .42
    assert metrics['room_lpd_w_m2']['source']['page'] == 2
    assert metrics['average_illuminance_lx']['source']['sha256'] == 'a' * 64
    assert result['conditions']['maintenance_factor']['value'] == .8
    assert result['glare']['label'] == 'RUG,max'
    assert result['glare']['verified_observer_calculation'] is False
    assert result['compliance']['status'] == 'not_evaluated'


@pytest.mark.parametrize(('old', 'new'), [
    ('23.2 lx', '--- lx'), ('WP1', 'WP2'), ('灯光场景 1', '灯光场景 2'),
    ('101 - Bridge test room', '102 - Other room'), ('0.800 m', '0.750 m'),
    ('0.80 (一般情况)', '0.70 (一般情况)'), ('70.0 %', '80.0 %'),
    ('Uo (g1)', 'Uo (g2)'), ('0.46', '1.46'), ('RUG, max', 'UGR'),
    ('基于 6.000', '基于未知'),
])
def test_missing_or_changed_results_require_review(summary_text, expected, old, new):
    with pytest.raises(RunError):
        parse(summary_text.replace(old, new), expected)


def test_duplicate_room_surface_or_page_is_not_silently_accepted(summary_text, expected):
    with pytest.raises(RunError):
        parse(summary_text + '\f' + summary_text, expected)
    row = next(line for line in summary_text.splitlines() if 'Uo (g1)' in line)
    with pytest.raises(RunError):
        parse(summary_text.replace(row, row + '\n' + row), expected)


def test_decimal_comma_and_report_values_are_not_hardcoded(summary_text, expected):
    result = parse(summary_text.replace('23.2 lx', '27,5 lx').replace('0.46', '0,51'), expected)
    assert result['metrics']['average_illuminance_lx']['value'] == 27.5
    assert result['metrics']['uniformity_u0']['value'] == .51


def test_imported_room_workplane_index_is_read_from_both_rows(summary_text, expected):
    adaptive = {**expected, 'surface_index': None}
    assert parse(summary_text.replace('WP1', 'WP2'), adaptive)['surface_index'] == 'WP2'
    with pytest.raises(RunError, match='index'):
        parse(summary_text.replace('WP1', 'WP2', 1), adaptive)
    with pytest.raises(RunError, match='index'):
        parse(summary_text.replace('WP1', 'WP0'), adaptive)
