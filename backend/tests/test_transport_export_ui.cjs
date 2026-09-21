const test = require('node:test');
const assert = require('node:assert/strict');
const {transportExportControls} = require('../static/transport_exports.js');

test('saved analyses offer a preview, full package, PDF and figures', () => {
  const markup = transportExportControls({processing_id: 'saved-run', implementation: {sha256: 'abc'}});
  assert.match(markup, /Download complete analysis/);
  assert.match(markup, /Preview report/);
  assert.match(markup, /format=report/);
  assert.match(markup, /format=figures/);
  assert.match(markup, /Individual figures and tables/);
  assert.match(markup, /role="status" aria-live="polite"/);
  assert.match(markup, /\/research\/analyses\/saved-run\/export/);
});

test('older saved analyses explain the required new run', () => {
  const markup = transportExportControls({processing_id: 'old-run'});
  assert.match(markup, /Analyze and save again/);
  assert.doesNotMatch(markup, /href=/);
});

test('run identifiers cannot inject HTML into export controls', () => {
  const markup = transportExportControls({processing_id: '"><script>alert(1)</script>', implementation: {}});
  assert.doesNotMatch(markup, /<script>/);
  assert.match(markup, /%3Cscript%3E/);
});
