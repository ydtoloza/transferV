'use strict';

const state = {
  tab: 'torrents',
  settings: null,
  settingsDirty: false,
  torrents: [],
  torrentsSig: '',
  transfers: [],
  status: null,
  filter: 'all',
  tracker: '',
  search: '',
  selected: new Set(),
  historyFilter: 'all',
  historyLimit: 50,
  openLogs: new Set(),
};

// ── Utils ──────────────────────────────────────────────────
const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));
const esc = (v) => String(v ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

function bytes(v) {
  if (!v) return '0 B';
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  let s = v, u = 0;
  while (s >= 1024 && u < units.length - 1) { s /= 1024; u++; }
  return `${s.toFixed(s >= 10 || u === 0 ? 0 : 1)} ${units[u]}`;
}

function duration(sec) {
  sec = Math.max(0, Math.round(sec));
  if (sec < 60) return `${sec}s`;
  const m = Math.floor(sec / 60);
  if (m < 60) return `${m}m ${sec % 60}s`;
  const h = Math.floor(m / 60);
  return h < 24 ? `${h}h ${m % 60}m` : `${Math.floor(h / 24)}d ${h % 24}h`;
}

const since = (iso) => (iso ? (Date.now() - new Date(iso).getTime()) / 1000 : 0);

function relTime(iso) {
  if (!iso) return '';
  const m = Math.floor(since(iso) / 60);
  if (m < 1) return 'ahora';
  if (m < 60) return `hace ${m} min`;
  const h = Math.floor(m / 60);
  return h < 24 ? `hace ${h} h` : `hace ${Math.floor(h / 24)} d`;
}

const pctOf = (msg) => { const m = msg && msg.match(/(\d+)%/); return m ? Number(m[1]) : 0; };

function speedOf(msg) {
  const m = msg && msg.match(/([\d.]+)\s*([KMGT])?B\/s/);
  if (!m) return 0;
  return parseFloat(m[1]) * ({ K: 1024, M: 1024 ** 2, G: 1024 ** 3, T: 1024 ** 4 }[m[2]] || 1);
}

const timers = {};
function debounce(key, fn, ms) { clearTimeout(timers[key]); timers[key] = setTimeout(fn, ms); }

function toast(title, sub = '', type = 'info') {
  const el = document.createElement('div');
  el.className = `toast ${type}`;
  el.innerHTML = `<strong>${esc(title)}</strong>${sub ? `<span>${esc(sub)}</span>` : ''}`;
  $('#toastContainer').append(el);
  setTimeout(() => { el.classList.add('leaving'); setTimeout(() => el.remove(), 250); }, 4000);
}

async function api(path, options = {}) {
  const res = await fetch(path, { headers: { 'Content-Type': 'application/json' }, ...options });
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(typeof body.detail === 'string' ? body.detail : res.statusText);
  }
  return res.json();
}

const ICON = {
  check: '<svg aria-hidden="true" width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round"><path d="M5 12l5 5L20 7"/></svg>',
  cross: '<svg aria-hidden="true" width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.6" stroke-linecap="round"><path d="M6 6l12 12M18 6L6 18"/></svg>',
  alert: '<svg aria-hidden="true" width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round"><path d="M12 8v5M12 16.5v.5"/><circle cx="12" cy="12" r="9"/></svg>',
  arrow: '<svg aria-hidden="true" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M5 12h14"/><path d="M13 6l6 6-6 6"/></svg>',
  trash: '<svg aria-hidden="true" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M4 7h16"/><path d="M10 11v6M14 11v6"/><path d="M6 7l1 13h10l1-13"/><path d="M9 7V4h6v3"/></svg>',
  eye: '<svg aria-hidden="true" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/></svg>',
};

// ── Derived data ───────────────────────────────────────────
const ACTIVE = ['pending', 'waiting', 'transferring'];
const running = () => state.transfers.find((t) => t.status === 'transferring');
const waiting = () => state.transfers.filter((t) => t.status === 'pending' || t.status === 'waiting').sort((a, b) => a.id - b.id);
const activeByHash = () => new Map(state.transfers.filter((t) => ACTIVE.includes(t.status)).map((t) => [t.torrent_hash, t]));

