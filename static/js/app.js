// Modbus Dash - start aplikacji, routing (#widok?parametry), pasek boczny.

import { $, $$, h, mount, get, Poller, toast, showError } from './core.js';

const VIEWS = ['dashboard', 'devices', 'scanner', 'presets', 'connections', 'integrations', 'help'];
const TITLES = {
  dashboard: 'Dashboard', devices: 'Urządzenia', scanner: 'Skaner rejestrów', presets: 'Presety',
  connections: 'Połączenia', integrations: 'Integracje', help: 'Pomoc',
};

/**
 * Kontekst przekazywany do widoków:
 *  ctx.info            - /api/info (wersje, symulator, features)
 *  ctx.params          - parametry z adresu (#devices?new=1&preset=x -> {new: '1', preset: 'x'})
 *  ctx.navigate(view, params, handoff) - przejście do widoku; handoff = dane w pamięci (np. szkic presetu)
 *  ctx.handoff         - dane przekazane przez poprzedni widok (jednorazowo)
 *  ctx.refreshHealth() - odświeża status w pasku bocznym
 * Widok: export function mount(root, ctx) -> {unmount()} (lub Promise tego)
 */
const ctx = {
  info: null,
  params: {},
  handoff: null,
  navigate(view, params = {}, handoff = null) {
    pendingHandoff = handoff;
    const q = new URLSearchParams(Object.entries(params).filter(([, v]) => v != null && v !== '')).toString();
    const hash = '#' + view + (q ? '?' + q : '');
    if (location.hash === hash) route(); else location.hash = hash;
  },
  refreshHealth: () => health.trigger(),
};

let pendingHandoff = null;
let current = null;      // {name, instance}
let routeSeq = 0;

function parseHash() {
  const raw = location.hash.replace(/^#\/?/, '');
  const [name, q] = raw.split('?');
  return { name: VIEWS.includes(name) ? name : 'dashboard', params: Object.fromEntries(new URLSearchParams(q || '')) };
}

async function route() {
  const seq = ++routeSeq;
  const { name, params } = parseHash();
  if (current && current.instance && current.instance.unmount) {
    try { current.instance.unmount(); } catch (e) { console.error(e); }
  }
  current = null;
  $$('.nav-item').forEach((a) => {
    const on = a.dataset.view === name;
    a.classList.toggle('active', on);
    if (on) a.setAttribute('aria-current', 'page'); else a.removeAttribute('aria-current');
  });
  document.title = `${TITLES[name]} - Modbus Dash`;
  closeNav();
  const root = $('#main');
  mount(root, h('div', { class: 'view-loading' }, h('span', { class: 'spinner lg' })));
  ctx.params = params;
  ctx.handoff = pendingHandoff;
  pendingHandoff = null;
  try {
    const mod = await import(`./views/${name}.js`);
    if (seq !== routeSeq) return;
    const view = h('div', { class: 'view', 'data-view': name });
    mount(root, view);
    const instance = await mod.mount(view, ctx);
    if (seq !== routeSeq) { instance && instance.unmount && instance.unmount(); return; }
    current = { name, instance };
  } catch (e) {
    console.error(e);
    if (seq !== routeSeq) return;
    mount(root, h('div', { class: 'page-content' },
      h('div', { class: 'notice notice-err' }, `Nie udało się wczytać widoku "${TITLES[name]}": ${e.message || e}`)));
  }
}

// ── nawigacja mobilna ────────────────────────────────────────

function openNav() {
  document.body.classList.add('nav-open');
  $('#nav-backdrop').hidden = false;
  $('#nav-toggle').setAttribute('aria-expanded', 'true');
}
function closeNav() {
  document.body.classList.remove('nav-open');
  $('#nav-backdrop').hidden = true;
  $('#nav-toggle').setAttribute('aria-expanded', 'false');
}

// ── status w pasku bocznym ───────────────────────────────────

const health = new Poller(async () => {
  let devices;
  try {
    devices = await get('/api/devices');
  } catch (e) {
    setHealth('off', 'Brak połączenia z serwerem');
    return;
  }
  const active = devices.filter((d) => d.enabled);
  const ok = active.filter((d) => d.status && d.status.state === 'ok').length;
  if (!active.length) setHealth('', 'Brak urządzeń');
  else if (ok === active.length) setHealth('on', `${ok}/${active.length} urządzeń OK`);
  else if (ok) setHealth('warn', `${ok}/${active.length} urządzeń OK`);
  else setHealth('off', `0/${active.length} urządzeń OK`);
}, 5000);

function setHealth(cls, text) {
  $('#health-dot').className = 'conn-dot ' + cls;
  $('#health-text').textContent = text;
  const top = $('#topbar-status');
  top.replaceChildren(h('span', { class: 'conn-dot ' + cls }), text);
}

// ── start ────────────────────────────────────────────────────

async function init() {
  $('#nav-toggle').addEventListener('click', () => (document.body.classList.contains('nav-open') ? closeNav() : openNav()));
  $('#nav-backdrop').addEventListener('click', closeNav);
  document.addEventListener('keydown', (e) => { if (e.key === 'Escape') closeNav(); });
  try {
    ctx.info = await get('/api/info');
    $('#app-version').textContent = `v${ctx.info.version} · pymodbus ${ctx.info.pymodbus}`;
    $('#sim-badge').hidden = !ctx.info.simulator;
    if (ctx.info.simulator) $('#sim-badge').textContent = `Symulator :${ctx.info.simulator.port}`;
  } catch (e) {
    ctx.info = { features: {}, simulator: null };
    showError(e, 'Serwer: ');
  }
  health.start();
  window.addEventListener('hashchange', route);
  route();
}

window.addEventListener('unhandledrejection', (e) => {
  if (e.reason && e.reason.name === 'AbortError') return;
  console.error(e.reason);
  toast(`Nieoczekiwany błąd: ${e.reason && e.reason.message ? e.reason.message : e.reason}`, 'err');
});

init();
