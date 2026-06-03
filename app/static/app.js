const state = {
  settings: null,
  torrents: [],
  transfers: [],
  activeTrackers: new Set(),
  activeStateFilter: '',
  lastRefreshAt: null,
  nextCycleSeconds: 15,
};

// ── Utils ──────────────────────────────────────────────────
const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));
const escHtml = (v) => String(v ?? '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;').replace(/'/g,'&#039;');

function bytes(v) {
  if (!v) return '0 B';
  const units = ['B','KB','MB','GB','TB'];
  let s = v, u = 0;
  while (s >= 1024 && u < units.length - 1) { s /= 1024; u++; }
  return `${s.toFixed(s >= 10 || u === 0 ? 0 : 1)} ${units[u]}`;
}

function pct(v) { return `${Math.round((v||0)*100)}%`; }

function relTime(iso) {
  if (!iso) return '';
  const diff = Date.now() - new Date(iso).getTime();
  const m = Math.floor(diff / 60000);
  if (m < 1) return 'ahora';
  if (m < 60) return `hace ${m}m`;
  const h = Math.floor(m / 60);
  if (h < 24) return `hace ${h}h`;
  return `hace ${Math.floor(h/24)}d`;
}

// ── Torrent state mapping ──────────────────────────────────
const STATE_MAP = {
  uploading:      { label: 'Seeding',       cls: 'state-seeding' },
  stalledUP:      { label: 'Seeding',       cls: 'state-seeding' },
  forcedUP:       { label: 'Seeding',       cls: 'state-seeding' },
  queuedUP:       { label: 'En cola (seed)',cls: 'state-queued' },
  checkingUP:     { label: 'Verificando',   cls: 'state-checking' },
  downloading:    { label: 'Descargando',   cls: 'state-downloading' },
  stalledDL:      { label: 'Descargando',   cls: 'state-downloading' },
  forcedDL:       { label: 'Descargando',   cls: 'state-downloading' },
  queuedDL:       { label: 'En cola',       cls: 'state-queued' },
  checkingDL:     { label: 'Verificando',   cls: 'state-checking' },
  checkingResumeData: { label: 'Verificando', cls: 'state-checking' },
  pausedUP:       { label: 'Pausado',       cls: 'state-paused' },
  pausedDL:       { label: 'Pausado',       cls: 'state-paused' },
  error:          { label: 'Error',         cls: 'state-error' },
  missingFiles:   { label: 'Archivos faltantes', cls: 'state-error' },
  moving:         { label: 'Moviendo',      cls: 'state-checking' },
  unknown:        { label: 'Desconocido',   cls: 'state-unknown' },
};

const TRANSFER_STATE_MAP = {
  pending:      { label: 'Pendiente',     cls: 'badge-warn' },
  waiting:      { label: 'Esperando',     cls: 'badge-warn' },
  transferring: { label: 'Transfiriendo', cls: 'badge-accent' },
  completed:    { label: 'Completado',    cls: 'badge-ok' },
  failed:       { label: 'Fallido',       cls: 'badge-bad' },
  cancelled:    { label: 'Cancelado',     cls: 'badge-default' },
  missing:      { label: 'Borrado en destino', cls: 'badge-missing' },
};

function stateInfo(raw) {
  return STATE_MAP[raw] || { label: raw, cls: 'state-unknown' };
}

function transferStateInfo(raw) {
  return TRANSFER_STATE_MAP[raw] || { label: raw, cls: 'badge-default' };
}

// ── Toast ──────────────────────────────────────────────────
function toast(title, sub = '', type = 'info') {
  const icons = {
    ok:    '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"/></svg>',
    error: '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>',
    info:  '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/></svg>',
  };
  const el = document.createElement('div');
  el.className = `toast toast-${type}`;
  el.innerHTML = `
    <div class="toast-icon">${icons[type] || icons.info}</div>
    <div class="toast-body">
      <div class="toast-title">${escHtml(title)}</div>
      ${sub ? `<div class="toast-sub">${escHtml(sub)}</div>` : ''}
    </div>`;
  $('#toastContainer').appendChild(el);
  setTimeout(() => {
    el.classList.add('leaving');
    el.addEventListener('animationend', () => el.remove());
  }, 4000);
}

// ── API ────────────────────────────────────────────────────
async function api(path, options = {}) {
  const res = await fetch(path, {
    headers: { 'Content-Type': 'application/json', ...(options.headers || {}) },
    ...options,
  });
  if (!res.ok) {
    const p = await res.json().catch(() => ({}));
    throw new Error(p.detail || res.statusText);
  }
  return res.json();
}

// ── Settings form ──────────────────────────────────────────
function getPath(src, path) { return path.split('.').reduce((v,k) => v?.[k], src); }
function setPath(tgt, path, val) {
  const parts = path.split('.');
  let cur = tgt;
  parts.slice(0,-1).forEach(p => { cur[p] = cur[p] || {}; cur = cur[p]; });
  cur[parts.at(-1)] = val;
}

function fillForm(settings) {
  $$('[name]').forEach(input => {
    const val = getPath(settings, input.name);
    if (input.type === 'checkbox') { input.checked = Boolean(val); return; }
    input.value = val ?? '';
  });
}

function readForm() {
  const s = structuredClone(state.settings);
  $$('[name]').forEach(input => {
    let val = input.type === 'checkbox' ? input.checked : input.value;
    if (input.type === 'number') val = Number(val);
    setPath(s, input.name, val);
  });
  return s;
}

// ── Render torrents ────────────────────────────────────────
function getFilteredTorrents() {
  const search = $('#torrentSearch')?.value.toLowerCase() || '';
  const stateF = state.activeStateFilter || '';

  return state.torrents.filter(t => {
    if (search && !t.name.toLowerCase().includes(search)) return false;
    if (state.activeTrackers.size > 0 && !state.activeTrackers.has(t.tracker)) return false;
    if (stateF) {
      if (torrentGroup(t) !== stateF) return false;
    }
    return true;
  });
}

function torrentGroup(torrent) {
  const { cls } = stateInfo(torrent.state);
  if (cls.includes('seeding')) return 'seeding';
  if (cls.includes('downloading')) return 'downloading';
  if (cls.includes('paused')) return 'paused';
  if (cls.includes('queued') || cls.includes('checking')) return 'queued';
  if (cls.includes('error')) return 'error';
  return 'unknown';
}

function updateStateFilterCounts() {
  const counts = { all: state.torrents.length, seeding: 0, downloading: 0, paused: 0, queued: 0, error: 0 };
  state.torrents.forEach(t => {
    const group = torrentGroup(t);
    if (counts[group] !== undefined) counts[group] += 1;
  });
  Object.entries(counts).forEach(([key, value]) => {
    const el = $(`#count-${key}`);
    if (el) el.textContent = value;
  });
  $$('.filter-row').forEach(row => {
    row.classList.toggle('active', row.dataset.stateFilter === state.activeStateFilter);
  });
}

function updateTrackerFilter() {
  const trackers = [...new Set(state.torrents.map(t => t.tracker).filter(Boolean))].sort();
  const container = $('#trackerFilters');
  if (!container) return;
  
  // Limpiar trackers activos que ya no existen
  const currentActive = new Set([...state.activeTrackers].filter(t => trackers.includes(t)));
  state.activeTrackers = currentActive;

  const trackerCounts = state.torrents.reduce((acc, torrent) => {
    if (torrent.tracker) acc[torrent.tracker] = (acc[torrent.tracker] || 0) + 1;
    return acc;
  }, {});

  let html = `<button class="tracker-pill ${state.activeTrackers.size === 0 ? 'active' : ''}" data-tracker=""><span class="check-box"></span><span>Todos</span><b>${state.torrents.length}</b></button>`;
  html += trackers.map(t => 
    `<button class="tracker-pill ${state.activeTrackers.has(t) ? 'active' : ''}" data-tracker="${escHtml(t)}"><span class="check-box"></span><span>${escHtml(t)}</span><b>${trackerCounts[t] || 0}</b></button>`
  ).join('');
  
  container.innerHTML = html;
}

function renderTorrents() {
  const list = getFilteredTorrents();
  updateStateFilterCounts();
  $('#torrentCount').textContent = `${list.length} de ${state.torrents.length} torrents`;

  // Badge
  const active = state.torrents.filter(t => !STATE_MAP[t.state] || !['state-seeding','state-paused'].includes(STATE_MAP[t.state]?.cls)).length;
  const badge = $('#navBadgeTorrents');
  badge.textContent = state.torrents.length;
  badge.classList.toggle('visible', state.torrents.length > 0);

  const el = $('#torrentList');
  if (!list.length) {
    el.innerHTML = `<div class="empty"><svg width="40" height="40" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/></svg><span>No hay torrents que mostrar</span></div>`;
    return;
  }

  el.innerHTML = `
    <table class="torrent-table">
      <thead>
        <tr>
          <th class="col-check"><div class="row-check"></div></th>
          <th class="col-priority">#</th>
          <th class="col-icon"></th>
          <th class="col-name">Nombre</th>
          <th class="col-size">Tamaño</th>
          <th class="col-progress">Progreso</th>
          <th class="col-action"></th>
          <th class="col-status">Estado</th>
          <th class="col-seeds">Seeds</th>
          <th class="col-peers">Peers</th>
          <th class="col-speed">Bajada</th>
          <th class="col-speed">Subida</th>
        </tr>
      </thead>
      <tbody>
        ${list.map((t, index) => {
          const { label, cls } = stateInfo(t.state);
          const isComplete = t.progress >= 1;
          const queued = t.queued;
          return `
            <tr data-hash="${escHtml(t.hash)}" title="${escHtml(t.content_path || t.save_path)}">
              <td><div class="row-check"></div></td>
              <td>${index + 1}</td>
              <td><span class="torrent-icon">↓</span></td>
              <td><span class="name-cell">${escHtml(t.name)}</span></td>
              <td>${bytes(t.size)}</td>
              <td>
                <div class="progress-cell">
                  <div class="progress-bar"><div class="progress-fill ${isComplete?'complete':''}" style="width:${pct(t.progress)}"></div></div>
                  <span class="progress-label">${Math.round((t.progress || 0) * 100)}</span>
                </div>
              </td>
              <td>
                ${t.transfer_status === 'completed'
                  ? `<span class="badge badge-ok" title="Transferido">OK</span>`
                  : `<button class="table-action" data-transfer="${escHtml(t.hash)}" ${queued?'disabled':''} title="${queued ? 'Ya está en cola' : 'Transferir'}">
                      <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 3v12"/><path d="m7 10 5 5 5-5"/><path d="M5 21h14"/></svg>
                    </button>`
                }
              </td>
              <td><span class="badge ${cls}">${label}</span></td>
              <td>0 (0)</td>
              <td>0 (0)</td>
              <td>-</td>
              <td>-</td>
            </tr>`;
        }).join('')}
      </tbody>
    </table>`;
}

// ── Render queue ───────────────────────────────────────────
function renderQueue() {
  const active = state.transfers.filter(t =>
    ['pending','waiting','transferring'].includes(t.status)
  );

  renderSidebarStats();

  const badge = $('#navBadgeQueue');
  badge.textContent = active.length;
  badge.classList.toggle('visible', active.length > 0);

  const el = $('#transferList');
  if (!active.length) {
    el.innerHTML = `<div class="empty"><svg width="40" height="40" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="22 12 18 12 15 21 9 3 6 12 2 12"/></svg><span>La cola está vacía</span></div>`;
    return;
  }

  el.innerHTML = active.map(t => {
    const { label, cls } = transferStateInfo(t.status);
    let isTransferring = t.status === 'transferring';
    let pctNum = 0;
    if (isTransferring && t.message && t.message.includes('%')) {
      const match = t.message.match(/(\d+)%/);
      if (match) pctNum = parseInt(match[1]);
    }

    return `
    <article class="item">
      <div class="item-header">
        <div class="item-title">${escHtml(t.torrent_name)}</div>
        <div class="item-actions">
          <span class="badge ${cls}">${label}</span>
          <span class="badge badge-default">${bytes(t.size)}</span>
        </div>
      </div>
      <div class="item-path">${escHtml(t.source_path)} → ${escHtml(t.destination_path)}</div>
      ${isTransferring ? `
        <div class="progress-bar">
          <div class="progress-fill" style="width:${pctNum}%"></div>
        </div>
      ` : ''}
      ${t.message && !isTransferring ? `<div class="item-path" style="margin-top:4px;color:var(--ink-2)">${escHtml(t.message)}</div>` : ''}
    </article>`;
  }).join('');
}

// ── Render logs ────────────────────────────────────────────
function renderSidebarStats() {
  const counts = {
    pending: state.transfers.filter(t => ['pending','waiting'].includes(t.status)).length,
    transferring: state.transfers.filter(t => t.status === 'transferring').length,
    completed: state.transfers.filter(t => t.status === 'completed').length,
    failed: state.transfers.filter(t => t.status === 'failed').length,
  };

  Object.entries(counts).forEach(([key, value]) => {
    const el = $(`#metric-${key}`);
    if (el) el.textContent = value;
  });

  const last = $('#activity-last-refresh');
  if (last) last.textContent = state.lastRefreshAt ? relTime(state.lastRefreshAt.toISOString()) : 'Ahora';

  const next = $('#activity-next-cycle');
  if (next) next.textContent = `${state.nextCycleSeconds} s`;
}

function renderLogs() {
  const filterVal = $('#logsFilter')?.value || '';
  const done = state.transfers.filter(t =>
    ['completed','failed','cancelled'].includes(t.status) &&
    (!filterVal || t.status === filterVal)
  );

  const el = $('#logsList');
  if (!done.length) {
    el.innerHTML = `<div class="empty"><svg width="40" height="40" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/></svg><span>No hay registros</span></div>`;
    return;
  }

  el.innerHTML = done.map(t => {
    const { label, cls } = transferStateInfo(t.status);
    const msgClean = (t.message || '').replace(/\d+\s+\d+%.*?\n?/g, '').trim();
    return `
    <article class="log-item">
      <div class="log-header">
        <div>
          <div class="item-title">${escHtml(t.torrent_name)}</div>
          <div class="item-meta">
            <span class="badge ${cls}">${label}</span>
            <span class="badge badge-default">${bytes(t.size)}</span>
            <span style="font-size:11px;color:var(--ink-3)">${relTime(t.completed_at || t.updated_at)}</span>
          </div>
          <div class="item-path">${escHtml(t.source_path)} → ${escHtml(t.destination_path)}</div>
        </div>
        <div class="item-actions">
          ${msgClean ? `<button class="log-toggle" data-id="${t.id}">Ver log</button>` : ''}
          <button class="btn btn-danger" data-delete="${t.id}" title="Eliminar del historial">
            <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="3 6 5 6 21 6"/><path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"/><path d="M10 11v6M14 11v6"/><path d="M9 6V4h6v2"/></svg>
          </button>
        </div>
      </div>
      ${msgClean ? `<div class="log-message" id="log-msg-${t.id}">${escHtml(msgClean)}</div>` : ''}
    </article>`;
  }).join('');
}

// ── Navigation ─────────────────────────────────────────────
function switchPanel(panelId) {
  $$('.nav-item[data-panel]').forEach(b => b.classList.remove('active'));
  $$('.panel').forEach(p => p.classList.remove('active'));
  $(`#nav-${panelId}`)?.classList.add('active');
  $(`#panel-${panelId}`)?.classList.add('active');
}

// ── Data loading ───────────────────────────────────────────
async function loadSettings() {
  state.settings = await api('/api/settings');
  fillForm(state.settings);
}

async function loadStatus() {
  try {
    const st = await api('/api/status');
    $('#dot-qbit').className = 'status-dot ' + (st.qbit ? 'ok' : 'bad');
    $('#dot-vps').className = 'status-dot ' + (st.vps ? 'ok' : 'bad');
    $('#dot-destination').className = 'status-dot ' + (st.destination ? 'ok' : 'bad');
  } catch (err) {
    $$('.status-dot').forEach(el => el.className = 'status-dot bad');
  }
}

async function restartSsh(target) {
  try {
    toast(`Reiniciando SSH en ${target}...`, '', 'info');
    await api(`/api/system/restart-ssh/${target}`, { method: 'POST' });
    toast(`SSH reiniciado (${target})`, '', 'ok');
    setTimeout(loadStatus, 2000);
  } catch (err) {
    toast(`Error al reiniciar SSH`, err.message, 'error');
  }
}

async function loadTorrents() {
  state.torrents = await api('/api/torrents');
  state.lastRefreshAt = new Date();
  updateTrackerFilter();
  renderTorrents();
  renderSidebarStats();
}

async function loadTransfers() {
  state.transfers = await api('/api/transfers');
  state.lastRefreshAt = new Date();
  renderQueue();
  renderLogs();
}

async function refreshAll() {
  try {
    await Promise.all([loadSettings(), loadTorrents(), loadTransfers(), loadStatus()]);
  } catch (err) {
    toast('Error al actualizar', err.message, 'error');
  }
}

async function saveSettings() {
  try {
    state.settings = await api('/api/settings', { method: 'PUT', body: JSON.stringify(readForm()) });
    fillForm(state.settings);
    toast('Ajustes guardados', '', 'ok');
  } catch (err) {
    toast('Error al guardar', err.message, 'error');
  }
}

async function queueTransfer(hash) {
  const torrent = state.torrents.find(t => t.hash === hash);
  if (!torrent) return;
  try {
    await api('/api/transfers', {
      method: 'POST',
      body: JSON.stringify({
        torrent_hash: torrent.hash,
        torrent_name: torrent.name,
        source_path: torrent.content_path,
        size: torrent.size,
      }),
    });
    toast('Transferencia agregada', torrent.name, 'ok');
    await Promise.all([loadTorrents(), loadTransfers()]);
  } catch (err) {
    toast('Error al agregar', err.message, 'error');
  }
}

async function deleteTransfer(id) {
  try {
    await api(`/api/transfers/${id}`, { method: 'DELETE' });
    state.transfers = state.transfers.filter(t => t.id !== Number(id));
    renderLogs();
    renderQueue();
    toast('Eliminado del historial', '', 'ok');
  } catch (err) {
    toast('Error al eliminar', err.message, 'error');
  }
}

// ── Theme ──────────────────────────────────────────────────
function applyTheme(theme) {
  document.documentElement.dataset.theme = theme;
  localStorage.setItem('transferv-theme', theme);
}

function toggleTheme() {
  const cur = document.documentElement.dataset.theme;
  applyTheme(cur === 'dark' ? 'light' : 'dark');
}

// ── Event bindings ─────────────────────────────────────────
function bindEvents() {
  // Sidebar nav
  $$('.nav-item[data-panel]').forEach(btn => {
    btn.addEventListener('click', () => switchPanel(btn.dataset.panel));
  });

  $('#refreshBtn')?.addEventListener('click', refreshAll);
  $('#saveBtn')?.addEventListener('click', saveSettings);
  $('#themeToggleBtn')?.addEventListener('click', toggleTheme);

  $('#runOnceBtn')?.addEventListener('click', async () => {
    try {
      await api('/api/worker/run-once', { method: 'POST' });
      await loadTransfers();
      toast('Ciclo procesado', '', 'info');
    } catch (err) {
      toast('Error', err.message, 'error');
    }
  });

  // Filters
  $('#torrentSearch')?.addEventListener('input', renderTorrents);
  $('#logsFilter')?.addEventListener('change', renderLogs);

  // Delegation: transfer, delete, log toggle, tracker pills, restart ssh
  document.body.addEventListener('click', (e) => {
    const restartBtn = e.target.closest('[data-restart]');
    if (restartBtn) {
      restartSsh(restartBtn.dataset.restart);
      return;
    }

    const trackerPill = e.target.closest('.tracker-pill');
    if (trackerPill) {
      const tracker = trackerPill.dataset.tracker;
      if (!tracker) {
        state.activeTrackers.clear();
      } else {
        if (state.activeTrackers.has(tracker)) {
          state.activeTrackers.delete(tracker);
        } else {
          state.activeTrackers.add(tracker);
        }
      }
      updateTrackerFilter();
      renderTorrents();
      return;
    }

    const stateFilterBtn = e.target.closest('[data-state-filter]');
    if (stateFilterBtn) {
      state.activeStateFilter = stateFilterBtn.dataset.stateFilter || '';
      renderTorrents();
      return;
    }

    const transferBtn = e.target.closest('[data-transfer]');
    if (transferBtn && !transferBtn.disabled) {
      queueTransfer(transferBtn.dataset.transfer);
      return;
    }
    const deleteBtn = e.target.closest('[data-delete]');
    if (deleteBtn) {
      deleteTransfer(deleteBtn.dataset.delete);
      return;
    }
    const logToggle = e.target.closest('.log-toggle');
    if (logToggle) {
      const msg = $(`#log-msg-${logToggle.dataset.id}`);
      if (msg) {
        msg.classList.toggle('expanded');
        logToggle.textContent = msg.classList.contains('expanded') ? 'Ocultar log' : 'Ver log';
      }
    }
  });
}

// ── Init ───────────────────────────────────────────────────
const savedTheme = localStorage.getItem('transferv-theme') || 'dark';
applyTheme(savedTheme);

bindEvents();
refreshAll();
setInterval(() => {
  state.nextCycleSeconds = 15;
  loadTorrents().catch(() => {});
  loadTransfers().catch(() => {});
}, 15000);
setInterval(() => {
  state.nextCycleSeconds = Math.max(0, state.nextCycleSeconds - 1);
  renderSidebarStats();
}, 1000);
setInterval(() => {
  loadStatus().catch(() => {});
}, 30000);