const GROUPS = {
  seed: ['uploading', 'stalledUP', 'forcedUP', 'queuedUP', 'checkingUP'],
  down: ['downloading', 'stalledDL', 'forcedDL', 'queuedDL', 'metaDL', 'forcedMetaDL', 'checkingDL'],
  paused: ['pausedUP', 'pausedDL', 'stoppedUP', 'stoppedDL'],
  error: ['error', 'missingFiles'],
};
const LABELS = {
  seed: 'Seeding', down: 'Descargando', paused: 'Pausado', error: 'Error', other: 'Verificando',
  queuedDL: 'En cola', missingFiles: 'Archivos faltantes', moving: 'Moviendo',
};

function groupOf(t) {
  for (const [g, states] of Object.entries(GROUPS)) if (states.includes(t.state)) return g;
  return 'other';
}

// ── Navigation ─────────────────────────────────────────────
const TABS = ['torrents', 'queue', 'history', 'settings'];

function showTab(tab) {
  if (!TABS.includes(tab)) tab = 'torrents';
  state.tab = tab;
  $$('[data-panel]').forEach((p) => { p.hidden = p.dataset.panel !== tab; });
  $$('.tabs a').forEach((a) => {
    if (a.dataset.tab === tab) a.setAttribute('aria-current', 'page');
    else a.removeAttribute('aria-current');
  });
  renderLive();
  renderSelection();
  if (tab === 'torrents') loadTorrents().catch(() => {});
}

// ── Header ─────────────────────────────────────────────────
function renderHeader() {
  $('#countTorrents').textContent = state.torrents.length;
  const active = state.transfers.filter((t) => ACTIVE.includes(t.status)).length;
  const q = $('#countQueue');
  q.textContent = active;
  q.hidden = active === 0;
}

function setChip(id, ok, title) {
  const el = $(id);
  el.classList.toggle('ok', !!ok);
  el.classList.toggle('bad', !ok);
  el.title = title;
}

function renderStatus() {
  const st = state.status;
  if (!st) return;
  setChip('#chip-qbit', st.qbit, st.qbit ? 'qBittorrent conectado' : 'qBittorrent sin conexión');
  setChip('#chip-vps', st.vps?.ok, st.vps?.ok ? 'SSH origen conectado' : `SSH origen: ${st.vps?.error || 'sin conexión'}`);
  $('#chip-destination').hidden = st.destination === null;
  if (st.destination) setChip('#chip-destination', st.destination.ok, st.destination.ok ? 'SSH destino conectado' : `SSH destino: ${st.destination.error}`);
}

// ── Live strip ─────────────────────────────────────────────
function renderLive() {
  const t = running();
  const strip = $('#liveStrip');
  strip.hidden = !t || state.tab === 'queue' || state.tab === 'settings';
  strip.classList.toggle('narrow', state.tab === 'history');
  if (!t) return;
  const pct = pctOf(t.message);
  const speed = speedOf(t.message);
  $('#liveName').textContent = t.torrent_name;
  $('#liveDest').textContent = `→ ${t.destination_path}`;
  $('#liveMeta').textContent = [`${pct}%`, speed ? `${bytes(speed)}/s` : '', t.started_at ? duration(since(t.started_at)) : ''].filter(Boolean).join(' · ');
  $('#liveBar').style.width = `${pct}%`;
  const n = waiting().length;
  $('#liveQueueLink').textContent = n ? `Ver cola (${n} en espera)` : 'Ver cola';
}

// ── Torrents ───────────────────────────────────────────────
function filteredTorrents() {
  const q = state.search.toLowerCase();
  return state.torrents.filter((t) =>
    (!q || t.name.toLowerCase().includes(q)) &&
    (!state.tracker || t.tracker === state.tracker) &&
    (state.filter === 'all' || groupOf(t) === state.filter));
}

const isTransferable = (t, active) => t.transfer_status !== 'completed' && !active.has(t.hash);

function renderFilters() {
  const defs = [['all', 'Todos'], ['down', 'Descargando'], ['seed', 'Seeding'], ['paused', 'Pausados'], ['error', 'Error']];
  const counts = { all: state.torrents.length };
  state.torrents.forEach((t) => { const g = groupOf(t); counts[g] = (counts[g] || 0) + 1; });
  $('#stateFilters').innerHTML = defs.map(([id, label]) =>
    `<button type="button" data-filter="${id}" aria-pressed="${state.filter === id}">${label} <span>${counts[id] || 0}</span></button>`).join('');

  const trackers = [...new Set(state.torrents.map((t) => t.tracker).filter(Boolean))].sort();
  if (state.tracker && !trackers.includes(state.tracker)) state.tracker = '';
  $('#trackerFilter').innerHTML = `<option value="">Todos</option>` +
    trackers.map((tr) => `<option value="${esc(tr)}" ${tr === state.tracker ? 'selected' : ''}>${esc(tr)}</option>`).join('');
}

