'use strict';

const byId = (id) => document.getElementById(id);
const now = () => performance.now();
const FEEDS = { point_cloud: '1e61', avoidance: '1e62' };
const WATCHDOG_MS = 6500;
const POINT_LIFE_MS = 4000;
const MAX_POINTS = 16000;
const RANGES = [5, 10, 20, 50];
const RINGS = { 5: 5, 10: 5, 20: 4, 50: 5 };
const LAYERS = [
  { label: '地面', color: '#80a1bf', radius: 2.1, visible: true },
  { label: '线缆', color: '#efba6c', radius: 2.8, visible: true },
  { label: '障碍', color: '#60e2ac', radius: 3.3, visible: true },
];
const OMNI_LAYER = { label: '原始回波', color: '#c9a4ff', visible: true };

const state = {
  points: [],
  omniPoints: [],
  omniAt: null,
  frameTimes: [],
  pointPacket: null,
  feeds: { point_cloud: 'waiting', avoidance: 'waiting' },
  deadlines: { point_cloud: 0, avoidance: 0 },
  range: 10,
  manualRange: null,
};

const canvas = byId('ppi');
const ctx = canvas.getContext('2d');
const profile = byId('profile');
const profileCtx = profile.getContext('2d');
let width = 0, height = 0, radius = 0, cx = 0, cy = 0;
let profileWidth = 0, profileHeight = 0;
let pendingDraw = false;
const sectors = [];
const rangeButtons = new Map();

function badge(id, text, tone = '') {
  const el = byId(id);
  el.textContent = text;
  el.className = 'badge ' + tone;
}

function resizePlot() {
  width = canvas.clientWidth;
  height = canvas.clientHeight;
  const ratio = Math.min(devicePixelRatio || 1, 2);
  canvas.width = Math.round(width * ratio);
  canvas.height = Math.round(height * ratio);
  ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
  profileWidth = profile.clientWidth;
  profileHeight = profile.clientHeight;
  profile.width = Math.round(profileWidth * ratio);
  profile.height = Math.round(profileHeight * ratio);
  profileCtx.setTransform(ratio, 0, 0, ratio, 0, 0);
  cx = width / 2;
  cy = height / 2;
  radius = Math.max(0, Math.min(width, height) / 2 - 32);
  schedulePlot();
}

function polar(angle, r) {
  const radians = angle * Math.PI / 180;
  return [cx + Math.sin(radians) * r, cy - Math.cos(radians) * r];
}

function drawGrid(range) {
  ctx.clearRect(0, 0, width, height);
  if (!radius) return;
  ctx.lineWidth = 1;
  ctx.beginPath();
  ctx.arc(cx, cy, radius, 0, 2 * Math.PI);
  const shade = ctx.createRadialGradient(cx, cy, 0, cx, cy, radius);
  shade.addColorStop(0, '#142a3b');
  shade.addColorStop(1, '#0e1b2b');
  ctx.fillStyle = shade;
  ctx.fill();
  ctx.strokeStyle = '#36536a';
  ctx.stroke();

  ctx.strokeStyle = '#263d53';
  for (let angle = 0; angle < 360; angle += 45) {
    const [x, y] = polar(angle, radius);
    ctx.beginPath();
    ctx.moveTo(cx, cy);
    ctx.lineTo(x, y);
    ctx.stroke();
  }

  ctx.font = '11px ui-monospace, Consolas, monospace';
  ctx.textBaseline = 'middle';
  ctx.textAlign = 'center';
  ctx.fillStyle = '#92a9c1';
  const rings = RINGS[range];
  for (let i = 1; i < rings; i++) {
    const r = radius * i / rings;
    ctx.beginPath();
    ctx.arc(cx, cy, r, 0, 2 * Math.PI);
    ctx.stroke();
    ctx.fillText((range * i / rings) + ' m', cx, cy - r + 9);
  }

  for (let angle = 0; angle < 360; angle += 45) {
    const [x, y] = polar(angle, radius + 17);
    ctx.fillStyle = angle === 0 ? '#68e7d4' : '#92a9c1';
    ctx.fillText(angle + '°', x, y);
  }
  ctx.beginPath();
  ctx.arc(cx, cy, 3, 0, 2 * Math.PI);
  ctx.fillStyle = '#68e7d4';
  ctx.fill();
}

