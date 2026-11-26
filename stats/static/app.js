"use strict";
// Public stats site. Everything from the server is shown with textContent:
// player names come from the game and from Steam, so they are never HTML.

const RESULTS = {
  won: ["Finale won", "good"], cleared: ["Cleared", "good"], lost: ["Wiped", "bad"],
  playing: ["Playing now", "warn"], restarted: ["Restarted", ""], changed: ["Map changed", ""],
  ended: ["Ended", ""], unfinished: ["Unfinished", ""],
};
const SORTS = [
  ["points", "Points"], ["per_hour", "Points per hour"], ["kills", "Kills"], ["specials", "Special kills"],
  ["tank_damage", "Tank damage"], ["teamwork", "Teamwork"], ["survived", "Chapters survived"],
];
const PERIODS = [["all", "All time"], ["month", "30 days"], ["week", "7 days"]];
const SPECIAL_NAMES = [["smokers", "Smokers"], ["boomers", "Boomers"], ["hunters", "Hunters"],
  ["spitters", "Spitters"], ["jockeys", "Jockeys"], ["chargers", "Chargers"], ["tanks", "Tanks"], ["witches", "Witches"]];

const $ = (id) => document.getElementById(id);
let view = 0;            // bumps on every navigation, so a slow answer can't paint over a newer page
let siteTitle = "";

// ------------------------------------------------------------------ helpers
function el(tag, props, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props || {})) {
    if (value === undefined || value === null || value === false) continue;
    if (key === "class") node.className = value;
    else if (key === "text") node.textContent = value;
    else if (key === "style") node.style.cssText = value;   // CSSOM: allowed by the CSP, unlike a style attribute
    else if (key === "on") for (const [ev, fn] of Object.entries(value)) node.addEventListener(ev, fn);
    else node.setAttribute(key, value === true ? "" : value);
  }
  for (const child of children.flat()) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

const number = (n) => (n ?? 0).toLocaleString();
const hours = (seconds) => `${((seconds || 0) / 3600).toFixed(1)} h`;

function duration(seconds) {
  if (seconds === null || seconds === undefined) return "–";
  const h = Math.floor(seconds / 3600), m = Math.floor((seconds % 3600) / 60), s = seconds % 60;
  return h ? `${h} h ${String(m).padStart(2, "0")} m` : `${m}:${String(s).padStart(2, "0")}`;
}

function ago(ts) {
  if (!ts) return "never";
  const s = Math.max(0, Date.now() / 1000 - ts);
  if (s < 90) return "just now";
  if (s < 3600) return `${Math.round(s / 60)} min ago`;
  if (s < 86400 * 1.5) return `${Math.round(s / 3600)} h ago`;
  if (s < 86400 * 45) return `${Math.round(s / 86400)} days ago`;
  return new Date(ts * 1000).toLocaleDateString();
}

const fullDate = (ts) => (ts ? new Date(ts * 1000).toLocaleString() : "");

function mapLabel(m) {
  const text = m.chapter ? `${m.campaign} · ${m.chapter}` : m.campaign;
  return el("span", { title: m.map, text });
}

function resultTag(result) {
  const [label, tone] = RESULTS[result] || [result, ""];
  return el("span", { class: `tag ${tone}`, text: label });
}

function avatar(p, big) {
  const cls = big ? "avatar big" : "avatar";
  if (p.avatar) return el("img", { class: cls, src: p.avatar, alt: "", loading: "lazy", referrerpolicy: "no-referrer" });
  return el("span", { class: cls, "aria-hidden": "true", text: (p.name || "?").trim().charAt(0).toUpperCase() || "?" });
}

function playerLink(p) {
  return el("a", { class: "player", href: `#/player/${p.steamid}` }, avatar(p), el("span", { class: "name", text: p.name }));
}

function card(title, ...body) {
  return el("section", { class: "card" }, title ? el("div", { class: "card-head" }, el("h2", { text: title })) : null, ...body);
}

function tile(label, value, sub) {
  return el("div", { class: "tile" }, el("span", { class: "label", text: label }), el("strong", { text: value }),
    sub ? el("span", { class: "sub", text: sub }) : null);
}

