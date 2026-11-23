"use strict";
// Everything the server sends (player names above all) is untrusted: it only
// ever reaches the page through textContent, never innerHTML.

const $ = (selector, root = document) => root.querySelector(selector);
const POLL_MS = 4000;
const MODE_LABELS = { coop: "Co-op", realism: "Realism", versus: "Versus", survival: "Survival", scavenge: "Scavenge" };
const DIFFICULTY_LABELS = { Easy: "Easy", Normal: "Normal", Hard: "Advanced", Impossible: "Expert" };

const state = { overview: null, maps: null, tab: "overview", jobSeen: null, jobHidden: null, timers: {} };

function h(tag, props = {}, ...children) {
  const el = document.createElement(tag);
  for (const [key, value] of Object.entries(props)) {
    if (key === "class") el.className = value;
    else if (key.startsWith("on")) el.addEventListener(key.slice(2), value);
    else if (value !== false && value != null) el.setAttribute(key, value === true ? "" : value);
  }
  for (const child of children.flat()) {
    if (child != null && child !== false) el.append(child);
  }
  return el;
}

function toast(message, bad = false) {
  const el = h("div", { class: "toast" + (bad ? " bad" : ""), role: bad ? "alert" : "status" }, message);
  $("#toasts").append(el);
  setTimeout(() => el.remove(), bad ? 8000 : 4000);
}

async function api(path, body) {
  const options = { credentials: "same-origin" };
  if (body !== undefined) {
    options.method = "POST";
    options.headers = { "Content-Type": "application/json", "X-Requested-With": "l4d2-panel" };
    options.body = JSON.stringify(body);
  }
  let response;
  try {
    response = await fetch(path, options);
  } catch {
    throw new Error("Can't reach the panel. Is the machine on?");
  }
  let data = {};
  try { data = await response.json(); } catch { /* non-JSON error page */ }
  if (response.status === 401 && path !== "/api/login") {
    showLogin();
    throw new Error("Signed out. Sign in again.");
  }
  if (!response.ok) throw new Error(data.error || `Request failed (${response.status}).`);
  return data;
}

// Run an action, show what happened, refresh. Returns the result or null.
async function act(fn, success) {
  try {
    const result = await fn();
    if (result && result.job) pollJob(true);
    else if (success || (result && result.message)) toast(success || result.message);
    refresh();
    return result || {};
  } catch (error) {
    toast(error.message, true);
    return null;
  }
}

function confirmDialog({ title, text, ok = "Confirm", check }) {
  const dialog = $("#confirm");
  $("#confirm-title").textContent = title;
  $("#confirm-text").textContent = text || "";
  $("#confirm-ok").textContent = ok;
  $("#confirm-extra").hidden = !check;
  $("#confirm-check").checked = false;
  if (check) $("#confirm-check-label").textContent = check;
  return new Promise((resolve) => {
    dialog.addEventListener("close", () => {
      resolve(dialog.returnValue === "ok" ? { checked: $("#confirm-check").checked } : null);
    }, { once: true });
    dialog.showModal();
  });
}

function whoIsOn() {
  const players = (state.overview && state.overview.players) || [];
  if (!players.length) return "Nobody is playing right now.";
  return `${players.length} player(s) will be disconnected: ${players.map((p) => p.name).join(", ")}.`;
}

// ------------------------------------------------------------------ format
function duration(seconds) {
  if (seconds == null) return "–";
  const d = Math.floor(seconds / 86400), hrs = Math.floor((seconds % 86400) / 3600), m = Math.floor((seconds % 3600) / 60);
  if (d) return `${d}d ${hrs}h`;
  if (hrs) return `${hrs}h ${m}m`;
  return `${m}m`;
}

function bytes(n) {
  if (n == null) return "?";
  const gb = n / 1073741824;
  return gb >= 10 ? `${gb.toFixed(0)} GB` : `${gb.toFixed(1)} GB`;
}

