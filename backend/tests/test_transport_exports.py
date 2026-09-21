"""Scientific, provenance and standalone replay contracts for electrical exports."""
import csv
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import zipfile

import numpy as np
import pytest
from pypdf import PdfReader
from scipy.stats import linregress

import database
import main
import research_analysis
import research_transport
import transport_exports
import transport_methods
from test_research import assert_raw, storage, upload, workbook
from test_photodetector import imported_pair, request_for


MIME = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
CHANNEL = {'current': 'AI', 'voltage': 'AV', 'terminal': 'A', 'current_scale': 1,
           'voltage_scale': 1, 'polarity': 1, 'voltage_status': 'measured'}


def analyze(client, data=None, options=None):
    data = data or workbook({'Sweep': [['AV', 'AI', 'Notes'], [-2, -4.1, '=1+1'], [-1, -1.95, ''],
                                      [0, .1, ''], [1, 2.05, ''], [2, 3.9, '']]})
    source = upload(client, data, 'transport.xlsx', 'Transport', MIME)
    payload = {'technique': 'transport', 'options': options or {'channels': [CHANNEL], 'units_confirmed': True, 'wiring_confirmed': True}}
    response = client.post(f'/research/files/{source["file_id"]}/analyze', json=payload)
    assert response.status_code == 200, response.text
    return source, data, response.json()


def replay(directory, *arguments):
    environment = os.environ.copy()
    environment.pop('PYTHONPATH', None)
    return subprocess.run([sys.executable, str(directory / 'reproduce.py'), *arguments],
                          cwd=directory, env=environment, capture_output=True, text=True, timeout=60)


def test_fit_statistics_match_independent_scipy_and_handle_zero():
    x = np.array([-2., -1., 0., 1., 2.])
    y = np.array([-4.1, -1.95, .1, 2.05, 3.9])
    result = transport_methods.linear_fit(list(zip(x, y)))
    expected = linregress(x, y)
    assert result['slope'] == pytest.approx(expected.slope)
    assert result['slope_standard_error'] == pytest.approx(expected.stderr)
    assert result['intercept_standard_error'] == pytest.approx(expected.intercept_stderr)
    assert result['slope_intercept_covariance'] == pytest.approx(0)
    assert sum(result['residuals']) == pytest.approx(0, abs=1e-14)
    crossing = transport_methods.linear_fit([[-2, 1], [-1, -1], [0, 1], [1, -1], [2, 1]])
    transport_methods.interpret_fit(crossing, CHANNEL, 'AV', {})
    assert crossing['resistance_ohm'] is None
    assert any('includes zero' in reason for reason in crossing['missing_inputs'])
    assert transport_methods.linear_fit([[1, 2], [1, 3], [1, 4]]) is None
    assert transport_methods.linear_fit([[1, 2], [2, 3]]) is None


@pytest.mark.parametrize('technique', ['transport', 'photodetector'])
def test_portable_calculations_replay_without_application_or_renderers(storage, technique):
    client, root = storage
    if technique == 'transport':
        _, _, result = analyze(client)
    else:
        dark, light, _, _ = imported_pair(client)
        result = client.post('/research/photodetector/analyze', json=request_for(dark, light,
            area_value=100, power_density=100, noise_mode='shot', wavelength_nm=532, monochromatic=True)).json()
    run = database.get_processing_run(result['processing_id'])
    files = transport_exports.portable_files(result, Path(run['result_path']).read_bytes(),
                                            transport_exports.verify_result_sources(result, main.RAW_DATA_DIR))
    manifest = {'files': {name: hashlib.sha256(content).hexdigest() for name, content in files.items()}}
    files['manifest.json'] = json.dumps(manifest).encode()
    destination = root / 'portable'
    for name, content in files.items():
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    checked = replay(destination)
    assert checked.returncode == 0, checked.stdout + checked.stderr
    assert (destination / 'reproduced' / 'verification.json').is_file()


