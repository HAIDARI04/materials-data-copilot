const test = require('node:test');
const assert = require('node:assert/strict');
const {
  transportNumericColumns, transportTraceBlocks, buildTransportTraces, transportDisplayPoints,
} = require('../static/transport_plot.js');

function fixture() {
  return {
    parameters: { options: { time_scale: .001 } },
    sheets: [{
      name: 'Run2109', measurement_type: 'current_time',
      columns: ['Time', 'AI', 'AV', 'BI', 'BV', 'RES', 'Note', 'Empty'],
      rows: [
        { Time: 0, AI: 1, AV: 5, BI: -1, BV: 0, RES: 10, Note: 'start' },
        { Time: 1000, AI: 2, AV: 5, BI: null, BV: 0, RES: 20 },
        { Time: 2000, AI: 3, AV: 5, BI: -3, BV: 0, RES: 30 },
      ],
      channels: [
        { current: 'AI', voltage: 'AV', current_scale: 2, voltage_scale: 1, polarity: -1 },
        { current: 'BI', voltage: 'BV', current_scale: 1, voltage_scale: 1, polarity: 1 },
      ],
      branches: [{ channel: 'AI', x_column: 'Time', x_unit: 's' }],
    }],
  };
}

test('numeric choices omit empty and text columns', () => {
  assert.deepEqual(transportNumericColumns(fixture().sheets), ['Time', 'AI', 'AV', 'BI', 'BV', 'RES']);
});

test('automatic I-t axes retain channel scales, polarity, time scale and missing-row gaps', () => {
  const result = fixture(), before = structuredClone(result);
  const traces = buildTransportTraces(result, 'auto', 'auto');
  assert.deepEqual(traces[0].points, [[0, -2], [1, -4], [2, -6]]);
  assert.equal(traces[0].xUnit, 's');
  assert.equal(traces[0].yUnit, 'A');
  assert.deepEqual(traces.filter(t => t.channel === 'BI').map(t => t.points), [[[0, -1]], [[2, -3]]]);
  assert.deepEqual(result, before);
});

test('either axis can use current, voltage, time or resistance without duplicate channel traces', () => {
  const result = fixture();
  assert.deepEqual(buildTransportTraces(result, 'AI', 'Time')[0].points, [[-2, 0], [-4, 1], [-6, 2]]);
  const resistance = buildTransportTraces(result, 'Time', 'RES');
  assert.equal(resistance.length, 1);
  assert.equal(resistance[0].yUnit, 'Ω');
  assert.deepEqual(resistance[0].points, [[0, 10], [1, 20], [2, 30]]);
  const voltage = buildTransportTraces(result, 'Time', 'AV');
  assert.equal(voltage.length, 1);
  assert.deepEqual(voltage[0].points, [[0, 5], [1, 5], [2, 5]]);
});

test('arbitrary axis pairs use the same source rows and preserve gaps', () => {
  const traces = buildTransportTraces(fixture(), 'BI', 'RES');
  assert.deepEqual(traces.map(t => t.points), [[[-1, 10]], [[-3, 30]]]);
  assert.deepEqual(buildTransportTraces(fixture(), 'Note', 'AI'), []);
});

test('voltage turnarounds are shared by adjacent branches', () => {
  assert.deepEqual(transportTraceBlocks([[-1, 1], [0, 2], [1, 3], [0, 4], [-1, 5]], true), [
    [[-1, 1], [0, 2], [1, 3]], [[1, 3], [0, 4], [-1, 5]],
  ]);
  const result = fixture();
  result.sheets[0].measurement_type = 'voltage_sweep';
  result.sheets[0].branches[0].x_column = 'AV';
  result.sheets[0].rows.forEach((row, i) => { row.AV = i - 1; });
  const traces = buildTransportTraces(result, 'auto', 'auto');
  assert.equal(traces[0].xUnit, 'V');
  assert.deepEqual(traces[0].points, [[-1, -2], [0, -4], [1, -6]]);
});

test('log display removes zeros without connecting across missing values or mutating signed data', () => {
  const points = [[0, -100], [1, 0], [2, null], [3, 10]];
  assert.deepEqual(transportDisplayPoints(points, 'log'), [[0, 2], [1, null], [2, null], [3, 1]]);
  assert.deepEqual(transportDisplayPoints(points, 'absolute'), [[0, 100], [1, 0], [2, null], [3, 10]]);
  assert.equal(points[0][1], -100);
});

