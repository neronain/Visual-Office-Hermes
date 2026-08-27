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
/* Pixel art only survives whole-number zoom — a fractional scale smears every
   sprite edge, which is exactly the "แตกๆ" look. So the scale is always an
   integer, and the +/- buttons step it rather than sliding it. */
const view = { s: 1, ox: 0, oy: 0, fit: 1 };
const zoom = { mode: 'fit', level: 2, panX: 0, panY: 0 };
const MAX_ZOOM = 8;
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

/* The office is a tile grid. Everything downstream — what gets drawn, what
 * blocks a walk, where somebody can sit — is derived from the one furniture
 * list built here, so the picture and the collision map can never disagree.
 *
 * Chairs, benches and sofas are deliberately NOT blocking: a seat you cannot
 * walk onto is a seat nobody ever reaches.
 */

const BLOCKING = new Set([
  'DESK_FRONT', 'TABLE_FRONT', 'COFFEE_TABLE', 'LARGE_PLANT',
  'PLANT', 'PLANT_2', 'BIN', 'SMALL_TABLE_FRONT',
]);

const WORK_PAD = 1;
const DESK_PITCH = 4;
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
  const workX0 = 1;
  const midWall = workX0 + workInterior;
  const loungeX0 = midWall + 1;
  const totalCols = loungeX0 + LOUNGE_INTERIOR + 1;

  const meetRow = FIRST_DESK_ROW + rows * DESK_ROW_PITCH + 1;
  const lastRow = meetRow + MEET_ROWS + 1;
  const totalRows = lastRow + 1;
  const doorTop = WALL_ROW + Math.max(3, Math.floor((totalRows - WALL_ROW) / 2) - 1);

  const plan = {
    cols: totalCols, rows: totalRows,
    W: totalCols * TILE, H: totalRows * TILE,
    workX0, workInterior, midWall, loungeX0, loungeInterior: LOUNGE_INTERIOR,
    firstFloorRow: WALL_ROW + 1, lastRow, meetRow, doorTop, doorRows: 3,
    items: [], seats: [], cells: [], cellsById: new Map(),
    door: { col: midWall, row: doorTop + 1 },
  };

  const add = (name, col, row, mirror) => plan.items.push({ name, col, row, mirror: !!mirror });
  const seat = (id, col, row, dir, kind, deskId) =>
    plan.seats.push({ id, col, row, dir, kind, deskId: deskId || null });

  // Desks, one per model in the roster.
  desks.forEach((desk, i) => {
    const col = workX0 + WORK_PAD + (i % cols) * DESK_PITCH;
    const row = FIRST_DESK_ROW + Math.floor(i / cols) * DESK_ROW_PITCH;
    const cell = { desk, col, row, benchRow: row + 2 };
    plan.cells.push(cell);
    plan.cellsById.set(desk.id, cell);
    add('DESK_FRONT', col, row);
    add('CUSHIONED_BENCH', col + 1, row + 2);
    // Facing up: the monitor is what they are looking at.
    seat(`desk:${desk.id}`, col + 1, row + 2, 'up', 'desk', desk.id);
  });

  // Wall decor sits on the void row above the wall, where the wall art shows.
  add('HANGING_PLANT', workX0, DECOR_ROW);
  add('DOUBLE_BOOKSHELF', workX0 + 1, DECOR_ROW);
  add('CLOCK', workX0 + Math.floor(workInterior / 2), DECOR_ROW);
  add('DOUBLE_BOOKSHELF', workX0 + workInterior - 3, DECOR_ROW);
  add('HANGING_PLANT', workX0 + workInterior - 1, DECOR_ROW);
  add('SMALL_PAINTING', loungeX0 + 1, DECOR_ROW);
  add('LARGE_PAINTING', loungeX0 + 3, DECOR_ROW);
  add('SMALL_PAINTING_2', loungeX0 + 6, DECOR_ROW);

  // Meeting table.
  const tableCol = workX0 + Math.max(0, Math.floor((workInterior - 3) / 2));
  add('TABLE_FRONT', tableCol, meetRow);
  add('PC_SIDE', tableCol, meetRow);
  add('PC_SIDE', tableCol, meetRow + 2);
  add('PC_SIDE', tableCol + 2, meetRow, true);
  add('PC_SIDE', tableCol + 2, meetRow + 2, true);
  [0, 2].forEach((d, i) => {
    add('WOODEN_CHAIR_SIDE', tableCol - 1, meetRow + d);
    add('WOODEN_CHAIR_SIDE', tableCol + 3, meetRow + d, true);
    seat(`meet:l${i}`, tableCol - 1, meetRow + d + 1, 'right', 'meeting');
    seat(`meet:r${i}`, tableCol + 3, meetRow + d + 1, 'left', 'meeting');
  });

  add('PLANT_2', workX0, lastRow - 3);
  add('BIN', workX0, lastRow);
  add('LARGE_PLANT', workX0 + workInterior - 2, lastRow - 3);

  // Lounge.
  const cx = loungeX0 + Math.floor(LOUNGE_INTERIOR / 2) - 1;
  const cy = plan.firstFloorRow + 3;
  add('SOFA_FRONT', cx, cy);
  add('SOFA_SIDE', cx - 1, cy + 1);
  add('COFFEE_TABLE', cx, cy + 1);
  add('COFFEE', cx, cy + 2);
  add('SOFA_SIDE', cx + 2, cy + 1, true);
  add('SOFA_BACK', cx, cy + 3);
  add('PLANT', loungeX0, plan.firstFloorRow);
  add('PLANT_2', loungeX0 + LOUNGE_INTERIOR - 1, plan.firstFloorRow);
  add('SMALL_TABLE_FRONT', loungeX0 + LOUNGE_INTERIOR - 2, lastRow - 3);

  seat('sofa:top-l', cx, cy, 'down', 'lounge');
  seat('sofa:top-r', cx + 1, cy, 'down', 'lounge');
  seat('sofa:bottom-l', cx, cy + 3, 'up', 'lounge');
  seat('sofa:bottom-r', cx + 1, cy + 3, 'up', 'lounge');
  seat('sofa:left', cx - 1, cy + 1, 'right', 'lounge');
  seat('sofa:right', cx + 2, cy + 1, 'left', 'lounge');

  buildGrid(plan);
  return plan;
}

