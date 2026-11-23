'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

test('UDP status keeps unknown measurements blank and clears expired returns', async () => {
  const ctx = new Proxy({}, { get: () => () => {} });
  function makeElement() {
    return {
      children: [],
      dataset: {},
      classList: { add() {}, toggle() {} },
      style: {},
      parentElement: { clientWidth: 400, clientHeight: 400 },
      getContext: () => ctx,
      appendChild(child) { this.children.push(child); },
      replaceChildren(...children) { this.textContent = children[1]; },
    };
  }

  const elements = new Map();
  const get = (id) => {
    if (!elements.has(id)) elements.set(id, makeElement());
    return elements.get(id);
  };
  let bootstrap;
  let stream;
  const document = {
    getElementById: get,
    createElement: makeElement,
    addEventListener: (_type, listener) => { bootstrap = listener; },
  };
  class EventSource {
    constructor(url) {
      assert.equal(url, '/events');
      stream = this;
    }
  }

  const js = fs.readFileSync(path.join(__dirname, '../frontend/app.js'), 'utf8');
  vm.runInNewContext(js, {
    document, EventSource,
    window: { addEventListener() {} },
    performance: { now: () => 1 },
    setInterval() {},
    requestAnimationFrame() {},
    fetch: async (url) => {
      assert.equal(url, '/api/state');
      return { json: async () => ({
        source: 'udp', data_status: 'waiting', running: null, height_cm: null,
        rpm: null, motor_speed: null, crc_ok: null,
      }) };
    },
  });

  await bootstrap();
  stream.onopen();
  assert.equal(get('badge-conn').textContent, '已连接');
  assert.equal(get('badge-run').textContent, '等待雷达数据');
  assert.equal(get('h-val').textContent, '—');
  assert.equal(get('rpm-val').textContent, '—');

  const send = (payload) => stream.onmessage({
    data: JSON.stringify({ type: 'frame', source: 'udp', ...payload }),
  });
  send({
    data_status: 'live', data_epoch: 0, rpm: null,
    points: [[90, 3, -0.4, 0.8, 2]],
    tracks: [{ id: 1, range: 3.12, az: 90 }],
    proto: { msg: '0x1E61', seq: 1, len: 114, count: 1, crc_ok: true, hex: '55' },
  });
  assert.equal(get('badge-run').textContent, '雷达数据正常');
  assert.equal(get('badge-crc').textContent, 'CRC OK');
  assert.equal(get('near-dist').textContent, '3.00');
  assert.match(get('near-az').textContent, /相对雷达 -0\.40 m/);
  assert.match(get('trk-list').innerHTML, /3\.12 m/);

  send({ data_status: 'stale', data_epoch: 1, points: [], tracks: [], proto: null });
  assert.equal(get('badge-conn').textContent, '已连接');
  assert.equal(get('badge-run').textContent, '雷达数据超时');
  assert.equal(get('near-dist').textContent, '—');
  assert.equal(get('trk-count').textContent, 0);
  assert.equal(get('proto-hex').textContent, '');

  send({ data_status: 'live', data_epoch: 1, points: [[90, 4, 0, 0.8, 2]], tracks: [] });
  assert.equal(get('near-dist').textContent, '4.00');
  send({ data_status: 'live', data_epoch: 2, points: [], tracks: [] });
  assert.equal(get('badge-run').textContent, '雷达数据正常');
  assert.equal(get('near-dist').textContent, '—');
});
