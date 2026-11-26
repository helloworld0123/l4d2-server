"use strict";
// Everything the server sends (player names above all) is untrusted: it only
// ever reaches the page through textContent, never innerHTML.

const $ = (selector, root = document) => root.querySelector(selector);
const POLL_MS = 4000;
const NEEDS_MODS = "Needs the mods that more than 4 player slots install (Settings).";
const SIGNED_OUT = "Signed out. Sign in again.";
const TABS = ["overview", "maps", "settings", "console"];
// What the Give picker offers: the game's own item names, by kind.
const GIVE_ITEMS = [
  ["Health", [["first_aid_kit", "First aid kit"], ["defibrillator", "Defibrillator"], ["pain_pills", "Pain pills"], ["adrenaline", "Adrenaline shot"]]],
  ["Throwables", [["molotov", "Molotov"], ["pipe_bomb", "Pipe bomb"], ["vomitjar", "Bile jar"]]],
  ["Rifles", [["rifle_ak47", "AK-47"], ["rifle", "M16 rifle"], ["rifle_desert", "SCAR (desert rifle)"], ["rifle_sg552", "SG 552"]]],
  ["Shotguns", [["autoshotgun", "Auto shotgun"], ["shotgun_spas", "SPAS combat shotgun"], ["pumpshotgun", "Pump shotgun"], ["shotgun_chrome", "Chrome shotgun"]]],
  ["Snipers", [["sniper_military", "Military sniper"], ["hunting_rifle", "Hunting rifle"], ["sniper_awp", "AWP"], ["sniper_scout", "Scout"]]],
  ["SMGs and pistols", [["smg", "Uzi"], ["smg_silenced", "Silenced SMG"], ["smg_mp5", "MP5"], ["pistol_magnum", "Magnum"], ["pistol", "Pistol"]]],
  ["Special", [["grenade_launcher", "Grenade launcher"], ["rifle_m60", "M60"], ["chainsaw", "Chainsaw"]]],
  ["Melee (some only exist on certain maps)", [["katana", "Katana"], ["machete", "Machete"], ["fireaxe", "Fire axe"], ["crowbar", "Crowbar"],
    ["baseball_bat", "Baseball bat"], ["cricket_bat", "Cricket bat"], ["frying_pan", "Frying pan"], ["electric_guitar", "Guitar"],
    ["tonfa", "Nightstick"], ["golfclub", "Golf club"]]],
  ["Upgrades for the weapon in hand", [["laser_sight", "Laser sight"], ["incendiary_ammo", "Incendiary ammo (one clip)"], ["explosive_ammo", "Explosive ammo (one clip)"]]],
  ["Deployable ammo packs", [["upgradepack_incendiary", "Incendiary ammo pack"], ["upgradepack_explosive", "Explosive ammo pack"]]],
];
const TEAM_NOTE = "Bots fill the seats nobody plays. Now, until the server restarts.";
// The session switches. key is the overview's name for the state: null there
// means the server can't do it, false/true that it is off/on.
const SWITCHES = [
  { key: "tank_run", label: "Tank run", note: "Tanks keep coming.", when: "Now, until you turn it off or the server restarts",
    set: (on) => api("/api/tankrun", { on }) },
  { key: "double_loot", label: "Double loot", note: "Twice the pills, throwables, ammo and special weapons.", when: "From the next map, until the server restarts",
    set: (on) => api("/api/loot", { on }) },
  { key: "infinite_ammo", label: "Infinite ammo", note: "Guns never run dry or need reloading; throwables don't run out either.", when: "Now, until the server restarts",
    set: (on) => api("/api/ammo", { on }) },
  { key: "reverse_ff", label: "Reverse friendly fire", note: "Shoot a teammate and you take the damage instead.", when: "Now, until the server restarts",
    set: (on) => api("/api/reverseff", { on }) },
  { key: "cheats", label: "Cheats", note: "Allows cheat commands for everyone.", when: "Now, until the server restarts",
    set: (on) => api("/api/settings", { cheats: on }).then(() => ({ message: `Cheats ${on ? "on" : "off"}.` })) },
];
const QUICK_COMMANDS = ["status", "users", "sm_roster", "sm plugins list", "sm exts list", "z_difficulty", "sm_cvar mp_gamemode", "sv_cheats"];
const PROBLEM_RE = /\b(?:errors?|failed|failure|exceptions?|fatal|crash(?:ed)?|warnings?|unable|couldn't|could not)\b/i;

const state = {
  overview: null, maps: null, mapFilter: "all", mapQuery: "", tab: "overview", jobSeen: null, jobHidden: null, timers: {},
  offline: "", settingsSaved: null, slotsSaved: null, rawSaved: null, history: [], historyAt: 0,
  seq: 0, drawn: {}, switching: {}, openPlayers: new Set(), holding: null, logLines: [], logError: "",
};

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

// Redraws only when what is shown really changed, so a button is never swapped
// out from under the pointer, the keyboard focus or a screen reader.
function changed(name, value) {
  const key = JSON.stringify(value);
  if (state.drawn[name] === key) return false;
  state.drawn[name] = key;
  return true;
}

// Replaces a list's rows and gives the keyboard focus back to the same control
// (found by its data-k) in the new ones.
function redraw(box, ...rows) {
  const active = document.activeElement;
  const key = box.contains(active) && active.dataset.k;
  box.replaceChildren(...rows);
  if (key) for (const el of box.querySelectorAll("[data-k]")) if (el.dataset.k === key) el.focus({ preventScroll: true });
}

// Good news leaves by itself; a problem stays until it is dismissed.
function toast(message, bad = false) {
  const box = $("#toasts");
  for (const old of box.children) if (old.dataset.message === message) old.remove();
  const el = h("div", { class: "toast" + (bad ? " bad" : ""), role: bad ? "alert" : "status", "data-message": message }, h("span", {}, message),
    h("button", { class: "toast-close", type: "button", "aria-label": "Dismiss", onclick: () => el.remove() }, "×"));
  box.append(el);
  // Crowded: earlier good news goes first, then the oldest problem; never this one.
  while (box.children.length > 4) {
    const others = [...box.children].filter((t) => t !== el);
    (others.find((t) => !t.classList.contains("bad")) || others[0]).remove();
  }
  if (!bad) setTimeout(() => el.remove(), 4000);
}

// Marks a button as working until the promise settles, so a slow action can't
// be fired twice and never looks like it was ignored.
async function busy(button, promise) {
  if (!button) return promise;
  // Not `disabled`: that would drop the keyboard focus. wire() swallows the clicks.
  button.setAttribute("aria-busy", "true");
  try {
    return await promise;
  } finally {
    button.removeAttribute("aria-busy");
  }
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
    throw new Error(SIGNED_OUT);
  }
  if (!response.ok) throw new Error(data.error || `Request failed (${response.status}).`);
  return data;
}

