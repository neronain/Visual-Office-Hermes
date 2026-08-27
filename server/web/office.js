/* Visual Office — top-down pixel office renderer.
 *
 * The server sends a whole snapshot on every change; this file owns only what a
 * snapshot cannot carry: where each character stands, which animation frame it
 * is on, and how it walks in through the door.
 *
 * Everything is drawn into a fixed virtual room, then scaled to fit the canvas
 * with integer-snapped rectangles, so the art stays crisp at any size. Labels
 * are drawn afterwards in screen space so text never blurs.
 *
 * The rule that shapes the art: a character must say which model is behind it.
 * A desk's chair and nameplate take the origin colour — green for a model on our
 * own machines, amber for a cloud model — and desk workers wear a collar to
 * match. Everything else is set dressing.
 */

'use strict';

/* ------------------------------------------------------------------ palette */

const C = {
  void:      '#141019',
  wall:      '#2e3a5c',
  wallTrim:  '#1f2742',
  wallBase:  '#3a4870',
  wood:      '#7d4c2c',
  woodLine:  '#6b3f24',
  carpet:    '#41719f',
  carpetAlt: '#4a7cae',
  tileA:     '#e4e0d6',
  tileB:     '#262b36',
  deskTop:   '#c9954f',
  deskEdge:  '#a1743a',
  deskLeg:   '#7c8596',
  crt:       '#dfe2e6',
  crtDark:   '#b3b8c0',
  crtOff:    '#3b3138',
  crtOn:     '#c0392b',
  chairDark: '#3f8a4a',
  meetSeat:  '#c3c9d4',
  meetSeatD: '#98a0af',
  sofa:      '#b8425f',
  sofaDark:  '#8e2f47',
  sofaLite:  '#d1596f',
  leaf:      '#2f7a3f',
  leafLite:  '#43a055',
  pot:       '#8a4f2c',
  frame:     '#b8863f',
  art:       '#5b4a6b',
  shelf:     '#8a5a33',
  shelfLine: '#6d4527',
  metal:     '#8b93a3',
  shadow:    'rgba(0,0,0,.28)',
};

const BOOKS = ['#c0392b', '#e8a33d', '#4fbb80', '#5b8fd6', '#b06fc0', '#d9d0b8'];
const ORIGIN_COLOR = { local: '#4fbb80', cloud: '#e8a33d', unknown: '#8b93a3' };

/* ------------------------------------------------------------------ sprite */

const SPRITE = [
  '...hhhhh...',
  '..hhhhhhh..',
  '.hhhhhhhhh.',
  '.hhsssssh..',
  '.hse.s.esh.',
  '.hhsssssh..',
  '..sssssss..',
  '...ccccc...',
  '..abbbbba..',
  '..abbbbba..',
  '..abbbbba..',
  '...bbbbb...',
  '...ppppp...',
  '...pp.pp...',
  '...pp.pp...',
  '...kk.kk...',
];
const SPRITE_W = 11, SPRITE_H = 16, PX = 3;

/* ------------------------------------------------------------------ layout */

const MARGIN = 22;
const WALL_H = 92;
const DESK_W = 168, DESK_H = 132, GAP_X = 26, GAP_Y = 22;
const WORK_PAD = 34;
const MEET_H = 150;
const DIVIDER = 34;
const LOUNGE_W = 404;
const MIN_ROOM_H = 452;

const canvas = document.getElementById('office');
const ctx = canvas.getContext('2d');

let snapshot = null;
const actors = new Map();
const view = { s: 1, ox: 0, oy: 0 };
let room = null;
let frame = 0;

/* ------------------------------------------------------------------ helpers */

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
    hair: ['#2c2118', '#4a2c1a', '#1b1b22', '#6b4a2a', '#3a2438'][h % 5],
    skin: ['#e8bd93', '#d09b6c', '#a9744a', '#f0d0ae', '#8a5a3a'][(h >> 3) % 5],
    shirt: `hsl(${hue} 48% 52%)`,
    pants: `hsl(${(hue + 30) % 360} 24% 34%)`,
    shoes: '#2a3340',
  };
}

