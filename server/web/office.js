/* Visual Office — canvas renderer.
 *
 * The server sends a whole snapshot on every change; this file owns only the
 * things a snapshot cannot carry: where each character currently stands, which
 * animation frame it is on, and how it walks from the door to its desk.
 *
 * The one rule that shapes the drawing: a character must say which model is
 * behind it. Desk-bound workers wear a collar in their desk's origin colour —
 * green for a model on our own machines, amber for a cloud model — and carry
 * the alias on a nameplate. Everything else is set dressing.
 */

'use strict';

const PIX = 4;                 // pixels per sprite cell
const SPRITE_W = 9, SPRITE_H = 13;
const DESK_W = 184, DESK_H = 150, DESK_GAP = 22;
const PAD = 36;
const LOUNGE_H = 132;

const SPRITE = [
  '...hhh...',
  '..hhhhh..',
  '..hsssh..',
  '..hsssh..',
  '...sss...',
  '..ccccc..',   // collar row — origin colour
  '.abbbbba.',
  '.abbbbba.',
  '..bbbbb..',
  '..bb.bb..',
  '..pp.pp..',
  '..pp.pp..',
  '..kk.kk..',
];

const ORIGIN_COLOR = { local: '#4fbb80', cloud: '#d9a03c', unknown: '#6d7f90' };

const canvas = document.getElementById('office');
const ctx = canvas.getContext('2d');

let snapshot = null;
let actors = new Map();        // worker id -> { x, y, tx, ty, seed, spawned }
let layout = { desks: new Map(), lounge: [], cols: 1 };
let frame = 0;

/* ---------------------------------------------------------------- helpers */

function hash(str) {
  let h = 2166136261;
  for (let i = 0; i < str.length; i++) {
    h ^= str.charCodeAt(i);
    h = Math.imul(h, 16777619);
  }
  return h >>> 0;
}

function palette(id) {
  const h = hash(id);
  const hue = h % 360;
  return {
    hair: `hsl(${(hue + 200) % 360} 30% ${24 + (h >> 8) % 14}%)`,
    skin: ['#e8bd93', '#d09b6c', '#a9744a', '#f0d0ae', '#8a5a3a'][h % 5],
    shirt: `hsl(${hue} 45% ${42 + (h >> 4) % 12}%)`,
    pants: `hsl(${(hue + 40) % 360} 22% 30%)`,
    shoes: '#2a3340',
  };
}

function fmt(n) {
  if (n >= 1e9) return (n / 1e9).toFixed(1) + 'B';
  if (n >= 1e6) return (n / 1e6).toFixed(1) + 'M';
  if (n >= 1e3) return (n / 1e3).toFixed(1) + 'k';
  return String(n | 0);
}

function dpr() { return window.devicePixelRatio || 1; }

/* Canvas maxWidth condenses glyphs instead of clipping, which turns a long
   model alias into an unreadable smear. Trim to an ellipsis instead. */
function clip(text, maxPx) {
  const value = String(text == null ? '' : text);
  if (ctx.measureText(value).width <= maxPx) return value;
  let lo = 0, hi = value.length;
  while (lo < hi) {
    const mid = (lo + hi + 1) >> 1;
    if (ctx.measureText(value.slice(0, mid) + '…').width <= maxPx) lo = mid;
    else hi = mid - 1;
  }
  return value.slice(0, lo) + '…';
}

/* ---------------------------------------------------------------- layout */

function computeLayout(width, height, desks) {
  const cols = Math.max(1, Math.floor((width - 2 * PAD + DESK_GAP) / (DESK_W + DESK_GAP)));
  const gridW = cols * DESK_W + (cols - 1) * DESK_GAP;
  const left = Math.max(PAD, (width - gridW) / 2);
  const map = new Map();

  desks.forEach((desk, i) => {
    const col = i % cols, row = Math.floor(i / cols);
    map.set(desk.id, {
      x: left + col * (DESK_W + DESK_GAP),
      y: PAD + row * (DESK_H + DESK_GAP),
      desk,
    });
  });

  const rows = Math.ceil(desks.length / cols) || 0;
  const loungeY = Math.max(
    PAD + rows * (DESK_H + DESK_GAP) + 18,
    height - LOUNGE_H
  );

  return { desks: map, cols, loungeY, width, height };
}

