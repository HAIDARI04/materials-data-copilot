/* Transport display uses saved, row-aligned measurements; it never changes analysis data. */
const TRANSPORT_COLORS = ['#226e61', '#d47534', '#8465a5', '#287bb5', '#c44464', '#72712f'];
const TRANSPORT_STYLES = { solid: [], dashed: [8, 5], dotted: [2, 4], 'dash-dot': [8, 4, 2, 4], 'long-dash': [14, 5], 'dash-dot-dot': [8, 4, 2, 4, 2, 4], 'short-dash': [4, 4], 'long-dash-dot': [14, 4, 2, 4], 'dense-dot': [1, 2], 'long-dash-dot-dot': [14, 4, 2, 4, 2, 4] };

async function loadTransportSelection(files, request) {
  return Promise.all(files.map(async file => {
    try {
      const history = (await request(`/research/files/${encodeURIComponent(file.file_id)}/analyses`))
        .filter(run => run.model_name === 'research_transport')
        .sort((a, b) => String(b.processed_at).localeCompare(String(a.processed_at)));
      if (!history.length) return { file, history, message: 'No saved analysis. Click Analyze and save selected.' };
      const result = await request(`/research/analyses/${encodeURIComponent(history[0].processing_id)}`);
      return { file, history, result };
    } catch (error) {
      return { file, history: [], message: `Could not load analysis: ${error.message}` };
    }
  }));
}

function showTransportResults(results, unavailable = []) {
  const target = document.querySelector('#research-result');
  const previousState = target.querySelector('#research-plot')?.transportPlotState;
  target.innerHTML = `<h3>Transport overlay · ${results.length} file(s)</h3>
    ${unavailable.length ? `<ul role="status">${unavailable.map(item => `<li>${escapeHtml(item.file.original_filename)}: ${escapeHtml(item.message)}</li>`).join('')}</ul>` : ''}
    <div id="research-plot"></div>
    <details><summary>Source analyses and exports</summary>${results.map(result => `<article><h4>${escapeHtml(result.original_filename)}</h4>
      ${transportExportControls(result)}
      <p>${escapeHtml((result.warnings || []).join(' '))}</p>
      ${researchJson({
    summary: result.summary, parameters: result.parameters, source_sha256: result.source_sha256, result_sha256: result.result_sha256,
    sheets: result.sheets?.map(sheet => ({ name: sheet.name, windows: sheet.windows, branches: sheet.branches?.map(branch => ({ channel: branch.channel, direction: branch.direction, fit: branch.fit })), diagnostics: sheet.diagnostics })), settings: result.settings
  })}
    </article>`).join('')}</details>`;
  bindTransportExports(target);
  if (results.length) renderTransportPlot(results, target.querySelector('#research-plot'), previousState);
  else target.querySelector('#research-plot').innerHTML = '<p>No selected files have a readable saved Transport analysis.</p>';
}

function transportVariable(column, sheet, options) {
  const channel = (sheet.channels || []).find(item => item.current === column);
  const voltage = (sheet.channels || []).find(item => item.voltage === column);
  if (column === (options.time_column || 'Time')) {
    return { unit: 's', scale: options.time_scale ?? 1 };
  }
  if (channel) return { unit: 'A', scale: (channel.current_scale ?? 1) * (channel.polarity ?? 1) };
  if (voltage) return { unit: 'V', scale: voltage.voltage_scale ?? 1 };
  if (['AI', 'BI', 'DrainI', 'GateI', 'SourceI'].includes(column)) return { unit: 'A', scale: 1 };
  if (['AV', 'BV', 'DrainV', 'GateV', 'SourceV'].includes(column)) return { unit: 'V', scale: 1 };
  return { unit: column === 'RES' ? 'Ω' : '', scale: 1 };
}

function transportNumericColumns(sheets) {
  return [...new Set(sheets.flatMap(sheet => (sheet.columns || []).filter(column =>
    (sheet.rows || []).some(row => Number.isFinite(row[column])))))];
}

function transportTraceBlocks(points, splitTurns) {
  const blocks = [];
  let block = [], direction = 0;
  const flush = () => { if (block.length) blocks.push(block); block = []; direction = 0; };
  for (const point of points) {
    if (!point.every(Number.isFinite)) { flush(); continue; }
    if (block.length) {
      const previous = block[block.length - 1];
      const sign = Math.sign(point[0] - previous[0]);
      if (splitTurns && sign && direction && sign !== direction) {
        blocks.push(block);
        block = [previous];
      }
      if (sign) direction = sign;
    }
    block.push(point);
  }
  flush();
  return blocks;
}

