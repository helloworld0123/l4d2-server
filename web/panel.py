"""What the panel can do, independent of HTTP.

Same rule of thumb as the Telegram bot: shell out to the `l4d2` CLI when it
owns state (maps.json, systemd, steamcmd, mods); use RCON when we need an
answer back. The panel additionally edits server.cfg and /etc/l4d2.env itself,
because the CLI has no non-interactive command for those.
"""

import collections
import contextlib
import errno
import fcntl
import functools
import hashlib
import json
import os
import re
import shutil
import subprocess
import threading
import time

from bot import jobs, players, rcon, sanitize

from . import gameio, serverconf

OFFICIAL = [
    ("c1m1_hotel", "Dead Center"), ("c2m1_highway", "Dark Carnival"),
    ("c3m1_plankcountry", "Swamp Fever"), ("c4m1_milltown_a", "Hard Rain"),
    ("c5m1_waterfront", "The Parish"), ("c6m1_riverbank", "The Passing"),
    ("c7m1_docks", "The Sacrifice"), ("c8m1_apartment", "No Mercy"),
    ("c9m1_alleys", "Crash Course"), ("c10m1_caves", "Death Toll"),
    ("c11m1_greenhouse", "Dead Air"), ("c12m1_hilltop", "Blood Harvest"),
    ("c13m1_alpinecreek", "Cold Stream"), ("c14m1_junkyard", "The Last Stand"),
]
# Every chapter of each official campaign, in order, so one can be picked directly.
OFFICIAL_CHAPTERS = {first: tuple(f"{first.split('m1_')[0]}m{number}_{name}" for number, name in
                                  enumerate([first.split("_", 1)[1], *later.split()], start=1))
                     for first, later in {
    "c1m1_hotel": "streets mall atrium", "c2m1_highway": "fairgrounds coaster barns concert",
    "c3m1_plankcountry": "swamp shantytown plantation",
    "c4m1_milltown_a": "sugarmill_a sugarmill_b milltown_b milltown_escape",
    "c5m1_waterfront": "park cemetery quarter bridge", "c6m1_riverbank": "bedlam port",
    "c7m1_docks": "barge port", "c8m1_apartment": "subway sewers interior rooftop",
    "c9m1_alleys": "lots", "c10m1_caves": "drainage ranchhouse mainstreet houseboat",
    "c11m1_greenhouse": "offices garage terminal runway", "c12m1_hilltop": "traintunnel bridge barn cornfield",
    "c13m1_alpinecreek": "southpinestream memorialbridge cutthroatcreek", "c14m1_junkyard": "lighthouse",
}.items()}
# The game modes and difficulties the panel offers, with the names players
# know them by. Apart from versus these are the ones the in-game !mode and
# !difficulty menus offer (cli/plugins/l4d2_admin_extras.sp); a test keeps the
# two lists in step.
MODE_LABELS = {
    "coop": "Co-op",
    "realism": "Realism",
    "versus": "Versus",
    "mutation4": "Hard Eight (8 special infected)",
    "mutation2": "Headshot!",
    "mutation3": "Bleed Out",
    "mutation8": "Ironman",
    "mutation16": "Hunting Party",
    "community1": "Special Delivery",
    "community5": "Death's Door",
}
MODES = tuple(MODE_LABELS)
# Modes a server can be in without the panel offering them: named when shown.
OTHER_MODE_LABELS = {"survival": "Survival", "scavenge": "Scavenge"}
# Any other mutation by number, exactly as the in-game !mode accepts them.
_NUMBERED_MODE_RE = re.compile(r"(?:mutation|community)[1-9][0-9]?")
DIFFICULTY_LABELS = {"Easy": "Easy", "Normal": "Normal", "Hard": "Advanced", "Impossible": "Expert"}
DIFFICULTIES = tuple(DIFFICULTY_LABELS)
TANK_RUN_CVAR = "l4d2_extras_tankrun"
# Survivor team size: asked for through the admin extras plugin, capped by MultiSlots.
TEAM_CVAR = "l4d2_extras_survivors"
TEAM_MAX_CVAR = "l4d_multislots_max_survivors"
NORMAL_TEAM = 4
# Double loot: how many of each item the game scatters over a map ("items per
# 100 yards square"). All of these default to LOOT_NORMAL. The game guards them
# as cheats, so they are set through SourceMod's sm_cvar.
LOOT_CVARS = tuple(f"director_{item}_density" for item in (
    "pain_pill", "adrenaline", "defibrillator", "molotov", "pipe_bomb", "vomitjar",
    "upgradepack", "super_weapon", "melee_weapon", "ammo"))
LOOT_NORMAL, LOOT_DOUBLE = "6.48", "12.96"
# Reverse friendly fire: the l4d_reverse_ff plugin's own switch. It is read
# from that cvar and set through the admin extras plugin (sm_reverseff), which
# remembers the choice across map changes.
REVERSE_FF_CVAR = "reverseff_enabled"
# Infinite ammo: the weapon in hand never runs dry. Guarded as a cheat like the loot.
AMMO_CVAR = "sv_infinite_ammo"
# Things done to survivors from the panel, and the admin extras command behind each.
SURVIVOR_ACTIONS = {
    "kit": ("sm_kit", "First aid kit given."),
    "heal": ("sm_heal", "Healed."),
    "ammo": ("sm_ammo", "Ammo refilled."),
    "respawn": ("sm_respawn", "Brought back."),
    "give": ("sm_give", "Given."),
}
# The session switches set together in one click. The survivor team is left alone:
# it decides who has a seat, not how the game plays.
PRESETS = {
    "normal": ("Normal", "Every switch off.", frozenset()),
    "loot": ("Loot party", "Double loot and infinite ammo, no tank run.", frozenset({"double_loot", "infinite_ammo"})),
    "chaos": ("Chaos night", "Tank run, double loot and infinite ammo.",
              frozenset({"tank_run", "double_loot", "infinite_ammo"})),
}
MAX_ACTIVITY = 60
# A roster check this long after the last one can't say when someone really left.
WATCH_GAP = 60
MIN_SLOTS, MAX_SLOTS = 4, 12
MAX_LOG_LINES = 500
OVERVIEW_TTL = 2.0
CVAR_TTL = 10.0
JOB_LINES = 400