function seatFor(worker, index) {
  const cell = worker.desk ? layout.desks.get(worker.desk) : null;
  if (cell) {
    const seated = cell.seatCount || 0;
    cell.seatCount = seated + 1;
    return {
      x: cell.x + 44 + seated * 26,
      y: cell.y + DESK_H - 46,
    };
  }
  // No desk: the open floor along the bottom.
  return {
    x: PAD + 62 + index * 104,
    y: layout.loungeY + 68,
  };
}

/* ---------------------------------------------------------------- drawing */

function drawFloor(w, h) {
  ctx.fillStyle = '#0b1016';
  ctx.fillRect(0, 0, w, h);

  ctx.strokeStyle = 'rgba(34,48,63,.38)';
  ctx.lineWidth = 1;
  for (let x = 0; x <= w; x += 48) {
    ctx.beginPath(); ctx.moveTo(x + .5, 0); ctx.lineTo(x + .5, h); ctx.stroke();
  }
  for (let y = 0; y <= h; y += 48) {
    ctx.beginPath(); ctx.moveTo(0, y + .5); ctx.lineTo(w, y + .5); ctx.stroke();
  }
}

function drawDesk(cell, busy) {
  const { x, y, desk } = cell;
  const color = ORIGIN_COLOR[desk.origin] || ORIGIN_COLOR.unknown;

  // Partition wall behind the desk.
  ctx.fillStyle = busy ? '#182231' : '#141d27';
  ctx.fillRect(x, y, DESK_W, DESK_H - 34);
  ctx.strokeStyle = '#22303f';
  ctx.strokeRect(x + .5, y + .5, DESK_W - 1, DESK_H - 34);

  // Origin stripe — the colour that says local GPU or cloud.
  ctx.fillStyle = color;
  ctx.fillRect(x, y, 4, DESK_H - 34);

  // Monitor.
  const mx = x + 26, my = y + 26;
  ctx.fillStyle = '#0d141c';
  ctx.fillRect(mx, my, 56, 38);
  ctx.fillStyle = busy ? (frame % 24 < 12 ? '#1d3a4d' : '#1a3344') : '#141f2a';
  ctx.fillRect(mx + 3, my + 3, 50, 32);
  if (busy) {
    ctx.fillStyle = 'rgba(79,187,128,.55)';
    for (let i = 0; i < 4; i++) {
      const wLine = 12 + ((hash(desk.id + i) + frame) % 26);
      ctx.fillRect(mx + 6, my + 7 + i * 7, wLine, 2);
    }
  }
  ctx.fillStyle = '#1b2632';
  ctx.fillRect(mx + 24, my + 38, 8, 6);
  ctx.fillRect(mx + 16, my + 44, 24, 3);

  // Desk surface.
  ctx.fillStyle = '#2a3644';
  ctx.fillRect(x + 8, y + DESK_H - 44, DESK_W - 16, 8);
  ctx.fillStyle = '#202a36';
  ctx.fillRect(x + 8, y + DESK_H - 36, DESK_W - 16, 4);

  // Nameplate.
  ctx.fillStyle = '#e6edf4';
  ctx.font = '600 13px "IBM Plex Sans Thai", system-ui, sans-serif';
  ctx.textBaseline = 'top';
  ctx.fillText(clip(desk.label, DESK_W - 20), x + 10, y + DESK_H - 28);

  ctx.fillStyle = color;
  ctx.font = '11px "IBM Plex Mono", ui-monospace, monospace';
  ctx.fillText(clip(desk.model, DESK_W - 20), x + 10, y + DESK_H - 12);
}

