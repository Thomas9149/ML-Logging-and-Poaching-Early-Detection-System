// Forest Watch phone app.
// Subscribes to the ntfy topics that forest/incidents.py publishes to
// (<prefix>-chainsawing, -logging, -gunshot, -poaching), shows each alert
// as a banner with a sound, keeps a log, and puts it on the forest map.

import { ForestMap, mockLocation, bearingText, formatDist, triangulate, MICS } from './map.js';

const TYPES = {
  chainsawing: { label: 'Chainsawing', level: 'HIGH', desc: 'A chainsaw has been running for several seconds',
                 sim: 'Chainsaw running - possible illegal logging' },
  logging:     { label: 'Logging', level: 'URGENT', desc: 'A tree fell after chainsawing',
                 sim: 'Tree fall heard 6 s after chainsawing - tree probably felled, illegal logging likely' },
  gunshot:     { label: 'Gunshot', level: 'HIGH', desc: 'One possible gunshot',
                 sim: 'Possible gunshot detected' },
  poaching:    { label: 'Poaching', level: 'URGENT', desc: 'Two or more gunshots within 10 minutes',
                 sim: '2 gunshots within 60 s - possible poaching' },
};
const ORDER = Object.keys(TYPES);
const DEFAULTS = {
  server: 'https://ntfy.sh', prefix: 'forestwatch-team42',
  subs: Object.fromEntries(ORDER.map((t) => [t, true])),
  sound: true, keepAwake: false,
};
const MAX_EVENTS = 300;
const LIVE_MS = 2 * 60e3;           // older messages count as history (no banner)
// Alerts of the same type closer together than this are one group (e.g. the
// Arduino repeats CHAINSAWING every 10 s while the saw keeps running). Every
// alert is still received and stored; the log, map and banner show the group.
const GROUP_GAP_MS = 30e3;

// ---- storage (wrapped: private mode can throw) ----------------------------
const store = {
  get(k, d) { try { const v = localStorage.getItem(k); return v ? JSON.parse(v) : d; } catch { return d; } },
  set(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch { /* ignore */ } },
};

let settings = { ...DEFAULTS, ...store.get('fw.settings', {}) };
settings.subs = { ...DEFAULTS.subs, ...(settings.subs || {}) };
const qs = new URLSearchParams(location.search);    // the QR code can preset these
if (qs.get('prefix')) settings.prefix = qs.get('prefix');
if (qs.get('server')) settings.server = qs.get('server');
store.set('fw.settings', settings);

let events = store.get('fw.events', []);
let filter = 'all';
let unseen = 0;

// ---- tiny DOM helper -----------------------------------------------------
function h(tag, props, ...kids) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(props || {})) {
    if (k === 'class') e.className = v;
    else if (k.startsWith('on')) e.addEventListener(k.slice(2), v);
    else if (v === true) e.setAttribute(k, '');
    else if (v !== false && v != null) e.setAttribute(k, v);
  }
  for (const c of kids.flat()) if (c != null && c !== false) e.append(c.nodeType ? c : String(c));
  return e;
}
const $ = (s) => document.querySelector(s);
const timeStr = (t) => new Date(t).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
const pct = (p) => (p == null ? '–' : `${Math.round(p * 100)}%`);
function ago(t) {
  const s = Math.max(0, (Date.now() - t) / 1000);
  if (s < 45) return 'just now';
  if (s < 3600) return `${Math.round(s / 60)} min ago`;
  if (s < 86400) return `${Math.round(s / 3600)} h ago`;
  return new Date(t).toLocaleDateString();
}
function dayLabel(t) {
  const d = new Date(t), today = new Date();
  const y = new Date(); y.setDate(today.getDate() - 1);
  if (d.toDateString() === today.toDateString()) return 'Today';
  if (d.toDateString() === y.toDateString()) return 'Yesterday';
  return d.toLocaleDateString([], { weekday: 'short', day: 'numeric', month: 'short' });
}
const where = (ev) => {
  const { nearest } = triangulate(ev);
  return `${formatDist(nearest.d)} ${bearingText(ev, nearest.mic)} of ${nearest.mic.name}`;
};
const sensorsText = (ev) => (ev.sensors?.length ? `sensor ${ev.sensors.join(', ')}` : 'sensor ?');