function isWallTile(plan, col, row) {
  if (row === WALL_ROW) return true;
  if (row < WALL_ROW || row > plan.lastRow) return false;
  if (col === 0 || col === plan.cols - 1) return true;
  if (col === plan.midWall) return !(row >= plan.doorTop && row < plan.doorTop + plan.doorRows);
  return false;
}

function buildGrid(plan) {
  const grid = new Uint8Array(plan.cols * plan.rows);
  for (let row = plan.firstFloorRow; row <= plan.lastRow; row++) {
    for (let col = 1; col < plan.cols - 1; col++) {
      if (!isWallTile(plan, col, row)) grid[row * plan.cols + col] = 1;
    }
  }
  plan.items.forEach((item) => {
    if (!BLOCKING.has(item.name)) return;
    const size = FURNITURE[item.name];
    if (!size) return;
    for (let dr = 0; dr < size[1] / TILE; dr++) {
      for (let dc = 0; dc < size[0] / TILE; dc++) {
        const col = item.col + dc, row = item.row + dr;
        if (col >= 0 && col < plan.cols && row >= 0 && row < plan.rows) {
          grid[row * plan.cols + col] = 0;
        }
      }
    }
  });
  // A seat must be reachable, whatever is drawn on it.
  plan.seats.forEach((s) => {
    if (s.col >= 0 && s.col < plan.cols && s.row >= 0 && s.row < plan.rows) {
      grid[s.row * plan.cols + s.col] = 1;
    }
  });
  grid[plan.door.row * plan.cols + plan.door.col] = 1;
  plan.grid = grid;
  plan.walkable = [];
  for (let row = 0; row < plan.rows; row++) {
    for (let col = 0; col < plan.cols; col++) {
      if (grid[row * plan.cols + col]) plan.walkable.push({ col, row });
    }
  }
}

function walkableAt(col, row) {
  if (col < 0 || row < 0 || col >= plan.cols || row >= plan.rows) return false;
  return plan.grid[row * plan.cols + col] === 1;
}

/** Breadth-first path over 4-neighbour tiles. Returns the steps after the start. */
function findPath(fromCol, fromRow, toCol, toRow) {
  if (fromCol === toCol && fromRow === toRow) return [];
  if (!walkableAt(toCol, toRow)) return [];

  const start = fromRow * plan.cols + fromCol;
  const goal = toRow * plan.cols + toCol;
  const parent = new Int32Array(plan.cols * plan.rows).fill(-1);
  const seen = new Uint8Array(plan.cols * plan.rows);
  seen[start] = 1;

  const queue = [start];
  let head = 0;
  const steps = [-plan.cols, plan.cols, -1, 1];

  while (head < queue.length) {
    const at = queue[head++];
    if (at === goal) {
      const path = [];
      let k = goal;
      while (k !== start) {
        path.unshift({ col: k % plan.cols, row: Math.floor(k / plan.cols) });
        k = parent[k];
      }
      return path;
    }
    const col = at % plan.cols;
    for (let i = 0; i < 4; i++) {
      const next = at + steps[i];
      if (next < 0 || next >= seen.length || seen[next]) continue;
      const nextCol = next % plan.cols;
      if (i >= 2 && Math.abs(nextCol - col) !== 1) continue;  // no wrapping across rows
      if (!plan.grid[next]) continue;
      seen[next] = 1;
      parent[next] = at;
      queue.push(next);
    }
  }
  return [];
}