async function api(path, params) {
  const query = new URLSearchParams(params || {}).toString();
  const reply = await fetch(query ? `${path}?${query}` : path, { headers: { Accept: "application/json" } });
  let data = null;
  try { data = await reply.json(); } catch { /* not JSON */ }
  if (!reply.ok) {
    const error = new Error((data && data.error) || `The site answered ${reply.status}.`);
    error.status = reply.status;
    throw error;
  }
  return data;
}

function parseHash() {
  const raw = location.hash.replace(/^#/, "") || "/";
  const [path, query] = raw.split("?");
  return { parts: path.split("/").filter(Boolean), params: new URLSearchParams(query || "") };
}

function go(path, params) {
  const query = new URLSearchParams(params).toString();
  location.hash = query ? `${path}?${query}` : path;
}

function paint(token, ...nodes) {
  if (token !== view) return false;
  $("loading").hidden = true;
  $("view").replaceChildren(...nodes);
  return true;
}

function failure(token, error) {
  paint(token, el("section", { class: "card error-box" },
    el("p", { text: error.status === 404 ? "There's nothing here. The link may be old." : error.message }),
    el("p", {}, el("a", { href: "#/", text: "Back to the leaderboard" }))));
}

function notReady() {
  return card("No games recorded yet",
    el("p", { class: "muted", text: "Stats appear here after the first chapter played on the server." }));
}

// ------------------------------------------------------------------ header
function renderLive(summary) {
  if (summary.title && summary.title !== siteTitle) {
    siteTitle = summary.title;
    $("title").textContent = siteTitle;
  }
  const live = summary.live || {};
  const box = $("live");
  box.classList.toggle("on", !!live.online);
  if (!summary.ready) $("live-text").textContent = "No stats yet";
  // An empty server hibernates and stops reporting, so this can't tell "empty" from "down".
  else if (!live.online) $("live-text").textContent = "Nobody playing";
  else {
    const where = live.chapter ? `${live.campaign} ${live.chapter}` : live.campaign;
    const count = live.slots ? `${live.count}/${live.slots}` : `${live.count}`;
    $("live-text").textContent = `${count} playing · ${where}`;
  }
  $("foot-note").textContent = summary.steam ? "Avatars and Steam hours come from Steam profiles that are public." : "";
}

async function refreshLive() {
  try {
    renderLive(await api("/api/summary"));
  } catch { /* the page itself shows errors */ }
}

function setDocTitle(part) {
  document.title = part ? `${part} · ${siteTitle || "Server stats"}` : siteTitle || "Server stats";
}

// ---------------------------------------------------------------- leaderboard
function nowPlaying(live) {
  if (!live || !live.online) {
    return card("Now playing", el("p", { class: "muted", text: live && live.updated
      ? `Nobody is playing right now. Last activity ${ago(live.updated)}.` : "Nobody is playing right now." }));
  }
  const list = live.players.length
    ? el("ul", { class: "list" }, live.players.map((p) => el("li", {}, el("div", { class: "line" },
      playerLink(p), el("span", { class: "faint small", text: `since ${ago(p.since)}` })))))
    : el("p", { class: "muted", text: "Nobody is on right now." });
  return card("Now playing",
    el("p", {}, el("b", {}, mapLabel(live)), el("span", { class: "muted", text: live.difficulty ? ` · ${live.difficulty}` : "" })),
    live.links ? subscribeLinks(live.links, "Get this map") : null,
    list);
}

function recentGames(recent) {
  if (!recent.length) return card("Recent games", el("p", { class: "muted", text: "None yet." }));
  return card("Recent games", el("ul", { class: "list" }, recent.map((r) => el("li", {},
    el("div", { class: "line" }, el("b", {}, mapLabel(r)), resultTag(r.result)),
    el("div", { class: "line small muted" },
      el("span", { text: [r.difficulty, r.duration !== null ? duration(r.duration) : null, r.boosted ? "boosted" : null]
        .filter(Boolean).join(" · ") }),
      el("span", { title: fullDate(r.ended), text: ago(r.ended) })),
    r.players.length ? el("div", { class: "people", text: r.players.join(", ") }) : null))));
}

function boardTable(data) {
  const sort = data.sort;
  const cols = [
    ["points", "Points", (p) => number(p.points), ""],
    ["per_hour", "Per hour", (p) => (p.per_hour === null ? "–" : number(p.per_hour)), "hide-sm"],
    ["playtime", "Hours", (p) => hours(p.playtime), "hide-sm"],
    ["kills", "Kills", (p) => number(p.kills), ""],
    ["specials", "Specials", (p) => number(p.specials), "hide-sm"],
    ["tank_damage", "Tank dmg", (p) => number(p.tank_damage), "hide-sm"],
    ["teamwork", "Teamwork", (p) => number(p.teamwork), "hide-sm"],
    ["survived", "Survived", (p) => number(p.survived), "hide-sm"],
    ["deaths", "Deaths", (p) => number(p.deaths), "hide-sm"],
  ];
  const cls = (key, base) => [base, "n", key === sort ? "sorted" : "", key === sort ? "" : cols.find((c) => c[0] === key)[3]]
    .filter(Boolean).join(" ");
  return el("div", { class: "table-wrap" }, el("table", {},
    el("thead", {}, el("tr", {}, el("th", { scope: "col", text: "#" }), el("th", { scope: "col", text: "Player" }),
      cols.map(([key, label]) => el("th", { scope: "col", class: cls(key), text: label })))),
    el("tbody", {}, data.players.map((p) => el("tr", {},
      el("td", {}, el("span", { class: `rank${p.rank <= 3 ? ` r${p.rank}` : ""}`, text: p.rank })),
      el("td", { class: "who" }, playerLink(p)),
      cols.map(([key, , value]) => el("td", { class: cls(key) }, value(p))))))));
}

async function showBoard(token, params) {
  const period = PERIODS.some(([k]) => k === params.get("period")) ? params.get("period") : "all";
  const sort = SORTS.some(([k]) => k === params.get("sort")) ? params.get("sort") : "points";
  const scope = params.get("scope") === "all" ? "all" : "ranked";
  const [summary, board] = await Promise.all([api("/api/summary"), api("/api/leaderboard", { period, sort, scope })]);
  renderLive(summary);
  setDocTitle("");
  if (!summary.ready) {
    return paint(token, notReady(), summary.addons > 0 ? el("p", { class: "callout" },
      "Joining us? Subscribe to the custom maps first. ", el("a", { href: "#/maps?get=1", text: "Get the maps →" })) : null);
  }
  const change = (next) => go("/", { period, sort, scope, ...next });

  const controls = el("div", { class: "controls" },
    el("div", { class: "seg", role: "group", "aria-label": "Period" }, PERIODS.map(([key, label]) =>
      el("button", { type: "button", "aria-pressed": String(key === period), text: label,
        on: { click: () => change({ period: key }) } }))),
    el("label", { class: "pick" }, "Rank by",
      el("select", { on: { change: (e) => change({ sort: e.target.value }) } },
        SORTS.map(([key, label]) => el("option", { value: key, selected: key === sort, text: label })))),
    el("label", { class: "check" },
      el("input", { type: "checkbox", checked: scope === "all", on: { change: (e) => change({ scope: e.target.checked ? "all" : "ranked" }) } }),
      "Include boosted games"));

  const t = summary.totals;
  const tiles = el("div", { class: "tiles" },
    tile("Players", number(t.players)),
    tile("Hours played", number(Math.round(t.playtime / 3600))),
    tile("Zombies killed", number(t.commons + t.specials), `${number(t.specials)} special`),
    tile("Tanks finished", number(t.tanks), `${number(t.witches)} witches`),
    tile("Chapters cleared", number(t.chapters), `${number(t.campaigns)} finales won`));

  const empty = board.players.length === 0;
  const boardCard = el("section", { class: "card" },
    el("div", { class: "card-head" }, el("h2", { text: "Leaderboard" }),
      el("span", { class: "muted small", text: board.total ? `${number(board.total)} players` : "" })),
    controls,
    empty ? el("p", { class: "empty", text: sort === "per_hour"
      ? "Nobody has played an hour in this period yet." : "Nobody has played in this period yet." }) : boardTable(board),
    scope === "ranked" ? el("p", { class: "note", text: "Ranked: games with cheats or admin help are left out." }) : null);

  // The table needs the full width; what's on now and lately sits underneath.
  const joining = summary.addons > 0 ? el("p", { class: "callout" },
    `Joining us? The server plays ${summary.addons} custom map${summary.addons === 1 ? "" : "s"}: subscribe first. `,
    el("a", { href: "#/maps?get=1", text: "Get the maps →" })) : null;
  paint(token, joining, tiles, boardCard, el("div", { class: "grid two" }, nowPlaying(summary.live), recentGames(summary.recent)));
}

// ------------------------------------------------------------------ player
function chapterTable(rows, showResult) {
  if (!rows.length) return el("p", { class: "muted", text: "None yet." });
  return el("div", { class: "table-wrap" }, el("table", {},
    el("thead", {}, el("tr", {}, el("th", { scope: "col", text: "Chapter" }), el("th", { scope: "col", class: "hide-sm", text: "Difficulty" }),
      showResult ? el("th", { scope: "col", text: "Result" }) : null,
      el("th", { scope: "col", class: "n", text: "Points" }), el("th", { scope: "col", class: "n hide-sm", text: "Kills" }),
      el("th", { scope: "col", class: "n hide-sm", text: "Teamwork" }), el("th", { scope: "col", class: "n hide-sm", text: "When" }))),
    el("tbody", {}, rows.map((r) => el("tr", {},
      el("td", { class: "who" }, mapLabel(r), r.boosted ? el("span", { class: "tag after", text: "boosted" }) : null),
      el("td", { class: "hide-sm", text: r.difficulty }),
      showResult ? el("td", {}, resultTag(r.result)) : null,
      el("td", { class: "n", text: number(r.points) }), el("td", { class: "n hide-sm", text: number(r.kills) }),
      el("td", { class: "n hide-sm", text: number(r.teamwork) }),
      el("td", { class: "n hide-sm", title: fullDate(r.ended), text: ago(r.ended) }))))));
}

async function showPlayer(token, steamid) {
  const p = await api("/api/player", { id: steamid });
  setDocTitle(p.name);
  const steam = p.steam;
  const head = el("section", { class: "card" }, el("div", { class: "profile" },
    avatar({ name: p.name, avatar: steam && steam.avatar }, true),
    el("div", { class: "who" },
      el("h2", { text: p.name }),
      el("div", { class: "facts small" },
        p.rank ? el("span", {}, "Ranked ", el("b", { text: `#${p.rank.place}` }), ` of ${number(p.rank.of)}`) : el("span", { text: "Not ranked yet" }),
        el("span", { title: fullDate(p.first_seen), text: `First seen ${ago(p.first_seen)}` }),
        el("span", { title: fullDate(p.last_seen), text: `Last seen ${ago(p.last_seen)}` }),
        el("span", { text: `${number(p.visits)} visit${p.visits === 1 ? "" : "s"}` }),
        el("a", { href: p.profile, rel: "noopener noreferrer", target: "_blank", text: "Steam profile ↗" })))));

  const r = p.ranked, a = p.all;
  const perHour = r.playtime >= p.min_per_hour ? number(Math.round(r.points * 3600 / r.playtime)) : "–";
  const tiles = el("div", { class: "tiles four" },
    tile("Points (ranked)", number(r.points), `${perHour} per hour`),
    tile("Time played", hours(a.playtime), `${number(a.rounds)} chapters played`),
    tile("Kills", number(a.commons + a.specials), `${number(a.headshots)} headshots`),
    tile("Teamwork", number(a.teamwork), `${number(a.heals)} heals · ${number(a.revives)} revives`),
    tile("Tank damage", number(a.tank_damage), `${number(a.tanks)} tanks finished`),
    tile("Survived", number(a.survived), `${number(a.campaigns)} finales won`),
    tile("Deaths", number(a.deaths), `${number(a.incaps)} times down`),
    tile("Friendly fire", number(a.ff_damage), "damage to teammates"));

  const most = Math.max(1, ...SPECIAL_NAMES.map(([key]) => a[key] || 0));
  const specials = card("Special infected killed", el("div", { class: "bars" }, SPECIAL_NAMES.map(([key, label]) =>
    el("div", { class: "bar" }, el("span", { class: "muted", text: label }),
      el("span", { class: "track" }, el("span", { class: "fill", style: `width:${Math.round(100 * (a[key] || 0) / most)}%` })),
      el("span", { class: "n", text: number(a[key]) })))));

  let steamCard = null;
  if (steam) {
    const bits = [];
    if (steam.persona && steam.persona !== p.name) bits.push(el("p", {}, "On Steam as ", el("b", { text: steam.persona })));
    if (steam.hours !== null && steam.hours !== undefined) bits.push(tile("L4D2 hours on Steam", number(steam.hours), "everywhere, not just here"));
    if (steam.achievements) bits.push(tile("Achievements", `${steam.achievements.done}/${steam.achievements.total}`));
    if (!steam.public) bits.push(el("p", { class: "muted", text: "This Steam profile is private, so lifetime figures aren't available." }));
    else if (!bits.length) bits.push(el("p", { class: "muted", text: "Steam figures are on their way; they refresh a few times a day." }));
    steamCard = card("Steam", ...bits);
  }

  const maps = card("Most played", p.maps.length ? el("ul", { class: "list" }, p.maps.map((m) => el("li", {},
    el("div", { class: "line" }, mapLabel(m), el("span", { class: "muted small", text: `${number(m.rounds)} played · ${number(m.cleared)} cleared` })))))
    : el("p", { class: "muted", text: "None yet." }));

  paint(token, el("a", { class: "back", href: "#/", text: "← Leaderboard" }), head, tiles,
    el("div", { class: "grid" },
      el("div", { class: "wide stack" },
        card("Best chapters", chapterTable(p.best, false)), card("Recent chapters", chapterTable(p.recent, true))),
      el("div", { class: "stack" }, specials, steamCard, maps)));
}

// ------------------------------------------------------------------ records
function recordCard(item, value, extra) {
  return el("div", { class: "record" }, el("span", { class: "label", text: item.label }), el("strong", { text: value }),
    playerLink(item), extra ? el("span", { class: "faint small" }, extra) : null);
}

async function showRecords(token, params) {
  const scope = params.get("scope") === "all" ? "all" : "ranked";
  const data = await api("/api/records", { scope });
  setDocTitle("Records");
  if (!data.ready) return paint(token, notReady());
  const toggle = el("label", { class: "check" },
    el("input", { type: "checkbox", checked: scope === "all", on: { change: (e) => go("/records", { scope: e.target.checked ? "all" : "ranked" }) } }),
    "Include boosted games");
  const chapter = data.chapter.map((item) => recordCard(item, number(item.value),
    [mapLabel(item), ` · ${item.difficulty} · ${ago(item.ended)}`]));
  const career = data.career.map((item) => recordCard(item, item.key === "playtime" ? hours(item.value) : number(item.value)));
  const fastest = data.fastest.length ? el("div", { class: "table-wrap" }, el("table", {},
    el("thead", {}, el("tr", {}, el("th", { scope: "col", text: "Chapter" }), el("th", { scope: "col", class: "n", text: "Time" }),
      el("th", { scope: "col", class: "hide-sm", text: "Difficulty" }), el("th", { scope: "col", text: "Team" }))),
    el("tbody", {}, data.fastest.map((f) => el("tr", {},
      el("td", { class: "who" }, mapLabel(f)), el("td", { class: "n", text: duration(f.seconds) }),
      el("td", { class: "hide-sm", text: f.difficulty }),
      el("td", { class: "who people" }, f.team.flatMap((t, i) => [i ? ", " : "", el("a", { href: `#/player/${t.steamid}`, text: t.name })])))))))
    : el("p", { class: "muted", text: "No chapter has been cleared yet." });
  paint(token,
    el("div", { class: "controls" }, el("h2", { text: "Records" }), toggle),
    card("Best single chapter", chapter.length ? el("div", { class: "records" }, chapter) : el("p", { class: "muted", text: "None yet." })),
    card("All-time leaders", career.length ? el("div", { class: "records" }, career) : el("p", { class: "muted", text: "None yet." })),
    card("Fastest clears", el("p", { class: "note", text: "From leaving the start area to reaching the saferoom or rescue." }), fastest));
}

// -------------------------------------------------------------------- maps
function subscribeLinks(links, label) {
  return el("span", { class: "actions" },
    el("a", { class: "btn primary", href: links.steam, text: label || "Subscribe in Steam" }),
    el("a", { class: "btn", href: links.web, target: "_blank", rel: "noopener noreferrer", text: "Workshop page ↗" }));
}

function getTheMaps(addons) {
  if (!addons.length) {
    return el("section", { class: "card", id: "get-maps" }, el("h2", { text: "Get the maps" }),
      el("p", { class: "muted", text: "Only the official campaigns are played here, so there's nothing to download." }));
  }
  const workshop = addons.filter((a) => a.links).length;
  return el("section", { class: "card", id: "get-maps" },
    el("div", { class: "card-head" }, el("h2", { text: "Get the maps" }),
      el("span", { class: "muted small", text: `${addons.length} custom map${addons.length === 1 ? "" : "s"}` })),
    el("p", { class: "muted", text: "Left 4 Dead 2 doesn't download custom maps for you, and the server sends away anyone "
      + "who doesn't have the map it switches to. Subscribe to each one below, then start the game once so Steam "
      + "downloads them before you join." }),
    el("ul", { class: "list" }, addons.map((a) => el("li", {},
      el("div", { class: "line" },
        el("span", {}, el("b", { text: a.title }),
          el("span", { class: "muted small", text: a.chapters > 1 ? ` · ${a.chapters} chapters` : "" })),
        a.links ? subscribeLinks(a.links) : null),
      a.links ? null : el("p", { class: "note", text: a.file
        ? `Not on the Workshop: ask an admin for ${a.file} and put it in your left4dead2\\addons folder.`
        : "Not on the Workshop: ask an admin for the file." })))),
    workshop ? el("p", { class: "note", text: "“Subscribe in Steam” opens the map in the Steam app; if nothing happens, "
      + "use the Workshop page and press Subscribe there." }) : null);
}

async function showMaps(token) {
  const data = await api("/api/maps");
  setDocTitle("Maps");
  const get = getTheMaps(data.addons || []);
  // The stats first, "Get the maps" underneath; before any game is recorded it's all there is.
  if (!data.ready) return paint(token, get);
  if (!data.maps.length) return paint(token, card("Maps played", el("p", { class: "muted", text: "No chapters played yet." })), get);
  paint(token, card("Maps played", el("div", { class: "table-wrap" }, el("table", {},
    el("thead", {}, el("tr", {}, el("th", { scope: "col", text: "Chapter" }), el("th", { scope: "col", class: "n", text: "Played" }),
      el("th", { scope: "col", class: "n", text: "Cleared" }), el("th", { scope: "col", class: "n hide-sm", text: "Wipes" }),
      el("th", { scope: "col", class: "n hide-sm", text: "Fastest" }), el("th", { scope: "col", class: "n hide-sm", text: "Last played" }))),
    el("tbody", {}, data.maps.map((m) => el("tr", {},
      el("td", { class: "who" }, mapLabel(m)), el("td", { class: "n", text: number(m.rounds) }),
      el("td", { class: "n", text: `${number(m.cleared)} (${Math.round(100 * m.cleared / Math.max(1, m.rounds))}%)` }),
      el("td", { class: "n hide-sm", text: number(m.lost) }),
      el("td", { class: "n hide-sm", text: duration(m.fastest) }),
      el("td", { class: "n hide-sm", title: fullDate(m.last_played), text: ago(m.last_played) }))))))), get);
}

// ------------------------------------------------------------------ router
async function route() {
  const token = ++view;
  const { parts, params } = parseHash();
  const tab = parts[0] === "records" ? "records" : parts[0] === "maps" ? "maps" : "board";
  for (const link of document.querySelectorAll(".tabs a")) {
    if (link.dataset.tab === tab && parts[0] !== "player") link.setAttribute("aria-current", "page");
    else link.removeAttribute("aria-current");
  }
  $("loading").hidden = false;
  try {
    if (parts[0] === "player" && /^[0-9]{15,20}$/.test(parts[1] || "")) await showPlayer(token, parts[1]);
    else if (parts[0] === "records") await showRecords(token, params);
    else if (parts[0] === "maps") {
      await showMaps(token);
      // From "Get the maps →": the list sits under the stats, so go straight to it.
      if (token === view && params.has("get")) $("get-maps")?.scrollIntoView();
    }
    else if (parts.length === 0) await showBoard(token, params);
    else throw Object.assign(new Error("Not found."), { status: 404 });
  } catch (error) {
    failure(token, error);
  }
  if (token === view && parts[0] === "player") $("main").focus({ preventScroll: true });
}

window.addEventListener("hashchange", () => { route(); window.scrollTo(0, 0); });
route();
refreshLive();
setInterval(() => { if (!document.hidden) refreshLive(); }, 30000);