_WORKSHOP_ID_RE = re.compile(r"[?&]id=(\d{4,20})\b")
_CVAR_REPLY_RE = re.compile(r'"(z_difficulty|sv_cheats|mp_gamemode|l4d2_extras_tankrun|l4d2_extras_survivors'
                            r'|l4d_multislots_max_survivors|director_pain_pill_density|sv_infinite_ammo|reverseff_enabled)'
                            r'"\s*=\s*"([^"]*)"')
# A dedicated server hides mp_gamemode from its console ("Unknown command"), so
# it is asked through SourceMod, which answers: [SM] Value of cvar "mp_gamemode": "coop"
_SM_MODE_RE = re.compile(r'Value of cvar "mp_gamemode":\s*"([^"]*)"')
# The admin extras plugin's sm_roster: userid, bot, state, health, temporary health, name.
_ROSTER_RE = re.compile(r"^roster (\d{1,6}) ([01]) (alive|down|dead) (\d{1,5}) (\d{1,5}) (.*)$", re.MULTILINE)
_ITEM_RE = re.compile(r"[a-z0-9_]{1,40}")
_MAX_PLAYERS_RE = re.compile(r"\((\d+) max\)")
_OFFICIAL_MAP_RE = re.compile(r"^c(\d{1,2})m\d{1,2}_")
_CONNECT_RE = re.compile(r"^\s*(?:In-game console:\s*)?(connect \S+)\s*$", re.MULTILINE)


# Both steps are needed: --upgrade brings the plugin's new source, the update builds and loads it.
OLD_EXTRAS_HINT = ("This needs the newest admin extras plugin. On the server, run "
                   "'sudo bash l4d2-setup.sh --upgrade' in the project folder, then press "
                   "Update game files here.")
# Fields of a maps.json entry that are shown or compared as text.
_INDEX_TEXT = ("campaign", "title", "first_map", "file", "url", "source", "id")


def mode_label(mode):
    return MODE_LABELS.get(mode) or OTHER_MODE_LABELS.get(mode) or mode


def _team(cvars):
    """(size, max) of the survivor team from the live cvars, or None when it can't be resized."""
    try:
        size, most = int(float(cvars[TEAM_CVAR])), int(float(cvars[TEAM_MAX_CVAR]))
    except (KeyError, ValueError, OverflowError):     # OverflowError: a value such as 1e999
        return None
    if most < NORMAL_TEAM:
        return None
    return max(NORMAL_TEAM, min(size, most)), most


def _loot_doubled(value):
    """Whether an item density is well above the game's normal one; None when unknown."""
    try:
        return float(value) >= 1.5 * float(LOOT_NORMAL)
    except (TypeError, ValueError):
        return None


def _cvar_on(value):
    """Whether a 0/1 cvar is on, read the way the game does (as a whole number)."""
    try:
        return int(float(value)) != 0
    except (ValueError, OverflowError):
        return False


def valid_mode(mode):
    return isinstance(mode, str) and (mode in MODES or bool(_NUMBERED_MODE_RE.fullmatch(mode)))


def _on_for(text):
    """Seconds in a status "connected" time (07:02 or 1:07:02); 0 when there is none."""
    seconds = 0
    for part in (text or "").split(":"):
        if not part.isdigit():
            return 0
        seconds = seconds * 60 + int(part)
    return seconds


class PanelError(Exception):
    """A request the panel refuses or could not carry out. Safe to show the user."""

    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def _one_at_a_time(method):
    """Session switches wait for each other, so a preset and a switch can't interleave."""
    @functools.wraps(method)
    def wrapped(self, *args, **kwargs):
        with self._session_lock:
            return method(self, *args, **kwargs)
    return wrapped


def _version(text):
    """Tells one server.cfg text from another, so an edit of an older one is noticed."""
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()


def _why(error):
    if error.errno == errno.ELOOP:
        return "it is a symbolic link, which the panel won't follow"
    return error.strerror or str(error)


class JobSlot:
    """One long-running CLI command at a time, with its output kept for the UI.

    One at a time for the same reason as the bot: two installs or an install
    and an update would fight over the same server files.
    """

    def __init__(self, log=print):
        self.log = log
        self._lock = threading.Lock()
        self._job = None
        self._seq = 0

    def start(self, name, argv, timeout, on_done=None):
        """Returns the job id, or None when another job is still running."""
        with self._lock:
            if self._job and self._job["running"]:
                return None
            self._seq += 1
            job = {"id": self._seq, "name": name, "running": True, "started": time.time(),
                   "finished": None, "exit_code": None,
                   "lines": collections.deque(maxlen=JOB_LINES)}
            previous, self._job = self._job, job
            try:
                threading.Thread(target=self._run, args=(job, argv, timeout, on_done),
                                 name="l4d2-job", daemon=True).start()
            except RuntimeError:  # out of threads: don't leave the slot stuck
                self._job = previous
                return None
        return job["id"]

    def _run(self, job, argv, timeout, on_done):
        def on_output(line, _accumulated):
            with self._lock:
                job["lines"].append(line)

        try:
            code, text = jobs.run(argv, timeout=timeout, on_output=on_output)
        except Exception as e:  # never leave the slot stuck on "running"
            code, text = 1, f"{e!r}"
        with self._lock:
            if not job["lines"] and text:
                job["lines"].extend(text.splitlines()[-JOB_LINES:])
            if code == 124:
                job["lines"].append(f"[timed out after {timeout}s and was killed]")
            job.update(running=False, finished=time.time(), exit_code=code)
        self.log(f"job {job['name']} finished with exit code {code}")
        if on_done:
            try:
                on_done(code)
            except Exception as e:
                self.log(f"job {job['name']} follow-up failed: {e!r}")

    def snapshot(self, with_output=False):
        with self._lock:
            if not self._job:
                return None
            snap = {k: v for k, v in self._job.items() if k != "lines"}
            if with_output:
                snap["output"] = "\n".join(self._job["lines"])
            return snap