function drawSprite(actor, worker) {
  const pal = palette(worker.id);
  const act = worker.activity;
  const walking = Math.abs(actor.tx - actor.x) + Math.abs(actor.ty - actor.y) > 1.5;
  const bob = (act === 'thinking' || act === 'idle') && frame % 60 < 30 ? 1 : 0;
  const step = walking && frame % 16 < 8;
  const type = act === 'typing' && frame % 12 < 6;

  const collar = ORIGIN_COLOR[worker.origin] || pal.shirt;
  const colors = {
    h: pal.hair, s: pal.skin, b: pal.shirt, a: pal.skin,
    p: pal.pants, k: pal.shoes, c: worker.desk ? collar : pal.shirt,
  };

  const ox = Math.round(actor.x - (SPRITE_W * PIX) / 2);
  const oy = Math.round(actor.y - SPRITE_H * PIX + bob);

  if (worker.gone) ctx.globalAlpha = 0.32;

  // Shadow.
  ctx.fillStyle = 'rgba(0,0,0,.35)';
  ctx.fillRect(ox + PIX, oy + SPRITE_H * PIX, (SPRITE_W - 2) * PIX, PIX);

  for (let row = 0; row < SPRITE_H; row++) {
    for (let col = 0; col < SPRITE_W; col++) {
      const key = SPRITE[row][col];
      if (key === '.') continue;
      let dx = col, dy = row;
      // Arms drop while typing; legs alternate while walking.
      if (type && key === 'a') dy += 1;
      if (step && row >= 10 && col < 4) dy -= 1;
      ctx.fillStyle = colors[key] || '#888';
      ctx.fillRect(ox + dx * PIX, oy + dy * PIX, PIX, PIX);
    }
  }

  ctx.globalAlpha = 1;
  drawBubble(worker, ox + (SPRITE_W * PIX) / 2, oy - 6);
  drawTag(worker, actor);
}

function drawBubble(worker, cx, cy) {
  if (worker.needs_input) {
    ctx.fillStyle = '#e4756a';
    ctx.fillRect(cx - 9, cy - 20, 18, 18);
    ctx.fillRect(cx - 3, cy - 2, 6, 5);
    ctx.fillStyle = '#0b1016';
    ctx.fillRect(cx - 2, cy - 17, 4, 9);
    ctx.fillRect(cx - 2, cy - 6, 4, 3);
    return;
  }
  if (worker.activity === 'thinking') {
    ctx.fillStyle = 'rgba(232,163,61,.85)';
    for (let i = 0; i < 3; i++) {
      const on = (Math.floor(frame / 12) % 3) >= i;
      if (!on) continue;
      ctx.fillRect(cx - 10 + i * 8, cy - 12, 5, 5);
    }
    return;
  }
  if (worker.activity === 'reading') {
    ctx.fillStyle = '#dfe7ef';
    ctx.fillRect(cx - 9, cy - 14, 18, 12);
    ctx.fillStyle = '#8fa2b4';
    ctx.fillRect(cx - 6, cy - 11, 12, 2);
    ctx.fillRect(cx - 6, cy - 7, 9, 2);
    return;
  }
  if (worker.activity === 'browsing') {
    ctx.strokeStyle = '#6fc3e8';
    ctx.lineWidth = 2;
    ctx.beginPath();
    ctx.arc(cx, cy - 8, 6, 0, Math.PI * 2);
    ctx.stroke();
    return;
  }
  if (worker.activity === 'running') {
    ctx.fillStyle = '#101820';
    ctx.fillRect(cx - 10, cy - 15, 20, 13);
    ctx.fillStyle = '#4fbb80';
    ctx.fillRect(cx - 7, cy - 11, frame % 20 < 10 ? 6 : 11, 2);
    ctx.fillRect(cx - 7, cy - 7, 4, 2);
  }
}

