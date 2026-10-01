// Widok: Połączenia - magistrale RS-485 / Modbus TCP, test połączenia, formularz dodawania/edycji.

import {
  h, mount as fill, get, post, put, del, enc, toast, showError, Poller, modal, confirmDialog,
  fmtAge, fmtTime, KINDS, field, select, markInvalid, pageHeader,
} from '../core.js';

const REFRESH_MS = 3000;
const ID_RE = /^[a-z0-9][a-z0-9_-]{0,31}$/;
const SERIAL_KINDS = new Set(['rtu', 'ascii']);
const BAUDRATES = [1200, 2400, 4800, 9600, 19200, 38400, 57600, 115200];
const PARITIES = [['N', 'Brak (N)'], ['E', 'Parzysta (E)'], ['O', 'Nieparzysta (O)']];
const KIND_HELP = {
  tcp: 'Licznik z Ethernetem albo bramka Modbus TCP, która tłumaczy protokół na RS-485. Port zwykle 502.',
  rtu_over_tcp: 'Bramka transparentna RS-485 <-> TCP (np. USR-TCP232, Elfin EW11, Waveshare) przesyłająca surowe ramki RTU. Port często 8899.',
  udp: 'Modbus TCP przesyłany przez UDP - spotykany rzadko, głównie w bramkach.',
  rtu: 'Port szeregowy RS-485 (przejściówka USB albo UART Raspberry Pi). Najczęstszy wybór dla liczników.',
  ascii: 'Port szeregowy w trybie Modbus ASCII - rzadko spotykany, głównie starsze urządzenia.',
};
const DEFAULTS = {
  name: '', kind: 'rtu', host: '', port: 502, serial_port: '', baudrate: 9600, parity: 'N', stopbits: 1, bytesize: 8,
  timeout: 1, retries: 1, delay_ms: 0, local_echo: false,
};

// ── pomocnicze ───────────────────────────────────────────────

const uid = () => 'f' + Math.random().toString(36).slice(2, 10);
const num = (v, d = 2) => Number(v).toLocaleString('pl-PL', { maximumFractionDigits: d });

function plural(n, one, few, many) {
  const n10 = n % 10, n100 = n % 100;
  if (n === 1) return one;
  if (n10 >= 2 && n10 <= 4 && (n100 < 12 || n100 > 14)) return few;
  return many;
}

function slugify(text) {
  const pl = { ą: 'a', ć: 'c', ę: 'e', ł: 'l', ń: 'n', ó: 'o', ś: 's', ź: 'z', ż: 'z' };
  return String(text || '').toLowerCase()
    .replace(/[ąćęłńóśźż]/g, (c) => pl[c])
    .normalize('NFD').replace(/\p{Mn}/gu, '')
    .replace(/[^a-z0-9_-]+/g, '-').replace(/-{2,}/g, '-')
    .replace(/^[-_]+/, '').slice(0, 32).replace(/[-_]+$/, '');
}

function uniqueId(base, taken) {
  const id = base || 'magistrala';
  if (!taken.has(id)) return id;
  for (let i = 2; i < 1000; i++) {
    const suffix = '-' + i;
    const cand = id.slice(0, 32 - suffix.length).replace(/[-_]+$/, '') + suffix;
    if (!taken.has(cand)) return cand;
  }
  return id;
}

