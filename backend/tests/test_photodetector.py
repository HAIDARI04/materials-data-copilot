import hashlib
import io
import json
import math
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path

import numpy as np
import pytest

import database
import photodetector
import photodetector_reports
from test_research import storage, workbook, upload, assert_raw


MIME = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'


def source_bytes(light=False, voltages=None):
    voltages = voltages or [-1, 0, 1]
    return workbook({'Run1': [['AI', 'AV'], *[
        [2e-9 * (voltage + 2) + (2e-6 if light else 0), voltage] for voltage in voltages]]})


def imported_pair(client):
    dark_bytes, light_bytes = source_bytes(), source_bytes(True, [-1, -.5, 0, .5, 1])
    dark = upload(client, dark_bytes, 'D00_I-V-dark.xlsx', 'Transport', MIME)
    light = upload(client, light_bytes, 'D00_I-V-light.xlsx', 'Transport', MIME)
    return dark, light, dark_bytes, light_bytes


def request_for(dark, light, **extra):
    return {'dark_file_id': dark['file_id'], 'light_file_id': light['file_id'],
        'same_device_confirmed': True, 'roles_units_confirmed': True, **extra}


def test_recipe_metrics_pairing_export_cache_and_provenance(storage):
    client, _ = storage
    dark, light, dark_bytes, light_bytes = imported_pair(client)
    pairs = client.get('/research/photodetector/suggestions').json()['pairs']
    assert [(pair['dark_file_id'], pair['light_file_id']) for pair in pairs] == [(dark['file_id'], light['file_id'])]
    inspected = client.get(f'/research/photodetector/files/{dark["file_id"]}/traces')
    assert inspected.status_code == 200
    assert inspected.json()['traces'][0]['current'] == 'AI'
    assert 'points' not in inspected.json()['traces'][0]
    payload = request_for(dark, light, area_value=100, area_unit='um2', power_density=100,
                          noise_mode='shot', wavelength_nm=532, monochromatic=True)
    response = client.post('/research/photodetector/analyze', json=payload)
    assert response.status_code == 200, response.text
    result = response.json()
    assert result['normalized_inputs']['active_area_cm2'] == pytest.approx(1e-6)
    assert result['normalized_inputs']['incident_power_w'] == pytest.approx(1e-7)
    assert result['summary']['matched_points'] == 5
    assert result['summary']['max_responsivity_a_w'] == pytest.approx(20)
    assert result['summary']['max_on_off_ratio'] == pytest.approx(1001)
    assert result['metrics']['zero_bias']['photocurrent_a'] == pytest.approx(2e-6)
    assert result['rows'][0]['detectivity_jones'] == pytest.approx(20 * math.sqrt(1e-6) / math.sqrt(2 * photodetector.Q * 2e-9))
    assert result['rows'][0]['eqe_percent'] == pytest.approx(20 * photodetector.H * photodetector.C / (photodetector.Q * 532e-9) * 100)
    assert any('shot-noise' in warning for warning in result['warnings'])
    assert client.post('/research/photodetector/analyze', json=payload).json()['processing_id'] == result['processing_id']
    assert client.get('/research/photodetector/history').json()[0]['processing_id'] == result['processing_id']
    figures = client.get(f'/research/photodetector/{result["processing_id"]}/figures').json()
    assert set(figures) == {'iv', 'response', 'ratio', 'diode'}
    for svg in figures.values():
        ET.fromstring(svg)
    report = client.get(f'/research/photodetector/{result["processing_id"]}/report')
    assert report.status_code == 200
    assert 'Methodology: worked photodetector calculation' in report.text
    assert 'Reproduce this analysis' in report.text
    assert dark['sha256'] in report.text and light['sha256'] in report.text
    package = client.get(f'/research/analyses/{result["processing_id"]}/export')
    assert package.status_code == 200, package.text
    with zipfile.ZipFile(io.BytesIO(package.content)) as archive:
        manifest = json.loads(archive.read('manifest.json'))
        for name, digest in manifest['files'].items():
            assert hashlib.sha256(archive.read(name)).hexdigest() == digest
        assert 'figures/iv.svg' in archive.namelist()
        assert 'matched-measurements.csv' in archive.namelist()
    assert_raw(dark, dark_bytes, 'D00_I-V-dark.xlsx', MIME)
    assert_raw(light, light_bytes, 'D00_I-V-light.xlsx', MIME)
    with database.connect_database() as connection:
        run = connection.execute('SELECT * FROM processing_runs WHERE processing_id=?', (result['processing_id'],)).fetchone()
    assert run['model_name'] == 'research_photodetector'
    assert hashlib.sha256(Path(run['result_path']).read_bytes()).hexdigest() == run['result_sha256']


