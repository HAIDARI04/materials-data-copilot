const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const preferences = require('../static/plot_preferences.js');

test('preferences survive serialization, isolate datasets, and tolerate broken or blocked storage', () => {
  const original = global.localStorage;
  const stored = new Map();
  global.localStorage = {getItem: key => stored.get(key), setItem: (key, value) => stored.set(key, value)};
  try {
    assert.equal(preferences.read('new'), null);
    assert.equal(preferences.write('first', {insetVisible: false, fileIds: ['a', 'b']}), true);
    assert.deepEqual(preferences.read('first'), {insetVisible: false, fileIds: ['a', 'b']});
    assert.equal(preferences.read('second'), null);
    stored.set('materialsCopilot.plot.v1.first', '{broken');
    assert.equal(preferences.read('first'), null);
    global.localStorage = {getItem() {throw Error('Denied');}, setItem() {throw Error('Quota');}};
    assert.equal(preferences.read('first'), null);
    assert.equal(preferences.write('first', {}), false);
  } finally { if (original === undefined) delete global.localStorage; else global.localStorage = original; }
});

test('Raman restores per-dataset view preferences and does not overwrite them while loading', () => {
  const html = fs.readFileSync(require.resolve('../static/upload.html'), 'utf8');
  const start = html.indexOf('    let restoringRamanPlot = false;');
  const end = html.indexOf('    let pendingFitAssignment', start);
  assert.ok(start > 0 && end > start);
  const stored = new Map();
  const context = vm.createContext({
    PlotPreferences: {read: key => structuredClone(stored.get(key)), write: (key, value) => stored.set(key, structuredClone(value)), position: preferences.position},
    activePreview: {file_id: 'raman-a', dataset_index: 2},
    plotDragMode: {value: 'pan'}, plotShell: {dataset: {}},
    seriesSelect: {value: 'normalized'}, plotViewport: {xMin: 100, xMax: 200, yMin: 0, yMax: 1},
    hiddenFitComponents: new Set(['peak-1']), hiddenFitLines: new Set(['total']),
    fitInsetToggle: {checked: true}, fitInsetPanel: {hidden: false, style: {left: '25px', top: '35px'}},
    plotPanel: {classList: {contains: () => true}}, togglePlotExpansion() {},
  });
  vm.runInContext(html.slice(start, end), context);
  vm.runInContext('saveRamanPlotPreferences()', context);
  const saved = structuredClone(stored.get('raman:raman-a:2'));
  vm.runInContext("restoringRamanPlot = true; seriesSelect.value = 'raw'; plotViewport = null; saveRamanPlotPreferences()", context);
  assert.deepEqual(stored.get('raman:raman-a:2'), saved);
  context.hiddenFitLines.clear(); context.hiddenFitComponents.clear(); context.fitInsetToggle.checked = false;
  vm.runInContext('restoreRamanPlotPreferences()', context);
  assert.equal(vm.runInContext('pendingRamanSeries', context), 'normalized');
  assert.deepEqual(context.plotViewport, saved.viewport);
  assert.equal(context.fitInsetToggle.checked, true);
  assert.equal(context.fitInsetPanel.style.left, '25px');
  assert.deepEqual([...context.hiddenFitLines], ['total']);
  assert.deepEqual([...context.hiddenFitComponents], ['peak-1']);
  context.activePreview.dataset_index = 3;
  vm.runInContext('restoreRamanPlotPreferences()', context);
  assert.equal(context.plotViewport, null);
  assert.equal(context.fitInsetToggle.checked, false);
});


test('Raman left drag pans at full view without Shift, and Zoom area stays available', () => {
  const html = fs.readFileSync(require.resolve('../static/upload.html'), 'utf8');
  const context = vm.createContext({
    activePlotState: {padding: {left: 0, top: 0}, plotWidth: 100, plotHeight: 100, xMin: 0, xMax: 10, yMin: 0, yMax: 10},
    activePreview: {}, plotViewport: null, plotDrag: null, plotDragMode: {value: 'pan'},
    plotCanvas: {getBoundingClientRect: () => ({left: 0, top: 0}), setPointerCapture() {}},
    plotShell: {classList: {add() {}}}, plotCursor: {}, plotSelection: {style: {}},
    drawPlot() {}, inspectPlot() {},
  });
  vm.runInContext(html.slice(html.indexOf('    function beginPlotSelection('), html.indexOf('    function finishPlotSelection(')), context);
  context.event = {button: 0, pointerId: 1, clientX: 50, clientY: 50, shiftKey: false, preventDefault() {}};
  vm.runInContext('beginPlotSelection(event)', context);
  assert.equal(context.plotDrag.mode, 'pan');
  context.event.clientX = 60; context.event.clientY = 70;
  vm.runInContext('movePlotPointer(event)', context);
  assert.deepEqual(JSON.parse(JSON.stringify(context.plotViewport)), {xMin: -1, xMax: 9, yMin: 2, yMax: 12});
  context.plotDragMode.value = 'zoom';
  vm.runInContext('beginPlotSelection(event)', context);
  assert.equal(context.plotDrag.mode, 'zoom');
});
