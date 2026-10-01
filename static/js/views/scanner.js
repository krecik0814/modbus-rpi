// Skaner rejestrów: odczyt zakresu, wszystkie interpretacje (float32/int32 w różnych kolejnościach bajtów),
// podpowiedzi, tryb Live z podświetlaniem zmian, eksport CSV, szkic presetu, wyszukiwanie Unit ID i zapis.

import {
  h, get, post, toast, showError, Poller, runJob, store, modal, confirmDialog, fmt, fmtHex, fmtTime,
  parseAddress, FUNCTIONS, GROUPS, download, csvCell, field, select, markInvalid, emptyState, pageHeader,
} from '../core.js';

const MAX_SPAN = 2000;
const LIVE_MS = 1500;
const LOG_MAX_SCANS = 300;
const LOG_MAX_CHARS = 8e6;          // ochrona pamięci przy dużych zakresach (~16 MB tekstu)
const ORDERS = ['ABCD', 'CDAB', 'BADC', 'DCBA'];
const ORDER_LABELS = {
  ABCD: 'ABCD (big endian)', CDAB: 'CDAB (zamiana słów)', BADC: 'BADC (zamiana bajtów)', DCBA: 'DCBA (little endian)',
};
const GUESS_CLASS = {
  voltage: 'voltage', line_volt: 'line_volt', current: 'current', power: 'power', frequency: 'system',
  pf: 'pf', energy: 'energy', thd: 'thd',
};
const VIEWS = {
  float32: ORDERS.map((o) => ({ head: ['float32', o], get: (r) => r.decoded?.float32?.[o], float: true })),
  int32: [
    { head: ['int32', 'ABCD'], get: (r) => r.decoded?.int32?.ABCD },
    { head: ['int32', 'CDAB'], get: (r) => r.decoded?.int32?.CDAB },
    { head: ['uint32', 'ABCD'], get: (r) => r.decoded?.uint32?.ABCD },
    { head: ['uint32', 'CDAB'], get: (r) => r.decoded?.uint32?.CDAB },
  ],
};
const CSV_HEAD = ['czas', 'adres', 'adres_hex', 'raw', 'u16', 'i16', 'float32_ABCD', 'float32_CDAB', 'float32_BADC',
  'float32_DCBA', 'int32_ABCD', 'int32_CDAB', 'uint32_ABCD', 'uint32_CDAB', 'podpowiedz'];
const DEFAULTS = {
  bus: '', unit: 1, func: 'input', start: '0', end: '100', step: 2, view: 'float32', onlyHints: false,
  us: { first: '1', last: '247', timeout: '0.3', func: 'input', address: '0' },
  wr: { func: 'holding', address: '0', values: '', coil: '1' },
};

const isBits = (func) => func === 'coil' || func === 'discrete';
const dec = (s) => s.replace('.', ',');

/** Do 6 cyfr znaczących; bardzo małe/duże w notacji wykładniczej (1,2e-38 = typowy objaw złej kolejności bajtów). */
function fmtNum(v) {
  if (v == null) return '-';
  if (typeof v !== 'number') return String(v);
  if (!Number.isFinite(v)) return String(v);
  if (v === 0) return '0';
  const a = Math.abs(v);
  if (a < 1e-4 || a >= 1e7) {
    const [m, e] = v.toExponential(5).split('e');
    return dec(m.replace(/\.?0+$/, '')) + 'e' + e.replace('+', '');
  }
  return dec(String(Number(v.toPrecision(6))));
}
const fmtInt = (v) => (v == null ? '-' : String(v));

function plural(n, one, few, many) {
  if (n === 1) return one;
  const d = n % 10, dd = n % 100;
  return d >= 2 && d <= 4 && !(dd >= 12 && dd <= 14) ? few : many;
}

const pad2 = (n) => String(n).padStart(2, '0');
function localStamp(d, sep = ' ') {
  return `${d.getFullYear()}-${pad2(d.getMonth() + 1)}-${pad2(d.getDate())}${sep}${pad2(d.getHours())}:${pad2(d.getMinutes())}:${pad2(d.getSeconds())}`;
}

function rangeText([a, b]) {
  return a === b ? fmtHex(a) : `${fmtHex(a)}-${fmtHex(b)} (${b - a + 1})`;
}

function notation(func, addr) {
  if (func === 'input' && addr <= 9998) return `notacja ${30001 + addr}`;
  if (func === 'holding' && addr <= 9998) return `notacja ${40001 + addr}`;
  return null;
}

/** Adres z pola tekstowego (dziesiętnie, 0x hex, 3xxxx/4xxxx). end=true dopuszcza 65536. */
function parseAddr(text, func, { end = false } = {}) {
  const s = String(text ?? '').trim();
  if (!s) return { error: 'podaj adres' };
  let r = parseAddress(s, func);
  if (!r && end && /^(65536|0x0*10000)$/i.test(s)) r = { address: 65536, note: null };
  if (!r) return { error: 'niepoprawny adres - dziesiętnie, hex (0x24) albo notacja 30001/40001' };
  let note = r.note ? `${r.note} = ${fmtHex(r.address)}` : /^0x/i.test(s) ? `= ${r.address}` : `= ${fmtHex(r.address)}`;
  if (func === 'holding' && /^3\d{4}$/.test(s)) note += ' (uwaga: 3xxxx to notacja Input Registers)';
  if (func === 'input' && /^4\d{4}$/.test(s)) note += ' (uwaga: 4xxxx to notacja Holding Registers)';
  return { address: r.address, note };
}

function parseIntIn(text, lo, hi) {
  const s = String(text ?? '').trim();
  if (!/^\d+$/.test(s)) return null;
  const n = Number(s);
  return n >= lo && n <= hi ? n : null;
}

function parseFloatIn(text, lo, hi) {
  const s = String(text ?? '').trim().replace(',', '.');
  if (!/^\d+(\.\d+)?$|^\.\d+$/.test(s)) return null;
  const n = Number(s);
  return n >= lo && n <= hi ? n : null;
}

/** Wartości do zapisu: "1234, 0x10, -5" -> [1234, 16, 65531]; ujemne jako int16. */
function parseWriteValues(text) {
  const parts = String(text ?? '').split(/[\s,;]+/).filter(Boolean);
  if (!parts.length) return { error: 'podaj wartość' };
  if (parts.length > 123) return { error: 'maksymalnie 123 rejestry w jednym zapisie' };
  const values = [];
  for (const p of parts) {
    let n;
    if (/^0x[0-9a-f]{1,4}$/i.test(p)) n = parseInt(p, 16);
    else if (/^-?\d+$/.test(p)) n = Number(p);
    else return { error: `"${p}" nie jest liczbą całkowitą` };
    if (n < -32768 || n > 65535) return { error: `${p} poza zakresem (-32768..65535)` };
    values.push(n < 0 ? n + 0x10000 : n);
  }
  return { values };
}

function spinnerLabel(text) {
  return [h('span', { class: 'spinner', 'aria-hidden': 'true' }), ' ', text];
}