// ---- events --------------------------------------------------------------
function relatedSpot(type, time) {
  const want = { logging: 'chainsawing', poaching: 'gunshot', gunshot: 'gunshot' }[type];
  if (!want) return null;
  const windowMs = type === 'logging' ? 15 * 60e3 : 10 * 60e3;
  const prev = events.filter((e) => e.type === want && time >= e.time && time - e.time <= windowMs)
                     .sort((a, b) => b.time - a.time)[0];
  return prev ? { x: prev.x, y: prev.y } : null;
}

// The latest earlier alert of this type that this one continues, if any.
function groupPrev(type, time) {
  return events.filter((e) => e.type === type && time >= e.time && time - e.time <= GROUP_GAP_MS)
               .sort((a, b) => b.time - a.time)[0] || null;
}

function makeEvent({ id, type, time, probability, sensors, message, simulated }) {
  const prev = groupPrev(type, time);       // same group -> same spot on the map
  const spot = prev ? { x: prev.x, y: prev.y } : mockLocation(id, relatedSpot(type, time));
  return { id, type, time, probability, sensors, message, simulated: !!simulated, ...spot };
}

// events (sorted by time) -> groups [{ type, items, first, last, count }]
function groupEvents(list) {
  const out = [], open = {};
  for (const ev of list) {
    const g = open[ev.type];
    if (g && ev.time - g.last.time <= GROUP_GAP_MS) {
      g.items.push(ev); g.last = ev; g.count++;
      g.peak = Math.max(g.peak ?? 0, ev.probability ?? 0);
    } else {
      open[ev.type] = { type: ev.type, items: [ev], first: ev, last: ev, count: 1, peak: ev.probability ?? null };
      out.push(open[ev.type]);
    }
  }
  return out.sort((a, b) => a.last.time - b.last.time);
}
const groupOf = (id) => groupEvents(events).find((g) => g.items.some((e) => e.id === id));
const spanText = (g) => (g.count > 1
  ? `${g.count} alerts · ${timeStr(g.first.time)}–${timeStr(g.last.time)}` : timeStr(g.last.time));

function fromNtfy(m) {
  const pre = `${settings.prefix}-`;
  if (!m.topic || !m.topic.startsWith(pre)) return null;
  const type = m.topic.slice(pre.length);
  if (!TYPES[type]) return null;
  const p = /P=(\d+(?:\.\d+)?)%/.exec(m.title || '');
  const s = /sensor\(s\)\s*([\d,\s]+)/.exec(m.message || '');
  return makeEvent({
    id: m.id, type, time: m.time * 1000,
    probability: p ? Number(p[1]) / 100 : null,
    sensors: s ? s[1].split(',').map((x) => x.trim()).filter(Boolean) : [],
    message: (m.message || '').split('\n')[0],
  });
}

function addEvent(ev, live) {
  if (events.some((e) => e.id === ev.id)) return false;
  events.push(ev);
  events.sort((a, b) => a.time - b.time);
  if (events.length > MAX_EVENTS) events = events.slice(-MAX_EVENTS);
  store.set('fw.events', events);
  renderAll();
  if (live) notify(ev);
  return true;
}

// ---- ntfy subscription (WebSocket) ---------------------------------------
let ws = null, retry = 0, retryTimer = null, missed = 0;
const sinceKey = () => `fw.since.${settings.server}|${settings.prefix}`;

function setStatus(state, text) {
  const el = $('#status');
  el.dataset.state = state;
  el.querySelector('.txt').textContent = text;
}