function drawTag(worker, actor) {
  const label = worker.desk_label || (worker.kind === 'subagent' ? 'ลูกน้อง' : worker.platform || 'session');
  const model = worker.model || '';
  const x = Math.round(actor.x);
  const y = Math.round(actor.y) + 8;

  ctx.textAlign = 'center';
  ctx.textBaseline = 'top';

  ctx.fillStyle = worker.gone ? '#57687a' : '#cbd8e5';
  ctx.font = '600 11px "IBM Plex Sans Thai", system-ui, sans-serif';
  ctx.fillText(clip(label, 132), x, y);

  if (model) {
    ctx.fillStyle = ORIGIN_COLOR[worker.origin] || '#6d7f90';
    ctx.font = '10px "IBM Plex Mono", ui-monospace, monospace';
    ctx.fillText(clip(model, 140), x, y + 13);
  }
  ctx.textAlign = 'left';
}

/* ---------------------------------------------------------------- loop */

function tick() {
  frame++;
  const w = canvas.clientWidth, h = canvas.clientHeight;
  const ratio = dpr();
  if (canvas.width !== Math.round(w * ratio) || canvas.height !== Math.round(h * ratio)) {
    canvas.width = Math.round(w * ratio);
    canvas.height = Math.round(h * ratio);
  }
  ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
  ctx.imageSmoothingEnabled = false;

  drawFloor(w, h);

  const desks = snapshot ? snapshot.desks : [];
  layout = computeLayout(w, h, desks);
  layout.desks.forEach((cell) => { cell.seatCount = 0; });

  const workers = snapshot ? snapshot.workers : [];
  const busyDesks = new Set(
    workers.filter((x) => !x.gone && x.desk && x.activity !== 'idle').map((x) => x.desk)
  );
  layout.desks.forEach((cell) => drawDesk(cell, busyDesks.has(cell.desk.id)));

  // Open-floor divider.
  if (workers.some((x) => !x.desk && !x.gone)) {
    ctx.strokeStyle = 'rgba(34,48,63,.9)';
    ctx.beginPath();
    ctx.moveTo(PAD, layout.loungeY + .5);
    ctx.lineTo(w - PAD, layout.loungeY + .5);
    ctx.stroke();
    ctx.fillStyle = '#3d4c5c';
    ctx.font = '10px "IBM Plex Mono", ui-monospace, monospace';
    ctx.fillText('พื้นที่ส่วนกลาง — session ที่ไม่ได้ผูกโต๊ะ', PAD, layout.loungeY + 8);
  }

  const alive = new Set();
  let deskless = 0;
  workers.forEach((worker) => {
    alive.add(worker.id);
    const target = seatFor(worker, worker.desk || worker.gone ? 0 : deskless++);
    let actor = actors.get(worker.id);
    if (!actor) {
      // Walk in through the door on the left.
      actor = { x: -20, y: target.y, tx: target.x, ty: target.y };
      actors.set(worker.id, actor);
    }
    actor.tx = worker.gone ? -30 : target.x;
    actor.ty = target.y;
    actor.x += (actor.tx - actor.x) * 0.08;
    actor.y += (actor.ty - actor.y) * 0.12;
    if (actor.x > -6) drawSprite(actor, worker);
  });

  actors.forEach((_, id) => { if (!alive.has(id)) actors.delete(id); });

  requestAnimationFrame(tick);
}

/* ---------------------------------------------------------------- panel */