def test_cross_channel_slope_uses_axis_scale_and_never_becomes_resistance(tmp_path):
    path = tmp_path / 'cross.xlsx'
    path.write_bytes(workbook({'Gate sweep': [['DrainI', 'DrainV', 'GateI', 'GateV'],
                                              [1, 1, 0, 0], [3, 1, 0, 1000], [5, 1, 0, 2000]]}))
    options = {'channels': [
        {**CHANNEL, 'current': 'DrainI', 'voltage': 'DrainV', 'terminal': 'Drain', 'voltage_scale': 1},
        {**CHANNEL, 'current': 'GateI', 'voltage': 'GateV', 'terminal': 'Gate', 'voltage_scale': .001},
    ], 'terminal_roles': ['Drain']}
    original_options = json.dumps(options, sort_keys=True)
    result = research_transport.analyze(path, options)
    branch = result['sheets'][0]['branches'][0]
    assert branch['points'] == [[0, 1], [1, 3], [2, 5]]
    assert branch['fit']['slope'] == pytest.approx(2)
    assert branch['fit']['response_kind'] == 'transfer_slope'
    assert branch['fit']['resistance_ohm'] is None
    assert json.dumps(options, sort_keys=True) == original_options
    assert 'tek-gm' in [reference['id'] for reference in result['references']]


def test_time_integration_does_not_bridge_missing_rows(tmp_path):
    path = tmp_path / 'time.xlsx'
    path.write_bytes(workbook({'Time': [['Time', 'AI', 'AV'], [0, 1, 5], [1, None, 5], [2, 3, 5], [3, 4, 5]]}))
    result = research_transport.analyze(path, {'channels': [CHANNEL], 'windows': [
        {'label': 'Gapped', 'kind': 'control', 'start': 0, 'end': 3},
        {'label': 'Contiguous', 'kind': 'control', 'start': 2, 'end': 3}]})
    gapped, valid = result['sheets'][0]['windows']
    assert gapped['integrated_signed_current_c'] is None
    assert 'gaps' in gapped['integration_reason']
    assert valid['integrated_signed_current_c'] == pytest.approx(3.5)
    assert valid['source_rows'] == [4, 5]
    assert result['sheets'][0]['observations'][1]['reason'].startswith('Missing')
    assert all(branch['fit'] is None for branch in result['sheets'][0]['branches'])


def test_omitted_excel_rows_retain_source_coordinates_and_break_traces(storage):
    client, _ = storage
    original = workbook({'Sweep': [['AV', 'AI'], [0, 0], [1, 2], [2, 4], [3, 6]]})
    output = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(original)) as source, zipfile.ZipFile(output, 'w') as target:
        for name in source.namelist():
            data = source.read(name)
            if name.endswith('sheet1.xml'):
                # Remove row 3 entirely, as Excel does for an empty row.
                import re
                data = re.sub(rb'<row r="3">.*?</row>', b'', data)
            target.writestr(name, data)
    record, data, result = analyze(client, output.getvalue())
    sheet = result['sheets'][0]
    assert [branch['source_rows'] for branch in sheet['branches']] == [[2], [4, 5]]
    assert sheet['observations'][1]['source_row'] == 3
    assert sheet['observations'][1]['x'] is None
    assert_raw(record, data, 'transport.xlsx', MIME)


