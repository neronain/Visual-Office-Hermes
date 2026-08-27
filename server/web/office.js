/* Visual Office — sprite renderer.
 *
 * The art under web/assets/ is the open-source Pixel Agents furniture, floor,
 * wall and character set (MIT, Pablo De Lucca; characters after JIK-A-4's free
 * Metro City pack). This file is our own renderer for it: no build step, no
 * framework — plain canvas drawImage against a tile grid.
 *
 * Floors and walls ship as grayscale patterns and are tinted at load time with
 * the same luminance→HSL colorize the original editor uses, so a wooden floor
 * and a blue carpet come from the same PNG. Furniture and characters are drawn
 * as-is.
 *
 * The server sends a whole snapshot on every change; this file owns only what a
 * snapshot cannot carry: where each character stands, which way it faces, which
 * animation frame it is on, and how it walks in through the door.
 *
 * The rule that shapes the room: a character must say which model is behind it.
 * Each desk's nameplate carries the alias in its origin colour — green for a
 * model on our own machines, amber for a cloud model — and the monitor only
 * glows while that desk is working.
 */

'use strict';

const TILE = 16;
const ASSETS = 'assets';

/* Colorize values lifted from the reference layout so the rooms read the same. */
const TINT = {
  wood:   { h: 25,  s: 48, b: -43,  c: -88 },
  carpet: { h: 209, s: 39, b: -25,  c: -80 },
  tile:   { h: 209, s: 0,  b: -16,  c: -8 },
  wall:   { h: 214, s: 30, b: -100, c: -55 },
};

const ORIGIN_COLOR = { local: '#4fbb80', cloud: '#e8a33d', unknown: '#b6c0cc' };

/* Furniture we actually place, with the sprite size the manifests declare. */
const FURNITURE = {
  DESK_FRONT:        [48, 32], PC_FRONT_OFF:  [16, 32],
  PC_FRONT_ON_1:     [16, 32], PC_FRONT_ON_2: [16, 32], PC_FRONT_ON_3: [16, 32],
  PC_SIDE:           [16, 32], CUSHIONED_BENCH: [16, 16],
  WOODEN_CHAIR_SIDE: [16, 32], TABLE_FRONT:   [48, 64],
  SOFA_FRONT:        [32, 16], SOFA_BACK:     [32, 16], SOFA_SIDE: [16, 32],
  COFFEE_TABLE:      [32, 32], COFFEE:        [16, 16],
  DOUBLE_BOOKSHELF:  [32, 32], BOOKSHELF:     [32, 16], CLOCK: [16, 32],
  HANGING_PLANT:     [16, 32], WHITEBOARD:    [32, 32],
  SMALL_PAINTING:    [16, 32], SMALL_PAINTING_2: [16, 32], LARGE_PAINTING: [32, 32],
  PLANT:             [16, 32], PLANT_2:       [16, 32], LARGE_PLANT: [32, 48],
  BIN:               [16, 16], SMALL_TABLE_FRONT: [32, 32],
};

const FURNITURE_FOLDER = {
  DESK_FRONT: 'DESK', PC_FRONT_OFF: 'PC', PC_FRONT_ON_1: 'PC', PC_FRONT_ON_2: 'PC',
  PC_FRONT_ON_3: 'PC', PC_SIDE: 'PC', WOODEN_CHAIR_SIDE: 'WOODEN_CHAIR',
  SOFA_FRONT: 'SOFA', SOFA_BACK: 'SOFA', SOFA_SIDE: 'SOFA',
  SMALL_TABLE_FRONT: 'SMALL_TABLE',
};

const canvas = document.getElementById('office');
const ctx = canvas.getContext('2d');

let snapshot = null;
const actors = new Map();
const art = { furniture: {}, floors: [], walls: null, chars: [], ready: false };
const view = { s: 1, ox: 0, oy: 0 };
let plan = null;
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

function fmt(n) {
  if (n >= 1e9) return (n / 1e9).toFixed(1) + 'B';
  if (n >= 1e6) return (n / 1e6).toFixed(1) + 'M';
  if (n >= 1e3) return (n / 1e3).toFixed(1) + 'k';
  return String(n | 0);
}

function loadImage(src) {
  return new Promise((resolve) => {
    const img = new Image();
    img.onload = () => resolve(img);
    img.onerror = () => resolve(null);
    img.src = src;
  });
}