function drawPoints(points, range) {
  let visible = 0;
  ctx.save();
  ctx.beginPath();
  ctx.arc(cx, cy, radius, 0, 2 * Math.PI);
  ctx.clip();

  const omni = state.omniPoints.filter(point => point[1] <= range);
  OMNI_LAYER.count.textContent = state.omniAt === null ? '—' : omni.length;
  if (OMNI_LAYER.visible && radius && omni.length) {
    ctx.beginPath();
    for (const [az, distance] of omni) {
      const [x, y] = polar(az, distance * radius / range);
      ctx.moveTo(x + 2.5, y);
      ctx.arc(x, y, 2.5, 0, 2 * Math.PI);
    }
    ctx.globalAlpha = .75;
    ctx.strokeStyle = OMNI_LAYER.color;
    ctx.lineWidth = 1.1;
    ctx.stroke();
    visible += omni.length;
  }

  LAYERS.forEach((layer, subtype) => {
    let count = 0;
    ctx.beginPath();
    for (const point of points) {
      if (point[4] !== subtype || point[1] > range) continue;
      count++;
      if (!radius || !layer.visible) continue;
      const [x, y] = polar(point[0], point[1] * radius / range);
      ctx.moveTo(x + layer.radius, y);
      ctx.arc(x, y, layer.radius, 0, 2 * Math.PI);
    }
    layer.count.textContent = count;
    if (!layer.visible) return;
    visible += count;
    ctx.globalAlpha = subtype === 0 ? .74 : .96;
    ctx.fillStyle = layer.color;
    ctx.fill();
  });
  ctx.restore();
  return visible;
}

function drawProfile(range) {
  const view = state.points.filter(point => point[1] <= range && LAYERS[point[4]].visible);
  const span = Math.max(1, Math.ceil(view.reduce((peak, point) => Math.max(peak, Math.abs(point[2])), 0)));
  const left = 32;
  const right = profileWidth - 6;
  const top = 7;
  const bottom = profileHeight - 15;
  const middle = (top + bottom) / 2;
  const chart = profileCtx;
  chart.clearRect(0, 0, profileWidth, profileHeight);
  if (profileWidth <= 50) return;
  chart.strokeStyle = '#2c4359';
  chart.beginPath();
  chart.moveTo(left, top);
  chart.lineTo(left, bottom);
  chart.moveTo(left, middle);
  chart.lineTo(right, middle);
  chart.stroke();
  chart.font = '10px ui-monospace, Consolas, monospace';
  chart.fillStyle = '#92a9c1';
  chart.textAlign = 'right';
  chart.fillText('+' + span + 'm', left - 4, top + 4);
  chart.fillText('−' + span + 'm', left - 4, bottom);
  chart.fillText(range + ' m', right, profileHeight - 1);
  chart.textAlign = 'left';
  chart.fillText('0', left, profileHeight - 1);
  for (const point of view) {
    const x = left + point[1] / range * (right - left);
    const y = middle - point[2] / span * (middle - top);
    chart.fillStyle = LAYERS[point[4]].color;
    chart.fillRect(x - 1, y - 1, 2, 2);
  }
}

function schedulePlot() {
  if (pendingDraw) return;
  pendingDraw = true;
  requestAnimationFrame(() => {
    pendingDraw = false;
    refreshPlot();
  });
}

