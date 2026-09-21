"""Portable, vector-figure lab reports and browser-printable PDF layouts."""
import csv
import hashlib
import io
import json
import math
import zipfile
from html import escape

import photodetector


def number(value):
    return f'{value:.5g}' if isinstance(value, (int, float)) and math.isfinite(value) else 'Not available'


def chart(title, series, left_unit, right_unit=None, window=None):
    def valid(point):
        return all(isinstance(value, (int, float)) and math.isfinite(value) for value in point)
    usable = [(label, points, color, right) for label, points, color, right in series if any(valid(point) for point in points)]
    if not usable:
        return f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 900 160" role="img" aria-label="{escape(title)}"><rect width="900" height="160" fill="white"/><text x="40" y="55" font-family="sans-serif" font-size="18">{escape(title)}</text><text x="40" y="100" font-family="sans-serif" font-size="14">No valid values. Review the missing inputs and fit requirements.</text></svg>'
    finite = [point for _, points, _, _ in usable for point in points if valid(point)]
    xmin, xmax = min(point[0] for point in finite), max(point[0] for point in finite)
    if xmin == xmax:
        xmin -= 1
        xmax += 1
    ranges = {}
    for right in [False, True]:
        values = [point[1] for _, points, _, axis in usable if axis == right for point in points if valid(point)]
        low, high = (min(values), max(values)) if values else (0, 1)
        pad = (high - low) * .05 or max(abs(high) * .05, 1e-12)
        ranges[right] = low - pad, high + pad
    x = lambda value: 100 + (value - xmin) / (xmax - xmin) * 680
    def y(value, right):
        low, high = ranges[right]
        return 330 - (value - low) / (high - low) * 240
    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 900 420" role="img" aria-label="{escape(title)}"><rect width="900" height="420" fill="white"/><g font-family="sans-serif" font-size="12" fill="#243c38"><text x="100" y="25" font-size="18">{escape(title)}</text>']
    if window:
        start, stop = max(xmin, window[0]), min(xmax, window[1])
        if stop > start:
            parts.append(f'<rect x="{x(start):.3f}" y="90" width="{x(stop)-x(start):.3f}" height="240" fill="#eaf2da"/><text x="{x(start)+4:.3f}" y="108">Fit window</text>')
    for index in range(5):
        xv = xmin + (xmax - xmin) * index / 4
        parts.append(f'<path d="M{x(xv):.3f} 90V330" stroke="#e5ebe8"/><text x="{x(xv):.3f}" y="352" text-anchor="middle">{number(xv)}</text>')
        for right in [False, True] if right_unit else [False]:
            low, high = ranges[right]
            value = low + (high - low) * index / 4
            parts.append(f'<text x="{790 if right else 90}" y="{y(value,right)+4:.3f}" text-anchor="{"start" if right else "end"}">{number(value)}</text>')
    parts.append(f'<path d="M100 90V330H780V90" fill="none" stroke="#82968d"/><text x="440" y="385" text-anchor="middle">Bias voltage (V)</text><text transform="translate(19 215) rotate(-90)" text-anchor="middle">{escape(left_unit)}</text>')
    if right_unit:
        parts.append(f'<text transform="translate(886 215) rotate(-90)" text-anchor="middle">{escape(right_unit)}</text>')
    for index, (label, points, color, right) in enumerate(usable):
        dash = ' stroke-dasharray="6 4"' if right or 'fit' in label.lower() else ''
        path, drawing = [], False
        for point in points:
            if not valid(point):
                drawing = False
                continue
            a, b = point
            path.append(f'{"L" if drawing else "M"}{x(a):.3f},{y(b,right):.3f}')
            drawing = True
        parts.append(f'<path d="{" ".join(path)}" fill="none" stroke="{color}" stroke-width="2"{dash}/>')
        for a, b in points:
            if not valid([a, b]):
                continue
            parts.append(f'<circle cx="{x(a):.3f}" cy="{y(b,right):.3f}" r="3" fill="{color}" fill-opacity="0"><title>{escape(label)}: {number(a)} V, {number(b)} {escape(right_unit if right else left_unit)}</title></circle>')
        parts.append(f'<text x="{100+index*235}" y="60" fill="{color}">{escape(label)} ({"right" if right else "left"} axis)</text>')
    return ''.join(parts) + '</g></svg>'


def figures(result):
    rows = result['rows']
    def points(column):
        return [[row['voltage_v'], row[column]] for row in rows]
    diode = result['diode']
    return {
        'iv': chart('Dark, illuminated and photocurrent', [
            ('Dark', points('dark_a'), '#334155', False), ('Light', points('light_a'), '#c56b19', False),
            ('Photocurrent', points('photocurrent_a'), '#007f73', True)], 'Current (A)', 'Photocurrent (A)'),
        'response': chart('Responsivity and detectivity vs bias', [
            ('Responsivity', points('responsivity_a_w'), '#007f73', False),
            ('Detectivity', points('detectivity_jones'), '#7653ad', True)], 'Responsivity (A/W)', 'Detectivity (Jones)'),
        'ratio': chart('Current on/off ratio', [('On/off ratio', points('on_off_ratio'), '#c56b19', False)], '|I_light| / |I_dark|'),
        'diode': chart('Forward dark-current fit', [('ln |Dark current|', diode['points'], '#334155', False),
            ('Linear fit', diode['fit_points'], '#bf4545', False)], 'ln(|I| / 1 A)', window=diode.get('fit_window_v')),
    }


