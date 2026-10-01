// Modbus Dash - wspólna infrastruktura frontendu.
// Zasada: dynamiczny tekst trafia do DOM wyłącznie jako tekst (h(), mount(), textContent),
// nigdy surowo do innerHTML.

export const $ = (sel, root = document) => root.querySelector(sel);
export const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

/**
 * Budowanie DOM: h('div', {class: 'x', onclick: fn, dataset: {id: 1}}, 'tekst', h('b', null, '!'))
 * Atrybuty: class, style (obiekt lub tekst), dataset, on* (funkcje), html (zaufany HTML - tylko stałe!),
 * pozostałe jako atrybuty (true -> pusty atrybut, false/null -> pominięty).
 */
export function h(tag, attrs, ...children) {
  const el = document.createElement(tag);
  if (attrs) {
    for (const [k, v] of Object.entries(attrs)) {
      if (v == null || v === false) continue;
      if (k === 'class') el.className = v;
      else if (k === 'style' && typeof v === 'object') Object.assign(el.style, v);
      else if (k === 'dataset') Object.assign(el.dataset, v);
      else if (k === 'html') el.innerHTML = v;
      else if (k.startsWith('on') && typeof v === 'function') el.addEventListener(k.slice(2), v);
      else if (k in el && typeof v !== 'string' && k !== 'list') el[k] = v;
      else el.setAttribute(k, v === true ? '' : v);
    }
  }
  append(el, children);
  return el;
}