def test_missing_inputs_are_withheld_and_both_sources_verified_on_export(storage):
    client, _ = storage
    dark, light, _, _ = imported_pair(client)
    result = client.post('/research/photodetector/analyze', json=request_for(dark, light)).json()
    assert result['metrics']['responsivity'] is None
    assert result['metrics']['detectivity'] is None
    assert result['diode']['ideality_factor'] is None
    assert 'responsivity' in result['missing']
    answer = client.post('/research/photodetector/explain', json={'processing_id': result['processing_id'], 'question': 'What data are missing?'}).json()
    assert 'incident power' in answer['answer']
    Path(dark['storage_path']).write_bytes(b'tampered test fixture')
    assert client.get(f'/research/analyses/{result["processing_id"]}/export').status_code == 409
    assert client.get(f'/research/photodetector/{result["processing_id"]}/report').status_code == 409


def test_confirmation_and_physical_inputs_are_validated(storage):
    client, _ = storage
    dark, light, _, _ = imported_pair(client)
    payload = request_for(dark, light)
    for change in [dict(roles_units_confirmed=False), dict(area_value=-1), dict(light_file_id=dark['file_id']),
                   dict(noise_mode='measured'), dict(thermionic_confirmed=True)]:
        assert client.post('/research/photodetector/analyze', json={**payload, **change}).status_code == 422


def test_time_data_and_opposite_sweeps_do_not_become_iv_pairs(storage):
    client, _ = storage
    dark, light, _, _ = imported_pair(client)
    time = upload(client, workbook({'Time': [['Time', 'AI', 'AV'], [0, 1, 5], [1, 2, 5], [2, 3, 5]]}), 'it.xlsx', 'Transport', MIME)
    assert client.get(f'/research/photodetector/files/{time["file_id"]}/traces').json()['traces'] == []
    assert client.post('/research/photodetector/analyze', json=request_for(dark, time)).status_code == 422
    reverse = upload(client, source_bytes(True, [1, 0, -1]), 'reverse.xlsx', 'Transport', MIME)
    response = client.post('/research/photodetector/analyze', json=request_for(dark, reverse))
    assert response.status_code == 422 and 'directions' in response.text


def test_no_extrapolation_current_floor_and_measured_noise(tmp_path):
    dark, light = tmp_path/'dark.xlsx', tmp_path/'light.xlsx'
    dark.write_bytes(workbook({'Run': [['AI', 'AV'], [0, -1], [1e-9, 0], [2e-9, 1], [3e-9, 2]]}))
    light.write_bytes(source_bytes(True, [0, .5, 1]))
    options = photodetector.Recipe(**request_for({'file_id': 'd'}, {'file_id': 'l'},
        area_value=1, area_unit='mm2', power_mode='direct', incident_power_w=1e-6,
        noise_mode='measured', noise_a_sqrt_hz=1e-12, current_floor_a=1e-9)).model_dump()
    result = photodetector.analyze(light, dark, options)
    assert [row['voltage_v'] for row in result['rows']] == [0, .5, 1]
    assert result['rows'][0]['on_off_ratio'] is None
    assert result['rows'][0]['detectivity_jones'] == pytest.approx(result['rows'][0]['responsivity_a_w'] * .1 / 1e-12)


def test_diode_factor_and_apparent_barrier_use_original_dark_points():
    temperature, ideality, saturation = 300, 1.8, 1e-12
    voltage = np.linspace(.2, .6, 21)
    current = saturation * np.exp(photodetector.Q * voltage / (ideality * photodetector.KB * temperature))
    options = photodetector.Recipe(**request_for({'file_id': 'd'}, {'file_id': 'l'},
        thermionic_confirmed=True, temperature_k=temperature, fit_min_v=.2, fit_max_v=.6,
        contact_area_cm2=1e-6, richardson_a_cm2_k2=120)).model_dump()
    result = photodetector.diode_fit(np.column_stack([voltage, current]), options)
    assert result['ideality_factor'] == pytest.approx(ideality)
    assert result['saturation_current_a'] == pytest.approx(saturation)
    assert result['barrier_height_ev'] == pytest.approx(photodetector.KB * temperature / photodetector.Q * math.log(1e-6 * 120 * temperature**2 / saturation))
    assert result['fit']['point_count'] == 21
    assert 'does not establish' in result['interpretation']


def test_missing_rows_split_branches_and_no_filename_substring_guess(tmp_path):
    path = tmp_path/'gap.xlsx'
    path.write_bytes(workbook({'Run': [['AI', 'AV'], [1, 0], [2, 1], [None, 2], [3, 3], [4, 4]]}))
    assert photodetector.read_traces(path)[0] == []
    files = [{'file_id': name, 'original_filename': name, 'technique': 'Transport'} for name in ['flashlight.xlsx', 'darkness.xlsx']]
    assert photodetector.suggest_pairs(files) == []


def test_vector_report_preserves_missing_points_and_escapes_labels():
    svg = photodetector_reports.chart('<unsafe>', [('sample', [[0, 1], [1, None], [2, 3]], '#123456', False)], 'A')
    ET.fromstring(svg)
    assert '&lt;unsafe&gt;' in svg
    assert 'M100.000' in svg and 'M780.000' in svg
