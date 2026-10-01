// Widok: Integracje - MQTT / Home Assistant, historia w SQLite, Prometheus, REST API i informacje o systemie.

import {
  h, mount as fill, get, put, enc, toast, showError, Poller, field, select, pageHeader, fmtTime, uid,
  parseIntStrict, plural, busy, fieldError,
} from '../core.js';

const STATUS_MS = 3000;
const MASK = '********';
const PREVIEW_LINES = 40;
const CONNECT_GRACE_MS = 10000;   // po tylu ms bez połączenia (i bez błędu z serwera) pokazujemy "brak połączenia"
const BUCKETS = [
  [10, '10 s (najdokładniej, największa baza)'],
  [30, '30 s'],
  [60, '1 min (zalecane)'],
  [300, '5 min'],
  [900, '15 min (najmniejsza baza)'],
];
const LOCAL_HOSTS = new Set(['localhost', '127.0.0.1', '::1', '[::1]']);
const PREFERRED_SAMPLE_KEYS = ['voltage_l1', 'voltage', 'current_l1', 'current', 'power_total', 'power', 'energy_import', 'frequency'];

// ── pomocnicze ───────────────────────────────────────────────

/** Klucz jak w backendzie (quantities.safe_key) - używany w nazwach topiców MQTT. */
function safeKey(s) {
  return String(s).replace(/[^A-Za-z0-9_]+/g, '_').replace(/^_+|_+$/g, '') || 'value';
}

const cleanTopic = (s) => String(s ?? '').trim().replace(/^\/+|\/+$/g, '');