function fmt(n) {
  if (n >= 1e9) return (n / 1e9).toFixed(1) + 'B';
  if (n >= 1e6) return (n / 1e6).toFixed(1) + 'M';
  if (n >= 1e3) return (n / 1e3).toFixed(1) + 'k';
  return String(n | 0);
}

function sx(x) { return view.ox + x * view.s; }
function sy(y) { return view.oy + y * view.s; }

/** Draw one world-space rectangle, snapped to whole device pixels. */
function R(x, y, w, h, color) {
  const x0 = Math.round(sx(x)), y0 = Math.round(sy(y));
  ctx.fillStyle = color;
  ctx.fillRect(
    x0, y0,
    Math.max(1, Math.round(sx(x + w)) - x0),
    Math.max(1, Math.round(sy(y + h)) - y0)
  );
}

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

/* ------------------------------------------------------------------ room plan */

function planRoom(desks) {
  const n = Math.max(desks.length, 1);
  const cols = Math.min(3, n);
  const rows = Math.ceil(n / cols);

  const workW = WORK_PAD * 2 + cols * DESK_W + (cols - 1) * GAP_X;
  const gridTop = MARGIN + WALL_H + WORK_PAD - 12;
  const workH = WORK_PAD + rows * (DESK_H + GAP_Y) + MEET_H;
  const roomH = Math.max(workH, MIN_ROOM_H);

  const workX = MARGIN;
  const workY = MARGIN + WALL_H;
  const divX = workX + workW;

  const cellList = desks.map((desk, i) => ({
    desk,
    x: workX + WORK_PAD + (i % cols) * (DESK_W + GAP_X),
    y: gridTop + Math.floor(i / cols) * (DESK_H + GAP_Y),
    seats: 0,
  }));

  return {
    W: MARGIN * 2 + workW + DIVIDER + LOUNGE_W,
    H: MARGIN * 2 + WALL_H + roomH,
    workX, workY, workW, roomH,
    divX,
    loungeX: divX + DIVIDER,
    loungeW: LOUNGE_W,
    meetY: gridTop + rows * (DESK_H + GAP_Y),
    doorY: workY + roomH * 0.52,
    cells: new Map(cellList.map((c) => [c.desk.id, c])),
    cellList,
  };
}

/* ------------------------------------------------------------------ scenery */

function drawFloors() {
  R(0, 0, room.W, room.H, C.void);

  R(room.workX, MARGIN, room.workW, WALL_H, C.wall);
  R(room.loungeX, MARGIN, room.loungeW, WALL_H, C.wall);
  R(room.workX, MARGIN, room.workW, 5, C.wallTrim);
  R(room.loungeX, MARGIN, room.loungeW, 5, C.wallTrim);
  R(room.workX, MARGIN + WALL_H - 6, room.workW, 6, C.wallBase);
  R(room.loungeX, MARGIN + WALL_H - 6, room.loungeW, 6, C.wallBase);

  // Work room — wooden planks.
  R(room.workX, room.workY, room.workW, room.roomH, C.wood);
  let band = 0;
  for (let y = room.workY; y < room.workY + room.roomH; y += 22, band++) {
    R(room.workX, y, room.workW, 2, C.woodLine);
    const stagger = band % 2 ? 60 : 0;
    for (let x = room.workX + stagger; x < room.workX + room.workW; x += 120) {
      R(x, y, 2, 22, C.woodLine);
    }
  }

  // Lounge — carpet, with a checkered strip at the front.
  R(room.loungeX, room.workY, room.loungeW, room.roomH, C.carpet);
  for (let y = room.workY; y < room.workY + room.roomH; y += 44) {
    R(room.loungeX, y, room.loungeW, 22, C.carpetAlt);
  }
  const tileTop = room.workY + room.roomH - 96;
  for (let ty = 0; ty < 4; ty++) {
    for (let tx = 0; tx * 24 < room.loungeW; tx++) {
      R(
        room.loungeX + tx * 24, tileTop + ty * 24,
        Math.min(24, room.loungeW - tx * 24), 24,
        (tx + ty) % 2 === 0 ? C.tileA : C.tileB
      );
    }
  }

  // Dividing wall with the doorway everyone walks through.
  R(room.divX, MARGIN, DIVIDER, room.roomH + WALL_H, C.wallTrim);
  R(room.divX + 4, MARGIN, DIVIDER - 8, room.roomH + WALL_H, C.wall);
  R(room.divX, room.doorY - 34, DIVIDER, 68, C.void);
  R(room.divX, room.doorY - 36, DIVIDER, 4, C.wallBase);
  R(room.divX, room.doorY + 32, DIVIDER, 4, C.wallBase);
}

