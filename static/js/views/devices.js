// Widok: Urządzenia - liczniki odczytywane w tle, formularz dodawania/edycji, rozpoznawanie licznika.

import {
  h, mount as fill, get, post, put, del, enc, toast, showError, Poller, runJob, modal, confirmDialog, fmt,
  fmtAge, fmtTime, stateBadge, STATES, KINDS, field, select, emptyState, pageHeader, plural, uid, slugify,
  uniqueId, busy, fieldError,
} from '../core.js';

const REFRESH_MS = 3000;
const ID_RE = /^[a-z0-9][a-z0-9_-]{0,31}$/;
const SERIAL_KINDS = new Set(['rtu', 'ascii']);

// ── pomocnicze ───────────────────────────────────────────────

const num = (v) => Number(v).toLocaleString('pl-PL', { maximumFractionDigits: 2 });

function serialText(s) {
  if (!s || !s.baudrate) return '';
  return `${s.baudrate} ${s.bytesize || 8}${s.parity || 'N'}${s.stopbits || 1}`;
}

function presetLabel(p) {
  const mm = [p.manufacturer, p.model].filter(Boolean).join(' ');
  return p.name + (mm && mm !== p.name ? ` (${mm})` : '') + (p.valid === false ? ' [błędny]' : '');
}

// ── widok ────────────────────────────────────────────────────