class Panel:
    def __init__(self, cfg, log=print):
        self.cfg = cfg
        self.log = log
        self.jobs = JobSlot(log)
        self._lock = threading.Lock()
        self._overview = (0.0, None)
        self._overview_seen = None  # the last overview, kept past invalidate() for names
        self._cvars = (0.0, {})
        self._restart_reasons = {}  # reason -> when it arose
        self._write_lock = threading.Lock()  # one settings save at a time
        self._session_lock = threading.RLock()
        # What happened lately, kept in memory only (like the job output).
        self._activity = collections.deque(maxlen=MAX_ACTIVITY)
        self._activity_lock = threading.Lock()
        self._seen = None      # steam id -> [name, checks missed]; None until the first look
        self._seen_at = 0.0
        self._looked_at = 0.0  # last look at the game, answered or not
        self._seen_map = ""

    # ------------------------------------------------------------- transports
    def rcon(self, command):
        return rcon.execute(command, self.cfg.rcon_password(),
                            host=self.cfg.rcon_host, port=self.cfg.rcon_port)

    def try_rcon(self, command):
        try:
            return True, self.rcon(command)
        except rcon.RconError as e:
            return False, str(e)

    def cli(self, *args, timeout=120):
        return jobs.run([self.cfg.l4d2_bin, *args], timeout=timeout)

    def _start_job(self, name, args, timeout, on_done=None):
        def done(code):
            if args[0] in ("restart", "update"):
                rcon.forget_rejection()     # the game restarted: a password it refused may be right now
            self.invalidate()
            self.record(f"{name}: {'done' if code == 0 else f'failed (exit {code})'}", "job" if code == 0 else "bad")
            if on_done:
                on_done(code)

        began = time.time()     # a quick job is done before start() returns
        job_id = self.jobs.start(name, [self.cfg.l4d2_bin, *args], timeout, done)
        if job_id is None:
            running = self.jobs.snapshot() or {"name": "another job"}
            raise PanelError(f"Busy: '{running['name']}' is still running. Try again when it finishes.", 409)
        self.record(f"{name}: started", "job", began)
        return {"job": job_id}

    @contextlib.contextmanager
    def _server_lock(self, what):
        """Hold the lock the l4d2 command uses while we rewrite the files it also rewrites.

        Without it, a `l4d2 slots` from the bot and a settings save from here
        could each read server.cfg, and the slower one would undo the other.
        """
        with self._write_lock:
            handle = open(self.cfg.lock_path, "a+")
            try:
                try:
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError:
                    handle.seek(0)
                    other = sanitize.display(handle.read().strip() or "another command")
                    raise PanelError(f"Busy: '{other}' is still running. Try again when it finishes.", 409)
                handle.seek(0)
                handle.truncate()
                handle.write(what)
                handle.flush()
                yield
                handle.seek(0)
                handle.truncate()
            finally:
                handle.close()  # closing releases the lock

    def _game(self, op, **args):
        """Read or write under the game folder, as the game user. Raises OSError."""
        return gameio.call(self.cfg.game_user, op, **args)

    def _game_text(self, path, limit=serverconf.MAX_CFG_BYTES * 4):
        """Text of a game-user file for display only: unreadable reads as empty."""
        try:
            return self._game("read", path=path, limit=limit)
        except OSError:
            return ""

    def _read_cfg(self):
        """server.cfg's text ("" only when it doesn't exist). Anything else is an error, never empty."""
        try:
            return self._game("read", path=self.cfg.cfg_path)
        except OSError as e:
            raise PanelError(f"Couldn't read server.cfg: {_why(e)}", 500)

    def invalidate(self):
        with self._lock:
            self._overview = (0.0, None)
            self._cvars = (0.0, {})

    def _needs_restart(self, reason):
        with self._lock:
            self._restart_reasons[reason] = time.time()

    # --------------------------------------------------------------- activity
    def record(self, text, kind="admin", when=None):
        """Note something for the activity list. The same thing again is counted, not repeated."""
        when = time.time() if when is None else when
        with self._activity_lock:
            last = self._activity[-1] if self._activity else None
            if last and last["text"] == text and kind == "admin":
                last.update(time=when, count=last["count"] + 1)
            else:
                self._activity.append({"time": when, "kind": kind, "text": text, "count": 1})

    def activity(self):
        """Newest first."""
        with self._activity_lock:
            return sorted((dict(entry) for entry in self._activity), key=lambda e: e["time"], reverse=True)

    def _watch(self, people, current):
        """Notice who came and went, and map changes, since the last look at the game.

        Only runs while a panel page is open (that is what asks for the status),
        so after a gap the time someone left isn't known. Someone has to be missing
        twice in a row: a map change empties the list for a moment.
        """
        now = time.time()
        here = {p.uniqueid: p for p in people if p.uniqueid and "PENDING" not in p.uniqueid.upper()}
        if self._seen is None:
            self._seen = {uid: [p.name, 0] for uid, p in here.items()}
        else:
            gap = now - self._looked_at > WATCH_GAP
            for uid, p in here.items():
                if uid not in self._seen:
                    joined = max(self._seen_at, now - _on_for(p.connected)) if p.connected else now
                    self.record(f"{sanitize.display(p.name, 64)} joined", "join", joined)
                self._seen[uid] = [p.name, 0]
            for uid in [uid for uid in self._seen if uid not in here]:
                self._seen[uid][1] += 1
                if self._seen[uid][1] >= 2 or gap:
                    name = sanitize.display(self._seen.pop(uid)[0], 64)
                    self.record(f"{name} left" + (" while no panel was open" if gap else ""), "leave")
        self._seen_at = now
        if current and self._seen_map and current != self._seen_map:
            name = self._campaign(current)[1]
            self.record(f"Map changed to {sanitize.display(name, 64) + ' · ' if name else ''}{current}", "map")
        self._seen_map = current or self._seen_map

    # ---------------------------------------------------------------- reading
    def _service_state(self):
        try:
            result = subprocess.run(["systemctl", "is-active", "l4d2"],
                                    capture_output=True, text=True, timeout=5)
            return result.stdout.strip() or "unknown"
        except (OSError, subprocess.SubprocessError):
            return "unknown"

    def _process_uptime(self):
        """Seconds the game process has been up, or None when it isn't running."""
        user = self.cfg.server_env.get("L4D2_USER", "l4d2")
        try:
            pids = subprocess.run(["pgrep", "-u", user, "-f", "srcds_linux"],
                                  capture_output=True, text=True, timeout=5).stdout.split()
            if not pids:
                return None
            etimes = subprocess.run(["ps", "-o", "etimes=", "-p", pids[0]],
                                    capture_output=True, text=True, timeout=5).stdout.strip()
            return int(etimes)
        except (OSError, ValueError, subprocess.SubprocessError):
            return None

    def _system(self):
        stats = {"cpus": os.cpu_count() or 1}
        try:
            stats["load"] = [round(x, 2) for x in os.getloadavg()]
        except OSError:
            stats["load"] = None
        try:
            with open("/proc/meminfo") as fh:
                mem = dict(line.split(":", 1) for line in fh)
            stats["mem_total"] = int(mem["MemTotal"].split()[0]) * 1024
            stats["mem_available"] = int(mem["MemAvailable"].split()[0]) * 1024
        except (OSError, KeyError, ValueError):
            pass
        try:
            target = self.cfg.server_dir if os.path.isdir(self.cfg.server_dir) else "/"
            usage = shutil.disk_usage(target)
            stats["disk_total"], stats["disk_free"] = usage.total, usage.free
        except OSError:
            pass
        return stats

    def _live_cvars(self):
        stamp, cached = self._cvars
        if time.time() - stamp < CVAR_TTL:
            return cached
        found = self._ask_cvars()
        if found is None:
            return {}      # not cached: one failed query must not blank the toggles for CVAR_TTL
        self._cvars = (time.time(), found)
        return found

    def _ask_cvars(self):
        """The live settings, asked fresh; None when the game can't be reached."""
        # The last three only exist with the mods (admin extras, MultiSlots); without
        # them the console answers "Unknown command" and the name is simply not found.
        ok, out = self.try_rcon(f"z_difficulty; sv_cheats; mp_gamemode; {TANK_RUN_CVAR}; {TEAM_CVAR}; {TEAM_MAX_CVAR}; "
                                f"{LOOT_CVARS[0]}; {AMMO_CVAR}; {REVERSE_FF_CVAR}; sm_cvar mp_gamemode")
        if not ok:
            return None
        found = dict(_CVAR_REPLY_RE.findall(out))
        mode = _SM_MODE_RE.search(out)
        if mode:
            found["mp_gamemode"] = mode.group(1)
        return found

    def _survivors(self):
        ok, out = self.try_rcon("sm_roster")
        if not ok or "unknown command" in out.lower():
            return None
        return [{
            "userid": userid,
            "bot": bot == "1",
            "state": state,
            "health": int(health),
            "temp": int(temp),
            "name": sanitize.display(name.strip(), 64),
        } for userid, bot, state, health, temp, name in _ROSTER_RE.findall(out)]

    def _index(self):
        try:
            data = json.loads(self._game_text(self.cfg.index_path, 4 * 1024 * 1024))
        except ValueError:
            return {}
        if not isinstance(data, dict):
            return {}
        # maps.json is the game user's: a text field holding anything else is dropped here, once.
        return {key: {name: value for name, value in item.items()
                      if name not in _INDEX_TEXT or isinstance(value, str)}
                for key, item in data.items() if isinstance(item, dict)}

    def _campaign(self, current):
        """(first map, name) of the campaign a map belongs to, or ("", "").

        The game reports the chapter being played (c2m3_coaster), while the
        panel lists campaigns by their first chapter (c2m1_highway).
        """
        current = (current or "").lower()
        if not current:
            return "", ""
        # Installed maps first: a custom campaign may reuse the cNmM_ naming.
        # maps.json is written by the game user, so every value is type-checked.
        for item in self._index().values():
            raw = item.get("maps")
            maps = [name.lower() for name in raw if isinstance(name, str)] if isinstance(raw, list) else []
            first = item.get("first_map")
            first = first if isinstance(first, str) and first else (maps[0] if maps else "")
            if first and (current in maps or current == first.lower()):
                name = item.get("campaign") or item.get("title")
                return first, name if isinstance(name, str) else ""
        official = _OFFICIAL_MAP_RE.match(current)
        if official:
            prefix = f"c{official.group(1)}m1_"
            for first, title in OFFICIAL:
                if first.startswith(prefix):
                    return first, title
        return "", ""

    def _pending_addons(self, uptime):
        """Addons written after the server started: not loaded until a restart."""
        if uptime is None:
            return 0
        started = time.time() - uptime
        try:
            return sum(1 for mtime in self._game("vpk_mtimes", directory=self.cfg.addons_dir)
                       if mtime > started)
        except OSError:
            return 0

    def _join(self, cfg_text):
        match = _CONNECT_RE.search(self._game_text(self.cfg.info_path))
        return {
            "connect": match.group(1) if match else None,
            "password": serverconf.read_cvar(cfg_text, "sv_password") or "",
        }

    def overview(self):
        with self._lock:
            stamp, base = self._overview
            if base is None or time.time() - stamp >= OVERVIEW_TTL:
                base = self._build_overview()
                self._overview = (time.time(), base)
                self._overview_seen = base
        return dict(base, job=self.jobs.snapshot(), activity=self.activity()[:30])

    def _build_overview(self):
        env = self.cfg.server_env
        cfg_text = self._game_text(self.cfg.cfg_path)
        uptime = self._process_uptime()

        ok, out = self.try_rcon("status")
        info, everyone = players.parse_status(out) if ok else ({}, [])
        error = None if ok else out
        if ok and not info and not everyone:
            # Never show "nobody is playing" when we simply failed to parse.
            ok, error = False, "The server answered, but not with a status the panel understands."
        cvars = self._live_cvars() if ok else {}
        survivors = self._survivors() if ok else None

        current = info.get("map", "").split(" at:")[0].strip()
        campaign_map, campaign = self._campaign(current)
        max_match = _MAX_PLAYERS_RE.search(info.get("players", ""))
        people = players.humans(everyone)
        if ok:
            self._watch(people, current)
        self._looked_at = time.time()
        # The console reports these in whatever case they were last typed in.
        difficulty = (cvars.get("z_difficulty") or serverconf.read_cvar(cfg_text, "z_difficulty") or "").capitalize()
        mode = (cvars.get("mp_gamemode") or env.get("L4D2_MODE", "")).lower()
        tank_run = cvars.get(TANK_RUN_CVAR)
        reverse_ff = cvars.get(REVERSE_FF_CVAR)
        team = _team(cvars)

        if uptime is not None:
            started = time.time() - uptime
            for reason, when in list(self._restart_reasons.items()):
                if when < started:
                    self._restart_reasons.pop(reason, None)
        reasons = sorted(self._restart_reasons)
        pending = self._pending_addons(uptime)
        if pending:
            reasons.append(f"{pending} new addon file(s) not loaded yet")

        return {
            "time": time.time(),
            "server": {
                "service": self._service_state(),
                "running": uptime is not None or ok,
                "uptime": uptime,
                "restart_needed": reasons,
            },
            "game": {
                "reachable": ok,
                "error": error,
                "hostname": info.get("hostname") or serverconf.read_cvar(cfg_text, "hostname") or "",
                "map": current,
                "campaign": campaign,
                "campaign_map": campaign_map,
                "humans": len(people),
                "bots": len(everyone) - len(people),
                "max_players": int(max_match.group(1)) if max_match else None,
                "difficulty": difficulty,
                "difficulty_label": DIFFICULTY_LABELS.get(difficulty, difficulty),
                "mode": mode,
                "mode_label": mode_label(mode),
                "cheats": cvars.get("sv_cheats") not in (None, "", "0"),
                # None: the server has no tank run (the admin extras plugin isn't loaded).
                "tank_run": None if tank_run is None else _cvar_on(tank_run),
                # None: the survivor team can't be resized (no admin extras plugin or no MultiSlots).
                "team": team and {"size": team[0], "max": team[1]},
                "double_loot": _loot_doubled(cvars.get(LOOT_CVARS[0])),
                "infinite_ammo": AMMO_CVAR in cvars and _cvar_on(cvars[AMMO_CVAR]),
                # None: the Reverse Friendly-Fire plugin isn't loaded.
                "reverse_ff": None if reverse_ff is None else _cvar_on(reverse_ff),
            },
            "players": [{
                "userid": p.userid,
                "name": sanitize.display(p.name, 64),
                "steamid": p.uniqueid,
                "connected": p.connected,
                "ping": p.ping,
            } for p in people],
            # None: no roster (the admin extras plugin isn't loaded, or is an older one).
            "survivors": survivors,
            "defaults": {"map": env.get("L4D2_MAP", ""), "mode": env.get("L4D2_MODE", ""),
                         "slots": self._slots(env)},
            "system": self._system(),
            "join": self._join(cfg_text),
            "presets": [{"id": key, "label": label, "note": note, "on": sorted(on)}
                        for key, (label, note, on) in PRESETS.items()],
        }

    @staticmethod
    def _slots(env):
        try:
            return int(env.get("L4D2_SLOTS", "4"))
        except ValueError:
            return 4

    def maps(self):
        custom = []
        items = sorted(self._index().items(),
                       key=lambda kv: (kv[1].get("campaign") or kv[1].get("title") or "").lower())
        # maps.json is the game user's: "file" may point anywhere, so ask as them.
        files = [os.path.join(self.cfg.addons_dir, item.get("file") or "?") for _, item in items]
        try:
            present = self._game("exists", paths=files) if files else []
        except OSError:
            present = [False] * len(files)
        for (key, item), found in zip(items, present):
            custom.append({
                "key": key,
                "name": item.get("campaign") or item.get("title") or key,
                "first_map": item.get("first_map"),
                "maps": self._chapters(item),
                "url": item.get("url"),
                "source": item.get("source"),
                "file": item.get("file"),
                "missing": not found,
            })
        return {
            "official": [{"map": name, "name": title, "maps": list(OFFICIAL_CHAPTERS[name])} for name, title in OFFICIAL],
            "custom": custom,
            "default": self.cfg.server_env.get("L4D2_MAP", ""),
        }

    @staticmethod
    def _chapters(item):
        # maps.json is the game user's: only names that could really be switched to.
        raw = item.get("maps")
        return [name for name in raw if isinstance(name, str) and sanitize.valid_map(name)] if isinstance(raw, list) else []

    def settings(self):
        env = self.cfg.server_env
        mode = env.get("L4D2_MODE", "coop")
        cfg_text = self._read_cfg()
        return {
            "hostname": serverconf.read_cvar(cfg_text, "hostname") or "",
            "password": serverconf.read_cvar(cfg_text, "sv_password") or "",
            "difficulty": (serverconf.read_cvar(cfg_text, "z_difficulty") or "Normal").capitalize(),
            "mode": mode,
            "default_map": env.get("L4D2_MAP", ""),
            "slots": self._slots(env),
            "rcon_enabled": bool(self.cfg.rcon_password()),
            # A mode set some other way (survival, a numbered mutation) stays
            # selectable, so saving another setting doesn't trip over it.
            "choices": {"modes": list(MODES) + ([mode] if mode not in MODES else []),
                        "difficulties": list(DIFFICULTIES),
                        "slots": list(range(MIN_SLOTS, MAX_SLOTS + 1))},
            "labels": {"modes": {m: mode_label(m) for m in (*MODES, mode)},
                       "difficulties": dict(DIFFICULTY_LABELS)},
        }

    def raw_config(self):
        text = self._read_cfg()
        return {"text": text, "path": self.cfg.cfg_path, "version": _version(text)}

    def logs(self, lines=100):
        count = max(1, min(int(lines), MAX_LOG_LINES))
        try:
            tail = self._game("tail", path=self.cfg.log_path, limit=256 * 1024).splitlines()[-count:]
        except OSError as e:
            raise PanelError(f"Can't read the server log: {_why(e)}", 404)
        return {"lines": tail}

    def job(self):
        return {"job": self.jobs.snapshot(with_output=True)}

    # ---------------------------------------------------------------- actions
    def control(self, action):
        if action in ("start", "stop"):
            code, out = self.cli(action)
            rcon.forget_rejection()
            self.invalidate()
            if code != 0:
                raise PanelError(out or f"l4d2 {action} failed (exit {code}).", 502)
            self.record(f"Server {'started' if action == 'start' else 'stopped'}")
            return {"message": f"Server {'started' if action == 'start' else 'stopped'}."}
        if action == "restart":
            return self._start_job("Restart server", ["restart"], 240)
        if action == "update":
            return self._start_job("Update game files", ["update"], 5400)
        raise PanelError("Unknown action.")

    def change_map(self, name, make_default=False):
        if not sanitize.valid_map(name):
            raise PanelError("That doesn't look like a map name.")
        if make_default:
            return self._start_job(f"Switch to {name} (new default)",
                                   ["changemap", name, "--default", "--no-restart"], 240)
        reply = self.rcon(f"changelevel {name}")
        self.invalidate()
        if "changelevel failed" in reply.lower():
            raise PanelError(f"The game can't find {name}. A map that was just installed only loads "
                             "after a server restart.", 409)
        self.record(f"Switched the map to {name}")
        return {"message": f"Switching to {name}..."}

    def _name_of(self, userid):
        """The name last seen for a userid, for the activity list only."""
        base = self._overview_seen or {}
        for person in [*(base.get("survivors") or []), *base.get("players", [])]:
            if person["userid"] == str(userid):
                return person["name"]
        return f"player #{userid}"

    def kick(self, userid, reason=""):
        if not re.fullmatch(r"\d{1,6}", str(userid)):
            raise PanelError("Bad player id.")
        # By userid, never by name: names are chosen by whoever joins.
        reason = sanitize.console_arg(reason, 80)
        who = self._name_of(userid)
        self.rcon(f'kickid {userid} "{reason}"' if reason else f"kickid {userid}")
        self.invalidate()
        self.record(f"Kicked {who}")
        return {"message": "Player kicked."}

    @_one_at_a_time
    def tank_run(self, on):
        """Start or stop a tank run: tanks keep coming until the map ends."""
        if not isinstance(on, bool):
            raise PanelError("Say whether the tank run should be on or off.")
        # Asked fresh rather than from the cache: the plugin may have just been
        # installed, and sm_tankrun on a server without it would do nothing.
        if TANK_RUN_CVAR not in dict(_CVAR_REPLY_RE.findall(self.rcon(TANK_RUN_CVAR))):
            raise PanelError("Tank run isn't available on this server. It comes with the mods that more "
                             "than 4 player slots install (Settings). If they are already installed, "
                             "update the game files to refresh them.", 409)
        reply = self.rcon(f"sm_tankrun {'on' if on else 'off'}")
        self.invalidate()
        if "unknown command" in reply.lower():
            # The setting can outlive the plugin that made it.
            raise PanelError("The admin extras plugin isn't loaded, so the tank run can't be switched. "
                             "Restart the server, or update the game files to refresh the mods.", 409)
        self.record(f"Tank run {'on' if on else 'off'}")
        return {"message": "Tank run on: tanks keep coming until this map ends." if on else "Tank run off."}

    def survivor_action(self, action, target, item=None):
        """Heal, equip or bring back one survivor (by userid) or all of them."""
        if action not in SURVIVOR_ACTIONS:
            raise PanelError("Unknown action.")
        if target == "all":
            who = "@all"
        elif isinstance(target, str) and re.fullmatch(r"[0-9]{1,6}", target):
            who = f"#{target}"     # by userid, never by name: names are chosen by whoever joins
        else:
            raise PanelError("Say which survivor.")
        command, done = SURVIVOR_ACTIONS[action]
        if action == "give":
            if not isinstance(item, str) or not _ITEM_RE.fullmatch(item):
                raise PanelError("Pick an item to give.")
            command = f"{command} {item}"
        reply = self.rcon(f"{command} {who}")
        self.invalidate()
        if "unknown command" in reply.lower():
            raise PanelError(OLD_EXTRAS_HINT, 409)
        # The commands only answer when they could not do it ("No matching client...").
        for line in reply.splitlines():
            refused = re.match(r"\[(?:Admin|SM)\]\s*(.+)", line.strip())
            if refused:
                raise PanelError(sanitize.display(refused.group(1), 200), 409)
        whom = "everyone" if target == "all" else self._name_of(target)
        self.record({"heal": f"Healed {whom}", "kit": f"First aid kit for {whom}", "ammo": f"Ammo for {whom}",
                     "respawn": f"Brought back {'the dead' if target == 'all' else whom}",
                     "give": f"Gave {(item or '').replace('_', ' ')} to {whom}"}[action])
        return {"message": done}

    @_one_at_a_time
    def reverse_ff(self, on):
        """Switch reverse friendly fire: whoever shoots a teammate takes the damage instead."""
        if not isinstance(on, bool):
            raise PanelError("Say whether reverse friendly fire should be on or off.")
        # Asked fresh, like the tank run: the plugin may have just been installed.
        if REVERSE_FF_CVAR not in dict(_CVAR_REPLY_RE.findall(self.rcon(REVERSE_FF_CVAR))):
            raise PanelError("Reverse friendly fire isn't installed on this server. It comes with the mods "
                             "that more than 4 player slots install (Settings). If they are already "
                             "installed, update the game files to refresh them.", 409)
        reply = self.rcon(f"sm_reverseff {'on' if on else 'off'}")
        self.invalidate()
        if "unknown command" in reply.lower():
            raise PanelError(OLD_EXTRAS_HINT, 409)
        self.record(f"Reverse friendly fire {'on' if on else 'off'}")
        return {"message": "Reverse friendly fire on: shoot a teammate and you take the damage."
                if on else "Reverse friendly fire off."}

    @_one_at_a_time
    def infinite_ammo(self, on):
        """Switch bottomless ammo for the weapon in hand on or off."""
        if not isinstance(on, bool):
            raise PanelError("Say whether infinite ammo should be on or off.")
        reply = self.rcon(f"sm_cvar {AMMO_CVAR} {'1' if on else '0'}")
        self.invalidate()
        if "unknown command" in reply.lower():
            raise PanelError("Infinite ammo needs SourceMod, which comes with the mods that more than "
                             "4 player slots install (Settings).", 409)
        # Asked back rather than trusted, like the loot.
        now = dict(_CVAR_REPLY_RE.findall(self.rcon(AMMO_CVAR))).get(AMMO_CVAR)
        if now is None or _cvar_on(now) is not on:
            raise PanelError("The game didn't take the ammo setting. Try again in a moment.", 502)
        self.record(f"Infinite ammo {'on' if on else 'off'}")
        return {"message": "Infinite ammo on: nobody needs to reload or find ammo." if on else "Infinite ammo off."}

    @_one_at_a_time
    def double_loot(self, on):
        """Double (or put back) how many items the game scatters over each map."""
        if not isinstance(on, bool):
            raise PanelError("Say whether double loot should be on or off.")
        value = LOOT_DOUBLE if on else LOOT_NORMAL
        reply = self.rcon("; ".join(f"sm_cvar {name} {value}" for name in LOOT_CVARS))
        self.invalidate()
        if "unknown command" in reply.lower():
            raise PanelError("Double loot needs SourceMod, which comes with the mods that more than "
                             "4 player slots install (Settings).", 409)
        # Asked back rather than trusted: sm_cvar answers even when it changed nothing.
        if _loot_doubled(dict(_CVAR_REPLY_RE.findall(self.rcon(LOOT_CVARS[0]))).get(LOOT_CVARS[0])) is not on:
            raise PanelError("The game didn't take the loot setting. Try again in a moment.", 502)
        self.record(f"Double loot {'on' if on else 'off'}")
        return {"message": ("Double loot on." if on else "Double loot off.")
                + " Items are placed when a map loads, so it shows from the next map."}

    @_one_at_a_time
    def preset(self, name):
        """Set every session switch the way a preset wants it, leaving out what the server can't do."""
        if not isinstance(name, str) or name not in PRESETS:
            raise PanelError("Unknown preset.")
        label, _note, wanted = PRESETS[name]
        ok, out = self.try_rcon("sv_cheats")
        if not ok:
            raise PanelError(out, 502)
        # Cheats first: turning them off puts cheat-guarded settings back to the
        # game's own values, which would undo the loot and ammo set before it.
        if _cvar_on(dict(_CVAR_REPLY_RE.findall(out)).get("sv_cheats", "0")):
            self.save_settings({"cheats": False})
        cvars = self._ask_cvars()
        if not cvars:
            raise PanelError("The game didn't say which switches are on. Try again in a moment.", 502)
        now = {
            "tank_run": None if TANK_RUN_CVAR not in cvars else _cvar_on(cvars[TANK_RUN_CVAR]),
            "double_loot": _loot_doubled(cvars.get(LOOT_CVARS[0])),
            "infinite_ammo": None if AMMO_CVAR not in cvars else _cvar_on(cvars[AMMO_CVAR]),
            "reverse_ff": None if REVERSE_FF_CVAR not in cvars else _cvar_on(cvars[REVERSE_FF_CVAR]),
        }
        switches = {"tank_run": ("tank run", self.tank_run), "double_loot": ("double loot", self.double_loot),
                    "infinite_ammo": ("infinite ammo", self.infinite_ammo),
                    "reverse_ff": ("reverse friendly fire", self.reverse_ff)}
        failed = []
        for key, (title, switch) in switches.items():
            want = key in wanted
            if now[key] is want or (now[key] is None and not want):
                continue
            try:
                switch(want)
            except (PanelError, rcon.RconError) as e:
                failed.append(f"{title} ({e})" if now[key] is not None else f"{title} (not available on this server)")
        self.record(f"Preset: {label}")
        if failed:
            return {"message": f"{label} set, except: {'; '.join(failed)}", "failed": failed}
        return {"message": f"{label} set." + (" Double loot shows from the next map." if "double_loot" in wanted
                                               and now["double_loot"] is not True else ""), "failed": []}

    def team_size(self, size):
        """Set how many survivors there are; bots fill the seats nobody plays."""
        if isinstance(size, bool) or not isinstance(size, int) or not NORMAL_TEAM <= size <= MAX_SLOTS:
            raise PanelError(f"Pick a survivor team size from {NORMAL_TEAM} to {MAX_SLOTS}.")
        # Asked fresh, like the tank run: the mods may have just been installed.
        team = _team(dict(_CVAR_REPLY_RE.findall(self.rcon(f"{TEAM_CVAR}; {TEAM_MAX_CVAR}"))))
        if team is None:
            raise PanelError("The survivor team can't be resized on this server. That comes with the mods "
                             "that more than 4 player slots install (Settings). If they are already "
                             "installed, update the game files to refresh them.", 409)
        if size > team[1]:
            raise PanelError(f"This server is set up for {team[1]} players. Raise the co-op player slots "
                             f"first to have {size} survivors.", 409)
        reply = self.rcon(f"sm_survivors {size}")
        self.invalidate()
        if "unknown command" in reply.lower():
            raise PanelError("The admin extras plugin isn't loaded, so the team can't be resized. "
                             "Restart the server, or update the game files to refresh the mods.", 409)
        self.record(f"Survivor team set to {size}")
        if size == NORMAL_TEAM:
            return {"message": "Survivor team back to the normal 4. Bots nobody needs leave."}
        return {"message": f"Survivor team set to {size}: bots fill the seats nobody plays."}

    def say(self, text):
        text = sanitize.console_arg(text)
        if not text:
            raise PanelError("Type a message first.")
        self.rcon(f'say "{text}"')
        self.record(f"Message: {sanitize.display(text, 80)}")
        return {"message": "Message sent."}

    def console(self, command):
        command = (command or "").strip()
        if not command or len(command) > 500 or "\x00" in command or "\n" in command:
            raise PanelError("Type a single console command.")
        if command.split()[0].lower() == "l4d2":
            raise PanelError("'l4d2 ...' is a shell command for the machine, not a game console command. "
                             "The panel's buttons cover start, stop, restart, update and maps; "
                             "here, type game commands such as: status")
        if sanitize.rcon_refused(command):
            raise PanelError("Refused: that would change the RCON settings the panel depends on. "
                             "Edit server.cfg instead.")
        output = self.rcon(command)
        self.invalidate()
        # The name only: arguments can hold passwords.
        self.record(f"Console command: {(re.match(r'[A-Za-z0-9_]{1,40}', command) or ['?'])[0]}")
        return {"output": output}

    def add_maps(self, text):
        ids = []
        for token in (text or "").split():
            match = _WORKSHOP_ID_RE.search(token)
            wid = match.group(1) if match else (token if re.fullmatch(r"\d{4,20}", token) else None)
            if not wid:
                raise PanelError(f"'{sanitize.display(token)}' is not a Steam Workshop link or id.")
            if wid not in ids:
                ids.append(wid)
        if not ids:
            raise PanelError("Paste at least one Steam Workshop link or id.")
        if len(ids) > 30:
            raise PanelError("That's a lot at once - add at most 30 links per go.")
        return self._start_job("Install Workshop maps", ["addmap", *ids, "--no-restart"], 1800)

    def remove_map(self, key):
        item = self._index().get(key)
        if item is None:
            raise PanelError("That map isn't installed.", 404)
        name = item.get("campaign") or item.get("title") or key
        return self._start_job(
            f"Remove {sanitize.display(name)}", ["removemap", key, "--no-restart"], 300,
            lambda code: code == 0 and self._needs_restart("a map was removed"))

    def update_maps(self):
        return self._start_job("Update Workshop maps", ["updatemaps", "--no-restart"], 1800)

    def set_slots(self, count):
        try:
            count = int(count)
        except (TypeError, ValueError):
            raise PanelError("Player slots must be a number.")
        if not MIN_SLOTS <= count <= MAX_SLOTS:
            raise PanelError(f"Player slots must be from {MIN_SLOTS} to {MAX_SLOTS}.")
        return self._start_job(
            f"Set co-op slots to {count}", ["slots", str(count), "--no-restart"], 900,
            lambda code: code == 0 and self._needs_restart("player slots changed"))

    def save_settings(self, data):
        """Apply any subset of the settings. Returns what changed and what couldn't go live."""
        # Turning cheats off resets loot and ammo, so it waits for a preset under way.
        if "cheats" in data and not isinstance(data["cheats"], bool):
            raise PanelError("Say whether cheats should be on or off.")
        if set(data) == {"cheats"}:
            # A console setting only: no file is touched, so a running job is no reason to wait.
            with self._session_lock:
                self._set_cheats(data["cheats"])
                self.invalidate()
                self.log("settings changed: cheats")
                return {"changed": ["cheats"], "live_failed": [], "restart_needed": False}
        with self._session_lock if "cheats" in data else contextlib.nullcontext():
            with self._server_lock("web panel: saving settings"):
                return self._save_settings(data)

    def _save_settings(self, data):
        current = self.settings()
        cfg_changes, live, changed, live_failed = {}, [], [], []

        if "hostname" in data:
            value = serverconf.clean_value(str(data["hostname"]), 64)
            if not value:
                raise PanelError("The server name can't be empty.")
            if value != current["hostname"]:
                cfg_changes["hostname"] = value
                live.append(("server name", f'hostname "{value}"'))
        if "password" in data:
            value = serverconf.clean_value(str(data["password"]), 32)
            if " " in value:
                raise PanelError("The password can't contain spaces (players type it in the console).")
            if value != current["password"]:
                cfg_changes["sv_password"] = value
                live.append(("password", f'sv_password "{value}"'))
        if "difficulty" in data:
            value = str(data["difficulty"]).capitalize()
            # As with the mode below: a value already in server.cfg that the
            # panel doesn't offer must not stop the other settings from saving.
            if value != current["difficulty"] and value not in DIFFICULTIES:
                raise PanelError("Unknown difficulty.")
            if value != current["difficulty"]:
                cfg_changes["z_difficulty"] = value
                live.append(("difficulty", f"z_difficulty {value}"))

        mode = data.get("mode")
        if mode is not None and mode != current["mode"] and not valid_mode(mode):
            raise PanelError("Unknown game mode.")
        default_map = data.get("default_map")
        if default_map is not None and default_map != current["default_map"] and not (
                isinstance(default_map, str) and sanitize.valid_map(default_map)):
            raise PanelError("That doesn't look like a map name.")

        # Everything is validated; from here on we write.
        if cfg_changes:
            text = self._read_cfg()
            for name, value in cfg_changes.items():
                text = serverconf.set_cvar(text, name, value)
            self._write_cfg(text)
            try:
                self._game("update_info", path=self.cfg.info_path,
                           hostname=cfg_changes.get("hostname"),
                           password=cfg_changes.get("sv_password"))
            except OSError:
                pass  # best effort, like update_info itself
            for label, command in live:
                changed.append(label)
                ok, _ = self.try_rcon(command)
                if not ok:
                    live_failed.append(label)

        if mode is not None and mode != current["mode"]:
            serverconf.set_env_value(self.cfg.server_env_path, "L4D2_MODE", mode)
            self._needs_restart("game mode changed")
            changed.append("game mode")
        if default_map is not None and default_map != current["default_map"]:
            serverconf.set_env_value(self.cfg.server_env_path, "L4D2_MAP", default_map)
            changed.append("default map")

        if "cheats" in data:
            self._set_cheats(data["cheats"])
            changed.append("cheats")

        self.invalidate()
        saved = [name for name in changed if name != "cheats"]
        if saved:
            self.record(f"Settings saved: {', '.join(saved)}")
        self.log("settings changed: " + (", ".join(changed) or "nothing"))
        return {"changed": changed, "live_failed": live_failed,
                "restart_needed": mode is not None and mode != current["mode"]}

    def _set_cheats(self, on):
        self.rcon(f"sv_cheats {'1' if on else '0'}")
        self.record(f"Cheats {'on' if on else 'off'}")

    def _write_cfg(self, text):
        # The save re-reads the file for the backup and writes nothing when that
        # fails: an unreadable or linked server.cfg is never replaced blind.
        try:
            self._game("save", path=self.cfg.cfg_path, text=text)
        except OSError as e:
            raise PanelError(f"Couldn't write server.cfg: {_why(e)}", 500)

    def save_raw_config(self, text, apply_now=False, base=None):
        if not isinstance(text, str) or "\x00" in text:
            raise PanelError("That isn't valid config text.")
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        if len(text.encode("utf-8")) > serverconf.MAX_CFG_BYTES:
            raise PanelError("server.cfg is limited to 64 KB.")
        if text and not text.endswith("\n"):
            text += "\n"
        # The running server still has the old password until it re-reads the
        # file, so "apply now" must authenticate with the one from before.
        old_password = self.cfg.rcon_password()
        with self._server_lock("web panel: saving server.cfg"):
            # base: the version that was edited. Saving an edit of an older one
            # would quietly undo whatever was saved in between.
            if base is not None and base != _version(self._read_cfg()):
                raise PanelError("server.cfg was changed since you opened it, so nothing was saved. Copy your "
                                 "edits, press Reload, and make them again.", 409)
            self._write_cfg(text)
        self.log("server.cfg replaced from the config editor")
        self.record("server.cfg edited")
        warnings = []
        new_password = rcon.parse_password(text)
        if new_password and new_password != old_password and not apply_now:
            warnings.append("rcon_password changed, but the running game still has the old one: the panel "
                            "can't see players or apply live changes until the server restarts.")
        if not new_password:
            warnings.append("rcon_password is empty: after the next restart the panel can no longer "
                            "see players or apply live changes.")
        applied = False
        if apply_now:
            try:
                rcon.execute("exec server.cfg", old_password,
                             host=self.cfg.rcon_host, port=self.cfg.rcon_port)
                applied = True
            except rcon.RconError as e:
                warnings.append(f"Saved, but couldn't apply it live: {e}")
        self.invalidate()
        return {"applied": applied, "warnings": warnings, "version": _version(text)}