function drawBookshelf(x, y, w) {
  R(x, y, w, 40, C.shelf);
  R(x, y + 18, w, 3, C.shelfLine);
  R(x, y + 37, w, 3, C.shelfLine);
  for (let row = 0; row < 2; row++) {
    let bx = x + 3, i = 0;
    while (bx < x + w - 5) {
      const bw = 4 + (hash(`${x}${row}${i}`) % 3);
      R(bx, y + 4 + row * 19, bw, 13, BOOKS[(hash(`b${x}${row}${i}`) >> 2) % BOOKS.length]);
      bx += bw + 2;
      i++;
    }
  }
}

function drawHangingPlant(x, y) {
  R(x + 6, y, 6, 12, C.pot);
  for (let i = 0; i < 7; i++) {
    const h = 16 + (hash(`hp${x}${i}`) % 22);
    const px = x - 5 + i * 4;
    R(px, y + 8, 3, h, i % 2 ? C.leaf : C.leafLite);
    R(px - 2, y + 2 + h, 3, 6, C.leaf);
  }
}

function drawClock(x, y) {
  R(x, y, 30, 30, '#e8e4dc');
  R(x + 2, y + 2, 26, 26, '#fbf7ee');
  R(x + 14, y + 8, 2, 8, '#c0392b');
  R(x + 14, y + 14, 8, 2, '#3b3138');
}

function drawPicture(x, y, w, h) {
  R(x, y, w, h, C.frame);
  R(x + 3, y + 3, w - 6, h - 6, C.art);
  const slot = (w - 12) / 4;
  for (let i = 0; i < 4; i++) {
    const bh = 6 + (hash(`pic${x}${i}`) % 12);
    R(x + 6 + i * slot, y + h - 6 - bh, Math.max(4, slot - 3), bh, BOOKS[(hash(`f${x}${i}`) >> 3) % BOOKS.length]);
  }
}

function drawPlant(x, y) {
  for (let i = 0; i < 6; i++) {
    const a = -0.95 + i * 0.38;
    const len = 22 + (hash(`pl${x}${i}`) % 10);
    for (let t = 0; t < len; t += 3) {
      R(x + 14 + Math.sin(a) * t, y + 26 - Math.cos(a) * t, 4, 4, i % 2 ? C.leaf : C.leafLite);
    }
  }
  R(x + 3, y + 26, 24, 20, C.pot);
  R(x + 3, y + 26, 24, 4, '#a2603a');
}

function drawTrashCan(x, y) {
  R(x, y, 18, 4, C.metal);
  R(x + 2, y + 4, 14, 18, '#6f7889');
  R(x + 5, y + 7, 2, 12, C.metal);
  R(x + 11, y + 7, 2, 12, C.metal);
}

