"""Conservative PL/XRD fits. Reference weights are similarity coefficients."""
import json
import re

import numpy as np
from scipy import sparse
from scipy.optimize import curve_fit, nnls
from scipy.signal import find_peaks, peak_widths
from scipy.sparse.linalg import spsolve

import wdf_reader


def read_xy(path, ras=False):
    raw = path.read_bytes()
    warnings = []
    try:
        text = raw.decode('utf-8-sig')
    except UnicodeDecodeError:
        text = raw.decode('latin-1')
        warnings.append('Latin-1 fallback used; numeric rows preserved, header text needs review.')
    rows, metadata, active, blocks = [], [], not ras, 0
    for line in text.splitlines():
        if ras and line.startswith('*RAS_INT_START'):
            active = True
            blocks += 1
            continue
        if ras and line.startswith('*RAS_INT_END'):
            active = False
            continue
        tokens = re.split(r'[\s,;]+', line.strip())
        try:
            pair = [float(tokens[0]), float(tokens[1])]
        except (ValueError, IndexError):
            metadata.append(line)
            continue
        if active and all(np.isfinite(pair)):
            rows.append(pair)
    if blocks > 1:
        raise ValueError('Multiple RAS intensity blocks require separate exports; they will not be concatenated.')
    if len(rows) < 8 or len(rows) > 500_000:
        raise ValueError('A spectrum must contain 8 to 500,000 finite XY observations.')
    return rows, metadata, warnings


def baseline(y, strength, asymmetry):
    difference = sparse.diags([np.ones(len(y)-2), -2*np.ones(len(y)-2), np.ones(len(y)-2)], [0, 1, 2], shape=(len(y)-2, len(y)), format='csc')
    penalty = strength * (difference.T @ difference)
    weights = np.ones(len(y))
    for _ in range(15):
        fitted = spsolve(sparse.diags(weights, format='csc') + penalty, weights*y)
        weights = np.where(y > fitted, asymmetry, 1-asymmetry)
    return fitted


def gaussian(x, height, center, sigma, offset):
    return height * np.exp(-0.5*((x-center)/sigma)**2) + offset


def fit_spectrum(points, options):
    x, y = np.asarray(points, dtype=float).T
    if len(x) < 8 or not np.all(np.isfinite(x)) or not np.all(np.isfinite(y)):
        raise ValueError('Spectrum contains insufficient or non-finite data.')
    if np.all(np.diff(x) < 0):
        x, y = x[::-1], y[::-1]
    if not np.all(np.diff(x) > 0):
        raise ValueError('Spectrum axis must be strictly monotonic; repeated coordinates need an explicit aggregation choice.')
    selected = options.get('fit_window')
    if selected:
        keep = (x >= selected[0]) & (x <= selected[1])
        x, y = x[keep], y[keep]
    if len(x) < 8:
        raise ValueError('Selected window contains fewer than 8 points.')
    bg = baseline(y, options.get('baseline_lambda', 1e7), options.get('baseline_asymmetry', .001))
    corrected = y-bg
    noise = float(np.median(np.abs(np.diff(corrected)-np.median(np.diff(corrected)))) / .67448975 / np.sqrt(2))
    prominence = max(options.get('prominence_sigma', 5)*noise, float(np.ptp(corrected))*1e-6, 1e-12)
    peaks, props = find_peaks(corrected, prominence=prominence)
    widths = peak_widths(corrected, peaks)[0] if len(peaks) else []
    fits = []
    for peak, width in sorted(zip(peaks, widths), key=lambda pair: corrected[pair[0]], reverse=True)[:30]:
        half = max(4, int(np.ceil(width * 1.5)))
        left, right = max(0, peak-half), min(len(x), peak+half+1)
        xx, yy = x[left:right], corrected[left:right]
        sigma = max(float(np.median(np.diff(x))) / 2, float(width*np.median(np.diff(x))/2.354820045))
        record = {'observed_center': float(x[peak]), 'status': 'not_fitted'}
        try:
            parameters, covariance = curve_fit(gaussian, xx, yy, p0=[float(corrected[peak]), float(x[peak]), sigma, 0],
                bounds=([0, xx[0], np.min(np.diff(x))/4, -np.inf], [np.inf, xx[-1], x[-1]-x[0], np.inf]), maxfev=10000)
            residual = yy-gaussian(xx, *parameters)
            uncertainty = np.sqrt(np.diag(covariance))
            reliable = bool(np.all(np.isfinite(uncertainty)))
            record.update(status='fit' if reliable else 'uncertain', height=float(parameters[0]), center=float(parameters[1]),
                fwhm=float(2.354820045*parameters[2]), area=float(parameters[0]*parameters[2]*np.sqrt(2*np.pi)),
                center_standard_error=float(uncertainty[1]) if reliable else None,
                residual_rms=float(np.sqrt(np.mean(residual**2))), point_count=len(xx))
        except (ValueError, RuntimeError, FloatingPointError):
            record['status'] = 'fit_failed'
        fits.append(record)
    return {'points': np.column_stack([x,y,bg,corrected]).tolist(), 'columns': ['x','raw','baseline','corrected'],
            'peaks': sorted(fits, key=lambda p: p.get('center', p['observed_center'])), 'noise_estimate': noise,
            'warnings': ['Local Gaussian fits can be biased by overlapping peaks; fit errors exclude calibration and baseline uncertainty.']}