function refreshPlot() {
  const t = now();
  state.points = state.points.filter(point => t - point[5] < POINT_LIFE_MS);
  state.omniPoints = state.omniPoints.filter(point => t - point[2] < POINT_LIFE_MS);
  if (state.omniAt !== null && t - state.omniAt >= POINT_LIFE_MS) {
    state.omniAt = null;
    updateFeedBadges();
  }
  state.frameTimes = state.frameTimes.filter(stamp => t - stamp < 1000);
  let peak = state.points.reduce((max, point) => Math.max(max, point[1]), 0);
  if (OMNI_LAYER.visible) peak = state.omniPoints.reduce((max, point) => Math.max(max, point[1]), peak);
  state.range = state.manualRange ?? (peak ? (RANGES.find(range => range >= peak * 1.04) ?? 50) : 10);
  byId('range-foot').textContent = '量程 ' + state.range + ' m';
  byId('stat-fps').textContent = state.pointPacket ? state.frameTimes.length + ' 帧/s' : '—';

  drawGrid(state.range);
  const visible = drawPoints(state.points, state.range);
  drawProfile(state.range);
  const feed = state.feeds.point_cloud;
  byId('ppi-hint').hidden = visible > 0;
  if (!visible && OMNI_LAYER.visible && state.omniAt !== null) {
    byId('ppi-message').textContent = '当前视图没有可见回波';
    byId('ppi-detail').textContent = '可切换图层或量程';
  } else if (feed === 'stale') {
    byId('ppi-message').textContent = '1E61 点云数据已过期';
    byId('ppi-detail').textContent = '重新收到有效帧后自动恢复';
  } else if (!state.pointPacket) {
    byId('ppi-message').textContent = '等待 1E61 点云';
    byId('ppi-detail').textContent = '收到有效数据后自动绘制';
  } else if (!visible) {
    byId('ppi-message').textContent = '当前视图没有可见点云';
    byId('ppi-detail').textContent = peak ? '可切换图层或量程' : '最近收到的点云帧没有可绘制的回波';
  }
}

function showProtocol(feed, packet) {
  const ids = feed === 'point_cloud' ? ['proto-kv', 'proto-hex'] : ['avoid-proto-kv', 'avoid-proto-hex'];
  const name = FEEDS[feed].toUpperCase();
  let text = name + ' · ' + (state.feeds[feed] === 'stale' ? '数据已过期' : '等待数据');
  if (packet) {
    text = name + ' · seq ' + packet.seq + ' · ' + packet.len + ' B · CRC OK';
    if (packet.count != null) text += ' · ' + packet.count + ' 点';
  }
  byId(ids[0]).textContent = text;
  byId(ids[1]).textContent = packet?.hex || '';
}

function showTracks(tracks) {
  const list = byId('trk-list');
  const awaiting = !state.pointPacket;
  byId('trk-count').textContent = awaiting ? '—' : tracks.length;
  list.replaceChildren();
  if (!tracks.length) {
    const entry = document.createElement('li');
    entry.className = 'empty';
    entry.textContent = awaiting ? '等待点云' : '暂无确认航迹';
    list.append(entry);
    return;
  }
  for (const track of tracks.slice(0, 6)) {
    const entry = document.createElement('li');
    const label = document.createElement('span');
    label.textContent = '#' + track.id;
    if (track.status === 'coasting') {
      const detail = document.createElement('small');
      detail.className = 'track-coasting';
      detail.textContent = '预测中';
      label.append(detail);
    }
    entry.append(label);
    for (const value of [track.range.toFixed(2) + ' m', track.az.toFixed(1) + '°']) {
      const cell = document.createElement('span');
      cell.textContent = value;
      entry.append(cell);
    }
    list.append(entry);
  }
}

