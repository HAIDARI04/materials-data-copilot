"""Human-readable explanations and tables derived exclusively from a saved run."""
import json
import math


def number(value):
    if value is None:
        return 'Unavailable'
    if isinstance(value, bool):
        return 'Yes' if value else 'No'
    if isinstance(value, (float, int)):
        return f'{value:.6g}' if math.isfinite(value) else 'Unavailable'
    if isinstance(value, list):
        return '[' + ', '.join(number(item) for item in value) + ']'
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False, allow_nan=False)
    return str(value)


def measurement(value, uncertainty=None):
    """Round uncertainty to two significant digits and the estimate to its decimal place."""
    if value is None:
        return 'Unavailable'
    if uncertainty is None or uncertainty <= 0:
        return number(value)
    exponent = math.floor(math.log10(uncertainty))
    place = exponent - 1
    if -6 <= place <= 3 and abs(value) < 1e6:
        digits = max(0, -place)
        return f'{round(value, -place):.{digits}f} +/- {round(uncertainty, -place):.{digits}f}'
    return f'{value:.5g} +/- {uncertainty:.2g}'


def data_tables(result):
    tables = {}
    if result['model_name'] == 'research_photodetector':
        tables['matched-measurements'] = [list(result['rows'][0]), *[list(row.values()) for row in result['rows']]]
        for role, points in result['raw_selected_branches'].items():
            tables[f'{role}-selected-branch'] = [['voltage_v', 'current_a'], *points]
        for role, trace in result['selected_traces'].items():
            if trace.get('source_observations'):
                tables[f'{role}-source-observations'] = [list(trace['source_observations'][0]), *[list(row.values()) for row in trace['source_observations']]]
        diode = result['diode']
        observed = dict(diode['points'])
        tables['diode-fit'] = [['forward_voltage_v', 'log_current', 'fitted_log_current', 'residual'],
                              *[[x, observed[x], predicted, observed[x] - predicted] for x, predicted in diode['fit_points']]]
        tables['parameters'] = [['quantity', 'value', 'bias_v', 'status']]
        for key, metric in result['metrics'].items():
            if key == 'zero_bias':
                for quantity, value in (metric or {}).items():
                    tables['parameters'].append([quantity, value, 0, 'calculated'])
            else:
                tables['parameters'].append([key, (metric or {}).get('value'), (metric or {}).get('voltage_v'),
                                             'assumption-dependent' if metric and key == 'detectivity' and result['parameters']['options']['noise_mode'] == 'shot' else 'calculated' if metric else result['missing'].get(key, 'Unavailable')])
        return tables
    observations = [['worksheet', 'source_row', 'channel', 'x_column', 'x_unit', 'original_x', 'original_current',
                     'x_scale', 'current_scale', 'polarity', 'x', 'current_a', 'branch_ids', 'fit_branch_ids', 'reason']]
    fits = [['worksheet', 'branch_id', 'channel', 'direction', 'response_kind', 'point_count', 'x_min', 'x_max',
             'slope_a_v', 'slope_standard_error', 'intercept_a', 'intercept_standard_error', 'r_squared', 'rmse_a',
             'resistance_ohm', 'resistance_standard_uncertainty_ohm', 'resistance_ci95_low', 'resistance_ci95_high',
             'resistivity_ohm_m', 'conductivity_s_m', 'status', 'missing_inputs']]
    residuals = [['worksheet', 'branch_id', 'source_row', 'x', 'current_a', 'fitted_current_a', 'residual_a']]
    windows = [['worksheet', 'label', 'channel', 'start_s', 'end_s', 'point_count', 'mean_a', 'population_sd_a',
                'drift_a_s', 'integrated_current_c', 'integration_reason', 'source_rows']]
    for index, sheet in enumerate(result['sheets'], 1):
        tables[f'worksheet-{index}'] = [['source_row', *sheet['columns']], *[[row_index, *[row.get(k) for k in sheet['columns']]] for row_index, row in enumerate(sheet['rows'], sheet.get('source_header_row', 1) + 1)]]
        for observation in sheet.get('observations', []):
            observations.append([sheet['name'], *[observation.get(key) for key in observations[0][1:]]])
        for branch in sheet['branches']:
            fit = branch.get('fit')
            tables[branch['branch_id']] = [['source_row', 'x', 'current_a', 'used_in_fit'],
                                           *[[row, *point, row in branch['fit_source_rows']] for row, point in zip(branch['source_rows'], branch['points'])]]
            if fit:
                ci = fit.get('resistance_ci95_ohm') or [None, None]
                fits.append([sheet['name'], branch['branch_id'], branch['channel'], branch['direction'], fit['response_kind'],
                             fit['point_count'], fit['x_min'], fit['x_max'], fit['slope'], fit['slope_standard_error'],
                             fit['intercept'], fit['intercept_standard_error'], fit['r_squared'], fit['rmse'],
                             fit.get('resistance_ohm'), fit.get('resistance_standard_uncertainty_ohm'), *ci,
                             fit.get('resistivity_ohm_m'), fit.get('conductivity_s_m'), fit['physical_status'], fit['missing_inputs']])
                selected_rows = set(branch['fit_source_rows'])
                selected = [p for r, p in zip(branch['source_rows'], branch['points']) if r in selected_rows]
                for row, point, predicted, residual in zip(branch['fit_source_rows'], selected, fit['fitted_values'], fit['residuals']):
                    residuals.append([sheet['name'], branch['branch_id'], row, *point, predicted, residual])
        for window in sheet['windows']:
            stats = window['current_statistics_a'] or {}
            windows.append([sheet['name'], window['label'], window['channel'], window['start'], window['end'], window['point_count'],
                            stats.get('mean'), stats.get('standard_deviation'), (window['drift_a_per_s'] or {}).get('slope'),
                            window['integrated_signed_current_c'], window.get('integration_reason'), window.get('source_rows')])
    tables.update(observations=observations, parameters=fits, residuals=residuals, windows=windows)
    return tables