function meter(id, fraction, text) {
  const pct = Math.max(0, Math.min(1, fraction || 0));
  const bar = $(`#m-${id}`);
  bar.style.width = `${(pct * 100).toFixed(0)}%`;
  bar.className = pct > 0.9 ? "full" : pct > 0.75 ? "hot" : "";
  $(`#m-${id}-text`).textContent = text;
}

// ---------------------------------------------------------------- overview
function renderOverview(o) {
  const { server, game, players, system, join } = o;
  const up = server.running;

  $("#hostname").textContent = game.hostname || "L4D2 Server";
  document.title = `${up ? players.length + " online" : "Offline"} · ${game.hostname || "L4D2 Server"}`;
  $("#status-dot").className = "dot " + (!up ? "down" : game.reachable ? "up" : "warn");
  $("#status-line").textContent = !up ? "Server is stopped"
    : !game.reachable ? "Running, but the panel can't talk to it"
    : `Running · ${game.campaign || game.map || "loading map"}`;
  $("#btn-start").disabled = up;
  $("#btn-stop").disabled = !up;
  $("#btn-restart").disabled = !up;

  $("#t-players").textContent = game.reachable ? `${game.humans} / ${game.max_players ?? "?"}` : "–";
  $("#t-bots").textContent = game.reachable && game.bots ? `+ ${game.bots} bot survivors` : "";
  $("#t-map").textContent = game.campaign || game.map || "–";
  $("#t-campaign").textContent = game.campaign ? game.map : "";
  $("#t-mode").textContent = MODE_LABELS[game.mode] || game.mode || "–";
  $("#t-difficulty").textContent = (DIFFICULTY_LABELS[game.difficulty] || game.difficulty || "") + (game.cheats ? " · cheats on" : "");
  $("#t-uptime").textContent = up ? duration(server.uptime) : "Stopped";
  $("#t-service").textContent = `service: ${server.service}`;

  renderPlayers(players, game, up);
  $("#players-updated").textContent = `updated ${new Date(o.time * 1000).toLocaleTimeString()}`;

  const lines = [];
  if (join.password) lines.push(`password ${join.password}`);
  lines.push(join.connect || "connect <server address>:27015");
  $("#join").textContent = lines.join("\n");

  if (system.load) meter("load", system.load[0] / system.cpus, `${system.load[0].toFixed(2)} on ${system.cpus} core(s)`);
  if (system.mem_total) {
    const used = system.mem_total - system.mem_available;
    meter("mem", used / system.mem_total, `${bytes(used)} of ${bytes(system.mem_total)}`);
  }
  if (system.disk_total) {
    const used = system.disk_total - system.disk_free;
    meter("disk", used / system.disk_total, `${bytes(system.disk_free)} free`);
  }

  renderBanners(o);
  const cheats = $("#cheats");
  if (document.activeElement !== cheats) cheats.checked = game.cheats;
  cheats.disabled = !game.reachable;
  if (state.maps) renderMaps();
  markPlaying();
  if (o.job && o.job.running && state.jobSeen !== o.job.id) pollJob(true);
}

function renderPlayers(players, game, up) {
  const box = $("#players");
  if (!up) return box.replaceChildren(h("p", { class: "empty" }, "The server is stopped."));
  if (!game.reachable) return box.replaceChildren(h("p", { class: "empty" }, "Can't read the player list right now."));
  if (!players.length) return box.replaceChildren(h("p", { class: "empty" }, "Nobody is playing right now."));
  const rows = players.map((p) => h("tr", {},
    h("td", { class: "name" }, p.name),
    h("td", { class: "id" }, p.steamid),
    h("td", { class: "num" }, p.connected || "–"),
    h("td", { class: "num" }, p.ping ? `${p.ping} ms` : "–"),
    h("td", { class: "num" }, h("button", { class: "btn small danger", onclick: () => kick(p) }, "Kick"))));
  box.replaceChildren(h("div", { class: "table-wrap" }, h("table", {},
    h("thead", {}, h("tr", {}, h("th", {}, "Player"), h("th", {}, "Steam ID"),
      h("th", { class: "num" }, "Connected"), h("th", { class: "num" }, "Ping"), h("th", {}))),
    h("tbody", {}, rows))));
}