function renderPanel(snap) {
  document.getElementById('office-name').textContent = snap.office.name || 'Visual Office';
  document.getElementById('gateway').textContent = snap.office.gateway_base_url || '';

  const live = snap.workers.filter((w) => !w.gone);
  document.getElementById('stat-active').textContent = live.length;
  document.getElementById('stat-calls').textContent = fmt(snap.totals.calls);
  document.getElementById('stat-in').textContent = fmt(snap.totals.tokens_in);
  document.getElementById('stat-out').textContent = fmt(snap.totals.tokens_out);

  const waitWrap = document.getElementById('stat-waiting-wrap');
  waitWrap.hidden = !snap.waiting;
  document.getElementById('stat-waiting').textContent = snap.waiting;

  document.getElementById('empty').hidden = live.length > 0;

  const desksEl = document.getElementById('desks');
  if (!snap.desks.length) {
    desksEl.innerHTML = '<li class="muted">ยังไม่ได้รับรายชื่อโต๊ะจากปลั๊กอิน</li>';
  } else {
    desksEl.innerHTML = snap.desks.map((d) => {
      const busy = d.seated && d.seated.length ? ' busy' : '';
      const total = d.tokens_in + d.tokens_out;
      const count = total ? `${fmt(total)} tok · ${d.calls} calls` : '—';
      const note = d.note ? `<div class="note">${esc(d.note)}</div>` : '';
      return `<li class="${esc(d.origin)}${busy}">`
        + `<div class="row1"><span class="name">${esc(d.label)}</span>`
        + `<span class="count">${esc(count)}</span></div>`
        + `<div class="model">${esc(d.model)}</div>${note}</li>`;
    }).join('');
  }

  const modelsEl = document.getElementById('models');
  const rows = Object.entries(snap.by_model)
    .filter(([name, v]) => name !== 'unknown' && v.calls > 0)
    .sort((a, b) => (b[1].tokens_in + b[1].tokens_out) - (a[1].tokens_in + a[1].tokens_out));
  modelsEl.innerHTML = rows.length
    ? rows.map(([name, v]) =>
        `<li><span class="m">${esc(name)}</span><span class="n">${fmt(v.tokens_in + v.tokens_out)} tok · ${v.calls}</span></li>`
      ).join('')
    : '<li class="muted">ยังไม่มีการเรียกโมเดล</li>';

  const problems = snap.office.problems || [];
  document.getElementById('problems-wrap').hidden = problems.length === 0;
  document.getElementById('problems').innerHTML = problems.map((p) => `<li>${esc(p)}</li>`).join('');

  document.getElementById('roster-source').textContent = snap.office.roster_source || '';
  const up = snap.office.uptime_seconds || 0;
  document.getElementById('uptime').textContent =
    `uptime ${Math.floor(up / 3600)}h ${Math.floor((up % 3600) / 60)}m · seq ${snap.seq}`;
}

function esc(value) {
  return String(value == null ? '' : value)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

/* ---------------------------------------------------------------- feed */

function setLink(state, title) {
  const dot = document.getElementById('link-dot');
  dot.className = 'dot' + (state ? ' ' + state : '');
  dot.title = title;
}

function apply(snap) {
  snapshot = snap;
  renderPanel(snap);
}

function connect() {
  let source;
  try {
    source = new EventSource('/api/stream');
  } catch (err) {
    return poll();
  }
  source.onopen = () => setLink('live', 'เชื่อมต่อแล้ว');
  source.onmessage = (event) => {
    try { apply(JSON.parse(event.data)); } catch (err) { /* ignore a bad frame */ }
  };
  source.onerror = () => {
    setLink('down', 'หลุดการเชื่อมต่อ — กำลังลองใหม่');
    source.close();
    setTimeout(connect, 3000);
  };
}

function poll() {
  fetch('/api/state')
    .then((r) => r.json())
    .then((snap) => { setLink('live', 'เชื่อมต่อแล้ว (polling)'); apply(snap); })
    .catch(() => setLink('down', 'ต่อเซิร์ฟเวอร์ไม่ได้'))
    .finally(() => setTimeout(poll, 2000));
}

fetch('/api/state').then((r) => r.json()).then(apply).catch(() => {});
connect();
requestAnimationFrame(tick);