def fit_steps(fit, x_unit='V'):
    n = number
    return [
        f'Fit I = m*x + b with equal weights and a free intercept. x is in {x_unit}; current I is in A. N = {fit["point_count"]}. [nist-ols]',
        f'Calculate the means: x_mean = {n(fit["x_mean"])} {x_unit}; I_mean = {n(fit["y_mean"])} A.',
        f'Calculate Sxx = sum((x - x_mean)^2) = {n(fit["sxx"])} {x_unit}^2 and Sxy = sum((x - x_mean)*(I - I_mean)) = {n(fit["sxy"])} A*{x_unit}.',
        f'Slope m = Sxy/Sxx = {n(fit["sxy"])}/{n(fit["sxx"])} = {n(fit["slope"])} A/{x_unit}. Intercept b = I_mean - m*x_mean = {n(fit["intercept"])} A.',
        f'For every selected source row, calculate predicted I = m*x + b and residual = measured I - predicted I. SSE = sum(residual^2) = {n(fit["residual_sum_squares"])} A^2; RMSE = sqrt(SSE/N) = {n(fit["rmse"])} A.',
        f'Degrees of freedom = N - 2 = {fit["degrees_of_freedom"]}. Fit standard uncertainty u(m) = sqrt(SSE/((N-2)*Sxx)) = {n(fit["slope_standard_error"])} A/{x_unit}. [gum]',
        f'95% slope interval = m +/- t(0.975, N-2)*u(m), with t = {n(fit["t_critical"])}: [{n(fit["slope_ci95"][0])}, {n(fit["slope_ci95"][1])}] A/{x_unit}. [scipy-t]',
        fit['uncertainty_scope'],
    ]