function destinationCell(t, active) {
  const tr = active.get(t.hash);
  if (tr?.status === 'transferring') return `<span class="dest-run">Transfiriendo ${pctOf(tr.message)}%</span>`;
  if (tr) return `<span class="dest-queued">En cola</span>`;
  if (t.transfer_status === 'completed') return `<span class="dest-ok">${ICON.check}Transferido</span>`;
  return `<button type="button" class="btn btn-sm" data-transfer="${esc(t.hash)}">${ICON.arrow}Transferir</button>`;
}

function renderTorrents() {
  const active = activeByHash();
  const list = filteredTorrents();
  const down = state.torrents.reduce((a, t) => a + (t.dlspeed || 0), 0);
  const up = state.torrents.reduce((a, t) => a + (t.upspeed || 0), 0);
  $('#totalDown').textContent = `↓ ${bytes(down)}/s`;
  $('#totalUp').textContent = `↑ ${bytes(up)}/s`;

  // Drop selections that are no longer transferable.
  for (const h of state.selected) {
    const t = state.torrents.find((x) => x.hash === h);
    if (!t || !isTransferable(t, active)) state.selected.delete(h);
  }

  $('#torrentRows').innerHTML = list.map((t) => {
    const g = groupOf(t);
    const pct = Math.round((t.progress || 0) * 100);
    const fill = g === 'error' ? 'var(--bad)' : pct >= 100 ? 'var(--ok)' : 'var(--accent)';
    const sel = state.selected.has(t.hash);
    const canPick = isTransferable(t, active);
    return `<tr class="${sel ? 'selected' : ''}" title="${esc(t.content_path || t.save_path)}">
      <td>${canPick ? `<input type="checkbox" data-select="${esc(t.hash)}" ${sel ? 'checked' : ''} aria-label="Seleccionar ${esc(t.name)}" />` : ''}</td>
      <td><div class="name-cell"><strong>${esc(t.name)}</strong><span>${esc(t.tracker || '—')}</span></div></td>
      <td class="cell-mono">${bytes(t.size)}</td>
      <td><div class="progress-cell"><div class="mini-bar"><div style="width:${pct}%;background:${fill}"></div></div><span class="cell-mono">${pct}%</span></div></td>
      <td><span class="badge s-${g}"><span class="dot"></span>${esc(LABELS[t.state] || LABELS[g])}</span></td>
      <td>${destinationCell(t, active)}</td>
      <td class="num cell-mono ${t.dlspeed ? 'is-live' : ''}">${t.dlspeed ? `${bytes(t.dlspeed)}/s` : '—'}</td>
      <td class="num cell-mono ${t.upspeed ? 'is-live' : ''}">${t.upspeed ? `${bytes(t.upspeed)}/s` : '—'}</td>
      <td class="num cell-mono">${t.eta > 0 && t.eta < 8640000 && pct < 100 ? duration(t.eta) : '—'}</td>
    </tr>`;
  }).join('');
  $('#torrentEmpty').hidden = list.length > 0;

  const pickable = list.filter((t) => isTransferable(t, active));
  const all = $('#selectAll');
  all.disabled = pickable.length === 0;
  all.checked = pickable.length > 0 && pickable.every((t) => state.selected.has(t.hash));
  renderSelection();
}

function renderSelection() {
  const n = state.selected.size;
  $('#selCount').textContent = n;
  $('#selectionBar').hidden = n === 0 || state.tab !== 'torrents';
}

const transferBody = (t) => JSON.stringify({ torrent_hash: t.hash, torrent_name: t.name, source_path: t.content_path, size: t.size });

async function queueTorrents(hashes) {
  const torrents = hashes.map((h) => state.torrents.find((t) => t.hash === h)).filter(Boolean);
  if (!torrents.length) return;
  const results = await Promise.allSettled(torrents.map((t) => api('/api/transfers', { method: 'POST', body: transferBody(t) })));
  const ok = results.filter((r) => r.status === 'fulfilled').length;
  const failed = results.find((r) => r.status === 'rejected');
  hashes.forEach((h) => state.selected.delete(h));
  if (torrents.length === 1) {
    ok ? toast('Agregado a la cola', torrents[0].name, 'ok') : toast('No se pudo agregar', failed.reason.message, 'error');
  } else {
    toast('Agregados a la cola', `${ok} de ${torrents.length}`, ok ? 'ok' : 'error');
  }
  await Promise.all([loadTransfers(), loadTorrents(true)]);
}