/* ------------------------------------------------------------------ scenery */

function drawFloor() {
  for (let row = plan.firstFloorRow; row <= plan.lastRow; row++) {
    for (let col = 1; col < plan.cols - 1; col++) {
      if (isWallTile(plan, col, row)) continue;
      let img = art.floors.wood;
      if (col > plan.midWall) img = row >= plan.lastRow - 1 ? art.floors.tile : art.floors.carpet;
      blit(img, col * TILE, row * TILE, TILE, TILE);
    }
  }
}

function drawWalls() {
  if (!art.walls) return;
  for (let row = WALL_ROW; row <= plan.lastRow; row++) {
    for (let col = 0; col < plan.cols; col++) {
      if (!isWallTile(plan, col, row)) continue;
      let mask = 0;
      if (isWallTile(plan, col, row - 1)) mask |= 1;
      if (isWallTile(plan, col + 1, row)) mask |= 2;
      if (isWallTile(plan, col, row + 1)) mask |= 4;
      if (isWallTile(plan, col - 1, row)) mask |= 8;
      blitFrame(art.walls, (mask % 4) * 16, Math.floor(mask / 4) * 32, 16, 32,
        col * TILE, (row - 1) * TILE);
    }
  }
  // The wall above a doorway hangs into it; lay the floor back over the opening
  // so the door reads as a way through and not a black slot.
  for (let row = plan.doorTop; row < plan.doorTop + plan.doorRows; row++) {
    blit(art.floors.wood, plan.midWall * TILE, row * TILE, TILE, TILE);
  }
}

/** Which PC sprite a desk shows: dark when the desk is idle, cycling when busy. */
function pcSprite(busy) {
  return busy ? `PC_FRONT_ON_${1 + (Math.floor(frame / 12) % 3)}` : 'PC_FRONT_OFF';
}

function drawScenery(busy) {
  plan.items.forEach((item) => {
    const size = FURNITURE[item.name];
    if (!size) return;
    blit(art.furniture[item.name], item.col * TILE, item.row * TILE, size[0], size[1], item.mirror);
  });
  plan.cells.forEach((cell) => {
    const name = pcSprite(busy.has(cell.desk.id));
    const size = FURNITURE[name];
    blit(art.furniture[name], (cell.col + 1) * TILE, cell.row * TILE, size[0], size[1]);
  });
}

/* ------------------------------------------------------------------ people */

/* The state machine follows the one Pixel Agents uses, because it is the part
 * that makes a room feel inhabited rather than staged:
 *
 *   SIT   working — seated at a desk, typing or reading
 *   WALK  moving along a tile path
 *   IDLE  standing; after a pause, wander to a random tile, and after a few
 *         wanders go back to a seat and rest
 *
 * Movement is tile-by-tile along a BFS path, so nobody walks through a desk.
 */

const DIR_ROW = { down: 0, up: 1, right: 2, left: 2 };
const WALK_CYCLE = [0, 1, 2, 1];
const WALK_SPEED = 44;           // px per second
const WALK_FRAME_SEC = 0.15;
const BUSY_FRAME_SEC = 0.3;
const WANDER_PAUSE = [2.5, 11];
const WANDER_MOVES = [2, 5];
const SEAT_REST = [12, 30];
const SIT_OFFSET = 6;

function randRange(min, max) { return min + Math.random() * (max - min); }
function randInt(min, max) { return min + Math.floor(Math.random() * (max - min + 1)); }

function tileCenter(col, row) {
  return { x: col * TILE + TILE / 2, y: row * TILE + TILE / 2 };
}

function seatById(id) {
  return plan.seats.find((s) => s.id === id) || null;
}

function newActor(worker, seat) {
  const start = seat || plan.seats[0] || { col: plan.door.col, row: plan.door.row, dir: 'down' };
  const at = tileCenter(plan.door.col, plan.door.row);
  return {
    col: plan.door.col, row: plan.door.row,
    x: at.x, y: at.y,
    dir: 'down', state: 'idle',
    frame: 0, frameTimer: 0,
    path: [], progress: 0,
    wanderTimer: randRange(WANDER_PAUSE[0], WANDER_PAUSE[1]),
    wanderCount: 0, wanderLimit: randInt(WANDER_MOVES[0], WANDER_MOVES[1]),
    seatTimer: 0, seatId: start.id || null,
    fade: 1, leaving: false,
  };
}

function repath(actor, col, row) {
  const path = findPath(actor.col, actor.row, col, row);
  if (!path.length) return false;
  actor.path = path;
  actor.progress = 0;
  actor.state = 'walk';
  actor.frame = 0;
  actor.frameTimer = 0;
  return true;
}