def report_model(result):
    photo = result['model_name'] == 'research_photodetector'
    options = result['parameters']['options']
    sections = []
    def section(title, paragraphs=(), rows=None, headers=None, steps=None):
        item = {'title': title, 'paragraphs': list(paragraphs)}
        if rows is not None:
            item.update(headers=headers or ['Item', 'Value'], rows=rows)
        if steps:
            item['steps'] = steps
        sections.append(item)
    section('Results at a glance', [
        'This report follows the saved measurement selection, unit conversions and analysis settings. The original uploads accompany the complete package.',
        'Values in tables are rounded for reading. CSV and JSON retain full saved numerical precision. Unavailable quantities remain explicitly identified.',
    ], [[key.replace('_', ' '), number(value)] for key, value in result['summary'].items()])
    if not photo:
        estimates = []
        for sheet in result['sheets']:
            for branch in sheet['branches']:
                fit = branch.get('fit')
                if fit:
                    label = sheet['name'] + ' / ' + branch['branch_id']
                    estimates.append([label + ': response slope', measurement(fit['slope'], fit['slope_standard_error']), 'A/V', 'Fit scatter only'])
                    estimates.append([label + ': resistance', measurement(fit.get('resistance_ohm'), fit.get('resistance_standard_uncertainty_ohm')), 'ohm', fit['physical_status'] if fit.get('resistance_ohm') is not None else 'Unavailable'])
            for window in sheet['windows']:
                estimates.append([window['label'] + ': mean current', number((window['current_statistics_a'] or {}).get('mean')), 'A', 'Window mean; uncertainty unavailable'])
        if estimates:
            section('Extracted parameters', ['Uncertainties below are standard uncertainties from fit scatter. Measurement assumptions and missing inputs are explained in each calculation.'],
                    estimates, ['Quantity / selection', 'Estimate +/- standard uncertainty', 'Unit', 'Status / scope'])
    context = []
    for source in result.get('sources', []):
        for key in ['original_filename', 'sample_id', 'instrument', 'measurement_date', 'material_system', 'substrate']:
            context.append([source['role'] + ': ' + key.replace('_', ' '), number(source.get(key)), 'Imported metadata' if source.get(key) and str(source[key]).lower() not in {'unknown', 'none'} else 'Missing or unconfirmed'])
    for key in ['temperature_k', 'measurement_configuration', 'geometry_model', 'length_m', 'width_m', 'thickness_m',
                'units_confirmed', 'wiring_confirmed', 'gate_connection', 'light_condition', 'biased_terminal', 'configuration_notes']:
        if key in options:
            value = options[key]
            context.append([key.replace('_', ' '), number(value), 'Student-confirmed' if value not in (None, '', 'unknown', 'none', False) else 'Missing or unconfirmed'])
    section('Measurement context and input origins', rows=context, headers=['Input', 'Value', 'Origin / status'])
    section('How the data were prepared', steps=[
        'Verify each original file against its saved SHA-256 and byte count.',
        'Read stored worksheet cell values. Spreadsheet formulas are not recalculated; cached values are used. Preserve worksheet names and row numbers.',
        'Apply only the declared scales and current polarity. Split traces at missing observations and sweep reversals. A turnaround belongs to both neighboring branches.',
        'Use saved fit windows and sweep directions. Retain excluded observations and their reasons in the analysis tables.',
        'Fit and interpret only the eligible observations. All derived outputs are stored separately from original measurements.',
    ])
    if photo:
        _photo_sections(result, section)
    else:
        for sheet in result['sheets']:
            observations = sheet.get('observations', [])
            section('Data inspection: ' + sheet['name'], rows=[
                ['Header source row (first nonempty row)', sheet.get('source_header_row', 1)],
                ['Worksheet data rows', len(sheet['rows'])],
                ['Channel observations with missing/non-finite values', sum(o['x'] is None for o in observations)],
                ['Finite channel observations outside all selected branches', sum(o['x'] is not None and not o['branch_ids'] for o in observations)],
                ['Finite channel observations excluded from all fits', sum(o['x'] is not None and not o['fit_branch_ids'] for o in observations)],
                ['Selected branches', len(sheet['branches'])],
                ['Compliance status', 'No validated compliance-channel mapping is available. Review acquisition flags and instrument limits before interpretation.'],
            ])
            if sheet['acquisition_settings']:
                section('Acquisition settings: ' + sheet['name'], rows=[[r['source_row'], r['field'], number(r['values'])] for r in sheet['acquisition_settings']], headers=['Settings row', 'Field', 'Stored value'])
            for branch in sheet['branches']:
                fit = branch.get('fit')
                source = f'{result["original_filename"]}; worksheet {sheet["name"]}; source rows {branch["first_row"]}-{branch["last_row"]}; {branch["direction"]} branch {branch["branch_id"]}.'
                preparation = f'x = stored {branch["x_column"]} * {number(branch["axis_scale"])} ({branch["x_unit"]}); I = stored {branch["channel"]} * {number(branch["current_scale"])} * polarity {branch["polarity"]} (A).'
                if not fit:
                    section('Trace: ' + branch['branch_id'], [source, preparation,
                        'This is a time trace; I-V fitting is not applicable.' if branch['x_unit'] == 's' else 'Fit unavailable: the selected interval needs at least three finite observations with varying voltage.'])
                    continue
                rows = [
                    ['Response slope', measurement(fit['slope'], fit['slope_standard_error']), 'A/V', 'Fit standard uncertainty'],
                    ['Current intercept', measurement(fit['intercept'], fit['intercept_standard_error']), 'A', 'Fit standard uncertainty'],
                    ['R squared', number(fit['r_squared']), 'dimensionless', 'Descriptive fit statistic'],
                    ['Resistance', measurement(fit.get('resistance_ohm'), fit.get('resistance_standard_uncertainty_ohm')), 'ohm', fit['physical_status'] if fit.get('resistance_ohm') is not None else 'Unavailable'],
                ]
                if options.get('geometry_model') == 'rectangular_bar':
                    rows.extend([
                        ['Resistivity', measurement(fit.get('resistivity_ohm_m'), fit.get('resistivity_standard_uncertainty_ohm_m')), 'ohm m', 'Calculated' if fit.get('resistivity_ohm_m') is not None else 'Unavailable'],
                        ['Conductivity', measurement(fit.get('conductivity_s_m'), fit.get('conductivity_standard_uncertainty_s_m')), 'S/m', 'Calculated' if fit.get('conductivity_s_m') is not None else 'Unavailable'],
                    ])
                steps = fit_steps(fit)
                if fit.get('resistance_ohm') is not None:
                    steps += [f'R = 1/m = 1/{number(fit["slope"])} = {number(fit["resistance_ohm"])} ohm. First-order fit uncertainty u(R) = u(m)/m^2 = {number(fit["resistance_standard_uncertainty_ohm"])} ohm. The 95% interval is obtained by inverting the slope interval: {number(fit["resistance_ci95_ohm"])} ohm. [gum]']
                if fit.get('resistivity_ohm_m') is not None:
                    steps += [f'For a uniform rectangular bar, rho = R*w*t/L = {number(fit["resistance_ohm"])} * {number(options["width_m"])} * {number(options["thickness_m"])} / {number(options["length_m"])} = {number(fit["resistivity_ohm_m"])} ohm m. Conductivity = 1/rho = {number(fit["conductivity_s_m"])} S/m. [openstax-resistance]',
                              'Combined relative standard uncertainty is the square root of the sum of squared relative fit, current, voltage, length, width and thickness contributions. This assumes independent inputs and small uncertainties. Current/voltage entries must describe additional calibration contributions, avoiding double counting fit scatter. No expanded interval is claimed without a justified coverage model. [gum]']
                section('Calculation: ' + branch['branch_id'], [source, preparation,
                        f'Fit window: {number(branch.get("fit_window")) if branch.get("fit_window") else "All observations in the selected branch"}. Exact fitted rows are in residuals.csv.',
                        fit['interpretation'], *fit['missing_inputs']], rows=rows,
                        headers=['Quantity', 'Estimate +/- standard uncertainty', 'Unit', 'Status / scope'], steps=steps)
            for window in sheet['windows']:
                stats = window['current_statistics_a'] or {}
                steps = [f'Select {window["channel"]} observations within [{number(window["start"])}, {number(window["end"])}] s, including endpoints. N = {window["point_count"]}.',
                         f'Mean = sum(I)/N = {number(stats.get("mean"))} A. Population standard deviation = sqrt(sum((I-mean)^2)/N) = {number(stats.get("standard_deviation"))} A.',
                         f'{window["integration_equation"]}; result = {number(window["integrated_signed_current_c"])} C. ' + (window.get('integration_reason') or '[numpy-trapezoid]')]
                if window.get('drift_a_per_s'):
                    steps += fit_steps(window['drift_a_per_s'], 's')
                section('Time window: ' + window['label'] + ' / ' + window['channel'], [window['uncertainty_scope']], steps=steps)
    section('Interpretation, uncertainty and next measurements', [*result.get('warnings', []),
        'Inspect residual patterns, sweep-to-sweep differences and instrument limits before assigning a physical mechanism. A high R squared alone does not validate a mechanism.',
        'Obtain independent repeats and instrument calibration uncertainties to evaluate repeatability and measurement uncertainty. Report fit-only uncertainty as such.',
        'Hall carrier density and mobility require a dedicated Hall workflow with magnetic-field and contact conventions. These quantities are not inferred from these I-V/I-t results.',
    ])
    section('Reproduce this analysis', steps=[
        'Extract the complete ZIP into a new folder. Read README.md and use the recorded Python version.',
        'Run python reproduce.py --verify-only to verify all packaged file hashes without scientific dependencies.',
        'Create a virtual environment and install the exact versions in requirements.txt.',
        'Run python reproduce.py. It imports the packaged implementation, reads original sources and recalculates all analysis fields.',
        'Read reproduced/verification.json. Counts, identifiers and text must match exactly. Floats use relative tolerance 1e-10 and absolute tolerance 1e-30 in the stored units.',
        'Use analysis.ipynb for an annotated walkthrough. The replay writes to reproduced/ and preserves the package inputs. Figure rendering is presentation; numerical reproduction verifies calculation.json.',
    ])
    provenance = [['Analysis ID', result['processing_id']], ['Analysis time (UTC)', result['processed_at']],
                  ['Model version', result['model_version']], ['Result SHA-256', result.get('result_sha256')],
                  ['Implementation SHA-256', (result.get('implementation') or {}).get('sha256')]]
    for source in result.get('sources', []):
        provenance += [[source['role'] + ': filename', source['original_filename']], [source['role'] + ': bytes', source['size_bytes']],
                       [source['role'] + ': SHA-256', source['sha256']], [source['role'] + ': imported at', source['imported_at']],
                       [source['role'] + ': package path', source['package_path']]]
    section('Provenance and software', rows=provenance)
    section('Saved analysis inputs', rows=[[key, number(value)] for key, value in options.items() if photo or key in {
        'sheet', 'channels', 'sweep_direction', 'terminal_roles', 'terminal_mapping', 'time_column', 'time_scale', 'windows',
        'fit_window', 'current_relative_standard_uncertainty', 'voltage_relative_standard_uncertainty', 'length_standard_uncertainty_m',
        'width_standard_uncertainty_m', 'thickness_standard_uncertainty_m'}])
    return {'title': 'Photodetector characterization report' if photo else 'Transport analysis report',
            'subtitle': result['original_filename'], 'sections': sections, 'references': result.get('references', [])}