function renderBanners(o) {
  const banners = [];
  if (o.server.running && !o.game.reachable) {
    banners.push(h("div", { class: "banner bad", role: "alert" }, h("span", {}, o.game.error || "RCON is unavailable.")));
  }
  if (o.server.running && o.server.restart_needed.length) {
    banners.push(h("div", { class: "banner" },
      h("span", {}, `Restart to apply: ${o.server.restart_needed.join("; ")}.`),
      h("button", { class: "btn small", onclick: () => control("restart") }, "Restart now")));
  }
  $("#banners").replaceChildren(...banners);
}

async function kick(player) {
  const yes = await confirmDialog({ title: `Kick ${player.name}?`, text: "They can rejoin unless you change the password.", ok: "Kick" });
  if (yes) act(() => api("/api/kick", { userid: player.userid }));
}

async function control(action) {
  const copy = {
    start: null,
    stop: { title: "Stop the server?", ok: "Stop" },
    restart: { title: "Restart the server?", ok: "Restart" },
    update: { title: "Update game files?", ok: "Update", extra: " The server is down while Steam downloads the update." },
  }[action];
  if (copy) {
    const yes = await confirmDialog({ title: copy.title, text: whoIsOn() + (copy.extra || ""), ok: copy.ok });
    if (!yes) return;
  }
  act(() => api("/api/control", { action }));
}

// -------------------------------------------------------------------- maps
// First map of the campaign being played right now ("" when unknown). The game
// reports the chapter (c2m3_coaster); cards and dropdowns list first chapters.
function playingMap() {
  const game = state.overview && state.overview.game;
  if (!game || !game.reachable) return "";
  return String(game.campaign_map || game.map || "").toLowerCase();
}

// The "starts on" dropdown is a saved setting, not the live map, so it can't
// follow the game. Instead the option being played is labelled, and a line
// under it says so when the two differ. Runs on every poll.
function markPlaying() {
  const select = $("#settings-form").default_map;
  const playing = playingMap();
  const game = state.overview && state.overview.game;
  // The saved start map may be a later chapter, so the exact map counts too.
  const chapter = playing ? String(game.map || "").toLowerCase() : "";
  const isPlaying = (value) => playing !== "" && [playing, chapter].includes(value.toLowerCase());
  for (const option of select.options) {
    const base = option.dataset.label || option.textContent;
    option.dataset.label = base;
    const text = isPlaying(option.value) ? `${base} · playing now` : base;
    if (option.textContent !== text) option.textContent = text;   // leave an open dropdown alone
  }
  const note = $("#default-map-note");
  if (!playing || !select.options.length) note.textContent = "";
  else if (isPlaying(select.value)) note.textContent = "This is the map being played now.";
  else note.textContent = `Now playing: ${game.campaign || game.map}. Changing this only affects the next restart; switch the running map on the Maps tab.`;
}

async function loadMaps() {
  try {
    state.maps = await api("/api/maps");
    renderMaps();
  } catch (error) { toast(error.message, true); }
}