function updateActor(actor, worker, dt) {
  actor.frameTimer += dt;
  const seat = seatById(actor.seatId);
  const active = !worker.gone && worker.activity !== 'idle';

  if (worker.gone) {
    if (actor.leaving) {
      actor.y -= WALK_SPEED * 1.4 * dt;
      actor.dir = 'up';
      actor.state = 'walk';
      if (actor.frameTimer >= WALK_FRAME_SEC) {
        actor.frameTimer -= WALK_FRAME_SEC;
        actor.frame = (actor.frame + 1) % 4;
      }
      actor.fade = Math.max(0, actor.fade - dt * 0.9);
      return;
    }
    if (actor.col === plan.door.col && actor.row === plan.door.row && !actor.path.length) {
      actor.leaving = true;
      return;
    }
    if (actor.state !== 'walk' && !repath(actor, plan.door.col, plan.door.row)) {
      actor.leaving = true;
      return;
    }
  }

  switch (actor.state) {
    case 'sit': {
      if (actor.frameTimer >= BUSY_FRAME_SEC) {
        actor.frameTimer -= BUSY_FRAME_SEC;
        actor.frame = (actor.frame + 1) % 2;
      }
      if (active) break;
      if (actor.seatTimer > 0) { actor.seatTimer -= dt; break; }
      actor.state = 'idle';
      actor.frame = 0;
      actor.wanderTimer = randRange(WANDER_PAUSE[0], WANDER_PAUSE[1]);
      actor.wanderCount = 0;
      actor.wanderLimit = randInt(WANDER_MOVES[0], WANDER_MOVES[1]);
      break;
    }

    case 'idle': {
      actor.frame = 0;
      if (active && seat) {
        if (actor.col === seat.col && actor.row === seat.row) {
          actor.state = 'sit';
          actor.dir = seat.dir;
        } else {
          repath(actor, seat.col, seat.row);
        }
        break;
      }
      actor.wanderTimer -= dt;
      if (actor.wanderTimer > 0) break;
      actor.wanderTimer = randRange(WANDER_PAUSE[0], WANDER_PAUSE[1]);

      if (seat && actor.wanderCount >= actor.wanderLimit
          && repath(actor, seat.col, seat.row)) break;

      const spots = plan.walkable;
      if (spots.length) {
        const target = spots[Math.floor(Math.random() * spots.length)];
        if (repath(actor, target.col, target.row)) actor.wanderCount++;
      }
      break;
    }

    case 'walk': {
      if (actor.frameTimer >= WALK_FRAME_SEC) {
        actor.frameTimer -= WALK_FRAME_SEC;
        actor.frame = (actor.frame + 1) % 4;
      }

      if (!actor.path.length) {
        const at = tileCenter(actor.col, actor.row);
        actor.x = at.x;
        actor.y = at.y;
        if (seat && actor.col === seat.col && actor.row === seat.row) {
          actor.state = 'sit';
          actor.dir = seat.dir;
          if (!active) {
            actor.seatTimer = randRange(SEAT_REST[0], SEAT_REST[1]);
            actor.wanderCount = 0;
            actor.wanderLimit = randInt(WANDER_MOVES[0], WANDER_MOVES[1]);
          }
        } else {
          actor.state = 'idle';
          actor.wanderTimer = randRange(WANDER_PAUSE[0], WANDER_PAUSE[1]);
        }
        actor.frame = 0;
        break;
      }

      const next = actor.path[0];
      actor.dir = next.col > actor.col ? 'right'
        : next.col < actor.col ? 'left'
        : next.row > actor.row ? 'down' : 'up';
      actor.progress += (WALK_SPEED / TILE) * dt;

      const from = tileCenter(actor.col, actor.row);
      const to = tileCenter(next.col, next.row);
      const t = Math.min(actor.progress, 1);
      actor.x = from.x + (to.x - from.x) * t;
      actor.y = from.y + (to.y - from.y) * t;

      if (actor.progress >= 1) {
        actor.col = next.col;
        actor.row = next.row;
        actor.x = to.x;
        actor.y = to.y;
        actor.path.shift();
        actor.progress = 0;
      }

      // Work arrived while wandering — turn around and head for the seat.
      if (active && seat) {
        const last = actor.path[actor.path.length - 1];
        if (!last || last.col !== seat.col || last.row !== seat.row) {
          repath(actor, seat.col, seat.row);
        }
      }
      break;
    }
    default:
  }
}

function characterFrame(actor, worker) {
  if (actor.state === 'walk') return WALK_CYCLE[actor.frame % 4];
  if (actor.state === 'idle') return WALK_CYCLE[1];
  // Seated: reading tools get the book pose, everything else types.
  const reading = worker.activity === 'reading' || worker.activity === 'browsing';
  return (reading ? 5 : 3) + (actor.frame % 2);
}