function connect() {
  clearTimeout(retryTimer);
  if (ws) { ws.onclose = null; ws.onerror = null; ws.close(); ws = null; }
  const topics = ORDER.filter((t) => settings.subs[t]).map((t) => `${settings.prefix}-${t}`);
  if (!topics.length) { setStatus('off', 'No subscriptions'); return; }
  let base;
  try { base = new URL(settings.server); } catch { setStatus('error', 'Bad server address'); return; }
  base.protocol = base.protocol === 'http:' ? 'ws:' : 'wss:';
  const since = store.get(sinceKey(), null) || '12h';      // catch up on what we missed
  const url = `${base.origin}${base.pathname.replace(/\/+$/, '')}/${topics.join(',')}/ws?since=${encodeURIComponent(since)}`;
  setStatus('connecting', 'Connecting…');
  missed = 0;
  try { ws = new WebSocket(url); } catch { scheduleRetry(); return; }
  ws.onopen = () => { retry = 0; setStatus('live', `Live · ${settings.prefix}`); };
  ws.onmessage = (e) => {
    let m; try { m = JSON.parse(e.data); } catch { return; }
    if (m.event !== 'message') return;
    store.set(sinceKey(), m.id);
    const ev = fromNtfy(m);
    if (!ev) return;
    const live = Date.now() - ev.time < LIVE_MS;
    if (addEvent(ev, live) && !live) { missed++; showToast(`${missed} earlier alert${missed > 1 ? 's' : ''} loaded`); }
  };
  ws.onerror = () => ws && ws.close();
  ws.onclose = () => { ws = null; scheduleRetry(); };
}
function scheduleRetry() {
  setStatus('error', 'Offline · retrying');
  const delay = Math.min(30000, 1000 * 2 ** retry++);
  retryTimer = setTimeout(connect, delay);
}
document.addEventListener('visibilitychange', () => {
  if (document.visibilityState === 'visible') {
    if (!ws || ws.readyState > 1) connect();
    requestWakeLock();
  }
});

// ---- alerting: banner + sound + vibration --------------------------------
let audio = null;
function unlockAudio() {
  try {
    audio = audio || new (window.AudioContext || window.webkitAudioContext)();
    if (audio.state === 'suspended') audio.resume();
  } catch { /* no audio */ }
  $('#sound-hint').hidden = true;
}
document.addEventListener('pointerdown', unlockAudio, { once: false, passive: true });

function beep(urgent) {
  if (!settings.sound || !audio || audio.state !== 'running') return;
  const n = urgent ? 3 : 2, f = urgent ? 880 : 660, t0 = audio.currentTime + 0.02;
  for (let i = 0; i < n; i++) {
    const o = audio.createOscillator(), g = audio.createGain(), t = t0 + i * 0.22;
    o.type = 'square'; o.frequency.value = f;
    g.gain.setValueAtTime(0.0001, t);
    g.gain.exponentialRampToValueAtTime(0.18, t + 0.01);
    g.gain.exponentialRampToValueAtTime(0.0001, t + 0.16);
    o.connect(g).connect(audio.destination);
    o.start(t); o.stop(t + 0.18);
  }
}