function append(el, children) {
  for (const c of children.flat(Infinity)) {
    if (c == null || c === false) continue;
    el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return el;
}

/** Zastępuje zawartość elementu. */
export function mount(el, ...children) {
  el.replaceChildren();
  return append(el, children);
}

// ── API ───────────────────────────────────────────────────────

class ApiError extends Error {
  constructor(message, status, data) {
    super(message);
    this.status = status;
    this.data = data || {};
    this.errors = (data && data.errors) || [];
  }
}

async function api(path, { method = 'GET', body, signal } = {}) {
  const opts = { method, signal, headers: { Accept: 'application/json' } };
  if (body !== undefined) {
    opts.headers['Content-Type'] = 'application/json';
    opts.body = JSON.stringify(body);
  }
  let r;
  try {
    r = await fetch(path, opts);
  } catch (e) {
    if (e.name === 'AbortError') throw e;
    throw new ApiError('Brak połączenia z serwerem Modbus Dash', 0);
  }
  let data = null;
  const ct = r.headers.get('Content-Type') || '';
  if (ct.includes('application/json')) {
    try { data = await r.json(); } catch { data = null; }
  }
  if (!r.ok) {
    const msg = (data && data.error) || `Błąd HTTP ${r.status}`;
    throw new ApiError(msg, r.status, data);
  }
  return data;
}
export const get = (p, o) => api(p, o);
export const post = (p, body = {}, o) => api(p, { ...o, method: 'POST', body });
export const put = (p, body, o) => api(p, { ...o, method: 'PUT', body });
export const del = (p, o) => api(p, { ...o, method: 'DELETE' });
export const enc = encodeURIComponent;

// ── powiadomienia ────────────────────────────────────────────

export function toast(msg, type = 'ok', ms) {
  const root = $('#toasts');
  const close = h('button', { 'aria-label': 'Zamknij', onclick: () => t.remove() }, '×');
  const t = h('div', { class: `toast toast-${type}`, role: type === 'err' ? 'alert' : 'status' }, h('span', null, msg), close);
  root.append(t);
  while (root.children.length > 5) root.firstElementChild.remove();
  setTimeout(() => t.remove(), ms ?? (type === 'err' ? 7000 : 3500));
  return t;
}
/** Wyświetla błąd (ApiError lub inny) jako toast; zwraca komunikat. */
export function showError(e, prefix = '') {
  if (e && e.name === 'AbortError') return '';
  const msg = prefix + (e && e.message ? e.message : String(e));
  toast(msg, 'err');
  return msg;
}

// ── odpytywanie bez nakładania się ───────────────────────────

/**
 * Wywołuje async fn co intervalMs; kolejne wywołanie dopiero po zakończeniu poprzedniego.
 * Wstrzymuje się, gdy karta przeglądarki jest ukryta.
 */
export class Poller {
  constructor(fn, intervalMs) {
    this.fn = fn;
    this.interval = intervalMs;
    this.timer = null;
    this.running = false;
    this.busy = false;
    this._vis = () => {
      if (!this.running) return;
      if (document.hidden) this._clear();
      else if (!this.timer && !this.busy) this._schedule(0);
    };
  }
  start(immediate = true) {
    if (this.running) return this;
    this.running = true;
    document.addEventListener('visibilitychange', this._vis);
    this._schedule(immediate ? 0 : this.interval);
    return this;
  }
  stop() {
    this.running = false;
    this._clear();
    document.removeEventListener('visibilitychange', this._vis);
  }
  trigger() { if (this.running && !this.busy) { this._clear(); this._schedule(0); } }
  _clear() { if (this.timer) { clearTimeout(this.timer); this.timer = null; } }
  _schedule(ms) {
    this._clear();
    if (!this.running || document.hidden) return;
    this.timer = setTimeout(() => this._tick(), ms);
  }
  async _tick() {
    this.timer = null;
    if (!this.running) return;
    this.busy = true;
    try { await this.fn(); } catch (e) { console.warn('Poller:', e); }
    this.busy = false;
    this._schedule(this.interval);
  }
}

// ── zadania w tle (/api/jobs) ────────────────────────────────

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

/** Uruchamia zadanie (startPromise -> job) i czeka na koniec; onProgress(job) przy każdej zmianie. */
export async function runJob(startPromise, onProgress, { signal } = {}) {
  let job = await startPromise;
  onProgress && onProgress(job);
  while (job.state === 'running') {
    await sleep(400);
    if (signal && signal.aborted) {
      await del(`/api/jobs/${enc(job.id)}`).catch(() => {});
      throw new DOMException('Anulowano', 'AbortError');
    }
    job = await get(`/api/jobs/${enc(job.id)}`);
    onProgress && onProgress(job);
  }
  if (job.state === 'error') throw new ApiError(job.error || 'Zadanie zakończone błędem', 500, job);
  return job;
}

// ── preferencje (localStorage może być niedostępny) ──────────

export const store = {
  get(key, def) {
    try {
      const v = localStorage.getItem('mdash.' + key);
      return v == null ? def : JSON.parse(v);
    } catch { return def; }
  },
  set(key, val) {
    try { localStorage.setItem('mdash.' + key, JSON.stringify(val)); } catch { /* tryb prywatny */ }
  },
};

// ── okna modalne ─────────────────────────────────────────────

/**
 * modal({title, subtitle, body: Node|[Node], actions: [{label, class, onClick(close)}], wide, onClose,
 *        closeOnBackdrop})
 * Zamyka się Esc, kliknięciem w tło (o ile closeOnBackdrop) i przyciskiem ×; przywraca fokus.
 * Zwraca {close, el}.
 */
export function modal({ title, subtitle, body, actions = [], wide = false, onClose, closeOnBackdrop = true } = {}) {
  const prevFocus = document.activeElement;
  const root = $('#modal-root');
  let closed = false;
  const close = () => {
    if (closed) return;
    closed = true;
    document.removeEventListener('keydown', onKey);
    overlay.remove();
    onClose && onClose();
    if (prevFocus && prevFocus.focus) prevFocus.focus();
  };
  const onKey = (e) => {
    if (e.key === 'Escape') { e.stopPropagation(); close(); }
    if (e.key === 'Tab') trapFocus(e, box);
  };
  const titleId = 'm' + Math.random().toString(36).slice(2);
  const box = h('div', { class: 'modal' + (wide ? ' wide' : ''), role: 'dialog', 'aria-modal': 'true', 'aria-labelledby': titleId, tabindex: '-1' },
    h('div', { class: 'modal-head' },
      h('div', null, h('h3', { id: titleId }, title || ''), subtitle ? h('p', null, subtitle) : null),
      h('button', { class: 'icon-btn', 'aria-label': 'Zamknij', onclick: close }, '×')),
    h('div', { class: 'modal-body' }, body),
    actions.length ? h('div', { class: 'modal-foot' }, actions.map((a) =>
      h('button', { class: 'btn ' + (a.class || 'btn-ghost'), type: 'button', onclick: () => a.onClick ? a.onClick(close) : close() }, a.label))) : null);
  const overlay = h('div', { class: 'modal-overlay', onmousedown: (e) => { if (closeOnBackdrop && e.target === overlay) close(); } }, box);
  root.append(overlay);
  document.addEventListener('keydown', onKey);
  const first = box.querySelector('input,select,textarea,.modal-foot .btn-primary,.modal-foot .btn-danger,.modal-foot .btn');
  (first || box).focus();
  return { close, el: box };
}

function trapFocus(e, box) {
  const f = $$('a[href],button:not([disabled]),input:not([disabled]),select,textarea,[tabindex]:not([tabindex="-1"])', box)
    .filter((x) => x.offsetParent !== null);
  if (!f.length) return;
  const first = f[0], last = f[f.length - 1];
  const a = document.activeElement;
  if (a === box || !box.contains(a)) {
    e.preventDefault();
    (e.shiftKey ? last : first).focus();
    return;
  }
  if (e.shiftKey && a === first) { e.preventDefault(); last.focus(); }
  else if (!e.shiftKey && a === last) { e.preventDefault(); first.focus(); }
}

/** Okno potwierdzenia (text: napis albo węzeł DOM); zwraca Promise<boolean>. */
export function confirmDialog(text, { title = 'Potwierdź', okLabel = 'Usuń', danger = true } = {}) {
  return new Promise((resolve) => {
    let result = false;
    modal({
      title,
      body: text instanceof Node ? text : h('p', { style: { color: 'var(--text2)' } }, text),
      actions: [
        { label: 'Anuluj' },
        { label: okLabel, class: danger ? 'btn-danger' : 'btn-primary', onClick: (close) => { result = true; close(); } },
      ],
      onClose: () => resolve(result),
    });
  });
}

// ── formatowanie ─────────────────────────────────────────────

export function fmt(v, decimals = 2) {
  if (v == null || Number.isNaN(v)) return '-';
  if (typeof v !== 'number') return String(v);
  return v.toLocaleString('pl-PL', { minimumFractionDigits: decimals, maximumFractionDigits: decimals, useGrouping: Math.abs(v) >= 10000 });
}
export const fmtHex = (n) => '0x' + Number(n).toString(16).toUpperCase().padStart(4, '0');
export const fmtTime = (ts) => new Date(ts * 1000).toLocaleTimeString('pl-PL');
export const fmtDateTime = (ts) => new Date(ts * 1000).toLocaleString('pl-PL');
export function fmtAge(sec) {
  if (sec == null) return '-';
  if (sec < 2) return 'teraz';
  if (sec < 60) return `${Math.round(sec)} s temu`;
  if (sec < 3600) return `${Math.round(sec / 60)} min temu`;
  return `${Math.round(sec / 3600)} h temu`;
}

/**
 * Adres rejestru z tekstu: "123", "0x7B", notacja 3xxxx/4xxxx (np. 30001 -> 0 dla Input, 40001 -> 0 dla Holding).
 * Zwraca {address, note} albo null.
 */
export function parseAddress(text, func) {
  const s = String(text ?? '').trim().toLowerCase();
  if (!s) return null;
  let n;
  if (/^0x[0-9a-f]+$/.test(s)) n = parseInt(s, 16);
  else if (/^\d+$/.test(s)) n = parseInt(s, 10);
  else return null;
  if (s.length === 5 && !s.startsWith('0x')) {
    if (func === 'input' && n >= 30001 && n <= 39999) return { address: n - 30001, note: `${n} → adres ${n - 30001} (notacja 3xxxx)` };
    if (func === 'holding' && n >= 40001 && n <= 49999) return { address: n - 40001, note: `${n} → adres ${n - 40001} (notacja 4xxxx)` };
  }
  if (n > 65535) return null;
  return { address: n, note: null };
}

// ── grupy, kolory, stany ─────────────────────────────────────

export const GROUPS = {
  voltage: { label: 'Napięcia fazowe', color: '#22c55e', cols: 'grid-3' },
  line_volt: { label: 'Napięcia międzyfazowe', color: '#22c55e', cols: 'grid-3' },
  current: { label: 'Prądy', color: '#eab308', cols: 'grid-3' },
  power: { label: 'Moce', color: '#f97316', cols: 'grid-3' },
  total: { label: 'Wartości sumaryczne', color: '#f97316', cols: 'grid-4' },
  pf: { label: 'Współczynnik mocy', color: '#a855f7', cols: 'grid-4' },
  system: { label: 'System', color: '#06b6d4', cols: 'grid-4' },
  energy: { label: 'Energia', color: '#3b82f6', cols: 'grid-3' },
  thd: { label: 'THD', color: '#ef4444', cols: 'grid-3' },
  other: { label: 'Inne', color: '#94a3b8', cols: 'grid-4' },
};
export const GROUP_ORDER = ['voltage', 'line_volt', 'current', 'power', 'total', 'pf', 'system', 'energy', 'thd', 'other'];
const PHASE_COLORS = { l1: '#ef4444', l2: '#eab308', l3: '#3b82f6' };

/** Kolor wartości: faza z klucza (…_l1) lub etykiety (L1), inaczej kolor grupy. */
export function colorFor(key, label, group) {
  const k = String(key).toLowerCase();
  const m = k.match(/_(l[123])(?:$|_)/) || k.match(/_(l[123])[123]$/);
  if (m) return PHASE_COLORS[m[1]];
  const lm = String(label || '').match(/\bL([123])\b/);
  if (lm) return PHASE_COLORS['l' + lm[1]];
  return (GROUPS[group] || GROUPS.other).color;
}

export const STATES = {
  ok: ['OK', 'badge-ok'],
  error: ['Błąd', 'badge-err'],
  stale: ['Nieaktualne', 'badge-warn'],
  waiting: ['Oczekiwanie', 'badge-info'],
  disabled: ['Wyłączone', 'badge-muted'],
  no_preset: ['Brak presetu', 'badge-warn'],
};
export function stateBadge(state) {
  const [label, cls] = STATES[state] || [state || '?', 'badge-muted'];
  return h('span', { class: `badge ${cls}` }, label);
}

export const KINDS = {
  tcp: 'Modbus TCP',
  rtu_over_tcp: 'RTU over TCP (bramka)',
  udp: 'Modbus UDP',
  rtu: 'RS-485 RTU',
  ascii: 'RS-485 ASCII',
};
export const FUNCTIONS = {
  input: 'Input Registers (FC04)',
  holding: 'Holding Registers (FC03)',
  coil: 'Coils (FC01)',
  discrete: 'Discrete Inputs (FC02)',
};

// ── pobieranie plików ────────────────────────────────────────

export function download(filename, text, mime = 'text/plain') {
  const url = URL.createObjectURL(new Blob([text], { type: mime + ';charset=utf-8' }));
  const a = h('a', { href: url, download: filename });
  document.body.append(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

/** Pole CSV (separator ";"): cudzysłowy, gdy trzeba; ochrona przed formułami arkusza. */
export function csvCell(v) {
  if (v == null) return '';
  let s = String(v);
  if (/^[=+\-@]/.test(s) && Number.isNaN(Number(s.replace(',', '.')))) s = "'" + s;
  return /[";\n\r]/.test(s) ? '"' + s.replace(/"/g, '""') + '"' : s;
}

// ── formularze ───────────────────────────────────────────────

/** Pole formularza z etykietą: field('Host', input, 'podpowiedź'). */
export function field(label, control, hint, attrs = {}) {
  if (!control.id) control.id = 'f' + Math.random().toString(36).slice(2);
  return h('div', { class: 'field', ...attrs },
    h('label', { for: control.id }, label), control, hint ? h('span', { class: 'hint' }, hint) : null);
}

/** <select> z opcjami [[value, label], ...] lub {value: label}; grupy: [{group: 'Nazwa', options: [...]}]. */
export function select(options, value, attrs = {}) {
  const el = h('select', attrs);
  const add = (parent, opts) => {
    const list = Array.isArray(opts) ? opts : Object.entries(opts);
    for (const o of list) {
      if (o && o.group) {
        const g = h('optgroup', { label: o.group });
        add(g, o.options);
        parent.append(g);
      } else {
        const [v, l] = Array.isArray(o) ? o : [o, o];
        parent.append(h('option', { value: v }, l));
      }
    }
  };
  add(el, options);
  if (value != null) el.value = String(value);
  return el;
}

/** Oznacza pole jako błędne (aria-invalid) albo zdejmuje oznaczenie. */
export function markInvalid(el, invalid) {
  if (invalid) el.setAttribute('aria-invalid', 'true');
  else el.removeAttribute('aria-invalid');
}

/** Polska odmiana liczebnika: plural(n, 'rejestr', 'rejestry', 'rejestrów'). */
export function plural(n, one, few, many) {
  const n10 = n % 10, n100 = n % 100;
  if (n === 1) return one;
  if (n10 >= 2 && n10 <= 4 && (n100 < 12 || n100 > 14)) return few;
  return many;
}

/** Losowy identyfikator elementu (np. dla par label/for). */
export const uid = (prefix = 'f') => prefix + Math.random().toString(36).slice(2, 10);

/** Identyfikator z nazwy: małe litery bez polskich znaków, cyfry, '_' i '-', maks. 32 znaki. */
export function slugify(text) {
  const pl = { ą: 'a', ć: 'c', ę: 'e', ł: 'l', ń: 'n', ó: 'o', ś: 's', ź: 'z', ż: 'z' };
  return String(text || '').toLowerCase()
    .replace(/[ąćęłńóśźż]/g, (c) => pl[c])
    .normalize('NFD').replace(/\p{Mn}/gu, '')
    .replace(/[^a-z0-9_-]+/g, '-').replace(/-{2,}/g, '-')
    .replace(/^[-_]+/, '').slice(0, 32).replace(/[-_]+$/, '');
}

/** Wolny identyfikator: base (albo fallback), a gdy zajęty - base-2, base-3... (maks. 32 znaki). */
export function uniqueId(base, taken, fallback) {
  const id = base || fallback;
  if (!taken.has(id)) return id;
  for (let i = 2; i < 1000; i++) {
    const suffix = '-' + i;
    const cand = id.slice(0, 32 - suffix.length).replace(/[-_]+$/, '') + suffix;
    if (!taken.has(cand)) return cand;
  }
  return id;
}

/** Przycisk w stanie "zajęty" na czas fn(); drugie kliknięcie jest ignorowane. */
export async function busy(btn, fn) {
  if (btn.dataset.busy) return undefined;
  btn.dataset.busy = '1';
  btn.disabled = true;
  btn.setAttribute('aria-busy', 'true');
  const sp = h('span', { class: 'spinner', 'aria-hidden': 'true' });
  btn.prepend(sp);
  try {
    return await fn();
  } finally {
    sp.remove();
    delete btn.dataset.busy;
    btn.disabled = false;
    btn.removeAttribute('aria-busy');
  }
}

/** Komunikat błędu pod polem formularza (z aria-errormessage); msg pusty - usuwa komunikat. */
export function fieldError(control, msg) {
  markInvalid(control, !!msg);
  const f = control.closest('.field');
  if (!f) return;
  let e = f.querySelector('.field-err');
  if (!msg) {
    if (e) e.remove();
    control.removeAttribute('aria-errormessage');
    return;
  }
  if (!e) {
    e = h('span', { class: 'field-err', id: control.id + '-err' });
    f.append(e);
  }
  e.textContent = msg;
  control.setAttribute('aria-errormessage', e.id);
}

/** Liczba całkowita bez znaku z pola tekstowego albo null. */
export function parseIntStrict(s) {
  s = String(s ?? '').trim();
  return /^\d+$/.test(s) ? parseInt(s, 10) : null;
}

/** Pusty stan z opcjonalnymi przyciskami [{label, href|onClick, class}]. */
export function emptyState(text, buttons = []) {
  return h('div', { class: 'empty' }, h('p', null, text),
    buttons.length ? h('div', { class: 'actions' }, buttons.map((b) => b.href
      ? h('a', { class: 'btn ' + (b.class || 'btn-primary'), href: b.href }, b.label)
      : h('button', { class: 'btn ' + (b.class || 'btn-primary'), onclick: b.onClick }, b.label))) : null);
}

/** Nagłówek strony widoku z akcjami. */
export function pageHeader(title, ...actions) {
  return h('div', { class: 'page-header' }, h('h2', null, title), h('div', { class: 'actions' }, actions));
}