function buildTransportTraces(result, xChoice, yChoice) {
  if (Array.isArray(result)) {
    return result.flatMap((source, sourceIndex) => {
      const sourceId = source.file_id || source.processing_id || String(sourceIndex);
      const filename = source.original_filename || `File ${sourceIndex + 1}`;
      return buildTransportTraces(source, xChoice, yChoice).map(trace => ({
        ...trace, id: JSON.stringify([sourceId, source.processing_id, trace.id]),
        sourceId, filename, label: `${filename} · ${trace.label}`,
        // Only set colorKey if not already set (preserve worksheet-specific colorKey)
        colorKey: trace.colorKey || sourceId,
      }));
    });
  }
  const options = result.parameters?.options || {};
  const traces = [];
  const channels = [...new Set((result.sheets || []).flatMap(sheet => transportNumericColumns([sheet])))];
  const currentChannels = [...new Set((result.sheets || []).flatMap(sheet => (sheet.channels || []).map(channel => channel.current)))];
  const styleChannels = [...new Set([...currentChannels, ...channels])];
  const styles = Object.keys(TRANSPORT_STYLES);

  for (const [sheetIndex, sheet] of (result.sheets || []).entries()) {
    const columns = transportNumericColumns([sheet]);
    const currents = (sheet.channels || []).map(channel => channel.current).filter(column => columns.includes(column));
    const yColumns = yChoice === 'auto' ? [...new Set(currents)] : [yChoice];
    for (const yColumn of yColumns) {
      if (!columns.includes(yColumn)) continue;
      const branch = (sheet.branches || []).find(item => item.channel === yColumn) || sheet.branches?.[0];
      const xColumn = xChoice === 'auto'
        ? sheet.measurement_type === 'current_time' ? (options.time_column || 'Time')
          : branch?.x_column || branch?.voltage
        : xChoice;
      if (!columns.includes(xColumn)) continue;
      const xVariable = transportVariable(xColumn, sheet, options);
      const yVariable = transportVariable(yColumn, sheet, options);
      const channel = (sheet.channels || []).find(item => item.current === yColumn);
      const pairs = (sheet.rows || []).map(row => [
        Number.isFinite(row[xColumn]) ? row[xColumn] * xVariable.scale : null,
        Number.isFinite(row[yColumn]) ? row[yColumn] * yVariable.scale : null,
      ]);
      const blocks = transportTraceBlocks(pairs, xVariable.unit === 'V');
      blocks.forEach((points, index) => {
        const direction = xVariable.unit === 's' ? 'time trace'
          : xVariable.unit === 'V' ? (points.at(-1)[0] >= points[0][0] ? 'increasing' : 'decreasing')
            : 'trace';
        // Create unique colorKey per worksheet using file_id + sheetIndex
        const colorKey = result.file_id ? `${result.file_id}_sheet_${sheetIndex}` : result.processing_id || 'measurement';
        traces.push({
          id: JSON.stringify([sheetIndex, xColumn, yColumn, index]),
          label: `${sheet.name} · ${yColumn} · ${direction} ${index + 1}`,
          worksheet: sheet.name, channel: yColumn, direction,
          role: channel?.terminal_role || 'Unknown',
          savedDirection: options.sweep_direction,
          xColumn, yColumn, xUnit: xVariable.unit, yUnit: yVariable.unit, points,
          colorKey,
        });
      });
    }
  }

  for (const trace of traces) {
    // Use channel as style key so different channels of same measurement get same base style
    const channelIndex = styleChannels.indexOf(trace.channel);
    // Direction modifier: 0=increasing/AI, 1=decreasing/BI
    const directionIndex = (trace.direction === 'decreasing' || trace.direction === 'BI') ? 1 : 0;
    trace.defaultStyle = styles[(channelIndex * 2 + directionIndex) % styles.length];
  }
  return traces;
}

function transportDisplayPoints(points, mode) {
  return points.map(([x, y]) => [x, !Number.isFinite(y) ? null : mode === 'log'
    ? (y === 0 ? null : Math.log10(Math.abs(y))) : mode === 'absolute' ? Math.abs(y) : y]);
}

function transportZoomBounds(bounds, xFraction, yFraction, factor) {
  const anchorX = bounds.xLow + (bounds.xHigh - bounds.xLow) * xFraction;
  const anchorY = bounds.yHigh - (bounds.yHigh - bounds.yLow) * yFraction;
  return {
    xLow: anchorX + (bounds.xLow - anchorX) * factor,
    xHigh: anchorX + (bounds.xHigh - anchorX) * factor,
    yLow: anchorY + (bounds.yLow - anchorY) * factor,
    yHigh: anchorY + (bounds.yHigh - anchorY) * factor
  };
}

function transportAxisTick(value, mode) {
  if (mode !== 'log') return value.toPrecision(3);
  const magnitude = 10 ** value;
  return Number.isFinite(magnitude) && magnitude > 0 ? magnitude.toExponential(2) : `10^${value.toFixed(2)}`;
}