test('missing worksheet variables do not invent observations', () => {
  const result = fixture();
  result.sheets.push({ name: 'Other', columns: ['Time', 'Temperature'], rows: [{ Time: 0, Temperature: 25 }], channels: [] });
  const traces = buildTransportTraces(result, 'Time', 'RES');
  assert.ok(traces.every(t => t.worksheet === 'Run2109'));
  assert.equal(buildTransportTraces(result, 'Time', 'Temperature')[0].yUnit, '');
});

test('multiple files retain separate identities and their own scales on shared axes', () => {
  const first = { ...fixture(), file_id: 'first', original_filename: 'first.xls' };
  const second = { ...fixture(), file_id: 'second', original_filename: 'second.xls' };
  second.parameters.options.time_scale = .002;
  second.sheets[0].channels[0].current_scale = 3;
  const before = structuredClone([first, second]);
  const traces = buildTransportTraces([first, second], 'Time', 'AI');
  assert.equal(traces.length, 2);
  assert.notEqual(traces[0].id, traces[1].id);
  assert.notEqual(traces[0].colorKey, traces[1].colorKey);
  assert.match(traces[0].label, /first.xls/);
  assert.match(traces[1].label, /second.xls/);
  assert.deepEqual(traces[0].points, [[0, -2], [1, -4], [2, -6]]);
  assert.deepEqual(traces[1].points, [[0, -3], [2, -6], [4, -9]]);
  assert.equal(traces[0].xUnit, traces[1].xUnit);
  assert.equal(traces[0].yUnit, traces[1].yUnit);
  assert.deepEqual([first, second], before);
});

test('selection loader gets the newest Transport analysis for every file and reports unavailable files', async () => {
  const { loadTransportSelection } = require('../static/transport_plot.js');
  const files = ['first', 'second', 'missing', 'failed'].map(file_id => ({ file_id }));
  const requests = [];
  const loaded = await loadTransportSelection(files, async path => {
    requests.push(path);
    if (path.includes('/failed/')) throw new Error('Offline');
    if (path.includes('/missing/')) return [];
    if (path.endsWith('/analyses')) {
      const file = path.includes('/first/') ? 'first' : 'second';
      return [
        { model_name: 'research_transport', processing_id: `${file}-old`, processed_at: '2026-01-01' },
        { model_name: 'research_pl', processing_id: 'wrong-technique', processed_at: '2026-12-01' },
        { model_name: 'research_transport', processing_id: `${file}-new`, processed_at: '2026-09-15' },
      ];
    }
    return { processing_id: path.split('/').at(-1) };
  });
  assert.deepEqual(loaded.slice(0, 2).map(item => item.result.processing_id), ['first-new', 'second-new']);
  assert.match(loaded[2].message, /No saved analysis/);
  assert.match(loaded[3].message, /Offline/);
  assert.equal(requests.length, 6);
  assert.ok(!requests.some(path => path.endsWith('-old') || path.endsWith('wrong-technique')));
  assert.deepEqual(await loadTransportSelection([], () => { throw new Error('Unexpected request'); }), []);
});