/* ------------------------------------------------------------------ colorize */

function hslToRgb(h, s, l) {
  const c = (1 - Math.abs(2 * l - 1)) * s;
  const hp = (((h % 360) + 360) % 360) / 60;
  const x = c * (1 - Math.abs((hp % 2) - 1));
  let r = 0, g = 0, b = 0;
  if (hp < 1) { r = c; g = x; }
  else if (hp < 2) { r = x; g = c; }
  else if (hp < 3) { g = c; b = x; }
  else if (hp < 4) { g = x; b = c; }
  else if (hp < 5) { r = x; b = c; }
  else { r = c; b = x; }
  const m = l - c / 2;
  return [Math.round((r + m) * 255), Math.round((g + m) * 255), Math.round((b + m) * 255)];
}

/** Grayscale pattern -> flat hue. Same maths the Pixel Agents editor uses. */
function colorize(img, tint) {
  const out = document.createElement('canvas');
  out.width = img.width;
  out.height = img.height;
  const g = out.getContext('2d', { willReadFrequently: true });
  g.drawImage(img, 0, 0);
  const data = g.getImageData(0, 0, out.width, out.height);
  const p = data.data;
  const sat = tint.s / 100;
  for (let i = 0; i < p.length; i += 4) {
    if (p[i + 3] === 0) continue;
    let l = (0.299 * p[i] + 0.587 * p[i + 1] + 0.114 * p[i + 2]) / 255;
    if (tint.c !== 0) l = 0.5 + (l - 0.5) * ((100 + tint.c) / 100);
    if (tint.b !== 0) l += tint.b / 200;
    l = Math.max(0, Math.min(1, l));
    const [r, gg, bb] = hslToRgb(tint.h, sat, l);
    p[i] = r; p[i + 1] = gg; p[i + 2] = bb;
  }
  g.putImageData(data, 0, 0);
  return out;
}

/* ------------------------------------------------------------------ loading */

async function loadArt() {
  const names = Object.keys(FURNITURE);
  const [floors, wall, chars, furn] = await Promise.all([
    Promise.all([0, 6, 8].map((i) => loadImage(`${ASSETS}/floors/floor_${i}.png`))),
    loadImage(`${ASSETS}/walls/wall_0.png`),
    Promise.all([0, 1, 2, 3, 4, 5].map((i) => loadImage(`${ASSETS}/characters/char_${i}.png`))),
    Promise.all(names.map((n) => loadImage(`${ASSETS}/furniture/${FURNITURE_FOLDER[n] || n}/${n}.png`))),
  ]);

  art.floors = {
    carpet: floors[0] ? colorize(floors[0], TINT.carpet) : null,
    wood: floors[1] ? colorize(floors[1], TINT.wood) : null,
    tile: floors[2] ? colorize(floors[2], TINT.tile) : null,
  };
  art.walls = wall ? colorize(wall, TINT.wall) : null;
  art.chars = chars.filter(Boolean);
  names.forEach((n, i) => { if (furn[i]) art.furniture[n] = furn[i]; });
  art.ready = art.chars.length > 0 && !!art.floors.wood;
}

/* ------------------------------------------------------------------ drawing */

function px(v) { return Math.round(view.ox + v * view.s); }
function py(v) { return Math.round(view.oy + v * view.s); }

function blit(img, x, y, w, h, mirror) {
  if (!img) return;
  const dx = px(x), dy = py(y);
  const dw = px(x + w) - dx, dh = py(y + h) - dy;
  if (mirror) {
    ctx.save();
    ctx.translate(dx + dw, dy);
    ctx.scale(-1, 1);
    ctx.drawImage(img, 0, 0, dw, dh);
    ctx.restore();
  } else {
    ctx.drawImage(img, dx, dy, dw, dh);
  }
}

function blitFrame(img, sxp, syp, sw, sh, x, y, mirror) {
  if (!img) return;
  const dx = px(x), dy = py(y);
  const dw = px(x + sw) - dx, dh = py(y + sh) - dy;
  if (mirror) {
    ctx.save();
    ctx.translate(dx + dw, dy);
    ctx.scale(-1, 1);
    ctx.drawImage(img, sxp, syp, sw, sh, 0, 0, dw, dh);
    ctx.restore();
  } else {
    ctx.drawImage(img, sxp, syp, sw, sh, dx, dy, dw, dh);
  }
}

