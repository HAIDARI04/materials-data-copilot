"""Publication figures from saved observations; no fitting occurs during export."""
import io
import threading
import textwrap


PLOT_LOCK = threading.RLock()
COLORS = ['#18685b', '#ba582b', '#5164a3', '#933a63']


def specifications(result):
    specs = []
    if result['model_name'] == 'research_photodetector':
        rows = result['rows']
        groups = [
            ('iv', 'Dark, illuminated and photocurrent', 'Current (A)', [('Dark', 'dark_a'), ('Light', 'light_a'), ('Photocurrent', 'photocurrent_a')]),
            ('response', 'Responsivity', 'Responsivity (A/W)', [('Responsivity', 'responsivity_a_w')]),
            ('detectivity', 'Specific detectivity', 'Detectivity (Jones)', [('Detectivity', 'detectivity_jones')]),
            ('ratio', 'Current on/off ratio', '|I light| / |I dark|', [('On/off', 'on_off_ratio')]),
            ('eqe', 'External quantum efficiency', 'EQE (%)', [('EQE', 'eqe_percent')]),
        ]
        for name, title, unit, columns in groups:
            specs.append({'id': name, 'title': title, 'x_label': 'Bias voltage (V)', 'y_label': unit,
                          'series': [{'label': label, 'points': [[row['voltage_v'], row[key]] for row in rows]} for label, key in columns],
                          'caption': 'Selected dark/light branches aligned by linear interpolation on their shared voltage range. Missing quantities remain unavailable. ' +
                                     ('Noise model: ' + result['parameters']['options']['noise_mode'] + '. ' if name == 'detectivity' else '')})
        diode = result['diode']
        specs.append({'id': 'diode', 'title': 'Forward dark-current fit', 'x_label': 'Forward voltage (V)',
                      'y_label': 'ln(|I| / 1 A)', 'window': diode.get('fit_window_v'),
                      'series': [{'label': 'Dark observations', 'points': diode['points']},
                                 {'label': 'Fit', 'points': diode['fit_points'], 'fit': True}],
                      'caption': diode.get('reason') or diode['interpretation']})
        fit = diode.get('fit')
        if fit and diode['fit_points']:
            observed = dict(diode['points'])
            specs.append({'id': 'diode-residuals', 'title': 'Dark-current fit residuals', 'x_label': 'Forward voltage (V)',
                          'y_label': 'Log-current residual', 'residual': True,
                          'series': [{'label': 'Residual', 'points': [[x, observed[x] - predicted] for x, predicted in diode['fit_points']]}],
                          'caption': 'Observed ln(|I| / 1 A) minus fitted value at each fitted dark observation.'})
        return specs
    for sheet_index, sheet in enumerate(result['sheets'], 1):
        for channel_index, channel in enumerate(dict.fromkeys(o['channel'] for o in sheet.get('observations', [])), 1):
            observations = [o for o in sheet['observations'] if o['channel'] == channel]
            specs.append({'id': f's{sheet_index}-c{channel_index}-full', 'title': f'{sheet["name"]}: {channel}, full measurement',
                          'x_label': f'{observations[0]["x_column"]} ({observations[0]["x_unit"]})', 'y_label': f'{channel} (A)',
                          'series': [{'label': 'All finite observations', 'points': [[o['x'], o['current_a']] for o in observations]}],
                          'caption': 'Acquisition order and missing-row gaps are preserved. See observations.csv for each source row and selection reason.'})
        for branch in sheet['branches']:
            identity = branch['branch_id']
            fit = branch.get('fit')
            selected = set(branch.get('fit_source_rows', []))
            included = [point for row, point in zip(branch['source_rows'], branch['points']) if row in selected]
            outside = [point for row, point in zip(branch['source_rows'], branch['points']) if row not in selected]
            series = [{'label': 'Trace' if branch['x_unit'] == 's' else 'Outside fit / fit unavailable', 'points': outside, 'scatter': True}]
            if included:
                series.append({'label': 'Used in fit', 'points': included, 'scatter': True})
            if fit:
                series.append({'label': 'OLS fit', 'points': [[p[0], y] for p, y in zip(included, fit['fitted_values'])], 'fit': True})
            specs.append({'id': identity, 'title': f'{sheet["name"]}: {branch["channel"]}, {branch["direction"]}',
                          'x_label': f'{branch["x_column"]} ({branch["x_unit"]})', 'y_label': f'{branch["channel"]} (A)',
                          'series': series, 'window': branch.get('fit_window'),
                          'caption': f'Source worksheet rows {branch["first_row"]}-{branch["last_row"]}. '
                                     'Turnaround observations may belong to both adjacent branches. Signed values are retained.'})
            if fit:
                specs.append({'id': identity + '-residuals', 'title': f'{identity}: fit residuals',
                              'x_label': f'{branch["x_column"]} ({branch["x_unit"]})', 'y_label': 'Current residual (A)',
                              'series': [{'label': 'Observed - fitted', 'points': [[p[0], r] for p, r in zip(included, fit['residuals'])], 'scatter': True}],
                              'residual': True, 'caption': 'Residuals use exactly the fitted source rows. Structure in residuals can indicate an inadequate linear model.'})
        for index, window in enumerate(sheet['windows'], 1):
            if not window['point_count']:
                continue
            options = result['parameters']['options']
            channel = next(c for c in sheet['channels'] if c['current'] == window['channel'])
            offset = sheet.get('source_header_row', 1) + 1
            points = [[sheet['rows'][row - offset][options.get('time_column', 'Time')] * options.get('time_scale', 1),
                       sheet['rows'][row - offset][window['channel']] * channel['current_scale'] * channel['polarity']]
                      for row in window['source_rows']]
            specs.append({'id': f's{sheet_index}-window-{index}', 'title': f'{window["label"]}: {window["channel"]}',
                          'x_label': 'Time (s)', 'y_label': 'Current (A)', 'window': [window['start'], window['end']],
                          'series': [{'label': 'Window observations', 'points': points, 'scatter': True}],
                          'caption': window.get('integration_reason') or 'Signed trapezoidal integration uses adjacent observations in the declared time window.'})
    return specs