/** Przycisk w stanie "zajęty" na czas fn(); drugie kliknięcie jest ignorowane. */
async function busy(btn, fn) {
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

function fieldError(control, msg) {
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

function parseIntStrict(s) {
  s = String(s ?? '').trim();
  return /^\d+$/.test(s) ? parseInt(s, 10) : null;
}
function parseDecimal(s) {
  s = String(s ?? '').trim().replace(',', '.');
  if (!s || !/^\d*\.?\d+$/.test(s)) return null;
  return Number(s);
}

/** Wynik testu połączenia: {ok, error, ms}. */
function pingResult(res, unit, kind) {
  const ms = res.ms != null ? ` (${num(res.ms, 0)} ms)` : '';
  if (res.ok) {
    return h('div', { class: 'ping-res notice notice-info' },
      h('span', { class: 'badge badge-ok' }, 'OK'), ' ',
      unit != null ? `Urządzenie o Unit ID ${unit} odpowiada${ms}.` : `Połączenie działa${ms}.`,
      unit == null ? h('div', { class: 'small muted' }, 'Podaj Unit ID, aby sprawdzić także odpowiedź licznika.') : null);
  }
  const serial = SERIAL_KINDS.has(kind);
  return h('div', { class: 'ping-res notice notice-err' },
    h('span', { class: 'badge badge-err' }, 'Błąd'), ' ', `${res.error || 'brak odpowiedzi'}${ms}`,
    h('div', { class: 'small muted' }, serial
      ? 'Sprawdź nazwę portu i uprawnienia (grupa dialout), prędkość, parzystość, Unit ID oraz przewody A/B (spróbuj je zamienić).'
      : 'Sprawdź adres IP i port, czy urządzenie jest w tej samej sieci oraz tryb bramki (Modbus TCP czy RTU over TCP).'));
}

async function runPing(promise, unit, kind) {
  try {
    return pingResult(await promise, unit, kind);
  } catch (e) {
    if (e.name === 'AbortError') throw e;
    // 502 = test wykonany, ale bez odpowiedzi - treść ma {ok:false, error, ms}
    if (e.data && e.data.ok === false) return pingResult(e.data, unit, kind);
    return h('div', { class: 'ping-res notice notice-err' }, h('strong', null, 'Nie udało się wykonać testu: '), e.message);
  }
}

// ── widok ────────────────────────────────────────────────────

export function mount(root, ctx) {
  const state = { buses: [], devices: [], ports: null, loaded: false, error: null, updated: null };
  let destroyed = false;
  let openModal = null;
  const ac = new AbortController();
  const cards = new Map();

  const addBtn = h('button', { class: 'btn btn-primary', type: 'button', onclick: () => openForm(null) }, '+ Dodaj połączenie');
  const statusLine = h('div', { class: 'status-line', role: 'status', 'aria-live': 'polite' },
    h('span', { class: 'spinner' }), 'Wczytywanie połączeń...');
  const list = h('div', { class: 'bus-list' });
  fill(root, h('div', { class: 'v-connections' },
    pageHeader('Połączenia', addBtn),
    h('div', { class: 'page-content conn-layout' },
      h('div', { class: 'conn-main' }, statusLine, list),
      helpCard())));

  const busById = (id) => state.buses.find((b) => b.id === id);
  const usersOf = (id) => state.devices.filter((d) => d.bus === id);

  // ── dane ──────────────────────────────────────────────────
  async function refresh() {
    try {
      const [buses, devices] = await Promise.all([
        get('/api/buses', { signal: ac.signal }),
        get('/api/devices', { signal: ac.signal }),
      ]);
      if (destroyed) return;
      Object.assign(state, { buses, devices, loaded: true, error: null, updated: Date.now() / 1000 });
      render();
    } catch (e) {
      if (destroyed || e.name === 'AbortError') return;
      if (!state.error) showError(e, 'Lista połączeń: ');
      state.error = e.message;
      renderStatus();
    }
  }
  const poller = new Poller(refresh, REFRESH_MS);

  async function loadPorts() {
    try {
      state.ports = await get('/api/serial-ports', { signal: ac.signal });
    } catch (e) {
      if (e.name !== 'AbortError') showError(e, 'Lista portów szeregowych: ');
      state.ports = state.ports || [];
    }
    return state.ports;
  }

  // ── lista ─────────────────────────────────────────────────
  function renderStatus() {
    if (!state.loaded && !state.error) return;
    const n = state.buses.length;
    const parts = [h('span', null, `${n} ${plural(n, 'połączenie', 'połączenia', 'połączeń')}`)];
    if (state.error) parts.push(h('span', { class: 'err-text' }, `Błąd odświeżania: ${state.error}`));
    else parts.push(h('span', { class: 'muted' }, `statystyki odświeżane co ${REFRESH_MS / 1000} s, ostatnio ${fmtTime(state.updated)}`));
    fill(statusLine, parts);
  }

  function render() {
    renderStatus();
    const ids = new Set(state.buses.map((b) => b.id));
    for (const [id, c] of cards) {
      if (!ids.has(id)) { c.el.remove(); cards.delete(id); }
    }
    for (const b of state.buses) {
      let c = cards.get(b.id);
      if (!c) { c = createCard(b.id); cards.set(b.id, c); }
      updateCard(c, b);
    }
    const want = state.buses.map((b) => cards.get(b.id).el);
    const have = [...list.children];
    if (want.length !== have.length || want.some((el, i) => el !== have[i])) list.append(...want);
  }

  function createCard(id) {
    const btn = (label, cls, onclick) =>
      h('button', { class: `btn btn-sm ${cls}`, type: 'button', onclick }, h('span', { class: 'lbl' }, label));
    const btns = {
      test: btn('Testuj', 'btn-primary', () => openPing(busById(id))),
      edit: btn('Edytuj', 'btn-ghost', () => openForm(busById(id))),
      add: btn('Dodaj urządzenie', 'btn-ghost', () => ctx.navigate('devices', { new: 1, bus: id })),
      remove: btn('Usuń', 'btn-ghost danger-text', (e) => remove(id, e.currentTarget)),
    };
    const body = h('div', { class: 'bus-body' });
    // lista urządzeń osobno: zawiera linki, więc przebudowujemy ją tylko przy zmianie (fokus klawiatury)
    const usersBox = h('div', { class: 'bus-users' });
    const note = h('p', { class: 'bus-note small muted', hidden: true });
    const el = h('article', { class: 'card bus-card', dataset: { id } }, body, usersBox, note,
      h('div', { class: 'bus-actions' }, btns.test, btns.edit, btns.add, btns.remove));
    return { el, body, usersBox, usersSig: null, note, btns };
  }

  function connBadge(b) {
    const s = b.stats;
    if (!s) return h('span', { class: 'badge badge-muted', title: 'Nikt jeszcze nie korzystał z tego połączenia' }, 'Nieużywane');
    if (s.connected) return h('span', { class: 'badge badge-ok' }, 'Połączono');
    if (s.last_error) return h('span', { class: 'badge badge-err' }, 'Rozłączono');
    return h('span', { class: 'badge badge-muted' }, 'Rozłączono');
  }

  function updateCard(c, b) {
    const s = b.stats;
    const users = usersOf(b.id);
    const titleId = `bus-${b.id}-title`;
    c.el.setAttribute('aria-labelledby', titleId);
    const rows = [
      ['Typ', KINDS[b.kind] || b.kind],
      ['Parametry', b.describe],
    ];
    const sig = JSON.stringify(users.map((d) => [d.id, d.name, d.unit]));
    if (sig !== c.usersSig) {
      c.usersSig = sig;
      fill(c.usersBox, h('span', { class: 'label' }, 'Urządzenia'), users.length
        ? h('ul', null, users.map((d) => h('li', null,
          h('a', { href: `#devices?edit=${enc(d.id)}` }, d.name || d.id), h('span', { class: 'muted' }, ` (Unit ID ${d.unit})`))))
        : h('p', { class: 'muted' }, 'brak - dodaj licznik przyciskiem "Dodaj urządzenie"'));
    }
    if (s) {
      const errPct = s.requests ? ` (${num((s.errors / s.requests) * 100, 1)}%)` : '';
      rows.push(
        ['Zapytania', String(s.requests ?? 0)],
        ['Błędy', h('span', { class: s.errors ? 'err-text' : '' }, `${s.errors ?? 0}${errPct}`,
          h('span', { class: 'muted' }, ` (timeout: ${s.timeouts ?? 0}${s.exceptions != null ? `, wyjątki Modbus: ${s.exceptions}` : ''})`))],
        ['Średni czas', s.avg_ms != null ? `${num(s.avg_ms, 1)} ms` : '-'],
        ['Ostatnia odpowiedź', s.last_ok_ts ? `${fmtTime(s.last_ok_ts)} (${fmtAge(Math.max(0, Date.now() / 1000 - s.last_ok_ts))})` : '-'],
      );
      if (s.last_error) {
        rows.push(['Ostatni błąd', [h('span', { class: 'err-text' }, s.last_error),
          s.last_error_ts ? h('span', { class: 'muted' }, ` (${fmtTime(s.last_error_ts)})`) : null]]);
      }
    }
    fill(c.body,
      h('div', { class: 'bus-head' },
        h('div', { class: 'bus-title' },
          h('h3', { id: titleId }, b.name || b.id),
          h('span', { class: 'mono small muted' }, b.id)),
        h('div', { class: 'bus-badges' },
          b.locked ? h('span', { class: 'badge badge-muted plain', title: 'Ustawione parametrami uruchomienia' }, 'CLI') : null,
          connBadge(b))),
      h('dl', { class: 'kv' }, rows.map(([k, v]) => [h('dt', null, k), h('dd', null, v)])));

    const editLbl = b.locked ? 'Szczegóły' : 'Edytuj';
    c.btns.edit.querySelector('.lbl').textContent = editLbl;
    c.btns.edit.setAttribute('aria-label', `${editLbl} ${b.name || b.id}`);
    c.btns.add.setAttribute('aria-label', `Dodaj urządzenie na ${b.name || b.id}`);
    c.btns.test.setAttribute('aria-label', `Testuj ${b.name || b.id}`);
    c.btns.remove.setAttribute('aria-label', `Usuń ${b.name || b.id}`);
    const cantDelete = b.locked || b.id === 'default';
    if (!c.btns.remove.dataset.busy) c.btns.remove.disabled = cantDelete;
    c.btns.remove.hidden = cantDelete;
    let note = '';
    if (b.locked && b.id === 'sim') note = 'Połączenie wbudowanego symulatora - tworzone przy starcie (--modbus-port, --sim-framing; wyłączysz je flagą --no-sim). Tylko do odczytu.';
    else if (b.locked) note = 'Ustawione parametrami uruchomienia (--serial, --tcp lub --rtu-over-tcp). Tylko do odczytu - zmiana wymaga ponownego uruchomienia aplikacji z innymi parametrami.';
    else if (b.id === 'default') note = 'Połączenie domyślne (używane, gdy nie wskazano innego) - można je edytować, ale nie usunąć.';
    c.note.hidden = !note;
    c.note.textContent = note;
  }

  // ── akcje ─────────────────────────────────────────────────
  async function remove(id, btn) {
    const b = busById(id);
    if (!b || b.locked) return;
    const users = usersOf(id);
    const text = users.length
      ? `Połączenie "${b.name}" jest używane przez: ${users.map((d) => d.name || d.id).join(', ')}. Serwer nie pozwoli go usunąć, dopóki nie usuniesz lub nie przeniesiesz tych urządzeń. Spróbować mimo to?`
      : `Usunąć połączenie "${b.name}" (${b.describe})?`;
    const ok = await confirmDialog(text, { title: 'Usuń połączenie' });
    if (!ok || destroyed) return;
    await busy(btn, async () => {
      try {
        await del(`/api/buses/${enc(id)}`, { signal: ac.signal });
        if (destroyed) return;
        toast(`Usunięto połączenie "${b.name}"`);
        state.buses = state.buses.filter((x) => x.id !== id);
        render();
      } catch (e) {
        showError(e, 'Nie udało się usunąć: ');
      }
    });
    poller.trigger();
  }

  function closeOpen() {
    if (openModal) openModal.close();
  }
  function track(m) {
    openModal = m;
    return m;
  }
  function untrack(m) {
    if (openModal === m) openModal = null;
  }

  /** Okno testu zapisanego połączenia (opcjonalnie z Unit ID). */
  function openPing(b) {
    if (!b) return;
    closeOpen();
    const firstUnit = usersOf(b.id)[0];
    const unitIn = h('input', { type: 'number', id: uid(), min: 0, max: 255, step: 1, inputmode: 'numeric',
      value: firstUnit ? String(firstUnit.unit) : '', placeholder: 'np. 1' });
    const result = h('div', { class: 'ping-out', 'aria-live': 'polite' });
    const go = h('button', { class: 'btn btn-primary', type: 'submit' }, 'Testuj');
    const formEl = h('form', { class: 'v-connections ping-form', novalidate: true, onsubmit: async (ev) => {
      ev.preventDefault();
      const raw = unitIn.value.trim();
      const unit = raw === '' ? null : parseIntStrict(raw);
      if (raw !== '' && (unit == null || unit > 255)) { fieldError(unitIn, 'Unit ID to liczba 0-255 albo puste pole'); unitIn.focus(); return; }
      fieldError(unitIn, null);
      await busy(go, async () => {
        fill(result, h('div', { class: 'small muted' }, h('span', { class: 'spinner' }), ' Testowanie...'));
        try {
          const out = await runPing(post(`/api/buses/${enc(b.id)}/ping`, unit == null ? {} : { unit }, { signal: ac.signal }), unit, b.kind);
          if (!destroyed) fill(result, out);
        } catch (e) {
          if (e.name !== 'AbortError') showError(e);
        }
      });
      poller.trigger();
    } },
    h('div', { class: 'toolbar' },
      field('Unit ID (opcjonalnie)', unitIn),
      go),
    h('p', { class: 'small muted' }, 'Puste pole = sprawdzenie samego połączenia (otwarcie portu / połączenie TCP). Z Unit ID test sprawdza też odpowiedź licznika.'),
    result);
    const m = track(modal({
      title: `Test połączenia: ${b.name || b.id}`,
      subtitle: b.describe,
      body: formEl,
      actions: [{ label: 'Zamknij' }],
      onClose: () => untrack(m),
    }));
  }

  // ── formularz ─────────────────────────────────────────────
  function openForm(bus) {
    closeOpen();
    const isNew = !bus;
    const locked = !!(bus && bus.locked);
    const b = { ...DEFAULTS, ...(bus || {}) };
    let idTouched = false;

    const nameIn = h('input', { type: 'text', id: uid(), maxlength: 64, autocomplete: 'off', value: b.name || '', placeholder: 'np. RS-485 rozdzielnia' });
    const idIn = h('input', { type: 'text', id: uid(), maxlength: 32, autocomplete: 'off', spellcheck: 'false', class: 'mono',
      value: isNew ? '' : b.id, placeholder: 'np. rs485', readonly: !isNew });
    const kindSel = select(Object.entries(KINDS), b.kind, { id: uid() });
    const kindHint = h('span');
    const hostIn = h('input', { type: 'text', id: uid(), autocomplete: 'off', spellcheck: 'false', value: b.host || '', placeholder: 'np. 192.168.1.50' });
    const portIn = h('input', { type: 'number', id: uid(), min: 1, max: 65535, step: 1, inputmode: 'numeric', value: String(b.port || 502) });
    const portHint = h('span');
    const listId = uid();
    const serialIn = h('input', { type: 'text', id: uid(), autocomplete: 'off', spellcheck: 'false', list: listId, value: b.serial_port || '', placeholder: '/dev/ttyUSB0' });
    const datalist = h('datalist', { id: listId });
    const portsBtn = h('button', { class: 'icon-btn', type: 'button', title: 'Odśwież listę portów', 'aria-label': 'Odśwież listę portów szeregowych', onclick: (e) => refreshPorts(e.currentTarget),
      html: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M21 12a9 9 0 1 1-2.64-6.36"/><path d="M21 3v6h-6"/></svg>' });
    const portChips = h('div', { class: 'port-chips' });
    const bauds = BAUDRATES.includes(Number(b.baudrate)) ? BAUDRATES : [...BAUDRATES, Number(b.baudrate)].sort((x, y) => x - y);
    const baudSel = select(bauds.map((v) => [String(v), String(v)]), String(b.baudrate), { id: uid() });
    const paritySel = select(PARITIES, b.parity, { id: uid() });
    const stopSel = select([['1', '1'], ['2', '2']], String(b.stopbits), { id: uid() });
    const byteSel = select([['8', '8'], ['7', '7']], String(b.bytesize), { id: uid() });
    const serialPreview = h('span', { class: 'mono' });
    const echoIn = h('input', { type: 'checkbox', id: uid(), checked: !!b.local_echo });
    const timeoutIn = h('input', { type: 'text', id: uid(), inputmode: 'decimal', autocomplete: 'off', value: String(b.timeout).replace('.', ',') });
    const retriesIn = h('input', { type: 'number', id: uid(), min: 0, max: 10, step: 1, inputmode: 'numeric', value: String(b.retries) });
    const delayIn = h('input', { type: 'number', id: uid(), min: 0, max: 5000, step: 1, inputmode: 'numeric', value: String(b.delay_ms) });
    const testUnitIn = h('input', { type: 'number', id: uid(), min: 0, max: 255, step: 1, inputmode: 'numeric', placeholder: 'np. 1',
      value: bus && usersOf(bus.id)[0] ? String(usersOf(bus.id)[0].unit) : '' });
    const testBtn = h('button', { class: 'btn btn-ghost', type: 'button', onclick: test }, 'Testuj połączenie');
    const testOut = h('div', { class: 'ping-out', 'aria-live': 'polite' });
    const formErr = h('div', { class: 'notice notice-err', role: 'alert', hidden: true });
    const saveBtn = h('button', { class: 'btn btn-primary', type: 'submit' }, isNew ? 'Dodaj połączenie' : 'Zapisz');
    const cancelBtn = h('button', { class: 'btn btn-ghost', type: 'button', onclick: () => m.close() }, locked ? 'Zamknij' : 'Anuluj');

    const tcpGroup = h('div', { class: 'form-grid host-grid' },
      field('Host / adres IP', hostIn),
      field('Port TCP', portIn),
      h('div', { class: 'field span-2 small muted' }, portHint));
    const serialGroup = h('div', { class: 'form-grid tight' },
      h('div', { class: 'field span-2' },
        h('label', { for: serialIn.id }, 'Port szeregowy'),
        h('div', { class: 'serial-row' }, serialIn, portsBtn),
        datalist, portChips),
      field('Prędkość [bit/s]', baudSel),
      field('Parzystość', paritySel),
      field('Bity stopu', stopSel),
      field('Bity danych', byteSel),
      h('div', { class: 'field span-2 small muted' }, h('span', null, 'Ramka: ', serialPreview)),
      h('div', { class: 'field span-2' },
        h('div', { class: 'field inline' }, echoIn,
          h('label', { for: echoIn.id, style: { textTransform: 'none', fontSize: '13px', color: 'var(--text)', letterSpacing: '0' } },
            'Adapter z lokalnym echem')),
        h('span', { class: 'hint' }, 'Zaznacz, gdy przejściówka odsyła własną transmisję (odbiornik włączony na stałe) - objaw: błędy "odpowiedź nie pasuje do zapytania".')));

    const isSerial = () => SERIAL_KINDS.has(kindSel.value);

    function renderPorts() {
      const ports = state.ports || [];
      fill(datalist, ports.map((p) => h('option', { value: p.device }, p.description || p.device)));
      if (state.ports == null) { fill(portChips, h('span', { class: 'hint' }, 'Wyszukiwanie portów...')); return; }
      if (!ports.length) {
        fill(portChips, h('span', { class: 'hint' }, 'Nie wykryto portów - wpisz ręcznie, np. /dev/serial0 (UART RPi), /dev/ttyUSB0 (USB), COM3 (Windows).'));
        return;
      }
      fill(portChips, h('span', { class: 'hint' }, 'Wykryte:'), ports.slice(0, 8).map((p) => h('button', {
        class: 'chip mono', type: 'button', disabled: locked, title: [p.description, p.hwid].filter(Boolean).join(' - '),
        onclick: () => { serialIn.value = p.device; fieldError(serialIn, null); serialIn.focus(); },
      }, p.device)));
    }
    async function refreshPorts(btn) {
      const run = async () => { await loadPorts(); if (!destroyed) renderPorts(); };
      if (btn) await busy(btn, run); else await run();
    }

    function updateKind() {
      const serial = isSerial();
      tcpGroup.hidden = serial;
      serialGroup.hidden = !serial;
      // błędy z ukrytej grupy pól nie mają już znaczenia
      for (const el of serial ? [hostIn, portIn] : [serialIn]) fieldError(el, null);
      kindHint.textContent = KIND_HELP[kindSel.value] || '';
      portHint.textContent = kindSel.value === 'rtu_over_tcp'
        ? 'Bramki transparentne często używają 8899 (USR, Elfin EW11)'
        : 'Standardowo 502';
      if (serial && state.ports == null) refreshPorts();
    }
    function updatePreview() {
      serialPreview.textContent = `${baudSel.value} ${byteSel.value}${paritySel.value}${stopSel.value}`;
    }
    function suggestId() {
      if (!isNew || idTouched) return;
      const taken = new Set(state.buses.map((x) => x.id));
      idIn.value = nameIn.value.trim() ? uniqueId(slugify(nameIn.value), taken) : '';
      fieldError(idIn, null);
    }
    kindSel.addEventListener('change', updateKind);
    for (const s of [baudSel, paritySel, stopSel, byteSel]) s.addEventListener('change', updatePreview);
    nameIn.addEventListener('input', suggestId);
    idIn.addEventListener('input', () => { idTouched = !!idIn.value; fieldError(idIn, null); });
    for (const el of [hostIn, portIn, serialIn, timeoutIn, retriesIn, delayIn, testUnitIn]) {
      el.addEventListener('input', () => fieldError(el, null));
    }
    renderPorts();
    updateKind();
    updatePreview();

    function collect({ forTest = false } = {}) {
      const errs = [];
      const set = (el, msg) => { fieldError(el, msg); if (msg) errs.push(el); };
      const serial = isSerial();
      if (isNew && !forTest) {
        const id = idIn.value.trim();
        if (!id) set(idIn, 'Podaj identyfikator');
        else if (!ID_RE.test(id)) set(idIn, 'Dozwolone: małe litery, cyfry, "_" i "-" (max 32 znaki, na początku litera lub cyfra)');
        else if (busById(id)) set(idIn, 'Połączenie o takim identyfikatorze już istnieje');
        else set(idIn, null);
      }
      const port = parseIntStrict(portIn.value);
      if (!serial) {
        const host = hostIn.value.trim();
        if (!host) set(hostIn, 'Podaj adres IP lub nazwę hosta');
        else if (/[\s/]/.test(host)) set(hostIn, 'Sam adres, bez spacji, "http://" i portu');
        else set(hostIn, null);
        if (port == null || port < 1 || port > 65535) set(portIn, 'Port 1-65535');
        else set(portIn, null);
      } else {
        if (!serialIn.value.trim()) set(serialIn, 'Podaj port szeregowy, np. /dev/ttyUSB0 albo COM3');
        else set(serialIn, null);
      }
      const timeout = parseDecimal(timeoutIn.value);
      if (timeout == null || timeout < 0.05 || timeout > 60) set(timeoutIn, 'Timeout w sekundach: 0,05-60');
      else set(timeoutIn, null);
      const retries = parseIntStrict(retriesIn.value);
      if (retries == null || retries > 10) set(retriesIn, 'Liczba 0-10');
      else set(retriesIn, null);
      const delay = parseIntStrict(delayIn.value);
      if (delay == null || delay > 5000) set(delayIn, 'Liczba 0-5000 ms');
      else set(delayIn, null);
      if (errs.length) { errs[0].focus(); return null; }
      return {
        id: isNew ? idIn.value.trim() : b.id,
        body: {
          name: nameIn.value.trim() || (isNew ? idIn.value.trim() : b.id),
          kind: kindSel.value,
          host: serial ? (b.host || '127.0.0.1') : hostIn.value.trim(),
          port: serial ? (b.port || 502) : port,
          serial_port: serialIn.value.trim(),
          baudrate: Number(baudSel.value),
          parity: paritySel.value,
          stopbits: Number(stopSel.value),
          bytesize: Number(byteSel.value),
          timeout,
          retries,
          delay_ms: delay,
          local_echo: serial ? echoIn.checked : false,
        },
      };
    }

    function showServerError(e) {
      formErr.hidden = false;
      fill(formErr, h('strong', null, 'Serwer odrzucił zmiany: '), e.message || String(e),
        e.errors && e.errors.length ? h('ul', { class: 'errors' }, e.errors.map((x) => h('li', null, x))) : null);
      const low = (e.message || '').toLowerCase();
      const map = [['identyfikator', idIn], ['rodzaj', kindSel], ['host', hostIn], ['port szereg', serialIn], ['serial', serialIn],
        ['baud', baudSel], ['prędko', baudSel], ['parzyst', paritySel], ['parity', paritySel], ['stop', stopSel],
        ['bytesize', byteSel], ['timeout', timeoutIn], ['retries', retriesIn], ['ponowie', retriesIn], ['delay', delayIn],
        ['przerw', delayIn], ['port', isSerial() ? serialIn : portIn]];
      const hit = map.find(([k]) => low.includes(k));
      if (hit) fieldError(hit[1], e.message);
    }

    async function test() {
      if (testBtn.dataset.busy) return;
      const v = locked ? { body: null } : collect({ forTest: true });
      if (!v) return;
      const raw = testUnitIn.value.trim();
      const unit = raw === '' ? null : parseIntStrict(raw);
      if (raw !== '' && (unit == null || unit > 255)) { fieldError(testUnitIn, 'Unit ID 0-255 albo puste'); testUnitIn.focus(); return; }
      fieldError(testUnitIn, null);
      await busy(testBtn, async () => {
        fill(testOut, h('div', { class: 'small muted' }, h('span', { class: 'spinner' }), ' Testowanie...'));
        const extra = unit == null ? {} : { unit };
        try {
          const req = locked
            ? post(`/api/buses/${enc(b.id)}/ping`, extra, { signal: ac.signal })
            : post('/api/buses/test', { ...v.body, ...extra }, { signal: ac.signal });
          const out = await runPing(req, unit, kindSel.value);
          if (!destroyed) fill(testOut, out);
        } catch (e) {
          if (e.name !== 'AbortError') showError(e);
        }
      });
    }

    async function submit(ev) {
      ev.preventDefault();
      if (locked || saveBtn.dataset.busy) return;
      formErr.hidden = true;
      const v = collect();
      if (!v) return;
      await busy(saveBtn, async () => {
        try {
          if (isNew) {
            // PUT nadpisuje - sprawdź świeżą listę, żeby nie nadpisać cudzej magistrali
            const fresh = await get('/api/buses', { signal: ac.signal });
            state.buses = fresh;
            if (fresh.some((x) => x.id === v.id)) {
              fieldError(idIn, 'Połączenie o takim identyfikatorze już istnieje');
              idIn.focus();
              return;
            }
          }
          const res = await put(`/api/buses/${enc(v.id)}`, v.body, { signal: ac.signal });
          if (destroyed) return;
          toast(isNew ? `Dodano połączenie "${res.name}"` : `Zapisano "${res.name}"`);
          const i = state.buses.findIndex((x) => x.id === res.id);
          if (i >= 0) state.buses[i] = res; else state.buses.push(res);
          render();
          m.close();
          poller.trigger();
          ctx.refreshHealth();
        } catch (e) {
          if (e.name === 'AbortError') return;
          showServerError(e);
          showError(e, 'Zapis: ');
        }
      });
    }

    const lockNote = locked ? h('div', { class: 'notice notice-warn' },
      h('strong', null, 'Tylko do odczytu. '),
      b.id === 'sim'
        ? ['To połączenie wbudowanego symulatora, tworzone przy starcie aplikacji (port ', h('code', null, '--modbus-port'),
          ', ramkowanie ', h('code', null, '--sim-framing'), '). Wyłączysz je flagą ', h('code', null, '--no-sim'), '.']
        : ['To połączenie ustawiono flagą ', h('code', null, '--serial'), ', ', h('code', null, '--tcp'), ' lub ',
          h('code', null, '--rtu-over-tcp'), ' przy uruchomieniu. Aby je zmienić, uruchom aplikację ponownie z innymi parametrami, np. ',
          h('code', null, 'python app.py --serial /dev/ttyUSB0 --baudrate 9600 --parity E'),
          ', albo uruchom bez tych flag i skonfiguruj połączenie tutaj.'],
      ' Test połączenia działa normalnie.') : null;

    const formEl = h('form', { class: 'v-connections bus-form', novalidate: true, onsubmit: submit },
      lockNote,
      formErr,
      h('div', { class: 'form-grid' },
        field('Nazwa', nameIn, null),
        field('Identyfikator', idIn, isNew ? 'Podpowiadany z nazwy; używany przez urządzenia' : 'Nie można zmienić'),
        field('Rodzaj połączenia', kindSel, kindHint, { class: 'field span-2' })),
      tcpGroup,
      serialGroup,
      h('details', { class: 'bus-advanced', open: !isNew && (b.retries !== 1 || b.delay_ms || b.timeout !== 1) ? true : null },
        h('summary', null, 'Zaawansowane: timeout, ponowienia, przerwa'),
        h('div', { class: 'form-grid tight' },
          field('Timeout [s]', timeoutIn, 'Czas oczekiwania na odpowiedź'),
          field('Ponowienia', retriesIn, 'Przy braku odpowiedzi'),
          field('Przerwa [ms]', delayIn, 'Między ramkami; wolne liczniki: 20-50'))),
      h('div', { class: 'test-box' },
        h('div', { class: 'field' },
          h('label', { for: testUnitIn.id }, 'Unit ID do testu (opcjonalnie)'),
          h('div', { class: 'test-row' }, testUnitIn, testBtn)),
        testOut),
      h('div', { class: 'form-actions' }, cancelBtn, locked ? null : saveBtn));

    if (locked) {
      for (const el of formEl.querySelectorAll('input,select')) {
        if (el !== testUnitIn) el.disabled = true;
      }
      portsBtn.disabled = true;
    }

    const m = track(modal({
      title: isNew ? 'Nowe połączenie' : (locked ? 'Szczegóły połączenia' : 'Edycja połączenia'),
      subtitle: isNew ? 'Port RS-485 albo adres bramki / licznika Modbus TCP' : `${b.name || b.id} (${b.id})`,
      body: formEl,
      onClose: () => untrack(m),
    }));
    if (locked) cancelBtn.focus();
  }

  function helpCard() {
    return h('aside', { class: 'card conn-help', 'aria-labelledby': 'conn-help-title' },
      h('div', { class: 'card-header' }, h('h3', { class: 'card-title', id: 'conn-help-title' }, 'Jak to działa')),
      h('div', { class: 'prose' },
        h('p', null, h('strong', null, 'Połączenie'), ' to droga do liczników: port RS-485 (przejściówka USB, UART Raspberry Pi) albo adres licznika / bramki Modbus TCP. ',
          h('strong', null, 'Urządzenie'), ' to konkretny licznik o danym Unit ID na tym połączeniu - jedna magistrala RS-485 może obsłużyć wiele liczników o różnych adresach.'),
        h('h3', null, 'Typowe ustawienia'),
        h('ul', null,
          h('li', null, 'Eastron SDM120 / SDM220: ', h('code', null, '2400 8N1'), ' (fabrycznie)'),
          h('li', null, 'Wiele liczników (Eastron SDM630, Finder, Carlo Gavazzi): ', h('code', null, '9600 8N1')),
          h('li', null, 'Orno OR-WE-5xx: często ', h('code', null, '9600 8E1')),
          h('li', null, 'Unit ID fabrycznie zwykle ', h('code', null, '1'), '; każdy licznik na magistrali musi mieć inny.')),
        h('h3', null, 'Nazwy portów'),
        h('ul', null,
          h('li', null, 'Raspberry Pi UART: ', h('code', null, '/dev/serial0'), ' (włącz UART w raspi-config, wyłącz konsolę szeregową)'),
          h('li', null, 'Przejściówka USB-RS485: ', h('code', null, '/dev/ttyUSB0')),
          h('li', null, 'Windows: ', h('code', null, 'COM3'), ' (numer sprawdzisz w Menedżerze urządzeń)')),
        h('h3', null, 'Brak odpowiedzi?'),
        h('ul', null,
          h('li', null, 'Zamień przewody A i B, sprawdź wspólną masę i terminator 120 Ω na końcach linii.'),
          h('li', null, 'Sprawdź prędkość, parzystość i Unit ID na wyświetlaczu licznika.'),
          h('li', null, 'Wolne liczniki: zwiększ timeout i dodaj przerwę 20-50 ms.'),
          h('li', null, 'Bramka TCP: tryb "Modbus TCP" (port 502) albo transparentny "RTU over TCP" (często 8899).'))));
  }

  // ── start ─────────────────────────────────────────────────
  (async () => {
    await refresh();
    if (destroyed) return;
    poller.start(false);
    const p = ctx.params || {};
    if (p.new === '1') openForm(null);
    else if (p.edit) {
      const b = busById(p.edit);
      if (b) openForm(b);
      else if (state.loaded) toast(`Nie znaleziono połączenia "${p.edit}"`, 'err');
    }
  })();

  return {
    unmount() {
      destroyed = true;
      poller.stop();
      ac.abort();
      if (openModal) openModal.close();
    },
  };
}