test('plot controller updates axes, line styles, visibility and movable inset', () => {
  // A DOM/canvas test double checks controller behavior, not browser layout.
  const { renderTransportPlot } = require('../static/transport_plot.js');
  const calls = [];
  const context = new Proxy({}, {
    get: (_, method) => (...args) => calls.push([method, ...args]),
    set: (_, name, value) => { calls.push([name, value]); return true; },
  });
  const elements = new Map();
  function element(selector) {
    if (!elements.has(selector)) elements.set(selector, {
      value: 'auto', checked: true, hidden: false, dataset: {}, style: {}, attributes: {},
      classList: { classes: new Set(), toggle(c, val) { if (val === undefined ? !this.classes.has(c) : val) this.classes.add(c); else this.classes.delete(c); }, add(c) { this.classes.add(c); }, remove(c) { this.classes.delete(c); }, contains(c) { return this.classes.has(c); } },
      textContent: '', innerHTML: '', offsetLeft: 20, offsetTop: 12,
      offsetWidth: 320, offsetHeight: 300, clientWidth: 900, clientHeight: 500,
      listeners: {}, getContext: () => context,
      addEventListener(name, handler) { (this.listeners[name] ||= []).push(handler); },
      emit(name, event = {}) { for (const handler of this.listeners[name] || []) handler(event); },
      setAttribute(name, value) { this.attributes[name] = value; },
      setPointerCapture() { }, releasePointerCapture() { }, focus() { },
      getBoundingClientRect() { return { left: 0, top: 0, width: 900, height: 500 }; },
    });
    return elements.get(selector);
  }
  const host = element('host');
  host.querySelector = element;
  host.querySelectorAll = () => [];
  host.isConnected = true;
  element('[name=transport_mode]').value = 'signed';
  const originals = {};
  for (const name of ['window', 'ResizeObserver', 'escapeHtml', 'researchSelect', 'PlotPreferences', 'localStorage']) originals[name] = global[name];
  global.window = { devicePixelRatio: 1 };
  global.ResizeObserver = class { observe() { } disconnect() { } };
  global.escapeHtml = text => String(text);
  global.researchSelect = (label, name) => `<label>${label}<select name="${name}"></select></label>`;
  try {
    renderTransportPlot(fixture(), host);
    assert.match(host.innerHTML, />Plot series<\/span>/);
    assert.match(element('#transport-series-toggle').textContent, /3 of 3 selected/);
    assert.ok(calls.some(call => call[0] === 'fillText' && call[1] === 'Time (s)'));

    const plot = element('#transport-chart');
    const ranges = () => element('#transport-ranges').textContent;
    const fullRange = ranges();
    const xBounds = () => ranges().match(/X: Time \(s\), ([\d.e+-]+) to ([\d.e+-]+)/).slice(1).map(Number);
    const span = () => { const [low, high] = xBounds(); return high - low; };
    plot.emit('wheel', { clientX: 477, clientY: 231, deltaY: -100, deltaMode: 0, preventDefault() { } });
    assert.ok(span() < 2);
    assert.ok(Math.abs(xBounds()[0] + xBounds()[1] - 2) < .0001);
    assert.equal(element('#transport-reset-view').disabled, false);
    element('#transport-reset-view').emit('click');
    assert.equal(ranges(), fullRange);
    const pointer = (x, y, shiftKey = false) => ({ button: 0, pointerId: 8, clientX: x, clientY: y, shiftKey, preventDefault() { } });
    assert.equal(element('[name=transport_drag]').value, 'pan');
    plot.emit('pointerdown', pointer(300, 200));
    plot.emit('pointermove', pointer(340, 220));
    plot.emit('pointerup', pointer(340, 220));
    assert.ok(xBounds()[0] < 0);
    assert.ok(Math.abs(span() - 2) < .0001);
    plot.emit('dblclick');
    element('[name=transport_drag]').value = 'zoom';
    plot.emit('pointerdown', pointer(200, 100));
    plot.emit('pointermove', pointer(600, 300));
    plot.emit('pointerup', pointer(600, 300));
    const firstSpan = span();
    assert.ok(firstSpan < 2);
    plot.emit('pointerdown', pointer(200, 100));
    plot.emit('pointermove', pointer(600, 300));
    plot.emit('pointerup', pointer(600, 300));
    assert.ok(span() < firstSpan);
    const beforePan = xBounds(), beforePanSpan = span();
    plot.emit('pointerdown', pointer(300, 200, true));
    plot.emit('pointermove', pointer(340, 220, true));
    plot.emit('pointerup', pointer(340, 220, true));
    assert.ok(xBounds()[0] < beforePan[0]);
    assert.ok(Math.abs(span() - beforePanSpan) < .0001);

    // Holding mouse wheel (button 1) works as pan without Shift key
    const middlePointer = (x, y) => ({ button: 1, pointerId: 9, clientX: x, clientY: y, shiftKey: false, preventDefault() { } });
    const beforeWheelPan = xBounds();
    plot.emit('pointerdown', middlePointer(300, 200));
    plot.emit('pointermove', middlePointer(340, 220));
    plot.emit('pointerup', middlePointer(340, 220));
    assert.ok(xBounds()[0] < beforeWheelPan[0]);

    const beforeCancel = ranges();
    plot.emit('pointerdown', pointer(300, 200, true));
    plot.emit('pointermove', pointer(350, 220, true));
    plot.emit('pointercancel', { pointerId: 8 });
    assert.equal(ranges(), beforeCancel);
    plot.emit('dblclick');
    assert.equal(ranges(), fullRange);
    plot.emit('pointerdown', pointer(200, 100));
    plot.emit('pointerup', pointer(201, 101));
    assert.equal(ranges(), fullRange);
    element('#transport-zoom-in').emit('click');
    assert.ok(span() < 2);
    plot.emit('keydown', { key: 'Home', preventDefault() { }, stopPropagation() { } });
    assert.equal(ranges(), fullRange);
    element('#transport-zoom-in').emit('click');
    element('#transport-zoom-out').emit('click');
    assert.ok(Math.abs(span() - 2) < .0001);
    element('[name=transport_mode]').value = 'log';
    host.emit('change', { target: { name: 'transport_mode', dataset: {} } });
    assert.equal(element('#transport-reset-view').disabled, true);
    assert.match(ranges(), /log scale/);
    element('[name=transport_mode]').value = 'signed';
    host.emit('change', { target: { name: 'transport_mode', dataset: {} } });
    assert.equal(ranges(), fullRange);

    calls.length = 0;
    host.emit('input', { target: { dataset: { traceColor: '0' }, value: '#ff0000' } });
    host.emit('change', { target: { dataset: { traceStyle: '0' }, value: 'dash-dot' } });
    assert.ok(calls.some(call => call[0] === 'strokeStyle' && call[1] === '#ff0000'));
    assert.ok(calls.some(call => call[0] === 'setLineDash' && JSON.stringify(call[1]) === '[8,4,2,4]'));
    assert.match(element('#transport-legend').innerHTML, /stroke="#ff0000"/);
    assert.match(element('#transport-legend-summary').textContent, /Plotted lines \(3\)/);

    element('[name=transport_y]').value = 'RES';
    host.emit('change', { target: { name: 'transport_y', dataset: {} } });
    assert.match(element('#transport-ranges').textContent, /Y: RES \(Ω\)/);
    assert.equal(element('#transport-series-toggle').textContent, '1 of 1 selected');
    calls.length = 0;
    host.emit('change', { target: { dataset: { traceVisible: '0' }, checked: false } });
    assert.match(element('#transport-ranges').textContent, /No selected numeric data/);
    assert.ok(calls.some(call => call[0] === 'fillRect'));
    assert.equal(element('#transport-legend').innerHTML, '');
    assert.match(element('#transport-legend-summary').textContent, /Plotted lines \(0\)/);

    element('#transport-hide-inset').emit('click');
    assert.equal(element('.transport-inset').hidden, true);
    host.emit('change', { target: { name: 'transport_inset', checked: true, dataset: {} } });
    assert.equal(element('.transport-inset').hidden, false);
    const handle = element('.transport-inset-handle');
    handle.emit('keydown', { target: handle, key: 'ArrowRight', preventDefault() { } });
    assert.equal(element('.transport-inset').style.left, '30px');
    handle.emit('pointerdown', { target: { closest: () => null }, button: 0, pointerId: 1, clientX: 0, clientY: 0, preventDefault() { } });
    handle.emit('pointermove', { pointerId: 1, clientX: 5000, clientY: 5000 });
    assert.equal(element('.transport-inset').style.left, '580px');
    assert.equal(element('.transport-inset').style.top, '200px');

    const first = { ...fixture(), file_id: 'first', original_filename: 'first.xls' };
    const second = { ...fixture(), file_id: 'second', original_filename: 'second.xls' };
    element('[name=transport_x]').value = 'auto';
    element('[name=transport_y]').value = 'auto';
    calls.length = 0;
    renderTransportPlot([first, second], host);
    assert.equal(calls.filter(call => call[0] === 'strokeRect').length, 1);
    assert.match(element('#transport-ranges').textContent, /2 file\(s\), 6 traces/);
    assert.match(element('#transport-legend').innerHTML, /first.xls/);
    assert.match(element('#transport-legend').innerHTML, /second.xls/);
    const colors = new Set(calls.filter(call => call[0] === 'strokeStyle').map(call => call[1]));
    assert.ok(colors.has('#226e61') && colors.has('#d47534'));

    const previousState = { ...host.transportPlotState, transport_x: 'Time' };
    const firstTrace = buildTransportTraces([first, second], 'Time', 'auto')[0];
    previousState.settings.get(firstTrace.id).color = '#ff0000';
    calls.length = 0;
    renderTransportPlot([first], host, previousState);
    assert.equal(element('[name=transport_x]').value, 'Time');
    assert.match(element('#transport-ranges').textContent, /1 file\(s\), 3 traces/);
    assert.ok(calls.some(call => call[0] === 'strokeStyle' && call[1] === '#ff0000'));

    second.sheets[0].measurement_type = 'voltage_sweep';
    second.sheets[0].branches[0].x_column = 'AV';
    element('[name=transport_x]').value = 'auto';
    calls.length = 0;
    renderTransportPlot([first, second], host);
    assert.equal(calls.filter(call => call[0] === 'strokeRect').length, 2);
    assert.match(element('#transport-ranges').textContent, /Different axis units/);

    // Recreate the renderer without its in-memory state, as on a page refresh.
    const stored = new Map();
    global.localStorage = { getItem: key => stored.get(key) ?? null, setItem: (key, value) => stored.set(key, value) };
    global.PlotPreferences = require('../static/plot_preferences.js');
    const persistedTrace = buildTransportTraces([first], 'Time', 'AI')[0];
    renderTransportPlot([first], host, {
      transport_x: 'Time', transport_y: 'AI', transport_mode: 'absolute', transport_drag: 'zoom',
      settings: new Map([[persistedTrace.id, { visible: true, color: '#ff0000', style: 'dotted' }]]),
      channelColors: new Map(), insetVisible: true, insetPosition: { left: 40, top: 50 },
    });
    element('#transport-zoom-in').emit('click');
    const savedRange = ranges();
    element('[name=transport_x]').value = 'auto';
    element('[name=transport_y]').value = 'auto';
    element('[name=transport_mode]').value = 'signed';
    calls.length = 0;
    renderTransportPlot([first], host);
    assert.equal(element('[name=transport_drag]').value, 'zoom');
    assert.equal(element('[name=transport_x]').value, 'Time');
    assert.equal(element('[name=transport_y]').value, 'AI');
    assert.equal(element('[name=transport_mode]').value, 'absolute');
    assert.equal(element('.transport-inset').hidden, false);
    assert.equal(element('.transport-inset').style.left, '40px');
    assert.equal(ranges(), savedRange);
    assert.ok(calls.some(call => call[0] === 'strokeStyle' && call[1] === '#ff0000'));
    assert.ok(calls.some(call => call[0] === 'setLineDash' && JSON.stringify(call[1]) === '[2,4]'));
  } finally {
    for (const [name, value] of Object.entries(originals)) {
      if (value === undefined) delete global[name]; else global[name] = value;
    }
  }
});