// ── Queue ──────────────────────────────────────────────────
function renderQueue() {
  const t = running();
  const wait = waiting();
  const count = (s) => state.transfers.filter((x) => x.status === s).length;
  $('#statRunning').textContent = t ? 1 : 0;
  $('#statWaiting').textContent = wait.length;
  $('#statDone').textContent = count('completed');
  $('#statFailed').textContent = count('failed');

  $('#nowCard').hidden = !t;
  if (t) {
    const pct = pctOf(t.message);
    const speed = speedOf(t.message);
    const doneBytes = t.size * pct / 100;
    $('#nowName').textContent = t.torrent_name;
    $('#nowPath').textContent = `${t.source_path} → ${t.destination_path}`;
    $('#nowBar').style.width = `${pct}%`;
    $('#nowPct').textContent = `${pct}%`;
    $('#nowSpeed').textContent = speed ? `${bytes(speed)}/s` : 'iniciando…';
    $('#nowBytes').textContent = t.size ? `${bytes(doneBytes)} de ${bytes(t.size)}` : '';
    $('#nowEta').textContent = speed && t.size ? `queda ~${duration((t.size - doneBytes) / speed)}` : '';
    $('#nowElapsed').textContent = t.started_at ? `iniciada hace ${duration(since(t.started_at))}` : '';
  }

  const offset = t ? 2 : 1;
  $('#waitingList').innerHTML = wait.map((w, i) => {
    const isWaiting = w.status === 'waiting' && w.message;
    return `<li>
      <span class="pos">${i + offset}</span>
      <div class="info"><strong>${esc(w.torrent_name)}</strong><span class="${isWaiting ? 'is-waiting' : ''}">${esc(w.message || 'Pendiente')}</span></div>
      <span class="cell-mono">${bytes(w.size)}</span>
      <button type="button" class="icon-btn" data-delete="${w.id}" aria-label="Quitar ${esc(w.torrent_name)} de la cola">${ICON.trash}</button>
    </li>`;
  }).join('');
  $('#queueEmpty').hidden = !!t || wait.length > 0;
  $('#waitingTitle').hidden = wait.length === 0;
}

// ── History ────────────────────────────────────────────────
const HISTORY = {
  completed: { label: 'Completado', icon: ICON.check, cls: 'ok' },
  failed: { label: 'Fallido', icon: ICON.cross, cls: 'bad' },
  missing: { label: 'Borrado en destino', icon: ICON.alert, cls: 'gone' },
  cancelled: { label: 'Cancelado', icon: ICON.cross, cls: 'gone' },
};

function historyNote(t) {
  const msg = (t.message || '').trim();
  if (t.status === 'missing') return 'el archivo ya no existe en el destino';
  if (t.status === 'completed') {
    if (/encontrado/i.test(msg)) return msg.charAt(0).toLowerCase() + msg.slice(1);
    if (t.started_at && t.completed_at) return `transferido en ${duration((new Date(t.completed_at) - new Date(t.started_at)) / 1000)}`;
    return 'transferido';
  }
  const line = msg.split('\n').map((l) => l.trim()).filter(Boolean).pop() || '';
  return line.length > 110 ? `${line.slice(0, 110)}…` : line;
}