function renderMaps() {
  const maps = state.maps, o = state.overview;
  const playing = playingMap();
  const up = o && o.game.reachable;
  $("#maps-current").textContent = (o && (o.game.campaign || o.game.map)) || "–";
  $("#maps-default").textContent = maps.default || "–";

  const cards = [];
  const card = (map, title, custom) => {
    const isCurrent = playing !== "" && playing === map.toLowerCase();
    return h("div", { class: "campaign" + (isCurrent ? " current" : "") },
      h("div", {}, h("span", { class: "title" }, title),
        custom && h("span", { class: "tag" }, "custom"),
        isCurrent && h("span", { class: "tag on" }, "playing"),
        maps.default === map && h("span", { class: "tag on" }, "default")),
      h("div", { class: "mapname" }, map),
      h("div", { class: "row tight" },
        h("button", { class: "btn small", disabled: !up, onclick: () => changeMap(map, title) }, isCurrent ? "Restart map" : "Play now")));
  };
  for (const c of maps.custom) if (c.first_map && !c.missing) cards.push(card(c.first_map, c.name, c));
  for (const m of maps.official) cards.push(card(m.map, m.name, null));
  $("#campaigns").replaceChildren(...cards);

  const list = $("#custom-maps");
  if (!maps.custom.length) {
    list.replaceChildren(h("p", { class: "empty" }, "No custom maps installed yet."));
  } else {
    list.replaceChildren(h("div", { class: "table-wrap" }, h("table", {}, h("tbody", {}, maps.custom.map((c) => h("tr", {},
      h("td", { class: "name" }, c.name,
        c.missing && h("span", { class: "tag bad" }, "file missing"),
        !c.first_map && h("span", { class: "tag" }, "addon")),
      h("td", { class: "id" }, c.first_map || ""),
      h("td", { class: "num" }, h("button", { class: "btn small danger", onclick: () => removeMap(c) }, "Remove"))))))));
  }

  const links = maps.custom.filter((c) => c.url).map((c) => `${c.name}\n${c.url}`);
  const files = maps.custom.filter((c) => !c.url).map((c) => c.file);
  let share = links.length ? links.join("\n\n") : "No Workshop maps installed - friends don't need anything extra.";
  if (files.length) share += `\n\nInstalled from files (send friends the same file):\n${files.join("\n")}`;
  $("#share").textContent = share;
}

async function changeMap(map, title) {
  const answer = await confirmDialog({
    title: `Switch to ${title}?`,
    text: "Players are moved to the new map. Anyone missing a custom map is kicked.",
    ok: "Switch map",
    check: "Also start on this map after every restart",
  });
  if (!answer) return;
  await act(() => api("/api/map", { name: map, default: answer.checked }));
  loadMaps();
}

async function removeMap(item) {
  const yes = await confirmDialog({ title: `Remove ${item.name}?`, text: "The map file is deleted from the server.", ok: "Remove" });
  if (yes) act(() => api("/api/maps/remove", { key: item.key }));
}

// ---------------------------------------------------------------- settings
function fillSelect(select, options, value) {
  select.replaceChildren(...options.map(([v, label]) => h("option", { value: v }, label)));
  select.value = value;
}

async function loadSettings() {
  try {
    const [s, maps] = await Promise.all([api("/api/settings"), api("/api/maps")]);
    state.maps = maps;
    const form = $("#settings-form");
    form.hostname.value = s.hostname;
    form.password.value = s.password;
    fillSelect(form.difficulty, s.choices.difficulties.map((d) => [d, DIFFICULTY_LABELS[d] || d]), s.difficulty);
    fillSelect(form.mode, s.choices.modes.map((m) => [m, MODE_LABELS[m] || m]), s.mode);
    const mapOptions = [
      ...maps.custom.filter((c) => c.first_map && !c.missing).map((c) => [c.first_map, `${c.name} (custom)`]),
      ...maps.official.map((m) => [m.map, m.name]),
    ];
    if (s.default_map && !mapOptions.some(([v]) => v === s.default_map)) mapOptions.unshift([s.default_map, s.default_map]);
    fillSelect(form.default_map, mapOptions, s.default_map);
    markPlaying();
    fillSelect($("#slots"), s.choices.slots.map((n) => [String(n), n === 4 ? "4 (normal, no mods)" : `${n} players`]), String(s.slots));
    $("#settings-note").textContent = s.rcon_enabled ? "" : "RCON is off: changes apply after a restart.";
  } catch (error) { toast(error.message, true); }
}

async function saveSettings(event) {
  event.preventDefault();
  const form = event.target;
  const result = await act(() => api("/api/settings", {
    hostname: form.hostname.value,
    password: form.password.value,
    difficulty: form.difficulty.value,
    mode: form.mode.value,
    default_map: form.default_map.value,
  }));
  if (!result) return;
  if (!result.changed.length) toast("Nothing to change.");
  else if (result.live_failed.length) toast(`Saved. Not applied to the running server yet: ${result.live_failed.join(", ")}. Restart to apply.`, true);
  else toast(`Saved: ${result.changed.join(", ")}.` + (result.restart_needed ? " Restart to apply the game mode." : ""));
  loadSettings();
}