def test_geometry_requires_confirmed_measurement_and_propagates_independent_uncertainty():
    fit = transport_methods.linear_fit([[0, .1], [1, 2.05], [2, 3.9], [3, 6.05]])
    options = {'units_confirmed': True, 'wiring_confirmed': True, 'measurement_configuration': 'four_terminal',
               'geometry_model': 'rectangular_bar', 'length_m': .01, 'width_m': .002, 'thickness_m': 1e-6,
               'current_relative_standard_uncertainty': .01, 'voltage_relative_standard_uncertainty': .02,
               'length_standard_uncertainty_m': 1e-4, 'width_standard_uncertainty_m': 2e-5,
               'thickness_standard_uncertainty_m': 1e-8}
    transport_methods.interpret_fit(fit, CHANNEL, 'AV', options)
    expected_rho = fit['resistance_ohm'] * .002 * 1e-6 / .01
    assert fit['resistivity_ohm_m'] == pytest.approx(expected_rho)
    expected_relative = np.sqrt((fit['resistance_standard_uncertainty_ohm'] / fit['resistance_ohm']) ** 2 + .01 ** 2 + .02 ** 2 + 3 * .01 ** 2)
    assert fit['resistivity_standard_uncertainty_ohm_m'] == pytest.approx(expected_rho * expected_relative)
    invalid = transport_methods.linear_fit([[0, 0], [1, 2], [2, 4]])
    transport_methods.interpret_fit(invalid, CHANNEL, 'AV', {**options, 'measurement_configuration': 'two_terminal'})
    assert invalid['resistivity_ohm_m'] is None


def test_complete_package_replays_offline_and_preserves_every_source_byte(storage):
    from openpyxl import load_workbook

    client, root = storage
    source, source_bytes, result = analyze(client)
    identity = result['processing_id']
    response = client.get(f'/research/analyses/{identity}/export')
    assert response.status_code == 200, response.text
    assert response.headers['cache-control'] == 'no-store'
    destination = root / 'package'
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        manifest = json.loads(archive.read('manifest.json'))
        assert set(manifest['files']) == set(archive.namelist()) - {'manifest.json'}
        for name, digest in manifest['files'].items():
            content = archive.read(name)
            assert hashlib.sha256(content).hexdigest() == digest
            assert len(content) == manifest['size_bytes'][name]
        assert archive.read('sources/source-1.xlsx') == source_bytes
        run = database.get_processing_run(identity)
        assert archive.read('result.json') == Path(run['result_path']).read_bytes()
        assert hashlib.sha256(archive.read('result.json')).hexdigest() == run['result_sha256']
        assert {'report.pdf', 'report.html', 'analysis.xlsx', 'references.bib', 'analysis.ipynb',
                'code/research_transport.py', 'code/transport_methods.py', 'recipe.json', 'reproduce.py'} <= set(archive.namelist())
        pdf = PdfReader(io.BytesIO(archive.read('report.pdf')))
        text = '\n'.join(page.extract_text() for page in pdf.pages)
        assert 'Sxy/Sxx' in text and '95% slope interval' in text and 'Method references' in text
        assert source['sha256'] in text.replace('\n', '')
        book = load_workbook(io.BytesIO(archive.read('analysis.xlsx')))
        assert book['worksheet-1']['D2'].value == '=1+1'
        assert book['worksheet-1']['D2'].data_type == 's'
        assert book['parameters']['I2'].value == pytest.approx(result['sheets'][0]['branches'][0]['fit']['slope'])
        assert book['parameters'].freeze_panes == 'A2'
        assert "'=1+1" in archive.read('tables/worksheet-1.csv').decode()
        archive.extractall(destination)
    assert not (destination / 'code' / 'database.py').exists()
    completed = replay(destination)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert json.loads((destination / 'reproduced' / 'verification.json').read_text())['status'] == 'passed'
    assert_raw(source, source_bytes, 'transport.xlsx', MIME)


def test_export_preview_catalog_and_individual_files_share_saved_run(storage):
    client, _ = storage
    _, _, result = analyze(client)
    root = f'/research/analyses/{result["processing_id"]}/export'
    listing = client.get(root + '/files')
    assert listing.status_code == 200
    files = listing.json()['files']
    for name in ['report.html', 'report.pdf', 'analysis.xlsx', 'tables/observations.csv', 'figures/s1-b1.svg']:
        assert name in [file['name'] for file in files]
        response = client.get(root + '/file', params={'name': name, 'inline': True})
        assert response.status_code == 200, response.text
        if name == 'report.html':
            assert 'inline;' in response.headers['content-disposition']
            assert 'data:image/svg+xml;base64,' in response.text
            assert "default-src 'none'" in response.headers['content-security-policy']
            assert '<script' not in response.text
    assert client.get(root + '/file', params={'name': '../../catalog.db'}).status_code == 404
    assert client.get(root + '?format=report').content.startswith(b'%PDF')
    assert client.get(root + '?format=unknown').status_code == 422
    assert client.get('/research/analyses/no-such-run/export/files').status_code == 404


