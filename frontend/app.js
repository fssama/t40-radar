'use strict';
/* RD2484R 雷达监视前端 v5
 * 数据源：SSE /events（backend 推送的 1E61 帧）
 * 点结构：[az°, r_m, z_m, snr_0_1, subtype]
 *   subtype 0 = ground（地面点，侧阵下视波束，铺满远处）
 *   subtype 1 = wire  （线缆，常是雷达自身高度附近）
 *   subtype 2 = pcla  （障碍物，真正需要看的）
 * 控制：POST /api/control  {action, value}
 *   action ∈ start | stop | toggle | motor | scene | height
 *   motor 走完整链路：后端 → VM UDP 50009 → duml_can_tool → CAN → ESC
 */
(() => {
  const $ = (id) => document.getElementById(id);
  const TAU = Math.PI * 2;
  const DEG = Math.PI / 180;

  const RANGES = [5, 10, 20, 50];
  const LIFE_MS = 4000;          // 点寿命
  const MAX_PTS = 16000;         // 上限，防内存膨胀
  const SUB_STYLE = [
    { c: 'rgba(120,155,195,0.45)', w: 2.8, name: '地面' },
    { c: 'rgba(240,180,41,0.95)', w: 3.8, name: '线缆' },
    { c: 'rgba(61,220,132,1.0)', w: 4.6, name: '障碍' },
  ];

  const S = {
    connected: false,
    running: false,
    crcOk: null,
    src: '—',
    range: 10,
    heightCm: null,
    udpStatus: null,
    udpEpoch: null,
    scene: 'room',
    motor: { value: 60, status: 'idle', detail: '' },
    points: [],
    tracks: [],
    proto: null,
    total: 0,
    marks: [],            // 帧率统计
  };

  /* ── 工具 ───────────────────────────────────────── */
  const clamp = (v, a, b) => Math.max(a, Math.min(b, v));
  const now = () => performance.now();

  function setBadge(el, text, cls) {
    if (!el) return;
    el.className = 'badge ' + (cls || '');
    el.replaceChildren(document.createElement('i'), text);
  }

  /* ── PPI 画布 ───────────────────────────────────── */
  const canvas = $('ppi');
  const ctx = canvas.getContext('2d');
  let W = 0, H = 0, CX = 0, CY = 0, R = 0, DPR = 1;

  function resize() {
    const wrap = canvas.parentElement;
    DPR = Math.min(window.devicePixelRatio || 1, 2);
    W = wrap.clientWidth;
    H = wrap.clientHeight;
    canvas.width = Math.round(W * DPR);
    canvas.height = Math.round(H * DPR);
    canvas.style.width = W + 'px';
    canvas.style.height = H + 'px';
    ctx.setTransform(DPR, 0, 0, DPR, 0, 0);
    R = Math.max(40, Math.min(W, H) / 2 - 18);
    CX = W / 2;
    CY = H / 2 + 4;
  }

  const toScreen = (azDeg, rM) => {
    const a = azDeg * DEG;
    const rr = clamp(rM / S.range, 0, 1.35) * R;
    return [CX + Math.sin(a) * rr, CY - Math.cos(a) * rr];
  };

  function drawGrid() {
    ctx.clearRect(0, 0, W, H);

    // 外环
    ctx.save();
    ctx.strokeStyle = 'rgba(90,110,135,0.28)';
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.arc(CX, CY, R, 0, TAU);
    ctx.stroke();

    // 量程环 + 标签
    const rings = S.range <= 10 ? [2, 4, 6, 8, 10]
               : S.range <= 20 ? [5, 10, 15, 20]
               : S.range <= 30 ? [10, 20, 30] : [10, 20, 30, 40, 50];
    ctx.font = '10px ui-monospace, Consolas, monospace';
    ctx.fillStyle = 'rgba(120,140,165,0.65)';
    ctx.textAlign = 'center';
    ctx.textBaseline = 'middle';
    for (const m of rings) {
      const rr = (m / S.range) * R;
      ctx.beginPath();
      ctx.strokeStyle = 'rgba(90,110,135,0.17)';
      ctx.arc(CX, CY, rr, 0, TAU);
      ctx.stroke();
      if (m < S.range * 0.995) {
        ctx.fillText(m + ' m', CX, CY - rr - 7);
      }
    }

    // 方位辐条
    ctx.strokeStyle = 'rgba(90,110,135,0.13)';
    for (let a = 0; a < 360; a += 45) {
      const [x, y] = toScreen(a, S.range);
      ctx.beginPath();
      ctx.moveTo(CX, CY);
      ctx.lineTo(x, y);
      ctx.stroke();
    }

    // 方位标签
    ctx.font = '10.5px ui-monospace, Consolas, monospace';
    ctx.fillStyle = 'rgba(130,150,175,0.75)';
    for (let a = 0; a < 360; a += 45) {
      const [x, y] = toScreen(a, S.range * 1.085);
      ctx.fillText(a + '°', x, y);
    }

    // 中心
    ctx.beginPath();
    ctx.fillStyle = 'rgba(45,212,191,0.9)';
    ctx.arc(CX, CY, 2.6, 0, TAU);
    ctx.fill();
    ctx.beginPath();
    ctx.strokeStyle = 'rgba(45,212,191,0.25)';
    ctx.arc(CX, CY, 8, 0, TAU);
    ctx.stroke();
    ctx.restore();
  }

  function drawPoints(t) {
    // 按 subtype 分三遍画，地面最底、障碍最上
    for (let sub = 0; sub < 3; sub++) {
      const st = SUB_STYLE[sub];
      ctx.beginPath();
      let n = 0;
      for (let i = 0; i < S.points.length; i++) {
        const p = S.points[i];
        if (p[4] !== sub) continue;
        const age = (t - p[5]) / LIFE_MS;
        if (age >= 1) continue;
        const [x, y] = toScreen(p[0], p[1]);
        // 障碍点按强度增强半径
        const rad = sub === 2 ? 1.4 + 2.6 * clamp(p[3], 0, 1) : st.w;
        ctx.moveTo(x + rad, y);
        ctx.arc(x, y, rad, 0, TAU);
        n++;
        if (n > MAX_PTS) break;
      }
      if (n === 0) continue;
      ctx.fillStyle = st.c;
      ctx.fill();
    }
  }

  function render() {
    const t = now();
    drawGrid();
    drawPoints(t);
    // 扫描角指示
    const ang = S.lastAngle;
    if (typeof ang === 'number' && S.running) {
      const [x, y] = toScreen(ang, S.range);
      const g = ctx.createLinearGradient(CX, CY, x, y);
      g.addColorStop(0, 'rgba(45,212,191,0.06)');
      g.addColorStop(1, 'rgba(45,212,191,0.30)');
      ctx.beginPath();
      ctx.strokeStyle = g;
      ctx.lineWidth = 1.6;
      ctx.moveTo(CX, CY);
      ctx.lineTo(x, y);
      ctx.stroke();
    }
  }

  function prune(t) {
    if (S.points.length > MAX_PTS) {
      S.points.splice(0, S.points.length - MAX_PTS);
    }
    // 惰性清理：每 1s 清一次过期点
    if (!prune.last || t - prune.last > 800) {
      prune.last = t;
      S.points = S.points.filter((p) => t - p[5] < LIFE_MS);
    }
  }

  /* ── 俯仰剖面 ───────────────────────────────────── */
  const ecl = $('elev');
  const ectx = ecl.getContext('2d');

  function drawElev() {
    const w = ecl.parentElement.clientWidth - 24;
    const h = 62;
    ecl.width = Math.round(w * DPR);
    ecl.height = Math.round(h * DPR);
    ecl.style.width = w + 'px';
    ecl.style.height = h + 'px';
    ectx.setTransform(DPR, 0, 0, DPR, 0, 0);
    ectx.clearRect(0, 0, w, h);

    // 以雷达高度为中线，纵轴 ±60cm 的 z 分布（按距离分箱）
    const NB = 40;
    const bins = new Array(NB).fill(0);
    for (const p of S.points) {
      if (p[4] === 0) continue;          // 地面点不参与剖面
      const b = clamp(Math.floor((p[1] / S.range) * NB), 0, NB - 1);
      bins[b] += 1;
    }
    const mx = Math.max(1, ...bins);
    const bw = w / NB;
    for (let i = 0; i < NB; i++) {
      if (!bins[i]) continue;
      const bh = (bins[i] / mx) * (h - 10);
      ectx.fillStyle = 'rgba(61,220,132,0.55)';
      ectx.fillRect(i * bw + 0.5, h - bh - 1, Math.max(1, bw - 1), bh);
    }
    // 基线
    ectx.strokeStyle = 'rgba(90,110,135,0.25)';
    ectx.beginPath();
    ectx.moveTo(0, h - 0.5);
    ectx.lineTo(w, h - 0.5);
    ectx.stroke();
  }

  /* ── 帧处理 ─────────────────────────────────────── */
  function onFrame(f) {
    if (!f || f.type === 'error') return;

    if (f.source === 'udp') {
      if (S.udpEpoch !== null && S.udpEpoch !== f.data_epoch) clearUdpHistory();
      S.udpEpoch = f.data_epoch ?? null;
      setUdpStatus(f.data_status);
      if (f.data_status !== 'live') return;
    } else {
      S.running = f.running;
      S.heightCm = f.height_cm ?? null;
      $('h-val').textContent = S.heightCm ?? '—';
      syncRunBtn();
    }
    S.lastAngle = f.angle;
    if (f.rpm != null) S.motor.value = f.rpm;

    const t = now();
    const pts = f.points || [];
    for (let i = 0; i < pts.length; i++) {
      const p = pts[i];
      // p = [az, r, z, snr, sub]
      S.points.push([p[0], p[1], p[2], p[3], p[4] | 0, t]);
      S.total++;
    }
    prune(t);
    S.tracks = f.tracks || [];
    S.proto = f.proto || null;

    // 自动量程：有点贴近边缘就升档
    if (pts.length) {
      let maxR = 0;
      for (const p of pts) if (p[1] > maxR) maxR = p[1];
      if (maxR > S.range * 0.96) {
        const next = RANGES.find((r) => r > maxR * 1.04);
        if (next && next > S.range) setRange(next);
      }
    }
    updateUI(f);
  }

  function updateUI(f = {}) {
    // 统计
    const t = now();
    if (f.type === 'frame') S.marks.push([t, (f.points || []).length]);
    while (S.marks.length && t - S.marks[0][0] > 1000) S.marks.shift();
    const fps = S.marks.length;
    let pps = 0;
    for (const m of S.marks) pps += m[1];

    let ng = 0, nw = 0, np = 0;
    for (const p of S.points) {
      if (p[4] === 0) ng++; else if (p[4] === 1) nw++; else np++;
    }
    $('ppi-stat').textContent =
      `点 ${S.points.length} · ${pps} pts/s · 地${ng} 线${nw} 障${np}`;
    $('stat-fps').textContent = `${fps} 帧/s · ${S.total} 点累计`;

    // 最近障碍（只认 pcla）
    let best = null;
    for (const p of S.points) {
      if (p[4] !== 2) continue;
      if (!best || p[1] < best[1]) best = p;
    }
    if (best) {
      $('near-dist').textContent = best[1].toFixed(2);
      const height = S.heightCm == null
        ? `相对雷达 ${best[2].toFixed(2)} m`
        : `离地 ${(S.heightCm / 100 + best[2]).toFixed(2)} m`;
      $('near-az').textContent = `方位 ${best[0].toFixed(1)}° · ${height}`;
    } else {
      $('near-dist').textContent = '—';
      $('near-az').textContent = S.udpStatus && S.udpStatus !== 'live'
        ? '暂无雷达数据'
        : '暂无障碍点';
    }

    // 航迹
    const tr = S.tracks || [];
    $('trk-count').textContent = tr.length;
    const ul = $('trk-list');
    if (!tr.length) {
      ul.innerHTML = '<li class="empty">无确认航迹</li>';
    } else {
      ul.innerHTML = tr.slice(0, 6).map((k) =>
        `<li><span>#${k.id ?? '—'}</span><span>${(k.range ?? k.dist ?? k.r)?.toFixed(2) ?? '—'} m</span><span>${(k.az ?? 0).toFixed?.(0) ?? '—'}°</span></li>`
      ).join('');
    }

    // 协议帧
    const p = f.proto;
    $('proto-kv').textContent = p
      ? `${p.msg} · seq ${p.seq} · ${p.len} B · ${p.count} 点 · CRC ${p.crc_ok ? 'OK' : '—'}`
      : '—';
    $('proto-hex').textContent = p?.hex || '';
    setBadge($('badge-crc'), p?.crc_ok ? 'CRC OK' : 'CRC —', p?.crc_ok ? 'on' : '');

    $('ppi-hint').classList.toggle('hide', !S.udpStatus || S.udpStatus === 'live');
    drawElev();
  }

  /* ── 事件绑定 ───────────────────────────────────── */
  function setRange(r) {
    S.range = r;
    for (const b of $('ranges').children) {
      b.classList.toggle('active', Number(b.dataset.r) === r);
    }
    $('range-foot').textContent = `0 – ${r} m`;
  }

  function buildRanges() {
    const box = $('ranges');
    box.innerHTML = '';
    for (const r of RANGES) {
      const b = document.createElement('button');
      b.textContent = r + ' m';
      b.dataset.r = r;
      b.onclick = () => setRange(r);
      box.appendChild(b);
    }
    setRange(S.range);
  }

  async function control(action, value) {
    try {
      const r = await fetch('/api/control', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ action, value }),
      });
      return await r.json();
    } catch (e) {
      return { ok: false, error: String(e) };
    }
  }

  function bindControls() {
    $('btn-run').onclick = async () => {
      const action = S.running ? 'stop' : 'start';
      const res = await control(action);
      if (res && res.ok !== false) {
        S.running = !S.running;
        syncRunBtn();
      }
    };

    const rpm = $('rpm');
    const rpmLabel = $('rpm-label');
    rpm.oninput = () => { rpmLabel.textContent = rpm.value; };
    rpm.onchange = async () => {
      const v = Number(rpm.value);
      S.motor.value = v;
      S.motor.status = 'sending';
      rpmLabel.textContent = v;
      $('rpm-val').textContent = v;
      $('rpm-sub').textContent = '正在下发…';
      const res = await control('motor', v);
      if (res && res.ok) {
        S.motor.status = 'ok';
        $('rpm-sub').textContent = `已下发 ${v} rpm${res.dry ? '（dry-run）' : ''}`;
      } else {
        S.motor.status = 'err';
        $('rpm-sub').textContent = '下发失败：' + (res && (res.error || res.err) || '未知错误');
      }
    };

    const h = $('height');
    h.oninput = () => {
      S.heightCm = Number(h.value);
      $('h-label').textContent = S.heightCm;
      $('h-val').textContent = S.heightCm;
    };
    h.onchange = () => control('height', S.heightCm);

    $('scene').onchange = (e) => {
      S.scene = e.target.value;
      control('scene', S.scene);
    };
  }

  function setUdpStatus(status = 'waiting') {
    if (S.udpStatus === status) return;
    S.src = 'UDP';
    S.udpStatus = status;
    S.running = null;
    S.heightCm = null;
    $('badge-src').textContent = S.src;
    $('btn-run').disabled = true;
    $('height').disabled = true;
    $('scene').disabled = true;
    $('h-val').textContent = '—';
    $('h-label').textContent = '—';
    $('h-sub').textContent = '1E61 未提供雷达离地高度';
    syncRunBtn();
    if (status === 'live') return;

    clearUdpHistory();
    $('ppi-hint').textContent = status === 'stale' ? '雷达数据超时' : '等待雷达数据…';
    updateUI();
  }

  function clearUdpHistory() {
    S.points = [];
    S.tracks = [];
    S.marks = [];
    S.proto = null;
    S.lastAngle = null;
  }

  function syncRunBtn() {
    const b = $('btn-run');
    if (S.udpStatus) {
      b.textContent = '真机接收';
      const text = { waiting: '等待雷达数据', live: '雷达数据正常', stale: '雷达数据超时' };
      setBadge($('badge-run'), text[S.udpStatus], S.udpStatus === 'live' ? 'on' : 'warn');
      return;
    }
    b.textContent = S.running ? '停止' : '启动';
    b.classList.toggle('stopping', S.running);
    setBadge($('badge-run'), S.running ? '运行中' : '已停止', S.running ? 'on' : '');
  }

  /* ── SSE ────────────────────────────────────────── */
  function connect() {
    const es = new EventSource('/events');
    es.onopen = () => {
      S.connected = true;
      setBadge($('badge-conn'), '已连接', 'on');
    };
    es.onerror = () => {
      S.connected = false;
      setBadge($('badge-conn'), '重连中', 'warn');
      if (S.src === 'UDP') setUdpStatus('waiting');
    };
    es.onmessage = (ev) => {
      try { onFrame(JSON.parse(ev.data)); } catch (_) { /* 忽略坏帧 */ }
    };
  }

  async function bootstrap() {
    buildRanges();
    bindControls();
    resize();
    window.addEventListener('resize', () => { resize(); drawElev(); });

    try {
      const st = await (await fetch('/api/state')).json();
      S.running = !!st.running;
      S.src = (st.source || '—').toUpperCase();
      $('badge-src').textContent = S.src;
      if (st.source === 'udp') setUdpStatus(st.data_status);
      const lastSpeed = st.motor_speed != null ? st.motor_speed : st.rpm;
      if (lastSpeed != null) {
        S.motor.value = lastSpeed;
        $('rpm').value = clamp(lastSpeed, 0, 1800);
        $('rpm-label').textContent = lastSpeed;
        $('rpm-val').textContent = lastSpeed;
        $('rpm-sub').textContent = '已下发 ' + lastSpeed + ' rpm · 拖动滑块可改';
      } else {
        $('rpm-val').textContent = '—';
        $('rpm-sub').textContent = '尚未下发 · 拖动滑块可设定';
      }
      if (st.height_cm != null) {
        S.heightCm = st.height_cm;
        $('height').value = S.heightCm;
        $('h-label').textContent = S.heightCm;
        $('h-val').textContent = S.heightCm;
      }
      setBadge($('badge-crc'), st.crc_ok ? 'CRC OK' : 'CRC —', st.crc_ok ? 'on' : '');
    } catch (_) { /* state 拿不到不致命 */ }

    syncRunBtn();
    connect();

    // 时钟
    const tick = () => {
      const d = new Date();
      $('clock').textContent = d.toTimeString().slice(0, 8);
    };
    tick();
    setInterval(tick, 1000);

    // 转速状态轮询：外部（脚本/其它客户端）改了速度时同步显示
    setInterval(async () => {
      if (document.activeElement === $('rpm')) return; // 正在拖动时不覆盖
      try {
        const st = await (await fetch('/api/state')).json();
        const lastSpeed = st.motor_speed != null ? st.motor_speed : st.rpm;
        if (lastSpeed != null && Number(lastSpeed) !== Number($('rpm').value)) {
          S.motor.value = lastSpeed;
          $('rpm').value = clamp(lastSpeed, 0, 1800);
          $('rpm-label').textContent = lastSpeed;
          $('rpm-val').textContent = lastSpeed;
          $('rpm-sub').textContent = '已下发 ' + lastSpeed + ' rpm · 拖动滑块可改';
        }
      } catch (_) { /* 忽略 */ }
    }, 3000);

    // 渲染循环
    (function loop() {
      render();
      requestAnimationFrame(loop);
    })();
  }

  document.addEventListener('DOMContentLoaded', bootstrap);
})();