def render(spec, fmt, source_hash):
    from matplotlib import rc_context
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    from matplotlib.figure import Figure

    if fmt not in {'svg', 'pdf', 'png'}:
        raise ValueError('Choose SVG, PDF or PNG for figures.')
    with PLOT_LOCK, rc_context({'font.family': 'DejaVu Sans', 'font.size': 10, 'svg.fonttype': 'none',
                               'pdf.fonttype': 42, 'axes.spines.top': False, 'axes.spines.right': False}):
        figure = Figure(figsize=(8, 5.1), facecolor='white')
        FigureCanvasAgg(figure)
        ax = figure.add_subplot(111)
        figure.subplots_adjust(left=.14, right=.96, bottom=.24, top=.84)
        valid_points = [(x, y) for series in spec['series'] for x, y in series['points'] if x is not None and y is not None]
        if not valid_points:
            ax.text(.5, .5, 'Quantity unavailable\nSee required inputs in the report', ha='center', va='center', transform=ax.transAxes)
        for index, series in enumerate(spec['series']):
            points = series['points']
            if not points:
                continue
            x = [float('nan') if p[0] is None else p[0] for p in points]
            y = [float('nan') if p[1] is None else p[1] for p in points]
            color = COLORS[index % len(COLORS)]
            if series.get('scatter'):
                ax.scatter(x, y, s=15, label=series['label'], color=color, zorder=3)
            else:
                ax.plot(x, y, '--' if series.get('fit') else '-o', markersize=3, linewidth=1.3, label=series['label'], color=color)
        if spec.get('window') and valid_points:
            low = max(spec['window'][0], min(x for x, _ in valid_points))
            high = min(spec['window'][1], max(x for x, _ in valid_points))
            if low < high:
                ax.axvspan(low, high, alpha=.1, color=COLORS[0], label='Selected window')
        if spec.get('residual'):
            ax.axhline(0, color='#79847f', linewidth=.8)
        ax.set_xlabel(spec['x_label'])
        ax.set_ylabel(spec['y_label'])
        ax.grid(alpha=.2)
        ax.ticklabel_format(axis='both', style='sci', scilimits=(-3, 4), useOffset=False)
        handles, labels = ax.get_legend_handles_labels()
        if handles:
            ax.legend(handles, labels, loc='best', fontsize=8, framealpha=.9)
        figure.suptitle('\n'.join(textwrap.wrap(spec['title'], 75)), x=.14, y=.97, ha='left', fontsize=12, fontweight='bold')
        figure.text(.14, .065, '\n'.join(textwrap.wrap(spec['caption'], 105)), fontsize=7, color='#44564f', va='bottom')
        figure.text(.14, .022, 'Source SHA-256: ' + source_hash, fontsize=6, color='#53665e')
        output = io.BytesIO()
        metadata = {'Creator': 'Materials Data Copilot'}
        if fmt == 'svg':
            metadata['Date'] = None
        if fmt == 'pdf':
            metadata.update(CreationDate=None, ModDate=None)
        figure.savefig(output, format=fmt, dpi=300, metadata=metadata)
        figure.clear()
        return output.getvalue()