function drawCharacter(actor, worker) {
  const sheet = art.chars[hash(worker.id) % art.chars.length];
  if (!sheet) return;
  const f = characterFrame(actor, worker);
  const sit = actor.state === 'sit' ? SIT_OFFSET : 0;
  const x = actor.x - 8;
  const y = actor.y + sit - 32;

  if (actor.fade < 1) ctx.globalAlpha = Math.max(0, actor.fade);
  blitFrame(sheet, f * 16, DIR_ROW[actor.dir] * 32, 16, 32, x, y, actor.dir === 'left');
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

  // Desk nameplates are off by default: they cover a lot of floor, and the
  // panel already names every desk with its model and its token count.
  if (showPlates) plan.cells.forEach((cell) => {
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
    if (!actor || worker.deskSeat || worker.gone) return;
    const cx = px(actor.x), top = py(actor.y + 5);
    ctx.font = '600 11px "IBM Plex Sans Thai", system-ui, sans-serif';
    const w = Math.min(Math.max(72, ctx.measureText(worker.model || '').width + 14), 180);
    ctx.fillStyle = 'rgba(14,11,18,.7)';
    ctx.fillRect(cx - w / 2, top, w, worker.model ? 28 : 15);
    ctx.fillStyle = '#f2f6fa';
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

let lastTick = 0;
let lastSim = 0;
let simDebt = 0;
const SIM_STEP = 1 / 60;
const SIM_CATCHUP_MAX = 1.5;   // seconds of world advanced per frame, at most

/** Give every worker a seat: its own desk first, then a desk running the same
 *  model (a top-level session IS the work happening at that desk), then a sofa. */
function assignSeats(workers) {
  const taken = new Set();
  workers.filter((w) => !w.gone && w.desk).forEach((w) => {
    w.deskSeat = `desk:${w.desk}`;
    taken.add(w.deskSeat);
  });

  workers.forEach((w) => {
    if (w.gone || w.desk) return;
    w.deskSeat = null;
    if (w.activity === 'idle' || !w.model) return;
    const match = plan.cells.find(
      (c) => c.desk.model === w.model && !taken.has(`desk:${c.desk.id}`)
    );
    if (match) {
      w.deskSeat = `desk:${match.desk.id}`;
      taken.add(w.deskSeat);
    }
  });

  const lounge = plan.seats.filter((s) => s.kind === 'lounge');
  let free = 0;
  workers.forEach((w) => {
    if (w.gone) { w.seatId = null; return; }
    if (w.deskSeat) { w.seatId = w.deskSeat; return; }
    const spot = lounge[free++ % Math.max(1, lounge.length)];
    w.seatId = spot ? spot.id : null;
  });
}

/** Advance every character by `seconds` of world time. Drawing is separate on
 *  purpose: a hidden tab stops painting, but the office should still be running
 *  when the viewer looks back. */
function stepWorld(seconds) {
  if (!plan || !snapshot) return;
  const workers = snapshot.workers;
  simDebt = Math.min(simDebt + Math.max(0, seconds), SIM_CATCHUP_MAX);

  assignSeats(workers);
  const alive = new Set();
  workers.forEach((worker) => {
    alive.add(worker.id);
    let actor = actors.get(worker.id);
    if (!actor) {
      actor = newActor(worker, seatById(worker.seatId));
      actors.set(worker.id, actor);
    }
    actor.seatId = worker.seatId || actor.seatId;
    for (let debt = simDebt; debt >= SIM_STEP; debt -= SIM_STEP) {
      updateActor(actor, worker, SIM_STEP);
    }
  });
  actors.forEach((_, id) => { if (!alive.has(id)) actors.delete(id); });
  simDebt %= SIM_STEP;
  lastSim = performance.now();
}

// requestAnimationFrame stops outright while the page is hidden, so a timer
// keeps the world moving. Hidden tabs throttle this to about once a second,
// which is exactly the resolution the catch-up above is built for.
setInterval(() => {
  const now = performance.now();
  if (now - lastTick < 400) return;   // frames are arriving; nothing to do
  stepWorld((now - lastSim) / 1000);
}, 1000);

function tick(stamp) {
  frame++;
  // A hidden tab gets one frame a second or none at all. Advancing by the raw
  // frame delta would leave everyone frozen mid-stride until the viewer looks
  // back; stepping a fixed simulation forward instead means the office keeps
  // running and is simply drawn less often. The catch-up is capped so a tab
  // left alone for an hour does not try to replay the hour on return.
  const elapsed = lastTick ? (stamp - lastTick) / 1000 : SIM_STEP;
  lastTick = stamp;
  simDebt = Math.min(simDebt + Math.max(0, elapsed), SIM_CATCHUP_MAX);

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
  const shape = desks.map((d) => d.id).join('|');
  if (!plan || plan.shape !== shape) {
    plan = planOffice(desks);
    plan.shape = shape;
    actors.clear();
  }

  const pad = 16;
  view.fit = Math.max(1, Math.floor(Math.min((cw - pad) / plan.W, (ch - pad) / plan.H)));
  view.s = zoom.mode === 'fit' ? view.fit : Math.min(MAX_ZOOM, Math.max(1, zoom.level));

  const overW = Math.max(0, plan.W * view.s - cw);
  const overH = Math.max(0, plan.H * view.s - ch);
  zoom.panX = Math.max(-overW / 2, Math.min(overW / 2, zoom.panX));
  zoom.panY = Math.max(-overH / 2, Math.min(overH / 2, zoom.panY));
  view.ox = (cw - plan.W * view.s) / 2 + zoom.panX;
  view.oy = (ch - plan.H * view.s) / 2 + zoom.panY;
  syncZoomUi(overW > 0 || overH > 0);

  // Advance the world first: what gets drawn below depends on the seat
  // assignments this step produces.
  stepWorld(elapsed);

  const busy = new Set(
    workers
      .filter((w) => !w.gone && w.deskSeat && w.activity !== 'idle')
      .map((w) => w.deskSeat.slice(5))
  );

  drawFloor();
  drawWalls();
  drawScenery(busy);

  const drawn = [];
  workers.forEach((worker) => {
    const actor = actors.get(worker.id);
    if (actor && actor.fade > 0.02) drawn.push({ actor, worker });
  });

  // Painter's order: whoever is further down the room is drawn last.
  drawn.sort((a, b) => a.actor.y - b.actor.y);
  drawn.forEach(({ actor, worker }) => drawCharacter(actor, worker));

  drawLabels(workers);
  requestAnimationFrame(tick);
}

/* ------------------------------------------------------------------ zoom ui */

const ZOOM_KEY = 'visual-office.zoom';
const PLATES_KEY = 'visual-office.plates';
let showPlates = false;

function loadZoom() {
  try {
    const saved = JSON.parse(localStorage.getItem(ZOOM_KEY) || 'null');
    if (saved && (saved.mode === 'fit' || saved.mode === 'manual')) {
      zoom.mode = saved.mode;
      zoom.level = Math.min(MAX_ZOOM, Math.max(1, saved.level | 0)) || 2;
    }
  } catch (err) {
    /* private windows and blocked storage both land here — the default is fine */
  }
}

function saveZoom() {
  try {
    localStorage.setItem(ZOOM_KEY, JSON.stringify({ mode: zoom.mode, level: zoom.level }));
  } catch (err) { /* nothing to do; the setting is a convenience, not state */ }
}

function setZoom(level) {
  zoom.level = Math.min(MAX_ZOOM, Math.max(1, level));
  zoom.mode = 'manual';
  saveZoom();
}

function syncZoomUi(pannable) {
  const label = document.getElementById('zoom-level');
  if (label) label.textContent = `${view.s}×${zoom.mode === 'fit' ? ' พอดี' : ''}`;
  const out = document.getElementById('zoom-out');
  const inc = document.getElementById('zoom-in');
  if (out) out.disabled = view.s <= 1;
  if (inc) inc.disabled = view.s >= MAX_ZOOM;
  canvas.classList.toggle('grabbable', pannable && !canvas.classList.contains('grabbing'));
}

function wirePlates() {
  const button = document.getElementById('toggle-plates');
  if (!button) return;
  try {
    showPlates = localStorage.getItem(PLATES_KEY) === '1';
  } catch (err) { /* blocked storage — the default stands */ }
  const paint = () => {
    button.setAttribute('aria-pressed', showPlates ? 'true' : 'false');
    button.title = showPlates ? 'ซ่อนป้ายชื่อโต๊ะ' : 'แสดงป้ายชื่อโต๊ะ';
  };
  button.addEventListener('click', () => {
    showPlates = !showPlates;
    try { localStorage.setItem(PLATES_KEY, showPlates ? '1' : '0'); } catch (err) { /* fine */ }
    paint();
  });
  paint();
}

function wireZoom() {
  loadZoom();
  const step = (delta) => setZoom((zoom.mode === 'fit' ? view.fit : zoom.level) + delta);
  document.getElementById('zoom-in').addEventListener('click', () => step(1));
  document.getElementById('zoom-out').addEventListener('click', () => step(-1));
  document.getElementById('zoom-fit').addEventListener('click', () => {
    zoom.mode = 'fit';
    zoom.panX = 0;
    zoom.panY = 0;
    saveZoom();
  });

  document.addEventListener('keydown', (event) => {
    if (event.target && /^(INPUT|TEXTAREA|SELECT)$/.test(event.target.tagName)) return;
    if (event.key === '+' || event.key === '=') step(1);
    else if (event.key === '-' || event.key === '_') step(-1);
    else if (event.key === '0') { zoom.mode = 'fit'; zoom.panX = 0; zoom.panY = 0; saveZoom(); }
    else return;
    event.preventDefault();
  });

  // Drag to pan, but only while the room is bigger than the window.
  let dragging = null;
  canvas.addEventListener('pointerdown', (event) => {
    if (!canvas.classList.contains('grabbable')) return;
    dragging = { x: event.clientX, y: event.clientY };
    canvas.classList.add('grabbing');
    canvas.setPointerCapture(event.pointerId);
  });
  canvas.addEventListener('pointermove', (event) => {
    if (!dragging) return;
    zoom.panX += event.clientX - dragging.x;
    zoom.panY += event.clientY - dragging.y;
    dragging = { x: event.clientX, y: event.clientY };
  });
  const stop = () => { dragging = null; canvas.classList.remove('grabbing'); };
  canvas.addEventListener('pointerup', stop);
  canvas.addEventListener('pointercancel', stop);
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


/* ------------------------------------------------------------------ desk editor */

/* Editing desks has to reach Hermes, not just this page. The server writes
 * desks.yaml; the plugin notices the file changed and re-registers the tool, so
 * a desk added here is callable on the next message without a restart.
 *
 * Reads are open, writes need the office token — the same rule the event API
 * uses. A viewer on this machine is handed the token; anyone else has to have
 * been told it, and is asked once. */

const TOKEN_KEY = 'visual-office.token';
let officeToken = null;
let editorState = null;

function loadToken() {
  try { return localStorage.getItem(TOKEN_KEY) || null; } catch (err) { return null; }
}

function saveToken(value) {
  officeToken = value;
  try { localStorage.setItem(TOKEN_KEY, value); } catch (err) { /* fine */ }
}

async function ensureToken() {
  if (officeToken) return officeToken;
  const stored = loadToken();
  if (stored) { officeToken = stored; return officeToken; }
  try {
    const res = await fetch('/api/token');
    if (res.ok) {
      const body = await res.json();
      if (body.token) { saveToken(body.token); return officeToken; }
    }
  } catch (err) { /* fall through to asking */ }
  const typed = window.prompt(
    'ใส่ token ของห้อง (เซิร์ฟเวอร์พิมพ์ตอนเริ่ม หรือดูใน ~/.hermes/visual-office/server.json)'
  );
  if (typed && typed.trim()) { saveToken(typed.trim()); return officeToken; }
  return null;
}

const ORIGIN_LABEL = { local: 'เครื่องเรา', cloud: 'คลาวด์', unknown: 'ไม่ระบุ' };

function deskRow(desk) {
  const row = document.createElement('div');
  row.className = `ed-desk ${desk.origin || 'unknown'}`;

  const field = (label, key, opts = {}) => {
    const wrap = document.createElement('label');
    if (opts.wide) wrap.className = 'wide';
    wrap.append(label);
    const input = document.createElement('input');
    input.type = 'text';
    input.value = desk[key] || '';
    input.maxLength = opts.max || 200;
    input.autocomplete = 'off';
    if (opts.placeholder) input.placeholder = opts.placeholder;
    if (opts.list) input.setAttribute('list', opts.list);
    input.addEventListener('input', () => { desk[key] = input.value; });
    wrap.append(input);
    row.append(wrap);
    return input;
  };

  field('id', 'id', { max: 32, placeholder: 'coder' });
  field('ชื่อที่คนอ่าน', 'label', { max: 64, placeholder: 'ช่างโค้ด' });

  const originWrap = document.createElement('label');
  originWrap.append('มาจาก');
  const origin = document.createElement('select');
  ['local', 'cloud', 'unknown'].forEach((value) => {
    const option = document.createElement('option');
    option.value = value;
    option.textContent = ORIGIN_LABEL[value];
    if ((desk.origin || 'unknown') === value) option.selected = true;
    origin.append(option);
  });
  origin.addEventListener('change', () => {
    desk.origin = origin.value;
    row.className = `ed-desk ${origin.value}`;
  });
  originWrap.append(origin);
  row.append(originWrap);

  const del = document.createElement('button');
  del.type = 'button';
  del.className = 'ed-del';
  del.title = 'ลบโต๊ะนี้';
  del.textContent = '\u00d7';
  del.addEventListener('click', () => {
    editorState.desks = editorState.desks.filter((d) => d !== desk);
    renderEditorDesks();
  });
  row.append(del);

  field('model (alias ที่ gateway เรียกได้)', 'model', {
    wide: true, list: 'model-options', placeholder: 'claude-sonnet-4.8',
  });
  field('toolsets (คั่นด้วย , )', 'toolsets', {
    wide: true, placeholder: 'file, terminal, web',
  });
  field('หมายเหตุ', 'note', { wide: true, max: 200, placeholder: 'โมเดลโค้ด 80B' });

  return row;
}

function renderEditorDesks() {
  const host = document.getElementById('ed-desks');
  host.innerHTML = '';
  editorState.desks.forEach((desk) => host.append(deskRow(desk)));
}

function setEditorMessage(text, ok) {
  const el = document.getElementById('editor-msg');
  el.textContent = text || '';
  el.classList.toggle('ok', !!ok);
}

async function openEditor() {
  const dialog = document.getElementById('editor');
  setEditorMessage('');
  let data = null;
  try {
    const res = await fetch('/api/desks');
    if (res.ok) data = await res.json();
  } catch (err) { /* handled below */ }

  if (!data || !Array.isArray(data.desks)) {
    // Open anyway. A dialog that refuses to appear looks like a broken button,
    // and the reason it refused is exactly what the person needs to read.
    editorState = { desks: [], writable: false };
    document.getElementById('ed-desks').innerHTML = '';
    const path = document.getElementById('editor-path');
    path.textContent = 'อ่านรายชื่อโต๊ะจากเซิร์ฟเวอร์ไม่ได้';
    path.classList.add('bad');
    document.getElementById('ed-save').disabled = true;
    document.getElementById('ed-add').disabled = true;
    setEditorMessage('เซิร์ฟเวอร์ห้องไม่ตอบ — ลองรีเฟรชหน้านี้');
    dialog.showModal();
    return;
  }

  data.office = data.office || {};
  data.gateway = data.gateway || {};

  editorState = {
    office: data.office.name || '',
    gateway: data.gateway.base_url || '',
    writable: data.writable,
    desks: data.desks.map((d) => ({
      id: d.id, label: d.label, model: d.model,
      origin: d.origin || 'unknown', provider: d.provider || '',
      note: d.note || '', role: d.role || 'leaf',
      toolsets: (d.toolsets || []).join(', '),
    })),
  };

  document.getElementById('ed-office-name').value = editorState.office;
  document.getElementById('ed-gateway').value = editorState.gateway;

  const options = document.getElementById('model-options');
  options.innerHTML = '';
  const models = new Set([...(data.known_models || []), ...data.desks.map((d) => d.model)]);
  models.forEach((name) => {
    if (!name) return;
    const option = document.createElement('option');
    option.value = name;
    options.append(option);
  });

  const path = document.getElementById('editor-path');
  path.textContent = data.writable ? data.path : data.problem;
  path.classList.toggle('bad', !data.writable);
  if (data.stale) setEditorMessage(data.stale);
  document.getElementById('ed-save').disabled = !data.writable;
  document.getElementById('ed-add').disabled = !data.writable;

  renderEditorDesks();
  dialog.showModal();
}

async function saveEditor() {
  const save = document.getElementById('ed-save');
  const token = await ensureToken();
  if (!token) { setEditorMessage('ต้องมี token ถึงจะบันทึกได้'); return; }

  save.disabled = true;
  setEditorMessage('กำลังบันทึก…');

  const payload = {
    office: { name: document.getElementById('ed-office-name').value },
    gateway: { base_url: document.getElementById('ed-gateway').value },
    desks: editorState.desks.map((d) => ({
      id: d.id, label: d.label, model: d.model, origin: d.origin,
      provider: d.provider, note: d.note, role: d.role,
      toolsets: d.toolsets,
    })),
  };

  let body;
  try {
    const res = await fetch('/api/desks', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${token}` },
      body: JSON.stringify(payload),
    });
    body = await res.json();
    if (!res.ok) {
      if (res.status === 401) {
        officeToken = null;
        try { localStorage.removeItem(TOKEN_KEY); } catch (err) { /* fine */ }
      }
      setEditorMessage(body.error || `บันทึกไม่สำเร็จ (${res.status})`);
      save.disabled = false;
      return;
    }
  } catch (err) {
    setEditorMessage('ต่อเซิร์ฟเวอร์ไม่ได้');
    save.disabled = false;
    return;
  }

  setEditorMessage(`บันทึกแล้ว ${body.desks} โต๊ะ — ${body.note}`, true);
  save.disabled = false;
  setTimeout(() => document.getElementById('editor').close(), 1600);
}

function wireEditor() {
  const dialog = document.getElementById('editor');
  if (!dialog) return;
  document.getElementById('open-editor').addEventListener('click', openEditor);
  document.getElementById('ed-cancel').addEventListener('click', () => dialog.close());
  document.getElementById('ed-save').addEventListener('click', saveEditor);
  document.getElementById('ed-add').addEventListener('click', () => {
    editorState.desks.push({
      id: '', label: '', model: '', origin: 'local',
      provider: '', note: '', role: 'leaf', toolsets: '',
    });
    renderEditorDesks();
  });
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

wireZoom();
wirePlates();
wireEditor();
loadArt().then(() => {
  fetch('/api/state').then((r) => r.json()).then(apply).catch(() => {});
  connect();
});
requestAnimationFrame(tick);
