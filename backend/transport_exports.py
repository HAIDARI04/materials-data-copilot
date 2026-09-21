"""Portable electrical-analysis packages with byte-exact sources and replay code."""
import csv
import hashlib
import io
import json
import re
from pathlib import Path
import zipfile

import database
import preview
import transport_documents
import transport_figures
from transport_report_model import data_tables, report_model, number


EXPORT_VERSION = '1.0.0'
TOLERANCES = {'relative': 1e-10, 'absolute': 1e-30}


def encoded(value):
    return json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True, allow_nan=False).encode('utf-8')


def verify_result_sources(result, raw):
    sources = result.get('sources', [])
    if not sources or not result.get('implementation') or not result.get('calculation_keys'):
        raise ValueError('This saved run predates complete exports. Use Analyze and save selected to create a new reproducible run; the earlier result remains unchanged.')
    data = {}
    for source in sources:
        record = database.get_imported_file(source['file_id'])
        if not record or record['sha256'] != source['sha256']:
            raise ValueError('Export source provenance mismatch.')
        path, _ = preview.verify_stored_file(record, raw)
        content = path.read_bytes()
        if len(content) != source['size_bytes'] or hashlib.sha256(content).hexdigest() != source['sha256']:
            raise ValueError('Source changed while preparing export.')
        package_path = source['package_path']
        if not re.fullmatch(r'sources/source-\d+\.[a-z0-9]+', package_path):
            raise ValueError('Unsafe source package path.')
        data[package_path] = content
    implementation = result['implementation']
    for name, source in implementation['files'].items():
        if not re.fullmatch(r'[a-z_]+\.py', name) or hashlib.sha256(source.encode()).hexdigest() != implementation['file_sha256'][name]:
            raise ValueError('Saved implementation checksum mismatch.')
    if hashlib.sha256(implementation['replay'].encode()).hexdigest() != implementation['replay_sha256']:
        raise ValueError('Saved replay implementation checksum mismatch.')
    return data


def csv_bytes(rows):
    output = io.StringIO(newline='')
    writer = csv.writer(output)
    for row in rows:
        cells = []
        for value in row:
            if isinstance(value, (dict, list)):
                value = json.dumps(value, ensure_ascii=False, allow_nan=False)
            if isinstance(value, str) and value.lstrip().startswith(('=', '+', '-', '@')):
                value = "'" + value
            cells.append(value)
        writer.writerow(cells)
    return output.getvalue().encode('utf-8')


def bibliography(references):
    entries = []
    for index, ref in enumerate(references, 1):
        key = ref.get('id', f'reference-{index}')
        fields = {field: ref[field] for field in ['title', 'author', 'year', 'doi', 'url'] if ref.get(field)}
        fields['note'] = ref.get('section', 'Method reference')
        entries.append('@misc{' + key + ',\n' + ',\n'.join(f'  {field} = {{{str(value).replace("{", "").replace("}", "")}}}' for field, value in fields.items()) + '\n}')
    return ('\n\n'.join(entries) + '\n').encode('utf-8')


def notebook(model):
    cells = [{'cell_type': 'markdown', 'metadata': {}, 'source': [
        '# Reproduce the saved Transport analysis\n',
        'Start Jupyter in the extracted package folder after setting up the environment described in README.md. This notebook uses the same packaged analysis code as reproduce.py.\n']}]
    def code(source):
        cells.append({'cell_type': 'code', 'metadata': {}, 'execution_count': None, 'outputs': [], 'source': source.splitlines(keepends=True)})
    code("from pathlib import Path\nimport json, subprocess, sys\nroot = Path.cwd()\nassert (root / 'recipe.json').is_file(), 'Open this notebook from the extracted package folder.'\nsubprocess.run([sys.executable, 'reproduce.py', '--verify-only'], check=True)\n")
    code("recipe = json.loads((root / 'recipe.json').read_text(encoding='utf-8'))\nrecipe['sources'], recipe['options'], recipe['environment']\n")
    for section in model['sections']:
        if section.get('steps') and section['title'] != 'Reproduce this analysis':
            cells.append({'cell_type': 'markdown', 'metadata': {}, 'source': ['## ' + section['title'] + '\n', *[f'{index}. {step}\n' for index, step in enumerate(section['steps'], 1)]]})
    code("subprocess.run([sys.executable, 'reproduce.py'], check=True)\nverification = json.loads((root / 'reproduced' / 'verification.json').read_text())\nverification\n")
    code("calculation = json.loads((root / 'reproduced' / 'calculation.json').read_text(encoding='utf-8'))\ncalculation['summary']\n")
    code("import csv\nwith (root / 'tables' / 'parameters.csv').open(encoding='utf-8', newline='') as handle:\n    parameters = list(csv.DictReader(handle))\nparameters[:10]\n")
    return encoded({'nbformat': 4, 'nbformat_minor': 5, 'metadata': {'kernelspec': {'display_name': 'Python 3', 'language': 'python', 'name': 'python3'}},
                    'cells': [{**cell, 'id': f'cell-{index}'} for index, cell in enumerate(cells)]})