def test_replay_detects_source_tampering_and_calculation_differences(storage):
    client, root = storage
    _, _, result = analyze(client)
    exported = client.get(f'/research/analyses/{result["processing_id"]}/export')
    with zipfile.ZipFile(io.BytesIO(exported.content)) as archive:
        archive.extractall(root / 'replay')
    folder = root / 'replay'
    source = folder / 'sources' / 'source-1.xlsx'
    original = source.read_bytes()
    source.write_bytes(original + b'changed')
    checked = replay(folder, '--verify-only')
    assert checked.returncode != 0 and 'Checksum mismatch' in checked.stderr
    source.write_bytes(original)
    expected = folder / 'calculation.json'
    data = json.loads(expected.read_text())
    data['sheets'][0]['branches'][0]['fit']['slope'] += 1
    expected.write_text(json.dumps(data), encoding='utf-8')
    manifest = json.loads((folder / 'manifest.json').read_text())
    manifest['files']['calculation.json'] = hashlib.sha256(expected.read_bytes()).hexdigest()
    (folder / 'manifest.json').write_text(json.dumps(manifest))
    checked = replay(folder)
    assert checked.returncode != 0 and '.fit.slope' in checked.stderr


def test_photodetector_package_replays_both_sources_and_keeps_missing_results(storage):
    client, root = storage
    dark, light, dark_bytes, light_bytes = imported_pair(client)
    response = client.post('/research/photodetector/analyze', json=request_for(dark, light))
    assert response.status_code == 200, response.text
    result = response.json()
    exported = client.get(f'/research/analyses/{result["processing_id"]}/export')
    assert exported.status_code == 200, exported.text
    with zipfile.ZipFile(io.BytesIO(exported.content)) as archive:
        assert archive.read('sources/source-1.xlsx') == light_bytes
        assert archive.read('sources/source-2.xlsx') == dark_bytes
        assert 'dark-source-observations.csv' in '\n'.join(archive.namelist())
        archive.extractall(root / 'photo')
    checked = replay(root / 'photo')
    assert checked.returncode == 0, checked.stdout + checked.stderr
    assert_raw(dark, dark_bytes, dark['original_filename'], MIME)
    assert_raw(light, light_bytes, light['original_filename'], MIME)


def test_source_tamper_blocks_all_export_routes(storage):
    client, _ = storage
    source, _, result = analyze(client)
    Path(source['storage_path']).write_bytes(b'tampered fixture')
    root = f'/research/analyses/{result["processing_id"]}/export'
    for suffix in ['', '/files', '/file?name=report.html', '?format=report']:
        assert client.get(root + suffix).status_code == 409


def test_new_settings_create_new_run_and_legacy_results_stay_readable(storage):
    client, _ = storage
    source, _, result = analyze(client)
    previous = Path(database.get_processing_run(result['processing_id'])['result_path']).read_bytes()
    changed = client.post(f'/research/files/{source["file_id"]}/analyze', json={'technique': 'transport', 'options': {'channels': [CHANNEL], 'fit_window': [-1, 2]}}).json()
    assert changed['processing_id'] != result['processing_id']
    assert Path(database.get_processing_run(result['processing_id'])['result_path']).read_bytes() == previous
    history = client.get(f'/research/files/{source["file_id"]}/analyses').json()
    assert {r['processing_id'] for r in history} == {changed['processing_id'], result['processing_id']}
    legacy = {**result}
    legacy.pop('implementation')
    with pytest.raises(ValueError, match='predates'):
        transport_exports.verify_result_sources(legacy, main.RAW_DATA_DIR)