function showAvoidance(data) {
  const stale = state.feeds.avoidance === 'stale';
  const active = data ? data.sectors.filter(sector => sector.valid) : [];
  const nearest = active.reduce((best, sector) => best === null || sector.dist_cm < best.dist_cm ? sector : best, null);
  byId('near-dist').textContent = nearest ? (nearest.dist_cm / 100).toFixed(2) : '—';
  byId('near-az').textContent = nearest ? '方位 ' + nearest.az.toFixed(1) + '°'
    : data ? '未报告有效距离' : stale ? '1E62 避障数据已过期' : '等待 1E62 避障结果';
  byId('avoidance-card').classList.toggle('has-hit', nearest !== null);
  byId('sector-count').textContent = (data ? active.length : '—') + ' / 16';

  sectors.forEach((cell, index) => {
    const sector = data?.sectors[index];
    cell.classList.toggle('hit', Boolean(sector?.valid));
    cell.classList.toggle('nearest', nearest !== null && nearest.index === index);
    cell.title = '扇区 ' + (index + 1) + ' · ' + (sector?.valid
      ? (sector.dist_cm / 100).toFixed(2) + ' m · ' + sector.az.toFixed(1) + '°'
      : data ? '暂无有效距离' : '暂无数据');
  });

  const height = data?.height_kalman_cm;
  const validHeight = data?.height_confidence > 0 && height > 0 && height < 10000;
  byId('h-val').textContent = validHeight ? height : '—';
  byId('h-sub').textContent = data ? '高度置信度 ' + data.height_confidence
    : stale ? '数据已过期' : '等待 1E62';
  showProtocol('avoidance', data?.proto);
}

function updateFeedBadges() {
  for (const [feed, id] of Object.entries(FEEDS)) {
    const value = state.feeds[feed];
    const el = byId('feed-' + id);
    el.className = 'feed-state ' + value;
    el.textContent = { live: '收包正常', stale: '数据过期', waiting: '等待数据' }[value];
  }
  const feeds = Object.values(state.feeds);
  if (feeds.every(value => value === 'live')) badge('badge-run', '雷达收包正常', 'on');
  else if (feeds.includes('live')) badge('badge-run', '收到部分雷达数据', 'warn');
  else if (state.omniAt !== null) badge('badge-run', '收到原始回波', 'on');
  else if (feeds.includes('stale')) badge('badge-run', '雷达数据中断', 'warn');
  else badge('badge-run', '等待雷达');
}

function setFeed(feed, status) {
  state.feeds[feed] = status;
  if (status === 'live') {
    state.deadlines[feed] ||= now() + WATCHDOG_MS;
    return;
  }
  state.deadlines[feed] = 0;
  if (feed === 'point_cloud') {
    state.points = [];
    state.frameTimes = [];
    state.pointPacket = null;
    showTracks([]);
    showProtocol(feed);
    schedulePlot();
  } else {
    showAvoidance(null);
  }
}

function notePacket(feed, age = 0) {
  state.deadlines[feed] = now() + WATCHDOG_MS - age * 1000;
  if (state.feeds[feed] !== 'live') {
    state.feeds[feed] = 'live';
    updateFeedBadges();
  }
}

function statusUpdate(feeds, avoidance) {
  for (const feed of Object.keys(FEEDS)) setFeed(feed, feeds[feed]);
  if (avoidance && feeds.avoidance === 'live') {
    notePacket('avoidance', avoidance.age_s || 0);
    showAvoidance(avoidance);
  }
  updateFeedBadges();
}

function onPacket(message) {
  switch (message.type) {
    case 'radar_status':
      statusUpdate(message.radar, message.avoidance);
      break;
    case 'avoidance':
      notePacket('avoidance');
      showAvoidance(message.avoidance);
      break;
    case 'tracks':
      if (state.feeds.point_cloud === 'live' && state.pointPacket) showTracks(message.tracks);
      break;
    case 'omni': {
      const t = now();
      const becameLive = state.omniAt === null;
      state.omniAt = t;
      for (const [az, distance] of message.points) state.omniPoints.push([az, distance, t]);
      if (state.omniPoints.length > MAX_POINTS) state.omniPoints.splice(0, state.omniPoints.length - MAX_POINTS);
      OMNI_LAYER.button.hidden = false;
      if (becameLive) updateFeedBadges();
      schedulePlot();
      break;
    }
    case 'frame': {
      notePacket('point_cloud');
      const t = now();
      state.frameTimes.push(t);
      for (const point of message.points) {
        if (LAYERS[point[4]]) state.points.push([...point, t]);
      }
      if (state.points.length > MAX_POINTS) state.points.splice(0, state.points.length - MAX_POINTS);
      state.pointPacket = message.proto;
      showTracks(message.tracks);
      showProtocol('point_cloud', message.proto);
      schedulePlot();
      break;
    }
  }
}

