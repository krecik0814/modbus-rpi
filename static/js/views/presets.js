// Widok: Presety - lista (moje + biblioteka), podgląd mapy rejestrów, edytor JSON z walidacją na żywo.
// Dynamiczny tekst wyłącznie przez h()/textContent (nazwy, etykiety i źródła pochodzą z plików użytkownika).

import {
  h, mount as fill, get, post, put, del, enc, toast, showError, modal, confirmDialog, download,
  fmtHex, field, markInvalid, emptyState, pageHeader, store, GROUPS, GROUP_ORDER,
} from '../core.js';

const VALIDATE_MS = 400;
const MAX_IMPORT = 2 * 1024 * 1024;
const DEFAULT_MAX_BLOCK = 64;
const DEFAULT_MAX_GAP = 10;

const TEMPLATE = {
  name: 'Nowy preset',
  manufacturer: '',
  model: '',
  description: '',
  phases: 3,
  register_type: 'input',
  byte_order: 'ABCD',
  data_type: 'float32',
  probe: 'voltage_l1',
  registers: {
    voltage_l1: { address: 0, label: 'Napięcie L1', unit: 'V', group: 'voltage', decimals: 1 },
  },
};

// typ -> liczba rejestrów (jak modbus_dash/codec.py)
const DATA_TYPES = { int16: 1, uint16: 1, int32: 2, uint32: 2, float32: 2, int64: 4, uint64: 4, float64: 4 };
const BYTE_ORDERS = ['ABCD', 'CDAB', 'BADC', 'DCBA'];
const ORDER_ALIASES = {
  abcd: 'ABCD', big_endian: 'ABCD', big: 'ABCD', be: 'ABCD',
  cdab: 'CDAB', word_swap: 'CDAB', wordswap: 'CDAB', ws: 'CDAB', lsw_first: 'CDAB', swapped: 'CDAB',
  badc: 'BADC', byte_swap: 'BADC', byteswap: 'BADC', bs: 'BADC',
  dcba: 'DCBA', little_endian: 'DCBA', little: 'DCBA', le: 'DCBA',
};
const TYPE_ALIASES = {
  float: 'float32', real: 'float32', f32: 'float32', ieee754: 'float32', double: 'float64', f64: 'float64',
  int: 'int16', short: 'int16', i16: 'int16', s16: 'int16', uint: 'uint16', word: 'uint16', u16: 'uint16',
  dint: 'int32', long: 'int32', i32: 'int32', s32: 'int32', udint: 'uint32', dword: 'uint32', ulong: 'uint32',
  u32: 'uint32', lint: 'int64', i64: 'int64', s64: 'int64', ulint: 'uint64', u64: 'uint64',
};
const FUNC_ALIASES = {
  input: 'input', ir: 'input', fc4: 'input', fc04: 'input', 4: 'input', input_registers: 'input', input_register: 'input',
  holding: 'holding', hr: 'holding', fc3: 'holding', fc03: 'holding', 3: 'holding',
  holding_registers: 'holding', holding_register: 'holding',
};
const FUNC_LABEL = { input: 'Input (FC04)', holding: 'Holding (FC03)' };
const ORDER_DESC = {
  ABCD: 'big-endian', CDAB: 'zamienione słowa (word swap)', BADC: 'zamienione bajty (byte swap)', DCBA: 'little-endian',
};

// ── pomocnicze ───────────────────────────────────────────────

const own = (obj, k) => (Object.prototype.hasOwnProperty.call(obj, k) ? obj[k] : undefined);
const isObj = (v) => v != null && typeof v === 'object' && !Array.isArray(v);

function plural(n, one, few, many) {
  const n10 = n % 10, n100 = n % 100;
  if (n === 1) return one;
  if (n10 >= 2 && n10 <= 4 && (n100 < 12 || n100 > 14)) return few;
  return many;
}
const regWord = (n) => `${n} ${plural(n, 'rejestr', 'rejestry', 'rejestrów')}`;
const errWord = (n) => `${n} ${plural(n, 'błąd', 'błędy', 'błędów')}`;
const phasesText = (p) => (p === 1 ? '1 faza' : p === 2 || p === 3 ? `${p} fazy` : '');

/** Tekst do wyszukiwania: małe litery, bez polskich znaków. */
const fold = (s) => String(s ?? '').toLowerCase().replace(/ł/g, 'l').normalize('NFD').replace(/\p{Mn}/gu, '');

/** Kopia bez pól technicznych (_id, _builtin, _save_as...). */
function stripMeta(obj) {
  if (!isObj(obj)) return obj;
  return Object.fromEntries(Object.entries(obj).filter(([k]) => !k.startsWith('_')));
}

/** Zwarty zapis wartości w jednej linii: {"a": 1, "b": [1, 2]}. */
function inline(v) {
  if (Array.isArray(v)) return '[' + v.map(inline).join(', ') + ']';
  if (isObj(v)) {
    const e = Object.entries(v);
    return e.length ? '{' + e.map(([k, x]) => JSON.stringify(k) + ': ' + inline(x)).join(', ') + '}' : '{}';
  }
  return JSON.stringify(v);
}

const TOP_ORDER = ['name', 'manufacturer', 'model', 'description', 'source', 'phases', 'register_type', 'byte_order',
  'data_type', 'address_offset', 'serial', 'read', 'probe', 'registers'];
const REG_ORDER = ['address', 'label', 'unit', 'group', 'decimals', 'type', 'data_type', 'byte_order', 'register_type',
  'scale', 'offset', 'invalid'];

/** Kopia obiektu z kluczami w podanej kolejności (pozostałe na końcu, bez zmian). */
function orderKeys(obj, order) {
  const entries = order.filter((k) => own(obj, k) !== undefined).map((k) => [k, obj[k]]);
  for (const e of Object.entries(obj)) if (!order.includes(e[0])) entries.push(e);
  return Object.fromEntries(entries);
}

/** Postać kanoniczna: pola w kolejności schematu, rejestry wg adresu (serwer nie zachowuje kolejności kluczy). */
function canonical(p) {
  if (!isObj(p)) return p;
  const out = orderKeys(p, TOP_ORDER);
  if (isObj(out.serial)) out.serial = orderKeys(out.serial, ['baudrate', 'bytesize', 'parity', 'stopbits']);
  if (isObj(out.read)) out.read = orderKeys(out.read, ['max_block', 'max_gap']);
  if (isObj(out.registers)) {
    const addr = (v) => (isObj(v) ? parseAddr(v.address) : null) ?? Infinity;
    const regs = Object.entries(out.registers).map(([k, v]) => [k, isObj(v) ? orderKeys(v, REG_ORDER) : v]);
    regs.sort((a, b) => (addr(a[1]) === addr(b[1]) ? 0 : addr(a[1]) < addr(b[1]) ? -1 : 1));
    out.registers = Object.fromEntries(regs);
  }
  return out;
}

/** JSON presetu: pola po jednym w linii, każdy rejestr w jednej linii (jak pliki biblioteki). */
function formatPreset(obj) {
  if (!isObj(obj)) return JSON.stringify(obj, null, 2);
  const entries = Object.entries(canonical(obj));
  if (!entries.length) return '{}';
  const lines = ['{'];
  entries.forEach(([k, v], i) => {
    const comma = i < entries.length - 1 ? ',' : '';
    if (k === 'registers' && isObj(v) && Object.keys(v).length) {
      const regs = Object.entries(v);
      lines.push('  "registers": {');
      regs.forEach(([rk, rv], j) => lines.push(`    ${JSON.stringify(rk)}: ${inline(rv)}${j < regs.length - 1 ? ',' : ''}`));
      lines.push('  }' + comma);
    } else {
      lines.push(`  ${JSON.stringify(k)}: ${inline(v)}${comma}`);
    }
  });
  lines.push('}');
  return lines.join('\n');
}

function lineColAt(text, pos) {
  const before = text.slice(0, pos);
  const line = before.split('\n').length;
  return { line, col: pos - before.lastIndexOf('\n') };
}
function posAt(text, line, col) {
  let pos = 0;
  for (let i = 1; i < line; i++) {
    const nl = text.indexOf('\n', pos);
    if (nl < 0) break;
    pos = nl + 1;
  }
  return Math.min(text.length, pos + Math.max(0, col - 1));
}

const SYNTAX_PL = [
  [/unexpected end of (json )?(input|data)|end of data/i, 'nieoczekiwany koniec danych - brakuje nawiasu zamykającego?'],
  [/expected ',' or '}' after property value/i, "brakuje przecinka albo '}' po wartości"],
  [/expected ',' or ']' after array element/i, "brakuje przecinka albo ']' po elemencie tablicy"],
  [/expected double-quoted property name|expected property name or '}'/i, 'oczekiwano nazwy pola w cudzysłowie (zbędny przecinek na końcu?)'],
  [/unexpected non-whitespace character after json/i, 'nadmiarowe znaki po zakończeniu obiektu JSON'],
  [/bad control character|control character/i, 'niedozwolony znak sterujący w tekście (np. tabulator lub nowa linia w cudzysłowie)'],
  [/unterminated string/i, 'niezamknięty cudzysłów'],
  [/bad escaped character|bad escape/i, 'nieprawidłowa sekwencja ze znakiem \\'],
  [/no number after minus sign|missing digits/i, 'nieprawidłowa liczba'],
];