function drawDesk(cell, busy, needsInput) {
  const { x, y, desk } = cell;
  const tint = ORIGIN_COLOR[desk.origin] || ORIGIN_COLOR.unknown;

  R(x + 4, y + 44, DESK_W - 8, 30, C.deskTop);
  R(x + 4, y + 70, DESK_W - 8, 6, C.deskEdge);
  R(x + 10, y + 76, 5, 16, C.deskLeg);
  R(x + DESK_W - 15, y + 76, 5, 16, C.deskLeg);

  const mx = x + DESK_W / 2 - 27, my = y + 6;
  R(mx, my, 54, 40, C.crt);
  R(mx, my + 34, 54, 6, C.crtDark);
  R(mx + 5, my + 5, 44, 26, C.crtOff);
  if (busy) {
    R(mx + 5, my + 5, 44, 26, frame % 26 < 13 ? '#4a2a2c' : '#3f2427');
    for (let i = 0; i < 3; i++) {
      const w = 8 + ((hash(desk.id + i) + frame) % 30);
      R(mx + 8, my + 9 + i * 7, Math.min(w, 38), 3, needsInput ? '#e4756a' : C.crtOn);
    }
  } else {
    R(mx + 8, my + 9, 16, 3, '#55444a');
  }
  R(mx + 21, my + 40, 12, 5, C.crtDark);

  R(x + DESK_W / 2 - 22, y + 52, 44, 12, C.crt);
  R(x + DESK_W / 2 - 19, y + 55, 38, 6, C.crtDark);

  // The chair carries the origin colour, so the room reads by model source.
  R(x + DESK_W / 2 - 20, y + 96, 40, 13, tint);
  R(x + DESK_W / 2 - 20, y + 107, 40, 4, C.chairDark);
}

function drawMeetingTable() {
  const x = room.workX + WORK_PAD + 6;
  const w = room.workW - WORK_PAD * 2 - 12;
  const y = room.meetY + 40;
  const h = 76;
  const slot = (w - 60) / 2.4;

  for (let i = 0; i < 3; i++) {
    R(x + 26 + i * slot, y - 26, 30, 22, C.meetSeat);
    R(x + 26 + i * slot, y - 8, 30, 5, C.meetSeatD);
  }
  R(x, y, w, h, C.deskTop);
  R(x, y + h - 8, w, 8, C.deskEdge);
  for (let i = 0; i < 3; i++) {
    R(x + 26 + i * slot, y + h + 4, 30, 22, C.meetSeat);
    R(x + 26 + i * slot, y + h + 4, 30, 5, C.meetSeatD);
  }
  R(x - 26, y + 20, 20, 32, C.meetSeat);
  R(x + w + 6, y + 20, 20, 32, C.meetSeat);

  drawPlant(room.workX + 8, room.workY + room.roomH - 88);
  drawTrashCan(room.workX + room.workW - 34, room.workY + room.roomH - 40);
}

function drawLounge() {
  const lx = room.loungeX, ly = room.workY;
  const cx = lx + room.loungeW / 2;
  const cy = ly + room.roomH * 0.42;

  R(cx - 62, cy - 78, 124, 30, C.sofa);
  R(cx - 62, cy - 78, 124, 7, C.sofaLite);
  R(cx - 62, cy - 54, 124, 6, C.sofaDark);

  R(cx - 62, cy + 44, 124, 30, C.sofa);
  R(cx - 62, cy + 44, 124, 7, C.sofaLite);
  R(cx - 62, cy + 68, 124, 6, C.sofaDark);

  R(cx - 104, cy - 34, 32, 88, C.sofa);
  R(cx - 104, cy - 34, 8, 88, C.sofaLite);
  R(cx + 72, cy - 34, 32, 88, C.sofa);
  R(cx + 96, cy - 34, 8, 88, C.sofaDark);

  R(cx - 54, cy - 32, 108, 74, C.deskTop);
  R(cx - 54, cy + 34, 108, 8, C.deskEdge);
  R(cx - 10, cy - 6, 20, 12, C.metal);
  R(cx + 8, cy - 2, 9, 4, C.metal);

  drawPlant(lx + 14, ly + 10);
  drawPlant(lx + room.loungeW - 44, ly + 10);
}