async function loadRawConfig() {
  try {
    const raw = await api("/api/config");
    $("#raw-text").value = raw.text;
    $("#raw-path").textContent = raw.path;
  } catch (error) { toast(error.message, true); }
}

async function saveRawConfig() {
  const yes = await confirmDialog({ title: "Replace server.cfg?", text: "The previous version is kept as a backup next to the file.", ok: "Save" });
  if (!yes) return;
  const result = await act(() => api("/api/config", { text: $("#raw-text").value, apply: $("#raw-apply").checked }));
  if (!result) return;
  toast(result.applied ? "Saved and applied." : "Saved.");
  for (const warning of result.warnings) toast(warning, true);
  loadSettings();
}

// ----------------------------------------------------------------- console
async function runRcon(event) {
  event.preventDefault();
  const input = $("#rcon-cmd"), out = $("#rcon-out");
  const command = input.value.trim();
  if (!command) return;
  input.value = "";
  let text;
  try {
    text = (await api("/api/rcon", { command })).output || "(no output)";
  } catch (error) { text = `! ${error.message}`; }
  out.textContent = (out.textContent + `\n] ${command}\n${text}\n`).slice(-20000).trimStart();
  out.scrollTop = out.scrollHeight;
  refresh();
}

async function loadLogs() {
  const out = $("#log-out");
  try {
    const { lines } = await api("/api/logs?lines=200");
    const atEnd = out.scrollHeight - out.scrollTop - out.clientHeight < 40;
    out.textContent = lines.join("\n") || "(log is empty)";
    if (atEnd) out.scrollTop = out.scrollHeight;
  } catch (error) { out.textContent = error.message; }
}

// --------------------------------------------------------------------- job
async function pollJob(show = false) {
  clearTimeout(state.timers.job);
  let job;
  try { job = (await api("/api/job")).job; } catch { return; }
  if (!job) return;
  // On page load, don't resurrect a job that finished long ago.
  if (!show && !job.running && state.jobSeen !== job.id && Date.now() / 1000 - job.finished > 600) return;
  if (show) state.jobHidden = null;
  state.jobSeen = job.id;
  const title = $("#job-title");
  title.className = job.running ? "spin" : "";
  title.textContent = job.running ? job.name : `${job.name}: ${job.exit_code === 0 ? "done" : `failed (exit ${job.exit_code})`}`;
  const out = $("#job-out");
  out.textContent = job.output || (job.running ? "Starting…" : "(no output)");
  out.scrollTop = out.scrollHeight;
  $("#job").hidden = state.jobHidden === job.id;
  if (job.running) {
    state.timers.job = setTimeout(pollJob, 2000);
  } else {
    refresh();
    if (state.tab === "maps") loadMaps();
    if (state.tab === "settings") loadSettings();
  }
}

// -------------------------------------------------------------------- shell
async function refresh() {
  clearTimeout(state.timers.poll);
  try {
    state.overview = await api("/api/overview");
    renderOverview(state.overview);
  } catch { /* api() already switched to the login screen if that was the cause */ }
  if (!$("#app").hidden) state.timers.poll = setTimeout(refresh, document.hidden ? POLL_MS * 5 : POLL_MS);
}

function showTab(name) {
  state.tab = name;
  for (const button of document.querySelectorAll(".tabs button")) {
    button.setAttribute("aria-selected", String(button.dataset.tab === name));
  }
  for (const section of document.querySelectorAll(".tab")) section.hidden = section.id !== `tab-${name}`;
  clearInterval(state.timers.logs);
  if (name === "maps") loadMaps();
  if (name === "settings") loadSettings();
  if (name === "console") {
    loadLogs();
    state.timers.logs = setInterval(() => { if ($("#log-follow").checked && !document.hidden) loadLogs(); }, 5000);
  }
}

function showLogin() {
  clearTimeout(state.timers.poll);
  clearTimeout(state.timers.job);
  clearInterval(state.timers.logs);
  $("#app").hidden = true;
  $("#login").hidden = false;
  $("#login-password").focus();
}