/** Przełącznik segmentowy (przyciski z aria-pressed). */
function segmented(label, options, value, onChange) {
  const btns = options.map(([v, l]) => h('button', {
    type: 'button', 'aria-pressed': String(v === value), onclick: () => { set(v); onChange(v); },
  }, l));
  function set(v) { options.forEach(([ov], i) => btns[i].setAttribute('aria-pressed', String(ov === v))); }
  return { el: h('div', { class: 'segmented', role: 'group', 'aria-label': label }, btns), set };
}

function loadPrefs(params) {
  const saved = store.get('scanner', {}) || {};
  const p = { ...DEFAULTS, ...saved, us: { ...DEFAULTS.us, ...(saved.us || {}) }, wr: { ...DEFAULTS.wr, ...(saved.wr || {}) } };
  if (!VIEWS[p.view]) p.view = 'float32';
  if (p.step !== 1 && p.step !== 2) p.step = 2;
  if (!FUNCTIONS[p.func]) p.func = 'input';
  // parametry z adresu (#scanner?bus=sim&unit=2&func=holding&start=0&end=80&scan=1)
  if (params.bus) p.bus = params.bus;
  if (params.unit && parseIntIn(params.unit, 0, 255) != null) p.unit = Number(params.unit);
  const f = params.func || params.register_type;
  if (f && FUNCTIONS[f]) p.func = f;
  if (params.start) p.start = params.start;
  if (params.end) p.end = params.end;
  return p;
}