export function mount(root, ctx) {
  const state = {
    devices: [],
    buses: [],
    presets: [],
    loaded: false,
    error: null,
    updated: null,
  };
  let destroyed = false;
  let form = null;            // otwarte okno formularza (wynik modal())
  const ac = new AbortController();
  const cards = new Map();    // id -> {el, body, btns, lockNote}

  const addBtn = h('button', { class: 'btn btn-primary', type: 'button', onclick: () => openForm(null) }, '+ Dodaj urządzenie');
  const statusLine = h('div', { class: 'status-line', role: 'status', 'aria-live': 'polite' },
    h('span', { class: 'spinner' }), 'Wczytywanie urządzeń...');
  const list = h('div', { class: 'dev-grid' });
  fill(root, h('div', { class: 'v-devices' },
    pageHeader('Urządzenia', addBtn),
    h('div', { class: 'page-content' }, statusLine, list)));

  const busById = (id) => state.buses.find((b) => b.id === id);
  const deviceById = (id) => state.devices.find((d) => d.id === id);

  // ── dane ──────────────────────────────────────────────────
  async function loadBuses() {
    try {
      state.buses = await get('/api/buses', { signal: ac.signal });
    } catch (e) {
      if (!destroyed) showError(e, 'Lista połączeń: ');
    }
  }
  async function loadPresets() {
    try {
      state.presets = await get('/api/presets', { signal: ac.signal });
    } catch (e) {
      if (!destroyed) showError(e, 'Lista presetów: ');
    }
  }

  let busReload = null;
  async function refresh() {
    try {
      const devices = await get('/api/devices', { signal: ac.signal });
      if (destroyed) return;
      state.devices = devices;
      state.loaded = true;
      state.error = null;
      state.updated = Date.now() / 1000;
      // urządzenie na nieznanej (świeżo dodanej) magistrali -> odśwież listę połączeń
      if (!busReload && devices.some((d) => !busById(d.bus))) {
        busReload = loadBuses().finally(() => { busReload = null; });
        await busReload;
      }
      render();
    } catch (e) {
      if (destroyed || e.name === 'AbortError') return;
      if (!state.error) showError(e, 'Lista urządzeń: ');
      state.error = e.message;
      renderStatus();
    }
  }

  const poller = new Poller(refresh, REFRESH_MS);

  // ── lista ─────────────────────────────────────────────────
  function renderStatus() {
    if (!state.loaded && !state.error) {
      fill(statusLine, h('span', { class: 'spinner' }), 'Wczytywanie urządzeń...');
      return;
    }
    const n = state.devices.length;
    const counts = {};
    for (const d of state.devices) {
      const s = (d.status && d.status.state) || 'waiting';
      counts[s] = (counts[s] || 0) + 1;
    }
    const parts = [h('span', null, `${n} ${plural(n, 'urządzenie', 'urządzenia', 'urządzeń')}`)];
    for (const s of ['ok', 'error', 'stale', 'no_preset', 'waiting', 'disabled']) {
      if (counts[s]) parts.push(h('span', null, stateBadge(s), ' ', counts[s]));
    }
    if (state.error) {
      parts.push(h('span', { class: 'err-text' }, `Błąd odświeżania: ${state.error}`));
    } else if (state.updated) {
      parts.push(h('span', { class: 'muted' }, `odświeżane co ${REFRESH_MS / 1000} s, ostatnio ${fmtTime(state.updated)}`));
    }
    fill(statusLine, parts);
  }

  function render() {
    renderStatus();
    if (!state.devices.length) {
      cards.clear();
      fill(list, emptyState('Nie masz jeszcze żadnych urządzeń. Dodaj licznik ręcznie albo znajdź go skanerem rejestrów.', [
        { label: '+ Dodaj urządzenie', onClick: () => openForm(null) },
        { label: 'Skaner rejestrów', href: '#scanner', class: 'btn-ghost' },
        { label: 'Połączenia', href: '#connections', class: 'btn-ghost' },
      ]));
      list.classList.add('is-empty');
      return;
    }
    if (list.classList.contains('is-empty')) {
      list.classList.remove('is-empty');
      list.replaceChildren();
    }
    const ids = new Set(state.devices.map((d) => d.id));
    for (const [id, c] of cards) {
      if (!ids.has(id)) { c.el.remove(); cards.delete(id); }
    }
    for (const d of state.devices) {
      let c = cards.get(d.id);
      if (!c) { c = createCard(d.id); cards.set(d.id, c); }
      updateCard(c, d);
    }
    // kolejność zmieniamy tylko gdy trzeba (przenoszenie węzła zabiera fokus)
    const want = state.devices.map((d) => cards.get(d.id).el);
    const have = [...list.children];
    if (want.length !== have.length || want.some((el, i) => el !== have[i])) list.append(...want);
  }

  function createCard(id) {
    const btn = (label, cls, onclick, title) =>
      h('button', { class: `btn btn-sm ${cls}`, type: 'button', onclick, title }, h('span', { class: 'lbl' }, label));
    const btns = {
      edit: btn('Edytuj', 'btn-ghost', () => openForm(deviceById(id))),
      read: btn('Odczytaj teraz', 'btn-ghost', (e) => readNow(id, e.currentTarget), 'Wykonaj odczyt natychmiast'),
      toggle: btn('Wyłącz', 'btn-ghost', (e) => toggle(id, e.currentTarget)),
      dash: btn('Pokaż na dashboardzie', 'btn-primary', () => ctx.navigate('dashboard', { device: id })),
      remove: btn('Usuń', 'btn-ghost danger-text', (e) => remove(id, e.currentTarget)),
    };
    const body = h('div', { class: 'dev-body' });
    const lockNote = h('p', { class: 'dev-lock small muted', hidden: true });
    const el = h('article', { class: 'card dev-card', dataset: { id } }, body, lockNote,
      h('div', { class: 'dev-actions' }, btns.dash, btns.read, btns.edit, btns.toggle, btns.remove));
    return { el, body, btns, lockNote };
  }

  function updateCard(c, d) {
    const st = d.status || {};
    const nm = d.name || d.id;
    const devState = st.state || (d.enabled ? 'waiting' : 'disabled');
    const bus = busById(d.bus);
    const titleId = `dev-${d.id}-title`;
    c.el.setAttribute('aria-labelledby', titleId);
    c.el.classList.toggle('is-disabled', !d.enabled);
    c.el.classList.toggle('is-error', devState === 'error');

    let presetText;
    if (d.preset_name) presetText = [d.preset_name, h('span', { class: 'muted' }, ` ${d.preset}`)];
    else if (d.preset) presetText = [d.preset, h('span', { class: 'warn-text' }, ' (nie wczytano)')];
    else presetText = h('span', { class: 'warn-text' }, 'brak - urządzenie nie jest odczytywane');

    const busText = bus
      ? [bus.name, h('span', { class: 'muted' }, ` ${bus.describe}`)]
      : [d.bus, h('span', { class: 'warn-text' }, ' (nieznane połączenie)')];

    let lastText = '-';
    if (st.last_ok_ts) {
      lastText = [fmtAge(Date.now() / 1000 - st.last_ok_ts),
        devState === 'ok' && st.duration_ms != null ? h('span', { class: 'muted' }, ` (${Math.round(st.duration_ms)} ms)`) : null,
        devState !== 'ok' && st.ts ? h('span', { class: 'muted' }, ` · ostatnia próba ${fmtAge(st.age)}`) : null];
    } else if (st.ts) {
      lastText = [h('span', { class: 'warn-text' }, 'nigdy'), h('span', { class: 'muted' }, ` · ostatnia próba ${fmtAge(st.age)}`)];
    } else if (d.enabled && d.preset) {
      lastText = h('span', { class: 'muted' }, 'jeszcze nie odczytano');
    }

    const showErr = st.error && devState !== 'ok' && devState !== 'disabled';
    fill(c.body,
      h('div', { class: 'dev-head' },
        h('div', { class: 'dev-title' },
          h('h3', { id: titleId }, nm),
          h('span', { class: 'mono small muted' }, d.id)),
        h('div', { class: 'dev-badges' },
          d.locked ? h('span', { class: 'badge badge-muted plain', title: 'Ustawione parametrami uruchomienia' }, 'CLI') : null,
          stateBadge(devState))),
      showErr ? h('div', { class: 'notice notice-err dev-error' }, st.error) : null,
      h('dl', { class: 'kv' },
        h('dt', null, 'Preset'), h('dd', null, presetText),
        h('dt', null, 'Połączenie'), h('dd', null, busText),
        h('dt', null, 'Unit ID'), h('dd', null, String(d.unit)),
        h('dt', null, 'Interwał'), h('dd', null, `${num(d.interval)} s`),
        h('dt', null, 'Ostatni udany odczyt'), h('dd', null, lastText),
        h('dt', null, 'Odczyty'), h('dd', null, `${st.polls ?? 0}`,
          h('span', { class: st.failures ? 'err-text' : 'muted' }, ` / błędy: ${st.failures ?? 0}`))));

    const editLbl = d.locked ? 'Szczegóły' : 'Edytuj';
    c.btns.toggle.querySelector('.lbl').textContent = d.enabled ? 'Wyłącz' : 'Włącz';
    c.btns.toggle.setAttribute('aria-label', `${d.enabled ? 'Wyłącz' : 'Włącz'} ${nm}`);
    c.btns.edit.querySelector('.lbl').textContent = editLbl;
    c.btns.edit.setAttribute('aria-label', `${editLbl} ${nm}`);
    c.btns.read.setAttribute('aria-label', `Odczytaj teraz ${nm}`);
    c.btns.dash.setAttribute('aria-label', `Pokaż na dashboardzie: ${nm}`);
    c.btns.remove.setAttribute('aria-label', `Usuń ${nm}`);
    for (const b of [c.btns.toggle, c.btns.remove]) {
      if (!b.dataset.busy) b.disabled = !!d.locked;
    }
    c.lockNote.hidden = !d.locked;
    c.lockNote.textContent = d.locked
      ? 'Urządzenie ustawione parametrami uruchomienia (wbudowany symulator, --sim-preset lub --preset) - tylko do odczytu.'
      : '';
  }

  // ── akcje ─────────────────────────────────────────────────
  function upsert(dev) {
    const i = state.devices.findIndex((d) => d.id === dev.id);
    if (i >= 0) state.devices[i] = dev; else state.devices.push(dev);
    render();
  }

  async function readNow(id, btn) {
    const d = deviceById(id);
    if (!d) return;
    await busy(btn, async () => {
      try {
        const res = await post(`/api/devices/${enc(id)}/read`, undefined, { signal: ac.signal });
        if (destroyed) return;
        const st = res.status || {};
        const vals = Object.values(res.values || {});
        const n = vals.filter((v) => v != null).length;
        const errs = Object.values(res.errors || {});
        const name = d.name || d.id;
        if (!res.preset && !d.preset) toast(`${name}: brak presetu - nie ma czego odczytać`, 'err');
        else if (n && !errs.length) toast(`${name}: odczytano ${n} ${plural(n, 'wartość', 'wartości', 'wartości')}${st.duration_ms != null ? ` w ${Math.round(st.duration_ms)} ms` : ''}`);
        else if (n) toast(`${name}: odczytano ${n} z ${vals.length}, błędy: ${errs[0]}`, 'info', 6000);
        else toast(`${name}: ${st.error || errs[0] || (STATES[st.state] || [])[0] || 'brak danych'}`, 'err');
        if (res.status) {
          const cur = deviceById(id);
          if (cur) upsert({ ...cur, status: res.status });
        }
        ctx.refreshHealth();
      } catch (e) {
        showError(e, 'Odczyt: ');
      }
    });
  }

  async function toggle(id, btn) {
    const d = deviceById(id);
    if (!d || d.locked) return;
    await busy(btn, async () => {
      try {
        const body = { name: d.name, bus: d.bus, unit: d.unit, preset: d.preset || null, interval: d.interval, enabled: !d.enabled };
        const res = await put(`/api/devices/${enc(id)}`, body, { signal: ac.signal });
        if (destroyed) return;
        toast(res.enabled ? `Włączono "${res.name}"` : `Wyłączono "${res.name}"`);
        upsert(res);
        ctx.refreshHealth();
      } catch (e) {
        showError(e, 'Nie udało się zmienić stanu: ');
      }
    });
    poller.trigger();
  }

  async function remove(id, btn) {
    const d = deviceById(id);
    if (!d || d.locked) return;
    const ok = await confirmDialog(
      `Usunąć urządzenie "${d.name || d.id}" (${d.id})? Zapisana historia pomiarów tego urządzenia też zostanie usunięta.`,
      { title: 'Usuń urządzenie' });
    if (!ok || destroyed) return;
    await busy(btn, async () => {
      try {
        await del(`/api/devices/${enc(id)}`, { signal: ac.signal });
        if (destroyed) return;
        toast(`Usunięto "${d.name || d.id}"`);
        state.devices = state.devices.filter((x) => x.id !== id);
        render();
        ctx.refreshHealth();
      } catch (e) {
        showError(e, 'Nie udało się usunąć: ');
      }
    });
  }

  // ── formularz ─────────────────────────────────────────────
  function openForm(dev, prefill = {}) {
    if (form) form.close();
    const isNew = !dev;
    const locked = !!(dev && dev.locked);
    const d = dev || {
      name: prefill.name || '',
      bus: prefill.bus || (state.buses.find((b) => b.id !== 'default' && !b.locked)
        || state.buses.find((b) => b.id !== 'default') || state.buses[0] || { id: 'default' }).id,
      unit: prefill.unit != null && prefill.unit !== '' ? prefill.unit : 1,
      preset: prefill.preset || '',
      interval: 1,
      enabled: true,
    };
    let idTouched = false;
    let detectAc = null;

    // pola
    const nameIn = h('input', { type: 'text', maxlength: 64, autocomplete: 'off', value: d.name || '', placeholder: 'np. Licznik główny' });
    const idIn = h('input', { type: 'text', maxlength: 32, autocomplete: 'off', spellcheck: 'false', class: 'mono',
      value: isNew ? '' : d.id, placeholder: 'np. licznik-glowny' });
    const busSel = h('select');
    const busHint = h('span');
    const unitIn = h('input', { type: 'number', min: 0, max: 255, step: 1, inputmode: 'numeric', value: String(d.unit) });
    const unitHint = h('span');
    const presetSel = h('select', { id: uid() });
    const presetHint = h('span', { class: 'hint' });
    const intervalIn = h('input', { type: 'text', inputmode: 'decimal', value: String(d.interval).replace('.', ','), autocomplete: 'off' });
    const enabledIn = h('input', { type: 'checkbox', id: uid(), checked: d.enabled !== false });
    const detectBtn = h('button', { class: 'btn btn-ghost btn-sm', type: 'button', onclick: detect },
      h('span', { class: 'lbl' }, 'Rozpoznaj licznik'));
    const detectBox = h('div', { class: 'detect-box', 'aria-live': 'polite' });
    const formErr = h('div', { class: 'notice notice-err', role: 'alert', hidden: true });
    const saveBtn = h('button', { class: 'btn btn-primary', type: 'submit' }, h('span', { class: 'lbl' }, isNew ? 'Dodaj urządzenie' : 'Zapisz'));
    const cancelBtn = h('button', { class: 'btn btn-ghost', type: 'button', onclick: () => m.close() }, locked ? 'Zamknij' : 'Anuluj');

    function fillBusSelect(current) {
      const opts = state.buses.map((b) => [b.id, `${b.name} (${b.describe})`]);
      if (current && !busById(current)) opts.unshift([current, `${current} [nie znaleziono]`]);
      fill(busSel, [...select(opts).children]);
      busSel.value = current;
    }
    function fillPresetSelect(current) {
      const mine = state.presets.filter((p) => !p.builtin);
      const lib = state.presets.filter((p) => p.builtin);
      const opts = [['', '- bez presetu (nie odczytuj) -']];
      if (current && !state.presets.some((p) => p.id === current)) opts.push([current, `${current} [nie znaleziono]`]);
      if (mine.length) opts.push({ group: 'Moje presety', options: mine.map((p) => [p.id, presetLabel(p)]) });
      if (lib.length) opts.push({ group: 'Biblioteka', options: lib.map((p) => [p.id, presetLabel(p)]) });
      fill(presetSel, [...select(opts).children]);
      for (const o of presetSel.querySelectorAll('option')) {
        const p = state.presets.find((x) => x.id === o.value);
        if (p && p.valid === false && o.value !== current) o.disabled = true;
      }
      presetSel.value = current || '';
    }
    fillBusSelect(d.bus);
    fillPresetSelect(d.preset || '');

    const curBus = () => busById(busSel.value);
    const isSerial = () => { const b = curBus(); return !!(b && SERIAL_KINDS.has(b.kind)); };

    function updateHints() {
      const b = curBus();
      fill(busHint, b ? `${KINDS[b.kind] || b.kind}${b.locked ? ', ustawione parametrami uruchomienia' : ''}. ` : '',
        h('a', { href: '#connections' }, 'Zarządzaj połączeniami'));
      unitHint.textContent = isSerial()
        ? 'RS-485: adres licznika 1-247 (fabrycznie zwykle 1)'
        : 'TCP: zwykle 1; bramka RS-485 przekazuje go do licznika (0-255)';
      const p = state.presets.find((x) => x.id === presetSel.value);
      const parts = [];
      if (!presetSel.value) parts.push('Bez presetu urządzenie nie będzie odczytywane.');
      else if (!p) parts.push('Preset nie istnieje - wybierz inny.');
      else {
        if (p.valid === false) parts.push(`Preset zawiera błędy: ${(p.errors || []).slice(0, 2).join('; ')}`);
        parts.push(`${p.register_count} ${plural(p.register_count, 'rejestr', 'rejestry', 'rejestrów')}, ${p.phases === 1 ? '1 faza' : p.phases ? `${p.phases} fazy` : 'liczba faz nieznana'}`);
        const s = serialText(p.serial);
        if (s) {
          parts.push(`ustawienia fabryczne ${s}`);
          if (b && SERIAL_KINDS.has(b.kind) && p.serial.baudrate && (p.serial.baudrate !== b.baudrate
            || (p.serial.parity && p.serial.parity !== b.parity))) {
            parts.push(`(połączenie ma ${b.baudrate} ${b.bytesize}${b.parity}${b.stopbits} - sprawdź ustawienia licznika)`);
          }
        }
      }
      presetHint.textContent = parts.join(' · ');
    }

    function suggestId() {
      if (!isNew || idTouched) return;
      const taken = new Set(state.devices.map((x) => x.id));
      idIn.value = nameIn.value.trim() ? uniqueId(slugify(nameIn.value), taken, 'licznik') : '';
      if (idIn.getAttribute('aria-invalid')) fieldError(idIn, null);
    }
    nameIn.addEventListener('input', suggestId);
    idIn.addEventListener('input', () => { idTouched = !!idIn.value; fieldError(idIn, null); });
    busSel.addEventListener('change', () => {
      updateHints();
      fieldError(busSel, null);
      if (unitIn.getAttribute('aria-invalid')) fieldError(unitIn, unitMsg());
    });
    presetSel.addEventListener('change', () => { updateHints(); fieldError(presetSel, null); });
    unitIn.addEventListener('input', () => fieldError(unitIn, null));
    intervalIn.addEventListener('input', () => fieldError(intervalIn, null));
    updateHints();
    if (isNew && d.name) suggestId();

    // walidacja
    function parseUnit() {
      const s = unitIn.value.trim();
      if (!/^\d+$/.test(s)) return null;
      return parseInt(s, 10);
    }
    function unitMsg() {
      const unit = parseUnit();
      if (unit == null || unit > 255) return 'Unit ID to liczba całkowita 0-255';
      if (isSerial() && (unit < 1 || unit > 247)) return 'Na RS-485 Unit ID musi być w zakresie 1-247';
      return null;
    }
    function validate({ onlyBus = false } = {}) {
      const errs = [];
      const set = (el, msg) => { fieldError(el, msg); if (msg) errs.push(el); };
      const unit = parseUnit();
      if (!busSel.value || !curBus()) set(busSel, 'Wybierz połączenie (albo dodaj je w zakładce Połączenia)');
      else set(busSel, null);
      set(unitIn, unitMsg());
      if (onlyBus) {
        if (errs.length) { errs[0].focus(); return null; }
        return { bus: busSel.value, unit };
      }
      if (isNew) {
        const id = idIn.value.trim();
        if (!id) set(idIn, 'Podaj identyfikator');
        else if (!ID_RE.test(id)) set(idIn, 'Dozwolone: małe litery, cyfry, "_" i "-" (max 32 znaki, na początku litera lub cyfra)');
        else if (deviceById(id)) set(idIn, 'Urządzenie o takim identyfikatorze już istnieje');
        else set(idIn, null);
      }
      const iv = Number(intervalIn.value.trim().replace(',', '.'));
      if (!intervalIn.value.trim() || !Number.isFinite(iv) || iv < 0.2 || iv > 3600) set(intervalIn, 'Interwał w sekundach: 0,2-3600');
      else set(intervalIn, null);
      const p = state.presets.find((x) => x.id === presetSel.value);
      if (presetSel.value && p && p.valid === false) set(presetSel, 'Ten preset zawiera błędy - popraw go w zakładce Presety');
      else set(presetSel, null);
      if (errs.length) { errs[0].focus(); return null; }
      return {
        id: isNew ? idIn.value.trim() : d.id,
        body: { name: nameIn.value.trim() || (isNew ? idIn.value.trim() : d.id), bus: busSel.value, unit, preset: presetSel.value || null, interval: iv, enabled: enabledIn.checked },
      };
    }

    function showServerError(e) {
      const msg = e.message || String(e);
      formErr.hidden = false;
      fill(formErr, h('strong', null, 'Serwer odrzucił zmiany: '), msg,
        e.errors && e.errors.length ? h('ul', { class: 'errors' }, e.errors.map((x) => h('li', null, x))) : null);
      const low = msg.toLowerCase();
      if (low.includes('unit id')) fieldError(unitIn, msg);
      else if (low.includes('interwał')) fieldError(intervalIn, msg);
      else if (low.includes('magistral')) fieldError(busSel, msg);
      else if (low.includes('identyfikator')) fieldError(idIn, msg);
      else if (low.includes('preset')) fieldError(presetSel, msg);
    }

    async function submit(ev) {
      ev.preventDefault();
      if (locked || saveBtn.dataset.busy) return;
      formErr.hidden = true;
      const v = validate();
      if (!v) return;
      await busy(saveBtn, async () => {
        try {
          if (isNew) {
            // PUT nadpisuje - upewnij się, że id jest wolne także po stronie serwera
            const fresh = await get('/api/devices', { signal: ac.signal });
            state.devices = fresh;
            if (fresh.some((x) => x.id === v.id)) {
              fieldError(idIn, 'Urządzenie o takim identyfikatorze już istnieje');
              idIn.focus();
              return;
            }
          }
          const res = await put(`/api/devices/${enc(v.id)}`, v.body, { signal: ac.signal });
          if (destroyed) return;
          toast(isNew ? `Dodano urządzenie "${res.name}"` : `Zapisano "${res.name}"`);
          upsert(res);
          ctx.refreshHealth();
          m.close();
          poller.trigger();
        } catch (e) {
          if (e.name === 'AbortError') return;
          showServerError(e);
          showError(e, 'Zapis: ');
        }
      });
    }

    // rozpoznawanie licznika
    async function detect() {
      if (detectAc || locked) return;
      const v = validate({ onlyBus: true });
      if (!v) return;
      detectAc = new AbortController();
      const myAc = detectAc;
      detectBtn.disabled = true;
      const bar = h('div', { style: { width: '0%' } });
      const prog = h('div', { class: 'progress', role: 'progressbar', 'aria-valuemin': '0', 'aria-valuemax': '100', 'aria-valuenow': '0', 'aria-label': 'Postęp rozpoznawania' }, bar);
      const msg = h('span', { class: 'small muted' }, `Łączenie z Unit ID ${v.unit}...`);
      const stopBtn = h('button', { class: 'btn btn-ghost btn-sm', type: 'button', onclick: () => myAc.abort() }, 'Anuluj');
      fill(detectBox, h('div', { class: 'detect-run' }, h('div', { class: 'detect-row' }, h('span', { class: 'spinner' }), msg, stopBtn), prog));
      try {
        const job = await runJob(post('/api/detect', { bus: v.bus, unit: v.unit }), (j) => {
          const p = j.progress || {};
          const pct = p.total ? Math.round((p.done / p.total) * 100) : 0;
          bar.style.width = pct + '%';
          prog.setAttribute('aria-valuenow', String(pct));
          msg.textContent = p.total ? `Sprawdzanie presetów ${p.done}/${p.total}${p.message ? ': ' + p.message : ''}` : (p.message || 'Sprawdzanie urządzenia...');
        }, { signal: myAc.signal });
        if (myAc.signal.aborted) return;
        if (job.state === 'cancelled') {
          fill(detectBox, h('div', { class: 'notice notice-info' }, 'Rozpoznawanie anulowane.'));
          return;
        }
        const result = job.result || {};
        await showCandidates(result.candidates || [], v, result.checked);
      } catch (e) {
        if (e.name === 'AbortError') {
          fill(detectBox, h('div', { class: 'notice notice-info' }, 'Rozpoznawanie anulowane.'));
          return;
        }
        fill(detectBox, h('div', { class: 'notice notice-err' },
          h('strong', null, 'Nie udało się rozpoznać licznika: '), e.message || String(e),
          h('p', { class: 'small muted' }, 'Sprawdź Unit ID, parametry połączenia i okablowanie (A/B). Możesz też przetestować połączenie w zakładce Połączenia.')));
      } finally {
        if (detectAc === myAc) detectAc = null;
        detectBtn.disabled = locked;
      }
    }

    async function showCandidates(cands, v, checkedCount) {
      const checked = checkedCount ?? cands.length;
      const good = cands.filter((c) => c.score > 0);
      if (!good.length) {
        fill(detectBox, h('div', { class: 'notice notice-warn' },
          'Urządzenie odpowiada, ale żaden preset nie dał wiarygodnych wartości. ',
          h('a', { href: `#scanner?bus=${enc(v.bus)}&unit=${enc(v.unit)}` }, 'Otwórz skaner rejestrów'),
          ', aby znaleźć rejestry i zbudować własny preset.'));
        return;
      }
      const top = good.slice(0, 6);
      fill(detectBox, h('div', { class: 'small muted' }, h('span', { class: 'spinner' }), ' Pobieranie opisów wartości...'));
      // etykiety i jednostki z presetów (wartości z zadania są surowe: {klucz: liczba})
      const metas = await Promise.all(top.map((c) => get(`/api/presets/${enc(c.id)}`, { signal: detectAc ? detectAc.signal : ac.signal })
        .then((p) => p.registers || {})
        .catch((e) => { if (e.name !== 'AbortError') console.warn('preset', c.id, e.message); return {}; })));
      if (destroyed) return;
      const items = top.map((c, i) => {
        const regs = metas[i];
        const pct = Math.round(c.score * 100);
        const vals = Object.entries(c.values || {}).slice(0, 6).map(([k, val]) => {
          const r = regs[k] || {};
          return h('span', { class: 'cand-val' }, h('span', { class: 'muted' }, `${r.label || k}: `),
            fmt(val, r.decimals ?? 2), r.unit ? ` ${r.unit}` : '');
        });
        const mm = [c.manufacturer, c.model].filter(Boolean).join(' ');
        const useBtn = h('button', { class: `btn btn-sm ${i === 0 ? 'btn-primary' : 'btn-ghost'}`, type: 'button',
          'aria-label': `Użyj presetu ${c.name}`, onclick: () => useCandidate(c, useBtn) }, 'Użyj');
        return h('li', { class: 'cand' },
          h('div', { class: 'cand-head' },
            h('div', null, h('strong', null, c.name), mm ? h('span', { class: 'muted small' }, ` ${mm}`) : null,
              h('span', { class: 'badge badge-muted plain cand-src' }, c.builtin ? 'Biblioteka' : 'Mój')),
            useBtn),
          h('div', { class: 'cand-score' },
            h('div', { class: 'progress', role: 'img', 'aria-label': `Dopasowanie ${pct}%` },
              h('div', { style: { width: pct + '%', background: pct >= 70 ? 'var(--green)' : pct >= 40 ? 'var(--yellow)' : 'var(--red)' } })),
            h('span', { class: 'mono small' }, `${pct}%`)),
          vals.length ? h('div', { class: 'cand-vals small' }, vals) : null);
      });
      fill(detectBox,
        h('div', { class: 'small muted' }, `Dopasowanie presetów do Unit ID ${v.unit} - najlepsze ${top.length} z ${checked} sprawdzonych. Wybierz właściwy przyciskiem "Użyj":`),
        h('ol', { class: 'cand-list' }, items));
    }

    async function useCandidate(c, btn) {
      if (!state.presets.some((p) => p.id === c.id)) {
        await busy(btn, loadPresets);
        fillPresetSelect(c.id);
      }
      presetSel.value = c.id;
      if (presetSel.value !== c.id) { toast(`Preset "${c.id}" nie jest dostępny na liście`, 'err'); return; }
      fieldError(presetSel, null);
      if (!nameIn.value.trim()) {
        nameIn.value = [c.manufacturer, c.model].filter(Boolean).join(' ') || c.name;
        suggestId();
      }
      updateHints();
      for (const b of detectBox.querySelectorAll('.cand button')) { b.textContent = 'Użyj'; b.classList.replace('btn-primary', 'btn-ghost'); }
      btn.textContent = 'Wybrano';
      btn.classList.replace('btn-ghost', 'btn-primary');
      toast(`Wybrano preset "${c.name}"`, 'info');
    }

    // układ
    const lockNote = locked ? h('div', { class: 'notice notice-warn' },
      h('strong', null, 'Tylko do odczytu. '),
      'To urządzenie jest zdefiniowane parametrami uruchomienia aplikacji (wbudowany symulator, ',
      h('code', null, '--sim-preset'), ' lub ', h('code', null, '--preset'), '). Aby je zmienić, uruchom aplikację ponownie z innymi parametrami. ',
      'Możesz też dodać własne urządzenie na tym samym połączeniu.') : null;

    const formEl = h('form', { class: 'v-devices dev-form', novalidate: true, onsubmit: submit },
      lockNote,
      formErr,
      h('div', { class: 'form-grid' },
        field('Nazwa', nameIn, isNew ? 'Wyświetlana na dashboardzie' : null),
        isNew
          ? field('Identyfikator', idIn, 'Stały, w adresach API i MQTT; podpowiadany z nazwy')
          : field('Identyfikator', h('input', { type: 'text', class: 'mono', value: d.id, readonly: true, 'aria-readonly': 'true' }), 'Nie można zmienić'),
        field('Połączenie', busSel, busHint, { class: 'field span-2' }),
        field('Unit ID', unitIn, unitHint),
        field('Interwał odczytu [s]', intervalIn, 'Co ile sekund odczytywać (0,2-3600)'),
        h('div', { class: 'field span-2' },
          h('div', { class: 'preset-label-row' },
            h('label', { class: 'label', for: presetSel.id }, 'Preset (mapa rejestrów)'),
            detectBtn),
          presetSel, presetHint),
        h('div', { class: 'span-2' }, detectBox),
        h('div', { class: 'field inline span-2' }, enabledIn, h('label', { for: enabledIn.id }, 'Odczytuj w tle (włączone)'))),
      h('div', { class: 'form-actions' }, cancelBtn, locked ? null : saveBtn));

    if (locked) {
      for (const el of formEl.querySelectorAll('input,select')) el.disabled = true;
      detectBtn.disabled = true;
    }

    const m = modal({
      closeOnBackdrop: false,
      title: isNew ? 'Nowe urządzenie' : (locked ? 'Szczegóły urządzenia' : 'Edycja urządzenia'),
      subtitle: isNew ? 'Licznik o danym Unit ID na wybranym połączeniu, odczytywany wg presetu' : `${d.name || d.id} (${d.id})`,
      body: formEl,
      onClose: () => {
        if (detectAc) detectAc.abort();
        if (form === m) form = null;
        // po zamknięciu formularza z linku (#devices?new=1) nie otwieraj go ponownie przy odświeżeniu
        if (!destroyed && /^#devices\?/.test(location.hash)) history.replaceState(history.state, '', '#devices');
      },
    });
    form = m;
    if (locked) cancelBtn.focus();
    // świeże listy połączeń i presetów (mogły się zmienić w innym widoku lub karcie)
    Promise.all([loadBuses(), loadPresets()]).then(() => {
      if (destroyed || form !== m) return;
      fillBusSelect(busSel.value || d.bus);
      fillPresetSelect(presetSel.value);
      updateHints();
    });
  }

  // ── start ─────────────────────────────────────────────────
  (async () => {
    await Promise.all([loadBuses(), loadPresets()]);
    if (destroyed) return;
    await refresh();
    if (destroyed) return;
    poller.start(false);
    const p = ctx.params || {};
    if (p.new === '1' || p.new === 'true') {
      openForm(null, { bus: p.bus, unit: p.unit, preset: p.preset, name: p.name });
    } else if (p.edit) {
      const d = deviceById(p.edit);
      if (d) openForm(d);
      else if (state.loaded) toast(`Nie znaleziono urządzenia "${p.edit}"`, 'err');
    }
  })();

  return {
    unmount() {
      destroyed = true;
      poller.stop();
      ac.abort();
      if (form) form.close();
    },
  };
}