function renderHistory() {
  const done = state.transfers.filter((t) => HISTORY[t.status]);
  const defs = [['all', 'Todos'], ['completed', 'Completados'], ['failed', 'Fallidos'], ['missing', 'Borrados en destino']];
  $('#historyFilters').innerHTML = defs.map(([id, label]) => {
    const n = id === 'all' ? done.length : done.filter((t) => t.status === id).length;
    return `<button type="button" data-hfilter="${id}" aria-pressed="${state.historyFilter === id}">${label} <span>${n}</span></button>`;
  }).join('');

  const list = done.filter((t) => state.historyFilter === 'all' || t.status === state.historyFilter);
  const shown = list.slice(0, state.historyLimit);
  $('#historyList').innerHTML = shown.map((t) => {
    const k = HISTORY[t.status];
    const open = state.openLogs.has(t.id);
    return `<li>
      <div class="h-row">
        <span class="h-icon ${k.cls}">${k.icon}</span>
        <div class="h-main"><strong>${esc(t.torrent_name)}</strong><span>${k.label}${historyNote(t) ? ` · ${esc(historyNote(t))}` : ''}</span></div>
        <span class="cell-mono">${bytes(t.size)}</span>
        <span class="h-when">${relTime(t.completed_at || t.updated_at)}</span>
        <button type="button" class="btn btn-sm" data-log="${t.id}" aria-expanded="${open}">${open ? 'Ocultar' : 'Detalles'}</button>
        <button type="button" class="icon-btn" data-delete="${t.id}" aria-label="Eliminar ${esc(t.torrent_name)} del historial">${ICON.trash}</button>
      </div>
      ${open ? `<div class="h-detail"><p class="mono faint small wrap">${esc(t.source_path)} → ${esc(t.destination_path)}</p>${t.message ? `<pre>${esc(t.message)}</pre>` : ''}</div>` : ''}
    </li>`;
  }).join('');
  $('#historyEmpty').hidden = list.length > 0;
  $('#historyMore').hidden = list.length <= shown.length;

  const mins = state.settings?.verify_interval_minutes;
  $('#verifyInfo').textContent = mins
    ? `El destino se revisa cada ${mins} min con una sola conexión SSH.`
    : 'La verificación automática del destino está desactivada.';
}

function renderTransfers() {
  renderHeader();
  renderLive();
  renderQueue();
  renderHistory();
  if (state.torrents.length) renderTorrents();
}

// ── Settings ───────────────────────────────────────────────
const MODE_INFO = {
  local_pull: {
    hint: 'Solo hace falta SSH al origen. La llave vive en este servidor.',
    dest: 'Carpeta de destino en este servidor',
    keyWhere: { vps: 'en este servidor', destination: '' },
    flow: [['n', 'Origen', 'VPS · qBittorrent'], ['e', 'rsync'], ['n', 'Este servidor', 'TransferV + destino']],
  },
  orchestrated_pull: {
    hint: 'TransferV entra por SSH al destino y le ordena descargar. La llave del origen debe estar en el destino.',
    dest: 'Carpeta en el servidor destino',
    keyWhere: { vps: 'en el servidor destino', destination: 'en este servidor' },
    flow: [['n', 'Origen', 'VPS · qBittorrent'], ['e', 'rsync'], ['n', 'Destino', 'servidor media'], ['c', 'ssh'], ['n', 'Este servidor', 'TransferV']],
  },
  remote_push: {
    hint: 'TransferV entra por SSH al origen y le ordena enviar. La llave del destino debe estar en el origen.',
    dest: 'Carpeta en el servidor destino',
    keyWhere: { vps: 'en este servidor', destination: 'en el servidor origen' },
    flow: [['n', 'Este servidor', 'TransferV'], ['c', 'ssh'], ['n', 'Origen', 'VPS · qBittorrent'], ['e', 'rsync'], ['n', 'Destino', 'servidor media']],
  },
};

function sshCardHtml(key, title, role) {
  const p = `${key}_ssh`;
  return `
    <div class="card-head"><h2>${esc(title)}</h2><span class="faint small">${esc(role)}</span></div>
    <div class="grid grid-ssh">
      <div class="field"><label for="s-${key}-user">Usuario</label><input name="${p}.username" id="s-${key}-user" placeholder="root" /></div>
      <div class="field"><label for="s-${key}-host">Host / IP</label><input name="${p}.host" id="s-${key}-host" placeholder="203.0.113.10" /></div>
      <div class="field"><label for="s-${key}-port">Puerto</label><input class="mono" name="${p}.port" id="s-${key}-port" type="number" min="1" max="65535" placeholder="22" /></div>
    </div>
    <div class="grid grid-auth">
      <div class="field">
        <span class="label" id="s-${key}-auth">Autenticación</span>
        <div class="seg-auth" role="radiogroup" aria-labelledby="s-${key}-auth">
          <label><input type="radio" name="${p}.auth_method" value="key" /><span>Llave SSH</span></label>
          <label><input type="radio" name="${p}.auth_method" value="password" /><span>Contraseña</span></label>
        </div>
      </div>
      <div class="field" data-auth="${key}:key">
        <label for="s-${key}-key">Ruta de la llave <span class="faint" data-keywhere="${key}"></span></label>
        <input class="mono" name="${p}.key_path" id="s-${key}-key" placeholder="/keys/id_ed25519" />
      </div>
      <div class="field" data-auth="${key}:password">
        <label for="s-${key}-pw">Contraseña SSH</label>
        <input name="${p}.password" id="s-${key}-pw" type="password" autocomplete="new-password" data-pw />
      </div>
    </div>`;
}

