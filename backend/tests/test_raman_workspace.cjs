const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync(require.resolve('../static/raman_workspace.js'), 'utf8');
function workspace(search) {
  const context = vm.createContext({location: {search}, URLSearchParams, module: {exports: {}}});
  vm.runInContext(source, context);
  return context.module.exports;
}
const files = [
  {file_id: 'transport', technique: 'Transport', original_filename: 'iv.csv'},
  {file_id: 'first', technique: 'Raman', original_filename: 'first.wdf'},
  {file_id: 'second', technique: 'Raman spectroscopy', original_filename: 'second.txt'},
  {file_id: 'photo', technique: 'Raman', original_filename: 'spot.png', data_category: 'image'},
  {file_id: 'paper', technique: 'Raman', original_filename: 'paper.txt', data_category: 'document'},
];
test('embedded Raman uses the parent desktop breakpoint and follows mobile resizing', () => {
  const context = vm.createContext({location: {search: '?workspace=raman'}, URLSearchParams, module: {exports: {}}});
  vm.runInContext(source, context);
  let onChange, onLeave, isWide;
  const media = {matches: true, addEventListener(name, callback) {onChange = callback;},
    removeEventListener(name, callback) {assert.equal(callback, onChange); onChange = null;}};
  context.window = {parent: {matchMedia(query) {assert.equal(query, '(min-width: 901px)'); return media;}},
    matchMedia() {throw Error('The narrower iframe must not select the mobile layout');},
    addEventListener(name, callback) {assert.equal(name, 'pagehide'); onLeave = callback;}};
  context.document = {body: {classList: {toggle(name, value) {assert.equal(name, 'raman-wide'); isWide = value;}}}};
  context.module.exports.bindLayout();
  assert.equal(isWide, true);
  media.matches = false;
  onChange();
  assert.equal(isWide, false);
  media.matches = true;
  onChange();
  assert.equal(isWide, true);
  onLeave();
  assert.equal(onChange, null);
});
test('Raman workspace isolates compatible spectra without changing upload behavior', () => {
  assert.deepEqual(workspace('?workspace=raman').files(files).map(f => f.file_id), ['first', 'second']);
  assert.equal(workspace('').files(files), files);
});
test('explicit Raman dataset wins over the previous upload selection with safe fallback', () => {
  const page = workspace('?workspace=raman&dataset=second');
  const spectra = page.files(files);
  assert.equal(page.initialFile(spectra, {fileId: 'first'}).file_id, 'second');
  assert.equal(workspace('?workspace=raman&dataset=missing').initialFile(spectra, {fileId: 'transport'}).file_id, 'first');
  assert.equal(page.initialFile([], null), undefined);
  assert.equal(workspace('').initialFile(files, {fileId: 'transport'}).file_id, 'transport');
});
test('workspace frame resizes only for its own same-origin studio', () => {
  const script = fs.readFileSync(require.resolve('../static/research.js'), 'utf8');
  const listeners = {};
  const frame = {contentWindow: {}, style: {}, classList: {toggle(name, value) {this[name] = value;}}};
  const context = vm.createContext({window: {addEventListener: (name, callback) => {listeners[name] = callback;}},
    document: {querySelector: () => frame}, location: {origin: 'http://localhost:8000'}});
  vm.runInContext(script, context);
  const event = {origin: 'http://localhost:8000', source: frame.contentWindow, data: {type: 'raman-studio-size', height: 1234, expanded: true}};
  listeners.message({...event, origin: 'https://external.example'});
  assert.equal(frame.style.height, undefined);
  listeners.message({...event, source: {}});
  assert.equal(frame.style.height, undefined);
  listeners.message(event);
  assert.equal(frame.style.height, '1234px');
  assert.equal(frame.classList['raman-studio-expanded'], true);
  listeners.message({...event, data: {...event.data, height: Infinity, expanded: false}});
  assert.equal(frame.style.height, '1234px');
  assert.equal(frame.classList['raman-studio-expanded'], false);
});

test('Raman studio has full width even before a cached outer stylesheet is refreshed', () => {
  const script = fs.readFileSync(require.resolve('../static/research.js'), 'utf8');
  const view = {innerHTML: ''};
  const context = vm.createContext({view, URLSearchParams, location: {search: '?dataset=sample-1'},
    window: {addEventListener() {}}, pageHeading: () => '', escapeHtml: value => value});
  vm.runInContext(script, context);
  vm.runInContext('renderRamanAnalysis()', context);
  assert.match(view.innerHTML, /width="100%"/);
  assert.match(view.innerHTML, /style="[^"]*width:100%/);
  assert.match(view.innerHTML, /layout=20260916-4/);
  assert.match(view.innerHTML, /dataset=sample-1/);
  const shell = fs.readFileSync(require.resolve('../static/workspace.html'), 'utf8');
  assert.match(shell, /workspace\.css\?v=20260916-raman-layout3/);
});