function notify(ev) {
  const T = TYPES[ev.type], urgent = T.level === 'URGENT';
  const g = groupOf(ev.id);
  beep(urgent);
  navigator.vibrate?.(urgent ? [300, 120, 300, 120, 300] : [200, 100, 200]);
  if (currentTab !== 'alerts') { unseen++; renderBadge(); }

  const wrap = $('#banner-wrap');
  wrap.replaceChildren();
  const close = () => { banner.classList.add('out'); setTimeout(() => banner.remove(), 250); };
  const banner = h('div', { class: `banner type-${ev.type} ${urgent ? 'urgent' : ''}`, role: 'alert' },
    h('div', { class: 'b-top' },
      h('span', { class: 'level' }, T.level),
      h('span', { class: 'b-time' }, timeStr(ev.time)),
      h('button', { class: 'b-x', 'aria-label': 'Dismiss', onclick: close }, '×')),
    h('div', { class: 'b-title' }, T.label,
      g && g.count > 1 ? h('span', { class: 'count' }, `×${g.count}`) : null),
    g && g.count > 1 ? h('div', { class: 'b-span' }, `${spanText(g)} – still going`) : null,
    h('div', { class: 'b-msg' }, ev.message || T.desc),
    h('div', { class: 'b-meta' }, `P ${pct(ev.probability)} · ${sensorsText(ev)} · ~${where(ev)}`),
    h('button', { class: 'btn b-map', onclick: () => { close(); showOnMap(ev.id); } }, 'Show on map'));
  wrap.append(banner);
  if (!urgent) setTimeout(close, 15000);
}

let toastTimer;
function showToast(text) {
  const t = $('#toast');
  t.textContent = text; t.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { t.hidden = true; }, 2500);
}

// ---- tabs ----------------------------------------------------------------
let currentTab = 'alerts';
function showTab(name) {
  currentTab = name;
  document.querySelectorAll('.tab').forEach((s) => s.classList.toggle('active', s.id === `tab-${name}`));
  document.querySelectorAll('.tabbtn').forEach((b) => b.classList.toggle('active', b.dataset.tab === name));
  if (name === 'alerts') { unseen = 0; renderBadge(); }
  if (name === 'forest') map.onResize();
}
document.querySelectorAll('.tabbtn').forEach((b) => b.addEventListener('click', () => showTab(b.dataset.tab)));
function renderBadge() {
  const b = $('#badge');
  b.hidden = unseen === 0;
  b.textContent = unseen > 9 ? '9+' : unseen;
}
function showOnMap(id) { showTab('forest'); requestAnimationFrame(() => map.focus(id)); }

// ---- alerts tab ----------------------------------------------------------
function renderHero() {
  const groups = groupEvents(events);
  const lastG = groups[groups.length - 1];
  const last = lastG?.last;
  const hero = $('#hero');
  if (!last) {
    const subs = ORDER.filter((t) => settings.subs[t]).map((t) => TYPES[t].label);
    hero.replaceChildren(h('div', { class: 'hero empty' },
      h('div', { class: 'hero-kicker' }, 'All quiet'),
      h('div', { class: 'hero-title' }, 'No alerts yet'),
      h('div', { class: 'hero-sub' }, subs.length ? `Listening for ${subs.join(', ').toLowerCase()}.` : 'Turn on a subscription in Settings.')));
    return;
  }
  const T = TYPES[last.type];
  hero.replaceChildren(h('button', { class: `hero type-${last.type}`, onclick: () => showOnMap(last.id) },
    h('div', { class: 'hero-kicker' }, h('span', { class: 'level' }, T.level), ` Latest alert · ${ago(last.time)}`),
    h('div', { class: 'hero-title' }, T.label,
      lastG.count > 1 ? h('span', { class: 'count' }, `×${lastG.count}`) : null),
    h('div', { class: 'hero-sub' }, last.message || T.desc),
    h('div', { class: 'hero-meta' },
      h('span', {}, spanText(lastG)), h('span', {}, `P ${pct(last.probability)}`),
      h('span', {}, `~${where(last)}`), h('span', { class: 'go' }, 'Map ›'))));
}

function renderFilters() {
  const groups = groupEvents(events);
  const counts = Object.fromEntries(ORDER.map((t) => [t, groups.filter((g) => g.type === t).length]));
  const chip = (key, label, n) => h('button', {
    class: `chip ${key !== 'all' ? `type-${key}` : ''} ${filter === key ? 'on' : ''}`,
    onclick: () => { filter = key; renderFilters(); renderLog(); },
  }, key !== 'all' ? h('i') : null, label, h('small', {}, n));
  $('#filters').replaceChildren(chip('all', 'All', groups.length), ...ORDER.map((t) => chip(t, TYPES[t].label, counts[t])));
}