/** JSON.parse z czytelnym błędem {message, line, col, pos}. */
function parseText(text) {
  if (!String(text).trim()) return { ok: false, error: { message: 'edytor jest pusty', line: 1, col: 1, pos: 0 } };
  try {
    return { ok: true, value: JSON.parse(text) };
  } catch (e) {
    const raw = String((e && e.message) || e);
    let line, col, pos, m;
    if ((m = raw.match(/line (\d+) column (\d+)/i))) {
      line = +m[1]; col = +m[2]; pos = posAt(text, line, col);
    } else if ((m = raw.match(/position (\d+)/i))) {
      pos = Math.min(+m[1], text.length); ({ line, col } = lineColAt(text, pos));
    } else if (/end of (json )?input/i.test(raw)) {
      pos = text.length; ({ line, col } = lineColAt(text, pos));
    }
    let message = raw.replace(/^JSON\.parse: /, '');
    const tr = SYNTAX_PL.find(([re]) => re.test(raw));
    if (tr) message = tr[1];
    else if ((m = raw.match(/unexpected (token|character) '?(.)'?/i))) message = `nieoczekiwany znak "${m[2]}"`;
    return { ok: false, error: { message, line, col, pos } };
  }
}

function parseAddr(v) {
  if (typeof v === 'number' && Number.isInteger(v)) return v;
  if (typeof v === 'string') {
    const s = v.trim().toLowerCase();
    if (/^0x[0-9a-f]+$/.test(s)) return parseInt(s, 16);
    if (/^[+-]?\d+$/.test(s)) return parseInt(s, 10);
  }
  return null;
}
function normOrder(v) {
  if (v == null) return null;
  const k = String(v).trim();
  return BYTE_ORDERS.includes(k.toUpperCase()) ? k.toUpperCase() : own(ORDER_ALIASES, k.toLowerCase()) || null;
}
function normType(v) {
  if (v == null) return null;
  const k = String(v).trim().toLowerCase();
  return own(DATA_TYPES, k) ? k : own(TYPE_ALIASES, k) || null;
}
const normFunc = (v) => (v == null ? null : own(FUNC_ALIASES, String(v).trim().toLowerCase()) || null);

const num = (v) => (typeof v === 'number'
  ? v.toLocaleString('pl-PL', { maximumFractionDigits: 10, useGrouping: false }) : String(v));

function serialText(s) {
  if (!isObj(s) || s.baudrate == null) return '';
  const parity = String(s.parity ?? 'N').trim().charAt(0).toUpperCase() || 'N';
  return `${s.baudrate} ${s.bytesize ?? 8}${parity}${s.stopbits ?? 1}`;
}

/** Tekst z linkami http(s) jako węzły DOM (rel=noopener, nowa karta). */
function linkify(text) {
  const out = [];
  const s = String(text ?? '');
  const re = /https?:\/\/[^\s<>"'`]+/gi;
  let last = 0, m;
  while ((m = re.exec(s))) {
    let url = m[0];
    // końcowa interpunkcja nie należy do adresu; ")" tylko gdy niesparowany
    while (/[.,;:!?\]}'"]$/.test(url) || (url.endsWith(')') && (url.split('(').length < url.split(')').length))) {
      url = url.slice(0, -1);
    }
    let href = null;
    try {
      const u = new URL(url);
      if (u.protocol === 'http:' || u.protocol === 'https:') href = u.href;
    } catch { href = null; }
    out.push(s.slice(last, m.index));
    out.push(href ? h('a', { href, target: '_blank', rel: 'noopener noreferrer' }, url) : url);
    last = m.index + url.length;
    re.lastIndex = last;
  }
  out.push(s.slice(last));
  return out.filter((x) => x !== '');
}

