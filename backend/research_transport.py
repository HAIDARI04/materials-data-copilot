"""Row-aligned electrical analysis with explicit channel conventions and windows."""
import copy
import math

import transport
import transport_methods


def numeric(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _channel_matches_roles(channel, selected_roles):
    if not selected_roles:
        return True
    role_names = {str(role).casefold() for role in selected_roles}
    terminal = str(channel.get("terminal_role") or channel.get("terminal") or "").casefold()
    current = str(channel.get("current") or "").casefold()
    if "instrument" in role_names and current in {"ai", "bi"}:
        return True
    return any(
        role == terminal
        or current == f"{role}i"
        or terminal == f"{role}i"
        for role in role_names
    )


def segments(values):
    """Turnaround observations belong to both neighboring branches."""
    if len(values) < 2:
        return [(0, len(values))]
    start, direction, result = 0, 0, []
    for index in range(1, len(values)):
        delta = values[index] - values[index - 1]
        sign = 1 if delta > 0 else -1 if delta < 0 else 0
        if sign and direction and sign != direction:
            result.append((start, index))
            start = index - 1
        if sign:
            direction = sign
    result.append((start, len(values)))
    return result


def analyze(path, options):
    options = copy.deepcopy(options)
    sheets, warnings = transport.read_workbook(path)
    result = {'sheets': [], 'settings': [], 'warnings': list(warnings), 'summary': {},
              'method_version': transport_methods.METHOD_VERSION}
    names = {sheet['name'] for sheet in sheets}
    settings_by_run = {}
    for sheet in sheets:
        if sheet['name'].casefold() != 'settings':
            continue
        current = None
        for row_index,row in enumerate(sheet['rows'],1):
            values = [v for v in row if v not in {None,''}]
            if len(values)==1 and str(values[0]) in names:
                current = str(values[0])
                settings_by_run.setdefault(current,[])
            elif current and values and not str(values[0]).startswith('==='):
                settings_by_run[current].append({'source_row':row_index,'field':str(values[0]),'values':values[1:]})
    selected = options.get('sheet')
    for sheet in sheets:
        if sheet['name'].casefold() in {'settings', 'calc'}:
            result['settings'].append(sheet)
            continue
        if selected and selected != sheet['name']:
            continue
        header_index = next((index for index, row in enumerate(sheet['rows']) if any(value not in (None, '') for value in row)), None)
        if header_index is None:
            continue
        source_offset = header_index + 2
        headers = [str(c).strip() if c is not None else '' for c in sheet['rows'][header_index]]
        if len([h for h in headers if h]) != len(set(h for h in headers if h)):
            raise ValueError(f'Duplicate column names in {sheet["name"]}; channel selection would be ambiguous.')
        rows = [dict(zip(headers, row)) for row in sheet['rows'][header_index + 1:]]
        summary = transport._analyze_sheet({**sheet, 'rows': sheet['rows'][header_index:]})
        summary.update(rows=rows, branches=[], windows=[], diagnostics={}, observations=[], source_header_row=header_index + 1,
                       acquisition_settings=settings_by_run.get(sheet['name'],[]))
        channels = copy.deepcopy(options.get('channels')) or [
            {'current': i, 'voltage': v, 'current_scale': 1, 'voltage_scale': 1, 'polarity': 1,
             'terminal': i, 'voltage_status': 'unknown'}
            for i, v in [('AI', 'AV'), ('BI', 'BV'), ('DrainI', 'DrainV'), ('GateI', 'GateV'), ('SourceI', 'SourceV')]]
        terminal_mapping = {
            str(current).casefold(): role
            for current, role in (options.get("terminal_mapping") or {}).items()
        }
        for channel in channels:
            channel.setdefault("instrument_terminal", channel.get("terminal"))
            mapped_role = terminal_mapping.get(str(channel["current"]).casefold())
            channel["terminal_role"] = (
                mapped_role
                if mapped_role and mapped_role != "Unknown"
                else (
                    channel.get("terminal")
                    if str(channel.get("terminal", "")).casefold() in {"source", "drain", "gate"}
                    else None
                )
            )
        voltage_scales = {}
        for channel in channels:
            axis = channel['voltage']
            scale = channel['voltage_scale']
            if axis in voltage_scales and voltage_scales[axis] != scale:
                raise ValueError(f'Conflicting scales for voltage column {axis}.')
            voltage_scales[axis] = scale
        selected_roles = options.get("terminal_roles") or []
        channels = [
            channel for channel in channels
            if _channel_matches_roles(channel, selected_roles)
        ]
        if not channels:
            raise ValueError(
                "No selected source, drain, or gate channels are present in this worksheet."
            )
        summary['channels'] = channels
        voltage_columns = [column for column in voltage_scales if column in headers]
        voltage_ranges = {}
        for column in voltage_columns:
            values = [row[column] for row in rows if numeric(row.get(column))]
            voltage_ranges[column] = (max(values) - min(values)) * voltage_scales[column] if values else 0
        active_sweep_voltage = max(
            voltage_ranges,
            key=voltage_ranges.get,
            default=None,
        )
        time_column = options.get('time_column', 'Time')
        is_time_trace = (
            not any(value > 1e-9 for value in voltage_ranges.values())
            and sum(numeric(row.get(time_column)) for row in rows) >= 2
        )
        if is_time_trace:
            summary['measurement_type'] = 'current_time'
        for channel in channels:
            current, voltage = channel['current'], channel['voltage']
            if options.get('channels') and (current not in headers or (not is_time_trace and voltage not in headers)):
                raise ValueError(f'Configured channels {current}/{voltage} are missing from {sheet["name"]}. Select the appropriate worksheet.')
            channel_voltage = (
                voltage
                if voltage_ranges.get(voltage, 0) > 1e-12
                else active_sweep_voltage
            )
            if channel_voltage is None and not is_time_trace:
                raise ValueError(
                    f'No varying voltage column is available for {current} on {sheet["name"]}.'
                )
            axis_column = time_column if is_time_trace else channel_voltage
            axis_scale = (
                options.get('time_scale', 1)
                if is_time_trace else voltage_scales[channel_voltage]
            )
            pairs = [(index, row[axis_column] * axis_scale,
                      row[current] * channel['current_scale'] * channel['polarity'])
                     for index, row in enumerate(rows)
                     if numeric(row.get(axis_column)) and numeric(row.get(current))]
            if any(not math.isfinite(value) for _, x, y in pairs for value in (x, y)):
                raise ValueError('Channel scaling produced non-finite values.')
            observations = {}
            if current in headers:
                for index, row in enumerate(rows):
                    valid = numeric(row.get(axis_column)) and numeric(row.get(current))
                    observation = {'source_row': index + source_offset, 'channel': current,
                                   'x_column': axis_column, 'x_unit': 's' if is_time_trace else 'V',
                                   'original_x': row.get(axis_column), 'original_current': row.get(current),
                                   'x_scale': axis_scale, 'current_scale': channel['current_scale'],
                                   'polarity': channel['polarity'],
                                   'x': row[axis_column] * axis_scale if valid else None,
                                   'current_a': row[current] * channel['current_scale'] * channel['polarity'] if valid else None,
                                   'branch_ids': [], 'fit_branch_ids': [],
                                   'reason': 'Not selected by sweep direction.' if valid else 'Missing or non-finite axis/current value.'}
                    observations[index] = observation
                    summary['observations'].append(observation)
            # Gaps split branches: do not interpolate absent observations into a sweep.
            blocks = []
            for pair in pairs:
                if not blocks or pair[0] != blocks[-1][-1][0] + 1:
                    blocks.append([])
                blocks[-1].append(pair)
            for block in blocks:
                for start, stop in segments([p[1] for p in block]):
                    branch = block[start:stop]
                    window = options.get('fit_window')
                    fit_pairs = [p for p in branch if not window or window[0] <= p[1] <= window[1]]
                    fit = None if is_time_trace else transport_methods.linear_fit([p[1:] for p in fit_pairs])
                    transport_methods.interpret_fit(fit, channel, channel_voltage, options)
                    direction = (
                        'time trace' if is_time_trace else
                        'increasing' if branch[-1][1] >= branch[0][1] else
                        'decreasing'
                    )
                    selected_direction = options.get("sweep_direction", "dual")
                    if not is_time_trace and selected_direction in {"increasing", "decreasing"} and direction != selected_direction:
                        continue
                    identity = f's{len(result["sheets"])+1}-b{len(summary["branches"])+1}'
                    fit_rows = [p[0] + source_offset for p in fit_pairs] if fit else []
                    for index, _, _ in branch:
                        observations[index]['branch_ids'].append(identity)
                        observations[index]['reason'] = 'Included in time trace.' if is_time_trace else 'Outside fit window.' if window and not window[0] <= observations[index]['x'] <= window[1] else 'Fit unavailable: at least three finite observations and varying x are required.'
                        if index + source_offset in fit_rows:
                            observations[index]['fit_branch_ids'].append(identity)
                            observations[index]['reason'] = 'Included in fit.'
                    summary['branches'].append({'branch_id': identity, 'channel': current, 'terminal': channel['instrument_terminal'],
                        'terminal_role': channel['terminal_role'],
                        'voltage': None if is_time_trace else channel_voltage,
                        'x_column': axis_column, 'x_unit': 's' if is_time_trace else 'V',
                        'first_row': branch[0][0] + source_offset, 'last_row': branch[-1][0] + source_offset,
                        'direction': direction,
                        'source_rows': [p[0] + source_offset for p in branch], 'fit_source_rows': fit_rows,
                        'fit_window': window if not is_time_trace else None,
                        'axis_scale': axis_scale, 'current_scale': channel['current_scale'], 'polarity': channel['polarity'],
                        'points': [[p[1], p[2]] for p in branch], 'fit': fit})
            time_column = options.get('time_column', 'Time')
            for window in options.get('windows', []):
                pairs = [(index + source_offset, row[time_column] * options.get('time_scale', 1), row[current] * channel['current_scale'] * channel['polarity'])
                         for index, row in enumerate(rows) if numeric(row.get(time_column)) and numeric(row.get(current))
                         and window['start'] <= row[time_column] * options.get('time_scale', 1) <= window['end']]
                summary['windows'].append({'channel': current, **window, 'point_count': len(pairs),
                    **transport_methods.window_statistics([p[1:] for p in pairs], [p[0] for p in pairs])})
        for observation in summary['observations']:
            if observation['fit_branch_ids']:
                observation['reason'] = 'Included in fit.'
        if options.get('channels') and len(channels) >= 2:
            sums = [sum(row[ch['current']] * ch['current_scale'] * ch['polarity'] for ch in channels)
                    for row in rows if all(numeric(row.get(ch['current'])) for ch in channels)]
            summary['diagnostics']['signed_current_sum_a'] = transport._statistics(sums)
            summary['diagnostics']['meaning'] = 'Sum of the selected signed channels; not a trapped-charge measurement.'
        result['sheets'].append(summary)
    if not result['sheets']:
        raise ValueError('No measurement worksheet matches the selection.')
    result['warnings'].append('Default channel units are inferred as A/V/s. Confirm scales, polarity, wiring and voltage status before physical interpretation.')
    result['warnings'].append('Settings rows retain their acquisition labels. No named sweep rate is converted to V/s without timing evidence.')
    result['summary'] = {'worksheet_count': len(result['sheets']), 'branch_count': sum(len(s['branches']) for s in result['sheets']),
                         'window_count': sum(len(s['windows']) for s in result['sheets'])}
    references = [ref for sheet in result['sheets'] for branch in sheet['branches'] for ref in (branch.get('fit') or {}).get('reference_ids', [])]
    references += [ref for sheet in result['sheets'] for window in sheet['windows'] for ref in window['reference_ids']]
    result['references'] = transport_methods.cited(references)
    result['resolved_options'] = options
    return result