export function mount(root, ctx) {
  const prefs = loadPrefs(ctx.params || {});
  const save = () => store.set('scanner', prefs);
  const canWrite = !!(ctx.info && ctx.info.features && ctx.info.features.write);

  let alive = true;
  let busesReady = false;
  let busCtl = null;         // ładowanie listy magistral
  let scanCtl = null;        // ręczny skan w toku
  let live = null;           // {req, poller, ctl, count}
  let liveLog = [];          // [{text}] - gotowe linie CSV kolejnych skanów Live
  let liveChars = 0;
  let logReq = null;         // parametry skanów w logu Live
  let last = null;           // {req, res, ts} - ostatni udany skan
  let table = null;          // stan wyrenderowanych wyników
  let unitCtl = null;        // wyszukiwanie Unit ID
  let writeCtl = null;
  let presetModal = null;

  // ── karta parametrów ────────────────────────────────────────
  const busSel = h('select', { name: 'bus', disabled: true }, h('option', { value: '' }, 'Ładowanie...'));
  const unitIn = h('input', { type: 'number', name: 'unit', min: 0, max: 255, step: 1, inputmode: 'numeric', value: String(prefs.unit) });
  const funcSel = select(FUNCTIONS, prefs.func, { name: 'register_type' });
  const startIn = h('input', { type: 'text', name: 'start', value: prefs.start, autocomplete: 'off', spellcheck: false, placeholder: 'np. 0, 0x24, 30001' });
  const endIn = h('input', { type: 'text', name: 'end', value: prefs.end, autocomplete: 'off', spellcheck: false, placeholder: 'np. 80' });
  const startNote = h('span', { 'aria-live': 'polite' });
  const endNote = h('span', { 'aria-live': 'polite' });
  const stepSeg = segmented('Krok skanowania', [[2, 'Pary (float/int32)'], [1, 'Co 1 rejestr']], prefs.step, (v) => {
    prefs.step = v; save();
  });
  const stepLabelId = 'sc-step-' + Math.random().toString(36).slice(2);
  stepSeg.el.setAttribute('aria-labelledby', stepLabelId);
  const stepField = h('div', { class: 'field' }, h('span', { class: 'label', id: stepLabelId }, 'Krok'), stepSeg.el,
    h('span', { class: 'hint' }, 'co 1 - wartości 32-bit pod nieparzystymi adresami'));
  const formErrors = h('ul', { class: 'errors', role: 'alert' });
  const busNotice = h('div');

  const scanBtn = h('button', { type: 'submit', class: 'btn btn-primary' }, 'Skanuj');
  const liveBtn = h('button', { type: 'button', class: 'btn btn-warn', 'aria-pressed': 'false', onclick: () => (live ? stopLive() : startLive()) }, 'Live');
  const csvBtn = h('button', { type: 'button', class: 'btn btn-ghost', disabled: true, onclick: exportCsv }, 'Eksport CSV');
  const presetBtn = h('button', { type: 'button', class: 'btn btn-success', disabled: true, onclick: createPreset }, 'Utwórz preset');

  const paramsFs = h('fieldset', { class: 'form-grid sc-form' },
    field('Magistrala', busSel),
    field('Unit ID', unitIn, '0-255, zwykle 1-247'),
    field('Funkcja', funcSel),
    field('Od adresu', startIn, startNote),
    field('Do adresu (wyłącznie)', endIn, endNote),
    stepField);
  const paramsForm = h('form', { novalidate: true, onsubmit: (e) => { e.preventDefault(); manualScan(); } },
    paramsFs, formErrors,
    h('div', { class: 'sc-actions' }, scanBtn, liveBtn, csvBtn, presetBtn));
  const paramsCard = h('section', { class: 'card', 'aria-labelledby': 'sc-params-t' },
    h('div', { class: 'card-header' }, h('h3', { class: 'card-title', id: 'sc-params-t' }, 'Parametry skanowania')),
    busNotice, paramsForm);

  // ── karta wyników ───────────────────────────────────────────
  const viewSeg = segmented('Widok kolumn 32-bit', [['float32', 'float32'], ['int32', 'int32']], prefs.view, (v) => {
    prefs.view = v; save(); applyView();
  });
  const onlyChk = h('input', { type: 'checkbox', checked: !!prefs.onlyHints, onchange: () => {
    prefs.onlyHints = onlyChk.checked; save(); applyOnly();
  } });
  const regControls = h('div', { class: 'sc-controls' },
    h('span', { class: 'label' }, 'Widok'), viewSeg.el,
    h('label', { class: 'sc-check' }, onlyChk, 'Tylko rozpoznane'));
  const resultsMeta = h('span', { class: 'small muted' });
  const liveLine = h('div', { class: 'status-line', role: 'status', hidden: true });
  const summary = h('div', { 'aria-live': 'polite' });
  const resultsBody = h('div', null, emptyState('Ustaw parametry i kliknij „Skanuj”. Na symulatorze spróbuj: magistrala sim, Unit ID 1, Input Registers, adresy 0-80.'));
  resultsBody.addEventListener('animationend', (e) => {
    if (e.animationName === 'flash') e.target.classList.remove('flash');
  });
  const resultsCard = h('section', { class: 'card', 'aria-labelledby': 'sc-res-t' },
    h('div', { class: 'card-header' },
      h('div', null, h('h3', { class: 'card-title', id: 'sc-res-t' }, 'Wyniki'), resultsMeta),
      regControls),
    liveLine, summary, resultsBody);

  // ── wyszukiwanie Unit ID ────────────────────────────────────
  const usFirst = h('input', { type: 'number', min: 0, max: 255, step: 1, inputmode: 'numeric', value: prefs.us.first });
  const usLast = h('input', { type: 'number', min: 0, max: 255, step: 1, inputmode: 'numeric', value: prefs.us.last });
  const usTimeout = h('input', { type: 'text', inputmode: 'decimal', value: prefs.us.timeout, autocomplete: 'off' });
  const usFunc = select([['input', FUNCTIONS.input], ['holding', FUNCTIONS.holding]], prefs.us.func);
  const usAddr = h('input', { type: 'text', value: prefs.us.address, autocomplete: 'off', spellcheck: false });
  const usAddrNote = h('span', { 'aria-live': 'polite' });
  const usBusNote = h('p', { class: 'small muted sc-gap' });
  const usErrors = h('ul', { class: 'errors', role: 'alert' });
  const usBtn = h('button', { type: 'submit', class: 'btn btn-primary' }, 'Szukaj');
  const usCancel = h('button', { type: 'button', class: 'btn btn-ghost', hidden: true, onclick: () => unitCtl && unitCtl.abort() }, 'Anuluj');
  const usBar = h('div', { style: { width: '0%' } });
  const usProgress = h('div', { class: 'progress', role: 'progressbar', 'aria-valuemin': '0', 'aria-valuemax': '100', 'aria-valuenow': '0', 'aria-label': 'Postęp wyszukiwania' }, usBar);
  const usMsg = h('span', { class: 'small muted' });
  const usStatus = h('div', { class: 'sc-progress', hidden: true }, usProgress, usMsg);
  const usList = h('div', { 'aria-live': 'polite' });
  const usForm = h('form', { novalidate: true, onsubmit: (e) => { e.preventDefault(); runUnitScan(); } },
    h('div', { class: 'form-grid sc-form' },
      field('Od Unit ID', usFirst),
      field('Do Unit ID', usLast),
      field('Timeout [s]', usTimeout, '0,05-5 s'),
      field('Funkcja testowa', usFunc),
      field('Adres testowy', usAddr, usAddrNote)),
    usErrors,
    h('div', { class: 'sc-actions' }, usBtn, usCancel));
  const unitsCard = h('section', { class: 'card', 'aria-labelledby': 'sc-us-t' },
    h('div', { class: 'card-header' }, h('h3', { class: 'card-title', id: 'sc-us-t' }, 'Szukaj urządzeń (Unit ID)')),
    h('p', { class: 'small muted sc-gap' }, 'Wysyła jedno zapytanie do każdego Unit ID z zakresu. Urządzenie, które odpowie wartością albo wyjątkiem Modbus, istnieje na magistrali.'),
    usBusNote, usForm, usStatus, usList);

  // ── zapis ───────────────────────────────────────────────────
  const writeCard = canWrite ? buildWriteCard() : h('section', { class: 'card', 'aria-labelledby': 'sc-wr-t' },
    h('div', { class: 'card-header' }, h('h3', { class: 'card-title', id: 'sc-wr-t' }, 'Zapis rejestrów')),
    h('p', { class: 'muted small' }, 'Zapis jest wyłączony. Aby zapisywać rejestry Holding i cewki z tej strony, uruchom Modbus Dash z flagą ',
      h('code', null, '--allow-write'), '.'));

  // ── wskazówki ───────────────────────────────────────────────
  const tipsCard = h('section', { class: 'card', 'aria-labelledby': 'sc-tips-t' },
    h('div', { class: 'card-header' }, h('h3', { class: 'card-title', id: 'sc-tips-t' }, 'Wskazówki')),
    h('div', { class: 'prose' }, h('ul', null,
      h('li', null, h('b', null, 'Sztuczka z czajnikiem: '), 'włącz Live, a potem duży odbiornik (czajnik ok. 2 kW). Komórki, które mignęły i wyraźnie urosły, to prąd i moc fazy, do której jest podłączony. Po wyłączeniu wartości wrócą.'),
      h('li', null, h('b', null, 'Zła kolejność bajtów: '), 'zamiast 230,1 widać liczby typu 1,2e-38 albo 4,6e21. Sprawdź sąsiednie kolumny ABCD/CDAB/BADC/DCBA - ta z sensowną wartością to właściwa kolejność. Najczęstsze są ABCD i CDAB.'),
      h('li', null, h('b', null, 'Notacja 30001/40001: '), 'w dokumentacji rejestr 30001 to adres 0 funkcji Input (FC04), a 40001 to adres 0 funkcji Holding (FC03). Wpisz numer z dokumentacji - skaner sam go przeliczy. Adresy hex (0x0024) też działają.'),
      h('li', null, h('b', null, 'Krok: '), '"Pary" czyta wartości 32-bit od adresu początkowego co 2. Jeśli wartości wyglądają na przesunięte o jeden rejestr, zacznij od nieparzystego adresu albo wybierz "Co 1 rejestr".'))));

  const lower = h('div', { class: 'sc-lower' }, unitsCard, h('div', null, writeCard, tipsCard));
  const wrap = h('div', { class: 'v-scanner' },
    pageHeader('Skaner rejestrów'),
    h('div', { class: 'page-content' }, paramsCard, resultsCard, lower));
  root.replaceChildren(wrap);

  // ── zdarzenia pól ───────────────────────────────────────────
  unitIn.addEventListener('input', () => {
    const n = parseIntIn(unitIn.value, 0, 255);
    if (n != null) { prefs.unit = n; save(); }
    markInvalid(unitIn, unitIn.value.trim() !== '' && n == null);
    updateTargetNotes();
  });
  funcSel.addEventListener('change', () => { prefs.func = funcSel.value; save(); updateNotes(); sync(); });
  startIn.addEventListener('input', () => { prefs.start = startIn.value; save(); updateNotes(); });
  endIn.addEventListener('input', () => { prefs.end = endIn.value; save(); updateNotes(); });
  busSel.addEventListener('change', () => { prefs.bus = busSel.value; save(); updateTargetNotes(); });
  for (const [el, key] of [[usFirst, 'first'], [usLast, 'last'], [usTimeout, 'timeout'], [usAddr, 'address']]) {
    el.addEventListener('input', () => { prefs.us[key] = el.value; save(); if (el === usAddr) updateUsNote(); });
  }
  usFunc.addEventListener('change', () => { prefs.us.func = usFunc.value; save(); updateUsNote(); });

  // ── walidacja i notatki pod polami ──────────────────────────
  function updateNotes() {
    const func = funcSel.value;
    const s = parseAddr(startIn.value, func);
    const e = parseAddr(endIn.value, func, { end: true });
    startNote.textContent = s.error ? (startIn.value.trim() ? s.error : '') : s.note;
    markInvalid(startIn, !!s.error && startIn.value.trim() !== '');
    let endText = e.error ? (endIn.value.trim() ? e.error : '') : e.note;
    let endBad = !!e.error && endIn.value.trim() !== '';
    if (!s.error && !e.error) {
      const span = e.address - s.address;
      if (span <= 0) { endText = 'musi być większy niż adres początkowy'; endBad = true; }
      else if (span > MAX_SPAN) { endText = `${span} adresów - maksymalnie ${MAX_SPAN}`; endBad = true; }
      else endText = `${e.note} · ${span} ${plural(span, 'adres', 'adresy', 'adresów')} (${fmtHex(s.address)}-${fmtHex(e.address - 1)})`;
    }
    endNote.textContent = endText;
    markInvalid(endIn, endBad);
    stepField.hidden = isBits(func);
  }

  function updateUsNote() {
    const r = parseAddr(usAddr.value, usFunc.value);
    usAddrNote.textContent = r.error ? (usAddr.value.trim() ? r.error : '') : r.note;
    markInvalid(usAddr, !!r.error && usAddr.value.trim() !== '');
  }

  function busLabel() {
    const o = busSel.selectedOptions[0];
    return o && o.value ? o.textContent : '?';
  }

  function updateTargetNotes() {
    usBusNote.textContent = `Magistrala z parametrów skanowania: ${busLabel()}.`;
    if (writeTarget) writeTarget.textContent = `Cel: ${busLabel()}, Unit ID ${unitIn.value.trim() || '?'} (z parametrów skanowania).`;
  }

  /** Waliduje formularz parametrów; zwraca zapytanie albo null (błędy pokazane przy polach). */
  function buildRequest() {
    const errs = [];
    const func = funcSel.value;
    const unit = parseIntIn(unitIn.value, 0, 255);
    markInvalid(unitIn, unit == null);
    if (unit == null) errs.push('Unit ID musi być liczbą całkowitą 0-255');
    if (!busSel.value) errs.push('wybierz magistralę');
    const s = parseAddr(startIn.value, func);
    const e = parseAddr(endIn.value, func, { end: true });
    markInvalid(startIn, !!s.error);
    markInvalid(endIn, !!e.error);
    if (s.error) errs.push('Od adresu: ' + s.error);
    if (e.error) errs.push('Do adresu: ' + e.error);
    if (!s.error && !e.error) {
      if (e.address <= s.address) { errs.push('adres końcowy musi być większy niż początkowy'); markInvalid(endIn, true); }
      else if (e.address - s.address > MAX_SPAN) { errs.push(`maksymalny zakres skanu to ${MAX_SPAN} adresów`); markInvalid(endIn, true); }
    }
    formErrors.replaceChildren(...errs.map((t) => h('li', null, t)));
    if (errs.length) {
      const bad = paramsFs.querySelector('[aria-invalid="true"]');
      if (bad) bad.focus();
      return null;
    }
    return {
      bus: busSel.value, unit, start: s.address, end: e.address, register_type: func,
      step: isBits(func) ? 1 : prefs.step,
    };
  }

  // ── stan przycisków ─────────────────────────────────────────
  function sync() {
    if (!alive) return;
    const scanning = !!scanCtl;
    paramsFs.disabled = !!live;
    scanBtn.disabled = scanning || !!live || !busesReady;
    scanBtn.setAttribute('aria-busy', String(scanning));
    scanBtn.replaceChildren(...(scanning ? spinnerLabel('Skanowanie...') : ['Skanuj']));
    liveBtn.disabled = !live && (scanning || !busesReady);
    liveBtn.className = 'btn ' + (live ? 'btn-danger' : 'btn-warn');
    liveBtn.setAttribute('aria-pressed', String(!!live));
    liveBtn.replaceChildren(...(live ? [h('span', { class: 'live-dot', 'aria-hidden': 'true' }), 'Stop'] : ['Live']));
    csvBtn.disabled = !(liveLog.length || last);
    csvBtn.textContent = liveLog.length ? `Eksport CSV (${liveLog.length})` : 'Eksport CSV';
    const hinted = !!(last && last.res.registers && last.res.registers.some((r) => r.hint));
    presetBtn.disabled = !hinted || !!presetModal;
    presetBtn.title = hinted ? '' : 'Najpierw zeskanuj rejestry - potrzebny jest co najmniej jeden rozpoznany';
    regControls.hidden = !(table && table.kind === 'regs');
    usBtn.disabled = !!unitCtl || !busesReady;
    usBtn.replaceChildren(...(unitCtl ? spinnerLabel('Szukanie...') : ['Szukaj']));
    usCancel.hidden = !unitCtl;
    if (writeBtn) {
      writeBtn.disabled = !!writeCtl || !busesReady;
      writeBtn.replaceChildren(...(writeCtl ? spinnerLabel('Zapisywanie...') : ['Zapisz...']));
    }
  }

  // ── magistrale ──────────────────────────────────────────────
  async function loadBuses() {
    busesReady = false;
    busSel.disabled = true;
    busNotice.replaceChildren();
    sync();
    busCtl = new AbortController();
    try {
      const signal = busCtl.signal;
      const [buses, devices] = await Promise.all([
        get('/api/buses', { signal }),
        get('/api/devices', { signal }).catch((e) => {
          if (e.name !== 'AbortError') showError(e, 'Lista urządzeń: ');
          return [];
        }),
      ]);
      if (!alive) return;
      const ids = buses.map((b) => b.id);
      busSel.replaceChildren(...buses.map((b) => h('option', { value: b.id },
        `${b.name || b.id} · ${b.describe || b.kind}`)));
      let pick = prefs.bus;
      if (!ids.includes(pick)) pick = devices.length && ids.includes(devices[0].bus) ? devices[0].bus : null;
      if (!pick) pick = ids.includes('default') ? 'default' : ids[0];
      busSel.value = pick || '';
      busSel.disabled = !ids.length;
      busesReady = ids.length > 0;
      if (!ids.length) busNotice.replaceChildren(h('div', { class: 'notice notice-warn' }, 'Brak skonfigurowanych magistral - dodaj połączenie w zakładce Połączenia.'));
      updateTargetNotes();
      sync();
      if (ctx.params && ctx.params.scan === '1' && busesReady) manualScan();
    } catch (e) {
      if (!alive || e.name === 'AbortError') return;
      busSel.replaceChildren(h('option', { value: '' }, 'Błąd ładowania'));
      busNotice.replaceChildren(h('div', { class: 'notice notice-err' },
        'Nie udało się pobrać listy magistral: ', e.message, ' ',
        h('button', { type: 'button', class: 'btn btn-ghost btn-sm', onclick: loadBuses }, 'Spróbuj ponownie')));
      showError(e, 'Magistrale: ');
      sync();
    } finally {
      busCtl = null;
    }
  }

  // ── skanowanie ──────────────────────────────────────────────
  async function manualScan() {
    if (scanCtl || live || !busesReady) return;
    const req = buildRequest();
    if (!req) return;
    scanCtl = new AbortController();
    sync();
    try {
      const res = await post('/api/scan', req, { signal: scanCtl.signal });
      if (!alive) return;
      liveLog = []; liveChars = 0; logReq = null;
      showResult(req, res, false);
    } catch (e) {
      if (!alive || e.name === 'AbortError') return;
      showScanError(e);
    } finally {
      scanCtl = null;
      sync();
    }
  }

  function startLive() {
    if (live || scanCtl || !busesReady) return;
    const req = buildRequest();
    if (!req) return;
    liveLog = []; liveChars = 0; logReq = req;
    const L = { req, ctl: null, count: 0, poller: null, ms: null };
    L.poller = new Poller(async () => {
      if (live !== L) return;
      L.ctl = new AbortController();
      try {
        const res = await post('/api/scan', L.req, { signal: L.ctl.signal });
        if (live !== L) return;
        L.count += 1;
        L.ms = res.duration_ms;
        showResult(L.req, res, true);
      } catch (e) {
        if (live !== L || e.name === 'AbortError') return;
        stopLive();
        showError(e, 'Live zatrzymany: ');
        showScanError(e);
      } finally {
        L.ctl = null;
      }
    }, LIVE_MS);
    live = L;
    L.poller.start(true);
    renderLiveLine();
    sync();
  }

  function stopLive() {
    const L = live;
    if (!L) return;
    live = null;
    L.poller.stop();
    if (L.ctl) L.ctl.abort();
    renderLiveLine(L);
    sync();
  }

  function renderLiveLine(stopped) {
    if (live) {
      liveLine.hidden = false;
      liveLine.replaceChildren(h('span', { class: 'live-dot', 'aria-hidden': 'true' }),
        h('b', null, 'Live'),
        `odczyt #${live.count}`,
        `w logu ${liveLog.length}/${LOG_MAX_SCANS} ${plural(liveLog.length, 'skan', 'skany', 'skanów')}`,
        live.ms != null ? `${fmt(live.ms, 0)} ms` : 'pierwszy odczyt...');
    } else if (stopped && stopped.count) {
      liveLine.hidden = false;
      liveLine.replaceChildren(h('span', { class: 'badge badge-muted' }, 'Live zatrzymany'),
        `${stopped.count} ${plural(stopped.count, 'odczyt', 'odczyty', 'odczytów')}`,
        liveLog.length ? `Eksport CSV zapisze ${liveLog.length} ${plural(liveLog.length, 'skan', 'skany', 'skanów')} z logu` : '');
    } else {
      liveLine.hidden = true;
      liveLine.replaceChildren();
    }
  }

  function showResult(req, res, fromLive) {
    const ts = Date.now();
    last = { req, res, ts };
    if (fromLive) {
      const text = csvLines(res, new Date(ts));
      liveLog.push({ text });
      liveChars += text.length;
      while (liveLog.length > LOG_MAX_SCANS || (liveChars > LOG_MAX_CHARS && liveLog.length > 1)) {
        liveChars -= liveLog.shift().text.length;
      }
    }
    renderSummary(req, res);
    renderResults(req, res);
    resultsMeta.textContent = ` · ${busLabelFor(req.bus)} · Unit ID ${req.unit} · ${FUNCTIONS[req.register_type]} · ${fmtHex(req.start)}-${fmtHex(req.end - 1)}${isBits(req.register_type) ? '' : req.step === 2 ? ' · pary' : ' · co 1'} · ${fmtTime(ts / 1000)}`;
    if (live) renderLiveLine();
    sync();
  }

  function busLabelFor(id) {
    const o = [...busSel.options].find((x) => x.value === id);
    return o ? o.textContent.split(' · ')[0] : id;
  }

  function renderSummary(req, res) {
    const total = req.end - req.start;
    const items = [
      h('span', { class: 'badge ' + (res.readable === total ? 'badge-ok' : 'badge-warn') },
        `Odczytano ${res.readable}/${total}`),
      `Zapytań: ${res.requests}`,
      `Czas: ${fmt(res.duration_ms, 0)} ms`,
    ];
    if (res.registers) {
      const n = res.registers.filter((r) => r.hint).length;
      items.push(h('span', { class: 'badge ' + (n ? 'badge-info' : 'badge-muted') }, `Rozpoznano: ${n}`));
    }
    const out = [h('div', { class: 'status-line' }, items)];
    if (res.unreadable && res.unreadable.length) out.push(unreadableLine(res.unreadable));
    if (res.error) out.push(h('div', { class: 'notice notice-warn' }, 'Skan przerwany: ', res.error));
    summary.replaceChildren(...out);
  }

  function unreadableLine(ranges) {
    const shown = ranges.slice(0, 24).map(rangeText).join(', ');
    const more = ranges.length > 24 ? ` i ${ranges.length - 24} więcej` : '';
    return h('p', { class: 'small sc-unread' }, h('span', { class: 'warn-text' }, 'Nieczytelne: '), shown + more);
  }

  function showScanError(e) {
    const out = [h('div', { class: 'notice notice-err', role: 'alert' },
      h('b', null, e.status === 502 ? 'Brak danych: ' : 'Błąd skanu: '), e.message,
      e.status === 502 || e.status === 0
        ? h('div', { class: 'small muted' }, 'Sprawdź Unit ID, funkcję (Input/Holding), zakres adresów i parametry magistrali.')
        : null,
      e.errors && e.errors.length ? h('ul', { class: 'errors' }, e.errors.map((t) => h('li', null, t))) : null)];
    const ur = e.data && Array.isArray(e.data.unreadable) ? e.data.unreadable : [];
    if (ur.length) out.push(unreadableLine(ur));
    if (last) out.push(h('p', { class: 'small muted' }, 'Poniżej wyniki poprzedniego udanego skanu.'));
    summary.replaceChildren(...out);
  }

  // ── tabela / siatka wyników ─────────────────────────────────
  function renderResults(req, res) {
    const kind = res.bits ? 'bits' : 'regs';
    const items = res.bits || res.registers || [];
    const key = [kind, req.bus, req.unit, req.register_type, req.start, req.end, req.step, items.length,
      items.length ? items[0].address : ''].join('|');
    const flashList = table && table.key === key ? [] : null;
    if (!flashList) {
      table = kind === 'bits' ? buildBits(items) : buildRegs(items, req.register_type);
      table.key = key;
      resultsBody.replaceChildren(table.el);
      applyOnly();
    }
    if (kind === 'bits') items.forEach((b, i) => updateBit(table.cells[i], b, flashList));
    else items.forEach((r, i) => updateRow(table.cells[i], r, flashList));
    if (flashList && flashList.length) {
      flashList.forEach((el) => el.classList.remove('flash'));
      void table.el.offsetWidth; // jeden reflow - restart animacji dla wszystkich zmienionych komórek
      flashList.forEach((el) => el.classList.add('flash'));
    }
  }

  function headCell(col) {
    return [h('span', { class: 'sc-sub' }, col.head[0]), col.head[1]];
  }

  function buildRegs(rows, func) {
    const cols = VIEWS[prefs.view];
    const heads = cols.map((c) => h('th', { class: 'right', scope: 'col' }, headCell(c)));
    const tbody = h('tbody');
    const cells = rows.map((r) => {
      const nota = notation(func, r.address);
      const c = {
        addr: h('td', { class: 'addr', title: `Adres ${r.address}${nota ? ' · ' + nota : ''}` },
          fmtHex(r.address), h('span', { class: 'sc-sub' }, String(r.address))),
        raw: h('td', { class: 'raw mono' }),
        u16: h('td', { class: 'num' }),
        i16: h('td', { class: 'num' }),
        v: cols.map(() => h('td', { class: 'num' })),
        hint: h('td', { class: 'sc-hint' }),
      };
      c.tr = h('tr', null, c.addr, c.raw, c.u16, c.i16, c.v, c.hint);
      tbody.append(c.tr);
      return c;
    });
    const el = h('div', { class: 'table-wrap tall' },
      h('table', { class: 'tbl sc-table' },
        h('thead', null, h('tr', null,
          h('th', { scope: 'col' }, 'Adres'), h('th', { scope: 'col' }, 'Raw'),
          h('th', { class: 'right', scope: 'col' }, 'u16'), h('th', { class: 'right', scope: 'col' }, 'i16'),
          heads, h('th', { scope: 'col' }, 'Identyfikacja'))),
        tbody));
    return { kind: 'regs', el, heads, cells };
  }

  function setCell(td, text, flashList) {
    if (td.textContent === text) return;
    td.textContent = text;
    td.classList.toggle('muted', text === '-' || text === '---- ----');
    if (flashList) flashList.push(td);
  }

  function colText(col, r) {
    const v = col.get(r);
    return col.float ? fmtNum(v) : fmtInt(v);
  }

  function updateRow(c, r, flashList) {
    setCell(c.raw, r.raw_hex || '-', flashList);
    setCell(c.u16, fmtInt(r.u16), flashList);
    setCell(c.i16, fmtInt(r.i16), flashList);
    VIEWS[prefs.view].forEach((col, j) => setCell(c.v[j], colText(col, r), flashList));
    const hk = r.hint ? JSON.stringify([r.hint.guess, r.hint.value, r.hint.type, r.hint.byte_order, r.hint.scale]) : '';
    if (c.hk !== hk) {
      const first = c.hk === undefined;
      c.hk = hk;
      c.hint.replaceChildren(...hintNodes(r.hint));
      if (flashList && !first) flashList.push(c.hint);
    }
    c.tr.classList.toggle('dim', !r.hint);
  }

  function hintNodes(hint) {
    if (!hint) return [h('span', { class: 'muted' }, '-')];
    const cls = GUESS_CLASS[hint.guess] || (GROUPS[hint.group] ? hint.group : 'other');
    const val = fmtNum(hint.value) + (hint.unit ? ' ' + hint.unit : '');
    const meta = [hint.type, hint.byte_order, hint.scale != null ? '×' + dec(String(hint.scale)) : null]
      .filter(Boolean).join(' / ');
    const alts = (hint.alternatives || []).map((a) => `${a.label} ${Math.round((a.score || 0) * 100)}%`).join(', ');
    const title = `Pewność ${Math.round((hint.score || 0) * 100)}%` + (alts ? `. Inne możliwości: ${alts}` : '');
    return [h('span', { class: `guess g-${cls}`, title }, `${hint.label}: ${val}`), h('span', { class: 'sc-sub' }, meta)];
  }

  function buildBits(bits) {
    const cells = bits.map((b) => {
      const v = h('b');
      const el = h('div', { class: 'sc-bit', role: 'listitem', title: `Adres ${b.address}` },
        h('span', null, fmtHex(b.address)), v);
      return { el, v };
    });
    const el = h('div', { class: 'sc-bits', role: 'list', 'aria-label': 'Stany bitów' }, cells.map((c) => c.el));
    return { kind: 'bits', el, cells };
  }

  function updateBit(c, b, flashList) {
    const t = b.value == null ? '?' : b.value ? '1' : '0';
    if (c.v.textContent === t) return;
    c.v.textContent = t;
    c.el.classList.toggle('on', t === '1');
    c.el.classList.toggle('unk', t === '?');
    if (flashList) flashList.push(c.el);
  }

  function applyView() {
    if (!table || table.kind !== 'regs' || !last || !last.res.registers) return;
    const cols = VIEWS[prefs.view];
    table.heads.forEach((th, i) => th.replaceChildren(...headCell(cols[i])));
    last.res.registers.forEach((r, i) => cols.forEach((col, j) => setCell(table.cells[i].v[j], colText(col, r), null)));
  }

  function applyOnly() {
    if (table && table.kind === 'regs') table.el.classList.toggle('sc-only', !!prefs.onlyHints);
  }

  // ── CSV ─────────────────────────────────────────────────────
  function csvNum(v) {
    if (v == null) return '';
    if (typeof v === 'number') return Number.isFinite(v) ? dec(String(v)) : '';
    return csvCell(v);
  }

  function hintText(hint) {
    if (!hint) return '';
    const meta = [hint.type, hint.byte_order, hint.scale != null ? '×' + dec(String(hint.scale)) : null].filter(Boolean).join(' ');
    return `${hint.label}: ${fmtNum(hint.value)}${hint.unit ? ' ' + hint.unit : ''} (${meta})`;
  }

  function csvLines(res, date) {
    const t = csvCell(localStamp(date));
    if (res.bits) {
      return res.bits.map((b) => [t, b.address, fmtHex(b.address), b.value == null ? '' : b.value ? 1 : 0,
        '', '', '', '', '', '', '', '', '', '', ''].join(';')).join('\r\n');
    }
    return (res.registers || []).map((r) => {
      const d = r.decoded || {};
      return [t, r.address, csvCell(r.address_hex || fmtHex(r.address)), csvCell(r.raw_hex), csvNum(r.u16), csvNum(r.i16),
        ...ORDERS.map((o) => csvNum(d.float32 && d.float32[o])),
        csvNum(d.int32 && d.int32.ABCD), csvNum(d.int32 && d.int32.CDAB),
        csvNum(d.uint32 && d.uint32.ABCD), csvNum(d.uint32 && d.uint32.CDAB),
        csvCell(hintText(r.hint))].join(';');
    }).join('\r\n');
  }

  function exportCsv() {
    let body, n, req;
    if (liveLog.length) {
      body = liveLog.map((e) => e.text).join('\r\n');
      n = liveLog.length;
      req = logReq || (last && last.req);
    } else if (last) {
      body = csvLines(last.res, new Date(last.ts));
      n = 1;
      req = last.req;
    } else {
      toast('Brak danych do eksportu - najpierw uruchom skan', 'err');
      return;
    }
    const d = new Date();
    const stamp = `${d.getFullYear()}${pad2(d.getMonth() + 1)}${pad2(d.getDate())}-${pad2(d.getHours())}${pad2(d.getMinutes())}${pad2(d.getSeconds())}`;
    const name = `skan_${req.bus}_u${req.unit}_${req.register_type}_${stamp}.csv`.replace(/[^\w.-]/g, '_');
    download(name, '﻿' + CSV_HEAD.join(';') + '\r\n' + body + '\r\n', 'text/csv');
    toast(`Wyeksportowano ${n} ${plural(n, 'skan', 'skany', 'skanów')} do ${name}`);
  }

  // ── szkic presetu ───────────────────────────────────────────
  function createPreset() {
    if (presetModal || !last || !last.res.registers) return;
    const rows = last.res.registers.filter((r) => r.hint);
    if (!rows.length) { toast('Brak rozpoznanych rejestrów - nie ma z czego utworzyć presetu', 'err'); return; }
    const counts = {};
    for (const r of rows) counts[r.hint.byte_order] = (counts[r.hint.byte_order] || 0) + 1;
    const best = ORDERS.reduce((b, o) => ((counts[o] || 0) > (counts[b] || 0) ? o : b), 'ABCD');
    const orderSel = select(ORDERS.map((o) => [o, `${ORDER_LABELS[o]} - ${counts[o] || 0} ${plural(counts[o] || 0, 'podpowiedź', 'podpowiedzi', 'podpowiedzi')}`]), best);
    const nameIn = h('input', { type: 'text', maxlength: 80, autocomplete: 'off', placeholder: 'np. Licznik w garażu' });
    const errBox = h('ul', { class: 'errors', role: 'alert' });
    const req = last.req;
    let ctl = null;
    let busyP = false;
    const m = modal({
      title: 'Utwórz preset ze skanu',
      subtitle: `${rows.length} ${plural(rows.length, 'rozpoznany rejestr', 'rozpoznane rejestry', 'rozpoznanych rejestrów')}. Szkic otworzy się w edytorze presetów - nic nie zostanie zapisane bez Twojej zgody.`,
      body: [h('div', { class: 'form-grid' },
        field('Kolejność bajtów', orderSel, 'domyślnie najczęstsza wśród podpowiedzi'),
        field('Nazwa (opcjonalnie)', nameIn)), errBox],
      actions: [{ label: 'Anuluj' }, { label: 'Utwórz szkic', class: 'btn-primary', onClick: (close) => submit(close) }],
      onClose: () => {
        if (ctl) ctl.abort();
        presetModal = null;
        sync();
      },
    });
    presetModal = m;
    sync();
    const okBtn = m.el.querySelector('.modal-foot .btn-primary');
    nameIn.addEventListener('keydown', (e) => { if (e.key === 'Enter') { e.preventDefault(); submit(m.close); } });

    async function submit(close) {
      if (busyP) return;
      busyP = true;
      okBtn.disabled = true;
      okBtn.replaceChildren(...spinnerLabel('Tworzenie...'));
      errBox.replaceChildren();
      ctl = new AbortController();
      try {
        const name = nameIn.value.trim();
        const preset = await post('/api/scan/preset', {
          registers: rows, byte_order: orderSel.value, register_type: req.register_type, name: name || undefined,
        }, { signal: ctl.signal });
        ctl = null;
        close();
        if (!alive) return;
        ctx.navigate('presets', { draft: '1' }, { draft: preset });
      } catch (e) {
        ctl = null;
        if (e.name === 'AbortError') return;
        errBox.replaceChildren(...[e.message, ...(e.errors || [])].map((t) => h('li', null, t)));
        busyP = false;
        okBtn.disabled = false;
        okBtn.textContent = 'Utwórz szkic';
      }
    }
  }

  // ── wyszukiwanie Unit ID ────────────────────────────────────
  async function runUnitScan() {
    if (unitCtl || !busesReady) return;
    const errs = [];
    const first = parseIntIn(usFirst.value, 0, 255);
    const lastU = parseIntIn(usLast.value, 0, 255);
    const timeout = parseFloatIn(usTimeout.value, 0.05, 5);
    const addr = parseAddr(usAddr.value, usFunc.value);
    markInvalid(usFirst, first == null);
    markInvalid(usLast, lastU == null || (first != null && lastU < first));
    markInvalid(usTimeout, timeout == null);
    markInvalid(usAddr, !!addr.error);
    if (first == null) errs.push('Od Unit ID: liczba całkowita 0-255');
    if (lastU == null) errs.push('Do Unit ID: liczba całkowita 0-255');
    else if (first != null && lastU < first) errs.push('Do Unit ID musi być większy lub równy Od Unit ID');
    if (timeout == null) errs.push('Timeout: liczba 0,05-5 s');
    if (addr.error) errs.push('Adres testowy: ' + addr.error);
    usErrors.replaceChildren(...errs.map((t) => h('li', null, t)));
    if (errs.length) return;
    const body = { bus: busSel.value, first, last: lastU, register_type: usFunc.value, address: addr.address, count: 1, timeout };
    const ctl = new AbortController();
    unitCtl = ctl;
    let lastJob = null;
    usStatus.hidden = false;
    usList.replaceChildren();
    setUnitProgress(0, lastU - first + 1, 'Uruchamianie...');
    sync();
    try {
      const job = await runJob(post('/api/scan/units', body, { signal: ctl.signal }), (j) => {
        if (!alive || unitCtl !== ctl) return;
        lastJob = j;
        setUnitProgress(j.progress.done, j.progress.total, j.progress.message);
        renderUnits(j.results || [], body, false);
      }, { signal: ctl.signal });
      if (!alive) return;
      const found = (job.result && job.result.found) || job.results || [];
      renderUnits(found, body, true);
      if (job.state === 'cancelled') {
        usMsg.textContent = 'Anulowano.';
      } else {
        usMsg.textContent = `Gotowe: ${found.length} ${plural(found.length, 'urządzenie', 'urządzenia', 'urządzeń')} (Unit ID ${first}-${lastU}).`;
        setUnitProgress(1, 1, usMsg.textContent);
        toast(`Znaleziono ${found.length} ${plural(found.length, 'urządzenie', 'urządzenia', 'urządzeń')}`, found.length ? 'ok' : 'info');
      }
    } catch (e) {
      if (!alive) return;
      if (e.name === 'AbortError') {
        usMsg.textContent = 'Anulowano - poniżej urządzenia znalezione do tej pory.';
        renderUnits((lastJob && lastJob.results) || [], body, true);
      } else {
        usMsg.textContent = '';
        usStatus.hidden = true;
        usList.replaceChildren(h('div', { class: 'notice notice-err', role: 'alert' }, 'Wyszukiwanie nie powiodło się: ', e.message));
        showError(e, 'Szukanie urządzeń: ');
      }
    } finally {
      if (unitCtl === ctl) unitCtl = null;
      sync();
    }
  }

  function setUnitProgress(done, total, message) {
    const pct = total ? Math.round((done / total) * 100) : 0;
    usBar.style.width = pct + '%';
    usProgress.setAttribute('aria-valuenow', String(pct));
    usMsg.textContent = message ? `${message} (${done}/${total})` : `${done}/${total}`;
  }

  function renderUnits(found, body, final) {
    if (!found.length) {
      usList.replaceChildren(final ? h('p', { class: 'muted small sc-gap' }, 'Nie znaleziono urządzeń. Sprawdź parametry magistrali (prędkość, parzystość), okablowanie A/B i zwiększ timeout.') : '');
      return;
    }
    usList.replaceChildren(...found.map((u) => h('div', { class: 'list-item sc-unit' },
      h('div', null,
        h('h4', null, `Unit ID ${u.unit} `, u.exception
          ? h('span', { class: 'badge badge-warn' }, 'wyjątek')
          : h('span', { class: 'badge badge-ok' }, 'odpowiada')),
        h('p', null, u.exception
          ? `Odpowiedź wyjątkiem: ${u.exception}`
          : `${body.register_type === 'holding' ? 'Holding' : 'Input'} ${body.address}: ${(u.registers || []).map((v) => `${v} (${fmtHex(v)})`).join(', ')}`)),
      h('div', { class: 'sc-unit-actions' },
        h('button', { type: 'button', class: 'btn btn-ghost btn-sm', onclick: () => scanThis(u.unit, body.bus) }, 'Skanuj ten'),
        h('button', { type: 'button', class: 'btn btn-primary btn-sm', onclick: () => ctx.navigate('devices', { new: 1, bus: body.bus, unit: u.unit }) }, 'Dodaj jako urządzenie')))));
  }

  function scanThis(unit, bus) {
    if (live) stopLive();
    unitIn.value = String(unit);
    markInvalid(unitIn, false);
    prefs.unit = unit;
    if ([...busSel.options].some((o) => o.value === bus)) { busSel.value = bus; prefs.bus = bus; }
    save();
    updateTargetNotes();
    paramsCard.scrollIntoView({ behavior: 'smooth', block: 'start' });
    unitIn.focus({ preventScroll: true });
    manualScan();
  }

  // ── zapis ───────────────────────────────────────────────────
  let writeBtn = null;
  let writeTarget = null;
  function buildWriteCard() {
    const wFunc = select([['holding', 'Holding Register (FC06 / FC16)'], ['coil', 'Coil (FC05)']], prefs.wr.func);
    const wAddr = h('input', { type: 'text', value: prefs.wr.address, autocomplete: 'off', spellcheck: false, placeholder: 'np. 0, 0x10, 40001' });
    const wAddrNote = h('span', { 'aria-live': 'polite' });
    const wVals = h('input', { type: 'text', value: prefs.wr.values, autocomplete: 'off', spellcheck: false, placeholder: 'np. 1234 albo 1, 2, 0x10' });
    const wValsNote = h('span', { 'aria-live': 'polite' });
    const wCoil = select([['1', '1 - włącz (ON)'], ['0', '0 - wyłącz (OFF)']], prefs.wr.coil);
    const valsField = field('Wartość (kilka po przecinku = FC16)', wVals, wValsNote);
    const coilField = field('Stan cewki', wCoil);
    const wErrors = h('ul', { class: 'errors', role: 'alert' });
    const wResult = h('div', { 'aria-live': 'polite' });
    writeTarget = h('p', { class: 'small muted sc-gap' });
    writeBtn = h('button', { type: 'submit', class: 'btn btn-danger' }, 'Zapisz...');

    const updateW = () => {
      const isCoil = wFunc.value === 'coil';
      valsField.hidden = isCoil;
      coilField.hidden = !isCoil;
      const a = parseAddr(wAddr.value, wFunc.value);
      wAddrNote.textContent = a.error ? (wAddr.value.trim() ? a.error : '') : a.note;
      markInvalid(wAddr, !!a.error && wAddr.value.trim() !== '');
      if (!isCoil) {
        const v = parseWriteValues(wVals.value);
        wValsNote.textContent = v.error ? (wVals.value.trim() ? v.error : 'liczby 0-65535, hex 0x.., ujemne jako int16')
          : `${v.values.length} ${plural(v.values.length, 'rejestr', 'rejestry', 'rejestrów')}: ${v.values.slice(0, 8).map((x) => fmtHex(x)).join(' ')}${v.values.length > 8 ? ' ...' : ''}`;
        markInvalid(wVals, !!v.error && wVals.value.trim() !== '');
      }
    };
    wFunc.addEventListener('change', () => { prefs.wr.func = wFunc.value; save(); updateW(); });
    wAddr.addEventListener('input', () => { prefs.wr.address = wAddr.value; save(); updateW(); });
    wVals.addEventListener('input', () => { prefs.wr.values = wVals.value; save(); updateW(); });
    wCoil.addEventListener('change', () => { prefs.wr.coil = wCoil.value; save(); });

    async function submitWrite() {
      if (writeCtl || !busesReady) return;
      const errs = [];
      const unit = parseIntIn(unitIn.value, 0, 255);
      if (unit == null) errs.push('Unit ID w parametrach skanowania musi być liczbą 0-255');
      const func = wFunc.value;
      const a = parseAddr(wAddr.value, func);
      markInvalid(wAddr, !!a.error);
      if (a.error) errs.push('Adres: ' + a.error);
      let values = null;
      if (func === 'holding') {
        const v = parseWriteValues(wVals.value);
        markInvalid(wVals, !!v.error);
        if (v.error) errs.push('Wartość: ' + v.error);
        else values = v.values;
        if (values && !a.error && a.address + values.length - 1 > 65535) errs.push('zapis wychodzi poza adres 65535');
      }
      wErrors.replaceChildren(...errs.map((t) => h('li', null, t)));
      if (errs.length) return;
      const bus = busSel.value;
      const target = `Magistrala: ${busLabel()}, Unit ID: ${unit}.`;
      let what;
      const body = { bus, unit, function: func, address: a.address };
      if (func === 'coil') {
        const on = wCoil.value === '1';
        body.value = on;
        what = `Coil (FC05), adres ${a.address} (${fmtHex(a.address)}): ${on ? '1 (ON)' : '0 (OFF)'}.`;
      } else if (values.length === 1) {
        body.value = values[0];
        what = `Holding Register (FC06), adres ${a.address} (${fmtHex(a.address)}): ${values[0]} (${fmtHex(values[0])}).`;
      } else {
        body.values = values;
        const endA = a.address + values.length - 1;
        what = `Holding Registers (FC16), adresy ${a.address}-${endA} (${fmtHex(a.address)}-${fmtHex(endA)}): ${values.join(', ')}.`;
      }
      const ok = await confirmDialog(`${target} ${what} Zapis zmienia ustawienia urządzenia - upewnij się, że wiesz, co oznacza ten rejestr.`,
        { title: 'Potwierdź zapis Modbus', okLabel: 'Zapisz', danger: true });
      if (!ok || !alive || writeCtl) return;
      const ctl = new AbortController();
      writeCtl = ctl;
      sync();
      try {
        await post('/api/write', body, { signal: ctl.signal });
        if (!alive) return;
        wResult.replaceChildren(h('div', { class: 'notice notice-info' },
          h('span', { class: 'badge badge-ok' }, 'Zapisano'), ` ${fmtTime(Date.now() / 1000)} · ${what}`));
        toast('Zapisano do urządzenia');
      } catch (e) {
        if (!alive || e.name === 'AbortError') return;
        wResult.replaceChildren(h('div', { class: 'notice notice-err', role: 'alert' }, h('b', null, 'Zapis nieudany: '), e.message));
        showError(e, 'Zapis: ');
      } finally {
        if (writeCtl === ctl) writeCtl = null;
        sync();
      }
    }

    const form = h('form', { novalidate: true, onsubmit: (e) => { e.preventDefault(); submitWrite(); } },
      h('div', { class: 'form-grid sc-form' }, field('Funkcja', wFunc), field('Adres', wAddr, wAddrNote), valsField, coilField),
      wErrors, h('div', { class: 'sc-actions' }, writeBtn), wResult);
    updateW();
    return h('section', { class: 'card', 'aria-labelledby': 'sc-wr-t' },
      h('div', { class: 'card-header' }, h('h3', { class: 'card-title', id: 'sc-wr-t' }, 'Zapis rejestrów'),
        h('span', { class: 'badge badge-warn' }, 'zapis włączony')),
      writeTarget, form);
  }

  // ── start ───────────────────────────────────────────────────
  updateNotes();
  updateUsNote();
  updateTargetNotes();
  sync();
  loadBuses();

  return {
    unmount() {
      alive = false;
      if (live) { live.poller.stop(); if (live.ctl) live.ctl.abort(); live = null; }
      for (const c of [busCtl, scanCtl, unitCtl, writeCtl]) if (c) c.abort();
      if (presetModal) presetModal.close();
    },
  };
}