function drawWallDecor() {
  const wx = room.workX, lx = room.loungeX;
  drawHangingPlant(wx + 14, MARGIN + 8);
  drawBookshelf(wx + 58, MARGIN + 18, 108);
  drawClock(wx + room.workW / 2 - 15, MARGIN + 22);
  drawBookshelf(wx + room.workW - 166, MARGIN + 18, 108);
  drawHangingPlant(wx + room.workW - 32, MARGIN + 8);

  drawPicture(lx + 34, MARGIN + 20, 40, 48);
  drawPicture(lx + room.loungeW / 2 - 52, MARGIN + 26, 104, 40);
  drawPicture(lx + room.loungeW - 74, MARGIN + 20, 40, 48);
}

/* ------------------------------------------------------------------ people */

function seatFor(worker, index) {
  const cell = worker.desk ? room.cells.get(worker.desk) : null;
  if (cell) {
    const n = cell.seats++;
    return { x: cell.x + DESK_W / 2 + (n ? 34 * (n % 2 ? 1 : -1) : 0), y: cell.y + 108 + (n ? 18 : 0) };
  }
  const cx = room.loungeX + room.loungeW / 2;
  const cy = room.workY + room.roomH * 0.42;
  const spots = [
    { x: cx - 84, y: cy + 100 }, { x: cx + 6, y: cy + 104 }, { x: cx + 86, y: cy + 100 },
    { x: cx - 84, y: cy - 86 }, { x: cx + 6, y: cy - 92 }, { x: cx + 86, y: cy - 86 },
  ];
  return spots[index % spots.length];
}

function drawSprite(actor, worker) {
  const pal = palette(worker.id);
  const act = worker.activity;
  const walking = Math.abs(actor.tx - actor.x) + Math.abs(actor.ty - actor.y) > 2;
  const bob = !walking && (act === 'thinking' || act === 'idle') && frame % 64 < 32 ? 1 : 0;
  const step = walking && frame % 18 < 9;
  const typing = act === 'typing' && frame % 14 < 7;

  const colors = {
    h: pal.hair, s: pal.skin, e: '#2a2028', a: pal.skin,
    b: pal.shirt, p: pal.pants, k: pal.shoes,
    c: worker.desk ? (ORIGIN_COLOR[worker.origin] || pal.shirt) : pal.shirt,
  };

  const ox = actor.x - (SPRITE_W * PX) / 2;
  const oy = actor.y - SPRITE_H * PX + bob;

  if (worker.gone) ctx.globalAlpha = 0.3;
  R(ox + PX * 2, oy + SPRITE_H * PX, (SPRITE_W - 4) * PX, PX, C.shadow);

  for (let row = 0; row < SPRITE_H; row++) {
    for (let col = 0; col < SPRITE_W; col++) {
      const key = SPRITE[row][col];
      if (key === '.') continue;
      let dy = row;
      if (typing && key === 'a') dy += 1;
      if (step && row >= 13 && col < 5) dy -= 1;
      R(ox + col * PX, oy + dy * PX, PX, PX, colors[key] || '#888');
    }
  }
  ctx.globalAlpha = 1;

  drawBubble(worker, ox + (SPRITE_W * PX) / 2, oy - 4);
}

function drawBubble(worker, cx, cy) {
  if (worker.needs_input) {
    R(cx - 11, cy - 26, 22, 22, '#e4756a');
    R(cx - 4, cy - 5, 8, 6, '#e4756a');
    R(cx - 2, cy - 22, 4, 11, '#3a1512');
    R(cx - 2, cy - 9, 4, 4, '#3a1512');
    return;
  }
  switch (worker.activity) {
    case 'thinking':
      for (let i = 0; i < 3; i++) {
        if ((Math.floor(frame / 14) % 3) < i) continue;
        R(cx - 13 + i * 10, cy - 16, 6, 6, '#f0d9a8');
      }
      return;
    case 'reading':
      R(cx - 12, cy - 19, 24, 16, '#efe9dd');
      R(cx - 8, cy - 15, 16, 3, '#9aa4b2');
      R(cx - 8, cy - 10, 11, 3, '#9aa4b2');
      return;
    case 'browsing':
      R(cx - 11, cy - 19, 22, 16, '#1d2b3a');
      R(cx - 8, cy - 16, 16, 3, '#6fc3e8');
      R(cx - 8, cy - 11, 9, 3, '#6fc3e8');
      return;
    case 'running':
      R(cx - 12, cy - 19, 24, 16, '#101820');
      R(cx - 9, cy - 15, frame % 24 < 12 ? 7 : 13, 3, '#4fbb80');
      R(cx - 9, cy - 10, 5, 3, '#4fbb80');
      return;
    default:
  }
}