test('zoom keeps its cursor anchor and rejects invalid or overflowed ranges', () => {
  const { transportZoomBounds, transportValidBounds } = require('../static/transport_plot.js');
  const bounds = { xLow: 0, xHigh: 10, yLow: -10, yHigh: 10 };
  const zoom = transportZoomBounds(bounds, .2, .75, .5);
  assert.deepEqual(zoom, { xLow: 1, xHigh: 6, yLow: -7.5, yHigh: 2.5 });
  assert.deepEqual(transportZoomBounds(zoom, .2, .75, 2), bounds);
  assert.equal(transportValidBounds(zoom), true);
  assert.equal(transportValidBounds({ ...bounds, xHigh: 0 }), false);
  assert.equal(transportValidBounds({ ...bounds, yLow: NaN }), false);
  assert.equal(transportValidBounds({ ...bounds, xLow: -1e308, xHigh: 1e308 }), false);
});


test('log-axis labels show positive magnitudes rather than signed logarithms', () => {
  const {transportAxisTick} = require('../static/transport_plot.js');
  assert.equal(transportAxisTick(-6, 'log'), '1.00e-6');
  assert.equal(transportAxisTick(0, 'log'), '1.00e+0');
  assert.equal(transportAxisTick(3, 'log'), '1.00e+3');
  assert.equal(transportAxisTick(-1e-6, 'signed'), '-0.00000100');
});