function showApp() {
  $("#login").hidden = true;
  $("#app").hidden = false;
  showTab("overview");
  refresh();
  pollJob();
}

function copyFrom(selector) {
  const text = $(selector).textContent;
  if (navigator.clipboard && window.isSecureContext) {
    navigator.clipboard.writeText(text).then(() => toast("Copied."), () => toast("Couldn't copy - select the text instead.", true));
  } else {
    // Plain http over Tailscale isn't a "secure context", so no clipboard API.
    const range = document.createRange();
    range.selectNodeContents($(selector));
    const selection = getSelection();
    selection.removeAllRanges();
    selection.addRange(range);
    toast(document.execCommand("copy") ? "Copied." : "Selected - press Ctrl+C to copy.");
  }
}

function wire() {
  $("#login-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const input = $("#login-password");
    $("#login-error").textContent = "";
    try {
      await api("/api/login", { password: input.value });
      input.value = "";
      showApp();
    } catch (error) { $("#login-error").textContent = error.message; }
  });
  $("#btn-logout").addEventListener("click", async () => {
    try { await api("/api/logout", {}); } catch { /* signing out regardless */ }
    showLogin();
  });
  for (const button of document.querySelectorAll("[data-control]")) {
    button.addEventListener("click", () => control(button.dataset.control));
  }
  for (const button of document.querySelectorAll(".tabs button")) {
    button.addEventListener("click", () => showTab(button.dataset.tab));
  }
  $("#say-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const input = $("#say-text");
    if (input.value.trim() && await act(() => api("/api/say", { text: input.value }))) input.value = "";
  });
  $("#addmap-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const input = $("#addmap-links");
    if (await act(() => api("/api/maps/add", { links: input.value }))) input.value = "";
  });
  $("#btn-update-maps").addEventListener("click", () => act(() => api("/api/maps/update", {})));
  $("#btn-copy-join").addEventListener("click", () => copyFrom("#join"));
  $("#btn-copy-share").addEventListener("click", () => copyFrom("#share"));

  $("#settings-form").addEventListener("submit", saveSettings);
  $("#settings-form").default_map.addEventListener("change", markPlaying);
  $("#btn-show-password").addEventListener("click", (event) => {
    const input = $("#settings-form").password;
    input.type = input.type === "password" ? "text" : "password";
    event.target.textContent = input.type === "password" ? "Show" : "Hide";
  });
  $("#cheats").addEventListener("change", async (event) => {
    const on = event.target.checked;
    if (!await act(() => api("/api/settings", { cheats: on }), `Cheats ${on ? "on" : "off"}.`)) event.target.checked = !on;
  });
  $("#btn-slots").addEventListener("click", async () => {
    const slots = Number($("#slots").value);
    const yes = await confirmDialog({
      title: `Set co-op to ${slots} players?`,
      text: slots > 4 ? "This downloads SourceMod and MultiSlots onto the server. It takes effect after a restart."
        : "MultiSlots is switched off. It takes effect after a restart.",
      ok: "Apply",
    });
    if (yes) act(() => api("/api/slots", { slots }));
  });
  $("#btn-update-game").addEventListener("click", () => control("update"));
  $("#raw-config").addEventListener("toggle", (event) => { if (event.target.open) loadRawConfig(); });
  $("#btn-raw-reload").addEventListener("click", loadRawConfig);
  $("#btn-raw-save").addEventListener("click", saveRawConfig);

  $("#rcon-form").addEventListener("submit", runRcon);
  $("#btn-log-refresh").addEventListener("click", loadLogs);
  $("#btn-job-close").addEventListener("click", () => { state.jobHidden = state.jobSeen; $("#job").hidden = true; });
  document.addEventListener("visibilitychange", () => { if (!document.hidden && !$("#app").hidden) refresh(); });
}

wire();
fetch("/api/session", { credentials: "same-origin" })
  .then((response) => response.json())
  .then((session) => (session.authenticated ? showApp() : showLogin()))
  .catch(showLogin);