def analyze(path, technique, options, references):
    if path.suffix.lower() == '.wdf':
        source = wdf_reader.read_wdf(path, include_points=True)
        unit = source['x_axis']['unit']
        if technique == 'pl' and unit not in {'eV', 'nm'}:
            raise ValueError('This WDF contains Raman-shift data. Use the Raman workflow.')
        datasets = source['datasets']
        if options.get('dataset_index', 0) >= len(datasets):
            raise ValueError('WDF spectrum index is out of range.')
        dataset = datasets[options.get('dataset_index', 0)]
        points, metadata, warnings = dataset['points'], {k:v for k,v in dataset.items() if k != 'points'}, []
    else:
        points, metadata, warnings = read_xy(path, ras=path.suffix.lower()=='.ras')
        unit = 'degree_2theta' if technique == 'xrd' else options.get('axis_unit')
        if unit not in {'eV', 'nm', 'degree_2theta'}:
            raise ValueError('Specify eV or nm for a text PL spectrum.')
    result = fit_spectrum(points, options)
    result.update(axis_unit=unit, acquisition_metadata=metadata)
    result['warnings'].extend(warnings)
    result['reference_matches'] = []
    if references and technique != 'xrd':
        raise ValueError('Reference-pattern mixtures are available only for XRD.')
    if references:
        x, _, _, y = np.asarray(result['points']).T
        templates, labels = [], []
        for record, refpath in references:
            reference = json.loads(refpath.read_text(encoding='utf-8-sig'))
            pattern = reference.get('pattern', [])
            if not pattern or len(pattern) > 10000:
                raise ValueError('Reference requires a nonempty pattern of [intensity, hkl, 2theta, d].')
            template = np.zeros_like(x)
            for row in pattern:
                intensity, center = float(row[0]), float(row[2])
                if not np.isfinite(intensity+center) or intensity < 0:
                    raise ValueError('Invalid reference pattern.')
                template += intensity*np.exp(-.5*((x-center)/options.get('reference_sigma', .08))**2)
            norm = np.linalg.norm(template)
            if not norm:
                continue
            templates.append(template/norm)
            labels.append({'file_id': record['file_id'], 'sha256': record['sha256'], 'label': reference.get('chemical_formula', record['original_filename'])})
        if templates:
            target = np.maximum(y, 0)
            weights, residual = nnls(np.asarray(templates).T, target)
            for label, template, weight in zip(labels, templates, weights):
                result['reference_matches'].append({**label, 'coefficient': float(weight),
                    'relative_coefficient': float(weight/sum(weights)) if sum(weights) else 0,
                    'cosine_similarity': float(np.dot(target, template)/np.linalg.norm(target)) if np.linalg.norm(target) else 0})
            result['reference_residual_norm'] = float(residual)
        result['warnings'].append('Reference coefficients are pattern similarities, not quantitative phase fractions or proof of phase identity.')
    result['summary'] = {'peak_count': len(result['peaks']), 'point_count': len(result['points']), 'axis_unit': unit}
    return result
