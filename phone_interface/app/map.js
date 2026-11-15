// Forest map: a draggable / zoomable mock of the area covered by three
// microphones. World units are metres; y grows downwards (south).
//
// Triangulation (shown here as a MOCK): a sound reaches each microphone at a
// slightly different time, because sound travels ~343 m per second. The time
// differences tell how much further the sound travelled to each microphone,
// which pins down where it came from. On the map this is drawn as a circle
// around each microphone (radius = distance to the sound); the circles meet
// at the source.

const NS = 'http://www.w3.org/2000/svg';
export const WORLD = { w: 4000, h: 3600 };
export const SPEED_OF_SOUND = 343;               // metres per second
export const MICS = [
  { sensor: 0, name: 'Mic 1', x: 2000, y: 560 },  // meadow, at the forest edge
  { sensor: 1, name: 'Mic 2', x: 1300, y: 1750 }, // on trees inside the forest
  { sensor: 2, name: 'Mic 3', x: 2700, y: 1750 },
];
const MAX_S = 2.5;                   // closest zoom (pixels per metre)
const LABEL_S = 0.45;                // show time labels from this zoom in
const RING_MS = 750;                 // triangulation ring animation
const RING_STAGGER_MS = 260;

function svgEl(tag, attrs = {}, parent) {
  const e = document.createElementNS(NS, tag);
  for (const [k, v] of Object.entries(attrs)) e.setAttribute(k, v);
  if (parent) parent.appendChild(e);
  return e;
}
const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));
const easeOut = (k) => 1 - Math.pow(1 - k, 3);

