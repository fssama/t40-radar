'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

test('expired feeds clear their readings and track-only events cannot restore them', () => {
  function element() {
    return {
      children: [],
      style: { setProperty() {} },
      classList: { toggle() {} },
      getContext: () => ({ setTransform() {} }),
      addEventListener() {},
      setAttribute() {},
      append(...children) { this.children.push(...children); },
      replaceChildren() { this.children = []; },
    };
  }
  const elements = new Map();
  const get = (id) => {
    if (!elements.has(id)) elements.set(id, element());
    return elements.get(id);
  };
  let stream;
  let time = 0;
  const source = fs.readFileSync(path.join(__dirname, '../frontend/app.js'), 'utf8');
  const context = vm.createContext({
    document: { getElementById: get, createElement: element },
    performance: { now: () => time },
    devicePixelRatio: 1,
    ResizeObserver: class { observe() {} },
    EventSource: class {
      constructor(url) {
        assert.equal(url, '/events');
        stream = this;
      }
    },
    requestAnimationFrame() {},
    setInterval() {},
    console: { warn(_message, error) { throw error; } },
  });
  vm.runInContext(source, context);
  const send = (message) => stream.onmessage({ data: JSON.stringify(message) });
  const track = { id: 1, range: 5, az: 45, status: 'observed' };
  const frame = {
    type: 'frame', points: [[45, 5, -0.5, 1, 2]], tracks: [track],
    proto: { seq: 1, len: 114, count: 1, crc_ok: true, hex: '55' },
  };
  send({ type: 'radar_status', radar: { point_cloud: 'waiting', avoidance: 'waiting' } });
  assert.equal(get('badge-run').textContent, '等待雷达');
  assert.equal(get('near-dist').textContent, '—');
  assert.equal(get('h-val').textContent, '—');
  send(frame);
  send({ type: 'avoidance', avoidance: {
    height_confidence: 80, height_kalman_cm: 123,
    sectors: [{ index: 0, az: 45, dist_cm: 210, valid: true }],
  } });
  assert.equal(get('trk-count').textContent, 1);
  assert.equal(get('near-dist').textContent, '2.10');
  assert.equal(get('h-val').textContent, 123);

  send({ type: 'radar_status', radar: { point_cloud: 'stale', avoidance: 'live' } });
  send({ type: 'tracks', tracks: [track] });
  assert.equal(get('trk-count').textContent, '—');
  assert.equal(get('proto-hex').textContent, '');
  assert.equal(get('near-dist').textContent, '2.10');

  time = 7000;
  context.tick();
  assert.equal(get('near-dist').textContent, '—');
  assert.equal(get('h-val').textContent, '—');
  send(frame);
  assert.equal(get('trk-count').textContent, 1);
});
