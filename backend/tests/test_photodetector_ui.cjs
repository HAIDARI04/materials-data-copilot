const test = require('node:test');
const assert = require('node:assert/strict');
const {photoReadiness, photoRecipePayload, photoRequest} = require('../static/photodetector.js');

test('unknown optical inputs retain partial analysis without claiming optical metrics', () => {
  const ready = photoReadiness({power_mode: 'density', area_value: '', power_density: '', noise_mode: 'none'});
  assert.equal(ready.photocurrent, true);
  assert.equal(ready.on_off, true);
  for (const key of ['responsivity', 'detectivity', 'eqe', 'diode']) assert.equal(ready[key], false);
});

test('direct power does not need area for responsivity but detectivity still needs area and noise', () => {
  const inputs = {power_mode: 'direct', incident_power_w: '1e-7', noise_mode: 'measured'};
  assert.equal(photoReadiness(inputs).responsivity, true);
  assert.equal(photoReadiness(inputs).detectivity, false);
  inputs.area_value = '100'; inputs.noise_a_sqrt_hz = '1e-13';
  assert.equal(photoReadiness(inputs).detectivity, true);
  inputs.wavelength_nm = '532';
  assert.equal(photoReadiness(inputs).eqe, false);
  inputs.monochromatic = true;
  assert.equal(photoReadiness(inputs).eqe, true);
});

test('wizard payload preserves confirmations and units while leaving unknown values null', () => {
  const payload = photoRecipePayload({dark_file_id: '001', light_file_id: '002', area_value: '',
    area_unit: 'um2', incident_power_w: '1e-7', dark_trace: '0', same_device_confirmed: true,
    roles_units_confirmed: false, monochromatic: false, current_floor_a: '0', notes: '532 nm run'});
  assert.deepEqual(payload, {dark_file_id: '001', light_file_id: '002', area_value: null,
    area_unit: 'um2', incident_power_w: 1e-7, dark_trace: 0, same_device_confirmed: true,
    roles_units_confirmed: false, monochromatic: false, current_floor_a: 0, notes: '532 nm run'});
});

test('validation failures become readable field messages and request keeps exact submitted inputs', async () => {
  const original = global.fetch;
  try {
    global.fetch = async (path, options) => {
      assert.equal(path, '/recipe');
      assert.deepEqual(JSON.parse(options.body), {area_value: null});
      return {ok: false, json: async () => ({detail: [{loc: ['body', 'temperature_k'], msg: 'Must be positive'}]})};
    };
    await assert.rejects(photoRequest('/recipe', {area_value: null}), /temperature_k: Must be positive/);
  } finally { global.fetch = original; }
});