def recipe(result):
    return {'schema_version': 1, 'processing_id': result['processing_id'],
            'technique': result['model_name'].removeprefix('research_'), 'model_version': result['model_version'],
            'options': result['parameters']['options'], 'sources': result['sources'],
            'environment': result['replay_environment'], 'tolerances': TOLERANCES,
            'implementation_sha256': result['implementation']['sha256']}


def portable_files(result, original_result, sources):
    """Reproduction does not depend on the document-rendering packages."""
    files = {**sources, 'result.json': original_result,
             'calculation.json': encoded({key: result[key] for key in result['calculation_keys']}),
             'recipe.json': encoded(recipe(result)),
             'reproduce.py': result['implementation']['replay'].encode('utf-8'),
             'requirements.txt': ('\n'.join(f'{name}=={version}' for name, version in result['replay_environment']['packages'].items()) + '\n').encode()}
    files.update({f'code/{name}': source.encode('utf-8') for name, source in result['implementation']['files'].items()})
    return files


def catalog(result):
    assets = [{'name': 'report.html', 'label': 'Offline report', 'media_type': 'text/html'},
              {'name': 'report.pdf', 'label': 'PDF report', 'media_type': 'application/pdf'},
              {'name': 'analysis.xlsx', 'label': 'Excel tables', 'media_type': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'}]
    for name in data_tables(result):
        assets.append({'name': f'tables/{name}.csv', 'label': name.replace('-', ' '), 'media_type': 'text/csv'})
    for spec in transport_figures.specifications(result):
        for fmt, mime in [('svg', 'image/svg+xml'), ('pdf', 'application/pdf'), ('png', 'image/png')]:
            assets.append({'name': f'figures/{spec["id"]}.{fmt}', 'label': f'{spec["title"]} ({fmt.upper()})', 'media_type': mime})
    return assets


def asset_bytes(result, name):
    allowed = {asset['name']: asset for asset in catalog(result)}
    if name not in allowed:
        raise ValueError('Choose a file listed in this analysis export catalog.')
    if name == 'analysis.xlsx':
        return transport_documents.workbook_bytes(result, data_tables(result))
    if name.startswith('tables/'):
        return csv_bytes(data_tables(result)[name[7:-4]])
    specs = transport_figures.specifications(result)
    if name.startswith('figures/'):
        stem, fmt = name[8:].rsplit('.', 1)
        spec = next(spec for spec in specs if spec['id'] == stem)
        return transport_figures.render(spec, fmt, result['source_sha256'])
    model = report_model(result)
    fmt = 'svg' if name.endswith('.html') else 'png'
    figures = [(spec, transport_figures.render(spec, fmt, result['source_sha256'])) for spec in specs]
    return transport_documents.html_report(model, figures) if fmt == 'svg' else transport_documents.pdf_report(model, figures)


def build_files(result, original_result, source_bytes, mode='complete'):
    if mode not in {'complete', 'figures'}:
        raise ValueError('Unknown export package type.')
    tables = data_tables(result)
    files = {f'tables/{name}.csv': csv_bytes(rows) for name, rows in tables.items()}
    files['analysis.xlsx'] = transport_documents.workbook_bytes(result, tables)
    rendered = []
    for spec in transport_figures.specifications(result):
        formats = {fmt: transport_figures.render(spec, fmt, result['source_sha256']) for fmt in ['svg', 'pdf', 'png']}
        for fmt, content in formats.items():
            files[f'figures/{spec["id"]}.{fmt}'] = content
        rendered.append((spec, formats))
    if mode == 'complete':
        model = report_model(result)
        files.update(portable_files(result, original_result, source_bytes))
        files['report.html'] = transport_documents.html_report(model, [(spec, formats['svg']) for spec, formats in rendered])
        files['report.pdf'] = transport_documents.pdf_report(model, [(spec, formats['png']) for spec, formats in rendered])
        files['report-model.json'] = encoded(model)
        files['references.bib'] = bibliography(model['references'])
        files['references.json'] = encoded(model['references'])
        files['references.md'] = ('# Method references\n\n' + '\n\n'.join(f'[{ref.get("id", "reference")}] {ref["title"]}. {ref.get("section", "")}\n{ref["url"]}' for ref in model['references'])).encode()
        files['analysis.ipynb'] = notebook(model)
        files['data-dictionary.json'] = encoded({
            'tables': {name: rows[0] for name, rows in tables.items()},
            'conventions': {'source_row': '1-based original worksheet row, including the header row',
                'blank': 'Unavailable or not applicable; never silently converted to zero',
                'numeric_precision': 'CSV and JSON retain saved float precision; Excel and reports format display values',
                'units': 'A, V, s, ohm, ohm m, S/m unless a column explicitly names another unit',
                'original_values': 'Unmodified workbook bytes in sources/. Parsed values in result.json. CSV strings starting with formula characters are prefixed with an apostrophe for spreadsheet safety.',
                'branch_ids': 'A source turnaround row can belong to two neighboring branches; do not sum branch counts as independent measurements.',
                'uncertainty': 'Standard errors are fit-only. 95% intervals use Student t with N-2 degrees of freedom. Combined geometry uncertainty is named separately.',
                'source_selected_branches': 'Photodetector selected-branch tables contain scaled and duplicate-averaged values. Source-observation tables retain row-level original values.'}})
        files['README.md'] = _readme(result).encode('utf-8')
        # Retain former public filenames for scripts using existing exports.
        for name, content in list(files.items()):
            if name.startswith('tables/worksheet-') or name in {'tables/matched-measurements.csv', 'tables/dark-selected-branch.csv', 'tables/light-selected-branch.csv'}:
                files[name.removeprefix('tables/')] = content
        if rendered:
            files['figure.svg'] = rendered[0][1]['svg']
        files['report.md'] = ('# ' + model['title'] + '\n\n' + '\n\n'.join('## ' + section['title'] + '\n\n' + '\n\n'.join(section['paragraphs'] + section.get('steps', [])) for section in model['sections'])).encode()
    else:
        files['README.md'] = b'Figures and tables from a saved analysis. Download the complete analysis package for original sources, the scientific report, references and executable reproduction.\n'
    manifest = {'schema_version': 1, 'export_version': EXPORT_VERSION, 'mode': mode,
                'processing_id': result['processing_id'], 'processed_at': result['processed_at'],
                'source_sha256': result['source_sha256'], 'result_sha256': result['result_sha256'],
                'files': {name: hashlib.sha256(content).hexdigest() for name, content in files.items()},
                'size_bytes': {name: len(content) for name, content in files.items()},
                'checksum_note': 'The manifest covers every other file. It cannot hash itself. Checksums detect changes, not authorship.'}
    files['manifest.json'] = encoded(manifest)
    return files


def _readme(result):
    python = result['replay_environment']['python']
    return f'''# Complete Transport analysis

Open report.html in a browser without an internet connection, or read report.pdf.
Analysis: {result['processing_id']}
Recorded Python: {python}

## Reproduce on Windows

Extract this ZIP into a new folder and open a terminal in that folder.

```powershell
python reproduce.py --verify-only
py -{'.'.join(python.split('.')[:2])} -m venv .venv
.venv\\Scripts\\python.exe -m pip install -r requirements.txt
.venv\\Scripts\\python.exe reproduce.py
```

## Reproduce on macOS or Linux

```sh
python3 reproduce.py --verify-only
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python reproduce.py
```

Use the recorded Python version where possible. Installing dependencies requires
internet access or a prepopulated local package cache. Once installed, analysis
and report viewing run offline. Jupyter is optional and is not required by the
script; analysis.ipynb provides a walkthrough when Jupyter is available.

The replay imports code/ and recalculates from the byte-exact originals in sources/.
It requires the recorded scientific package versions and compares every field in
calculation.json. Integers, booleans, identifiers and text match exactly. Floats
use relative tolerance 1e-10 and absolute tolerance 1e-30 in their stored units.
Failures exit with a nonzero status and identify the mismatched file or field.
Successful output is written to reproduced/, leaving all package inputs intact.
The comparison checks calculations, not PDF/PNG binary identity or pagination.

## Files and interpretation

- report.pdf / report.html: methods, worked calculations, diagnostics and references.
- tables/: full-precision converted observations, selections, results and residuals.
- analysis.xlsx: a formatted snapshot of those tables; it does not recalculate the analysis.
- figures/: individually labeled SVG, PDF and 300-dpi PNG figures.
- recipe.json: resolved analysis settings, source identities and environment.
- result.json: the exact saved application result; checksum is in manifest.json.
- calculation.json: all numerical and descriptive outputs verified by replay.
- references.bib / references.md: citations for the implemented methods.
- data-dictionary.json: units, missing-value and row-selection conventions.
- manifest.json: SHA-256 and byte count of every other packaged file.

CSV strings that could execute as spreadsheet formulas have an apostrophe prefix.
The original strings and file bytes remain in result.json and sources/.
Unavailable quantities are blank in numeric CSV cells and explained in the report.
Fitted standard errors describe fit scatter. They do not include unprovided
instrument, calibration or geometry uncertainty. Refer to each result's scope.
'''


def export_package(run, result, raw, mode='complete'):
    sources = verify_result_sources(result, raw)
    original = Path(run['result_path']).read_bytes()
    if hashlib.sha256(original).hexdigest() != run['result_sha256']:
        raise ValueError('Saved result changed while preparing export.')
    files = build_files(result, original, sources, mode)
    output = io.BytesIO()
    with zipfile.ZipFile(output, 'w', zipfile.ZIP_DEFLATED) as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    return output.getvalue()