def _photo_sections(result, section):
    n = number
    options, inputs = result['parameters']['options'], result['normalized_inputs']
    section('Dark/light pairing and alignment', [
        'The student confirmed that both measurements belong to the same device, with reviewed roles and units.',
        'Repeated voltages are averaged within each selected branch. The comparison grid is the union of the voltage points within the shared range; linear interpolation is used without extrapolation.',
    ], [[role, trace['sheet'], trace['current'], trace['voltage'], f'{trace["first_row"]}-{trace["last_row"]}', trace['direction']] for role, trace in result['selected_traces'].items()],
        ['Role', 'Worksheet', 'Current', 'Voltage', 'Source rows', 'Direction'])
    row = result['rows'][len(result['rows']) // 2]
    section('Methodology: worked photodetector calculation', [
        f'The following substitution uses bias {n(row["voltage_v"])} V. matched-measurements.csv contains every calculated bias point.',
        'Input measurement uncertainties have not been supplied to this recipe; propagated optical-metric uncertainty is unavailable.',
    ], steps=[
        f'I_ph = I_light - I_dark = {n(row["light_a"])} - {n(row["dark_a"])} = {n(row["photocurrent_a"])} A.',
        f'Illuminated active area = {n(inputs["active_area_cm2"])} cm^2; irradiance = {n(inputs["irradiance_w_cm2"])} W/cm^2. For uniform illumination P = irradiance * active area, or use directly supplied incident power. P = {n(inputs["incident_power_w"])} W.',
        f'Responsivity = |I_ph|/P = {n(abs(row["photocurrent_a"]))}/{n(inputs["incident_power_w"])} = {n(row["responsivity_a_w"])} A/W. [hamamatsu-photodiodes]',
        f'On/off ratio = |I_light|/|I_dark| = {n(abs(row["light_a"]))}/{n(abs(row["dark_a"]))} = {n(row["on_off_ratio"])}. Withhold ratios at or below the declared dark-current floor {n(options["current_floor_a"])} A.',
        f'Detectivity D* = responsivity*sqrt(area_cm2)/noise_A_per_sqrt_Hz = {n(row["detectivity_jones"])} Jones. Noise mode: {options["noise_mode"]}. Shot-noise mode uses sqrt(2*q*|I_dark|) and omits other noise sources. [hamamatsu-photodiodes]',
        f'For confirmed monochromatic illumination, EQE(%) = 100*responsivity*h*c/(q*wavelength_m) = {n(row["eqe_percent"])} %. Wavelength = {n(options.get("wavelength_nm"))} nm. [hamamatsu-photodiodes]',
    ])
    section('Photodetector constants and missing inputs', [
        'SI constants: q = 1.602176634e-19 C; h = 6.62607015e-34 J s; c = 299792458 m/s; k = 1.380649e-23 J/K. These are exact SI defining constants. [si-constants]',
        *[f'{key}: {value}' for key, value in result['missing'].items()],
    ])
    diode = result['diode']
    if diode.get('fit'):
        paragraphs = [diode.get('reason') or diode['interpretation']]
        steps = [f'Fit ln(|I_dark| / 1 A) = m*V_forward + b in {n(diode.get("fit_window_v"))} V. Use selected dark observations before comparison-grid interpolation.',
                 f'm = {n(diode["fit"]["slope"])} V^-1; b = {n(diode["fit"]["intercept"])}; R squared = {n(diode["fit"]["r_squared"])}.',
                 f'Under the high-forward thermionic approximation, n = q/(k*T*m) = {n(diode.get("ideality_factor"))}; I_s = exp(b) A = {n(diode.get("saturation_current_a"))} A. [schottky-ideality]',
                 f'Apparent barrier in eV = (k*T/q)*ln(contact_area*Richardson_constant*T^2/I_s) = {n(diode.get("barrier_height_ev"))}. [schottky-ideality]']
        section('Optional thermionic-emission model', paragraphs, steps=steps)
