"""Confirmed dark/light I-V recipes, with explicit optical and diode assumptions."""
import math
import re
from pathlib import Path
from typing import Literal

import numpy as np
from pydantic import BaseModel, Field, model_validator

import research_transport
import transport
import transport_methods

Q = 1.602176634e-19
KB = 1.380649e-23
H = 6.62607015e-34
C = 299792458.0
AREA_TO_CM2 = {'um2': 1e-8, 'mm2': .01, 'cm2': 1.0}
DENSITY_TO_W_CM2 = {'mW/cm2': .001, 'W/cm2': 1.0, 'W/m2': .0001}
REFERENCES = [
    {'id': 'hamamatsu-photodiodes', 'title': 'Hamamatsu: silicon photodiode technical note',
     'author': 'Hamamatsu Photonics', 'section': 'Photosensitivity, quantum efficiency and noise characteristics',
     'url': 'https://www.hamamatsu.com/content/dam/hamamatsu-photonics/sites/documents/99_SALES_LIBRARY/ssd/si_pd_kspd9001e.pdf'},
    {'id': 'si-constants', 'title': 'The International System of Units (SI)', 'author': 'BIPM',
     'section': 'The seven defining constants', 'url': 'https://www.bipm.org/en/publications/si-brochure'},
    {'id': 'schottky-ideality', 'title': 'Ideality factor in transport theory of Schottky barrier diodes',
     'url': 'https://arxiv.org/abs/1204.0335'},
]


class Recipe(BaseModel):
    model_config = {'extra': 'forbid', 'allow_inf_nan': False}
    dark_file_id: str
    light_file_id: str
    dark_trace: int = Field(default=0, ge=0)
    light_trace: int = Field(default=0, ge=0)
    same_device_confirmed: Literal[True]
    roles_units_confirmed: Literal[True]
    dark_current_scale: float = Field(default=1, gt=0, le=1e12)
    light_current_scale: float = Field(default=1, gt=0, le=1e12)
    dark_voltage_scale: float = Field(default=1, gt=0, le=1e12)
    light_voltage_scale: float = Field(default=1, gt=0, le=1e12)
    dark_polarity: Literal[-1, 1] = 1
    light_polarity: Literal[-1, 1] = 1
    area_value: float | None = Field(default=None, gt=0, le=1e16)
    area_unit: Literal['um2', 'mm2', 'cm2'] = 'um2'
    power_mode: Literal['density', 'direct'] = 'density'
    power_density: float | None = Field(default=None, gt=0, le=1e12)
    density_unit: Literal['mW/cm2', 'W/cm2', 'W/m2'] = 'mW/cm2'
    incident_power_w: float | None = Field(default=None, gt=0, le=1e12)
    wavelength_nm: float | None = Field(default=None, gt=0, le=1e7)
    monochromatic: bool = False
    noise_mode: Literal['none', 'shot', 'measured'] = 'none'
    noise_a_sqrt_hz: float | None = Field(default=None, gt=0)
    current_floor_a: float = Field(default=0, ge=0)
    temperature_k: float | None = Field(default=None, gt=0, le=5000)
    thermionic_confirmed: bool = False
    forward_sign: Literal[-1, 1] = 1
    fit_min_v: float | None = Field(default=None, gt=0)
    fit_max_v: float | None = Field(default=None, gt=0)
    contact_area_cm2: float | None = Field(default=None, gt=0, le=1e8)
    richardson_a_cm2_k2: float | None = Field(default=None, gt=0, le=1e8)
    notes: str = Field(default='', max_length=4000)

    @model_validator(mode='after')
    def consistent(self):
        if self.dark_file_id == self.light_file_id:
            raise ValueError('Choose two different files for dark and light.')
        if self.noise_mode == 'measured' and self.noise_a_sqrt_hz is None:
            raise ValueError('Enter measured noise spectral density, in A/sqrt(Hz).')
        if self.thermionic_confirmed:
            if self.temperature_k is None or self.fit_min_v is None or self.fit_max_v is None:
                raise ValueError('A diode fit needs temperature and both forward-voltage limits.')
            if self.fit_max_v <= self.fit_min_v:
                raise ValueError('The upper fit limit must exceed the lower limit.')
        return self


