const state = {
  settings: null,
  torrents: [],
  transfers: [],
};

const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => Array.from(document.querySelectorAll(selector));

function showNotice(message, isError = false) {
  const notice = $("#notice");
  notice.textContent = message;
  notice.hidden = false;
  notice.classList.toggle("error", isError);
  clearTimeout(showNotice.timer);
  showNotice.timer = setTimeout(() => {
    notice.hidden = true;
  }, 6000);
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    ...options,
  });
  if (!response.ok) {
    const payload = await response.json().catch(() => ({}));
    throw new Error(payload.detail || response.statusText);
  }
  return response.json();
}

function getPath(source, path) {
  return path.split(".").reduce((value, key) => value?.[key], source);
}

function setPath(target, path, value) {
  const parts = path.split(".");
  let cursor = target;
  parts.slice(0, -1).forEach((part) => {
    cursor[part] = cursor[part] || {};
    cursor = cursor[part];
  });
  cursor[parts.at(-1)] = value;
}

function fillSettingsForm(settings) {
  $$("[name]").forEach((input) => {
    const value = getPath(settings, input.name);
    if (input.type === "checkbox") {
      input.checked = Boolean(value);
      return;
    }
    input.value = value ?? "";
  });
}

function readSettingsForm() {
  const settings = structuredClone(state.settings);
  $$("[name]").forEach((input) => {
    let value = input.type === "checkbox" ? input.checked : input.value;
    if (input.type === "number") {
      value = Number(value);
    }
    setPath(settings, input.name, value);
  });
  return settings;
}

function bytes(value) {
  if (!value) return "0 B";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let size = value;
  let unit = 0;
  while (size >= 1024 && unit < units.length - 1) {
    size /= 1024;
    unit += 1;
  }
  return `${size.toFixed(size >= 10 || unit === 0 ? 0 : 1)} ${units[unit]}`;
}

function percent(value) {
  return `${Math.round((value || 0) * 100)}%`;
}

function transferButton(torrent) {
  const disabled = torrent.queued ? "disabled" : "";
  const text = torrent.queued ? "En cola" : "Transferir";
  return `<button type="button" data-transfer="${torrent.hash}" ${disabled}>${text}</button>`;
}

function renderTorrents() {
  $("#torrentCount").textContent = `${state.torrents.length} torrents`;
  const list = $("#torrentList");
  if (!state.torrents.length) {
    list.innerHTML = `<div class="empty">No hay torrents para mostrar.</div>`;
    return;
  }
  list.innerHTML = state.torrents.map((torrent) => `
    <article class="item">
      <div class="item-main">
        <div>
          <div class="title">${escapeHtml(torrent.name)}</div>
          <div class="meta">${escapeHtml(torrent.state)} · ${bytes(torrent.size)} · ${escapeHtml(torrent.content_path || torrent.save_path)}</div>
          <div class="progress"><span style="width:${percent(torrent.progress)}"></span></div>
        </div>
        <div class="actions">
          <span class="badge">${percent(torrent.progress)}</span>
          ${transferButton(torrent)}
        </div>
      </div>
    </article>
  `).join("");
}

function renderTransfers() {
  const list = $("#transferList");
  if (!state.transfers.length) {
    list.innerHTML = `<div class="empty">Todavía no hay transferencias.</div>`;
    return;
  }
  list.innerHTML = state.transfers.map((transfer) => `
    <article class="item">
      <div class="item-main">
        <div>
          <div class="title">${escapeHtml(transfer.torrent_name)}</div>
          <div class="meta">${escapeHtml(transfer.source_path)} → ${escapeHtml(transfer.destination_path)}</div>
          <div class="meta">${escapeHtml(transfer.message || "")}</div>
        </div>
        <div class="actions">
          <span class="badge ${transfer.status}">${escapeHtml(transfer.status)}</span>
          <span class="badge">${bytes(transfer.size)}</span>
        </div>
      </div>
    </article>
  `).join("");
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

async function loadSettings() {
  state.settings = await api("/api/settings");
  fillSettingsForm(state.settings);
}

async function loadTorrents() {
  state.torrents = await api("/api/torrents");
  renderTorrents();
}

async function loadTransfers() {
  state.transfers = await api("/api/transfers");
  renderTransfers();
}

async function refreshAll() {
  try {
    await Promise.all([loadSettings(), loadTorrents(), loadTransfers()]);
  } catch (error) {
    showNotice(error.message, true);
  }
}

async function saveSettings() {
  try {
    state.settings = await api("/api/settings", {
      method: "PUT",
      body: JSON.stringify(readSettingsForm()),
    });
    fillSettingsForm(state.settings);
    showNotice("Ajustes guardados.");
  } catch (error) {
    showNotice(error.message, true);
  }
}

async function queueTransfer(hash) {
  const torrent = state.torrents.find((item) => item.hash === hash);
  if (!torrent) return;
  try {
    await api("/api/transfers", {
      method: "POST",
      body: JSON.stringify({
        torrent_hash: torrent.hash,
        torrent_name: torrent.name,
        source_path: torrent.content_path,
        size: torrent.size,
      }),
    });
    showNotice("Transferencia agregada a la cola.");
    await Promise.all([loadTorrents(), loadTransfers()]);
  } catch (error) {
    showNotice(error.message, true);
  }
}

function bindEvents() {
  $("#refreshBtn").addEventListener("click", refreshAll);
  $("#saveBtn").addEventListener("click", saveSettings);
  $("#runOnceBtn").addEventListener("click", async () => {
    try {
      await api("/api/worker/run-once", { method: "POST" });
      await loadTransfers();
      showNotice("Ciclo procesado.");
    } catch (error) {
      showNotice(error.message, true);
    }
  });

  document.body.addEventListener("click", (event) => {
    const transfer = event.target.closest("[data-transfer]");
    if (transfer) queueTransfer(transfer.dataset.transfer);
  });

  $$(".tab").forEach((tab) => {
    tab.addEventListener("click", () => {
      $$(".tab").forEach((item) => item.classList.remove("active"));
      $$(".panel").forEach((item) => item.classList.remove("active"));
      tab.classList.add("active");
      $(`#${tab.dataset.tab}`).classList.add("active");
    });
  });
}

bindEvents();
refreshAll();
setInterval(() => {
  loadTorrents().catch(() => {});
  loadTransfers().catch(() => {});
}, 15000);