function buildSettingsForm() {
  $$('[data-ssh]').forEach((s) => { s.innerHTML = sshCardHtml(s.dataset.ssh, s.dataset.title, s.dataset.role); });
  $$('input[data-pw]').forEach((input) => {
    const wrap = document.createElement('div');
    wrap.className = 'password-field';
    input.replaceWith(wrap);
    wrap.append(input);
    wrap.insertAdjacentHTML('beforeend', `<button type="button" class="pw-toggle" aria-label="Mostrar u ocultar contraseña">${ICON.eye}</button>`);
  });
}

const getPath = (src, path) => path.split('.').reduce((v, k) => v?.[k], src);
function setPath(obj, path, val) {
  const parts = path.split('.');
  let cur = obj;
  parts.slice(0, -1).forEach((k) => { cur[k] = cur[k] || {}; cur = cur[k]; });
  cur[parts.at(-1)] = val;
}
const formInputs = () => $$('#settingsForm [name]');

function fillForm(settings) {
  formInputs().forEach((input) => {
    const val = getPath(settings, input.name);
    if (input.type === 'checkbox') input.checked = !!val;
    else if (input.type === 'radio') input.checked = input.value === String(val);
    else input.value = val ?? '';
  });
  syncForm();
  setDirty(false);
}

function readForm() {
  const s = structuredClone(state.settings || {});
  formInputs().forEach((input) => {
    if (input.type === 'radio' && !input.checked) return;
    let val = input.type === 'checkbox' ? input.checked : input.value.trim();
    if (input.type === 'number') {
      // Empty/invalid keeps the previous value instead of saving 0.
      val = val === '' || Number.isNaN(Number(val)) ? getPath(state.settings, input.name) : Number(val);
    }
    setPath(s, input.name, val);
  });
  return s;
}

const currentMode = () => $('#settingsForm [name="transfer_mode"]:checked')?.value || 'local_pull';

// Hidden fields keep their values; readForm still serializes them.
function syncForm() {
  const mode = currentMode();
  const info = MODE_INFO[mode];
  $$('#settingsForm [data-modes]').forEach((el) => { el.hidden = !el.dataset.modes.split(' ').includes(mode); });
  $('#modeHint').textContent = info.hint;
  $('#destLabel').textContent = info.dest;
  $('#modeFlow').innerHTML = info.flow.map(([kind, a, b]) => kind === 'n'
    ? `<div class="flow-node"><strong>${a}</strong><span>${b}</span></div>`
    : `<div class="flow-edge ${kind === 'c' ? 'ctl' : ''}">${a}</div>`).join('');
  $$('[data-keywhere]').forEach((el) => {
    const where = info.keyWhere[el.dataset.keywhere];
    el.textContent = where ? `(${where})` : '';
  });
  $$('[data-auth]').forEach((el) => {
    const [key, method] = el.dataset.auth.split(':');
    const chosen = $(`#settingsForm [name="${key}_ssh.auth_method"]:checked`)?.value || 'key';
    el.hidden = chosen !== method;
  });
  const on = $('#s-wh-enabled').checked;
  $$('[data-webhook]').forEach((el) => { el.hidden = !on; });
  $('#webhookPill').textContent = on ? 'Activadas' : 'Desactivadas';
  $('#webhookPill').classList.toggle('on', on);
  const rs = ($('#s-rsync').value || '').split(' ').slice(0, 2).join(' ');
  $('#advSummary').textContent = `cola cada ${$('#s-interval').value || '—'} s · destino cada ${$('#s-verify').value || '—'} min · rsync ${rs}`;
}

function setDirty(dirty) {
  state.settingsDirty = dirty;
  $('#saveBtn').disabled = !dirty;
  const el = $('#settingsState');
  el.classList.toggle('dirty', dirty);
  el.lastElementChild.textContent = dirty ? 'Cambios sin guardar' : 'Todo guardado';
}