def suggest_pairs(files):
    groups = {}
    for record in files:
        name = record['original_filename']
        if Path(name).suffix.lower() not in {'.xls', '.xlsx'}:
            continue
        if 'transport' not in str(record.get('technique', '')).lower():
            continue
        stem = Path(name).stem.lower()
        match = re.search(r'(^|[\s_-])(dark|light|illuminated)(?=$|[\s_-])', stem)
        if not match:
            continue
        role = 'dark' if match[2] == 'dark' else 'light'
        key = re.sub(r'[\s_-]+', '-', stem[:match.start(2)] + stem[match.end(2):]).strip('-')
        groups.setdefault(key, {'dark': [], 'light': []})[role].append(record)
    pairs = []
    for group in groups.values():
        for dark in group['dark']:
            for light in group['light']:
                pairs.append({'dark_file_id': dark['file_id'], 'light_file_id': light['file_id'],
                    'dark_filename': dark['original_filename'], 'light_filename': light['original_filename'],
                    'reason': 'Matching filename apart from dark/light label. Confirm the device, channel, wiring and acquisition conditions.',
                    'sample_match': dark.get('sample_id') == light.get('sample_id')})
                if len(pairs) == 50:
                    return pairs
    return pairs


def read_traces(path):
    """Keep monotonic branches and missing-row gaps separate; never join acquisitions."""
    sheets, warnings = transport.read_workbook(path)
    traces = []
    for sheet in sheets:
        if sheet['name'].casefold() in {'settings', 'calc'} or not sheet['rows']:
            continue
        header_index = next((index for index, row in enumerate(sheet['rows']) if any(value not in (None, '') for value in row)), None)
        if header_index is None:
            continue
        headers = [str(value).strip() if value is not None else '' for value in sheet['rows'][header_index]]
        if len([h for h in headers if h]) != len(set(h for h in headers if h)):
            raise ValueError(f'Duplicate column names in {sheet["name"]}; channel selection would be ambiguous.')
        rows = [dict(zip(headers, row)) for row in sheet['rows'][header_index + 1:]]
        columns = [('AI', 'AV'), ('BI', 'BV'), ('DrainI', 'DrainV'), ('GateI', 'GateV'), ('SourceI', 'SourceV')]
        ranges = {}
        for _, voltage in columns:
            values = [row[voltage] for row in rows if research_transport.numeric(row.get(voltage))]
            ranges[voltage] = max(values) - min(values) if values else 0
        sweep = max(ranges, key=ranges.get)
        if ranges[sweep] <= 1e-9:
            continue
        for current, own_voltage in columns:
            if current not in headers:
                continue
            voltage = own_voltage if ranges[own_voltage] > 1e-9 else sweep
            blocks = []
            for row_index, row in enumerate(rows, header_index + 2):
                if not all(research_transport.numeric(row.get(column)) for column in [current, voltage]):
                    continue
                if not blocks or row_index != blocks[-1][-1][2] + 1:
                    blocks.append([])
                blocks[-1].append([float(row[voltage]), float(row[current]), row_index])
            for block in blocks:
                for start, stop in research_transport.segments([point[0] for point in block]):
                    branch = block[start:stop]
                    if len({point[0] for point in branch}) < 3:
                        continue
                    traces.append({'index': len(traces), 'sheet': sheet['name'], 'current': current,
                        'voltage': voltage, 'direction': 'increasing' if branch[-1][0] > branch[0][0] else 'decreasing',
                        'first_row': branch[0][2], 'last_row': branch[-1][2], 'point_count': len(branch),
                        'source_observations': [{'source_row': point[2], 'stored_voltage': point[0], 'stored_current': point[1]} for point in branch],
                        'voltage_min': min(point[0] for point in branch), 'voltage_max': max(point[0] for point in branch),
                        'points': [point[:2] for point in branch]})
    return traces, warnings


def finite(value):
    return float(value) if value is not None and math.isfinite(value) else None


def selected_trace(path, index, current_scale, voltage_scale, polarity):
    traces, warnings = read_traces(path)
    if index >= len(traces):
        raise ValueError('Selected I-V branch is unavailable. I-t files need a time-response recipe; choose an I-V sweep here.')
    trace = traces[index]
    grouped = {}
    for voltage, current in trace['points']:
        grouped.setdefault(voltage * voltage_scale, []).append(current * current_scale * polarity)
    if len(grouped) != len(trace['points']):
        warnings.append('Repeated voltages were averaged within the selected branch only.')
    points = sorted((voltage, math.fsum(currents) / len(currents)) for voltage, currents in grouped.items())
    if not all(math.isfinite(value) for point in points for value in point):
        raise ValueError('Channel scaling produced non-finite values.')
    return {key: value for key, value in trace.items() if key != 'points'}, np.asarray(points), warnings