function place(name, col, row, mirror) {
  const size = FURNITURE[name];
  if (!size) return;
  blit(art.furniture[name], col * TILE, row * TILE, size[0], size[1], mirror);
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

/* ------------------------------------------------------------------ the plan */

const WORK_PAD = 1;      // interior columns of padding either side of the desks
const DESK_PITCH = 4;    // 3-tile desk + 1 tile of elbow room
const LOUNGE_INTERIOR = 8;
const DECOR_ROW = 0;
const WALL_ROW = 1;
const FIRST_DESK_ROW = 3;
const DESK_ROW_PITCH = 5;
const MEET_ROWS = 4;

function planOffice(desks) {
  const n = Math.max(desks.length, 1);
  const cols = Math.min(3, n);
  const rows = Math.ceil(n / cols);

  const workInterior = Math.max(9, WORK_PAD * 2 + cols * DESK_PITCH - 1);
  const workX0 = 1;                       // first interior column
  const midWall = workX0 + workInterior;  // shared wall column
  const loungeX0 = midWall + 1;
  const totalCols = loungeX0 + LOUNGE_INTERIOR + 1;

  const meetRow = FIRST_DESK_ROW + rows * DESK_ROW_PITCH + 1;
  const lastRow = meetRow + MEET_ROWS + 1;
  const totalRows = lastRow + 1;

  const cells = desks.map((desk, i) => {
    const c = workX0 + WORK_PAD + (i % cols) * DESK_PITCH;
    const r = FIRST_DESK_ROW + Math.floor(i / cols) * DESK_ROW_PITCH;
    return { desk, col: c, row: r, benchRow: r + 2, seats: 0 };
  });

  const doorTop = WALL_ROW + Math.max(3, Math.floor((totalRows - WALL_ROW) / 2) - 1);

  return {
    cols: totalCols, rows: totalRows,
    W: totalCols * TILE, H: totalRows * TILE,
    workX0, workInterior, midWall, loungeX0, loungeInterior: LOUNGE_INTERIOR,
    firstFloorRow: WALL_ROW + 1, lastRow,
    meetRow, doorTop, doorRows: 3,
    cells, cellsById: new Map(cells.map((c) => [c.desk.id, c])),
  };
}

function isWall(col, row) {
  if (row === WALL_ROW) return true;
  if (row < WALL_ROW || row > plan.lastRow) return false;
  if (col === 0 || col === plan.cols - 1) return true;
  if (col === plan.midWall) return !(row >= plan.doorTop && row < plan.doorTop + plan.doorRows);
  return false;
}

/* ------------------------------------------------------------------ scenery */

function drawFloor() {
  for (let row = plan.firstFloorRow; row <= plan.lastRow; row++) {
    for (let col = 1; col < plan.cols - 1; col++) {
      if (isWall(col, row)) continue;
      let img = art.floors.wood;
      if (col > plan.midWall) {
        img = row >= plan.lastRow - 1 ? art.floors.tile : art.floors.carpet;
      } else if (col === plan.midWall) {
        img = art.floors.wood;
      }
      blit(img, col * TILE, row * TILE, TILE, TILE);
    }
  }
}

function drawWalls() {
  if (!art.walls) return;
  for (let row = WALL_ROW; row <= plan.lastRow; row++) {
    for (let col = 0; col < plan.cols; col++) {
      if (!isWall(col, row)) continue;
      let mask = 0;
      if (isWall(col, row - 1)) mask |= 1;
      if (isWall(col + 1, row)) mask |= 2;
      if (isWall(col, row + 1)) mask |= 4;
      if (isWall(col - 1, row)) mask |= 8;
      const sxp = (mask % 4) * 16;
      const syp = Math.floor(mask / 4) * 32;
      // Wall pieces are two tiles tall and hang above their own tile.
      blitFrame(art.walls, sxp, syp, 16, 32, col * TILE, (row - 1) * TILE);
    }
  }

  // The wall above a doorway hangs down into it. Lay the floor back over the
  // opening so the door reads as a way through and not a black slot.
  for (let row = plan.doorTop; row < plan.doorTop + plan.doorRows; row++) {
    blit(art.floors.wood, plan.midWall * TILE, row * TILE, TILE, TILE);
  }
}

function drawWallDecor() {
  const w0 = plan.workX0, wi = plan.workInterior;
  const r = DECOR_ROW;
  place('HANGING_PLANT', w0, r);
  place('DOUBLE_BOOKSHELF', w0 + 1, r);
  place('CLOCK', w0 + Math.floor(wi / 2), r);
  place('DOUBLE_BOOKSHELF', w0 + wi - 3, r);
  place('HANGING_PLANT', w0 + wi - 1, r);

  const l0 = plan.loungeX0;
  place('SMALL_PAINTING', l0 + 1, r);
  place('LARGE_PAINTING', l0 + 3, r);
  place('SMALL_PAINTING_2', l0 + 6, r);
}

function drawStaticFurniture() {
  const w0 = plan.workX0, wi = plan.workInterior;

  // Meeting table with laptops and chairs down both sides.
  const tableCol = w0 + Math.max(0, Math.floor((wi - 3) / 2));
  const tableRow = plan.meetRow;
  place('WOODEN_CHAIR_SIDE', tableCol - 1, tableRow);
  place('WOODEN_CHAIR_SIDE', tableCol - 1, tableRow + 2);
  place('TABLE_FRONT', tableCol, tableRow);
  place('PC_SIDE', tableCol, tableRow);
  place('PC_SIDE', tableCol, tableRow + 2);
  place('PC_SIDE', tableCol + 2, tableRow, true);
  place('PC_SIDE', tableCol + 2, tableRow + 2, true);
  place('WOODEN_CHAIR_SIDE', tableCol + 3, tableRow, true);
  place('WOODEN_CHAIR_SIDE', tableCol + 3, tableRow + 2, true);

  place('PLANT_2', w0, plan.lastRow - 3);
  place('BIN', w0, plan.lastRow);
  place('LARGE_PLANT', w0 + wi - 2, plan.lastRow - 3);

  // Lounge.
  const l0 = plan.loungeX0;
  const cx = l0 + Math.floor(plan.loungeInterior / 2) - 1;
  const cy = plan.firstFloorRow + 3;
  place('SOFA_FRONT', cx, cy);
  place('SOFA_SIDE', cx - 1, cy + 1);
  place('COFFEE_TABLE', cx, cy + 1);
  place('COFFEE', cx, cy + 2);
  place('SOFA_SIDE', cx + 2, cy + 1, true);
  place('SOFA_BACK', cx, cy + 3);
  place('PLANT', l0, plan.firstFloorRow);
  place('PLANT_2', l0 + plan.loungeInterior - 1, plan.firstFloorRow);
  place('SMALL_TABLE_FRONT', l0 + plan.loungeInterior - 2, plan.lastRow - 3);
}

function drawDesks(busy, waiting) {
  plan.cells.forEach((cell) => {
    place('DESK_FRONT', cell.col, cell.row);
    const on = busy.has(cell.desk.id);
    const name = on
      ? `PC_FRONT_ON_${1 + (Math.floor(frame / 12) % 3)}`
      : 'PC_FRONT_OFF';
    place(name, cell.col + 1, cell.row);
    place('CUSHIONED_BENCH', cell.col + 1, cell.benchRow);
    if (waiting.has(cell.desk.id)) {
      const cx = (cell.col + 1) * TILE + TILE / 2;
      const cy = cell.row * TILE - 6;
      ctx.fillStyle = '#e4756a';
      ctx.fillRect(px(cx - 6), py(cy - 14), px(cx + 6) - px(cx - 6), py(cy) - py(cy - 14));
      ctx.fillStyle = '#3a1512';
      ctx.fillRect(px(cx - 1.5), py(cy - 11), Math.max(1, px(cx + 1.5) - px(cx - 1.5)), py(cy - 5) - py(cy - 11));
      ctx.fillRect(px(cx - 1.5), py(cy - 3.5), Math.max(1, px(cx + 1.5) - px(cx - 1.5)), py(cy - 1.5) - py(cy - 3.5));
    }
  });
}

/* ------------------------------------------------------------------ people */

/* Sheet layout: rows down/up/right, 7 frames of 16x32.
   0-2 walk · 3-4 typing · 5-6 reading. Left is the mirrored right row. */
const DIR_ROW = { down: 0, up: 1, right: 2, left: 2 };
const WALK_CYCLE = [0, 1, 2, 1];

function characterFrame(worker, moving) {
  if (moving) return WALK_CYCLE[Math.floor(frame / 8) % 4];
  switch (worker.activity) {
    case 'typing':
    case 'running':
      return 3 + (Math.floor(frame / 10) % 2);
    case 'reading':
    case 'browsing':
      return 5 + (Math.floor(frame / 16) % 2);
    default:
      return 0;
  }
}

function seatFor(worker, index) {
  const cell = worker.desk ? plan.cellsById.get(worker.desk) : null;
  if (cell) {
    const n = cell.seats++;
    return {
      x: (cell.col + 1) * TILE + TILE / 2 + (n ? (n % 2 ? 18 : -18) : 0),
      y: cell.benchRow * TILE + TILE + (n ? 14 : 0),
      dir: 'down',
      seated: true,
    };
  }
  const l0 = plan.loungeX0;
  const cx = l0 + Math.floor(plan.loungeInterior / 2) - 1;
  const cy = plan.firstFloorRow + 3;
  const spots = [
    { x: (cx + 1) * TILE, y: (cy + 5) * TILE, dir: 'up' },
    { x: (cx - 1) * TILE + 8, y: (cy + 2) * TILE + 8, dir: 'right' },
    { x: (cx + 3) * TILE - 8, y: (cy + 2) * TILE + 8, dir: 'left' },
    { x: (cx + 1) * TILE, y: (cy + 1) * TILE, dir: 'down' },
    { x: (l0 + 1) * TILE, y: (plan.lastRow - 1) * TILE, dir: 'up' },
    { x: (l0 + plan.loungeInterior - 2) * TILE, y: (plan.lastRow - 1) * TILE, dir: 'up' },
  ];
  return Object.assign({ seated: false }, spots[index % spots.length]);
}

function drawCharacter(actor, worker) {
  const sheet = art.chars[hash(worker.id) % art.chars.length];
  if (!sheet) return;
  const dx = actor.tx - actor.x, dy = actor.ty - actor.y;
  const moving = Math.abs(dx) + Math.abs(dy) > 1.5;

  let dir = actor.dir || 'down';
  if (moving) dir = Math.abs(dx) > Math.abs(dy) ? (dx > 0 ? 'right' : 'left') : (dy > 0 ? 'down' : 'up');
  else if (actor.rest) dir = actor.rest;
  actor.dir = dir;

  const f = characterFrame(worker, moving);
  const sit = !moving && actor.seated ? 6 : 0;
  const x = actor.x - 8;
  const y = actor.y + sit - 32;

  if (worker.gone) ctx.globalAlpha = 0.35;
  blitFrame(sheet, f * 16, DIR_ROW[dir] * 32, 16, 32, x, y, dir === 'left');
  ctx.globalAlpha = 1;

  drawBubble(worker, actor.x, y - 2);
}

function drawBubble(worker, cx, cy) {
  const rect = (x, y, w, h, color) => {
    ctx.fillStyle = color;
    ctx.fillRect(px(x), py(y), Math.max(1, px(x + w) - px(x)), Math.max(1, py(y + h) - py(y)));
  };
  if (worker.needs_input) {
    rect(cx - 7, cy - 17, 14, 14, '#e4756a');
    rect(cx - 2, cy - 4, 5, 4, '#e4756a');
    rect(cx - 1.5, cy - 14, 3, 7, '#3a1512');
    rect(cx - 1.5, cy - 6, 3, 2.5, '#3a1512');
    return;
  }
  if (worker.activity === 'thinking') {
    for (let i = 0; i < 3; i++) {
      if ((Math.floor(frame / 14) % 3) < i) continue;
      rect(cx - 9 + i * 7, cy - 11, 4, 4, '#f4e2b4');
    }
  }
}

/* ------------------------------------------------------------------ labels */

function drawLabels(workers) {
  // Fixed type sizes on purpose: the room scales with the window, but a
  // nameplate that scales with it is either unreadable or absurd.
  ctx.textBaseline = 'top';
  ctx.textAlign = 'center';

  plan.cells.forEach((cell) => {
    const tint = ORIGIN_COLOR[cell.desk.origin] || ORIGIN_COLOR.unknown;
    const cx = px((cell.col + 1.5) * TILE);
    const top = py((cell.benchRow + 1) * TILE + 6);

    ctx.font = '600 12px "IBM Plex Sans Thai", system-ui, sans-serif';
    const labelW = ctx.measureText(cell.desk.label).width;
    ctx.font = '10.5px "IBM Plex Mono", ui-monospace, monospace';
    const modelW = ctx.measureText(cell.desk.model).width;
    const plate = Math.min(Math.max(96, Math.max(labelW, modelW) + 18), 190);

    ctx.fillStyle = 'rgba(14,11,18,.84)';
    ctx.fillRect(cx - plate / 2, top, plate, 33);
    ctx.fillStyle = tint;
    ctx.fillRect(cx - plate / 2, top, 3, 33);

    ctx.font = '600 12px "IBM Plex Sans Thai", system-ui, sans-serif';
    ctx.fillStyle = '#f6f1e6';
    ctx.fillText(clip(cell.desk.label, plate - 14), cx + 1, top + 3);

    ctx.font = '10.5px "IBM Plex Mono", ui-monospace, monospace';
    ctx.fillStyle = tint;
    ctx.fillText(clip(cell.desk.model, plate - 14), cx + 1, top + 18);
  });

  workers.forEach((worker) => {
    const actor = actors.get(worker.id);
    if (!actor || worker.desk) return;
    const cx = px(actor.x), top = py(actor.y + 5);
    ctx.font = '600 11px "IBM Plex Sans Thai", system-ui, sans-serif';
    const w = Math.min(Math.max(72, ctx.measureText(worker.model || '').width + 14), 180);
    ctx.fillStyle = 'rgba(14,11,18,.7)';
    ctx.fillRect(cx - w / 2, top, w, worker.model ? 28 : 15);
    ctx.fillStyle = worker.gone ? '#8b98a6' : '#f2f6fa';
    ctx.fillText(clip(worker.platform || 'session', w - 8), cx, top + 1);
    if (worker.model) {
      ctx.font = '10px "IBM Plex Mono", ui-monospace, monospace';
      ctx.fillStyle = '#cfdae6';
      ctx.fillText(clip(worker.model, w - 8), cx, top + 15);
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
  ctx.fillStyle = '#141019';
  ctx.fillRect(0, 0, cw, ch);

  if (!art.ready) { requestAnimationFrame(tick); return; }

  const desks = snapshot ? snapshot.desks : [];
  const workers = snapshot ? snapshot.workers : [];
  plan = planOffice(desks);
  plan.cells.forEach((c) => { c.seats = 0; });

  const pad = 12;
  view.s = Math.max(1, Math.min((cw - pad) / plan.W, (ch - pad) / plan.H, 4));
  view.ox = (cw - plan.W * view.s) / 2;
  view.oy = (ch - plan.H * view.s) / 2;

  drawFloor();
  drawWalls();
  drawWallDecor();
  drawStaticFurniture();

  const busy = new Set(
    workers.filter((w) => !w.gone && w.desk && w.activity !== 'idle').map((w) => w.desk)
  );
  const waiting = new Set(workers.filter((w) => w.needs_input && w.desk).map((w) => w.desk));
  drawDesks(busy, waiting);

  const doorX = plan.midWall * TILE + TILE / 2;
  const doorY = (plan.doorTop + 1) * TILE + TILE;
  const alive = new Set();
  let loose = 0;
  workers.forEach((worker) => {
    alive.add(worker.id);
    const seat = seatFor(worker, worker.desk || worker.gone ? 0 : loose++);
    let actor = actors.get(worker.id);
    if (!actor) {
      actor = { x: doorX, y: doorY, tx: seat.x, ty: seat.y, dir: 'down' };
      actors.set(worker.id, actor);
    }
    actor.tx = worker.gone ? doorX : seat.x;
    actor.ty = worker.gone ? doorY : seat.y;
    actor.rest = worker.gone ? 'down' : seat.dir;
    actor.seated = !worker.gone && seat.seated;
    actor.x += (actor.tx - actor.x) * 0.06;
    actor.y += (actor.ty - actor.y) * 0.08;
    drawCharacter(actor, worker);
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

loadArt().then(() => {
  fetch('/api/state').then((r) => r.json()).then(apply).catch(() => {});
  connect();
});
requestAnimationFrame(tick);
