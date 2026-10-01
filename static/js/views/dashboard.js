// Widok: Dashboard - wartości na żywo, wykresy z historią, szczegóły wielkości.

import {
  h, mount as fill, get, enc, Poller, store, modal, showError, toast, fmt, fmtAge,
  GROUPS, GROUP_ORDER, colorFor, stateBadge, emptyState, select,
} from '../core.js';
import { AreaChart } from '../charts.js';

const RANGES = [
  [300, '5 min'], [900, '15 min'], [3600, '1 h'],
  [6 * 3600, '6 h'], [86400, '24 h'], [7 * 86400, '7 dni'], [30 * 86400, '30 dni'],
];
const MEMORY_RANGE = 3600;       // do 1 h - pełna rozdzielczość z pamięci
const MAX_DRAW_POINTS = 600;     // tyle punktów rysujemy na małym wykresie
const OVERVIEW = [
  ['power_total', 'Moc czynna'], ['power_l1', 'Moc czynna'], ['energy_import', 'Energia pobrana'],
  ['energy_export', 'Energia oddana'], ['energy_total', 'Energia'], ['frequency', 'Częstotliwość'],
  ['pf_total', 'cos φ'],
];

export function mount(root, ctx) {
  const state = {
    devices: [],
    deviceId: null,
    mode: store.get('dash.mode', 'cards'),
    range: store.get('dash.range', 900),
    data: null,          // ostatnia odpowiedź /values
    series: {},          // key -> [[ts, v], ...]
    lastTs: 0,
    built: null,         // sygnatura zbudowanego układu
    els: {},             // key -> {val, card}
    charts: {},          // key -> AreaChart
    detail: null,        // {key, chart, modal}
    historyAt: 0,
    loadingHistory: false,
  };
  const historyAvailable = !!(ctx.info && ctx.info.features && ctx.info.features.history);

  // ── szkielet ──────────────────────────────────────────────
  const devSel = h('select', { 'aria-label': 'Urządzenie', onchange: () => selectDevice(devSel.value) });
  const modeBtns = h('div', { class: 'segmented', role: 'group', 'aria-label': 'Tryb' },
    h('button', { type: 'button', dataset: { mode: 'cards' }, onclick: () => setMode('cards') }, 'Karty'),
    h('button', { type: 'button', dataset: { mode: 'charts' }, onclick: () => setMode('charts') }, 'Wykresy'));
  const rangeSel = select(RANGES.filter(([s]) => s <= MEMORY_RANGE || historyAvailable).map(([s, l]) => [s, l]),
    state.range, { 'aria-label': 'Zakres historii', onchange: () => setRange(+rangeSel.value) });
  const csvBtn = h('button', { class: 'btn btn-ghost btn-sm', type: 'button', onclick: exportCsv, title: 'Eksport historii do CSV' }, 'CSV');
  const header = h('div', { class: 'page-header' },
    h('h2', null, 'Dashboard'),
    h('div', { class: 'actions' }, devSel, modeBtns, rangeSel, csvBtn));
  const statusLine = h('div', { class: 'status-line' });
  const notice = h('div');
  const overview = h('div', { class: 'big-stats' });
  const body = h('div');
  const content = h('div', { class: 'page-content' }, statusLine, notice, overview, body);
  fill(root, header, content);

  // ── urządzenia ────────────────────────────────────────────
  async function loadDevices() {
    try {
      state.devices = await get('/api/devices');
    } catch (e) {
      fill(body, h('div', { class: 'notice notice-err' }, `Nie udało się pobrać listy urządzeń: ${e.message}`));
      return;
    }
    if (!state.devices.length) {
      header.querySelector('.actions').hidden = true;
      fill(body, emptyState('Brak skonfigurowanych urządzeń. Dodaj licznik albo znajdź go skanerem.', [
        { label: 'Dodaj urządzenie', href: '#devices?new=1' },
        { label: 'Skaner rejestrów', href: '#scanner', class: 'btn-ghost' },
      ]));
      return;
    }
    fill(devSel, state.devices.map((d) => h('option', { value: d.id },
      d.name + (d.enabled ? '' : ' (wyłączone)'))));
    const wanted = ctx.params.device || store.get('dash.device');
    const first = state.devices.find((d) => d.id === wanted) || state.devices.find((d) => d.enabled) || state.devices[0];
    devSel.value = first.id;
    selectDevice(first.id);
  }

  function selectDevice(id) {
    state.deviceId = id;
    store.set('dash.device', id);
    resetLayout();
    poller.trigger();
    loadHistory();
  }

  function resetLayout() {
    destroyCharts();
    state.series = {};
    state.lastTs = 0;
    state.built = null;
    state.data = null;
    fill(overview);
    fill(body, h('div', { class: 'empty' }, h('span', { class: 'spinner lg' }), h('p', null, 'Łączenie...')));
  }

  function setMode(mode) {
    state.mode = mode;
    store.set('dash.mode', mode);
    updateModeButtons();
    state.built = null;
    if (state.data) render(state.data);
  }
  function updateModeButtons() {
    for (const b of modeBtns.children) b.setAttribute('aria-pressed', String(b.dataset.mode === state.mode));
    rangeSel.hidden = state.mode !== 'charts' && !state.detail;
  }
  function setRange(sec) {
    state.range = sec;
    store.set('dash.range', sec);
    state.series = {};
    state.lastTs = 0;
    loadHistory();
  }

  // ── dane ──────────────────────────────────────────────────
  const poller = new Poller(async () => {
    if (!state.deviceId) return;
    let data;
    try {
      data = await get(`/api/devices/${enc(state.deviceId)}/values`);
    } catch (e) {
      renderStatus(null, e.message);
      return;
    }
    state.data = data;
    const ts = data.status && data.status.ts;
    if (ts && ts > state.lastTs && data.status.state === 'ok') {
      if (state.range <= MEMORY_RANGE && state.lastTs) appendSample(ts, data.values);
      state.lastTs = ts;
    }
    render(data);
    // długie zakresy (SQLite) odświeżamy rzadko
    if (state.range > MEMORY_RANGE && Date.now() - state.historyAt > 60000) loadHistory();
  }, 1000);

  function appendSample(ts, values) {
    const cutoff = ts - state.range;
    for (const [k, v] of Object.entries(values)) {
      const s = state.series[k] || (state.series[k] = []);
      s.push([ts, v]);
      while (s.length && s[0][0] < cutoff) s.shift();
    }
  }

  async function loadHistory() {
    if (!state.deviceId || state.loadingHistory) return;
    if (state.mode !== 'charts' && !state.detail) { state.historyAt = 0; return; }
    state.loadingHistory = true;
    const id = state.deviceId;
    try {
      const res = await get(`/api/devices/${enc(id)}/history?seconds=${state.range}&max_points=800`);
      if (id !== state.deviceId) return;
      const series = {};
      res.keys.forEach((k, i) => { series[k] = res.points.map((p) => [p[0], p[i + 1]]); });
      state.series = series;
      if (res.points.length) state.lastTs = Math.max(state.lastTs, res.points[res.points.length - 1][0]);
      state.historyAt = Date.now();
      redrawCharts();
    } catch (e) {
      showError(e, 'Historia: ');
    } finally {
      state.loadingHistory = false;
    }
  }

  // ── renderowanie ──────────────────────────────────────────
  function renderStatus(data, error) {
    if (!data) {
      fill(statusLine, stateBadge('error'), h('span', null, error || 'Brak danych'));
      return;
    }
    const st = data.status || {};
    const parts = [stateBadge(st.state)];
    if (data.preset) parts.push(h('span', null, [data.preset.manufacturer, data.preset.model].filter(Boolean).join(' ') || data.preset.name));
    parts.push(h('span', null, `Unit ID ${data.device.unit}`));
    if (st.age != null) parts.push(h('span', null, `aktualizacja ${fmtAge(st.age)}`));
    if (st.duration_ms != null) parts.push(h('span', null, `odczyt ${Math.round(st.duration_ms)} ms`));
    if (st.failures) parts.push(h('span', null, `błędy: ${st.failures}/${st.polls}`));
    fill(statusLine, parts);
    let msg = null;
    if (st.state === 'no_preset') {
      msg = h('div', { class: 'notice notice-warn' }, st.error || 'Urządzenie nie ma przypisanego presetu. ',
        h('a', { href: `#devices?edit=${enc(data.device.id)}` }, 'Wybierz preset'));
    } else if (st.state === 'disabled') {
      msg = h('div', { class: 'notice notice-info' }, 'Urządzenie jest wyłączone. ',
        h('a', { href: `#devices?edit=${enc(data.device.id)}` }, 'Włącz je w zakładce Urządzenia'));
    } else if ((st.state === 'error' || st.state === 'stale') && st.error) {
      msg = h('div', { class: 'notice notice-err' }, `Ostatni odczyt nieudany: ${st.error}`);
    } else if (st.state === 'stale') {
      msg = h('div', { class: 'notice notice-warn' }, 'Dane są nieaktualne - urządzenie nie odpowiada od dłuższego czasu.');
    }
    fill(notice, msg);
    content.classList.toggle('stale', st.state === 'error' || st.state === 'stale');
  }

  function render(data) {
    renderStatus(data);
    const keys = Object.keys(data.meta);
    if (!keys.length) {
      if (state.built !== 'empty') {
        fill(overview);
        fill(body, data.status.state === 'waiting'
          ? h('div', { class: 'empty' }, h('span', { class: 'spinner lg' }), h('p', null, 'Oczekiwanie na pierwszy odczyt...'))
          : null);
        state.built = 'empty';
      }
      return;
    }
    const sig = state.mode + '|' + keys.join(',');
    if (state.built !== sig) build(data, keys, sig);
    else update(data);
  }

  function grouped(data) {
    const groups = {};
    for (const [k, m] of Object.entries(data.meta)) {
      const g = GROUPS[m.group] ? m.group : 'other';
      (groups[g] || (groups[g] = [])).push({ key: k, ...m });
    }
    return groups;
  }

  function build(data, keys, sig) {
    destroyCharts();
    state.els = {};
    // przegląd
    const ov = [];
    const used = new Set();
    for (const [k, label] of OVERVIEW) {
      if (ov.length >= 4 || !(k in data.meta)) continue;
      if (label === 'Moc czynna' && used.has('Moc czynna')) continue;
      used.add(label);
      const m = data.meta[k];
      const val = h('span', { class: 'val' });
      ov.push(h('div', { class: 'big-stat' }, h('div', { class: 'lbl' }, label), val));
      state.els['ov:' + k] = { val, meta: m };
    }
    fill(overview, ov);
    overview.hidden = ov.length < 2;

    const groups = grouped(data);
    const out = [];
    for (const g of GROUP_ORDER) {
      const items = groups[g];
      if (!items) continue;
      const meta = GROUPS[g];
      out.push(h('div', { class: 'group-header' }, meta.label));
      const cols = state.mode === 'charts'
        ? (items.length >= 3 ? 'grid-3' : `grid-${items.length}`)
        : meta.cols;
      const grid = h('div', { class: `grid ${cols}${state.mode === 'charts' ? ' charts' : ''}` });
      for (const it of items) {
        const color = colorFor(it.key, it.label, it.group);
        const val = h('span', { class: 'val' });
        if (state.mode === 'charts') {
          const canvas = h('canvas', { 'aria-hidden': 'true' });
          const card = h('div', {
            class: 'chart-card', tabindex: '0', role: 'button', 'aria-label': `${it.label} - szczegóły`,
            onclick: () => openDetail(it.key), onkeydown: (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); openDetail(it.key); } },
          },
          h('div', { class: 'head' },
            h('div', null, h('div', { class: 'lbl' }, it.label), h('div', { class: 'sub' }, it.unit ? `Wartość w ${it.unit}` : '')),
            val),
          canvas);
          grid.append(card);
          state.els[it.key] = { val, card, meta: it, unitInline: false };
          state.charts[it.key] = new AreaChart(canvas, { color, decimals: it.decimals, unit: it.unit });
        } else {
          const card = h('div', {
            class: 'val-card', tabindex: '0', role: 'button', 'aria-label': `${it.label} - wykres`,
            onclick: () => openDetail(it.key), onkeydown: (e) => { if (e.key === 'Enter') openDetail(it.key); },
            style: { cursor: 'pointer' },
          },
          h('div', { class: 'acc', style: { background: color } }),
          h('div', { class: 'lbl', title: it.label }, it.label),
          h('div', null, val, h('span', { class: 'unt' }, it.unit)));
          grid.append(card);
          state.els[it.key] = { val, card, meta: it };
        }
      }
      out.push(grid);
    }
    fill(body, out);
    state.built = sig;
    update(data);
    if (state.mode === 'charts') {
      if (Object.keys(state.series).length) redrawCharts(); else loadHistory();
    }
  }

  function update(data) {
    for (const [k, el] of Object.entries(state.els)) {
      const key = k.startsWith('ov:') ? k.slice(3) : k;
      const v = data.values[key];
      const m = el.meta;
      if (k.startsWith('ov:')) {
        el.val.replaceChildren(fmt(v, m.decimals), h('span', { class: 'unt' }, m.unit));
        continue;
      }
      el.val.textContent = state.mode === 'charts' && m.unit ? `${fmt(v, m.decimals)} ${m.unit}` : fmt(v, m.decimals);
      const err = data.errors && data.errors[key];
      el.card.classList.toggle('is-error', !!err || v == null);
      el.card.title = err ? `Błąd: ${err}` : '';
    }
    if (state.mode === 'charts') redrawCharts();
    if (state.detail) updateDetail();
  }

  function redrawCharts() {
    for (const [k, chart] of Object.entries(state.charts)) chart.setData(downsample(state.series[k] || [], MAX_DRAW_POINTS));
    if (state.detail) updateDetail();
  }

  function destroyCharts() {
    for (const c of Object.values(state.charts)) c.destroy();
    state.charts = {};
  }

  // ── szczegóły ─────────────────────────────────────────────
  function openDetail(key) {
    const m = state.data && state.data.meta[key];
    if (!m) return;
    const stats = {};
    const stat = (label, color) => {
      const v = h('span', { class: 'stat-val', style: color ? { color } : null }, '-');
      stats[label] = v;
      return h('div', { class: 'stat' }, h('span', { class: 'stat-label' }, label), v);
    };
    const canvas = h('canvas', { 'aria-label': `Wykres: ${m.label}` });
    const rangeCopy = select(rangeSel.options ? [...rangeSel.options].map((o) => [o.value, o.textContent]) : [], state.range,
      { 'aria-label': 'Zakres', onchange: () => { rangeSel.value = rangeCopy.value; setRange(+rangeCopy.value); } });
    const bodyEl = h('div', { style: { display: 'flex', flexDirection: 'column', height: '100%' } },
      h('div', { class: 'toolbar', style: { justifyContent: 'space-between', marginBottom: '12px' } },
        h('div', { class: 'stats-row', style: { marginBottom: '0' } },
          stat('Aktualna'), stat('Min', 'var(--cyan)'), stat('Maks', 'var(--red)'), stat('Średnia', 'var(--yellow)'), stat('Punkty', 'var(--text2)')),
        rangeCopy),
      h('div', { class: 'chart-box' }, canvas));
    const color = colorFor(key, m.label, m.group);
    const dlg = modal({
      title: m.label,
      subtitle: `${m.unit ? 'Wartość w ' + m.unit + ' · ' : ''}${key}`,
      body: bodyEl,
      wide: true,
      onClose: () => {
        if (state.detail) state.detail.chart.destroy();
        state.detail = null;
        updateModeButtons();
      },
    });
    const chart = new AreaChart(canvas, { color, decimals: m.decimals, unit: m.unit, axes: true, markers: true });
    state.detail = { key, chart, modal: dlg, stats, meta: m };
    updateModeButtons();
    if (!state.series[key] || !state.series[key].length || state.historyAt === 0) loadHistory();
    updateDetail();
  }

  function updateDetail() {
    const d = state.detail;
    if (!d) return;
    const series = state.series[d.key] || [];
    d.chart.setData(downsample(series, 1500));
    const st = d.chart.stats();
    const cur = state.data ? state.data.values[d.key] : null;
    const dec = d.meta.decimals;
    d.stats['Aktualna'].textContent = fmt(cur, dec);
    d.stats['Min'].textContent = st ? fmt(st.min, dec) : '-';
    d.stats['Maks'].textContent = st ? fmt(st.max, dec) : '-';
    d.stats['Średnia'].textContent = st ? fmt(st.avg, dec) : '-';
    d.stats['Punkty'].textContent = String(series.length);
  }

  // ── eksport ───────────────────────────────────────────────
  function exportCsv() {
    if (!state.deviceId) return;
    const a = h('a', { href: `/api/devices/${enc(state.deviceId)}/history.csv?seconds=${state.range}`, download: '' });
    document.body.append(a);
    a.click();
    a.remove();
    toast('Pobieranie CSV...', 'info', 2000);
  }

  updateModeButtons();
  poller.start();
  loadDevices();

  return {
    unmount() {
      poller.stop();
      destroyCharts();
      if (state.detail) state.detail.modal.close();
    },
  };
}

/** Uśrednia serię do maks. n punktów, zachowując przerwy (null). */
function downsample(series, n) {
  if (series.length <= n) return series;
  const step = series.length / n;
  const out = [];
  for (let i = 0; i < n; i++) {
    const a = Math.floor(i * step), b = Math.floor((i + 1) * step);
    let sum = 0, cnt = 0;
    for (let j = a; j < b; j++) { const v = series[j][1]; if (v != null && Number.isFinite(v)) { sum += v; cnt++; } }
    out.push([series[Math.min(b - 1, series.length - 1)][0], cnt ? sum / cnt : null]);
  }
  return out;
}