def analyze(light_path, dark_path, options):
    dark_trace, dark, dark_warnings = selected_trace(dark_path, options['dark_trace'], options['dark_current_scale'], options['dark_voltage_scale'], options['dark_polarity'])
    light_trace, light, light_warnings = selected_trace(light_path, options['light_trace'], options['light_current_scale'], options['light_voltage_scale'], options['light_polarity'])
    if dark_trace['direction'] != light_trace['direction']:
        raise ValueError('Select matching sweep directions for dark and light; hysteresis branches cannot be paired automatically.')
    low, high = max(dark[0, 0], light[0, 0]), min(dark[-1, 0], light[-1, 0])
    grid = np.unique(np.concatenate([dark[:, 0], light[:, 0]]))
    grid = grid[(grid >= low) & (grid <= high)]
    if len(grid) < 3 or high <= low:
        raise ValueError('The selected branches need at least three overlapping voltage points.')
    dark_i, light_i = np.interp(grid, dark[:, 0], dark[:, 1]), np.interp(grid, light[:, 0], light[:, 1])
    area = options.get('area_value')
    area = area * AREA_TO_CM2[options['area_unit']] if area else None
    density = options.get('power_density')
    density = density * DENSITY_TO_W_CM2[options['density_unit']] if density else None
    power = options.get('incident_power_w') if options['power_mode'] == 'direct' else area * density if area and density else None
    missing = {}
    if not power:
        missing['responsivity'] = 'Enter incident power on the active area, or illuminated active area and irradiance.'
    if not (area and power and options['noise_mode'] != 'none'):
        missing['detectivity'] = 'Provide active area, optical power and measured noise, or explicitly choose the shot-noise estimate.'
    if not (power and options.get('wavelength_nm') and options.get('monochromatic')):
        missing['eqe'] = 'EQE requires optical power and a confirmed monochromatic wavelength.'
    warnings = list(dict.fromkeys(dark_warnings + light_warnings))
    warnings += ['Dark and light currents are linearly interpolated only within their shared voltage range and selected contiguous branches.',
        'Current on/off ratio is |I_light|/|I_dark|. It is not a signal-to-noise ratio.',
        'Peak values describe this acquisition, not a ranking against other devices.']
    if options['noise_mode'] == 'shot':
        warnings.append('Detectivity is a dark-current shot-noise-limited estimate; thermal, 1/f, contact and instrument noise are omitted.')
    if options['noise_mode'] == 'measured':
        warnings.append('The supplied noise spectral density is assumed applicable at every plotted bias and the stated measurement conditions.')
    rows = []
    for voltage, dark_current, light_current in zip(grid, dark_i, light_i):
        photo = float(light_current - dark_current)
        response = abs(photo) / power if power else None
        denominator_ok = abs(dark_current) > options['current_floor_a']
        noise = options.get('noise_a_sqrt_hz') if options['noise_mode'] == 'measured' else math.sqrt(2 * Q * abs(dark_current)) if options['noise_mode'] == 'shot' and denominator_ok else None
        detectivity = response * math.sqrt(area) / noise if response is not None and area and noise else None
        eqe = response * H * C / (Q * options['wavelength_nm'] * 1e-9) * 100 if 'eqe' not in missing else None
        rows.append({'voltage_v': float(voltage), 'dark_a': float(dark_current), 'light_a': float(light_current),
            'photocurrent_a': photo, 'on_off_ratio': finite(abs(light_current) / abs(dark_current)) if denominator_ok else None,
            'responsivity_a_w': finite(response), 'detectivity_jones': finite(detectivity), 'eqe_percent': finite(eqe)})
    if any(row['on_off_ratio'] is None for row in rows):
        warnings.append('Ratios and shot-noise estimates are withheld where dark current is zero or at/below the supplied current floor.')
    def peak(column):
        available = [row for row in rows if row[column] is not None]
        if not available:
            return None
        row = max(available, key=lambda item: item[column])
        return {'value': row[column], 'voltage_v': row['voltage_v']}
    zero = None
    if low <= 0 <= high:
        zero = {'light_a': float(np.interp(0, light[:, 0], light[:, 1])), 'dark_a': float(np.interp(0, dark[:, 0], dark[:, 1]))}
        zero['photocurrent_a'] = zero['light_a'] - zero['dark_a']
    else:
        missing['short_circuit_current'] = 'The selected sweeps do not bracket zero bias; extrapolation is disabled.'
    diode = diode_fit(dark, options)
    if not diode.get('ideality_factor'):
        missing['diode'] = diode['reason']
    return {'rows': rows, 'selected_traces': {'dark': dark_trace, 'light': light_trace},
        'raw_selected_branches': {'dark': dark.tolist(), 'light': light.tolist()},
        'normalized_inputs': {'active_area_cm2': area, 'irradiance_w_cm2': density, 'incident_power_w': power},
        'metrics': {'responsivity': peak('responsivity_a_w'), 'on_off_ratio': peak('on_off_ratio'),
                    'detectivity': peak('detectivity_jones'), 'eqe': peak('eqe_percent'), 'zero_bias': zero},
        'diode': diode, 'missing': missing, 'warnings': warnings,
        'references': REFERENCES[:2] + (REFERENCES[2:] + transport_methods.cited(['nist-ols', 'gum', 'scipy-t']) if diode.get('fit') else []),
        'summary': {'matched_points': len(rows), 'voltage_min_v': float(low), 'voltage_max_v': float(high),
            'max_responsivity_a_w': (peak('responsivity_a_w') or {}).get('value'),
            'max_on_off_ratio': (peak('on_off_ratio') or {}).get('value'),
            'max_detectivity_jones': (peak('detectivity_jones') or {}).get('value'),
            'ideality_factor': diode.get('ideality_factor'), 'barrier_height_ev': diode.get('barrier_height_ev')}}