// ---- deterministic randomness (same event id -> same spot every time) ----
export function hashStr(s) {
  let h = 2166136261;
  for (let i = 0; i < s.length; i++) { h ^= s.charCodeAt(i); h = Math.imul(h, 16777619); }
  return h >>> 0;
}
export function rng(seed) {
  let a = seed >>> 0;
  return () => {
    a = (a + 0x6D2B79F5) | 0;
    let t = Math.imul(a ^ (a >>> 15), 1 | a);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}
export function forestEdge(x) {
  return 820 + 70 * Math.sin(x / 310) + 35 * Math.sin(x / 97 + 1.3);
}

/** Made-up location in the forest, inside the triangle of microphones (where
 *  triangulation works best). `near` keeps related events together: logging
 *  next to the chainsawing, poaching next to the gunshots. */
export function mockLocation(seed, near) {
  const r = rng(hashStr(seed));
  let x, y;
  if (near) {
    const a = r() * Math.PI * 2, d = 90 + r() * 110;
    x = near.x + Math.cos(a) * d; y = near.y + Math.sin(a) * d;
  } else {
    let u = r(), v = r();
    if (u + v > 1) { u = 1 - u; v = 1 - v; }        // uniform point in triangle
    const [A, B, C] = MICS, cx = (A.x + B.x + C.x) / 3, cy = (A.y + B.y + C.y) / 3;
    x = A.x + u * (B.x - A.x) + v * (C.x - A.x);
    y = A.y + u * (B.y - A.y) + v * (C.y - A.y);
    x = cx + (x - cx) * 0.85; y = cy + (y - cy) * 0.85;  // stay off the edges
  }
  x = clamp(x, 120, WORLD.w - 120);
  y = clamp(Math.max(y, forestEdge(x) + 120), 0, WORLD.h - 120);
  return { x: Math.round(x), y: Math.round(y) };
}

export function distanceM(p, mic) { return Math.hypot(p.x - mic.x, p.y - mic.y); }
export function bearingText(p, mic) {
  const deg = (Math.atan2(p.x - mic.x, -(p.y - mic.y)) * 180 / Math.PI + 360) % 360;
  const names = ['N', 'NNE', 'NE', 'ENE', 'E', 'ESE', 'SE', 'SSE', 'S', 'SSW', 'SW', 'WSW', 'W', 'WNW', 'NW', 'NNW'];
  return names[Math.round(deg / 22.5) % 16];
}
export function formatDist(m) {
  return m >= 1000 ? `${(m / 1000).toFixed(1)} km` : `${Math.round(m / 10) * 10} m`;
}

/** Mock triangulation result: when each microphone heard the sound.
 *  Returns microphones sorted by arrival, plus an accuracy radius. */
export function triangulate(ev) {
  const hits = MICS.map((mic) => {
    const d = distanceM(ev, mic);
    return { mic, d, t: d / SPEED_OF_SOUND };
  }).sort((a, b) => a.t - b.t);
  for (const hit of hits) hit.dt = hit.t - hits[0].t;
  const errorM = Math.round(15 + (1 - (ev.probability ?? 0.6)) * 60);
  return { hits, errorM, nearest: hits[0] };
}

// ---- static scenery ------------------------------------------------------
const TRACK = 'M2070 600 C2160 760 1900 950 2050 1250 S2400 1800 2300 2300 S2000 3000 2150 3600';
const STREAM = 'M0 2250 C600 2100 900 2550 1500 2400 S2600 2650 3100 2450 S3700 2600 4000 2530';

function drawWorld(svg, g) {
  const defs = svgEl('defs', {}, svg);
  const mow = svgEl('pattern', { id: 'mow', width: 160, height: 160, patternUnits: 'userSpaceOnUse',
                                 patternTransform: 'rotate(-8)' }, defs);
  svgEl('rect', { width: 80, height: 160, class: 'w-mow' }, mow);

  svgEl('rect', { width: WORLD.w, height: WORLD.h, class: 'w-floor' }, g);
  let d = `M0 0 H${WORLD.w} V${forestEdge(WORLD.w).toFixed(0)}`;
  for (let x = WORLD.w; x >= 0; x -= 40) d += ` L${x} ${forestEdge(x).toFixed(0)}`;
  d += ' Z';
  svgEl('path', { d, class: 'w-field' }, g);
  svgEl('path', { d, fill: 'url(#mow)' }, g);

  svgEl('path', { class: 'w-track', d: TRACK }, g);
  svgEl('path', { class: 'w-water', d: STREAM }, g);

  const r = rng(42), trees = [];
  const clearOfMics = (x, y, m) => MICS.every((mic) => Math.hypot(x - mic.x, y - mic.y) > m);
  for (let i = 0; i < 1400; i++) {
    const x = r() * WORLD.w, top = forestEdge(x) - 30;
    const y = top + r() * (WORLD.h - top);
    trees.push({ x, y, s: 16 + r() * 22, c: 1 + Math.floor(r() * 3) });
  }
  for (let i = 0; i < 40; i++) {                         // lone trees in the meadow
    const x = r() * WORLD.w, y = 60 + r() * (forestEdge(x) - 160);
    if (clearOfMics(x, y, 180)) trees.push({ x, y, s: 14 + r() * 14, c: 1 + Math.floor(r() * 3) });
  }
  trees.sort((a, b) => a.y - b.y);
  const tg = svgEl('g', { class: 'w-trees' }, g);
  for (const t of trees) svgEl('circle', { cx: t.x.toFixed(0), cy: t.y.toFixed(0), r: t.s.toFixed(0), class: `t${t.c}` }, tg);

  // re-draw the track and stream over the trees so they read as clearings
  svgEl('path', { class: 'w-track over', d: TRACK }, g);
  svgEl('path', { class: 'w-water over', d: STREAM }, g);

  for (const [x, y, text] of [[700, 380, 'Meadow'], [3350, 1500, 'Forest'], [700, 3100, 'Forest']]) {
    svgEl('text', { x, y, class: 'w-label' }, g).textContent = text;
  }
}

const SYMBOLS = {
  chainsawing: '<path d="M-7 2.5 L-4.6 -2.5 L-2.2 2.5 L0.2 -2.5 L2.6 2.5 L5 -2.5 L7 1" class="sym-line"/>',
  logging: '<path d="M-5 6 V0 H5 V6 Z" class="sym-fill"/><ellipse cx="0" cy="0" rx="5" ry="2" class="sym-fill"/>' +
           '<path d="M2 -3 L8 -8" class="sym-line"/><path d="M5 -9 L9 -5" class="sym-line"/>',
  gunshot: '<circle r="5" class="sym-line"/><path d="M0 -8.5 V-3 M0 3 V8.5 M-8.5 0 H-3 M3 0 H8.5" class="sym-line"/>',
  poaching: '<circle r="5" class="sym-line"/><circle r="2" class="sym-fill"/><path d="M0 -8.5 V-3 M0 3 V8.5 M-8.5 0 H-3 M3 0 H8.5" class="sym-line"/>',
};
const MIC_GLYPH =
  '<rect x="-4" y="-11" width="8" height="13" rx="4" class="sym-fill"/>' +
  '<path d="M-7.5 -1 A7.5 7.5 0 0 0 7.5 -1 M0 6.5 V10 M-4 10 H4" class="sym-line"/>';

function hhmm(t) {
  return new Date(t).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
}
const textW = (s, px = 6.6) => s.length * px + 16;

// ---- the interactive map -------------------------------------------------
export class ForestMap {
  constructor(root, { onSelect, bottomInset } = {}) {
    this.root = root;
    this.onSelect = onSelect || (() => {});
    this.bottomInset = bottomInset || (() => 0);   // height covered by the details sheet
    this.events = [];
    this.markers = new Map();
    this.selected = null;
    this.selectedAt = 0;
    this.view = { s: 0.3, tx: 0, ty: 0 };
    this.ready = false;

    this.svg = svgEl('svg', { class: 'map-svg' }, root);
    this.world = svgEl('g', {}, this.svg);
    drawWorld(this.svg, this.world);

    const ov = this.overlay = svgEl('g', {}, this.svg);
    this.coverage = svgEl('polygon', { class: 'coverage' }, ov);

    // triangulation drawing: one ring + line + distance label per microphone
    this.tri = svgEl('g', { class: 'tri' }, ov);
    this.triParts = MICS.map(() => ({
      ring: svgEl('circle', { class: 'tri-ring' }, this.tri),
      line: svgEl('line', { class: 'tri-line' }, this.tri),
    }));
    for (const p of this.triParts) {             // labels above all rings/lines
      p.labelBg = svgEl('rect', { class: 'tri-label-bg', rx: 9, height: 18 }, this.tri);
      p.label = svgEl('text', { class: 'tri-label' }, this.tri);
    }
    this.fix = svgEl('circle', { class: 'tri-fix' }, this.tri);

    this.markerLayer = svgEl('g', {}, ov);
    this.micEls = MICS.map((mic) => {
      const g = svgEl('g', { class: 'mic' }, ov);
      g.innerHTML =
        '<circle r="30" class="mic-glow"/>' +
        '<rect x="-17" y="-17" width="34" height="34" rx="10" class="mic-body"/>' + MIC_GLYPH +
        '<rect y="22" height="20" rx="10" class="label-bg"/><text y="36" class="label-txt"></text>';
      return { mic, g, bg: g.querySelector('.label-bg'), txt: g.querySelector('.label-txt'), text: null };
    });
    this.selLayer = svgEl('g', {}, ov);          // selected marker sits above the mic labels

    // on-screen controls
    this.scale = document.createElement('div');
    this.scale.className = 'scalebar';
    this.scale.innerHTML = '<i></i><span></span>';
    const ctl = document.createElement('div');
    ctl.className = 'map-controls';
    ctl.innerHTML =
      '<button data-z="in" aria-label="Zoom in">+</button>' +
      '<button data-z="out" aria-label="Zoom out">−</button>' +
      '<button data-z="fit" aria-label="Show all microphones">' +
      '<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="3"/><path d="M12 2v4M12 18v4M2 12h4M18 12h4"/><circle cx="12" cy="12" r="7" fill="none"/></svg></button>';
    ctl.addEventListener('click', (e) => {
      const z = e.target.closest('button')?.dataset.z;
      const w = root.clientWidth, h = root.clientHeight;
      if (z === 'in') this.zoomAt(w / 2, h / 2, 1.6, true);
      if (z === 'out') this.zoomAt(w / 2, h / 2, 1 / 1.6, true);
      if (z === 'fit') this.animateTo(this.fitView(MICS, this.bottomInset() + 30));
    });
    root.append(this.scale, ctl);

    this.bindGestures();
    new ResizeObserver(() => this.onResize()).observe(root);
  }

  onResize() {
    const w = this.root.clientWidth, h = this.root.clientHeight;
    if (!w || !h) return;
    if (!this.ready) {
      this.ready = true;
      this.view = this.clampView(this.fitView(MICS));
    }
    this.render();
  }

  /** View that fits these points, leaving room for overlays (and the sheet). */
  fitView(points, bottomPad = 40) {
    const w = this.root.clientWidth || 360, h = this.root.clientHeight || 600;
    const xs = points.map((p) => p.x), ys = points.map((p) => p.y);
    const minX = Math.min(...xs), maxX = Math.max(...xs), minY = Math.min(...ys), maxY = Math.max(...ys);
    const top = 70, side = 50, availH = h - top - bottomPad;
    const s = clamp(Math.min((w - 2 * side) / Math.max(1, maxX - minX), availH / Math.max(1, maxY - minY)),
                    this.minS(), MAX_S);
    return { s, tx: w / 2 - ((minX + maxX) / 2) * s, ty: top + availH / 2 - ((minY + maxY) / 2) * s };
  }

  toScreen(p) { const v = this.view; return { x: p.x * v.s + v.tx, y: p.y * v.s + v.ty }; }

  // ---- gestures: one finger drags, two fingers pinch, mouse wheel zooms --
  bindGestures() {
    const svg = this.svg, pts = new Map();
    let start = null, pinch = null, downMarker = null;
    const pos = (e) => { const r = svg.getBoundingClientRect(); return { x: e.clientX - r.left, y: e.clientY - r.top }; };

    svg.addEventListener('pointerdown', (e) => {
      svg.setPointerCapture(e.pointerId);
      pts.set(e.pointerId, pos(e));
      this.stopAnim();
      if (pts.size === 1) {
        downMarker = e.target.closest?.('[data-id]')?.dataset.id || null;
        start = { ...pos(e), tx: this.view.tx, ty: this.view.ty, moved: false };
      } else if (pts.size === 2) {
        const [a, b] = [...pts.values()];
        pinch = { d: Math.hypot(a.x - b.x, a.y - b.y), mid: { x: (a.x + b.x) / 2, y: (a.y + b.y) / 2 }, ...this.view };
        if (start) start.moved = true;
      }
    });
    svg.addEventListener('pointermove', (e) => {
      if (!pts.has(e.pointerId)) return;
      pts.set(e.pointerId, pos(e));
      if (pts.size === 1 && start) {
        const p = pos(e), dx = p.x - start.x, dy = p.y - start.y;
        if (Math.hypot(dx, dy) > 6) start.moved = true;
        if (start.moved) { this.view.tx = start.tx + dx; this.view.ty = start.ty + dy; this.clampView(); this.schedule(); }
      } else if (pts.size === 2 && pinch) {
        const [a, b] = [...pts.values()];
        const d = Math.hypot(a.x - b.x, a.y - b.y), mid = { x: (a.x + b.x) / 2, y: (a.y + b.y) / 2 };
        const s = clamp(pinch.s * d / pinch.d, this.minS(), MAX_S);
        const wx = (pinch.mid.x - pinch.tx) / pinch.s, wy = (pinch.mid.y - pinch.ty) / pinch.s;
        this.view = { s, tx: mid.x - wx * s, ty: mid.y - wy * s };
        this.clampView(); this.schedule();
      }
    });
    const end = (e) => {
      if (!pts.has(e.pointerId)) return;
      pts.delete(e.pointerId);
      if (pts.size === 0) {
        if (start && !start.moved && e.type === 'pointerup') this.select(downMarker);
        start = null; pinch = null;
      } else if (pts.size === 1) {          // finger lifted mid-pinch: keep dragging
        pinch = null;
        const p = [...pts.values()][0];
        start = { ...p, tx: this.view.tx, ty: this.view.ty, moved: true };
      }
    };
    svg.addEventListener('pointerup', end);
    svg.addEventListener('pointercancel', end);
    svg.addEventListener('wheel', (e) => {
      e.preventDefault();
      const p = pos(e);
      this.zoomAt(p.x, p.y, Math.exp(-e.deltaY * 0.0015));
    }, { passive: false });
    // stop iOS Safari zooming the whole page on pinch
    for (const t of ['gesturestart', 'gesturechange']) svg.addEventListener(t, (e) => e.preventDefault());
  }

  // zoomed all the way out, the world still fills the screen
  minS() { return Math.max(this.root.clientWidth / WORLD.w, this.root.clientHeight / WORLD.h, 0.01); }
  clampView(v = this.view) {
    // never show empty space beyond the edge of the mapped area
    const w = this.root.clientWidth, h = this.root.clientHeight;
    v.s = clamp(v.s, this.minS(), MAX_S);
    v.tx = clamp(v.tx, w - WORLD.w * v.s, 0);
    v.ty = clamp(v.ty, h - WORLD.h * v.s, 0);
    return v;
  }

  zoomAt(x, y, factor, animate = false) {
    const v = this.view, s = clamp(v.s * factor, this.minS(), MAX_S);
    const wx = (x - v.tx) / v.s, wy = (y - v.ty) / v.s;
    const target = { s, tx: x - wx * s, ty: y - wy * s };
    if (animate) this.animateTo(target); else { this.view = target; this.clampView(); this.schedule(); }
  }

  animateTo(target, ms = 420) {
    this.stopAnim();
    target = this.clampView({ ...target });
    const from = { ...this.view }, t0 = performance.now();
    const step = (now) => {
      const e = easeOut(Math.min(1, (now - t0) / ms));
      this.view = { s: from.s + (target.s - from.s) * e, tx: from.tx + (target.tx - from.tx) * e, ty: from.ty + (target.ty - from.ty) * e };
      this.render();
      this.anim = e < 1 ? requestAnimationFrame(step) : null;
    };
    this.anim = requestAnimationFrame(step);
  }
  stopAnim() { if (this.anim) cancelAnimationFrame(this.anim); this.anim = null; }

  schedule() {
    if (this.pending) return;
    this.pending = requestAnimationFrame(() => { this.pending = null; this.render(); });
  }

  // ---- data ---------------------------------------------------------------
  setEvents(events) {
    this.events = [...events].sort((a, b) => a.time - b.time);
    const keep = new Set(this.events.map((e) => e.id));
    for (const [id, m] of this.markers) if (!keep.has(id)) { m.remove(); this.markers.delete(id); }
    for (const ev of this.events) {
      let m = this.markers.get(ev.id);
      if (!m) {
        m = svgEl('g', { class: `marker type-${ev.type}`, 'data-id': ev.id });
        const label = hhmm(ev.time), w = textW(label) - 2;
        m.innerHTML =
          '<circle r="26" class="pulse"/>' +
          '<circle r="24" class="hit"/>' +
          '<circle r="14" class="dot"/>' + SYMBOLS[ev.type] +
          `<rect x="${-w / 2}" y="19" width="${w}" height="18" rx="9" class="label-bg"/>` +
          `<text y="32" class="label-txt">${label}</text>`;
        this.markers.set(ev.id, m);
      }
      if (m.parentNode !== this.selLayer) this.markerLayer.appendChild(m);   // newest on top
    }
    if (this.selected && !keep.has(this.selected)) this.select(null);
    this.render();
  }

  select(id) {
    this.selected = id || null;
    this.selectedAt = performance.now();
    for (const [mid, m] of this.markers) m.classList.toggle('selected', mid === this.selected);
    const ev = this.events.find((e) => e.id === this.selected) || null;
    for (const m of [...this.selLayer.children]) this.markerLayer.appendChild(m);
    if (ev) this.selLayer.appendChild(this.markers.get(ev.id));
    this.markerLayer.classList.toggle('dimmed', !!ev);
    this.tri.setAttribute('class', ev ? `tri type-${ev.type}` : 'tri');
    this.render();
    this.onSelect(ev);
  }

  focus(id) {
    const ev = this.events.find((e) => e.id === id);
    if (!ev) return;
    this.select(id);
    this.animateTo(this.fitView([...MICS, ev], this.bottomInset() + 30));   // keep clear of the sheet
  }

  render() {
    if (!this.ready) return;
    const v = this.view, now = Date.now();
    this.world.setAttribute('transform', `translate(${v.tx.toFixed(1)} ${v.ty.toFixed(1)}) scale(${v.s.toFixed(5)})`);

    const micPts = MICS.map((m) => this.toScreen(m));
    this.coverage.setAttribute('points', micPts.map((p) => `${p.x.toFixed(1)},${p.y.toFixed(1)}`).join(' '));

    for (const ev of this.events) {
      const m = this.markers.get(ev.id), p = this.toScreen(ev);
      m.setAttribute('transform', `translate(${p.x.toFixed(1)} ${p.y.toFixed(1)})`);
      m.classList.toggle('recent', now - ev.time < 3 * 60e3);
      m.classList.toggle('nolabel', v.s < LABEL_S && ev.id !== this.selected);
    }

    const sel = this.events.find((e) => e.id === this.selected);
    const tri = sel ? triangulate(sel) : null;
    this.renderTriangulation(sel, tri, micPts);

    // microphones (with arrival time while an event is selected)
    this.micEls.forEach((el, i) => {
      const p = micPts[i];
      el.g.setAttribute('transform', `translate(${p.x.toFixed(1)} ${p.y.toFixed(1)})`);
      const hit = tri?.hits.find((x) => x.mic === el.mic);
      const text = hit ? `${el.mic.name} · ${hit.dt === 0 ? 'heard first' : `+${hit.dt.toFixed(2)} s`}` : el.mic.name;
      if (text !== el.text) {
        el.text = text; el.txt.textContent = text;
        const w = textW(text); el.bg.setAttribute('x', -w / 2); el.bg.setAttribute('width', w);
      }
      el.g.classList.toggle('first', !!hit && hit.dt === 0);
    });

    // scale bar: the largest round distance that fits in ~90 px
    const nice = [10, 20, 50, 100, 200, 500, 1000, 2000, 5000];
    const metres = nice.filter((n) => n * v.s <= 90).pop() || nice[0];
    this.scale.querySelector('i').style.width = `${metres * v.s}px`;
    this.scale.querySelector('span').textContent = formatDist(metres);
  }

  /** Rings grow out of each microphone in the order the sound reached them
   *  and meet at the source; then the accuracy circle appears. */
  renderTriangulation(sel, tri, micPts) {
    this.tri.style.display = sel ? '' : 'none';
    if (!sel) return;
    const v = this.view, p = this.toScreen(sel), elapsed = performance.now() - this.selectedAt;
    let animating = false;
    tri.hits.forEach((hit, rank) => {
      const i = MICS.indexOf(hit.mic), part = this.triParts[i], m = micPts[i];
      const k = clamp((elapsed - rank * RING_STAGGER_MS) / RING_MS, 0, 1), e = easeOut(k);
      if (k < 1) animating = true;
      part.ring.setAttribute('cx', m.x); part.ring.setAttribute('cy', m.y);
      part.ring.setAttribute('r', Math.max(0, hit.d * v.s * e));
      part.line.setAttribute('x1', m.x); part.line.setAttribute('y1', m.y);
      part.line.setAttribute('x2', m.x + (p.x - m.x) * e); part.line.setAttribute('y2', m.y + (p.y - m.y) * e);
      const txt = formatDist(hit.d), w = textW(txt);
      const lx = (m.x + p.x) / 2, ly = (m.y + p.y) / 2;
      part.label.textContent = txt;
      part.label.setAttribute('x', lx); part.label.setAttribute('y', ly + 4);
      part.labelBg.setAttribute('x', lx - w / 2); part.labelBg.setAttribute('y', ly - 9);
      part.labelBg.setAttribute('width', w);
      const show = k >= 1 ? '' : 'none';
      part.label.style.display = show; part.labelBg.style.display = show;
    });
    this.fix.setAttribute('cx', p.x); this.fix.setAttribute('cy', p.y);
    this.fix.setAttribute('r', Math.max(18, tri.errorM * v.s));
    this.fix.style.display = animating ? 'none' : '';
    if (animating) this.schedule();
  }
}
