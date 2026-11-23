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
MIN_SLOTS, MAX_SLOTS = 4, 12
MAX_LOG_LINES = 500
OVERVIEW_TTL = 2.0
CVAR_TTL = 10.0
JOB_LINES = 400

_WORKSHOP_ID_RE = re.compile(r"[?&]id=(\d{4,20})\b")
_CVAR_REPLY_RE = re.compile(r'"(z_difficulty|sv_cheats|mp_gamemode|l4d2_extras_tankrun)"\s*=\s*"([^"]*)"')
_MAX_PLAYERS_RE = re.compile(r"\((\d+) max\)")
_OFFICIAL_MAP_RE = re.compile(r"^c(\d{1,2})m\d{1,2}_")
_CONNECT_RE = re.compile(r"^\s*(?:In-game console:\s*)?(connect \S+)\s*$", re.MULTILINE)


def mode_label(mode):
    return MODE_LABELS.get(mode) or OTHER_MODE_LABELS.get(mode) or mode


def _cvar_on(value):
    """Whether a 0/1 cvar is on, read the way the game does (as a whole number)."""
    try:
        return int(float(value)) != 0
    except ValueError:
        return False


def valid_mode(mode):
    return isinstance(mode, str) and (mode in MODES or bool(_NUMBERED_MODE_RE.fullmatch(mode)))


class PanelError(Exception):
    """A request the panel refuses or could not carry out. Safe to show the user."""

    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


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
        self._cvars = (0.0, {})
        self._restart_reasons = {}  # reason -> when it arose
        self._write_lock = threading.Lock()  # one settings save at a time

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
            self.invalidate()
            if on_done:
                on_done(code)

        job_id = self.jobs.start(name, [self.cfg.l4d2_bin, *args], timeout, done)
        if job_id is None:
            running = self.jobs.snapshot() or {"name": "another job"}
            raise PanelError(f"Busy: '{running['name']}' is still running. Try again when it finishes.", 409)
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
        # The last one only exists with the admin extras plugin; without it the
        # console answers "Unknown command" and the name is simply not found.
        ok, out = self.try_rcon(f"z_difficulty; sv_cheats; mp_gamemode; {TANK_RUN_CVAR}")
        if not ok:
            return {}      # not cached: one failed query must not blank the toggles for CVAR_TTL
        found = dict(_CVAR_REPLY_RE.findall(out))
        self._cvars = (time.time(), found)
        return found

    def _index(self):
        try:
            data = json.loads(self._game_text(self.cfg.index_path, 4 * 1024 * 1024))
        except ValueError:
            return {}
        if not isinstance(data, dict):
            return {}
        return {key: item for key, item in data.items() if isinstance(item, dict)}

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
        return dict(base, job=self.jobs.snapshot())

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

        current = info.get("map", "").split(" at:")[0].strip()
        campaign_map, campaign = self._campaign(current)
        max_match = _MAX_PLAYERS_RE.search(info.get("players", ""))
        people = players.humans(everyone)
        # The console reports these in whatever case they were last typed in.
        difficulty = (cvars.get("z_difficulty") or serverconf.read_cvar(cfg_text, "z_difficulty") or "").capitalize()
        mode = (cvars.get("mp_gamemode") or env.get("L4D2_MODE", "")).lower()
        tank_run = cvars.get(TANK_RUN_CVAR)

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
            },
            "players": [{
                "userid": p.userid,
                "name": sanitize.display(p.name, 64),
                "steamid": p.uniqueid,
                "connected": p.connected,
                "ping": p.ping,
            } for p in people],
            "defaults": {"map": env.get("L4D2_MAP", ""), "mode": env.get("L4D2_MODE", ""),
                         "slots": self._slots(env)},
            "system": self._system(),
            "join": self._join(cfg_text),
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
                "maps": item.get("maps") or [],
                "url": item.get("url"),
                "source": item.get("source"),
                "file": item.get("file"),
                "missing": not found,
            })
        return {
            "official": [{"map": name, "name": title} for name, title in OFFICIAL],
            "custom": custom,
            "default": self.cfg.server_env.get("L4D2_MAP", ""),
        }

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
        return {"text": self._read_cfg(), "path": self.cfg.cfg_path}

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
            self.invalidate()
            if code != 0:
                raise PanelError(out or f"l4d2 {action} failed (exit {code}).", 502)
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
        self.rcon(f"changelevel {name}")
        self.invalidate()
        return {"message": f"Switching to {name}..."}

    def kick(self, userid, reason=""):
        if not re.fullmatch(r"\d{1,6}", str(userid)):
            raise PanelError("Bad player id.")
        # By userid, never by name: names are chosen by whoever joins.
        reason = sanitize.console_arg(reason, 80)
        self.rcon(f'kickid {userid} "{reason}"' if reason else f"kickid {userid}")
        self.invalidate()
        return {"message": "Player kicked."}

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
        return {"message": "Tank run on: tanks keep coming until this map ends." if on else "Tank run off."}

    def say(self, text):
        text = sanitize.console_arg(text)
        if not text:
            raise PanelError("Type a message first.")
        self.rcon(f'say "{text}"')
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
            self.rcon(f"sv_cheats {'1' if data['cheats'] else '0'}")
            changed.append("cheats")

        self.invalidate()
        self.log("settings changed: " + (", ".join(changed) or "nothing"))
        return {"changed": changed, "live_failed": live_failed,
                "restart_needed": mode is not None and mode != current["mode"]}

    def _write_cfg(self, text):
        # The save re-reads the file for the backup and writes nothing when that
        # fails: an unreadable or linked server.cfg is never replaced blind.
        try:
            self._game("save", path=self.cfg.cfg_path, text=text)
        except OSError as e:
            raise PanelError(f"Couldn't write server.cfg: {_why(e)}", 500)

    def save_raw_config(self, text, apply_now=False):
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
            self._write_cfg(text)
        self.log("server.cfg replaced from the config editor")
        warnings = []
        if not serverconf.read_cvar(text, "rcon_password"):
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
        return {"applied": applied, "warnings": warnings}