/* ------------------------------------------------------------------ labels */

function drawLabels(workers) {
  ctx.textBaseline = 'top';
  ctx.textAlign = 'center';

  room.cellList.forEach((cell) => {
    const tint = ORIGIN_COLOR[cell.desk.origin] || ORIGIN_COLOR.unknown;
    const cx = sx(cell.x + DESK_W / 2);
    const top = sy(cell.y + DESK_H - 12);
    const width = DESK_W * view.s;

    // A nameplate, so the label reads on a wooden floor instead of fighting it.
    R(cell.x + 8, cell.y + DESK_H - 16, DESK_W - 16, 40, 'rgba(20,16,25,.78)');
    R(cell.x + 8, cell.y + DESK_H - 16, 4, 40, tint);

    ctx.font = `600 ${Math.max(11, 13 * view.s)}px "IBM Plex Sans Thai", system-ui, sans-serif`;
    ctx.fillStyle = '#f6f1e6';
    ctx.fillText(clip(cell.desk.label, width), cx, top);

    ctx.font = `${Math.max(10, 11.5 * view.s)}px "IBM Plex Mono", ui-monospace, monospace`;
    ctx.fillStyle = tint;
    ctx.fillText(clip(cell.desk.model, width), cx, top + Math.max(15, 17 * view.s));
  });

  workers.forEach((worker) => {
    const actor = actors.get(worker.id);
    if (!actor || worker.desk) return;
    const cx = sx(actor.x), top = sy(actor.y + 6);
    ctx.font = `600 ${Math.max(10, 11.5 * view.s)}px "IBM Plex Sans Thai", system-ui, sans-serif`;
    ctx.fillStyle = worker.gone ? '#7d8a99' : '#f2f6fa';
    ctx.fillText(clip(worker.platform || 'session', 160), cx, top);
    if (worker.model) {
      ctx.font = `${Math.max(9, 10.5 * view.s)}px "IBM Plex Mono", ui-monospace, monospace`;
      ctx.fillStyle = '#c7d4e2';
      ctx.fillText(clip(worker.model, 180), cx, top + Math.max(13, 14 * view.s));
    }
  });

  ctx.textAlign = 'left';
}

/* ------------------------------------------------------------------ loop */