def diode_fit(dark, options):
    result = {'ideality_factor': None, 'barrier_height_ev': None, 'points': [], 'fit_points': [],
              'reason': 'Enable the thermionic-emission approximation and enter temperature and a forward fit window.'}
    sign = options['forward_sign']
    # Use original dark observations, not the interpolated comparison grid.
    positive = [(float(v * sign), math.log(abs(float(i)))) for v, i in dark if v * sign > 0 and i * sign > options['current_floor_a']]
    result['points'] = sorted(positive)
    if not options['thermionic_confirmed']:
        return result
    selected = [point for point in positive if options['fit_min_v'] <= point[0] <= options['fit_max_v']]
    result['fit_window_v'] = [options['fit_min_v'], options['fit_max_v']]
    if len(selected) < 4:
        result['reason'] = 'Select at least four forward dark-current observations above the current floor with consistent current polarity.'
        return result
    fit = transport_methods.linear_fit(selected)
    result['fit'] = fit
    if not fit or fit['slope'] <= 0 or (fit['r_squared'] or 0) < .95:
        result['reason'] = 'The chosen log-current window is not sufficiently linear with positive slope (R² must be at least 0.95). Review the fit region.'
        return result
    thermal_v = KB * options['temperature_k'] / Q
    n = 1 / (thermal_v * fit['slope'])
    if options['fit_min_v'] * fit['slope'] < 3:
        result['reason'] = 'The selected region is too close to zero bias for the high-forward-current approximation; move the fit window upward.'
        return result
    saturation = math.exp(fit['intercept']) if -700 < fit['intercept'] < 700 else None
    result.update(ideality_factor=finite(n), saturation_current_a=finite(saturation), reason=None,
        fit_points=[[v, fit['intercept'] + fit['slope'] * v] for v, _ in sorted(selected)],
        interpretation='A log-linear slope alone does not establish a transport mechanism. Leakage, series resistance, contacts and temperature can affect this fitted factor.')
    contact_area, richardson = options.get('contact_area_cm2'), options.get('richardson_a_cm2_k2')
    if contact_area and richardson and saturation:
        barrier = thermal_v * (math.log(contact_area) + math.log(richardson) + 2 * math.log(options['temperature_k']) - math.log(saturation))
        result['barrier_height_ev'] = finite(barrier) if barrier > 0 else None
    if result['barrier_height_ev'] is None:
        result['barrier_reason'] = 'A positive apparent barrier requires contact area and a material-specific Richardson constant, under the thermionic model.'
    return result


def explain(result, question):
    question = question.lower()
    if any(word in question for word in ['ideality', 'diode', 'barrier', 'factor']):
        diode = result['diode']
        return (f"Fitted ideality factor: {diode['ideality_factor']:.4g}. " + diode['interpretation']) if diode.get('ideality_factor') else diode['reason']
    if any(word in question for word in ['detectivity', 'best bias', 'highest']):
        peak = result['metrics']['detectivity']
        return (f"The largest computed detectivity is {peak['value']:.4g} Jones at {peak['voltage_v']:.4g} V. This is specific to the supplied noise model and acquisition; also consider dark current, stability and power consumption.") if peak else result['missing'].get('detectivity', 'No valid detectivity point is available above the current floor.')
    if any(word in question for word in ['compare', 'standard', 'mos2', 'performant', 'literature']):
        return 'A fair literature comparison needs matched wavelength, irradiance, active area, bias, bandwidth and measured noise. This recipe does not assign a device ranking from unmatched reports. Use the cited methods and add a suitably matched reference measurement.'
    if any(word in question for word in ['missing', 'next', 'need']):
        return ' '.join(result['missing'].values()) or 'All enabled metrics have the required inputs. Next, check repeatability and measure noise at the chosen operating bias.'
    response = result['metrics']['responsivity']
    parts = [f"Compared {len(result['rows'])} bias points over {result['summary']['voltage_min_v']:.4g} to {result['summary']['voltage_max_v']:.4g} V."]
    if response:
        parts.append(f"Peak responsivity is {response['value']:.4g} A/W at {response['voltage_v']:.4g} V: current change per watt on the active area.")
    else:
        parts.append(result['missing']['responsivity'])
    parts.append('On/off ratio describes the change in current magnitude, not measured signal-to-noise. ' + ('Some metrics need additional inputs.' if result['missing'] else 'Review the assumptions before comparing devices.'))
    return ' '.join(parts)