def report_html(result, offline=False):
    fig = figures(result)
    controls = '<p>Use your browser Print command to save this report as PDF.</p>' if offline else '<button id="photo-print-report" type="button">Print / Save as PDF</button>'
    methodology = [
        'User-confirmed dark and illuminated branches from the same device are compared. The selected direction, channel scales and polarity are retained in result.json.',
        'Each branch is split at missing observations and voltage reversals. Repeated voltages are averaged within that branch. Both currents are linearly interpolated onto the union of their voltage grids, limited to their overlap; no extrapolation is used.',
        'I_ph = I_light - I_dark. R = |I_ph| / P_incident. For uniform illumination, P_incident = irradiance × illuminated active area. On/off = |I_light| / |I_dark| above the declared current floor.',
        'D* = R sqrt(A) / i_noise, with A in cm² and noise spectral density in A/sqrt(Hz). If explicitly selected, the shot-noise approximation uses sqrt(2 q |I_dark|); this omits other noise sources.',
        'Monochromatic EQE (%) = 100 R h c / (q wavelength). Broadband presets do not supply a monochromatic wavelength.',
        'Optional thermionic model: ln|I| = slope × V_forward + intercept. n = q/(k T slope) and I_s = exp(intercept). At least four original dark observations, R² ≥ 0.95 and slope × V_min ≥ 3 are required.',
        'Apparent barrier (eV) = (kT/q) ln(A_contact A_Richardson T² / I_s). Contact area and the material-specific Richardson constant must be supplied separately. This model does not prove the dominant transport mechanism.',
    ]
    metadata = json.dumps({'sources': result['sources'], 'parameters': result['parameters'], 'normalized_inputs': result['normalized_inputs'],
        'processing_id': result['processing_id'], 'model_version': result['model_version'], 'processed_at': result['processed_at'],
        'result_sha256': result.get('result_sha256'), 'environment': result.get('environment')}, indent=2, ensure_ascii=False)
    cards = ''.join(f'<tr><th>{escape(key.replace("_", " "))}</th><td>{number(value)}</td></tr>' for key, value in result['summary'].items())
    return f'''<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>Photodetector lab report</title>
<style>body{{font:15px system-ui,sans-serif;max-width:1000px;margin:30px auto;padding:0 24px;color:#203a34}}h1,h2{{color:#174f46}}table{{border-collapse:collapse;width:100%}}th,td{{padding:8px;border:1px solid #cddbd6;text-align:left}}th{{width:60%}}svg{{width:100%;height:auto}}.figure{{break-inside:avoid}}pre{{white-space:pre-wrap;overflow-wrap:anywhere;font-size:11px}}li{{margin:8px 0}}@media print{{@page{{size:A4 landscape;margin:15mm}}body{{max-width:none;margin:0;padding:0;font-size:11pt}}button{{display:none}}.figure{{break-before:page}}h2{{break-after:avoid}}a{{color:inherit}}}}</style></head><body>
{controls}<h1>Photodetector characterization report</h1><p>{escape(photodetector.explain(result, 'summary'))}</p>
<h2>Extracted parameters</h2><table>{cards}</table><p>Units appear in parameter names. Full peak bias locations and zero-bias values are recorded below.</p>
<pre>{escape(json.dumps(result['metrics'], indent=2, ensure_ascii=False))}</pre>
<h2>Inputs still needed</h2><ul>{''.join(f'<li>{escape(value)}</li>' for value in result['missing'].values()) or '<li>All enabled calculations have their required inputs.</li>'}</ul>
<h2>Interpretation and limitations</h2><ul>{''.join(f'<li>{escape(value)}</li>' for value in result['warnings'])}</ul><p>{escape(result['diode'].get('interpretation') or result['diode']['reason'])}</p>
{''.join(f'<section class="figure">{svg}</section>' for svg in fig.values())}
<h2>Methodology</h2><ol>{''.join(f'<li>{escape(value)}</li>' for value in methodology)}</ol>
<h2>Source metadata and reproducibility</h2><pre>{escape(metadata)}</pre>
<h2>Method references</h2><ul>{''.join(f'<li><a href="{escape(ref["url"])}">{escape(ref["title"])}</a></li>' for ref in result['references'])}</ul>
{'' if offline else '<script src="/assets/photodetector.js"></script>'}</body></html>'''


def export_result(result):
    files = {'report.html': report_html(result, offline=True).encode('utf-8'),
             'result.json': json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False).encode('utf-8')}
    for name, svg in figures(result).items():
        files[f'figures/{name}.svg'] = svg.encode('utf-8')
    output = io.StringIO(newline='')
    writer = csv.DictWriter(output, fieldnames=list(result['rows'][0]))
    writer.writeheader()
    writer.writerows(result['rows'])
    files['matched-measurements.csv'] = output.getvalue().encode('utf-8')
    for role, points in result['raw_selected_branches'].items():
        output = io.StringIO(newline='')
        writer = csv.writer(output)
        writer.writerow(['scaled_voltage_v', 'scaled_current_a'])
        writer.writerows(points)
        files[f'{role}-selected-branch.csv'] = output.getvalue().encode('utf-8')
    files['README.txt'] = b'Open report.html in any browser. Use Print / Save as PDF for a PDF copy. Vector SVG figures can be imported into publications. result.json retains inputs, units, assumptions, environment, file IDs and original source hashes. CSV branch values include the selected scales and polarity; source workbooks are immutable and remain in managed storage.\n'
    files['manifest.json'] = json.dumps({name: hashlib.sha256(data).hexdigest() for name, data in files.items()}, indent=2).encode()
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as archive:
        for name, data in files.items():
            archive.writestr(name, data)
    return buffer.getvalue()