async function saveSettings() {
  const payload = readForm();
  try { JSON.parse(payload.webhook?.headers_json || '{}'); } catch {
    toast('Headers del webhook inválidos', 'Deben ser JSON válido', 'error');
    return false;
  }
  try {
    state.settings = await api('/api/settings', { method: 'PUT', body: JSON.stringify(payload) });
    fillForm(state.settings);
    renderHistory();
    toast('Ajustes guardados', '', 'ok');
    loadStatus(true);
    return true;
  } catch (err) {
    toast('Error al guardar', err.message, 'error');
    return false;
  }
}

async function testConnection() {
  const btn = $('#testBtn');
  if (btn.disabled) return;
  if (state.settingsDirty && !(await saveSettings())) return;
  btn.disabled = true;
  btn.textContent = 'Probando…';
  const st = await loadStatus(true);
  btn.disabled = false;
  btn.textContent = 'Probar conexión';
  const box = $('#connResults');
  box.hidden = false;
  if (!st) { box.innerHTML = `<div class="conn-row bad"><strong>${ICON.cross}Error</strong><span>No se pudo consultar el estado</span></div>`; return; }
  const rows = [
    ['qBittorrent', { ok: st.qbit, error: 'No se pudo iniciar sesión. Revisa URL, usuario y contraseña.' }],
    ['SSH origen', st.vps],
    ['SSH destino', st.destination],
  ].filter(([, r]) => r);
  box.innerHTML = rows.map(([name, r]) =>
    `<div class="conn-row ${r.ok ? 'ok' : 'bad'}"><strong>${r.ok ? ICON.check : ICON.cross}${name}</strong><span>${esc(r.ok ? 'Conectado' : r.error)}</span></div>`).join('');
}

// ── Data loading ───────────────────────────────────────────
async function loadSettings() {
  state.settings = await api('/api/settings');
  fillForm(state.settings);
}

async function loadStatus(force = false) {
  try {
    state.status = await api(`/api/status${force ? '?force=true' : ''}`);
    renderStatus();
    return state.status;
  } catch {
    return null;
  }
}

async function loadTorrents(force = false) {
  let torrents;
  try {
    torrents = await api('/api/torrents');
  } catch (err) {
    state.torrents = [];
    state.torrentsSig = '';
    $('#torrentRows').innerHTML = '';
    $('#torrentEmpty').hidden = false;
    $('#torrentEmpty').textContent = `No se pudo leer qBittorrent: ${err.message}`;
    renderHeader();
    return;
  }
  const sig = JSON.stringify(torrents);
  if (!force && sig === state.torrentsSig) return;  // unchanged: skip the DOM rebuild
  state.torrentsSig = sig;
  state.torrents = torrents;
  $('#torrentEmpty').textContent = 'No hay torrents con este filtro.';
  renderFilters();
  renderTorrents();
  renderHeader();
}

async function loadTransfers() {
  state.transfers = await api('/api/transfers');
  renderTransfers();
}

async function refreshAll() {
  const jobs = [loadTorrents(), loadTransfers(), loadStatus()];
  if (!state.settingsDirty) jobs.push(loadSettings());  // never wipe unsaved edits
  await Promise.allSettled(jobs);
}

async function deleteTransfer(id) {
  try {
    await api(`/api/transfers/${id}`, { method: 'DELETE' });
    state.transfers = state.transfers.filter((t) => t.id !== Number(id));
    renderTransfers();
    toast('Eliminado', '', 'ok');
  } catch (err) {
    toast('No se pudo eliminar', err.message, 'error');
  }
}

// ── Real-time (SSE) ────────────────────────────────────────
function connectEvents() {
  if (!window.EventSource) return;
  const sse = new EventSource('/api/events');
  sse.addEventListener('transfers', () => debounce('transfers', () => loadTransfers().catch(() => {}), 300));
  sse.addEventListener('torrents', () => debounce('torrents', () => loadTorrents().catch(() => {}), 1000));
  // Progress arrives as a small payload and is patched in place.
  sse.addEventListener('progress', (e) => {
    const { id, message } = JSON.parse(e.data);
    const t = state.transfers.find((x) => x.id === id);
    if (!t) return debounce('transfers', () => loadTransfers().catch(() => {}), 300);
    t.status = 'transferring';
    t.message = message;
    renderLive();
    renderQueue();
    if (state.tab === 'torrents') renderTorrents();
  });
  sse.addEventListener('open', () => debounce('transfers', () => loadTransfers().catch(() => {}), 300));
}