function buildPlotControls() {
  for (const range of [null, ...RANGES]) {
    const button = document.createElement('button');
    button.type = 'button';
    button.textContent = range ? range + ' m' : '自动';
    button.setAttribute('aria-pressed', String(range === null));
    button.addEventListener('click', () => {
      state.manualRange = range;
      for (const [key, item] of rangeButtons) item.setAttribute('aria-pressed', String(key === range));
      schedulePlot();
    });
    rangeButtons.set(range, button);
    byId('ranges').append(button);
  }
  for (const layer of [...LAYERS, OMNI_LAYER]) {
    const button = document.createElement('button');
    const dot = document.createElement('i');
    const count = document.createElement('b');
    button.type = 'button';
    button.setAttribute('aria-pressed', 'true');
    dot.className = 'dot';
    dot.style.setProperty('--dot', layer.color);
    layer.count = count;
    count.textContent = '0';
    if (layer === OMNI_LAYER) {
      layer.button = button;
      button.id = 'layer-omni';
      button.title = '侧阵原始 CFAR 回波（斜距）';
      button.hidden = true;
    }
    button.append(dot, layer.label, count);
    button.addEventListener('click', () => {
      layer.visible = !layer.visible;
      button.setAttribute('aria-pressed', String(layer.visible));
      schedulePlot();
    });
    byId('legend').append(button);
  }
  for (let i = 0; i < 16; i++) {
    const cell = document.createElement('span');
    sectors.push(cell);
    byId('sector-strip').append(cell);
  }
}

let sendingMotor = false;
async function refreshSetpoint() {
  if (sendingMotor) return;
  try {
    const response = await fetch('/api/state');
    const { motor_speed } = await response.json();
    if (!sendingMotor) byId('rpm-val').textContent = motor_speed ?? '—';
  } catch (_) {
    // The SSE badge reports a dropped web connection.
  }
}

byId('motor-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  if (sendingMotor || !form.reportValidity()) return;
  const speed = byId('rpm').valueAsNumber;
  const button = byId('motor-submit');
  const output = byId('rpm-sub');
  sendingMotor = true;
  button.disabled = true;
  output.textContent = '正在下发…';
  try {
    const response = await fetch('/api/control', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ action: 'motor', value: speed }),
    });
    const result = await response.json();
    if (!response.ok || !result.ok) throw new Error(result.error || '电机桥未确认指令');
    byId('rpm-val').textContent = result.speed;
    output.textContent = '已下发 ' + result.speed + ' rpm';
  } catch (error) {
    output.textContent = '下发失败：' + error.message;
  } finally {
    sendingMotor = false;
    button.disabled = false;
  }
});

buildPlotControls();
showAvoidance(null);
new ResizeObserver(resizePlot).observe(canvas.parentElement);
resizePlot();
const stream = new EventSource('/events');
stream.onopen = () => {
  badge('badge-conn', '网页已连接', 'on');
  refreshSetpoint();
};
stream.onerror = () => {
  badge('badge-conn', '网页重连中', 'warn');
  state.omniAt = null;
  state.omniPoints = [];
  statusUpdate({ point_cloud: 'waiting', avoidance: 'waiting' });
};
stream.onmessage = (event) => {
  try {
    onPacket(JSON.parse(event.data));
  } catch (error) {
    console.warn('无法显示雷达数据', error);
  }
};

function tick() {
  byId('clock').textContent = new Date().toLocaleTimeString('zh-CN', { hour12: false });
  for (const feed of Object.keys(FEEDS)) {
    if (state.feeds[feed] === 'live' && now() >= state.deadlines[feed]) {
      setFeed(feed, 'stale');
      updateFeedBadges();
    }
  }
}
tick();
setInterval(tick, 1000);
setInterval(() => { if (state.pointPacket || state.omniAt !== null) schedulePlot(); }, 300);
setInterval(refreshSetpoint, 5000);