// Run an action, show what happened, refresh. Returns the result or null.
async function act(fn, success, button) {
  try {
    const result = await busy(button, fn());
    if (result && result.job) pollJob(true);
    else if (success || (result && result.message)) toast(success || result.message);
    refresh();
    return result || {};
  } catch (error) {
    toast(error.message, true);
    return null;
  }
}

// Resolves to { checked, choice } on OK and to null on Cancel, Esc or a click outside.
function confirmDialog({ title, text, ok = "Confirm", check, choose, danger = false }) {
  const dialog = $("#confirm");
  $("#confirm-title").textContent = title;
  $("#confirm-text").textContent = text || "";
  $("#confirm-ok").textContent = ok;
  $("#confirm-ok").className = "btn " + (danger ? "danger-solid" : "primary");
  $("#confirm-choose").hidden = !choose;
  if (choose) {
    $("#confirm-choose-label").textContent = choose.label;
    fillSelect($("#confirm-select"), choose.options, choose.value);
  }
  $("#confirm-extra").hidden = !check;
  $("#confirm-check").checked = false;
  if (check) $("#confirm-check-label").textContent = check;
  // Esc leaves the previous answer in place, so an earlier "ok" must not linger.
  dialog.returnValue = "";
  return new Promise((resolve) => {
    dialog.addEventListener("close", () => {
      resolve(dialog.returnValue === "ok" ? { checked: $("#confirm-check").checked, choice: choose ? $("#confirm-select").value : null } : null);
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
  // Only what can be done now. A server that is crash-looping isn't up but can still be stopped.
  const busyService = ["active", "activating", "reloading"].includes(server.service);
  $("#btn-start").hidden = up;
  $("#btn-stop").hidden = !up && !busyService;
  $("#btn-restart").hidden = !up && !busyService;

  $("#t-players").textContent = game.reachable ? `${game.humans} / ${game.max_players ?? "?"}` : "–";
  // The game's own bot count includes the infected; the roster knows the survivors.
  const botCount = (o.survivors || []).filter((s) => s.bot).length;
  $("#t-bots").textContent = game.reachable && botCount ? `+ ${botCount} bot survivor${botCount === 1 ? "" : "s"}` : "";
  $("#t-map").textContent = game.campaign || game.map || "–";
  $("#t-campaign").textContent = game.campaign ? game.map : "";
  $("#t-mode").textContent = game.mode_label || "–";
  $("#t-difficulty").textContent = game.difficulty_label || "";
  $("#t-uptime").textContent = up ? duration(server.uptime) : "Stopped";
  $("#t-service").textContent = `service: ${server.service}`;

  renderPlayers(o, up);

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
  renderSwitches(game, o.presets || []);
  renderActivity(o.activity || []);
  // team is null when the server can't resize the survivor team (no mods).
  const team = $("#team-size");
  if (!state.teamBusy && changed("team", game.team)) {
    const sizes = [];
    for (let n = 4; n <= (game.team ? game.team.max : 4); n++) sizes.push(n);
    fillSelect(team, sizes.map((n) => [String(n), n === 4 ? "4 survivors (normal)" : `${n} survivors (up to ${n - 1} bots)`]),
      String(game.team ? game.team.size : 4));
  }
  team.disabled = !game.reachable || game.team == null;
  $("#team-note").textContent = !game.reachable || game.team != null ? TEAM_NOTE : NEEDS_MODS;
  if (state.maps) renderMaps();
  markPlaying();
  if (o.job && o.job.running && state.jobSeen !== o.job.id) pollJob(true);
}

// ----------------------------------------------------------------- session
const switchId = (key) => `sw-${key.replace("_", "-")}`;

function buildSwitches() {
  $("#switches").replaceChildren(...SWITCHES.map((s) => h("label", { class: "switch" },
    h("input", { type: "checkbox", role: "switch", id: switchId(s.key), "aria-describedby": `${switchId(s.key)}-note`, disabled: true,
      onchange: (event) => flip(s, event.target.checked) }),
    h("span", { class: "switch-text" }, h("span", {}, s.label),
      h("span", { class: "muted small", id: `${switchId(s.key)}-note` }, s.note, h("span", { class: "when" }, s.when))))));
}

// While a change is on its way the switch shows what was asked for and ignores
// further clicks; afterwards (and at every refresh) it shows what the server has.
async function flip(s, on) {
  if (s.key in state.switching || state.presetBusy) return drawSwitch(s);
  state.switching[s.key] = on;
  drawSwitch(s);
  try {
    const result = await s.set(on);
    toast(result.message);
    if (state.overview) state.overview.game[s.key] = on;
  } catch (error) {
    toast(error.message, true);
  }
  delete state.switching[s.key];
  drawSwitch(s);
  if (state.overview) renderSwitches(state.overview.game, state.overview.presets || []);
  refresh();
}

function drawSwitch(s) {
  const input = $("#" + switchId(s.key)), game = state.overview && state.overview.game;
  const pending = s.key in state.switching;
  input.checked = pending ? state.switching[s.key] : !!game && game[s.key] === true;
  input.setAttribute("aria-busy", String(pending));
  input.disabled = !pending && (!game || !game.reachable || game[s.key] == null);
}

function renderSwitches(game, presets) {
  for (const s of SWITCHES) {
    drawSwitch(s);
    const missing = game.reachable && game[s.key] == null;
    const note = $(`#${switchId(s.key)}-note`);
    if (changed(`note-${s.key}`, missing)) note.replaceChildren(missing ? NEEDS_MODS : s.note, missing ? "" : h("span", { class: "when" }, s.when));
  }
  const isOn = (key) => (key in state.switching ? state.switching[key] : game[key] === true);
  const on = game.reachable ? SWITCHES.filter((s) => isOn(s.key)) : [];
  const busyKeys = Object.keys(state.switching);
  if (changed("modifiers", [on.map((s) => s.key), busyKeys])) {
    $("#modifiers").hidden = !on.length;
    redraw($("#modifiers"), h("span", { class: "muted small" }, "On now"), ...on.map((s) => h("button", {
      class: "chip", type: "button", "data-k": s.key, "aria-label": `Turn off ${s.label}`, title: "Turn off", "aria-busy": busyKeys.includes(s.key) && "true",
      onclick: () => { if (!(s.key in state.switching)) flip(s, false); },
    }, s.label, h("span", { class: "x", "aria-hidden": "true" }, "×"))));
  }
  const keys = on.map((s) => s.key).sort().join();
  if (changed("presets", [presets, keys, game.reachable, state.presetBusy])) {
    redraw($("#presets"), ...presets.map((p) => h("button", {
      class: "btn small", type: "button", "data-k": p.id, title: p.note, "aria-pressed": String(game.reachable && [...p.on].sort().join() === keys),
      disabled: !game.reachable, "aria-busy": state.presetBusy === p.id && "true", onclick: () => applyPreset(p),
    }, p.label)));
  }
}

async function applyPreset(preset) {
  if (state.presetBusy || Object.keys(state.switching).length) return;
  state.presetBusy = preset.id;
  renderSwitches(state.overview.game, state.overview.presets || []);
  try {
    const result = await api("/api/preset", { name: preset.id });
    toast(result.message, result.failed.length > 0);
  } catch (error) { toast(error.message, true); }
  state.presetBusy = null;
  await refresh();
  if (state.overview) renderSwitches(state.overview.game, state.overview.presets || []);
}

function ago(then, now) {
  const seconds = Math.max(0, now - then);
  if (seconds < 60) return "just now";
  if (seconds < 3600) return `${Math.floor(seconds / 60)} min ago`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)} h ago`;
  return `${Math.floor(seconds / 86400)} d ago`;
}

function renderActivity(entries) {
  // Panel and browser clocks can differ: the age is worked out from the panel's own.
  const now = state.overview.time;
  const rows = entries.map((e) => [e.kind, e.count > 1 ? `${e.text} (×${e.count})` : e.text, ago(e.time, now), e.time]);
  if (!changed("activity", rows)) return;
  $("#activity").replaceChildren(...(rows.length ? rows.map(([kind, text, age, time]) => h("li", { class: kind },
    h("span", { class: "what" }, text), h("time", { datetime: new Date(time * 1000).toISOString(), title: new Date(time * 1000).toLocaleString() }, age)))
    : [h("li", { class: "empty" }, "Nothing yet. Joins, kicks, map changes and what is done in this panel show up here.")]));
}

// -------------------------------------------------------------- survivors
const EVERYONE = { userid: "all", name: "everyone" };

function healthBar(s) {
  if (s.state === "dead") return h("div", { class: "health" }, h("span", { class: "tag bad" }, "Dead"));
  const down = s.state === "down";
  const total = s.health + s.temp;
  const tone = down || total < 25 ? "critical" : total < 40 ? "low" : "";
  const solid = h("i", { class: tone }), temp = h("i", { class: "temp" });
  // Someone who is down bleeds out from 300, not 100.
  const full = down ? 300 : 100;
  const share = Math.min(100, (s.health / full) * 100);
  solid.style.width = `${share}%`;
  temp.style.width = `${Math.max(0, Math.min(100 - share, s.temp))}%`;
  return h("div", { class: "health" },
    h("div", { class: "health-head" },
      down ? h("span", { class: "tag bad" }, "Down") : h("span", {}, "Health"),
      h("span", {}, down ? `${s.health} left` : s.temp ? `${s.health} + ${s.temp}` : String(s.health))),
    h("div", { class: "bar", role: "img", "aria-label": down ? `Down, ${s.health} left` : `Health ${total}` }, solid, temp));
}

// One list for everyone in the game. The roster (admin extras plugin) knows the
// survivor team, bots included; the status list knows the humans, with ping and
// Steam ID. Joined by userid: a human in both is one row, a human only in the
// status list is watching or still loading, and a server without the plugin
// falls back to the humans alone.
function renderPlayers(o, up) {
  const { game, players, survivors } = o;
  const box = $("#players");
  const roster = survivors || [];
  const usable = up && game.reachable && roster.length > 0;
  for (const button of $("#team-actions").querySelectorAll("button")) {
    if (button.getAttribute("aria-busy") !== "true") button.disabled = !usable;
  }
  $("#team-actions").hidden = !usable;
  const dead = usable ? roster.filter((s) => s.state === "dead").length : 0;
  $("#btn-respawn-all").hidden = !dead;
  $("#btn-respawn-all").textContent = `Respawn dead (${dead})`;
  const bots = roster.filter((s) => s.bot).length;
  $("#players-count").textContent = !up || !game.reachable ? ""
    : [`${players.length} online`, bots && `${bots} bot${bots === 1 ? "" : "s"}`].filter(Boolean).join(" · ");

  const live = (p) => [["ping", p.ping ? `${p.ping} ms` : ""], ["on", p.connected ? `on for ${p.connected}` : ""]];
  // Never mid-click or while an action is still running: the next poll catches up.
  if (box.contains(state.holding) || box.querySelector('[aria-busy="true"]')) return;
  // Ping and time connected tick all the time: they are updated in place.
  if (!changed("players", [up, game.reachable, survivors, players.map((p) => [p.userid, p.name, p.steamid])])) {
    for (const p of players) for (const [what, text] of live(p)) {
      const el = box.querySelector(`[data-v="${CSS.escape(`${p.userid}-${what}`)}"]`);
      if (el && el.textContent !== text) el.textContent = text;
    }
    return;
  }

  if (!up) return box.replaceChildren(h("p", { class: "empty" }, "The server is stopped. Press Start to bring it up."));
  if (!game.reachable) return box.replaceChildren(h("p", { class: "empty" }, "Can't read the player list right now."));
  if (!players.length && !roster.length) {
    return box.replaceChildren(h("p", { class: "empty" }, "Nobody is in the game right now. Once someone joins, every survivor and bot is listed here with Heal, Kit, Ammo, Give and Respawn buttons."));
  }

  const humans = new Map(players.map((p) => [p.userid, p]));
  const button = (label, name, target, kind, onclick) => h("button", {
    class: "btn small " + kind, type: "button", "data-k": `${target.userid}-${name}`, "aria-label": `${label.replace("…", "")} ${target.name}`, onclick,
  }, label);
  const meta = (p) => [...live(p).map(([what, text]) => h("span", { "data-v": `${p.userid}-${what}` }, text)), h("span", {}, p.steamid)];
  const kickButton = (p) => h("button", { class: "kick", type: "button", "data-k": `${p.userid}-kick`, "aria-label": `Kick ${p.name}`, onclick: () => kick(p) }, "Kick");
  const row = (id, name, tags, human, health, urgent, actions, state_ = "") => {
    const open = state.openPlayers.has(id);
    const more = h("button", { class: "btn small more", type: "button", "data-k": `${id}-more`, "aria-expanded": String(open), "aria-label": `More for ${name}`,
      onclick: () => {
        const now = el.classList.toggle("open");
        more.setAttribute("aria-expanded", String(now));
        if (now) state.openPlayers.add(id); else state.openPlayers.delete(id);
      } }, "⋯");
    const el = h("div", { class: `player ${state_}${open ? " open" : ""}${urgent ? " urgent" : ""}` },
      // Kick sits with the player's details, away from the Heal / Give buttons, so
      // reaching for one never lands on the other.
      h("div", { class: "who" }, h("span", { class: "name" }, name, tags),
        human && h("span", { class: "meta" }, ...meta(human), kickButton(human))),
      health || h("span", {}), h("div", { class: "actions" }, ...actions),
      // On a phone the row is one line; this opens the rest of it.
      (human || actions.length > 1) && more);
    return el;
  };

  const rows = roster.map((s) => {
    const human = !s.bot && humans.get(s.userid);
    if (human) humans.delete(s.userid);
    const act_ = (label, name, kind) => button(label, name, s, kind, (event) => name === "give" ? openGive(s)
      : survivorAction(name, s, event.currentTarget));
    // The one thing this survivor needs stands out; the rest stays quiet.
    const need = needOf(s);
    const actions = s.state === "dead" ? [act_("Respawn", "respawn", "primary")]
      : [act_(s.state === "down" ? "Revive" : "Heal", "heal", { urgent: "primary", hurt: "", fine: "quiet" }[need]),
        act_("Kit", "kit", "quiet"), act_("Ammo", "ammo", "quiet"), act_("Give…", "give", "quiet")];
    return row(s.userid, s.name, s.bot && h("span", { class: "tag" }, "bot"), human, healthBar(s), need === "urgent", actions, s.state);
  });
  // Humans the roster doesn't have: spectating, on the infected team, or loading in.
  const others = [...humans.values()].map((p) => row(p.userid, p.name, survivors != null && h("span", { class: "tag" }, "not on the team"), p, null, false, []));
  const parts = [...rows, ...others];
  if (survivors == null) {
    parts.push(h("p", { class: "muted small hint-line" },
      "Health, healing and handing out items need the newest admin extras plugin: update the game files, set more than 4 player slots once in Settings, then restart."));
  }
  redraw(box, h("div", { class: "players" }, ...parts));
}

// What a survivor needs: "urgent" (dead, down or nearly), "hurt" or "fine".
function needOf(s) {
  if (s.state !== "alive" || s.health + s.temp < 40) return "urgent";
  return s.health < 100 ? "hurt" : "fine";
}

const ACTION_DONE = {
  heal: (who) => `Healed ${who}.`,
  kit: (who) => `First aid kit given to ${who}.`,
  ammo: (who) => `Ammo refilled for ${who}.`,
  respawn: (who) => who === "everyone" ? "The dead are back." : `${who} is back.`,
};

async function survivorAction(name, target, button) {
  await act(() => api("/api/survivor", { action: name, target: target.userid }), ACTION_DONE[name](target.name), button);
}

// The picker stays open after a click: a survivor often needs a weapon, a
// throwable and a kit in one go. What happened is shown inside it, because a
// modal dialog covers the toasts.
function openGive(target) {
  state.giveTarget = target;
  $("#give-title").textContent = `Give to ${target.name}`;
  $("#give-search").value = "";
  setGiveStatus("Pick an item. You can give several.");
  renderGiveItems();
  $("#give").showModal();
  $("#give-search").focus();
}

function setGiveStatus(text, tone = "") {
  const status = $("#give-status");
  status.textContent = text;
  status.className = "give-status small " + (tone || "muted");
}

function renderGiveItems() {
  const query = $("#give-search").value.trim().toLowerCase();
  const groups = [];
  for (const [title, items] of GIVE_ITEMS) {
    const shown = items.filter(([id, label]) => !query || label.toLowerCase().includes(query) || id.includes(query));
    if (!shown.length) continue;
    groups.push(h("div", { class: "give-group" }, h("h3", {}, title),
      h("div", { class: "give-grid" }, ...shown.map(([id, label]) =>
        h("button", { class: "btn", type: "button", onclick: (event) => giveItem(id, label, event.currentTarget) }, label)))));
  }
  $("#give-items").replaceChildren(...(groups.length ? groups : [h("p", { class: "empty" }, "No item matches that.")]));
}

async function giveItem(id, label, button) {
  const target = state.giveTarget;
  if (!target) return;
  setGiveStatus(`Giving ${label}…`);
  try {
    await busy(button, api("/api/survivor", { action: "give", target: target.userid, item: id }));
    setGiveStatus(`✓ ${label} given to ${target.name}.`, "ok");
    refresh();
  } catch (error) {
    setGiveStatus(error.message, "bad");
  }
}

function renderBanners(o) {
  const down = o && o.server.running && !o.game.reachable ? o.game.error || "RCON is unavailable." : "";
  const restart = o && o.server.running ? o.server.restart_needed : [];
  if (!changed("banners", [state.offline, !!o, down, restart])) return;
  const banners = [];
  if (state.offline) {
    banners.push(h("div", { class: "banner bad", role: "alert" },
      h("span", {}, `${state.offline} ${o ? "This is the last state it reported; retrying…" : "Retrying…"}`)));
  }
  if (down) banners.push(h("div", { class: "banner bad", role: "alert" }, h("span", {}, down)));
  if (restart.length) {
    banners.push(h("div", { class: "banner" },
      h("span", {}, `Restart to apply: ${restart.join("; ")}.`),
      h("button", { class: "btn small", type: "button", "data-k": "restart", onclick: (event) => control("restart", event.currentTarget) }, "Restart now")));
  }
  redraw($("#banners"), ...banners);
}

async function kick(player) {
  const yes = await confirmDialog({ title: `Kick ${player.name}?`, text: "They can rejoin unless you change the password.", ok: "Kick", danger: true });
  if (yes) act(() => api("/api/kick", { userid: player.userid }));
}

async function control(action, button) {
  const copy = {
    start: null,
    stop: { title: "Stop the server?", ok: "Stop" },
    restart: { title: "Restart the server?", ok: "Restart" },
    update: { title: "Update game files?", ok: "Update", extra: " The server is down while Steam downloads the update." },
  }[action];
  if (copy) {
    const playing = !!(state.overview && state.overview.players.length);
    const yes = await confirmDialog({ title: copy.title, text: whoIsOn() + (copy.extra || ""), ok: copy.ok, danger: action === "stop" || playing });
    if (!yes) return;
  }
  act(() => api("/api/control", { action }), null, button);
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
  if (!state.maps) $("#campaigns").replaceChildren(h("div", { class: "skeleton empty", "aria-busy": "true", "aria-label": "Loading campaigns" }, h("i"), h("i"), h("i")));
  try {
    state.maps = await api("/api/maps");
    renderMaps();
  } catch (error) {
    toast(error.message, true);
    if (!state.maps) $("#campaigns").replaceChildren(h("p", { class: "empty" }, "Couldn't load the campaigns. ",
      h("button", { class: "btn small", type: "button", onclick: loadMaps }, "Try again")));
  }
}

// Name of the campaign a map belongs to, with the chapter when it isn't the first.
function mapTitle(map) {
  if (!map || !state.maps) return map || "";
  const name = map.toLowerCase();
  for (const c of [...state.maps.custom, ...state.maps.official]) {
    const chapters = (c.maps || []).map((m) => m.toLowerCase()), at = chapters.indexOf(name);
    if (at > 0) return `${c.name}, chapter ${at + 1}`;
    if (at === 0 || (c.first_map || c.map || "").toLowerCase() === name) return c.name;
  }
  return map;
}

function renderMaps() {
  const maps = state.maps, o = state.overview;
  const playing = playingMap();
  const up = !!(o && o.game.reachable);
  const live = up ? String(o.game.map || "").toLowerCase() : "";
  $("#maps-current").textContent = (up && (mapTitle(o.game.map) || o.game.campaign)) || "–";
  $("#maps-default").textContent = mapTitle(maps.default) || "–";

  const query = state.mapQuery.trim().toLowerCase();
  if (!changed("maps", [maps, playing, up, live, state.mapFilter, query])) return;

  const cards = [];
  const card = (c, first, custom) => {
    const isCurrent = playing !== "" && playing === first.toLowerCase();
    const chapters = c.maps && c.maps.length ? c.maps : [first];
    const at = chapters.findIndex((m) => m.toLowerCase() === live);
    const starts = chapters.some((m) => m.toLowerCase() === String(maps.default).toLowerCase()) || first === maps.default;
    const button = (label, name, kind, onclick) => h("button", { class: "btn small " + kind, type: "button", disabled: !up, "data-k": `${first}-${name}`,
      "aria-label": `${label.replace("…", "")}: ${c.name}`, title: up ? null : "The server isn't running", onclick }, label);
    return h("div", { class: "campaign" + (isCurrent ? " current" : "") },
      h("div", {}, h("span", { class: "title" }, c.name),
        custom && h("span", { class: "tag" }, "custom"),
        isCurrent && h("span", { class: "tag on" }, "playing"),
        starts && h("span", { class: "tag" }, "starts here")),
      h("div", { class: "mapname" }, isCurrent && at >= 0 && chapters.length > 1 ? `chapter ${at + 1} of ${chapters.length} · ${chapters[at]}`
        : chapters.length > 1 ? `${chapters.length} chapters · ${first}` : first),
      h("div", { class: "row tight wrap" },
        isCurrent && live && button("Restart chapter", "restart", "", (event) => restartChapter(o.game.map, c.name, event.currentTarget)),
        button(isCurrent ? "Play from…" : "Play…", "play", isCurrent ? "quiet" : "", () => changeMap(c, first, chapters, custom))));
  };
  const playable = maps.custom.filter((c) => c.first_map && !c.missing);
  const counts = { all: playable.length + maps.official.length, custom: playable.length, official: maps.official.length };
  for (const button of $("#map-filters").querySelectorAll("button")) {
    button.setAttribute("aria-pressed", String(button.dataset.filter === state.mapFilter));
    button.querySelector(".count").textContent = counts[button.dataset.filter];
  }
  const matches = (name, map) => !query || name.toLowerCase().includes(query) || map.toLowerCase().includes(query);
  if (state.mapFilter !== "official") for (const c of playable) if (matches(c.name, c.first_map)) cards.push(card(c, c.first_map, true));
  if (state.mapFilter !== "custom") for (const m of maps.official) if (matches(m.name, m.map)) cards.push(card(m, m.map, false));
  // Typing in the search box must not rebuild the box being typed in, only the cards.
  if (cards.length) redraw($("#campaigns"), ...cards);
  else if (query) redraw($("#campaigns"), h("p", { class: "empty" }, `No campaign matches "${state.mapQuery.trim()}".`));
  else redraw($("#campaigns"), h("p", { class: "empty" }, "No custom campaigns installed yet. Add one below."));

  const list = $("#custom-maps");
  if (!maps.custom.length) {
    redraw(list, h("p", { class: "empty" }, "No custom maps installed yet."));
  } else {
    redraw(list, h("div", { class: "table-wrap" }, h("table", {}, h("tbody", {}, maps.custom.map((c) => h("tr", {},
      h("td", { class: "name" }, c.name,
        c.missing && h("span", { class: "tag bad" }, "file missing"),
        !c.first_map && h("span", { class: "tag" }, "addon")),
      h("td", { class: "id" }, c.first_map || ""),
      h("td", { class: "num" }, h("button", { class: "btn small danger", type: "button", "data-k": `remove-${c.key}`, "aria-label": `Remove ${c.name}`, onclick: () => removeMap(c) }, "Remove"))))))));
  }

  const links = maps.custom.filter((c) => c.url).map((c) => `${c.name}\n${c.url}`);
  const files = maps.custom.filter((c) => !c.url).map((c) => c.file);
  let share = links.length ? links.join("\n\n") : "No Workshop maps installed - friends don't need anything extra.";
  if (files.length) share += `\n\nInstalled from files (send friends the same file):\n${files.join("\n")}`;
  $("#share").textContent = share;
}

async function changeMap(campaign, first, chapters, custom) {
  const answer = await confirmDialog({
    title: `Play ${campaign.name}?`,
    text: "Everyone in the game moves to the new map." + (custom ? " Anyone who doesn't have this custom map is disconnected." : ""),
    ok: "Switch map",
    choose: chapters.length > 1 && { label: "Start at", value: first,
      options: chapters.map((m, i) => [m, `Chapter ${i + 1} · ${m}`]) },
    check: "Also start here after every restart",
  });
  if (!answer) return;
  await act(() => api("/api/map", { name: answer.choice || first, default: answer.checked }));
  loadMaps();
}

async function restartChapter(map, name, button) {
  const yes = await confirmDialog({ title: "Restart this chapter?", text: `${name} starts over from ${map}. Everyone goes back to its beginning.`, ok: "Restart chapter" });
  if (yes) await act(() => api("/api/map", { name: map, default: false }), "Restarting the chapter…", button);
}

async function removeMap(item) {
  const yes = await confirmDialog({ title: `Remove ${item.name}?`, text: "The map file is deleted from the server.", ok: "Remove", danger: true });
  if (yes) act(() => api("/api/maps/remove", { key: item.key }));
}

// ---------------------------------------------------------------- settings
function fillSelect(select, options, value) {
  // A current value that isn't among the choices still gets an option: a
  // select with no matching option would silently submit "" instead.
  if (value !== "" && value != null && !options.some(([v]) => v === value)) options = [[value, value], ...options];
  select.replaceChildren(...options.map(([v, label]) => h("option", { value: v }, label)));
  select.value = value;
}

// Loads what is saved without losing what is being typed: a field that was
// edited keeps its edit (unless everything is to be discarded), the rest update.
async function loadSettings(discard = false) {
  try {
    const [s, maps] = await Promise.all([api("/api/settings"), api("/api/maps")]);
    state.maps = maps;
    const form = $("#settings-form"), before = discard ? null : state.settingsSaved, typed = settingsValues();
    const value = (name) => (before && typed[name] !== before[name] ? typed[name] : s[name]);
    form.hostname.value = value("hostname");
    form.password.value = value("password");
    fillSelect(form.difficulty, s.choices.difficulties.map((d) => [d, s.labels.difficulties[d] || d]), value("difficulty"));
    fillSelect(form.mode, s.choices.modes.map((m) => [m, s.labels.modes[m] || m]), value("mode"));
    const mapOptions = [
      ...maps.custom.filter((c) => c.first_map && !c.missing).map((c) => [c.first_map, `${c.name} (custom)`]),
      ...maps.official.map((m) => [m.map, m.name]),
    ];
    const startMap = value("default_map");
    if (startMap && !mapOptions.some(([map]) => map === startMap)) mapOptions.unshift([startMap, mapTitle(startMap)]);
    fillSelect(form.default_map, mapOptions, startMap);
    markPlaying();
    const slots = $("#slots"), picked = !discard && state.slotsSaved != null && slots.value !== state.slotsSaved ? slots.value : String(s.slots);
    fillSelect(slots, s.choices.slots.map((n) => [String(n), n === 4 ? "4 (normal, no mods)" : `${n} players`]), picked);
    state.slotsSaved = String(s.slots);
    state.settingsNote = s.rcon_enabled ? "" : "RCON is off: changes apply after a restart.";
    state.settingsSaved = { hostname: s.hostname, password: s.password, difficulty: s.difficulty, mode: s.mode, default_map: s.default_map };
    markSettingsDirty();
  } catch (error) { toast(error.message, true); }
}

function settingsValues() {
  const form = $("#settings-form");
  return {
    hostname: form.hostname.value,
    password: form.password.value,
    difficulty: form.difficulty.value,
    mode: form.mode.value,
    default_map: form.default_map.value,
  };
}

// Save is only offered when there is something to save, and says so.
const settingsDirty = () => state.settingsSaved != null && JSON.stringify(settingsValues()) !== JSON.stringify(state.settingsSaved);
const rawDirty = () => state.rawSaved != null && $("#raw-text").value !== state.rawSaved;

function markSettingsDirty() {
  const dirty = settingsDirty();
  $("#settings-unsaved").hidden = !dirty && !rawDirty();
  // The keyboard focus moves on first: a disabled button can't keep it.
  if (!dirty && document.activeElement === $("#btn-settings-save")) $("#settings-note").focus();
  $("#btn-settings-save").disabled = !dirty;
  $("#btn-settings-reset").hidden = !dirty;
  $("#settings-note").textContent = dirty ? "Unsaved changes" : state.settingsNote || "";
}

async function saveSettings(event) {
  event.preventDefault();
  // Only what was edited: the other fields may have been changed elsewhere since.
  const values = settingsValues(), sent = {};
  for (const name in values) if (values[name] !== state.settingsSaved[name]) sent[name] = values[name];
  const result = await act(() => api("/api/settings", sent), null, $("#btn-settings-save"));
  if (!result) return markSettingsDirty();
  // What was sent is now the saved state (the server may tidy it up: the reload
  // shows that), while anything typed since then still counts as an edit.
  state.settingsSaved = { ...state.settingsSaved, ...sent };
  markSettingsDirty();
  if (!result.changed.length) toast("Nothing to change.");
  else if (result.live_failed.length) toast(`Saved. Not applied to the running server yet: ${result.live_failed.join(", ")}. Restart to apply.`, true);
  else toast(`Saved: ${result.changed.join(", ")}.` + (result.restart_needed ? " Restart to apply the game mode." : ""));
  loadSettings();
  refreshRawConfig();
}

// Nothing can be typed or saved until the file was really read: saving a box
// that failed to load would empty server.cfg.
async function loadRawConfig() {
  const box = $("#raw-text"), seq = state.rawSeq = (state.rawSeq || 0) + 1;
  box.readOnly = true;
  if (document.activeElement === $("#btn-raw-save")) box.focus();
  $("#btn-raw-save").disabled = true;
  try {
    const raw = await api("/api/config");
    if (seq !== state.rawSeq) return;
    box.value = raw.text;
    // Read back: a text box normalises line endings, which is not an edit.
    state.rawSaved = box.value;
    state.rawVersion = raw.version;
    $("#raw-path").textContent = raw.path;
  } catch (error) { toast(error.message, true); }
  if (seq !== state.rawSeq) return;
  box.readOnly = $("#btn-raw-save").disabled = state.rawSaved == null;
  markSettingsDirty();
}

// The open editor follows changes made elsewhere, as long as nothing was typed.
function refreshRawConfig() {
  if ($("#raw-config").open && !rawDirty()) loadRawConfig();
}

async function reloadRawConfig() {
  if (rawDirty() && !await confirmDialog({ title: "Discard your edits?", text: "server.cfg is read again and what you typed is lost.", ok: "Discard", danger: true })) return;
  loadRawConfig();
}

async function saveRawConfig() {
  const yes = await confirmDialog({ title: "Replace server.cfg?", text: "The previous version is kept as a backup next to the file.", ok: "Save" });
  if (!yes) return;
  const text = $("#raw-text").value;
  const result = await act(() => api("/api/config", { text, apply: $("#raw-apply").checked, base: state.rawVersion }), null, $("#btn-raw-save"));
  if (!result) return;
  state.rawSaved = text;
  state.rawVersion = result.version;
  toast(result.applied ? "Saved and applied." : "Saved.");
  for (const warning of result.warnings) toast(warning, true);
  loadSettings();
  refreshRawConfig();
}

// ----------------------------------------------------------------- console
async function runRcon(event) {
  event.preventDefault();
  const input = $("#rcon-cmd"), out = $("#rcon-out");
  const command = input.value.trim();
  if (!command || $("#rcon-form button").getAttribute("aria-busy") === "true") return;
  if (state.history[state.history.length - 1] !== command) state.history.push(command);
  state.history = state.history.slice(-50);
  state.historyAt = state.history.length;
  input.value = "";
  let text;
  try {
    text = (await busy($("#rcon-form button"), api("/api/rcon", { command }))).output || "(no output)";
  } catch (error) { text = `! ${error.message}`; }
  out.textContent = (out.textContent + `\n] ${command}\n${text}\n`).slice(-20000).trimStart();
  out.scrollTop = out.scrollHeight;
  refresh();
}

async function loadLogs() {
  try {
    state.logLines = (await api("/api/logs?lines=300")).lines;
    state.logError = "";
  } catch (error) { state.logError = error.message; }
  renderLogs();
}

function renderLogs() {
  const out = $("#log-out");
  const query = $("#log-search").value.trim().toLowerCase(), only = $("#log-errors").checked;
  const problem = (line) => PROBLEM_RE.test(line);
  const lines = state.logLines.filter((line) => (!query || line.toLowerCase().includes(query)) && (!only || problem(line)));
  if (!changed("log", [state.logError, lines, query, only])) return;
  // A fresh or filtered log starts at its newest line; otherwise only someone
  // already at the end is kept there.
  const filter = JSON.stringify([query, only]);
  const atEnd = state.logFilter !== filter || out.scrollHeight - out.scrollTop - out.clientHeight < 40;
  state.logFilter = filter;
  if (state.logError) out.replaceChildren(state.logError);
  else if (!lines.length) out.replaceChildren(state.logLines.length ? "No line matches." : "(log is empty)");
  else out.replaceChildren(...lines.map((line) => h("span", { class: "line" + (problem(line) ? " problem" : "") }, line + "\n")));
  if (atEnd) out.scrollTop = out.scrollHeight;
}

// --------------------------------------------------------------------- job
async function pollJob(show = false) {
  clearTimeout(state.timers.job);
  let job;
  try { job = (await api("/api/job")).job; } catch {
    // One failed look must not leave the card spinning for good.
    if (!$("#app").hidden) state.timers.job = setTimeout(pollJob, POLL_MS);
    return;
  }
  if (!job) {      // the panel was restarted and no longer knows about it
    $("#job").hidden = true;
    return;
  }
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
    if (state.tab === "settings") { loadSettings(); refreshRawConfig(); }
  }
}

// -------------------------------------------------------------------- shell
async function refresh() {
  clearTimeout(state.timers.poll);
  const seq = ++state.seq;
  let overview = null;
  try {
    overview = await api("/api/overview");
  } catch (error) {
    // api() already switched to the login screen if that was the cause. Anything
    // else is the panel being unreachable: say so rather than show stale numbers as live.
    if (seq === state.seq && !$("#app").hidden) {
      state.offline = error.message;
      renderBanners(state.overview);
    }
  }
  // A newer refresh is under way (an action was just taken): this answer is
  // from before it and would flip things back for a moment.
  if (seq !== state.seq) return;
  if (!$("#app").hidden) state.timers.poll = setTimeout(refresh, document.hidden ? POLL_MS * 5 : POLL_MS);
  if (!overview) return;
  state.overview = overview;
  state.offline = "";
  renderOverview(overview);
}

function showTab(name, focus = false) {
  if (!TABS.includes(name)) name = "overview";
  state.tab = name;
  for (const button of document.querySelectorAll(".tabs button")) {
    const selected = button.dataset.tab === name;
    button.setAttribute("aria-selected", String(selected));
    button.tabIndex = selected ? 0 : -1;       // one tab stop; arrows move between tabs
    if (selected && focus) button.focus();
  }
  for (const section of document.querySelectorAll(".tab")) section.hidden = section.id !== `tab-${name}`;
  // The tab is kept in the address, so a reload or the back button lands where you were.
  if (location.hash.slice(1) !== name) history.replaceState(null, "", name === "overview" ? location.pathname : `#${name}`);
  clearInterval(state.timers.logs);
  if (name === "maps") loadMaps();
  if (name === "settings") { loadSettings(); refreshRawConfig(); }
  if (name === "console") {
    loadLogs();
    state.timers.logs = setInterval(() => { if ($("#log-follow").checked && !document.hidden) loadLogs(); }, 5000);
  }
}

function showLogin() {
  clearTimeout(state.timers.poll);
  clearTimeout(state.timers.job);
  clearInterval(state.timers.logs);
  // An open dialog would sit on top of the sign-in form and keep the focus.
  $("#confirm").returnValue = "";
  for (const dialog of [$("#confirm"), $("#give")]) if (dialog.open) dialog.close();
  $("#app").hidden = true;
  $("#login").hidden = false;
  $("#login-password").focus();
}

function showApp() {
  $("#login").hidden = true;
  $("#app").hidden = false;
  for (const old of [...$("#toasts").children]) if (old.dataset.message === SIGNED_OUT) old.remove();
  showTab(location.hash.slice(1));
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
      await busy(event.submitter, api("/api/login", { password: input.value }));
      input.value = "";
      showApp();
    } catch (error) {
      $("#login-error").textContent = error.message;
      input.select();
    }
  });
  $("#btn-logout").addEventListener("click", async () => {
    if ((settingsDirty() || rawDirty()) && !await confirmDialog({ title: "Sign out?", text: "Settings you changed are not saved yet and will be lost.", ok: "Sign out", danger: true })) return;
    state.settingsSaved = state.rawSaved = state.slotsSaved = null;
    try { await api("/api/logout", {}); } catch { /* signing out regardless */ }
    location.reload();      // leaves nothing (passwords, server.cfg) behind in the page
  });
  for (const button of document.querySelectorAll("[data-control]")) {
    button.addEventListener("click", () => control(button.dataset.control, button));
  }
  for (const button of document.querySelectorAll(".tabs button")) {
    button.addEventListener("click", () => showTab(button.dataset.tab));
  }
  $(".tabs").addEventListener("keydown", (event) => {
    const at = TABS.indexOf(state.tab);
    const to = { ArrowRight: at + 1, ArrowLeft: at - 1, Home: 0, End: TABS.length - 1 }[event.key];
    if (to === undefined) return;
    event.preventDefault();
    showTab(TABS[(to + TABS.length) % TABS.length], true);
  });
  window.addEventListener("hashchange", () => { if (!$("#app").hidden) showTab(location.hash.slice(1)); });
  $("#map-search").addEventListener("input", (event) => {
    state.mapQuery = event.target.value;
    if (state.maps) renderMaps();
  });
  for (const button of $("#map-filters").querySelectorAll("button")) {
    button.addEventListener("click", () => {
      state.mapFilter = button.dataset.filter;
      if (state.maps) renderMaps();
    });
  }
  $("#say-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const input = $("#say-text"), text = input.value;
    // What was typed while it was being sent stays.
    if (text.trim() && await act(() => api("/api/say", { text }), "Message sent.", event.submitter) && input.value === text) input.value = "";
  });
  $("#addmap-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const input = $("#addmap-links");
    const links = input.value;
    if (!links.trim()) return input.focus();
    if (await act(() => api("/api/maps/add", { links }), null, event.submitter) && input.value === links) input.value = "";
  });
  $("#btn-update-maps").addEventListener("click", (event) => act(() => api("/api/maps/update", {}), null, event.currentTarget));
  $("#btn-copy-join").addEventListener("click", () => copyFrom("#join"));
  $("#btn-copy-share").addEventListener("click", () => copyFrom("#share"));

  $("#settings-form").addEventListener("submit", saveSettings);
  $("#settings-form").addEventListener("input", markSettingsDirty);
  $("#btn-settings-reset").addEventListener("click", () => loadSettings(true));
  $("#raw-text").addEventListener("input", markSettingsDirty);
  window.addEventListener("beforeunload", (event) => { if (settingsDirty() || rawDirty()) event.preventDefault(); });
  $("#rcon-cmd").addEventListener("keydown", (event) => {
    if (event.key !== "ArrowUp" && event.key !== "ArrowDown") return;
    if (!state.history.length) return;
    event.preventDefault();
    state.historyAt = Math.max(0, Math.min(state.history.length, state.historyAt + (event.key === "ArrowUp" ? -1 : 1)));
    event.target.value = state.history[state.historyAt] || "";
  });
  $("#settings-form").default_map.addEventListener("change", markPlaying);
  $("#btn-show-password").addEventListener("click", (event) => {
    const input = $("#settings-form").password;
    input.type = input.type === "password" ? "text" : "password";
    event.target.textContent = input.type === "password" ? "Show" : "Hide";
  });
  buildSwitches();
  // Remembers what the pointer is pressing, so a redraw can't swallow the click.
  document.addEventListener("pointerdown", (event) => { state.holding = event.button === 0 ? event.target : null; });
  for (const end of ["pointerup", "pointercancel", "contextmenu", "dragend"]) document.addEventListener(end, () => setTimeout(() => { state.holding = null; }));
  window.addEventListener("blur", () => { state.holding = null; });
  // A button that is still working ignores further presses (Enter in a form included).
  document.addEventListener("click", (event) => {
    if (event.target.closest && event.target.closest('button[aria-busy="true"]')) { event.preventDefault(); event.stopImmediatePropagation(); }
  }, true);
  $("#confirm").addEventListener("click", (event) => { if (event.target === $("#confirm")) $("#confirm").close(); });
  for (const button of $("#team-actions").querySelectorAll("button")) {
    button.addEventListener("click", () => {
      if (button.dataset.team === "give") openGive(EVERYONE);
      else survivorAction(button.dataset.team, EVERYONE, button);
    });
  }
  $("#give-close").addEventListener("click", () => $("#give").close());
  $("#give-search").addEventListener("input", renderGiveItems);
  // A click on the dimmed backdrop lands on the dialog element itself.
  $("#give").addEventListener("click", (event) => { if (event.target === $("#give")) $("#give").close(); });
  $("#team-size").addEventListener("change", async (event) => {
    const size = Number(event.target.value), turn = state.teamBusy = {};
    // The arrow keys pass through every size on the way: wait for the last one.
    await new Promise((resolve) => setTimeout(resolve, 600));
    if (state.teamBusy !== turn) return;
    await act(() => api("/api/survivors", { size }));
    if (state.teamBusy !== turn) return;
    state.teamBusy = null;
    delete state.drawn.team;      // the next refresh shows what the server really has
    refresh();
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
  $("#btn-update-game").addEventListener("click", (event) => control("update", event.currentTarget));
  $("#raw-config").addEventListener("toggle", (event) => { if (event.target.open && !rawDirty()) loadRawConfig(); });
  $("#btn-raw-reload").addEventListener("click", reloadRawConfig);
  $("#btn-raw-save").addEventListener("click", saveRawConfig);

  $("#rcon-form").addEventListener("submit", runRcon);
  $("#rcon-quick").replaceChildren(...QUICK_COMMANDS.map((command) => h("button", { class: "chip plain", type: "button", onclick: () => {
    $("#rcon-cmd").value = command;
    $("#rcon-form").requestSubmit();
  } }, command)));
  $("#btn-log-refresh").addEventListener("click", loadLogs);
  $("#log-search").addEventListener("input", renderLogs);
  $("#log-errors").addEventListener("change", renderLogs);
  $("#btn-job-close").addEventListener("click", () => { state.jobHidden = state.jobSeen; $("#job").hidden = true; });
  document.addEventListener("visibilitychange", () => { if (!document.hidden && !$("#app").hidden) refresh(); });
}

wire();
fetch("/api/session", { credentials: "same-origin" })
  .then((response) => response.json())
  .then((session) => (session.authenticated ? showApp() : showLogin()))
  .catch(showLogin);