const safeFileName = (s) => String(s || 'preset').replace(/[\\/:*?"<>|\u0000-\u001f]+/g, '_').trim().slice(0, 80) || 'preset';

/** Wiersze tabeli rejestrów (wartości domyślne z poziomu presetu, adresy z address_offset). */
function registerRows(data) {
  const regs = isObj(data.registers) ? data.registers : {};
  const offset = Number.isInteger(data.address_offset) ? data.address_offset : 0;
  const rows = [];
  for (const [key, spec0] of Object.entries(regs)) {
    const spec = isObj(spec0) ? spec0 : {};
    const fileAddr = parseAddr(spec.address);
    const address = fileAddr == null ? null : fileAddr + offset;
    const pick = (local, def, norm, fallback) => {
      if (local != null) {
        const n = norm(local);
        return { text: n || String(local), inherited: false, bad: !n };
      }
      const n = def != null ? norm(def) : fallback;
      return { text: n || String(def), inherited: true, bad: !n };
    };
    const type = pick(spec.type ?? spec.data_type, data.data_type, normType, 'float32');
    const func = pick(spec.register_type, data.register_type, normFunc, 'input');
    if (!func.bad) func.text = FUNC_LABEL[func.text];
    rows.push({
      key,
      label: typeof spec.label === 'string' ? spec.label : key,
      address, fileAddr,
      func,
      type,
      order: pick(spec.byte_order, data.byte_order, normOrder, 'ABCD'),
      scale: spec.scale ?? 1,
      offset: spec.offset ?? 0,
      unit: typeof spec.unit === 'string' ? spec.unit : '',
      group: typeof spec.group === 'string' && spec.group ? spec.group : 'other',
    });
  }
  rows.sort((a, b) => (a.address ?? Infinity) - (b.address ?? Infinity)
    || a.func.text.localeCompare(b.func.text) || a.key.localeCompare(b.key));
  return rows;
}

function groupBadge(group) {
  const g = own(GROUPS, group);
  return h('span', { class: 'guess g-' + (g ? group : 'other'), title: group }, g ? g.label : group);
}

// Niezapisana praca z edytora, gdy widok zamknięto bez pytania (np. przycisk Wstecz przeglądarki).
let stash = null;

// ── widok ────────────────────────────────────────────────────

export function mount(root, ctx) {
  const S = {
    list: null,          // podsumowania z GET /api/presets
    listError: null,
    q: '',
    // null | {kind: 'loading'|'error', id} | {kind: 'preset', id, builtin, raw} | {kind: 'draft', origin, label}
    sel: null,
    tab: store.get('presets.tab', 'preview') === 'editor' ? 'editor' : 'preview',
    text: '',            // treść edytora
    baseline: '',        // treść zapisana (do wykrywania zmian)
    alwaysDirty: false,  // szkic ze skanera / importu - niezapisany od początku
    parsed: null,        // ostatni poprawnie sparsowany obiekt z edytora
    val: { state: 'idle' },
  };
  const ac = new AbortController();
  let destroyed = false;
  let detailAc = null;
  let valAc = null;
  let valSeq = 0;
  let valTimer = null;
  let opBusy = false;
  let ed = null;          // elementy szczegółów bieżącego presetu
  let allowNav = false;
  let navDialog = false;
  const modals = new Set();
  const narrow = window.matchMedia('(max-width: 900px)');

  // ── szkielet ──────────────────────────────────────────────
  const fileIn = h('input', { type: 'file', accept: '.json,application/json', hidden: true, tabindex: '-1', 'aria-hidden': 'true' });
  fileIn.addEventListener('change', onFile);
  const newBtn = h('button', { class: 'btn btn-primary', type: 'button', onclick: () => newDraft() }, '+ Nowy preset');
  const importBtn = h('button', { class: 'btn btn-ghost', type: 'button', onclick: pickFile }, 'Importuj JSON');
  const searchIn = h('input', {
    type: 'search', placeholder: 'nazwa, producent, model...', autocomplete: 'off', spellcheck: false,
    'aria-controls': 'pr-lists',
  });
  searchIn.addEventListener('input', () => { S.q = searchIn.value; renderList(); });
  const listBox = h('div', { class: 'pr-lists', id: 'pr-lists' });
  const detail = h('section', { class: 'pr-detail', 'aria-label': 'Szczegóły presetu' });

  const restoreBox = h('div', { class: 'pr-restore' });
  fill(root, h('div', { class: 'v-presets' },
    pageHeader('Presety', importBtn, newBtn),
    h('div', { class: 'page-content' },
      restoreBox,
      h('div', { class: 'split' },
        h('div', { class: 'pr-side card' }, field('Szukaj presetu', searchIn), listBox),
        detail),
      helpCard()),
    fileIn));

  // ── stan edytora ──────────────────────────────────────────
  const isEditable = () => !!S.sel && (S.sel.kind === 'draft' || (S.sel.kind === 'preset' && !S.sel.builtin));
  const isDirty = () => isEditable() && (S.alwaysDirty || S.text !== S.baseline);
  const summaryOf = (id) => (S.list || []).find((p) => p.id === id) || null;

  /** Dane presetu do nagłówka i podglądu. */
  function currentData() {
    if (!S.sel) return null;
    if (S.sel.kind === 'preset' && S.sel.builtin) return S.sel.raw;
    return S.parsed;
  }
  function currentName() {
    const d = currentData();
    if (d && typeof d.name === 'string' && d.name.trim()) return d.name.trim();
    if (S.sel && S.sel.id) return S.sel.id;
    return 'Nowy preset';
  }

  async function confirmDiscard(text) {
    if (!isDirty()) return true;
    const ok = await confirmDialog(text || 'Edytor zawiera niezapisane zmiany. Odrzucić je?',
      { title: 'Niezapisane zmiany', okLabel: 'Odrzuć zmiany' });
    return ok && !destroyed;
  }

  function setUrl(params) {
    const q = new URLSearchParams(params).toString();
    const hash = '#presets' + (q ? '?' + q : '');
    if (location.hash !== hash) history.replaceState(history.state, '', hash);
  }

  function resetEditor() {
    clearTimeout(valTimer);
    valTimer = null;
    if (valAc) { valAc.abort(); valAc = null; }
    valSeq++;
    S.text = S.baseline = '';
    S.alwaysDirty = false;
    S.parsed = null;
    S.val = { state: 'idle' };
  }

  function loadEditor(text, { baseline = text, alwaysDirty = false } = {}) {
    S.text = text;
    S.baseline = baseline;
    S.alwaysDirty = alwaysDirty;
    const p = parseText(text);
    S.parsed = p.ok && isObj(p.value) ? p.value : null;
  }

  // ── lista ─────────────────────────────────────────────────
  async function loadList() {
    try {
      const list = await get('/api/presets', { signal: ac.signal });
      if (destroyed) return;
      S.list = Array.isArray(list) ? list : [];
      S.listError = null;
    } catch (e) {
      if (e.name === 'AbortError' || destroyed) return;
      if (S.list) showError(e, 'Lista presetów: ');
      else S.listError = e.message;
    }
    renderList();
  }

  function renderList() {
    if (S.list == null) {
      fill(listBox, S.listError
        ? h('div', { class: 'notice notice-err', role: 'alert' },
          h('p', null, 'Nie udało się wczytać presetów: ', S.listError),
          h('button', { type: 'button', class: 'btn btn-ghost btn-sm pr-retry', onclick: () => { S.listError = null; renderList(); loadList(); } }, 'Spróbuj ponownie'))
        : h('div', { class: 'status-line' }, h('span', { class: 'spinner' }), 'Wczytywanie presetów...'));
      return;
    }
    const words = fold(S.q).split(/\s+/).filter(Boolean);
    const match = (p) => {
      if (!words.length) return true;
      const hay = fold([p.name, p.manufacturer, p.model, p.id].join(' '));
      return words.every((w) => hay.includes(w));
    };
    const mine = S.list.filter((p) => !p.builtin);
    const lib = S.list.filter((p) => p.builtin);
    const fm = mine.filter(match);
    const fl = lib.filter(match);
    const count = (shown, all) => (words.length ? `${shown} z ${all}` : String(all));
    const draft = S.sel && S.sel.kind === 'draft' ? draftItem() : null;
    fill(listBox,
      h('section', { class: 'pr-sec', 'aria-label': 'Moje presety' },
        h('h3', { class: 'pr-sec-title' }, `Moje presety (${count(fm.length, mine.length)})`),
        draft,
        fm.length ? fm.map(itemEl)
          : draft ? null
            : h('p', { class: 'pr-none' }, words.length ? 'Brak pasujących presetów.'
              : 'Nie masz jeszcze własnych presetów. Skopiuj preset z biblioteki albo utwórz nowy.')),
      h('section', { class: 'pr-sec', 'aria-label': 'Biblioteka' },
        h('h3', { class: 'pr-sec-title' }, `Biblioteka (${count(fl.length, lib.length)})`),
        fl.length ? fl.map(itemEl)
          : h('p', { class: 'pr-none' }, words.length ? 'Brak pasujących presetów.' : 'Biblioteka jest pusta.')));
  }

  function itemEl(p) {
    const selected = !!S.sel && S.sel.kind !== 'draft' && S.sel.id === p.id;
    const mm = [p.manufacturer, p.model].filter((x) => typeof x === 'string' && x).join(' ');
    const meta = [regWord(p.register_count || 0), phasesText(p.phases)].filter(Boolean).join(' · ');
    const badges = [
      p.valid === false ? h('span', { class: 'badge badge-err', title: (p.errors || []).join('\n') }, 'Błędny') : null,
      p.overrides_builtin ? h('span', { class: 'badge badge-info' }, 'Nadpisuje wbudowany') : null,
    ].filter(Boolean);
    return h('button', {
      type: 'button', class: 'list-item pr-item' + (selected ? ' selected' : ''),
      'aria-current': selected ? 'true' : null, dataset: { id: p.id }, onclick: () => requestSelect(p.id),
    },
    h('span', { class: 'pr-item-main' },
      h('span', { class: 'pr-name' }, String(p.name || p.id)),
      mm ? h('span', { class: 'pr-mm' }, mm) : null,
      h('span', { class: 'pr-meta' }, meta)),
    badges.length ? h('span', { class: 'pr-badges' }, badges) : null);
  }

  function draftItem() {
    const regs = S.parsed && isObj(S.parsed.registers) ? Object.keys(S.parsed.registers).length : null;
    return h('button', {
      type: 'button', class: 'list-item pr-item selected', 'aria-current': 'true', onclick: scrollToDetail,
    },
    h('span', { class: 'pr-item-main' },
      h('span', { class: 'pr-name' }, currentName()),
      h('span', { class: 'pr-meta' }, regs == null ? 'szkic' : `szkic · ${regWord(regs)}`)),
    h('span', { class: 'pr-badges' }, h('span', { class: 'badge badge-warn' }, 'Niezapisany')));
  }

  function scrollToDetail() {
    if (narrow.matches) detail.scrollIntoView({ behavior: 'smooth', block: 'start' });
  }

  // ── wybór presetu ─────────────────────────────────────────
  async function requestSelect(id) {
    if (S.sel && S.sel.kind === 'preset' && S.sel.id === id) { scrollToDetail(); return; }
    if (!(await confirmDiscard())) return;
    await selectPreset(id);
    scrollToDetail();
  }

  async function selectPreset(id, { tab } = {}) {
    resetEditor();
    if (detailAc) detailAc.abort();
    const myAc = detailAc = new AbortController();
    S.sel = { kind: 'loading', id };
    setUrl({ id });
    renderList();
    ed = null;
    fill(detail, h('div', { class: 'card' }, h('div', { class: 'status-line' }, h('span', { class: 'spinner' }), 'Wczytywanie presetu...')));
    try {
      const raw = await get(`/api/presets/${enc(id)}`, { signal: myAc.signal });
      if (destroyed || myAc !== detailAc) return;
      detailAc = null;
      const builtin = !!raw._builtin;
      const clean = stripMeta(raw);
      S.sel = { kind: 'preset', id, builtin, raw: clean };
      if (!builtin) loadEditor(formatPreset(clean));
      S.tab = tab || (store.get('presets.tab', 'preview') === 'editor' ? 'editor' : 'preview');
      renderList();
      renderDetail();
    } catch (e) {
      if (e.name === 'AbortError' || destroyed || myAc !== detailAc) return;
      detailAc = null;
      S.sel = { kind: 'error', id };
      renderList();
      renderDetailError(id, e);
    }
  }

  function openDraft(content, origin, label) {
    resetEditor();
    if (detailAc) { detailAc.abort(); detailAc = null; }
    const text = typeof content === 'string' ? content : formatPreset(stripMeta(content));
    S.sel = { kind: 'draft', origin, label };
    loadEditor(text, { baseline: origin === 'new' ? text : '', alwaysDirty: origin !== 'new' });
    S.tab = 'editor';
    setUrl({});
    renderList();
    renderDetail();
    scrollToDetail();
    if (ed && ed.ta && !narrow.matches) ed.ta.focus({ preventScroll: true });
  }

  async function newDraft() {
    if (!(await confirmDiscard())) return;
    openDraft(TEMPLATE, 'new');
  }

  // ── szczegóły ─────────────────────────────────────────────
  function renderDetailError(id, e) {
    ed = null;
    const sum = summaryOf(id);
    const canDelete = sum && !sum.builtin;
    const delBtn = canDelete ? h('button', { type: 'button', class: 'btn btn-danger btn-sm' }, 'Usuń plik presetu') : null;
    if (delBtn) delBtn.addEventListener('click', () => removePreset(delBtn, id, String(sum.name || id)));
    fill(detail, h('div', { class: 'card' },
      h('div', { class: 'notice notice-err', role: 'alert' },
        e.status === 404 ? `Nie znaleziono presetu "${id}".` : `Nie udało się wczytać presetu "${id}": ${e.message}`),
      h('div', { class: 'pr-actions' },
        h('button', { type: 'button', class: 'btn btn-ghost btn-sm', onclick: () => selectPreset(id) }, 'Spróbuj ponownie'),
        delBtn)));
  }

  function renderDetail() {
    const sel = S.sel;
    if (!sel) {
      ed = null;
      fill(detail, h('div', { class: 'card' }, emptyState('Wybierz preset z listy, aby zobaczyć mapę rejestrów, albo utwórz własny.', [
        { label: '+ Nowy preset', onClick: () => newDraft() },
        { label: 'Importuj JSON', class: 'btn-ghost', onClick: pickFile },
      ])));
      return;
    }
    const editable = isEditable();
    ed = { head: h('div', { class: 'pr-head-main' }), pane: h('div', { class: 'pr-pane' }) };

    let tabs = null;
    if (editable) {
      const mk = (id, label) => h('button', {
        type: 'button', 'aria-pressed': String(S.tab === id), dataset: { tab: id }, onclick: () => setTab(id),
      }, label);
      tabs = h('div', { class: 'segmented pr-tabs', role: 'group', 'aria-label': 'Tryb widoku' },
        mk('preview', 'Podgląd'), mk('editor', 'Edytor JSON'));
      ed.tabs = tabs;
    }

    fill(detail, h('div', { class: 'card pr-card' },
      h('div', { class: 'pr-head' }, ed.head, tabs),
      actionsBar(),
      ed.pane));
    renderHead();
    if (editable) {
      buildEditor();
      validateNow();
    }
    renderPane();
  }

  function btn(label, cls, onClick, attrs = {}) {
    const b = h('button', { type: 'button', class: 'btn btn-sm ' + cls, ...attrs }, label);
    b.addEventListener('click', () => onClick(b));
    return b;
  }

  function actionsBar() {
    const sel = S.sel;
    const bar = h('div', { class: 'pr-actions' });
    ed.actions = bar;
    const use = () => btn('Użyj w urządzeniu', 'btn-ghost', async () => {
      if (!(await confirmDiscard('Preset ma niezapisane zmiany - urządzenie użyje ostatnio zapisanej wersji. Odrzucić zmiany i przejść dalej?'))) return;
      allowNav = true;
      ctx.navigate('devices', { new: 1, preset: sel.id });
    });
    const dl = btn('Pobierz JSON', 'btn-ghost', downloadJson);
    if (sel.kind === 'preset' && sel.builtin) {
      fill(bar, btn('Kopiuj do moich', 'btn-primary', copyBuiltin), use(), dl);
      return bar;
    }
    ed.saveBtn = btn('Zapisz', 'btn-primary', (b) => save(b), { title: 'Ctrl+S' });
    const imp = btn('Importuj JSON', 'btn-ghost', pickFile);
    if (sel.kind === 'draft') {
      fill(bar, ed.saveBtn, dl, imp,
        btn('Odrzuć szkic', 'btn-ghost pr-push', async () => {
          if (!(await confirmDiscard('Odrzucić szkic presetu? Nie został zapisany.'))) return;
          resetEditor();
          S.sel = null;
          renderList();
          renderDetail();
        }));
      return bar;
    }
    const delBtn = btn('Usuń', 'btn-danger pr-push', (b) => removePreset(b, sel.id, currentName()));
    fill(bar, ed.saveBtn, btn('Zapisz jako nowy', 'btn-ghost', saveAsNew), use(), dl, imp, delBtn);
    return bar;
  }

  function renderHead() {
    if (!ed) return;
    const sel = S.sel;
    const d = currentData() || {};
    const sum = sel.kind === 'preset' ? summaryOf(sel.id) : null;
    const mm = [d.manufacturer, d.model].filter((x) => typeof x === 'string' && x).join(' ');
    const regs = isObj(d.registers) ? Object.keys(d.registers).length : 0;
    const tags = [];
    if (sel.kind === 'draft') {
      tags.push(h('span', { class: 'badge badge-warn' }, sel.origin === 'scanner' ? 'Szkic ze skanera'
        : sel.origin === 'import' ? 'Zaimportowany szkic' : 'Nowy szkic'));
      if (sel.label) tags.push(h('span', { class: 'pr-id muted small' }, sel.label));
    } else {
      tags.push(h('span', { class: 'badge ' + (sel.builtin ? 'badge-muted' : 'badge-ok') }, sel.builtin ? 'Wbudowany' : 'Mój preset'));
      tags.push(h('code', { class: 'pr-id', title: 'Identyfikator (nazwa pliku)' }, sel.id));
      if (sum && sum.overrides_builtin) tags.push(h('span', { class: 'badge badge-info' }, 'Nadpisuje wbudowany'));
      if (sel.builtin && sum && sum.valid === false) tags.push(h('span', { class: 'badge badge-err' }, 'Błędny'));
    }
    tags.push(h('span', { class: 'muted small' }, [regWord(regs), phasesText(d.phases)].filter(Boolean).join(' · ')));
    if (isEditable()) {
      ed.dirtyBadge = h('span', { class: 'badge badge-warn', hidden: !isDirty() }, 'Niezapisane zmiany');
      tags.push(ed.dirtyBadge);
    }
    fill(ed.head,
      h('h3', { class: 'pr-title' }, currentName()),
      mm ? h('p', { class: 'pr-sub' }, mm) : null,
      h('div', { class: 'pr-tags' }, tags));
    updateDirtyUi();
  }

  function updateDirtyUi() {
    if (!ed) return;
    const dirty = isDirty();
    if (ed.dirtyBadge) ed.dirtyBadge.hidden = !dirty;
    if (ed.saveBtn && !opBusy) ed.saveBtn.disabled = S.sel.kind === 'preset' && !dirty;
    if (ed.revertBtn) ed.revertBtn.disabled = S.sel.kind !== 'preset' || S.text === S.baseline;
  }

  function setTab(tab) {
    if (S.tab === tab) return;
    S.tab = tab;
    if (S.sel && S.sel.kind === 'preset') store.set('presets.tab', tab);
    if (ed && ed.tabs) ed.tabs.querySelectorAll('button').forEach((b) => b.setAttribute('aria-pressed', String(b.dataset.tab === tab)));
    renderPane();
    if (tab === 'editor' && ed && ed.ta) ed.ta.focus({ preventScroll: true });
  }

  function renderPane() {
    if (!ed) return;
    if (isEditable() && S.tab === 'editor') {
      fill(ed.pane, ed.editor);
      return;
    }
    fill(ed.pane, previewEl());
  }

  // ── podgląd ───────────────────────────────────────────────
  function previewEl() {
    const sel = S.sel;
    const notes = [];
    let errors = [];
    if (isEditable()) {
      const p = parseText(S.text);
      if (!p.ok || !isObj(p.value)) {
        notes.push(h('div', { class: 'notice notice-warn' },
          S.parsed ? 'Edytor zawiera błąd składni JSON - podgląd pokazuje ostatnią poprawną wersję. '
            : 'Edytor zawiera błąd składni JSON - popraw go, aby zobaczyć podgląd. ',
          h('button', { type: 'button', class: 'btn btn-ghost btn-sm', onclick: () => setTab('editor') }, 'Przejdź do edytora')));
      } else if (isDirty() && sel.kind === 'preset') {
        notes.push(h('div', { class: 'notice notice-info' }, 'Podgląd uwzględnia niezapisane zmiany z edytora.'));
      }
      if (S.val.state === 'errors') errors = S.val.errors || [];
      else if (S.val.state === 'idle' && sel.kind === 'preset' && !isDirty()) {
        const sum = summaryOf(sel.id);
        if (sum && sum.valid === false) errors = sum.errors || [];
      }
    } else {
      const sum = summaryOf(sel.id);
      if (sum && sum.valid === false) errors = sum.errors || [];
    }
    if (sel.kind === 'preset') {
      const sum = summaryOf(sel.id);
      if (sum && sum.overrides_builtin) {
        notes.push(h('div', { class: 'notice notice-info' },
          'Ten preset przesłania preset wbudowany o tym samym identyfikatorze. Po jego usunięciu znów będzie używana wersja z biblioteki.'));
      }
    }
    if (errors.length) {
      notes.push(h('div', { class: 'notice notice-err', role: 'alert' },
        `Preset zawiera ${errWord(errors.length)} i nie może być używany:`,
        h('ul', { class: 'errors' }, errors.map((t) => h('li', null, String(t))))));
    }
    const d = currentData();
    if (!d) return h('div', null, notes);

    const read = isObj(d.read) ? d.read : {};
    const dflt = (v, def) => (v == null ? `${def} (domyślnie)` : num(v));
    const func = normFunc(d.register_type ?? 'input');
    const order = normOrder(d.byte_order ?? 'ABCD');
    const dtype = normType(d.data_type ?? 'float32');
    const kv = [
      ['Producent', d.manufacturer || '-'],
      ['Model', d.model || '-'],
      ['Fazy', d.phases ?? '3 (domyślnie)'],
      ['Funkcja (domyślna)', func ? FUNC_LABEL[func] : String(d.register_type)],
      ['Kolejność bajtów', order ? `${order} - ${ORDER_DESC[order]}` : String(d.byte_order)],
      ['Typ danych', dtype || String(d.data_type)],
      ['address_offset', d.address_offset == null ? '0 (domyślnie)' : num(d.address_offset)],
      ['Port szeregowy', serialText(d.serial) || '-'],
      ['read.max_block / max_gap', `${dflt(read.max_block, DEFAULT_MAX_BLOCK)} / ${dflt(read.max_gap, DEFAULT_MAX_GAP)}`],
      ['Rejestr rozpoznawania (probe)', d.probe ? String(d.probe) : '-'],
    ];
    const rows = registerRows(d);
    const offset = Number.isInteger(d.address_offset) ? d.address_offset : 0;
    const out = [
      ...notes,
      typeof d.description === 'string' && d.description ? h('p', { class: 'pr-desc' }, d.description) : null,
      d.source ? h('p', { class: 'pr-src' }, h('span', { class: 'label' }, 'Źródło: '), linkify(String(d.source))) : null,
      h('dl', { class: 'kv pr-kv' }, kv.map(([k, v]) => [h('dt', null, k), h('dd', null, String(v))])),
      h('h4', { class: 'pr-h4' }, `Rejestry (${rows.length})`),
      offset ? h('p', { class: 'muted small pr-note' }, `Adresy uwzględniają address_offset (${offset > 0 ? '+' : ''}${offset}); adres z pliku w podpowiedzi.`) : null,
      rows.length ? registerTable(rows, offset) : h('p', { class: 'pr-none' }, 'Preset nie ma jeszcze rejestrów.'),
    ];
    if (sel.kind === 'preset' && sel.builtin) {
      out.push(h('details', { class: 'pr-raw' },
        h('summary', null, 'Pokaż JSON'),
        h('pre', { class: 'code' }, formatPreset(d))));
    }
    return h('div', { class: 'pr-preview' }, out);
  }

  function registerTable(rows, offset) {
    const cell = (r, cls) => h('td', { class: [cls, r.inherited ? 'inh' : '', r.bad ? 'err-text' : ''].filter(Boolean).join(' '),
      title: r.bad ? 'nieznana wartość' : r.inherited ? 'wartość domyślna presetu' : null }, r.text);
    const scaleText = (r) => {
      const parts = [];
      if (r.scale !== 1) parts.push(`× ${num(r.scale)}`);
      if (r.offset !== 0) parts.push(typeof r.offset === 'number' ? `${r.offset < 0 ? '-' : '+'} ${num(Math.abs(r.offset))}` : `+ ${r.offset}`);
      return parts.join(' ') || '-';
    };
    return h('div', { class: 'table-wrap tall' },
      h('table', { class: 'tbl pr-regs' },
        h('thead', null, h('tr', null,
          ['Klucz', 'Etykieta', 'Adres', 'Hex', 'Funkcja', 'Typ', 'Kolejność', 'Skala / offset', 'Jedn.', 'Grupa']
            .map((t) => h('th', { scope: 'col' }, t)))),
        h('tbody', null, rows.map((r) => {
          const bad = r.address == null;
          const fileNote = offset && r.fileAddr != null ? `w pliku: ${r.fileAddr}` : null;
          return h('tr', null,
            h('td', { class: 'mono pr-key' }, r.key),
            h('td', null, r.label),
            h('td', { class: 'addr' + (bad ? ' err-text' : ''), title: bad ? 'nieprawidłowy adres' : fileNote }, bad ? '?' : String(r.address)),
            h('td', { class: 'mono muted nowrap' }, bad ? '-' : fmtHex(r.address)),
            cell(r.func, 'nowrap'),
            cell(r.type, 'mono'),
            cell(r.order, 'mono'),
            h('td', { class: 'mono nowrap' }, scaleText(r)),
            h('td', { class: 'nowrap' }, r.unit),
            h('td', null, groupBadge(r.group)));
        }))));
  }

  // ── edytor ────────────────────────────────────────────────
  function buildEditor() {
    const hintId = 'pr-hint-' + Math.random().toString(36).slice(2, 8);
    const valId = hintId + '-v';
    const ta = h('textarea', {
      class: 'json-editor', spellcheck: false, autocapitalize: 'off', autocomplete: 'off', autocorrect: 'off',
      'aria-label': 'Treść presetu (JSON)', 'aria-describedby': `${hintId} ${valId}`,
    });
    ta.value = S.text;
    let escArmed = false;
    ta.addEventListener('input', () => {
      S.text = ta.value;
      updateDirtyUi();
      scheduleValidate();
    });
    ta.addEventListener('keydown', (e) => {
      if (e.key === 'Escape') { escArmed = true; return; }
      if (e.key === 'Tab' && !e.ctrlKey && !e.altKey && !e.metaKey) {
        if (escArmed) { escArmed = false; return; }   // Esc, potem Tab - wyjście z pola
        e.preventDefault();
        indent(ta, e.shiftKey);
        return;
      }
      if (e.key === 'Enter' && !e.shiftKey && !e.ctrlKey && !e.altKey && !e.metaKey && !e.isComposing) {
        e.preventDefault();
        newline(ta);
      }
      escArmed = false;
    });
    const formatBtn = h('button', { type: 'button', class: 'btn btn-ghost btn-sm' }, 'Formatuj');
    formatBtn.addEventListener('click', () => {
      const p = parseText(ta.value);
      if (!p.ok) { validateNow(); toast('Nie można sformatować - popraw błąd składni JSON', 'err'); gotoError(); return; }
      replaceAll(ta, formatPreset(p.value));
    });
    const revertBtn = S.sel.kind === 'preset'
      ? h('button', { type: 'button', class: 'btn btn-ghost btn-sm' }, 'Cofnij zmiany') : null;
    if (revertBtn) {
      revertBtn.addEventListener('click', async () => {
        if (!(await confirmDialog('Przywrócić ostatnio zapisaną wersję presetu? Niezapisane zmiany zostaną utracone.',
          { title: 'Cofnij zmiany', okLabel: 'Cofnij zmiany' }))) return;
        if (destroyed || !ed || ed.ta !== ta) return;
        replaceAll(ta, S.baseline);
      });
    }
    ed.ta = ta;
    ed.revertBtn = revertBtn;
    ed.val = h('div', { class: 'pr-val', id: valId, role: 'status', 'aria-live': 'polite' });
    ed.editor = h('div', { class: 'pr-editor' },
      h('div', { class: 'pr-ed-tools' },
        formatBtn, revertBtn,
        h('span', { class: 'muted small', id: hintId },
          'Tab - wcięcie, Shift+Tab - cofnięcie wcięcia, Esc i Tab - wyjście z pola, Ctrl+S - zapis. Formatuj porządkuje pola i sortuje rejestry wg adresu.')),
      ta, ed.val);
    updateDirtyUi();
  }

  /** Wstawia tekst w miejscu zaznaczenia (z zachowaniem historii Ctrl+Z, gdy przeglądarka pozwala). */
  function insertText(ta, text) {
    let ok = false;
    try { ok = document.execCommand('insertText', false, text); } catch { ok = false; }
    if (!ok) {
      ta.setRangeText(text, ta.selectionStart, ta.selectionEnd, 'end');
      ta.dispatchEvent(new Event('input', { bubbles: true }));
    }
  }
  function replaceAll(ta, text) {
    ta.focus({ preventScroll: true });
    ta.select();
    insertText(ta, text);
    ta.setSelectionRange(0, 0);
    ta.scrollTop = 0;
  }

  function indent(ta, outdent) {
    const v = ta.value;
    const s = ta.selectionStart, e = ta.selectionEnd;
    if (!outdent && !v.slice(s, e).includes('\n')) { insertText(ta, '  '); return; }
    const ls = v.lastIndexOf('\n', s - 1) + 1;
    const endPos = e > s && v[e - 1] === '\n' ? e - 1 : e;
    let le = v.indexOf('\n', endPos);
    if (le < 0) le = v.length;
    const lines = v.slice(ls, le).split('\n');
    const removedFirst = outdent ? (lines[0].match(/^ {1,2}/) || [''])[0].length : -2;
    const out = lines.map((l) => (outdent ? l.replace(/^ {1,2}/, '') : '  ' + l)).join('\n');
    if (out === v.slice(ls, le)) return;
    ta.setSelectionRange(ls, le);
    insertText(ta, out);
    if (s === e) {
      const caret = Math.max(ls, s - removedFirst);
      ta.setSelectionRange(caret, caret);
    } else {
      ta.setSelectionRange(ls, ls + out.length);
    }
  }

  /** Enter zachowuje wcięcie bieżącej linii (+2 spacje po { lub [). */
  function newline(ta) {
    const v = ta.value;
    const s = ta.selectionStart;
    const ls = v.lastIndexOf('\n', s - 1) + 1;
    let ind = (v.slice(ls, s).match(/^ */) || [''])[0];
    if (/[{[]\s*$/.test(v.slice(ls, s))) ind += '  ';
    insertText(ta, '\n' + ind);
  }

  function gotoError() {
    if (!ed || !ed.ta || S.val.state !== 'syntax' || S.val.pos == null) return;
    if (S.tab !== 'editor') setTab('editor');
    const ta = ed.ta;
    const pos = Math.min(S.val.pos, ta.value.length);
    ta.focus({ preventScroll: true });
    ta.setSelectionRange(pos, Math.min(pos + 1, ta.value.length));
    const lh = parseFloat(getComputedStyle(ta).lineHeight) || 20;
    ta.scrollTop = Math.max(0, ((S.val.line || 1) - 4) * lh);
  }

  // ── walidacja na żywo ─────────────────────────────────────
  function scheduleValidate() {
    clearTimeout(valTimer);
    valTimer = setTimeout(validateNow, VALIDATE_MS);
  }

  async function validateNow() {
    clearTimeout(valTimer);
    valTimer = null;
    if (valAc) { valAc.abort(); valAc = null; }
    if (!isEditable()) return;
    const seq = ++valSeq;
    const sel = S.sel;
    const before = valKey();
    const p = parseText(S.text);
    const prevName = currentName();
    if (!p.ok) {
      S.val = { state: 'syntax', ...p.error };
    } else if (!isObj(p.value)) {
      S.val = { state: 'errors', errors: ['preset musi być obiektem JSON { ... }'] };
    } else {
      S.parsed = p.value;
    }
    renderHead();
    if (sel.kind === 'draft' && prevName !== currentName()) renderList();
    if (!p.ok || !isObj(p.value)) { renderVal(); refreshPreview(before); return; }
    S.val = S.val.state === 'ok' || S.val.state === 'errors' ? { ...S.val, pending: true } : { state: 'idle', pending: true };
    renderVal();
    const myAc = valAc = new AbortController();
    try {
      const r = await post('/api/presets/validate', stripMeta(p.value), { signal: myAc.signal });
      if (destroyed || seq !== valSeq) return;
      S.val = r && r.ok ? { state: 'ok', count: r.register_count || 0 } : { state: 'errors', errors: (r && r.errors) || [] };
    } catch (e) {
      if (e.name === 'AbortError' || destroyed || seq !== valSeq) return;
      S.val = { state: 'neterr', message: e.message };
    } finally {
      if (valAc === myAc) valAc = null;
    }
    renderVal();
    refreshPreview(before);
  }

  const valKey = () => JSON.stringify([S.val.state, S.val.errors || null]);
  /** Podgląd pokazuje błędy walidacji - odśwież go, gdy się zmieniły. */
  function refreshPreview(before) {
    if (ed && S.tab !== 'editor' && valKey() !== before) renderPane();
  }

  function renderVal() {
    if (!ed || !ed.val) return;
    const v = S.val;
    ed.val.classList.toggle('is-pending', !!v.pending);
    let content;
    if (v.state === 'syntax') {
      const where = v.line ? ` (linia ${v.line}, kolumna ${v.col})` : '';
      content = h('div', { class: 'pr-val-row' },
        h('span', { class: 'badge badge-err' }, 'Błąd składni JSON'),
        h('span', { class: 'err-text' }, `${v.message}${where}`),
        v.pos != null ? h('button', { type: 'button', class: 'btn btn-ghost btn-sm', onclick: gotoError }, 'Pokaż miejsce') : null);
    } else if (v.state === 'errors') {
      content = [
        h('div', { class: 'pr-val-row' }, h('span', { class: 'badge badge-err' }, `Preset niepoprawny - ${errWord(v.errors.length)}`)),
        h('ul', { class: 'errors' }, v.errors.map((t) => h('li', null, String(t)))),
      ];
    } else if (v.state === 'ok') {
      content = h('div', { class: 'pr-val-row' }, h('span', { class: 'badge badge-ok' }, `Preset poprawny - ${regWord(v.count)}`));
    } else if (v.state === 'neterr') {
      content = h('div', { class: 'pr-val-row' },
        h('span', { class: 'badge badge-warn' }, 'Nie sprawdzono'),
        h('span', { class: 'warn-text' }, `Nie udało się sprawdzić presetu: ${v.message}`),
        h('button', { type: 'button', class: 'btn btn-ghost btn-sm', onclick: () => validateNow() }, 'Sprawdź ponownie'));
    } else {
      content = h('div', { class: 'pr-val-row muted' }, 'Sprawdzanie...');
    }
    fill(ed.val, v.pending ? h('span', { class: 'spinner pr-val-spin', 'aria-hidden': 'true' }) : null, content);
  }

  // ── operacje ──────────────────────────────────────────────
  /** Przycisk "zajęty" na czas fn(); pozostałe akcje zablokowane, drugie kliknięcie ignorowane. */
  async function busy(button, fn) {
    if (opBusy) return undefined;
    opBusy = true;
    const scope = button ? button.closest('.pr-actions') : null;
    const others = scope ? [...scope.querySelectorAll('button')].filter((b) => !b.disabled) : [];
    others.forEach((b) => { b.disabled = true; });
    let sp = null;
    if (button) {
      button.disabled = true;
      button.setAttribute('aria-busy', 'true');
      sp = h('span', { class: 'spinner', 'aria-hidden': 'true' });
      button.prepend(sp);
    }
    try {
      return await fn();
    } finally {
      opBusy = false;
      if (sp) sp.remove();
      if (button) { button.removeAttribute('aria-busy'); button.disabled = false; }
      others.forEach((b) => { b.disabled = false; });
      updateDirtyUi();
    }
  }

  /** Treść edytora jako obiekt do zapisu albo null (z komunikatem). */
  function editorBody() {
    const p = parseText(S.text);
    if (p.ok && isObj(p.value)) return stripMeta(p.value);
    validateNow();
    if (S.tab !== 'editor') setTab('editor');
    toast(p.ok ? 'Preset musi być obiektem JSON { ... }' : `Nie można zapisać - błąd składni JSON: ${p.error.message}`, 'err');
    gotoError();
    return null;
  }

  function showSaveErrors(e) {
    if (e.errors && e.errors.length) {
      S.val = { state: 'errors', errors: e.errors };
      renderVal();
      if (S.tab !== 'editor') renderPane();
      toast(`Preset niepoprawny - ${errWord(e.errors.length)}. Szczegóły pod edytorem.`, 'err');
    } else {
      showError(e, 'Zapis presetu: ');
    }
  }

  async function save(button) {
    if (!isEditable() || opBusy || !ed) return;
    const sel = S.sel;
    if (sel.kind === 'preset' && !isDirty()) { toast('Brak zmian do zapisania', 'info'); return; }
    const body = editorBody();
    if (!body) return;
    const sent = S.text;
    await busy(button || ed.saveBtn, async () => {
      try {
        if (sel.kind === 'draft') {
          const name = (typeof body.name === 'string' && body.name.trim()) || 'Nowy preset';
          const r = await post('/api/presets', { ...body, _save_as: name }, { signal: ac.signal });
          if (destroyed) return;
          S.alwaysDirty = false;
          S.baseline = S.text;
          toast(`Zapisano preset "${name}" (${r.id})`);
          await loadList();
          if (!destroyed) await selectPreset(r.id, { tab: S.tab });
        } else {
          await put(`/api/presets/${enc(sel.id)}`, body, { signal: ac.signal });
          if (destroyed || S.sel !== sel) return;
          S.baseline = sent;
          toast(`Zapisano zmiany w presecie "${currentName()}"`);
          updateDirtyUi();
          await loadList();
          if (S.tab !== 'editor') renderPane();
        }
        ctx.refreshHealth();
      } catch (e) {
        if (e.name === 'AbortError' || destroyed) return;
        showSaveErrors(e);
      }
    });
  }

  function saveAsNew() {
    if (!isEditable() || opBusy) return;
    const body = editorBody();
    if (!body) return;
    const base = (typeof body.name === 'string' && body.name.trim()) || S.sel.id || 'Nowy preset';
    const nameIn = h('input', { type: 'text', maxlength: 80, autocomplete: 'off', spellcheck: false });
    nameIn.value = `${base} (kopia)`.slice(0, 80);
    const errBox = h('ul', { class: 'errors', role: 'alert' });
    let ctl = null;
    let running = false;
    const m = modal({
      title: 'Zapisz jako nowy preset',
      subtitle: 'Treść z edytora trafi do nowego pliku; bieżący preset pozostanie bez zmian.',
      body: [field('Nazwa nowego presetu', nameIn, 'Z nazwy powstanie identyfikator pliku (unikalny).'), errBox],
      actions: [{ label: 'Anuluj' }, { label: 'Zapisz', class: 'btn-primary', onClick: (close) => submit(close) }],
      onClose: () => { if (ctl) ctl.abort(); modals.delete(m); },
    });
    modals.add(m);
    nameIn.select();
    const okBtn = m.el.querySelector('.modal-foot .btn-primary');
    nameIn.addEventListener('keydown', (e) => { if (e.key === 'Enter') { e.preventDefault(); submit(m.close); } });
    nameIn.addEventListener('input', () => { markInvalid(nameIn, false); errBox.replaceChildren(); });

    async function submit(close) {
      if (running) return;
      const name = nameIn.value.trim();
      if (!name) {
        markInvalid(nameIn, true);
        errBox.replaceChildren(h('li', null, 'Podaj nazwę nowego presetu.'));
        nameIn.focus();
        return;
      }
      running = true;
      okBtn.disabled = true;
      okBtn.replaceChildren(h('span', { class: 'spinner', 'aria-hidden': 'true' }), 'Zapisywanie...');
      errBox.replaceChildren();
      ctl = new AbortController();
      try {
        const r = await post('/api/presets', { ...body, name, _save_as: name }, { signal: ctl.signal });
        ctl = null;
        close();
        if (destroyed) return;
        S.alwaysDirty = false;
        S.baseline = S.text;
        toast(`Zapisano nowy preset "${name}" (${r.id})`);
        await loadList();
        if (!destroyed) await selectPreset(r.id, { tab: S.tab });
      } catch (e) {
        ctl = null;
        if (e.name === 'AbortError') return;
        errBox.replaceChildren(...[e.message, ...(e.errors || [])].map((t) => h('li', null, String(t))));
        running = false;
        okBtn.disabled = false;
        okBtn.textContent = 'Zapisz';
      }
    }
  }

  async function copyBuiltin(button) {
    const sel = S.sel;
    if (!sel || sel.kind !== 'preset' || !sel.builtin) return;
    const raw = sel.raw;
    const name = `${(typeof raw.name === 'string' && raw.name.trim()) || sel.id} (kopia)`;
    await busy(button, async () => {
      try {
        const r = await post('/api/presets', { ...raw, _save_as: name }, { signal: ac.signal });
        if (destroyed) return;
        toast(`Skopiowano do "Moje presety" jako "${r.id}"`);
        await loadList();
        if (!destroyed) await selectPreset(r.id, { tab: 'editor' });
      } catch (e) {
        if (e.name === 'AbortError' || destroyed) return;
        if (e.errors && e.errors.length) {
          // błędny preset wbudowany: otwórz kopię jako szkic do poprawienia
          openDraft(raw, 'import', `kopia "${sel.id}"`);
          toast(`Preset zawiera ${errWord(e.errors.length)} - otwarto kopię jako szkic do poprawienia`, 'err');
        } else {
          showError(e, 'Kopiowanie presetu: ');
        }
      }
    });
  }

  async function removePreset(button, id, name) {
    if (opBusy) return;
    let users = null;
    let usersErr = null;
    await busy(button, async () => {
      try {
        const devs = await get('/api/devices', { signal: ac.signal });
        users = (devs || []).filter((d) => d.preset === id);
      } catch (e) {
        if (e.name !== 'AbortError') usersErr = e.message;
      }
    });
    if (destroyed) return;
    const sum = summaryOf(id);
    const text = [`Usunąć preset "${name}" (plik ${id}.json)? Tej operacji nie można cofnąć.`];
    if (sum && sum.overrides_builtin) {
      text.push(h('br'), h('br'), 'Po usunięciu znów będzie używany preset wbudowany o tym samym identyfikatorze.');
    } else if (users && users.length) {
      text.push(h('br'), h('br'), `Używają go urządzenia: ${users.map((d) => d.name || d.id).join(', ')}. `,
        'Przestaną być odczytywane, dopóki nie wybierzesz dla nich innego presetu.');
    }
    if (usersErr) text.push(h('br'), h('br'), `(Nie udało się sprawdzić, czy preset jest używany: ${usersErr})`);
    if (isDirty()) text.push(h('br'), h('br'), 'Niezapisane zmiany w edytorze zostaną utracone.');
    const ok = await confirmDialog(text, { title: 'Usuń preset', okLabel: 'Usuń preset' });
    if (!ok || destroyed) return;
    await busy(button.isConnected ? button : null, async () => {
      try {
        await del(`/api/presets/${enc(id)}`, { signal: ac.signal });
        if (destroyed) return;
        toast(`Usunięto preset "${name}"`);
        const wasSelected = !!S.sel && S.sel.kind !== 'draft' && S.sel.id === id;
        if (wasSelected) {
          resetEditor();
          S.sel = null;
        }
        await loadList();
        if (destroyed) return;
        if (wasSelected && !S.sel) {
          if (summaryOf(id)) {
            await selectPreset(id);   // odsłonięty preset wbudowany
          } else {
            setUrl({});
            renderList();
            renderDetail();
          }
        }
        ctx.refreshHealth();
      } catch (e) {
        if (e.name === 'AbortError' || destroyed) return;
        showError(e, 'Usuwanie presetu: ');
      }
    });
  }

  function downloadJson() {
    const sel = S.sel;
    if (!sel) return;
    let text;
    if (sel.kind === 'preset' && sel.builtin) {
      text = formatPreset(sel.raw);
    } else {
      const p = parseText(S.text);
      text = p.ok && isObj(p.value) ? formatPreset(stripMeta(p.value)) : S.text;
      if (!p.ok) toast('Uwaga: plik zawiera błąd składni JSON', 'info');
    }
    const fname = sel.kind === 'preset' ? sel.id : currentName();
    download(`${safeFileName(fname)}.json`, text + '\n', 'application/json');
  }

  function pickFile() {
    fileIn.value = '';
    fileIn.click();
  }

  async function onFile() {
    const f = fileIn.files && fileIn.files[0];
    if (!f) return;
    if (f.size > MAX_IMPORT) {
      toast(`Plik "${f.name}" jest za duży (${Math.round(f.size / 1024)} KiB, maks. 2 MiB)`, 'err');
      fileIn.value = '';
      return;
    }
    let text;
    try {
      text = await f.text();
    } catch (e) {
      showError(e, `Odczyt pliku "${f.name}": `);
      return;
    } finally {
      fileIn.value = '';
    }
    if (destroyed) return;
    if (!(await confirmDiscard('Edytor zawiera niezapisane zmiany. Odrzucić je i wczytać plik?'))) return;
    text = text.replace(/^﻿/, '');
    const p = parseText(text);
    const good = p.ok && isObj(p.value);
    openDraft(good ? formatPreset(stripMeta(p.value)) : text, 'import', f.name);
    if (good) toast(`Wczytano "${f.name}" - sprawdź i zapisz preset`, 'info');
    else toast(`Plik "${f.name}" nie jest poprawnym presetem JSON - popraw go w edytorze`, 'err');
  }

  // ── ochrona niezapisanych zmian ───────────────────────────
  // Linki wewnątrz aplikacji (#widok) przechwytujemy przed nawigacją i pytamy o zgodę.
  // Wstecz/Dalej przeglądarki nie da się zatrzymać bez routera - wtedy praca trafia do `stash`.
  function onDocClick(e) {
    if (e.defaultPrevented || e.button !== 0 || e.ctrlKey || e.metaKey || e.shiftKey || e.altKey) return;
    const a = e.target instanceof Element ? e.target.closest('a[href]') : null;
    if (!a || (a.target && a.target !== '_self') || a.hasAttribute('download')) return;
    const href = a.getAttribute('href') || '';
    if (!href.startsWith('#') || href === location.hash || !isDirty()) return;
    e.preventDefault();
    if (navDialog) return;
    navDialog = true;
    confirmDialog('Edytor presetu zawiera niezapisane zmiany. Odrzucić je i przejść dalej?',
      { title: 'Niezapisane zmiany', okLabel: 'Odrzuć i przejdź' }).then((ok) => {
      navDialog = false;
      if (!ok || destroyed) return;
      allowNav = true;
      location.hash = href;
    });
  }
  function onBeforeUnload(e) {
    if (!isDirty()) return;
    e.preventDefault();
    e.returnValue = '';
  }
  function onKey(e) {
    if (!(e.ctrlKey || e.metaKey) || e.altKey || e.shiftKey || String(e.key || '').toLowerCase() !== 's') return;
    if (!S.sel || document.querySelector('#modal-root .modal-overlay')) return;
    if (!isEditable()) {
      if (S.sel.kind === 'preset' && S.sel.builtin) {
        e.preventDefault();
        toast('Preset wbudowany jest tylko do odczytu - użyj "Kopiuj do moich", aby go edytować', 'info');
      }
      return;
    }
    e.preventDefault();
    save(null);
  }
  document.addEventListener('click', onDocClick, true);
  window.addEventListener('beforeunload', onBeforeUnload);
  document.addEventListener('keydown', onKey);

  function offerRestore(saved) {
    const what = saved.sel.kind === 'draft' ? 'szkicu' : 'presetu';
    fill(restoreBox, h('div', { class: 'notice notice-warn', role: 'status' },
      `Masz niezapisane zmiany ${what} "${saved.name}" z poprzedniej wizyty w tym widoku. `,
      h('button', {
        type: 'button', class: 'btn btn-primary btn-sm',
        onclick: async () => {
          if (!(await confirmDiscard())) return;
          restoreBox.replaceChildren();
          resetEditor();
          if (detailAc) { detailAc.abort(); detailAc = null; }
          S.sel = saved.sel;
          loadEditor(saved.text, { baseline: saved.baseline, alwaysDirty: saved.alwaysDirty });
          S.tab = 'editor';
          setUrl(S.sel.kind === 'preset' ? { id: S.sel.id } : {});
          renderList();
          renderDetail();
          scrollToDetail();
        },
      }, 'Przywróć'),
      h('button', { type: 'button', class: 'btn btn-ghost btn-sm', onclick: () => restoreBox.replaceChildren() }, 'Odrzuć')));
  }

  // ── start ─────────────────────────────────────────────────
  if (stash) {
    offerRestore(stash);
    stash = null;
  }
  renderList();
  const listReady = loadList();
  if (ctx.params.draft) {
    const d = ctx.handoff && ctx.handoff.draft;
    if (isObj(d) || typeof d === 'string') {
      openDraft(d, 'scanner');
      toast('Szkic presetu ze skanera - sprawdź rejestry i zapisz', 'info');
    } else {
      setUrl({});
      renderDetail();
      toast('Szkic ze skanera nie jest już dostępny (strona została odświeżona). Utwórz go ponownie w skanerze.', 'err');
    }
  } else if (ctx.params.id) {
    const id = ctx.params.id;
    S.sel = { kind: 'loading', id };
    fill(detail, h('div', { class: 'card' }, h('div', { class: 'status-line' }, h('span', { class: 'spinner' }), 'Wczytywanie presetu...')));
    listReady.then(async () => {
      if (destroyed || !S.sel || S.sel.kind !== 'loading' || S.sel.id !== id) return;
      await selectPreset(id);
      if (destroyed || narrow.matches) return;
      const it = listBox.querySelector('.list-item.selected');
      if (it) it.scrollIntoView({ block: 'nearest' });
    });
  } else {
    renderDetail();
  }

  return {
    unmount() {
      destroyed = true;
      ac.abort();
      if (detailAc) detailAc.abort();
      if (valAc) valAc.abort();
      clearTimeout(valTimer);
      if (isDirty() && !allowNav) {
        stash = { sel: S.sel, text: S.text, baseline: S.baseline, alwaysDirty: S.alwaysDirty, name: currentName() };
        toast(`Niezapisane zmiany presetu "${stash.name}" zachowano - wróć do Presetów, aby je przywrócić`, 'info', 8000);
      }
      document.removeEventListener('click', onDocClick, true);
      window.removeEventListener('beforeunload', onBeforeUnload);
      document.removeEventListener('keydown', onKey);
      for (const m of [...modals]) m.close();
    },
  };
}

// ── pomoc: format presetu ────────────────────────────────────

const PRESET_FIELDS = [
  ['name', 'Nazwa wyświetlana na liście i w urządzeniach', '"Eastron SDM630"'],
  ['manufacturer, model', 'Producent i model licznika', '"Eastron", "SDM630"'],
  ['description', 'Opis (dowolny tekst)', '"Licznik 3-fazowy..."'],
  ['source', 'Źródło mapy rejestrów: dokumentacja, linki (informacyjnie)', '"https://..."'],
  ['phases', 'Liczba faz: 1, 2 lub 3 (domyślnie 3)', '3'],
  ['register_type', 'Domyślna funkcja odczytu: input (FC04) albo holding (FC03)', '"input"'],
  ['byte_order', 'Domyślna kolejność bajtów: ABCD, CDAB, BADC, DCBA (aliasy: big_endian, word_swap, byte_swap, little_endian)', '"ABCD"'],
  ['data_type', 'Domyślny typ danych: int16, uint16, int32, uint32, float32, int64, uint64, float64', '"float32"'],
  ['address_offset', 'Dodawany do każdego adresu, np. -1 dla adresów 1-based z dokumentacji', '0'],
  ['serial', 'Fabryczne ustawienia portu RS-485 (informacyjnie)', '{"baudrate": 9600, "parity": "N", "stopbits": 1}'],
  ['read.max_block', `Maks. liczba rejestrów w jednym zapytaniu, 1-125 (domyślnie ${DEFAULT_MAX_BLOCK})`, '{"max_block": 64}'],
  ['read.max_gap', `Maks. przerwa między rejestrami łączonymi w jedno zapytanie, 0-125 (domyślnie ${DEFAULT_MAX_GAP})`, '{"max_gap": 10}'],
  ['probe', 'Rejestr używany przy automatycznym rozpoznawaniu licznika', '"voltage_l1"'],
  ['registers', 'Mapa rejestrów {klucz: opis}; klucz: litery, cyfry, _ . - (maks. 64 znaki)', '{"voltage_l1": {...}}'],
];
const REGISTER_FIELDS = [
  ['address', 'Adres 0-65535: liczba albo tekst szesnastkowy', '0 lub "0x0156"'],
  ['label', 'Etykieta wyświetlana na dashboardzie (domyślnie klucz)', '"Napięcie L1"'],
  ['unit', 'Jednostka', '"V"'],
  ['decimals', 'Miejsca po przecinku, 0-10 (domyślnie 2)', '1'],
  ['group', 'Grupa na dashboardzie (lista niżej, domyślnie other)', '"voltage"'],
  ['type', 'Typ danych - nadpisuje data_type presetu', '"uint32"'],
  ['byte_order', 'Kolejność bajtów - nadpisuje domyślną', '"CDAB"'],
  ['register_type', 'Funkcja odczytu - nadpisuje domyślną', '"holding"'],
  ['scale, offset', 'Wartość fizyczna = surowa × scale + offset (scale nie może być 0)', '0.1, 0'],
  ['invalid', 'Surowe wartości oznaczające "brak pomiaru" (np. w licznikach ABB)', '[65535]'],
];
const BYTE_ORDER_EXAMPLE = [
  ['ABCD', 'big-endian (najczęstsza: Eastron, Orno, Finder, Schneider, Janitza)', '0x4366', '0x0000'],
  ['CDAB', 'zamienione słowa, młodsze pierwsze (Carlo Gavazzi, część Schneider)', '0x0000', '0x4366'],
  ['BADC', 'zamienione bajty w każdym słowie', '0x6643', '0x0000'],
  ['DCBA', 'little-endian, całkowicie odwrócona', '0x0000', '0x6643'],
];
const EXAMPLE = `{
  "name": "Eastron SDM630",
  "manufacturer": "Eastron", "model": "SDM630", "description": "...",
  "phases": 3,
  "register_type": "input",
  "byte_order": "ABCD",
  "data_type": "float32",
  "address_offset": 0,
  "serial": {"baudrate": 9600, "parity": "N", "stopbits": 1},
  "read": {"max_block": 64, "max_gap": 10},
  "probe": "voltage_l1",
  "registers": {
    "voltage_l1": {"address": 0, "unit": "V", "decimals": 1, "group": "voltage",
                   "label": "Napięcie L1"},
    "energy_import": {"address": "0x0048", "unit": "kWh", "group": "energy",
                      "label": "Energia pobrana", "type": "uint32", "scale": 0.01,
                      "invalid": [4294967295]}
  }
}`;

function helpTable(head, rows) {
  return h('div', { class: 'table-wrap' }, h('table', { class: 'tbl' },
    h('thead', null, h('tr', null, head.map((t) => h('th', { scope: 'col' }, t)))),
    h('tbody', null, rows.map((r) => h('tr', null, r.map((c, i) => h('td', i === 0 || i >= 2 ? { class: 'mono' } : null, c)))))));
}

function helpCard() {
  const d = h('details', { class: 'card pr-help', open: !!store.get('presets.help', false) },
    h('summary', null, h('span', { class: 'card-title' }, 'Format presetu'), h('span', { class: 'muted small' }, 'pola JSON, kolejność bajtów, przykład')),
    h('div', { class: 'prose' },
      h('p', null, 'Preset to plik JSON z mapą rejestrów jednego modelu licznika (schemat 2, zgodny wstecz ze starym formatem). ',
        'Pola na poziomie presetu ustalają wartości domyślne, które pojedynczy rejestr może nadpisać. ',
        'Wartość fizyczna = surowa × scale + offset.'),
      h('h3', null, 'Pola presetu'),
      helpTable(['Pole', 'Znaczenie', 'Przykład'], PRESET_FIELDS),
      h('h3', null, 'Pola rejestru'),
      helpTable(['Pole', 'Znaczenie', 'Przykład'], REGISTER_FIELDS),
      h('p', null, 'Typy danych zajmują: int16 i uint16 - 1 rejestr; int32, uint32 i float32 - 2 rejestry; int64, uint64 i float64 - 4 rejestry.'),
      h('p', null, 'Grupy: ', GROUP_ORDER.map((g, i) => [i ? ', ' : '', h('code', null, g), ` (${GROUPS[g].label})`])),
      h('h3', null, 'Kolejność bajtów'),
      h('p', null, 'Wartości 32-bitowe zajmują dwa rejestry po 16 bitów, a producenci różnie układają w nich bajty. ',
        'Litery oznaczają bajty wartości zapisanej big-endian (A = najstarszy). Przykład: 230,0 V jako float32 = 0x43660000.'),
      helpTable(['Kolejność', 'Opis', 'Rejestr 1', 'Rejestr 2'], BYTE_ORDER_EXAMPLE),
      h('p', null, 'Dla typów 64-bitowych ta sama zasada: ABCD = słowa od najstarszego, CDAB = słowa od najmłodszego. ',
        'Dla typów 16-bitowych liczy się tylko zamiana bajtów (BADC, DCBA). Jeśli odczyt daje absurdalne liczby, ',
        'sprawdź kolejność w skanerze rejestrów - pokazuje wszystkie warianty naraz.'),
      h('h3', null, 'Przykład'),
      h('pre', { class: 'code' }, EXAMPLE)));
  d.addEventListener('toggle', () => store.set('presets.help', d.open));
  return d;
}