function renderLog() {
  const list = groupEvents(events).filter((g) => filter === 'all' || g.type === filter).reverse();
  $('#log-count').textContent = list.length ? `${list.length} event${list.length > 1 ? 's' : ''}` : '';
  if (!list.length) {
    $('#log').replaceChildren(h('p', { class: 'muted empty-log' }, 'Detected events will be listed here with the time they were heard.'));
    return;
  }
  const out = [];
  let day = null, group = null;
  for (const g of list) {
    const ev = g.last;
    const d = dayLabel(ev.time);
    if (d !== day) { day = d; out.push(h('div', { class: 'day' }, d)); group = h('div', { class: 'card list' }); out.push(group); }
    const T = TYPES[ev.type];
    group.append(h('button', { class: `row log-row type-${ev.type}`, onclick: () => showOnMap(ev.id) },
      h('span', { class: 'time' }, timeStr(ev.time)),
      h('i', { class: 'swatch' }),
      h('span', { class: 'what' }, h('b', {}, T.label,
          g.count > 1 ? h('span', { class: 'count' }, `×${g.count}`) : null,
          ev.simulated ? h('em', {}, 'test') : null),
        h('small', {}, g.count > 1
          ? `${T.level} · since ${timeStr(g.first.time)} · peak P ${pct(g.peak)}`
          : `${T.level} · P ${pct(ev.probability)} · ${sensorsText(ev)}`)),
      h('span', { class: 'chev' }, '›')));
  }
  $('#log').replaceChildren(...out);
}

function renderAll() {
  renderHero(); renderFilters(); renderLog();
  map.setEvents(groupEvents(events).map((g) => g.last));
}

// ---- forest tab: bottom sheet for the selected marker --------------------
function renderSheet(ev) {
  const sheet = $('#sheet');
  if (!ev) { sheet.hidden = true; return; }
  const T = TYPES[ev.type];
  const tri = triangulate(ev);
  sheet.replaceChildren(
    h('div', { class: 'grab' }),
    h('div', { class: 'sheet-head' },
      h('span', { class: `level type-${ev.type}` }, T.level),
      h('b', {}, T.label),
      h('button', { class: 'b-x', 'aria-label': 'Close', onclick: () => map.select(null) }, '×')),
    h('div', { class: 'sheet-meta' },
      h('span', {}, (() => { const g = groupOf(ev.id); return g ? spanText(g) : timeStr(ev.time); })()),
      h('span', {}, ago(ev.time)),
      h('span', {}, `P ${pct(ev.probability)}`), h('span', {}, `~${where(ev)} ± ${tri.errorM} m`)),
    h('div', { class: 'arrivals' }, ...tri.hits.map((hit) =>
      h('span', { class: hit.dt === 0 ? 'first' : '' },
        h('b', {}, hit.mic.name), hit.dt === 0 ? 'first' : `+${hit.dt.toFixed(2)} s`))),
    h('details', { class: 'tri-more' },
      h('summary', {}, 'How is this located?', h('em', {}, 'mock')),
      h('p', {},
        `${ev.message || T.desc}. Sound travels about 343 m per second, so the delay at each microphone ` +
        `shows how much further away it is: ` +
        tri.hits.map((x) => `${x.mic.name} ${formatDist(x.d)}`).join(', ') +
        `. Each circle on the map has that distance as its radius, and the circles meet where the sound came from. ` +
        `The positions here are made up; the real version will use the actual arrival times.`)));
  sheet.hidden = false;
}