function tick() {
  frame++;
  const cw = canvas.clientWidth, ch = canvas.clientHeight;
  const ratio = window.devicePixelRatio || 1;
  if (canvas.width !== Math.round(cw * ratio) || canvas.height !== Math.round(ch * ratio)) {
    canvas.width = Math.round(cw * ratio);
    canvas.height = Math.round(ch * ratio);
  }
  ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
  ctx.imageSmoothingEnabled = false;
  ctx.fillStyle = C.void;
  ctx.fillRect(0, 0, cw, ch);

  const desks = snapshot ? snapshot.desks : [];
  const workers = snapshot ? snapshot.workers : [];
  room = planRoom(desks);

  view.s = Math.min(cw / room.W, ch / room.H);
  view.ox = (cw - room.W * view.s) / 2;
  view.oy = (ch - room.H * view.s) / 2;

  drawFloors();
  drawWallDecor();
  drawLounge();
  if (desks.length) drawMeetingTable();

  const busy = new Set(
    workers.filter((w) => !w.gone && w.desk && w.activity !== 'idle').map((w) => w.desk)
  );
  const waiting = new Set(workers.filter((w) => w.needs_input && w.desk).map((w) => w.desk));
  room.cellList.forEach((cell) => drawDesk(cell, busy.has(cell.desk.id), waiting.has(cell.desk.id)));

  const alive = new Set();
  let loose = 0;
  workers.forEach((worker) => {
    alive.add(worker.id);
    const target = seatFor(worker, worker.desk || worker.gone ? 0 : loose++);
    let actor = actors.get(worker.id);
    if (!actor) {
      actor = { x: room.divX + DIVIDER / 2, y: room.doorY, tx: target.x, ty: target.y };
      actors.set(worker.id, actor);
    }
    actor.tx = worker.gone ? room.divX + DIVIDER / 2 : target.x;
    actor.ty = worker.gone ? room.doorY : target.y;
    actor.x += (actor.tx - actor.x) * 0.07;
    actor.y += (actor.ty - actor.y) * 0.09;
    drawSprite(actor, worker);
  });
  actors.forEach((_, id) => { if (!alive.has(id)) actors.delete(id); });

  drawLabels(workers);
  requestAnimationFrame(tick);
}

/* ------------------------------------------------------------------ panel */

function esc(value) {
  return String(value == null ? '' : value)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

function renderPanel(snap) {
  document.getElementById('office-name').textContent = snap.office.name || 'Visual Office';
  document.getElementById('gateway').textContent = snap.office.gateway_base_url || '';

  const live = snap.workers.filter((w) => !w.gone);
  document.getElementById('stat-active').textContent = live.length;
  document.getElementById('stat-calls').textContent = fmt(snap.totals.calls);
  document.getElementById('stat-in').textContent = fmt(snap.totals.tokens_in);
  document.getElementById('stat-out').textContent = fmt(snap.totals.tokens_out);

  document.getElementById('stat-waiting-wrap').hidden = !snap.waiting;
  document.getElementById('stat-waiting').textContent = snap.waiting;
  document.getElementById('empty').hidden = live.length > 0;

  document.getElementById('desks').innerHTML = snap.desks.length
    ? snap.desks.map((d) => {
        const busy = d.seated && d.seated.length ? ' busy' : '';
        const total = d.tokens_in + d.tokens_out;
        const count = total ? `${fmt(total)} tok · ${d.calls} calls` : '—';
        const note = d.note ? `<div class="note">${esc(d.note)}</div>` : '';
        return `<li class="${esc(d.origin)}${busy}">`
          + `<div class="row1"><span class="name">${esc(d.label)}</span>`
          + `<span class="count">${esc(count)}</span></div>`
          + `<div class="model">${esc(d.model)}</div>${note}</li>`;
      }).join('')
    : '<li class="muted">ยังไม่ได้รับรายชื่อโต๊ะจากปลั๊กอิน</li>';

  const rows = Object.entries(snap.by_model)
    .filter(([name, v]) => name !== 'unknown' && v.calls > 0)
    .sort((a, b) => (b[1].tokens_in + b[1].tokens_out) - (a[1].tokens_in + a[1].tokens_out));
  document.getElementById('models').innerHTML = rows.length
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

/* ------------------------------------------------------------------ feed */

function setLink(state, title) {
  const dot = document.getElementById('link-dot');
  dot.className = 'dot' + (state ? ' ' + state : '');
  dot.title = title;
}

function apply(snap) {
  snapshot = snap;
  renderPanel(snap);
}

function poll() {
  fetch('/api/state')
    .then((r) => r.json())
    .then((snap) => { setLink('live', 'เชื่อมต่อแล้ว (polling)'); apply(snap); })
    .catch(() => setLink('down', 'ต่อเซิร์ฟเวอร์ไม่ได้'))
    .finally(() => setTimeout(poll, 2000));
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

fetch('/api/state').then((r) => r.json()).then(apply).catch(() => {});
connect();
requestAnimationFrame(tick);