function transportValidBounds(bounds) {
  return Object.values(bounds).every(Number.isFinite)
    && bounds.xHigh > bounds.xLow && bounds.yHigh > bounds.yLow
    && Number.isFinite(bounds.xHigh - bounds.xLow) && Number.isFinite(bounds.yHigh - bounds.yLow);
}

function renderTransportPlot(result, host, previousState = null) {
  const sources = Array.isArray(result) ? result : [result];
  const preferences = typeof PlotPreferences === 'undefined' ? null : PlotPreferences;
  const preferenceKey = `transport:${JSON.stringify(sources.map(source => source.file_id || source.processing_id).sort())}`;
  const saved = preferences?.read(preferenceKey);
  if (saved) {
    const pairs = value => Array.isArray(value) ? value.filter(item => Array.isArray(item) && item.length === 2 && typeof item[0] === 'string') : [];
    previousState = {
      ...saved,
      settings: new Map(pairs(saved.settings).filter(([, state]) => state && typeof state.visible === 'boolean' && /^#[0-9a-f]{6}$/i.test(state.color) && Object.hasOwn(TRANSPORT_STYLES, state.style))),
      channelColors: new Map(pairs(saved.channelColors).filter(([, color]) => /^#[0-9a-f]{6}$/i.test(color))),
    };
  }
  const columns = transportNumericColumns(sources.flatMap(source => source.sheets || []));
  if (!columns.length) {
    host.innerHTML = '<p>No saved numeric rows are available. Analyze and save the selected file again.</p>';
    return;
  }
  const axisChoices = [['auto', 'Automatic'], ...columns.map(column => [column, column])];
  host.innerHTML = `<div class="research-form research-plot-controls">
    ${researchSelect('X axis', 'transport_x', axisChoices)}
    ${researchSelect('Y axis', 'transport_y', [['auto', 'Current channels'], ...axisChoices.slice(1)])}
    <div class="plot-series-control"><span id="transport-series-label" class="plot-series-label">Plot series</span>
      <div class="plot-series-dropdown"><button type="button" class="button" id="transport-series-toggle" aria-labelledby="transport-series-label transport-series-toggle" aria-expanded="false" aria-controls="transport-series-menu">All selected</button>
        <div id="transport-series-menu" class="plot-series-menu" hidden></div>
      </div>
    </div>
    ${researchSelect('Display', 'transport_mode', [['signed', 'Signed values'], ['absolute', 'Absolute values'], ['log', 'Absolute values, logarithmic scale']])}
    <!-- Left drag zoom, Middle drag pan - no selector needed -->
  </div>
  <div class="transport-plot-toolbar"><button type="button" class="button compact" id="transport-zoom-in">Zoom in</button><button type="button" class="button compact" id="transport-zoom-out">Zoom out</button><button type="button" class="button compact" id="transport-reset-view">Reset view</button><label><input type="checkbox" name="transport_inset"> Show transport inset</label><button type="button" class="button compact" id="transport-reset-style">Reset line styles</button></div>
  <div class="transport-plot-shell"><canvas class="transport-chart" id="transport-chart" role="img" tabindex="0" aria-label="Transport measurements"></canvas>
    <aside class="transport-inset" hidden aria-label="Transport trace editor">
      <div class="transport-inset-handle" tabindex="0" aria-label="Move transport inset with arrow keys or drag"><strong>Transport traces</strong><button type="button" class="button compact" id="transport-hide-inset">Hide</button></div>
      <div class="transport-inset-body"><p>Drag the header to move this box. Choose a color and line style for each trace.</p><div id="transport-trace-editors"></div></div>
    </aside>
  </div>
  <details class="transport-legend-dropdown">
    <summary id="transport-legend-summary">Plotted lines</summary>
    <ul class="research-plot-legend" id="transport-legend"></ul>
  </details><div class="research-chart-properties" id="transport-ranges" role="status"></div>`;
  const get = selector => host.querySelector(selector);
  const canvas = get('#transport-chart');
  const inset = get('.transport-inset');
  const menu = get('#transport-series-menu');
  const toggle = get('#transport-series-toggle');
  const settings = previousState?.settings || new Map();
  const channelColors = previousState?.styleVersion === 2 ? previousState.channelColors || new Map() : new Map();
  // Preserve explicit styling while replacing recognizable defaults from the old channel-color scheme.
  if (previousState?.styleVersion !== 2) {
    for (const trace of buildTransportTraces(result, previousState?.transport_x || 'auto', previousState?.transport_y || 'auto')) {
      const state = settings.get(trace.id);
      const oldColor = previousState?.channelColors?.get(trace.sourceId ? JSON.stringify([trace.sourceId, trace.channel]) : trace.channel);
      if (state && state.color === oldColor) delete state.color;
      if (state && state.style === (trace.direction === 'decreasing' ? 'dashed' : 'solid')) delete state.style;
    }
  }
  for (const name of ['transport_x', 'transport_y', 'transport_mode']) {
    const value = previousState?.[name];
    if (value && (name === 'transport_mode' ? ['signed', 'absolute', 'log'].includes(value) : value === 'auto' || columns.includes(value))) get(`[name=${name}]`).value = value;
  }
  // Drag behavior: left button zoom, middle button pan
  get('[name=transport_inset]').checked = previousState?.insetVisible === true;
  inset.hidden = !get('[name=transport_inset]').checked;
  host.transportPlotState = { settings, channelColors, styleVersion: 2 };
  const zoomViews = new Map(Array.isArray(saved?.zoomViews) ? saved.zoomViews.filter(item => Array.isArray(item) && typeof item[0] === 'string' && item[1] && transportValidBounds(item[1])) : []);
  const savedPosition = preferences?.position(previousState?.insetPosition);
  if (savedPosition) {
    inset.style.right = 'auto'; inset.style.left = `${savedPosition.left}px`; inset.style.top = `${savedPosition.top}px`;
  }
  const savePreferences = () => {
    const value = {
      ...host.transportPlotState, settings: [...settings], channelColors: [...channelColors],
      zoomViews: [...zoomViews], insetVisible: get('[name=transport_inset]').checked,
      insetPosition: preferences?.position({ left: parseFloat(inset.style.left), top: parseFloat(inset.style.top) }),
    };
    host.transportPlotState.insetVisible = value.insetVisible;
    host.transportPlotState.insetPosition = value.insetPosition;
    preferences?.write(preferenceKey, value);
  };
  let plotPanels = [], gesture = null;
  let traces = [];
  function traceSettings(trace) {
    const colorKey = trace.colorKey || trace.channel;
    if (!channelColors.has(colorKey)) channelColors.set(colorKey, TRANSPORT_COLORS[channelColors.size % TRANSPORT_COLORS.length]);
    const savedDirection = trace.savedDirection;
    if (!settings.has(trace.id)) settings.set(trace.id, {
      visible: !['increasing', 'decreasing'].includes(savedDirection)
        || !['increasing', 'decreasing'].includes(trace.direction) || trace.direction === savedDirection,
      color: channelColors.get(colorKey),
      style: trace.defaultStyle,
    });
    const state = settings.get(trace.id);
    state.color ??= channelColors.get(colorKey);
    state.style ??= trace.defaultStyle;
    return state;
  }
  const swatch = state => `<svg width="28" height="10" aria-hidden="true"><line x1="0" y1="5" x2="28" y2="5" stroke="${state.color}" stroke-width="2" stroke-dasharray="${TRANSPORT_STYLES[state.style].join(' ')}"/></svg>`;
  function updateVisibility() {
    host.querySelectorAll('[data-trace-visible]').forEach(input => {
      input.checked = traceSettings(traces[Number(input.dataset.traceVisible)]).visible;
    });
    host.querySelectorAll('[data-trace-group]').forEach(input => {
      const members = input.dataset.traceGroup.split(',').map(index => traces[Number(index)]);
      const count = members.filter(trace => traceSettings(trace).visible).length;
      input.checked = count === members.length;
      input.indeterminate = count > 0 && count < members.length;
      input.closest('label').querySelector('small').textContent = `${count}/${members.length} selected`;
    });
    const count = traces.filter(trace => traceSettings(trace).visible).length;
    toggle.textContent = `${count} of ${traces.length} selected`;
  }
  function rebuild() {
    traces = buildTransportTraces(result, get('[name=transport_x]').value, get('[name=transport_y]').value);
    const channels = [...new Set(traces.map(trace => trace.channel))];
    menu.innerHTML = `<div class="plot-series-actions">${['All', 'Clear', 'Increasing', 'Decreasing', 'AI', 'BI'].map(action => `<button type="button" class="button compact" data-trace-action="${action.toLowerCase()}">${action}</button>`).join('')}</div>` +
      channels.map(channel => {
        const members = traces.map((trace, index) => ({ trace, index })).filter(item => item.trace.channel === channel);
        return `<div class="plot-series-group"><label class="plot-series-group-header"><input type="checkbox" data-trace-group="${members.map(item => item.index).join(',')}"><strong>${escapeHtml(channel)}</strong><small></small></label>${members.map(({ trace, index }) => `<label><input type="checkbox" data-trace-visible="${index}">${escapeHtml(trace.label)}</label>`).join('')}</div>`;
      }).join('');
    get('#transport-trace-editors').innerHTML = traces.map((trace, index) => {
      const state = traceSettings(trace);
      return `<div class="transport-trace-editor"><label class="transport-trace-title"><input type="checkbox" data-trace-visible="${index}">${escapeHtml(trace.label)}</label>
        <small>${escapeHtml(trace.role)} · ${trace.points.length} points · ${escapeHtml(trace.xColumn)} → ${escapeHtml(trace.yColumn)}</small>
        <div class="transport-line-options"><label>Line color<input type="color" value="${state.color}" data-trace-color="${index}" aria-label="Line color for ${escapeHtml(trace.label)}"></label>
        <label>Line style<select data-trace-style="${index}" aria-label="Line style for ${escapeHtml(trace.label)}">${Object.keys(TRANSPORT_STYLES).map(style => `<option value="${style}" ${style === state.style ? 'selected' : ''}>${style[0].toUpperCase() + style.slice(1)}</option>`).join('')}</select></label></div></div>`;
    }).join('') || '<p>No numeric observations match these axes.</p>';
    updateVisibility();
    draw();
  }
  function draw() {
    plotPanels = [];
    // Cursor: grabbing when panning (middle drag), crosshair otherwise
    canvas.style.cursor = gesture?.pan ? 'grabbing' : 'crosshair';
    for (const name of ['transport_x', 'transport_y', 'transport_mode']) host.transportPlotState[name] = get(`[name=${name}]`).value;
    const width = Math.max(320, canvas.clientWidth || 900), height = 500;
    const ratio = window.devicePixelRatio || 1;
    canvas.width = width * ratio; canvas.height = height * ratio;
    const ctx = canvas.getContext('2d');
    ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
    ctx.fillStyle = '#ffffff'; ctx.fillRect(0, 0, width, height);
    const selected = traces.filter(trace => traceSettings(trace).visible);
    const mode = get('[name=transport_mode]').value;
    // Keep different physical units in separate vertical panels, never on one ambiguous axis.
    const groups = new Map();
    for (const trace of selected) {
      const groupKey = JSON.stringify([trace.xUnit || trace.xColumn, trace.yUnit || trace.yColumn]);
      if (!groups.has(groupKey)) groups.set(groupKey, []);
      groups.get(groupKey).push({ ...trace, display: transportDisplayPoints(trace.points, mode) });
    }
    const rangeLabels = [];
    let plotted = 0;
    for (const [groupIndex, group] of [...groups.values()].entries()) {
      const finite = group.flatMap(trace => trace.display).filter(point => point.every(Number.isFinite));
      if (!finite.length) continue;
      let xmin = Infinity, xmax = -Infinity, ymin = Infinity, ymax = -Infinity;
      for (const [x, y] of finite) { xmin = Math.min(xmin, x); xmax = Math.max(xmax, x); ymin = Math.min(ymin, y); ymax = Math.max(ymax, y); }
      const xNames = [...new Set(group.map(trace => trace.xColumn))].join(' / ');
      const yNames = [...new Set(group.map(trace => trace.yColumn))].join(' / ');
      const xLabel = `${xNames}${group[0].xUnit ? ` (${group[0].xUnit})` : ''}`;
      const baseYLabel = `${yNames}${group[0].yUnit ? ` (${group[0].yUnit})` : ''}`;
      const yLabel = mode === 'signed' ? baseYLabel : mode === 'absolute' ? `|${baseYLabel}|` : `|${baseYLabel}| (log scale)`;
      const panelHeight = height / groups.size;
      const frame = { left: 78, right: width - 24, top: groupIndex * panelHeight + 24, bottom: (groupIndex + 1) * panelHeight - 62 };
      // Give constant-value traces a visible center position.
      const xPad = xmax === xmin ? Math.max(Math.abs(xmin) * .05, 1e-12) : 0;
      const yPad = ymax === ymin ? Math.max(Math.abs(ymin) * .05, 1e-12) : (ymax - ymin) * .04;
      const panelKey = JSON.stringify([group[0].xUnit || group[0].xColumn, group[0].yUnit || group[0].yColumn]);
      const bounds = zoomViews.get(panelKey) || { xLow: xmin - xPad, xHigh: xmax + xPad, yLow: ymin - yPad, yHigh: ymax + yPad };
      const { xLow, xHigh, yLow, yHigh } = bounds;
      plotPanels.push({ key: panelKey, frame, bounds });
      const px = value => frame.left + (value - xLow) / (xHigh - xLow) * (frame.right - frame.left);
      const py = value => frame.bottom - (value - yLow) / (yHigh - yLow) * (frame.bottom - frame.top);
      ctx.font = '11px system-ui'; ctx.lineWidth = 1; ctx.setLineDash([]);
      for (let index = 0; index <= 4; index++) {
        const x = xLow + (xHigh - xLow) * index / 4, y = yLow + (yHigh - yLow) * index / 4;
        ctx.strokeStyle = '#e1e8e4'; ctx.beginPath(); ctx.moveTo(px(x), frame.top); ctx.lineTo(px(x), frame.bottom); ctx.stroke();
        ctx.beginPath(); ctx.moveTo(frame.left, py(y)); ctx.lineTo(frame.right, py(y)); ctx.stroke();
        ctx.fillStyle = '#66746f'; ctx.textAlign = 'center'; ctx.fillText(x.toPrecision(3), px(x), frame.bottom + 18);
        ctx.textAlign = 'right'; ctx.fillText(transportAxisTick(y, mode), frame.left - 8, py(y) + 4);
      }
      ctx.strokeStyle = '#71837c'; ctx.strokeRect(frame.left, frame.top, frame.right - frame.left, frame.bottom - frame.top);
      ctx.fillStyle = '#192522'; ctx.textAlign = 'center'; ctx.fillText(xLabel, (frame.left + frame.right) / 2, frame.bottom + 44);
      ctx.save(); ctx.translate(14, (frame.top + frame.bottom) / 2); ctx.rotate(-Math.PI / 2); ctx.fillText(yLabel, 0, 0); ctx.restore();
      ctx.save(); ctx.beginPath(); ctx.rect(frame.left, frame.top, frame.right - frame.left, frame.bottom - frame.top); ctx.clip();
      for (const trace of group) {
        const state = traceSettings(trace);
        ctx.strokeStyle = state.color; ctx.fillStyle = state.color; ctx.lineWidth = 2; ctx.setLineDash(TRANSPORT_STYLES[state.style]); ctx.beginPath();
        let active = false;
        for (const [x, y] of trace.display) {
          if (!Number.isFinite(x) || !Number.isFinite(y)) { active = false; continue; }
          active ? ctx.lineTo(px(x), py(y)) : ctx.moveTo(px(x), py(y)); active = true;
        }
        ctx.stroke();
        if (trace.display.filter(point => point.every(Number.isFinite)).length === 1) {
          const point = trace.display.find(point => point.every(Number.isFinite));
          ctx.beginPath(); ctx.arc(px(point[0]), py(point[1]), 3, 0, Math.PI * 2); ctx.fill();
        }
      }
      ctx.restore(); ctx.setLineDash([]);
      plotted += group.length;
      rangeLabels.push(`X: ${xLabel}, ${xLow.toPrecision(5)} to ${xHigh.toPrecision(5)}; Y: ${yLabel}, ${transportAxisTick(yLow, mode)} to ${transportAxisTick(yHigh, mode)}`);
    }
    if (gesture && !gesture.pan) {
      ctx.fillStyle = 'rgba(34,110,97,.12)'; ctx.strokeStyle = '#226e61'; ctx.lineWidth = 1; ctx.setLineDash([4, 3]);
      const left = Math.min(gesture.start.x, gesture.end.x), top = Math.min(gesture.start.y, gesture.end.y);
      const width = Math.abs(gesture.end.x - gesture.start.x), height = Math.abs(gesture.end.y - gesture.start.y);
      ctx.fillRect(left, top, width, height); ctx.strokeRect(left, top, width, height); ctx.setLineDash([]);
    }
    get('#transport-reset-view').disabled = !zoomViews.size;
    get('#transport-legend').innerHTML = selected.map(trace => `<li>${swatch(traceSettings(trace))}${escapeHtml(trace.label)}</li>`).join('');
    get('#transport-legend-summary').textContent = `Plotted lines (${selected.length})`;
    const plottedSources = new Set(selected.filter(trace => trace.points.some(point => transportDisplayPoints([point], mode)[0].every(Number.isFinite))).map(trace => trace.sourceId || 'single'));
    const sourceCount = new Set(sources.map((source, index) => Array.isArray(result)
      ? source.file_id || source.processing_id || String(index) : 'single')).size;
    const missingSources = sourceCount - plottedSources.size;
    get('#transport-ranges').textContent = plotted ? `${plottedSources.size} file(s), ${plotted} traces. ${rangeLabels.join(' · ')}${groups.size > 1 ? ' Different axis units are shown in separate panels.' : ''}${missingSources > 0 ? ` ${missingSources} file(s) have no visible data for the selected axes and display mode.` : ''}${mode === 'log' ? ' Zero values are omitted; gaps are preserved.' : ''}` : 'No selected numeric data for these axes and display mode.';
    canvas.setAttribute('aria-label', get('#transport-ranges').textContent);
    savePreferences();
  }
  host.addEventListener('change', event => {
    const input = event.target;
    if (['transport_x', 'transport_y', 'transport_mode'].includes(input.name)) {
      gesture = null; zoomViews.clear(); rebuild(); return;
    }
    if (input.name === 'transport_inset') inset.hidden = !input.checked;
    if (input.dataset.traceVisible !== undefined) {
      traceSettings(traces[Number(input.dataset.traceVisible)]).visible = input.checked;
      updateVisibility();
    }
    if (input.dataset.traceGroup !== undefined) {
      input.dataset.traceGroup.split(',').forEach(index => { traceSettings(traces[Number(index)]).visible = input.checked; });
      updateVisibility();
    }
    if (input.dataset.traceStyle !== undefined) traceSettings(traces[Number(input.dataset.traceStyle)]).style = input.value;
    if (input.dataset.traceColor !== undefined) traceSettings(traces[Number(input.dataset.traceColor)]).color = input.value;
    draw();
  });
  host.addEventListener('input', event => {
    if (event.target.dataset.traceColor !== undefined) {
      traceSettings(traces[Number(event.target.dataset.traceColor)]).color = event.target.value;
      draw();
    }
  });
  const closeMenu = () => { menu.hidden = true; toggle.setAttribute('aria-expanded', 'false'); };
  toggle.addEventListener('click', () => { menu.hidden = !menu.hidden; toggle.setAttribute('aria-expanded', String(!menu.hidden)); });
  host.addEventListener('keydown', event => { if (event.key === 'Escape') { closeMenu(); toggle.focus(); } });
  // Auto-close dropdown when mouse leaves the dropdown area
  host.addEventListener('mouseleave', event => {
    if (event.target.closest('.plot-series-dropdown')) closeMenu();
  });
  host.addEventListener('click', event => {
    if (!event.target.closest('.plot-series-dropdown')) closeMenu();
    const action = event.target.closest('[data-trace-action]')?.dataset.traceAction;
    if (action) {
      const filterMap = {
        'all': null,
        'clear': null,
        'increasing': 'increasing',
        'decreasing': 'decreasing',
        'ai': 'AI',
        'bi': 'BI'
      };
      const filterDir = filterMap[action];
      if (action === 'all') traces.forEach(trace => { traceSettings(trace).visible = true; });
      else if (action === 'clear') traces.forEach(trace => { traceSettings(trace).visible = false; });
      else if (filterDir) traces.forEach(trace => { traceSettings(trace).visible = trace.direction === filterDir; });
      updateVisibility(); draw();
    }
  });
  get('#transport-hide-inset').addEventListener('click', () => { inset.hidden = true; get('[name=transport_inset]').checked = false; savePreferences(); });
  get('#transport-reset-style').addEventListener('click', () => {
    settings.forEach(state => { delete state.color; delete state.style; });
    for (const trace of traces) {
      const state = traceSettings(trace);
      state.color = channelColors.get(trace.colorKey || trace.channel); state.style = trace.defaultStyle;
    }
    // Discard styles for inactive axis combinations as well.
    for (const [id, state] of settings) if (!state.color) settings.delete(id);
    rebuild();
  });
  const position = event => {
    const rect = canvas.getBoundingClientRect();
    return {
      x: (event.clientX - rect.left) * Math.max(320, canvas.clientWidth || 900) / rect.width,
      y: (event.clientY - rect.top) * 500 / rect.height
    };
  };
  const panelAt = point => plotPanels.find(({ frame }) => point.x >= frame.left && point.x <= frame.right && point.y >= frame.top && point.y <= frame.bottom);
  const setView = (panel, bounds) => { if (transportValidBounds(bounds)) zoomViews.set(panel.key, bounds); };
  const resetView = () => { gesture = null; zoomViews.clear(); draw(); };
  const zoomAll = factor => {
    gesture = null;
    plotPanels.forEach(panel => setView(panel, transportZoomBounds(panel.bounds, .5, .5, factor)));
    draw();
  };
  get('#transport-zoom-in').addEventListener('click', () => zoomAll(.8));
  get('#transport-zoom-out').addEventListener('click', () => zoomAll(1.25));
  get('#transport-reset-view').addEventListener('click', resetView);
  canvas.addEventListener('dblclick', resetView);
  canvas.addEventListener('auxclick', event => { if (event.button === 1) event.preventDefault(); });
  canvas.addEventListener('wheel', event => {
    if (gesture) return;
    const point = position(event), panel = panelAt(point);
    if (!panel) return;
    event.preventDefault();
    const delta = event.deltaY * (event.deltaMode === 1 ? 16 : event.deltaMode === 2 ? 500 : 1);
    const factor = Math.exp(Math.max(-.5, Math.min(.5, delta * .002)));
    const { frame } = panel;
    setView(panel, transportZoomBounds(panel.bounds, (point.x - frame.left) / (frame.right - frame.left), (point.y - frame.top) / (frame.bottom - frame.top), factor));
    draw();
  }, { passive: false });
  canvas.addEventListener('pointerdown', event => {
    if (event.button !== 0 && event.button !== 1) return;
    const start = position(event), panel = panelAt(start);
    if (!panel) return;
    // Left button (0) for zoom, Middle button (1) for pan
    const isPan = event.button === 1;
    gesture = { id: event.pointerId, start, end: start, panel, pan: isPan, original: zoomViews.get(panel.key) };
    canvas.setPointerCapture(event.pointerId); canvas.focus(); event.preventDefault(); draw();
  });
  canvas.addEventListener('pointermove', event => {
    if (gesture?.id !== event.pointerId) return;
    const point = position(event), { frame, bounds } = gesture.panel;
    gesture.end = { x: Math.max(frame.left, Math.min(frame.right, point.x)), y: Math.max(frame.top, Math.min(frame.bottom, point.y)) };
    if (gesture.pan) {
      const dx = (point.x - gesture.start.x) / (frame.right - frame.left) * (bounds.xHigh - bounds.xLow);
      const dy = (point.y - gesture.start.y) / (frame.bottom - frame.top) * (bounds.yHigh - bounds.yLow);
      setView(gesture.panel, { xLow: bounds.xLow - dx, xHigh: bounds.xHigh - dx, yLow: bounds.yLow + dy, yHigh: bounds.yHigh + dy });
    }
    draw();
  });
  canvas.addEventListener('pointerup', event => {
    if (gesture?.id !== event.pointerId) return;
    const { start, end, panel, pan } = gesture;
    // Only draw zoom box for left button (zoom mode)
    if (!pan && Math.abs(end.x - start.x) >= 6 && Math.abs(end.y - start.y) >= 6) {
      const { frame, bounds } = panel;
      const x = pixel => bounds.xLow + (pixel - frame.left) / (frame.right - frame.left) * (bounds.xHigh - bounds.xLow);
      const y = pixel => bounds.yHigh - (pixel - frame.top) / (frame.bottom - frame.top) * (bounds.yHigh - bounds.yLow);
      setView(panel, { xLow: x(Math.min(start.x, end.x)), xHigh: x(Math.max(start.x, end.x)), yLow: y(Math.max(start.y, end.y)), yHigh: y(Math.min(start.y, end.y)) });
    }
    gesture = null; canvas.releasePointerCapture(event.pointerId); draw();
  });
  const cancelGesture = () => {
    if (!gesture) return;
    if (gesture.original) zoomViews.set(gesture.panel.key, gesture.original);
    else zoomViews.delete(gesture.panel.key);
    gesture = null; draw();
  };
  canvas.addEventListener('pointercancel', cancelGesture);
  canvas.addEventListener('lostpointercapture', cancelGesture);
  canvas.addEventListener('keydown', event => {
    if (['+', '=', '-', 'Home', 'Escape'].includes(event.key)) {
      event.preventDefault(); event.stopPropagation();
      if (event.key === 'Home') resetView();
      else if (event.key === 'Escape') cancelGesture();
      else zoomAll(event.key === '-' ? 1.25 : .8);
    }
  });
  const handle = get('.transport-inset-handle');
  const shell = get('.transport-plot-shell');
  let drag = null;
  const moveInset = (left, top) => {
    inset.style.right = 'auto';
    inset.style.left = `${Math.max(0, Math.min(left, shell.clientWidth - (inset.offsetWidth || Math.min(320, shell.clientWidth - 24))))}px`;
    inset.style.top = `${Math.max(0, Math.min(top, shell.clientHeight - (inset.offsetHeight || Math.min(340, shell.clientHeight - 24))))}px`;
    savePreferences();
  };
  handle.addEventListener('pointerdown', event => {
    if (event.target.closest('button') || event.button !== 0) return;
    drag = { id: event.pointerId, x: event.clientX, y: event.clientY, left: inset.offsetLeft, top: inset.offsetTop };
    handle.setPointerCapture(event.pointerId); event.preventDefault();
  });
  handle.addEventListener('pointermove', event => {
    if (drag?.id === event.pointerId) moveInset(drag.left + event.clientX - drag.x, drag.top + event.clientY - drag.y);
  });
  for (const name of ['pointerup', 'pointercancel', 'lostpointercapture']) handle.addEventListener(name, () => { drag = null; });
  handle.addEventListener('keydown', event => {
    if (event.target !== handle) return;
    const offsets = { ArrowLeft: [-10, 0], ArrowRight: [10, 0], ArrowUp: [0, -10], ArrowDown: [0, 10] };
    if (offsets[event.key]) { event.preventDefault(); moveInset(inset.offsetLeft + offsets[event.key][0], inset.offsetTop + offsets[event.key][1]); }
  });
  const resize = new ResizeObserver(() => {
    if (!host.isConnected) { resize.disconnect(); return; }
    draw();
    if (inset.style.left) moveInset(inset.offsetLeft, inset.offsetTop);
  });
  resize.observe(canvas);
  rebuild();
  if (savedPosition) moveInset(savedPosition.left, savedPosition.top);
}

if (typeof module !== 'undefined' && module.exports) {
  module.exports = { transportNumericColumns, transportTraceBlocks, buildTransportTraces, transportDisplayPoints, renderTransportPlot, loadTransportSelection, transportZoomBounds, transportValidBounds, transportAxisTick };
}