/** Argument powłoki (sh) w apostrofach, gdy zawiera znaki specjalne. */
function shq(s) {
  s = String(s);
  return /^[\w.,:/@%+=-]+$/.test(s) ? s : "'" + s.replace(/'/g, "'\\''") + "'";
}

function fmtDuration(sec) {
  if (sec < 120) return `${Math.round(sec)} s`;
  if (sec < 3600) return `${Math.round(sec / 60)} min`;
  if (sec < 172800) return `${(sec / 3600).toLocaleString('pl-PL', { maximumFractionDigits: 1 })} h`;
  return `${(sec / 86400).toLocaleString('pl-PL', { maximumFractionDigits: 1 })} dni`;
}

/** Zdejmuje komunikaty błędów ze wszystkich oznaczonych pól w root. */
function clearFieldErrors(root) {
  root.querySelectorAll('[aria-invalid="true"]').forEach((c) => fieldError(c, null));
}

/** Pole wyboru z etykietą obok (i opcjonalną podpowiedzią pod spodem). */
function checkRow(label, hint) {
  const input = h('input', { type: 'checkbox', id: uid('i') });
  const hintEl = hint ? h('span', { class: 'hint', id: input.id + '-h' }, hint) : null;
  if (hintEl) input.setAttribute('aria-describedby', hintEl.id);
  return { input, el: h('div', { class: 'int-check' }, input, h('label', { for: input.id }, label), hintEl) };
}

/** Kopiowanie do schowka: Clipboard API (tylko HTTPS/localhost), potem execCommand, na końcu zaznaczenie tekstu. */
async function copyText(text, sourceEl) {
  if (navigator.clipboard && window.isSecureContext) {
    try {
      await navigator.clipboard.writeText(text);
      return true;
    } catch (e) {
      console.info('Clipboard API niedostępne, próba execCommand:', e && e.message);
    }
  }
  const prev = document.activeElement;
  const ta = h('textarea', { readonly: true, 'aria-hidden': 'true', style: { position: 'fixed', top: '0', left: '0', width: '1px', height: '1px', opacity: '0' } });
  ta.value = text;
  document.body.append(ta);
  ta.select();
  let ok = false;
  try { ok = document.execCommand('copy'); } catch { ok = false; }
  ta.remove();
  if (prev && prev.focus) prev.focus();
  if (!ok && sourceEl) {
    const sel = window.getSelection();
    const range = document.createRange();
    range.selectNodeContents(sourceEl);
    sel.removeAllRanges();
    sel.addRange(range);
  }
  return ok;
}

// ── widok ────────────────────────────────────────────────────

export function mount(root, ctx) {
  const ac = new AbortController();
  const timers = new Set();
  const life = {
    signal: ac.signal,
    alive: () => !ac.signal.aborted,
    timeout(fn, ms) {
      const t = setTimeout(() => { timers.delete(t); fn(); }, ms);
      timers.add(t);
      return t;
    },
    copyBtn(getText, getSource, aria) {
      const b = h('button', {
        type: 'button', class: 'btn btn-ghost btn-sm int-copy-btn', 'aria-label': aria,
        onclick: async () => {
          const ok = await copyText(getText(), getSource());
          if (!life.alive()) return;
          if (ok) {
            b.textContent = 'Skopiowano';
            life.timeout(() => { b.textContent = 'Kopiuj'; }, 1500);
            toast('Skopiowano do schowka', 'ok', 1500);
          } else {
            toast('Nie udało się skopiować automatycznie - tekst jest zaznaczony, naciśnij Ctrl+C.', 'info', 6000);
          }
        },
      }, 'Kopiuj');
      return b;
    },
  };

  const info = infoCard(ctx, life);
  const mqtt = mqttCard(life);
  const hist = historyCard(ctx, life, () => info.render(ctx.info));
  const prom = prometheusCard(ctx, life);
  const rest = restCard(ctx, life);

  fill(root, h('div', { class: 'v-integrations' },
    pageHeader('Integracje'),
    h('div', { class: 'page-content' },
      mqtt.el,
      h('div', { class: 'int-cols' }, hist.el, prom.el),
      rest.el,
      info.el)));

  window.scrollTo({ top: 0, behavior: 'instant' });   // router przewija tylko przy zmianie widoku
  mqtt.start();
  hist.load();

  (async () => {
    let list = [];
    let err = null;
    try {
      list = await get('/api/devices', { signal: ac.signal });
    } catch (e) {
      if (e.name === 'AbortError' || !life.alive()) return;
      err = e;
      showError(e, 'Lista urządzeń: ');
    }
    if (!life.alive()) return;
    mqtt.setDevices(list, err);
    hist.setDevices(list);
    rest.setDevices(list, err);
  })();

  return {
    unmount() {
      ac.abort();
      mqtt.stop();
      timers.forEach(clearTimeout);
      timers.clear();
    },
  };
}

// ── MQTT / Home Assistant ────────────────────────────────────

function mqttState(st, waitingLong) {
  if (!st.enabled) return ['Wyłączone', 'badge-muted'];
  if (!st.available) return ['Brak paho-mqtt', 'badge-err'];
  if (st.connected) return ['Połączono', 'badge-ok'];
  if (st.last_error || waitingLong) return ['Brak połączenia', 'badge-err'];
  return ['Łączenie...', 'badge-info'];
}

function mqttCard(life) {
  let saved = null;            // ostatnie ustawienia z serwera
  let status = null;
  let lastPoll = null;
  let gen = 0;                 // zmienia się przy każdym zapisie - odrzuca spóźnione odpowiedzi odpytywania
  let waitingSince = null;     // od kiedy włączone i niepołączone (czas klienta)
  let devices = null;
  let devError = null;
  let sampleDevId = null;
  const samples = new Map();   // id urządzenia -> {values} | {error} | {loading}

  // ── formularz ──
  const en = checkRow('Włącz publikację MQTT', 'odczyty wszystkich aktywnych liczników trafiają do brokera');
  const host = h('input', { type: 'text', autocomplete: 'off', spellcheck: 'false', placeholder: 'np. 192.168.1.10' });
  const port = h('input', { type: 'number', min: '1', max: '65535', step: '1', inputmode: 'numeric' });
  const user = h('input', { type: 'text', autocomplete: 'off', spellcheck: 'false' });
  const pass = h('input', { type: 'password', autocomplete: 'new-password' });
  const passHint = h('span', { class: 'hint' });
  const passClear = checkRow('Usuń zapisane hasło');
  const tls = checkRow('TLS (szyfrowane połączenie)', 'zwykle port 8883; certyfikat brokera musi być zaufany w systemie');
  const prefix = h('input', { type: 'text', spellcheck: 'false', placeholder: 'modbus-dash' });
  const interval = h('input', { type: 'number', min: '1', max: '3600', step: '1', inputmode: 'numeric' });
  const retain = checkRow('Retain dla odczytów', 'broker pamięta ostatni stan; nowy klient (np. HA po restarcie) dostaje go od razu');
  const ha = checkRow('Home Assistant discovery', 'czujniki pojawią się w Home Assistant automatycznie');
  const haPrefix = h('input', { type: 'text', spellcheck: 'false', placeholder: 'homeassistant' });

  const passField = field('Hasło', pass);
  passField.append(passHint, passClear.el);

  const errBox = h('div', { class: 'notice notice-err int-err', role: 'alert', hidden: true });
  const dirtyBadge = h('span', { class: 'badge badge-warn', hidden: true }, 'Niezapisane zmiany');
  const saveBtn = h('button', { type: 'submit', class: 'btn btn-primary' }, 'Zapisz');
  const resetBtn = h('button', {
    type: 'button', class: 'btn btn-ghost',
    onclick: () => { if (saved) { fillForm(saved); clearErrors(); } },
  }, 'Cofnij zmiany');

  const fs = h('fieldset', { disabled: true },
    h('legend', { class: 'int-sub' }, 'Broker'),
    h('div', { class: 'form-grid' },
      h('div', { class: 'span-all' }, en.el),
      field('Host brokera', host, 'adres IP lub nazwa, bez mqtt://'),
      field('Port', port, '1883, z TLS zwykle 8883'),
      field('Użytkownik', user, 'puste = bez logowania'),
      passField,
      h('div', { class: 'span-all' }, tls.el)),
    h('div', { class: 'int-sub', role: 'presentation' }, 'Publikacja'),
    h('div', { class: 'form-grid' },
      field('Prefiks topików', prefix, 'początek nazw topików, np. modbus-dash'),
      field('Interwał publikacji [s]', interval, '1-3600; co ile wysyłać odczyty'),
      h('div', { class: 'span-all' }, retain.el),
      h('div', { class: 'span-all' }, ha.el),
      field('Prefiks discovery', haPrefix, 'w Home Assistant domyślnie homeassistant')),
    errBox,
    h('div', { class: 'form-actions' }, dirtyBadge, resetBtn, saveBtn));

  const formLoad = h('div', { class: 'status-line', 'aria-live': 'polite' }, h('span', { class: 'spinner' }), 'Wczytywanie ustawień MQTT...');
  const form = h('form', {
    class: 'int-form', novalidate: true, 'aria-label': 'Ustawienia MQTT',
    onsubmit: (e) => { e.preventDefault(); save(); },
    oninput: onEdit, onchange: onEdit,
  }, formLoad, fs);

  // ── panel stanu ──
  const headBadge = h('span', { class: 'badge badge-muted', role: 'status' }, 'Wczytywanie...');
  const paho = h('div', { class: 'notice notice-warn', hidden: true },
    h('strong', null, 'Brak biblioteki paho-mqtt. '),
    'Zainstaluj paho-mqtt: ', h('code', null, 'pip install paho-mqtt'),
    ' (w katalogu aplikacji, w tym samym środowisku Pythona), a potem uruchom aplikację ponownie.');
  const dd = {
    state: h('dd'), broker: h('dd'), published: h('dd'), discovered: h('dd'), error: h('dd'), updated: h('dd'),
  };
  const pollErr = h('div', { class: 'notice notice-err', hidden: true });
  const statusPanel = h('section', { class: 'int-panel', 'aria-labelledby': 'int-mqtt-st' },
    h('h4', { class: 'int-sub', id: 'int-mqtt-st' }, 'Stan połączenia'),
    paho, pollErr,
    h('dl', { class: 'kv' },
      h('dt', null, 'Stan'), dd.state,
      h('dt', null, 'Broker'), dd.broker,
      h('dt', null, 'Opublikowano'), dd.published,
      h('dt', null, 'Discovery HA'), dd.discovered,
      h('dt', null, 'Ostatni błąd'), dd.error,
      h('dt', null, 'Odświeżono'), dd.updated));

  // ── przykładowe topiki ──
  const devSelWrap = h('div', { class: 'int-devsel' });
  const topicList = h('ul', { class: 'int-topics' });
  const payloadPre = h('pre', { class: 'code int-payload' });
  const payloadNote = h('p', { class: 'small muted' });
  const subPre = h('pre', { class: 'code int-wrap' });
  const subCopy = life.copyBtn(() => subPre.textContent, () => subPre, 'Kopiuj polecenie mosquitto_sub');
  const topicsPanel = h('section', { class: 'int-panel', 'aria-labelledby': 'int-mqtt-tp' },
    h('div', { class: 'int-panel-head' }, h('h4', { class: 'int-sub', id: 'int-mqtt-tp' }, 'Przykładowe topiki'), devSelWrap),
    topicList,
    h('div', { class: 'label' }, 'Przykładowa treść state'),
    payloadPre, payloadNote,
    h('div', { class: 'label' }, 'Podgląd wiadomości w konsoli'),
    h('div', { class: 'int-copy' }, subPre, subCopy),
    h('div', {
      class: 'notice notice-info int-ha small',
      html: '<strong>Home Assistant:</strong> dodaj integrację <b>MQTT</b> (np. z dodatkiem Mosquitto broker) i wpisz tutaj ten sam broker. '
        + 'Przy włączonym discovery każdy licznik pojawi się w HA jako urządzenie z czujnikami napięcia, prądu, mocy i energii. '
        + 'Czujniki energii (kWh) mają <code>state_class: total_increasing</code>, więc można je dodać w panelu <b>Energia</b>. '
        + 'Po restarcie HA konfiguracja jest wysyłana ponownie automatycznie.',
    }));

  const el = h('section', { class: 'card', 'aria-labelledby': 'int-mqtt-t' },
    h('div', { class: 'card-header' }, h('h3', { class: 'card-title', id: 'int-mqtt-t' }, 'MQTT / Home Assistant'), headBadge),
    h('p', { class: 'small muted int-lead' }, 'Publikuje odczyty liczników do brokera MQTT (np. Mosquitto). Home Assistant, Node-RED czy openHAB mogą je stamtąd czytać.'),
    h('div', { class: 'int-mqtt' }, form, h('div', null, statusPanel, topicsPanel)));

  // ── logika formularza ──
  function syncDisabled() {
    haPrefix.disabled = !ha.input.checked;
    pass.disabled = passClear.input.checked;
  }

  function fillForm(s) {
    en.input.checked = !!s.enabled;
    host.value = s.host ?? '';
    port.value = s.port ?? '';
    user.value = s.username ?? '';
    pass.value = '';
    const hasPw = s.password === MASK;
    passClear.input.checked = false;
    passClear.el.hidden = !hasPw;
    pass.placeholder = hasPw ? 'zapisane - zostaw puste' : '';
    passHint.textContent = hasPw ? 'Hasło jest zapisane; wpisz nowe, aby je zmienić.' : 'puste = bez hasła';
    tls.input.checked = !!s.tls;
    prefix.value = s.topic_prefix ?? '';
    interval.value = s.interval ?? '';
    retain.input.checked = !!s.retain;
    ha.input.checked = !!s.ha_discovery;
    haPrefix.value = s.ha_prefix ?? '';
    syncDisabled();
    updateDirty();
    renderTopics();
  }

  /** Ustawienia zmienione gdzie indziej trafiają do pól, których użytkownik nie ruszał. */
  function mergeUntouched(prev, next) {
    const cur = snapshot(), old = savedSnapshot(prev), neu = savedSnapshot(next);
    const fields = {
      enabled: (v) => { en.input.checked = v; }, host: (v) => { host.value = v; }, port: (v) => { port.value = v; },
      username: (v) => { user.value = v; }, tls: (v) => { tls.input.checked = v; },
      topic_prefix: (v) => { prefix.value = v; }, interval: (v) => { interval.value = v; },
      retain: (v) => { retain.input.checked = v; }, ha_discovery: (v) => { ha.input.checked = v; },
      ha_prefix: (v) => { haPrefix.value = v; },
    };
    for (const [k, set] of Object.entries(fields)) {
      if (cur[k] === old[k] && neu[k] !== old[k]) set(neu[k]);
    }
    syncDisabled();
    renderTopics();
  }

  function snapshot() {
    return {
      enabled: en.input.checked, host: host.value.trim(), port: port.value.trim(), username: user.value.trim(),
      pw: pass.value !== '' || passClear.input.checked, tls: tls.input.checked, topic_prefix: prefix.value.trim(),
      interval: interval.value.trim(), retain: retain.input.checked, ha_discovery: ha.input.checked, ha_prefix: haPrefix.value.trim(),
    };
  }
  function savedSnapshot(s) {
    return {
      enabled: !!s.enabled, host: String(s.host ?? ''), port: String(s.port ?? ''), username: String(s.username ?? ''),
      pw: false, tls: !!s.tls, topic_prefix: String(s.topic_prefix ?? ''), interval: String(s.interval ?? ''),
      retain: !!s.retain, ha_discovery: !!s.ha_discovery, ha_prefix: String(s.ha_prefix ?? ''),
    };
  }
  function isDirty() {
    if (!saved) return false;
    const a = snapshot(), b = savedSnapshot(saved);
    return Object.keys(a).some((k) => a[k] !== b[k]);
  }
  const sameSettings = (a, b) => [...new Set([...Object.keys(a), ...Object.keys(b)])].every((k) => a[k] === b[k]);
  const updateDirty = () => { dirtyBadge.hidden = !isDirty(); };

  function onEdit(e) {
    const t = e.target;
    if (t === tls.input && e.type === 'change') {
      // wygoda: domyślny port dla TLS i bez TLS
      if (tls.input.checked && port.value.trim() === '1883') port.value = '8883';
      else if (!tls.input.checked && port.value.trim() === '8883') port.value = '1883';
    }
    if (t && t.getAttribute && t.getAttribute('aria-invalid') === 'true') fieldError(t, null);
    syncDisabled();
    updateDirty();
    renderTopics();
  }

  function clearErrors() {
    clearFieldErrors(form);
    errBox.hidden = true;
    errBox.replaceChildren();
  }

  function collect() {
    const errs = [];
    const enabled = en.input.checked;
    const hostV = host.value.trim();
    if (enabled && !hostV) errs.push([host, 'Podaj adres brokera']);
    else if (/\s|:\/\//.test(hostV)) errs.push([host, 'Podaj sam adres (np. 192.168.1.10), bez mqtt:// i spacji']);
    const portV = parseIntStrict(port.value);
    if (portV == null || portV < 1 || portV > 65535) errs.push([port, 'Port: liczba całkowita 1-65535']);
    const intV = parseIntStrict(interval.value);
    if (intV == null || intV < 1 || intV > 3600) errs.push([interval, 'Interwał: liczba całkowita 1-3600 s']);
    const pre = cleanTopic(prefix.value);
    if (!pre) errs.push([prefix, 'Podaj prefiks topików, np. modbus-dash']);
    else if (/[+#\s]/.test(pre)) errs.push([prefix, 'Prefiks nie może zawierać spacji ani znaków + i #']);
    const haOn = ha.input.checked;
    let haV = cleanTopic(haPrefix.value);
    if (haOn && !haV) errs.push([haPrefix, 'Podaj prefiks discovery (zwykle homeassistant)']);
    else if (haOn && /[+#\s]/.test(haV)) errs.push([haPrefix, 'Prefiks nie może zawierać spacji ani znaków + i #']);
    if (!haOn && (!haV || /[+#\s]/.test(haV))) haV = saved.ha_prefix || 'homeassistant';
    let password = pass.value;
    if (passClear.input.checked) password = '';
    else if (!password && saved.password === MASK) password = MASK;
    const moved = hostV !== saved.host || portV !== saved.port
      || user.value.trim() !== (saved.username || '') || tls.input.checked !== !!saved.tls;
    if (password === MASK && moved) {
      // serwer nie wyśle zapisanego hasła do innego brokera ani dla innego konta
      errs.push([pass, 'Zmieniono brokera, konto albo TLS - wpisz hasło ponownie (albo zaznacz „Usuń zapisane hasło”)']);
    }
    return {
      errs,
      body: {
        enabled, host: hostV, port: portV, username: user.value.trim(), password, tls: tls.input.checked,
        topic_prefix: pre, ha_discovery: haOn, ha_prefix: haV, interval: intV, retain: retain.input.checked,
      },
    };
  }

  const SERVER_FIELDS = [[/port/i, port], [/interwa/i, interval], [/prefiks/i, prefix], [/hasło/i, pass]];

  async function save() {
    if (!saved) return;
    clearErrors();
    const { body, errs } = collect();
    if (errs.length) {
      errs.forEach(([c, m]) => fieldError(c, m));
      errs[0][0].focus();
      return;
    }
    await busy(saveBtn, async () => {
      gen++;
      try {
        const res = await put('/api/settings/mqtt', body, { signal: life.signal });
        if (!life.alive()) return;
        gen++;
        waitingSince = null;
        applyServer(res, true);
        const s = res.settings;
        toast(s.enabled && res.status.available ? 'Zapisano ustawienia MQTT - łączenie z brokerem...' : 'Zapisano ustawienia MQTT', 'ok');
      } catch (e) {
        gen++;
        if (e.name === 'AbortError' || !life.alive()) return;
        const msg = e.message || String(e);
        errBox.replaceChildren(h('strong', null, 'Nie zapisano: '), msg);
        errBox.hidden = false;
        if (e.status === 400) SERVER_FIELDS.forEach(([re, c]) => { if (re.test(msg)) fieldError(c, msg); });
        else showError(e, 'MQTT: ');
      }
    });
  }

  function applyServer(data, fromSave) {
    const s = data.settings;
    const first = !saved;
    const wasDirty = isDirty();
    const changed = first || !sameSettings(saved, s);
    const prev = saved;
    saved = s;
    if (fromSave || first || (changed && !wasDirty)) fillForm(s);
    else {
      if (changed) mergeUntouched(prev, s);
      updateDirty();
    }
    if (first) {
      formLoad.remove();
      fs.disabled = false;
    }
    status = data.status;
    renderStatus();
  }

  // ── odpytywanie stanu ──
  const poller = new Poller(async () => {
    const myGen = gen;
    try {
      const data = await get('/api/settings/mqtt', { signal: life.signal });
      if (!life.alive() || myGen !== gen || saveBtn.dataset.busy) return;
      lastPoll = Date.now();
      applyServer(data, false);
    } catch (e) {
      if (e.name === 'AbortError' || !life.alive()) return;
      if (!saved) {
        formLoad.replaceChildren(h('div', { class: 'notice notice-err' },
          h('strong', null, 'Nie udało się wczytać ustawień MQTT: '), e.message, ' Ponawiam co 3 s. ',
          h('button', { type: 'button', class: 'btn btn-ghost btn-sm', onclick: () => poller.trigger() }, 'Spróbuj teraz')));
      }
      renderStatus(e);
    }
  }, STATUS_MS);

  function setText(node, text) {
    if (node.textContent !== text) node.textContent = text;
  }

  function renderStatus(err) {
    pollErr.hidden = !err;
    if (err) {
      pollErr.replaceChildren(h('strong', null, 'Nie udało się odświeżyć stanu: '), err.message || String(err), ' (ponawiam co 3 s)');
      setText(headBadge, 'Brak danych');
      headBadge.className = 'badge badge-warn';
      return;
    }
    if (!status || !saved) return;
    if (status.enabled && status.available && !status.connected) waitingSince = waitingSince || Date.now();
    else waitingSince = null;
    const waitingLong = waitingSince != null && Date.now() - waitingSince > CONNECT_GRACE_MS;
    const [label, cls] = mqttState(status, waitingLong);
    setText(headBadge, label);
    headBadge.className = 'badge ' + cls;
    paho.hidden = status.available;

    if (dd.state.textContent !== label) dd.state.replaceChildren(h('span', { class: 'badge ' + cls }, label));
    setText(dd.broker, `${saved.host || '-'}:${saved.port}${saved.tls ? ' (TLS)' : ''}${saved.username ? `, użytkownik ${saved.username}` : ''}`);
    const n = status.published || 0;
    setText(dd.published, `${n.toLocaleString('pl-PL')} ${plural(n, 'wiadomość', 'wiadomości', 'wiadomości')}`);
    const disc = status.discovered_devices || [];
    const discText = !saved.ha_discovery ? 'wyłączone' : disc.length ? disc.join(', ') : (status.connected ? 'oczekiwanie na odczyty' : '-');
    setText(dd.discovered, discText);
    let errText = status.last_error || '';
    if (!errText && waitingLong) errText = `broker nie odpowiada - sprawdź host, port i czy broker działa (${saved.host}:${saved.port})`;
    setText(dd.error, errText || 'brak');
    dd.error.className = errText ? 'err-text' : '';
    setText(dd.updated, lastPoll ? fmtTime(lastPoll / 1000) : '-');
  }

  // ── przykłady topiców ──
  function currentDevice() {
    if (!devices || !devices.length) return null;
    return devices.find((d) => d.id === sampleDevId) || devices[0];
  }

  function pickSample(values) {
    const keys = Object.keys(values).filter((k) => typeof values[k] === 'number');
    const chosen = PREFERRED_SAMPLE_KEYS.filter((k) => keys.includes(k)).slice(0, 4);
    for (const k of keys) { if (chosen.length >= 4) break; if (!chosen.includes(k)) chosen.push(k); }
    return chosen;
  }

  async function loadSample(dev) {
    if (!dev || samples.has(dev.id)) return;
    samples.set(dev.id, { loading: true });
    try {
      const data = await get(`/api/devices/${enc(dev.id)}/values`, { signal: life.signal });
      if (!life.alive()) return;
      samples.set(dev.id, { values: data.values || {} });
    } catch (e) {
      if (e.name === 'AbortError' || !life.alive()) return;
      samples.set(dev.id, { error: e.message || String(e) });
    }
    renderTopics();
  }

  function renderTopics() {
    const pre = cleanTopic(prefix.value) || 'modbus-dash';
    const haP = cleanTopic(haPrefix.value) || 'homeassistant';
    const dev = currentDevice();
    const devKey = dev ? dev.id : '<urządzenie>';
    const sample = dev ? samples.get(dev.id) : null;
    const keys = sample && sample.values ? pickSample(sample.values) : [];
    const firstKey = keys[0] || 'voltage_l1';

    const rows = [
      [`${pre}/status`, 'online / offline - stan Modbus Dash (retained, ostatnia wola)'],
      [`${pre}/${devKey}/state`, 'JSON ze wszystkimi wartościami licznika i czasem odczytu ts'],
      [`${pre}/${devKey}/availability`, 'online / offline - czy licznik odpowiada'],
    ];
    if (ha.input.checked) {
      rows.push([`${haP}/sensor/${safeKey(pre)}/${dev ? safeKey(dev.id) : devKey}_${safeKey(firstKey)}/config`,
        'konfiguracja czujnika dla Home Assistant (discovery, retained) - jedna na każdą wartość']);
    }
    topicList.replaceChildren(...rows.map(([t, d]) => h('li', null, h('code', null, t), h('span', null, d))));

    if (!devices) {
      payloadPre.textContent = '...';
      payloadNote.textContent = devError ? `Nie udało się pobrać listy urządzeń: ${devError}` : 'Wczytywanie urządzeń...';
    } else if (!dev) {
      payloadPre.textContent = '{"voltage_l1": 230.1, "current_l1": 1.52, "power_total": 349.0, "ts": 1700000000.0}';
      payloadNote.textContent = 'Brak urządzeń - to tylko przykład. Dodaj licznik w zakładce Urządzenia.';
    } else if (!sample || sample.loading) {
      payloadPre.textContent = '...';
      payloadNote.textContent = 'Wczytywanie przykładowych wartości...';
    } else if (sample.error) {
      payloadPre.textContent = '{"...": 0, "ts": 0}';
      payloadNote.textContent = `Nie udało się pobrać wartości urządzenia: ${sample.error}`;
    } else {
      const parts = keys.map((k) => `${JSON.stringify(k)}: ${JSON.stringify(sample.values[k])}`);
      if (keys.length) parts.push('...');
      parts.push(`"ts": ${(Date.now() / 1000).toFixed(3)}`);
      payloadPre.textContent = `{${parts.join(', ')}}`;
      payloadNote.textContent = keys.length
        ? `Bieżące wartości urządzenia "${dev.name || dev.id}" (skrócone).`
        : `Urządzenie "${dev.name || dev.id}" nie ma jeszcze odczytów.`;
    }

    const hostV = host.value.trim() || 'localhost';
    const portV = parseIntStrict(port.value) || 1883;
    const parts = ['mosquitto_sub', '-h', shq(hostV), '-p', String(portV)];
    if (user.value.trim()) parts.push('-u', shq(user.value.trim()), '-P', "'HASŁO'");
    if (tls.input.checked) parts.push('--capath', '/etc/ssl/certs');
    parts.push('-t', shq(`${pre}/#`), '-v');
    subPre.textContent = parts.join(' ');
  }

  function setDevices(list, err) {
    devices = list || [];
    devError = err ? (err.message || String(err)) : null;
    if (devices.length > 1) {
      const sel = select(devices.map((d) => [d.id, d.name ? `${d.name} (${d.id})` : d.id]), devices[0].id, {
        'aria-label': 'Urządzenie w przykładach',
        onchange: () => { sampleDevId = sel.value; loadSample(currentDevice()); renderTopics(); },
      });
      fill(devSelWrap, sel);
    }
    loadSample(currentDevice());
    renderTopics();
  }

  renderTopics();

  return {
    el,
    setDevices,
    start: () => poller.start(),
    stop: () => poller.stop(),
  };
}

// ── historia ─────────────────────────────────────────────────

function historyCard(ctx, life, onChange) {
  let saved = null;
  let available = null;
  let active = null;
  let minInterval = 1;

  const en = checkRow('Zapisuj historię w bazie SQLite', 'plik history.sqlite w katalogu danych (--data-dir)');
  const retention = h('input', { type: 'number', min: '1', max: '3650', step: '1', inputmode: 'numeric' });
  const bucket = select(BUCKETS, 60);
  const memory = h('input', { type: 'number', min: '60', max: '86400', step: '1', inputmode: 'numeric' });
  const bucketHint = h('span', { class: 'hint' });
  const memoryHint = h('span', { class: 'hint' });
  const bucketField = field('Agregacja w bazie', bucket);
  bucketField.append(bucketHint);
  const memoryField = field('Bufor w pamięci [punkty]', memory);
  memoryField.append(memoryHint);

  const headBadge = h('span', { class: 'badge badge-muted' }, 'Wczytywanie...');
  const stateBox = h('div');
  const errBox = h('div', { class: 'notice notice-err', role: 'alert', hidden: true });
  const dirtyBadge = h('span', { class: 'badge badge-warn', hidden: true }, 'Niezapisane zmiany');
  const saveBtn = h('button', { type: 'submit', class: 'btn btn-primary' }, 'Zapisz');
  const resetBtn = h('button', { type: 'button', class: 'btn btn-ghost', onclick: () => { if (saved) { fillForm(saved); clearErrors(); } } }, 'Cofnij zmiany');
  const loadBox = h('div', { class: 'status-line', 'aria-live': 'polite' }, h('span', { class: 'spinner' }), 'Wczytywanie ustawień historii...');

  const fs = h('fieldset', { disabled: true },
    h('legend', { class: 'sr-only' }, 'Ustawienia historii'),
    h('div', { class: 'form-grid' },
      h('div', { class: 'span-all' }, en.el),
      field('Retencja [dni]', retention, '1-3650; starsze dane są usuwane automatycznie'),
      bucketField,
      h('div', { class: 'span-all' }, memoryField)),
    errBox,
    h('div', { class: 'form-actions' }, dirtyBadge, resetBtn, saveBtn));

  const form = h('form', {
    novalidate: true, 'aria-label': 'Ustawienia historii',
    onsubmit: (e) => { e.preventDefault(); save(); },
    oninput: onEdit, onchange: onEdit,
  }, loadBox, stateBox, fs);

  const el = h('section', { class: 'card', 'aria-labelledby': 'int-hist-t' },
    h('div', { class: 'card-header' }, h('h3', { class: 'card-title', id: 'int-hist-t' }, 'Historia'), headBadge),
    h('p', { class: 'small muted int-lead' },
      'Ostatnie odczyty są trzymane w pamięci w pełnej rozdzielczości. Równolegle średnie, minima i maksima z każdego okresu agregacji trafiają do bazy SQLite - z nich korzystają wykresy dłuższych okresów i eksport CSV.'),
    form);

  function updateHints() {
    const b = Number(bucket.value) || 60;
    bucketHint.textContent = `${Math.round(86400 / b).toLocaleString('pl-PL')} punktów na wartość na dobę`;
    const pts = parseIntStrict(memory.value);
    memoryHint.textContent = pts
      ? `60-86400. Przy odczycie co ${minInterval.toLocaleString('pl-PL')} s to ok. ${fmtDuration(pts * minInterval)} wstecz. Zmiana czyści bieżący bufor wykresów.`
      : '60-86400 punktów na urządzenie';
  }

  function fillForm(s) {
    en.input.checked = !!s.enabled;
    retention.value = s.retention_days ?? '';
    if (![...bucket.options].some((o) => o.value === String(s.bucket_seconds))) {
      bucket.append(h('option', { value: String(s.bucket_seconds) }, `${s.bucket_seconds} s`));
    }
    bucket.value = String(s.bucket_seconds);
    memory.value = s.memory_points ?? '';
    updateHints();
    updateDirty();
  }

  function isDirty() {
    if (!saved) return false;
    return en.input.checked !== !!saved.enabled
      || retention.value.trim() !== String(saved.retention_days)
      || bucket.value !== String(saved.bucket_seconds)
      || memory.value.trim() !== String(saved.memory_points);
  }
  const updateDirty = () => { dirtyBadge.hidden = !isDirty(); };

  function onEdit(e) {
    const t = e.target;
    if (t && t.getAttribute && t.getAttribute('aria-invalid') === 'true') fieldError(t, null);
    updateHints();
    updateDirty();
  }

  function clearErrors() {
    clearFieldErrors(form);
    errBox.hidden = true;
    errBox.replaceChildren();
  }

  function renderState() {
    if (!saved) return;
    let badge, note;
    if (!available) {
      badge = ['Wyłączony (--no-history)', 'badge-muted'];
      note = ['notice-info', 'Aplikację uruchomiono z flagą --no-history - zapis do bazy jest niedostępny, wykresy pokazują tylko odczyty z bufora w pamięci.'];
    } else if (saved.enabled && active) {
      badge = ['Zapis aktywny', 'badge-ok'];
    } else if (saved.enabled) {
      badge = ['Uruchamianie', 'badge-warn'];
    } else {
      badge = ['Wyłączony', 'badge-muted'];
      note = ['notice-info', 'Zapis do bazy jest wyłączony - wykresy pokazują tylko odczyty z bufora w pamięci. Zapisane wcześniej dane pozostają w pliku history.sqlite w katalogu danych.'];
    }
    headBadge.textContent = badge[0];
    headBadge.className = 'badge ' + badge[1];
    fill(stateBox, note ? h('div', { class: 'notice ' + note[0] }, note[1]) : null);
  }

  function apply(data) {
    saved = data.settings;
    available = !!data.available;
    active = !!data.active;
    // wspólna flaga (Dashboard: zakresy dłuższe niż 1 h, karta Informacje)
    if (ctx.info) {
      ctx.info.features = ctx.info.features || {};
      ctx.info.features.history = active;
    }
    onChange();
    fillForm(saved);
    renderState();
  }

  async function load() {
    fill(loadBox, h('span', { class: 'spinner' }), 'Wczytywanie ustawień historii...');
    loadBox.hidden = false;
    try {
      const data = await get('/api/settings/history', { signal: life.signal });
      if (!life.alive()) return;
      apply(data);
      loadBox.hidden = true;
      fs.disabled = false;
    } catch (e) {
      if (e.name === 'AbortError' || !life.alive()) return;
      fill(loadBox, h('div', { class: 'notice notice-err' },
        h('strong', null, 'Nie udało się wczytać ustawień historii: '), e.message, ' ',
        h('button', { type: 'button', class: 'btn btn-ghost btn-sm', onclick: load }, 'Spróbuj ponownie')));
    }
  }

  const SERVER_FIELDS = [[/retencj/i, retention], [/agregacj/i, bucket], [/bufor/i, memory]];

  async function save() {
    if (!saved) return;
    clearErrors();
    const errs = [];
    const ret = parseIntStrict(retention.value);
    if (ret == null || ret < 1 || ret > 3650) errs.push([retention, 'Retencja: liczba całkowita 1-3650 dni']);
    const mem = parseIntStrict(memory.value);
    if (mem == null || mem < 60 || mem > 86400) errs.push([memory, 'Bufor: liczba całkowita 60-86400 punktów']);
    const b = parseIntStrict(bucket.value);
    if (!BUCKETS.some(([v]) => v === b)) errs.push([bucket, 'Wybierz okres agregacji z listy']);
    if (errs.length) {
      errs.forEach(([c, m]) => fieldError(c, m));
      errs[0][0].focus();
      return;
    }
    await busy(saveBtn, async () => {
      try {
        const res = await put('/api/settings/history', {
          enabled: en.input.checked, retention_days: ret, bucket_seconds: b, memory_points: mem,
        }, { signal: life.signal });
        if (!life.alive()) return;
        apply(res);
        toast('Zapisano ustawienia historii', 'ok');
      } catch (e) {
        if (e.name === 'AbortError' || !life.alive()) return;
        const msg = e.message || String(e);
        errBox.replaceChildren(h('strong', null, 'Nie zapisano: '), msg);
        errBox.hidden = false;
        if (e.status === 400) SERVER_FIELDS.forEach(([re, c]) => { if (re.test(msg)) fieldError(c, msg); });
        else showError(e, 'Historia: ');
      }
    });
  }

  function setDevices(list) {
    const ints = (list || []).filter((d) => d.enabled && typeof d.interval === 'number' && d.interval > 0).map((d) => d.interval);
    minInterval = ints.length ? Math.min(...ints) : 1;
    updateHints();
  }

  updateHints();
  return { el, load, setDevices };
}

// ── Prometheus ───────────────────────────────────────────────

function prometheusCard(ctx, life) {
  const features = (ctx.info && ctx.info.features) || {};
  const url = location.origin + '/metrics';
  const isLocal = LOCAL_HOSTS.has(location.hostname);
  const target = location.host || 'localhost:5000';
  const lines = [
    'scrape_configs:',
    '  - job_name: modbus-dash',
    '    scrape_interval: 15s',
    '    metrics_path: /metrics',
  ];
  if (location.protocol === 'https:') lines.push('    scheme: https');
  if (features.auth) lines.push('    basic_auth:', '      username: USER', '      password: HASŁO');
  lines.push('    static_configs:', `      - targets: ['${isLocal ? target.replace(location.hostname, 'IP-RASPBERRY-PI') : target}']`);
  const scrape = lines.join('\n');

  const urlCode = h('code', { class: 'int-url' }, url);
  const scrapePre = h('pre', { class: 'code' }, scrape);
  const previewBox = h('div', { class: 'int-preview', 'aria-live': 'polite' });
  const previewBtn = h('button', { type: 'button', class: 'btn btn-ghost', onclick: preview }, 'Podgląd');
  const hideBtn = h('button', { type: 'button', class: 'btn btn-ghost', hidden: true, onclick: () => { previewBox.replaceChildren(); hideBtn.hidden = true; previewBtn.textContent = 'Podgląd'; } }, 'Ukryj');

  const metricsTable = h('div', { class: 'table-wrap' }, h('table', { class: 'tbl' },
    h('thead', null, h('tr', null, h('th', { scope: 'col' }, 'Metryka'), h('th', { scope: 'col' }, 'Znaczenie'))),
    h('tbody', null, [
      ['modbus_dash_value', 'wartość z licznika; etykiety device, device_name, key, label, unit, group'],
      ['modbus_dash_up', '1 gdy ostatni odczyt się udał, inaczej 0'],
      ['modbus_dash_polls_total', 'liczba odczytów (licznik)'],
      ['modbus_dash_poll_failures_total', 'liczba nieudanych odczytów'],
      ['modbus_dash_poll_duration_seconds', 'czas trwania ostatniego odczytu'],
      ['modbus_dash_last_poll_timestamp_seconds', 'czas ostatniej próby odczytu (unix)'],
      ['modbus_dash_last_success_timestamp_seconds', 'czas ostatniego udanego odczytu (unix)'],
      ['modbus_dash_info', 'wersja Modbus Dash w etykiecie version (wartość zawsze 1)'],
    ].map(([m, d]) => h('tr', null, h('td', null, h('code', null, m)), h('td', null, d))))));

  const el = h('section', { class: 'card', 'aria-labelledby': 'int-prom-t' },
    h('div', { class: 'card-header' }, h('h3', { class: 'card-title', id: 'int-prom-t' }, 'Prometheus')),
    h('p', { class: 'small muted int-lead' }, 'Metryki w formacie tekstowym Prometheusa - do zbierania w Prometheusie lub VictoriaMetrics i wykresów w Grafanie.'),
    h('div', { class: 'label' }, 'Adres metryk'),
    h('div', { class: 'int-copy' }, urlCode, life.copyBtn(() => url, () => urlCode, 'Kopiuj adres metryk')),
    isLocal ? h('p', { class: 'small muted int-gap' }, 'Otwierasz panel lokalnie - jeśli Prometheus działa na innym komputerze, użyj adresu IP Raspberry Pi zamiast localhost.') : null,
    h('div', { class: 'label int-gap' }, 'Fragment prometheus.yml'),
    h('div', { class: 'int-copy' }, scrapePre, life.copyBtn(() => scrape, () => scrapePre, 'Kopiuj konfigurację Prometheusa')),
    features.auth ? h('p', { class: 'small muted' }, 'Panel wymaga logowania (--auth) - wpisz w basic_auth te same dane.') : null,
    h('div', { class: 'int-actions' }, previewBtn, hideBtn,
      h('a', { class: 'btn btn-ghost', href: '/metrics', target: '_blank', rel: 'noopener' }, 'Otwórz /metrics')),
    previewBox,
    h('details', { class: 'int-details' },
      h('summary', null, 'Dostępne metryki i przykładowe zapytania'),
      metricsTable,
      h('p', { class: 'small muted int-gap' }, 'Przykłady PromQL:'),
      h('pre', { class: 'code' }, 'modbus_dash_value{key="power_total"}\nincrease(modbus_dash_value{key="energy_import"}[1d])\nmodbus_dash_up == 0')));

  async function preview() {
    await busy(previewBtn, async () => {
      let text;
      try {
        let r;
        try {
          r = await fetch('/metrics', { signal: life.signal, headers: { Accept: 'text/plain' } });
        } catch (e) {
          if (e.name === 'AbortError') throw e;
          throw new Error('Brak połączenia z serwerem Modbus Dash');
        }
        if (!r.ok) throw new Error(`Błąd HTTP ${r.status}`);
        text = await r.text();
      } catch (e) {
        if (e.name === 'AbortError' || !life.alive()) return;
        fill(previewBox, h('div', { class: 'notice notice-err' }, h('strong', null, 'Nie udało się pobrać metryk: '), e.message));
        return;
      }
      if (!life.alive()) return;
      const all = text.replace(/\n$/, '').split('\n');
      const values = all.filter((l) => l.startsWith('modbus_dash_value{')).length;
      const shown = all.slice(0, PREVIEW_LINES);
      fill(previewBox,
        h('div', { class: 'status-line int-gap' },
          `Pokazano ${shown.length} z ${all.length} linii`, ' · ', `${values} ${plural(values, 'wartość', 'wartości', 'wartości')}`, ' · ', `pobrano ${fmtTime(Date.now() / 1000)}`),
        h('pre', { class: 'code int-metrics', tabindex: '0', 'aria-label': 'Podgląd metryk' }, shown.join('\n') + (all.length > shown.length ? '\n...' : '')));
      previewBtn.textContent = 'Odśwież podgląd';
      hideBtn.hidden = false;
    });
  }

  return { el };
}

// ── REST API ─────────────────────────────────────────────────

function restCard(ctx, life) {
  const features = (ctx.info && ctx.info.features) || {};
  const body = h('div', null, h('div', { class: 'status-line' }, h('span', { class: 'spinner' }), 'Wczytywanie urządzeń...'));
  const el = h('section', { class: 'card', 'aria-labelledby': 'int-rest-t' },
    h('div', { class: 'card-header' }, h('h3', { class: 'card-title', id: 'int-rest-t' }, 'REST API')),
    h('p', { class: 'small muted int-lead' },
      'Wszystkie odpowiedzi są w JSON (oprócz CSV i /metrics). Przydatne w skryptach, Node-RED albo do monitoringu dostępności (np. Uptime Kuma). Dodaj ',
      h('code', null, '| jq'), ', aby sformatować wynik.'),
    body);

  function render(devices, err) {
    const origin = location.origin;
    const dev = devices && devices.length ? devices[0] : null;
    const id = dev ? enc(dev.id) : '<id>';
    const auth = features.auth ? ' -u USER:HASŁO' : '';
    const curl = (method, path, extra = '') => `curl -s${method === 'GET' ? '' : ' -X ' + method}${auth}${extra} ${shq(origin + path)}`;
    const rows = [
      ['GET', '/api/devices', 'Lista urządzeń ze stanem ostatniego odczytu.'],
      ['GET', `/api/devices/${id}/values`, 'Bieżące wartości licznika (z pamięci; można pytać co sekundę).'],
      ['POST', `/api/devices/${id}/read`, 'Natychmiastowy odczyt z licznika i jego wynik.'],
      ['GET', `/api/devices/${id}/history?seconds=3600`, 'Historia z ostatniej godziny: klucze i punkty [czas, wartości...].'],
      ['GET', `/api/devices/${id}/history.csv?seconds=86400`, 'Eksport CSV z ostatniej doby (separator ;, przecinek dziesiętny).', ' -OJ'],
      ['GET', '/api/health', 'Stan wszystkich urządzeń: HTTP 200 gdy wszystko działa, 503 gdy któreś ma błąd.'],
      ['GET', '/api/info', 'Wersja, symulator i włączone funkcje.'],
      ['GET', '/api/presets', 'Lista presetów (wbudowanych i własnych).'],
      ['GET', '/metrics', 'Metryki Prometheus (tekst).'],
    ];
    const table = h('div', { class: 'table-wrap' }, h('table', { class: 'tbl int-rest' },
      h('thead', null, h('tr', null,
        h('th', { scope: 'col' }, 'Metoda'), h('th', { scope: 'col' }, 'Adres i opis'), h('th', { scope: 'col' }, 'Przykład (curl)'))),
      h('tbody', null, rows.map(([m, path, desc, extra]) => {
        const cmd = curl(m, path, extra);
        const code = h('code', { class: 'int-curl' }, cmd);
        return h('tr', null,
          h('td', null, h('span', { class: 'badge plain ' + (m === 'GET' ? 'badge-info' : 'badge-warn') }, m)),
          h('td', { class: 'int-rest-path' }, h('code', null, path), h('div', { class: 'small muted' }, desc)),
          h('td', { class: 'int-rest-cmd' }, h('div', { class: 'int-copy' }, code,
            life.copyBtn(() => cmd, () => code, `Kopiuj polecenie curl dla ${path}`))));
      }))));
    const notes = [];
    if (err) notes.push(h('div', { class: 'notice notice-warn' }, `Nie udało się pobrać listy urządzeń (${err.message || err}) - w przykładach zamiast identyfikatora jest <id>.`));
    else if (!dev) notes.push(h('div', { class: 'notice notice-info' }, 'Brak urządzeń - w przykładach zamiast identyfikatora jest <id>. Dodaj licznik w zakładce Urządzenia.'));
    else notes.push(h('p', { class: 'small muted int-gap' }, `Przykłady używają urządzenia "${dev.name || dev.id}" (id: ${dev.id}).`));
    if (features.auth) notes.push(h('p', { class: 'small muted' }, 'Panel wymaga logowania - zamień USER:HASŁO na dane z --auth.'));
    notes.push(h('p', { class: 'small muted' },
      'Zapytania zmieniające stan (POST, PUT, DELETE) z treścią muszą mieć nagłówek Content-Type: application/json. Pełna lista w README (sekcja API REST).'));
    fill(body, notes[0], table, notes.slice(1));
  }

  return { el, setDevices: render };
}

// ── informacje ───────────────────────────────────────────────

function infoCard(ctx, life) {
  const body = h('div');
  const el = h('section', { class: 'card', 'aria-labelledby': 'int-info-t' },
    h('div', { class: 'card-header' }, h('h3', { class: 'card-title', id: 'int-info-t' }, 'Informacje')),
    body);

  const feat = (on, label, onText, offText) => h('span', { class: 'badge ' + (on ? 'badge-ok' : 'badge-muted') }, `${label}: ${on ? onText : offText}`);

  function render(info) {
    if (!info || !info.version) {
      const retry = h('button', {
        type: 'button', class: 'btn btn-ghost btn-sm',
        onclick: () => busy(retry, async () => {
          try {
            const data = await get('/api/info', { signal: life.signal });
            if (!life.alive()) return;
            ctx.info = data;
            render(data);
          } catch (e) {
            if (e.name === 'AbortError' || !life.alive()) return;
            showError(e, 'Informacje: ');
          }
        }),
      }, 'Spróbuj ponownie');
      fill(body, h('div', { class: 'notice notice-err' }, 'Nie udało się pobrać informacji o serwerze. ', retry));
      return;
    }
    const f = info.features || {};
    const sim = info.simulator;
    const simText = sim
      ? `port ${sim.port} (${sim.framing === 'rtu' ? 'RTU over TCP' : 'Modbus TCP'}), ${(sim.devices || []).map((d) => `${d.preset} @ Unit ${d.unit}`).join(', ') || 'brak urządzeń'}`
      : 'wyłączony';
    fill(body,
      h('dl', { class: 'kv' },
        h('dt', null, 'Modbus Dash'), h('dd', null, `v${info.version}`),
        h('dt', null, 'pymodbus'), h('dd', null, info.pymodbus || '-'),
        h('dt', null, 'Python'), h('dd', null, info.python || '-'),
        h('dt', null, 'Symulator'), h('dd', null, simText),
        h('dt', null, 'Połączenie default'), h('dd', null, (info.default_bus && info.default_bus.describe) || '-'),
        h('dt', null, 'Adres panelu'), h('dd', null, location.origin),
        h('dt', null, 'Funkcje'), h('dd', null, h('div', { class: 'int-feat' },
          feat(f.mqtt, 'MQTT (paho-mqtt)', 'dostępne', 'brak biblioteki'),
          feat(f.history, 'Historia SQLite', 'aktywna', 'wyłączona'),
          feat(f.write, 'Zapis rejestrów', 'włączony (--allow-write)', 'wyłączony'),
          feat(f.auth, 'Logowanie', 'włączone (--auth)', 'wyłączone')))));
  }

  render(ctx.info);
  return { el, render };
}