// ---- settings tab --------------------------------------------------------
function renderSettings() {
  $('#subs').replaceChildren(...ORDER.map((t) => {
    const input = h('input', { type: 'checkbox' });
    input.checked = !!settings.subs[t];
    input.addEventListener('change', () => { settings.subs[t] = input.checked; saveSettings(true); });
    return h('label', { class: `row toggle type-${t}` },
      h('i', { class: 'swatch' }),
      h('span', {}, h('b', {}, TYPES[t].label, h('em', { class: 'lvl' }, TYPES[t].level)), h('small', {}, TYPES[t].desc)),
      input, h('i'));
  }));
  $('#in-prefix').value = settings.prefix;
  $('#in-server').value = settings.server;
  $('#in-sound').checked = settings.sound;
  $('#in-awake').checked = settings.keepAwake;
  $('#topic-list').replaceChildren(...ORDER.flatMap((t, i) => [i ? ', ' : '', h('code', {}, `${settings.prefix}-${t}`)]));
  $('#sim').replaceChildren(...ORDER.map((t) =>
    h('button', { class: `btn sim type-${t}`, onclick: () => simulate(t) }, h('i', { class: 'swatch' }), TYPES[t].label)));
}
function saveSettings(reconnect) {
  store.set('fw.settings', settings);
  renderSettings(); renderHero();
  if (reconnect) connect();
}
$('#in-prefix').addEventListener('change', (e) => {
  const v = e.target.value.trim().replace(/[^\w-]/g, '');
  if (v) settings.prefix = v;
  saveSettings(true);
});
$('#in-server').addEventListener('change', (e) => { settings.server = e.target.value.trim() || DEFAULTS.server; saveSettings(true); });
$('#in-sound').addEventListener('change', (e) => { settings.sound = e.target.checked; saveSettings(false); unlockAudio(); beep(false); });
$('#in-awake').addEventListener('change', (e) => { settings.keepAwake = e.target.checked; saveSettings(false); requestWakeLock(); });
$('#btn-clear').addEventListener('click', () => {
  if (!confirm('Clear the event log on this phone?')) return;
  events = []; store.set('fw.events', events); map.select(null); renderAll();
});

let wakeLock = null;
async function requestWakeLock() {
  try {
    if (settings.keepAwake && 'wakeLock' in navigator && document.visibilityState === 'visible') {
      wakeLock = await navigator.wakeLock.request('screen');
    } else if (!settings.keepAwake && wakeLock) { await wakeLock.release(); wakeLock = null; }
  } catch { /* not supported or refused */ }
}

// ---- simulation (this phone only) ----------------------------------------
function simulate(type) {
  const r = Math.random();
  const p = { chainsawing: 0.8 + r * 0.15, logging: 0.6 + r * 0.38, gunshot: 0.35 + r * 0.6, poaching: 0.6 + r * 0.3 }[type];
  addEvent(makeEvent({ id: `sim-${Date.now()}-${Math.floor(r * 1e6)}`, type, time: Date.now(),
                       probability: p, sensors: ['0'], message: TYPES[type].sim, simulated: true }), true);
}
let storyTimers = [];
$('#btn-story').addEventListener('click', () => {
  storyTimers.forEach(clearTimeout);
  showToast('Demo story: watch the banners');
  storyTimers = [[0, 'chainsawing'], [2500, 'chainsawing'], [5000, 'chainsawing'], [8000, 'logging'],
                 [14000, 'gunshot'], [20000, 'gunshot'], [20400, 'poaching']]
    .map(([ms, t]) => setTimeout(() => simulate(t), ms));
});

// ---- start ---------------------------------------------------------------
const map = new ForestMap($('#map'), {
  onSelect: renderSheet,
  bottomInset: () => ($('#sheet').hidden ? 0 : $('#sheet').offsetHeight + 16),
});
renderSettings();
renderAll();
$('#sound-hint').hidden = false;
if (['#forest', '#settings'].includes(location.hash)) showTab(location.hash.slice(1));
setInterval(renderHero, 30000);
connect();
requestWakeLock();