// ── Events ─────────────────────────────────────────────────
function bindEvents() {
  window.addEventListener('hashchange', () => showTab(location.hash.slice(1)));

  $('#torrentSearch').addEventListener('input', (e) => { state.search = e.target.value; renderTorrents(); });
  $('#trackerFilter').addEventListener('change', (e) => { state.tracker = e.target.value; renderTorrents(); });
  $('#selectAll').addEventListener('change', (e) => {
    const active = activeByHash();
    filteredTorrents().filter((t) => isTransferable(t, active)).forEach((t) => {
      e.target.checked ? state.selected.add(t.hash) : state.selected.delete(t.hash);
    });
    renderTorrents();
  });
  $('#btnBulkTransfer').addEventListener('click', () => queueTorrents([...state.selected]));
  $('#btnBulkClear').addEventListener('click', () => { state.selected.clear(); renderTorrents(); });

  $('#runOnceBtn').addEventListener('click', async () => {
    try { await api('/api/worker/run-once', { method: 'POST' }); toast('Procesando la cola', '', 'ok'); }
    catch (err) { toast('Error', err.message, 'error'); }
  });
  $('#verifyBtn').addEventListener('click', async () => {
    try { await api('/api/worker/verify', { method: 'POST' }); toast('Verificando el destino', 'Los cambios aparecerán en unos segundos', 'ok'); }
    catch (err) { toast('Error', err.message, 'error'); }
  });
  $('#historyMore').addEventListener('click', () => { state.historyLimit += 50; renderHistory(); });

  const form = $('#settingsForm');
  form.addEventListener('input', () => { setDirty(true); syncForm(); });
  form.addEventListener('change', () => { setDirty(true); syncForm(); });
  form.addEventListener('submit', (e) => { e.preventDefault(); saveSettings(); });
  $('#saveBtn').addEventListener('click', saveSettings);
  $('#testBtn').addEventListener('click', testConnection);
  window.addEventListener('beforeunload', (e) => { if (state.settingsDirty) e.preventDefault(); });

  document.body.addEventListener('change', (e) => {
    const box = e.target.closest('[data-select]');
    if (!box) return;
    box.checked ? state.selected.add(box.dataset.select) : state.selected.delete(box.dataset.select);
    renderTorrents();
  });

  document.body.addEventListener('click', (e) => {
    const t = e.target;
    const filter = t.closest('[data-filter]');
    if (filter) { state.filter = filter.dataset.filter; renderFilters(); renderTorrents(); return; }
    const hfilter = t.closest('[data-hfilter]');
    if (hfilter) { state.historyFilter = hfilter.dataset.hfilter; state.historyLimit = 50; renderHistory(); return; }
    const transfer = t.closest('[data-transfer]');
    if (transfer) { transfer.disabled = true; queueTorrents([transfer.dataset.transfer]); return; }
    const del = t.closest('[data-delete]');
    if (del) { deleteTransfer(del.dataset.delete); return; }
    const log = t.closest('[data-log]');
    if (log) {
      const id = Number(log.dataset.log);
      state.openLogs.has(id) ? state.openLogs.delete(id) : state.openLogs.add(id);
      renderHistory();
      return;
    }
    const pw = t.closest('.pw-toggle');
    if (pw) {
      const input = pw.parentElement.querySelector('input');
      input.type = input.type === 'password' ? 'text' : 'password';
      pw.classList.toggle('is-visible', input.type === 'text');
    }
  });
}

// ── Init ───────────────────────────────────────────────────
buildSettingsForm();
bindEvents();
showTab(location.hash.slice(1) || 'torrents');
connectEvents();
refreshAll();

const visible = () => !document.hidden;
// Speeds come from qBittorrent (no SSE): poll only while the Torrents tab is on screen.
setInterval(() => { if (visible() && state.tab === 'torrents') loadTorrents().catch(() => {}); }, 10000);
// The server caches this for 60 s, so extra tabs cost no SSH connections.
setInterval(() => { if (visible()) loadStatus(); }, 60000);
// Elapsed timers.
setInterval(() => { if (visible() && running()) { renderLive(); if (state.tab === 'queue') renderQueue(); } }, 1000);
document.addEventListener('visibilitychange', () => { if (visible()) refreshAll(); });
