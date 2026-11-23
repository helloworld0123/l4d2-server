#!/usr/bin/env python3
# =============================================================================
#  l4d2 - manage the Left 4 Dead 2 dedicated server (installed by l4d2-setup.sh)
#
#  Run "l4d2" or "l4d2 menu" for the interactive menu, "l4d2 help" for commands.
#  Settings shared with the systemd service live in /etc/l4d2.env
#
#  Privileges: this starts as root (systemd and /etc/l4d2.env need it) but
#  immediately switches its effective user to the game user and stays there.
#  Everything under the game user's home - maps, mods, server.cfg - is read and
#  written with that user's rights, so a link or odd path planted there can't
#  make root open, overwrite, chown or delete something else. Root is taken
#  back only inside `with root():`.
#
#  This guards against path tricks, not against a bug that runs code: the
#  process can still become root again, by design. For the same reason the
#  services that call this (bot, web panel) must not set NoNewPrivileges=, or
#  the `sudo -u` calls below stop working.
# =============================================================================
import contextlib
import fcntl
import hashlib
import io
import json
import os
import pwd
import re
import shutil
import stat
import struct
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.parse
import urllib.request
import zipfile

ENV_FILE = "/etc/l4d2.env"
STEAM_API = "https://api.steampowered.com/ISteamRemoteStorage/{}/v1/"
L4D2_APPID = 550
UA = {"User-Agent": "l4d2-server-helper/1.0"}
MAPNAME_RE = re.compile(r"^[A-Za-z0-9_\-]{1,64}$")
WT_TITLE = "L4D2 Server"

# ---- player-slot mods (8+ survivors in co-op) ----
SM_BRANCH = "1.12"   # SourceMod / Metamod:Source stable branch
SM_BASES = ["https://sm.alliedmods.net/smdrop", "https://www.sourcemod.net/smdrop"]
MM_BASES = ["https://mms.alliedmods.net/mmsdrop", "https://www.sourcemm.net/mmsdrop"]
#
# Every mod download is pinned to one exact version and checked against a
# SHA-256 recorded here, so what gets installed is what was reviewed - not
# whatever the upstream branch happens to hold today. To move to a newer
# version: change the name/commit, download it once by hand, put its
# `sha256sum` here, then run `sudo bash l4d2-setup.sh --upgrade`.
SM_FILE = "sourcemod-1.12.0-git7260-linux.tar.gz"
SM_SHA256 = "b01719e6143bc7d5bb63aae8086d031574da6ef838f041ba4b8ab72c8c75b888"
MM_FILE = "mmsource-1.12.0-git1230-linux.tar.gz"
MM_SHA256 = "22dab1bbd312025e700dd526f136e2f6e3efcda6fcf6c1b66c0319488f513882"
L4DTOOLZ_ZIP = "https://github.com/lakwsh/l4dtoolz/releases/download/2.5.1/l4dtoolz-2.5.1-main.zip"
L4DTOOLZ_SHA256 = "86c5461a69fd756b8c90d48553a7111a3d7814012aa0bd3c510aae5591fb1049"
# GitHub builds commit archives on demand and their bytes can change, so for
# Left4DHooks the files inside are checked rather than the zip itself.
L4DHOOKS_COMMIT = "7672bdefc4e03a693ae7fdc06e470df41b4cc069"
L4DHOOKS_ZIP = f"https://github.com/SilvDev/Left4DHooks/archive/{L4DHOOKS_COMMIT}.zip"
L4DHOOKS_FILES = {   # path under addons/sourcemod/ -> sha256
    "data/left4dhooks.l4d1.cfg": "c63e8228eb06bcad8ce4152e34a25b22206ab0d7ecc5032c7e21087a7b2b4b63",
    "data/left4dhooks.l4d2.cfg": "fecbe6d63e7736a49f6fa721d91ac71537da6e6f78870359584ede5eed2a3451",
    "gamedata/left4dhooks.l4d1.txt": "235dd88c6ade1175d9ddddf47c8e9c350c2e194ac492d82ecec6ff10f5e46c03",
    "gamedata/left4dhooks.l4d2.txt": "79c401eebe30ef2aff827b92d6b6c98288ad4c001f39a1945b4fdfdb7e10882b",
    "gamedata/lux_library.txt": "e2a0ec91d808180063b26a04c97737ee3f756133668f9b30e972e61880c7e94e",
    "plugins/left4dhooks.smx": "a5097583f718a58b249bec7aa2f7d019b03a9d70bb148dc4dbdc4646b43a1105",
}
# The last commit of this repository that still ships compiled plugins; they
# were deleted from its main branch on 2026-10-25.
HARRY_COMMIT = "2c180b24d85c4f053f1b3a60a9d29f6c8b001bfa"
HARRY_RAW = f"https://raw.githubusercontent.com/fbef0102/L4D1_2-Plugins/{HARRY_COMMIT}/"
# (repo path, path under addons/sourcemod/, sha256)
HARRY_FILES = [
    ("l4d_CreateSurvivorBot/plugins/l4d_CreateSurvivorBot.smx", "plugins/l4d_CreateSurvivorBot.smx",
     "e73f1d67af5217466496a49fdd605df067575e11ace7a709d3932b350d7662a4"),
    ("l4d_CreateSurvivorBot/gamedata/l4d_CreateSurvivorBot.txt", "gamedata/l4d_CreateSurvivorBot.txt",
     "58e63079f02ce88a85d04ad2df7ba6be4c008c2722d6800439a68617cde6774c"),
    ("l4dmultislots/plugins/l4dmultislots.smx", "plugins/l4dmultislots.smx",
     "a05a58dd824278e97b32714d0075e34da168b4d9ff91fb7a4d67d9a81126155f"),
    ("l4dmultislots/translations/l4dmultislots.phrases.txt", "translations/l4dmultislots.phrases.txt",
     "a0cc3bbbddbcd296a95c912341a28b2703557a8333b91aa2940a7a0e902aca51"),
    ("l4dafkfix_deadbot/plugins/l4dafkfix_deadbot.smx", "plugins/l4dafkfix_deadbot.smx",
     "fce5c5d3098020ce05208a8354502c076f49bd4ae4552aadfe20d0d6a14e953d"),
    ("l4d_both_fixUpgradePack/plugins/l4d_both_fixUpgradePack.smx", "plugins/l4d_both_fixUpgradePack.smx",
     "bc0b219c738617e850083ef6e68e841ac17a79bd91bc2dcd3b6cfd379299f724"),
    ("l4d_both_fixUpgradePack/gamedata/l4d_both_fixUpgradePack.txt", "gamedata/l4d_both_fixUpgradePack.txt",
     "48629cd9d573ebc30a92925fa4d68254b6fccee8337a9542ff8b978bd4bfb475"),
    ("l4d2_rescue_vehicle_multi/plugins/l4d2_rescue_vehicle_multi.smx", "plugins/l4d2_rescue_vehicle_multi.smx",
     "702f110b8cd28868df7269124f67d9d548252496ebb011cc2fdd77cd60ba1963"),
    ("l4d2_rescue_vehicle_multi/gamedata/l4d2_rescue_vehicle_multi.txt", "gamedata/l4d2_rescue_vehicle_multi.txt",
     "2f84e9c5804945dd00c362295495517493493a617042993ef05fa67c272fa39b"),
    ("l4d_full_slot_bot_replace_fix/plugins/l4d_full_slot_bot_replace_fix.smx", "plugins/l4d_full_slot_bot_replace_fix.smx",
     "9d6c04908a3d0970657862b600707968e57f948324439fdcb7eac01306e0790c"),
    ("l4d_full_slot_bot_replace_fix/gamedata/l4d_full_slot_bot_replace_fix.txt", "gamedata/l4d_full_slot_bot_replace_fix.txt",
     "6f8943453fab3d9264f8bb34efdb5dbde785d4510aacadddfc04b9c5a66a68ba"),
]
SLOT_PLUGINS = [os.path.basename(d) for _, d, _ in HARRY_FILES if d.endswith(".smx")]
LOCK_FILE = "/run/l4d2.lock"
KEEP_BACKUPS = 10
CFG_BEGIN = "// >>> player slots - managed by 'l4d2 slots', do not edit by hand"
CFG_END = "// <<< player slots"
MIN_SLOTS, MAX_SLOTS = 4, 12

OFFICIAL = [
    ("c1m1_hotel", "Dead Center"), ("c2m1_highway", "Dark Carnival"),
    ("c3m1_plankcountry", "Swamp Fever"), ("c4m1_milltown_a", "Hard Rain"),
    ("c5m1_waterfront", "The Parish"), ("c6m1_riverbank", "The Passing"),
    ("c7m1_docks", "The Sacrifice"), ("c8m1_apartment", "No Mercy"),
    ("c9m1_alleys", "Crash Course"), ("c10m1_caves", "Death Toll"),
    ("c11m1_greenhouse", "Dead Air"), ("c12m1_hilltop", "Blood Harvest"),
    ("c13m1_alpinecreek", "Cold Stream"), ("c14m1_junkyard", "The Last Stand"),
]


# ----------------------------------------------------------------- settings
def load_env(path=ENV_FILE):
    env = {}
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip('"')
    return env


def set_env(key, value):
    value = str(value)
    # bash sources this file as root, so values are allowlisted, not escaped.
    if not re.fullmatch(r"[A-Za-z0-9_.:/-]*", value):
        die(f"refusing to store {key}={value!r} in {ENV_FILE}")
    with root():
        with open(ENV_FILE) as f:
            lines = f.read().splitlines()
        for i, line in enumerate(lines):
            if line.startswith(key + "="):
                lines[i] = f"{key}={value}"
                break
        else:
            lines.append(f"{key}={value}")
        fd, tmp = tempfile.mkstemp(prefix=".l4d2-env-", dir=os.path.dirname(ENV_FILE))
        with os.fdopen(fd, "w") as f:
            f.write("\n".join(lines) + "\n")
            os.fchmod(f.fileno(), 0o644)
        os.replace(tmp, ENV_FILE)
    ENV[key] = value


def drop_privileges():
    """Become the game user for everything except what `with root():` wraps."""
    if os.geteuid() != 0:
        return
    pw = pwd.getpwnam(USER)
    os.setgroups([pw.pw_gid])
    os.setegid(pw.pw_gid)
    os.seteuid(pw.pw_uid)
    os.umask(0o022)


@contextlib.contextmanager
def root():
    """Briefly take root back. A no-op when we never had it (tests)."""
    if os.getuid() != 0 or os.geteuid() == 0:
        yield
        return
    uid, gid = os.geteuid(), os.getegid()
    try:
        os.seteuid(0)
        os.setegid(0)
        yield
    finally:
        os.seteuid(0)                 # needed before the group can be changed back
        os.setegid(gid)
        os.seteuid(uid)


def systemctl(action):
    with root():
        return subprocess.run(["systemctl", action, "l4d2"]).returncode


_lock_handle = None


@contextlib.contextmanager
def locked(what):
    """One state-changing command at a time, across the shell, the bot and the web panel."""
    global _lock_handle
    if _lock_handle is not None:      # already ours (a menu action calling another command)
        yield
        return
    with root():
        handle = open(LOCK_FILE, "a+")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.seek(0)
        other = handle.read().strip() or "another l4d2 command"
        handle.close()
        print(f"Busy: '{other}' is still running. Try again when it finishes.", file=sys.stderr)
        sys.exit(75)
    _lock_handle = handle
    try:
        handle.seek(0)
        handle.truncate()
        handle.write(what)
        handle.flush()
        yield
    finally:
        _lock_handle = None
        try:
            handle.seek(0)
            handle.truncate()
        finally:
            handle.close()            # closing releases the lock


ENV = {}
USER = HOME = DIR = GAME = ADDONS = CFG = CONLOG = INDEX = INFO = STEAMCMD = SMDIR = ""


def init_paths():
    global ENV, USER, HOME, DIR, GAME, ADDONS, CFG, CONLOG, INDEX, INFO, STEAMCMD, SMDIR
    ENV = load_env()
    USER = ENV.get("L4D2_USER", "l4d2")
    HOME = ENV.get("L4D2_HOME", f"/home/{USER}")
    DIR = ENV.get("L4D2_DIR", f"{HOME}/l4d2server")
    STEAMCMD = ENV.get("L4D2_STEAMCMD", "/usr/games/steamcmd")
    GAME = os.path.join(DIR, "left4dead2")
    ADDONS = os.path.join(GAME, "addons")
    SMDIR = os.path.join(ADDONS, "sourcemod")
    CFG = os.path.join(DIR, "left4dead2", "cfg", "server.cfg")
    CONLOG = os.path.join(DIR, "left4dead2", "console.log")
    INDEX = os.path.join(HOME, "maps.json")
    INFO = os.path.join(HOME, "SERVER-INFO.txt")


# ----------------------------------------------------------------- small helpers
def die(msg):
    print(f"Error: {msg}", file=sys.stderr)
    sys.exit(1)


def human(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit in ("B", "KB") else f"{n:.1f} {unit}"
        n /= 1024


def as_user(cmd):
    return ["sudo", "-u", USER, "-H"] + cmd


def ask_yes(prompt):
    if not sys.stdin.isatty():
        return False
    return input(f"{prompt} [y/N] ").strip().lower() in ("y", "yes")


def new_tmp():
    """A private scratch directory in the game user's home (same filesystem as addons)."""
    return tempfile.mkdtemp(prefix=".l4d2-tmp-", dir=HOME)


def addon_path(name):
    """Full path of an addon file, or None unless name is a plain .vpk file name.

    Names come from maps.json, which the game user can edit: without this an
    entry like "/etc/passwd" or "../../x" would be opened or deleted.
    """
    if (not isinstance(name, str) or not name or name != os.path.basename(name) or "\x00" in name
            or name.startswith(".") or not name.lower().endswith(".vpk")):
        return None
    return os.path.join(ADDONS, name)


def admin_isfile(path):
    """Does this file exist, as seen by the admin who typed it (not by the game user)?"""
    with root():
        return os.path.isfile(path)


def addon_exists(name):
    path = addon_path(name)
    return bool(path) and os.path.isfile(path)


def read_file(path, default=""):
    try:
        with open(path, errors="replace") as f:
            return f.read()
    except OSError:
        return default


def safe_name(name):
    name = re.sub(r"[^A-Za-z0-9._-]", "_", os.path.basename(name))
    return name if name.lower().endswith(".vpk") else name + ".vpk"


def load_index():
    try:
        with open(INDEX) as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {k: v for k, v in data.items() if isinstance(v, dict)}


def save_index(idx):
    write_file(INDEX, json.dumps(idx, indent=2, sort_keys=True).encode())


def parse_workshop_id(s):
    m = re.search(r"[?&]id=(\d+)", s)
    if m:
        return m.group(1)
    if re.fullmatch(r"\d{4,20}", s):
        return s
    return None


# ----------------------------------------------------------------- server state
def server_pid():
    r = subprocess.run(["pgrep", "-u", USER, "-f", "srcds_linux"], capture_output=True, text=True)
    pids = r.stdout.split()
    return int(pids[0]) if pids else None


def server_started_at():
    pid = server_pid()
    if not pid:
        return None
    r = subprocess.run(["ps", "-o", "etimes=", "-p", str(pid)], capture_output=True, text=True)
    try:
        return time.time() - int(r.stdout.strip())
    except ValueError:
        return None


def pending_addons():
    """Addons added/changed after the server started (not loaded until restart)."""
    start = server_started_at()
    if start is None or not os.path.isdir(ADDONS):
        return []
    return [f for f in os.listdir(ADDONS)
            if f.lower().endswith(".vpk") and os.path.getmtime(os.path.join(ADDONS, f)) > start]


def send_console(command):
    return subprocess.run(as_user(["tmux", "send-keys", "-t", "l4d2", command, "Enter"])).returncode == 0


def restart_server():
    print("Restarting the server...")
    if systemctl("restart") != 0:
        print("systemd could not restart the service. Check: journalctl -u l4d2 -n 30")
        return False
    for _ in range(20):
        if server_pid():
            print("Server is back up (the map takes a few more seconds to load).")
            return True
        time.sleep(2)
    print("Server didn't come back. Check: l4d2 logs")
    return False


def maybe_restart(flags, what):
    if "--restart" in flags:
        restart_server()
    elif "--no-restart" in flags or not server_pid():
        if server_pid():
            print(f"Run 'l4d2 restart' to load {what}.")
    elif ask_yes(f"Restart the server now to load {what}? Anyone playing will be disconnected."):
        restart_server()
    else:
        print(f"OK. Run 'l4d2 restart' later to load {what}.")


# ----------------------------------------------------------------- VPK reading
def parse_mission(raw):
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        text = raw.decode("utf-16", errors="replace")
    else:
        text = raw.decode("utf-8", errors="replace").lstrip("\ufeff")
    title = None
    m = re.search(r'"DisplayTitle"\s+"([^"]*)"', text, re.I)
    if m and m.group(1).strip() and not m.group(1).startswith("#"):
        title = m.group(1).strip()
    first = None
    for mode in ("coop", "versus", "survival", "scavenge"):
        m = re.search(r'"' + mode + r'"\s*\{(?:\s|//[^\n]*)*"1"\s*\{(.*?)\}', text, re.I | re.S)
        if m:
            mm = re.search(r'"Map"\s+"([^"]+)"', m.group(1), re.I)
            if mm and MAPNAME_RE.match(mm.group(1).strip()):
                first = mm.group(1).strip()
                break
    return title, first


def vpk_info(path):
    """Return {'maps': [...], 'campaign': str|None, 'first_map': str|None}, or None if not a VPK."""
    try:
        with open(path, "rb") as f:
            hdr = f.read(12)
            if len(hdr) < 12:
                return None
            sig, ver, tree_size = struct.unpack("<III", hdr)
            if sig != 0x55AA1234 or ver not in (1, 2):
                return None
            hlen = 12 if ver == 1 else 28
            f.seek(hlen)
            tree = f.read(tree_size)
            pos = 0

            def rs():
                nonlocal pos
                end = tree.index(b"\0", pos)
                s = tree[pos:end].decode("utf-8", "replace")
                pos = end + 1
                return s

            entries = {}
            while True:
                ext = rs()
                if not ext:
                    break
                while True:
                    d = rs()
                    if not d:
                        break
                    while True:
                        n = rs()
                        if not n:
                            break
                        _crc, pre, arch, off, length, _term = struct.unpack_from("<IHHIIH", tree, pos)
                        pos += 18
                        preload = tree[pos:pos + pre]
                        pos += pre
                        full = (n if d.strip() == "" else f"{d}/{n}") + f".{ext}"
                        entries[full.lower()] = (preload, arch, off, length)

            data_base = hlen + tree_size

            def read_entry(e):
                preload, arch, off, length = e
                if arch != 0x7FFF or length == 0:
                    return preload
                f.seek(data_base + off)
                return preload + f.read(length)

            maps = sorted(k[5:-4] for k in entries if k.startswith("maps/") and k.endswith(".bsp") and "/" not in k[5:])
            title = first = None
            for k in sorted(entries):
                if k.startswith("missions/") and k.endswith(".txt"):
                    title, first = parse_mission(read_entry(entries[k]))
                    if first:
                        break
            if not first and maps:
                first = maps[0]
            return {"maps": maps, "campaign": title, "first_map": first}
    except (OSError, ValueError, struct.error):
        return None


# ----------------------------------------------------------------- Steam Workshop
def steam_api(endpoint, fields):
    body = urllib.parse.urlencode(fields).encode()
    req = urllib.request.Request(STEAM_API.format(endpoint), data=body, headers=UA)
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r).get("response", {})


def expand_collections(ids):
    fields = {"collectioncount": len(ids)}
    for i, x in enumerate(ids):
        fields[f"publishedfileids[{i}]"] = x
    try:
        resp = steam_api("GetCollectionDetails", fields)
    except Exception:
        return ids
    out = []
    for d in resp.get("collectiondetails", []):
        kids = [c["publishedfileid"] for c in d.get("children", [])] if d.get("result") == 1 else []
        if kids:
            print(f"Collection {d['publishedfileid']}: {len(kids)} items")
            out += kids
        else:
            out.append(str(d.get("publishedfileid")))
    seen = set()
    return [x for x in (out or ids) if not (x in seen or seen.add(x))]


def get_details(ids):
    fields = {"itemcount": len(ids)}
    for i, x in enumerate(ids):
        fields[f"publishedfileids[{i}]"] = x
    resp = steam_api("GetPublishedFileDetails", fields)
    return {str(d.get("publishedfileid")): d for d in resp.get("publishedfiledetails", [])}


def download(url, dest, expected=0):
    req = urllib.request.Request(url, headers=UA)
    tty = sys.stdout.isatty()
    with urllib.request.urlopen(req, timeout=60) as r, open(dest, "wb") as f:
        total = int(r.headers.get("Content-Length") or 0) or expected
        done, last = 0, 0.0
        while True:
            chunk = r.read(1 << 20)
            if not chunk:
                break
            f.write(chunk)
            done += len(chunk)
            if tty and time.time() - last > 0.3:
                pct = f"{done * 100 // total:3d}% " if total else ""
                tot = f" / {human(total)}" if total else ""
                print(f"\r  {pct}{human(done)}{tot}      ", end="", flush=True)
                last = time.time()
    if tty:
        print(f"\r  done: {human(done)}                    ")
    if total and done < total:
        raise IOError(f"incomplete download ({human(done)} of {human(total)})")


def steamcmd_download(wid):
    if not os.path.exists(STEAMCMD):
        return None
    print("  Trying SteamCMD...")
    os.chdir("/tmp")
    subprocess.run(as_user([STEAMCMD, "+force_install_dir", DIR, "+login", "anonymous",
                            "+workshop_download_item", str(L4D2_APPID), wid, "+quit"]),
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    d = os.path.join(DIR, "steamapps", "workshop", "content", str(L4D2_APPID), wid)
    best = None
    for root, _, files in os.walk(d):
        for fn in files:
            p = os.path.join(root, fn)
            if vpk_info(p) is not None and (best is None or os.path.getsize(p) > os.path.getsize(best)):
                best = p
    return best


# ----------------------------------------------------------------- install / remove
def install_vpk(src, dest_name, key, meta):
    info = vpk_info(src)
    if info is None:
        print(f"  x {meta.get('title', dest_name)}: not a valid .vpk file, skipped.")
        return None
    os.makedirs(ADDONS, exist_ok=True)
    dest = addon_path(dest_name)
    if dest is None:
        print(f"  x {dest_name}: not a usable addon file name, skipped.")
        return None
    os.chmod(src, 0o644)
    os.utime(src, None)           # mark as "newer than the running server"
    os.replace(src, dest)         # same filesystem: atomic, and replaces a link rather than following it
    meta.update(file=dest_name, maps=info["maps"], first_map=info["first_map"],
                campaign=info["campaign"] or meta.get("title"), added=int(time.time()))
    idx = load_index()
    idx[key] = meta
    save_index(idx)
    if info["maps"]:
        print(f"  OK  {meta['campaign']}  ->  start map: {info['first_map']}  ({len(info['maps'])} maps)")
    else:
        print(f"  OK  {meta['campaign']}  (no maps inside - a mod/skin/script addon)")
    return meta


def add_local(path):
    tmpd = new_tmp()
    try:
        srcs = []
        low = path.lower()
        if not low.endswith((".zip", ".vpk")):
            print(f"x {path}: only .vpk or .zip files are supported.")
            return []
        # The admin named this file, so it is opened with their (root) rights -
        # it may sit in a home directory the game user can't read. Everything
        # written from it goes into the game user's scratch directory.
        with root():
            source = open(path, "rb")
        with source:
            if low.endswith(".zip"):
                with zipfile.ZipFile(source) as z:
                    for n in z.namelist():
                        if n.lower().endswith(".vpk") and not n.endswith("/"):
                            target = os.path.join(tmpd, safe_name(n))
                            with z.open(n) as s, open(target, "wb") as d:
                                shutil.copyfileobj(s, d)
                            srcs.append(target)
                if not srcs:
                    print(f"x {path}: no .vpk files inside this zip.")
            else:
                target = os.path.join(tmpd, safe_name(path))
                with open(target, "wb") as d:
                    shutil.copyfileobj(source, d)
                srcs.append(target)
        added = []
        for s in srcs:
            name = os.path.basename(s)
            print(f"+ {name}")
            r = install_vpk(s, name, "file:" + name.lower(),
                            {"source": "file", "title": os.path.splitext(name)[0]})
            if r:
                added.append(r)
        return added
    finally:
        shutil.rmtree(tmpd, ignore_errors=True)


def add_workshop(ids, force=False):
    ids = expand_collections(ids)
    try:
        details = get_details(ids)
    except Exception as e:
        print(f"Could not reach the Steam Workshop API ({e}). Will try SteamCMD instead.")
        details = {}
    idx = load_index()
    added = []
    for wid in ids:
        d = details.get(wid)
        title = (d or {}).get("title") or f"Workshop item {wid}"
        if d is not None and d.get("result") != 1:
            print(f"x {wid}: not found, private, or removed from the Workshop.")
            continue
        if d and int(d.get("consumer_app_id") or 0) not in (0, L4D2_APPID):
            print(f"x {title}: this is not a Left 4 Dead 2 Workshop item.")
            continue
        old = idx.get(wid)
        if (old and not force and d and old.get("time_updated") == d.get("time_updated")
                and addon_exists(old.get("file"))):
            print(f"= {title}: already installed and up to date.")
            continue
        print(f"+ {title}  ({wid})")
        tmpd = new_tmp()
        try:
            src = None
            url = (d or {}).get("file_url")
            if url:
                src = os.path.join(tmpd, f"{wid}.vpk")
                try:
                    download(url, src, int((d or {}).get("file_size") or 0))
                except Exception as e:
                    print(f"  Direct download failed ({e}).")
                    src = None
            if not src:
                src = steamcmd_download(wid)
            if not src:
                print(f"  x Could not download it. Get the .vpk on your PC and run: l4d2 addmap /path/to/file.vpk")
                continue
            meta = {"source": "workshop", "id": wid, "title": title,
                    "time_updated": (d or {}).get("time_updated"),
                    "url": f"https://steamcommunity.com/sharedfiles/filedetails/?id={wid}"}
            r = install_vpk(src, f"{wid}.vpk", wid, meta)
            if r:
                added.append(r)
        finally:
            shutil.rmtree(tmpd, ignore_errors=True)
    return added


def add_targets(targets, force=False):
    ws, added = [], []
    for t in targets:
        if admin_isfile(t):
            added += add_local(t)
        elif parse_workshop_id(t):
            ws.append(parse_workshop_id(t))
        else:
            print(f"x {t}: not a Workshop link/ID or an existing .vpk/.zip file.")
    if ws:
        added += add_workshop(ws, force)
    return added


# ----------------------------------------------------------------- commands
def cmd_addmap(args):
    flags = [a for a in args if a.startswith("--")]
    targets = [a for a in args if not a.startswith("--")]
    if not targets:
        die("usage: l4d2 addmap <workshop link or id | collection link | file.vpk | file.zip> ...")
    added = add_targets(targets, force="--force" in flags)
    if not added:
        print("Nothing new was installed.")
        return
    seen = set()
    maps = [m for m in added if m.get("first_map") and not (m["first_map"] in seen or seen.add(m["first_map"]))]
    if maps:
        print("\nTo play one, restart (if asked) and then run:")
        for m in maps:
            print(f"   l4d2 changemap {m['first_map']}     # {m['campaign']}")
    if any(m.get("source") == "workshop" for m in added):
        print("\nFriends need the same maps - run 'l4d2 share' for links to send them.")
    maybe_restart(flags, "the new maps")


def cmd_maps(_args=None):
    idx = load_index()
    print(f"Default starting map: {ENV.get('L4D2_MAP')}  (mode: {ENV.get('L4D2_MODE')})\n")
    if not idx:
        print("No custom maps installed yet.  Add one with:  l4d2 addmap <workshop link>")
        return
    for key, m in sorted(idx.items(), key=lambda kv: (kv[1].get("campaign") or "").lower()):
        missing = "" if addon_exists(m.get("file")) else "   [FILE MISSING]"
        print(f"{m.get('campaign') or m.get('title')}{missing}")
        print(f"    start map : {m.get('first_map') or '(none - not a map)'}")
        print(f"    source    : {m.get('url') or m.get('file')}")
    pend = pending_addons()
    if pend:
        print(f"\n{len(pend)} addon(s) added since the server started - run 'l4d2 restart' to load them.")


def share_text():
    idx = load_index()
    ws = [m for m in idx.values() if m.get("source") == "workshop"]
    files = [m for m in idx.values() if m.get("source") != "workshop"]
    if not idx:
        return "No custom maps installed - friends don't need anything extra."
    lines = ["Subscribe to these on the Steam Workshop before joining,",
             "then start L4D2 once so they download:", ""]
    for m in ws:
        lines += [f"  {m.get('title')}", f"  {m.get('url')}", ""]
    if files:
        lines += ["These were installed from files - send friends the same file",
                  "for their left4dead2\\addons folder:"]
        lines += [f"  {m.get('file')}" for m in files]
    return "\n".join(lines)


def find_items(arg):
    idx = load_index()
    wid = parse_workshop_id(arg)
    a = arg.lower()
    return [k for k, m in idx.items()
            if k == arg or (wid and m.get("id") == wid) or m.get("file", "").lower() == a
            or (m.get("first_map") or "").lower() == a]


def cmd_removemap(args):
    flags = [a for a in args if a.startswith("--")]
    targets = [a for a in args if not a.startswith("--")]
    if not targets:
        die("usage: l4d2 removemap <workshop id | file name | start map name>")
    idx = load_index()
    removed = False
    for t in targets:
        keys = find_items(t)
        if not keys:
            print(f"x {t}: not found. See installed maps with: l4d2 maps")
            continue
        for k in keys:
            m = idx.pop(k)
            target = addon_path(m.get("file"))
            if target is None:
                print(f"  ! {k}: its recorded file name ({m.get('file')!r}) is not an addon file - "
                      "nothing was deleted from disk.")
            else:
                try:
                    os.remove(target)
                except FileNotFoundError:
                    pass
            print(f"- Removed {m.get('campaign') or m.get('title')}")
            removed = True
            if ENV.get("L4D2_MAP") in (m.get("maps") or []):
                set_env("L4D2_MAP", "c1m1_hotel")
                print("  (it was the default starting map - reset to c1m1_hotel)")
    save_index(idx)
    if removed:
        maybe_restart(flags, "the change")


def known_maps():
    names = {m for m, _ in OFFICIAL}
    for m in load_index().values():
        names.update(m.get("maps") or [])
    return {n.lower() for n in names}


def cmd_changemap(args, from_menu=False):
    flags = [a for a in args if a.startswith("--")]
    rest = [a for a in args if not a.startswith("--")]
    make_default = "--default" in flags
    if rest:
        name = rest[0]
    else:
        name = pick_map()
        if not name:
            return
        make_default = wt_yesno(f"Also make {name} the default map the server starts on?")
    if not MAPNAME_RE.match(name):
        die(f"'{name}' doesn't look like a map name.")
    if name.lower() not in known_maps():
        print(f"Note: '{name}' isn't an official or installed map I know of - trying anyway.")
    if make_default:
        set_env("L4D2_MAP", name)
        print(f"Default starting map set to {name}.")
    if not server_pid():
        print("The server isn't running." + (" It will start on this map: l4d2 start" if make_default else ""))
        return
    if pending_addons():
        print("New addons were added since the server started; they load only after a restart.")
        if make_default or ask_yes(f"Restart now and start on {name}? (makes it the default map)"):
            if not make_default:
                set_env("L4D2_MAP", name)
            restart_server()
        return
    if send_console(f"changelevel {name}"):
        print(f"Switching to {name}... players will be moved along automatically.")
    else:
        print("Couldn't reach the server console. Try: l4d2 restart")


def cmd_updatemaps(args):
    flags = [a for a in args if a.startswith("--")]
    ids = [k for k, m in load_index().items() if m.get("source") == "workshop"]
    if not ids:
        print("No Workshop maps installed.")
        return []
    print(f"Checking {len(ids)} Workshop item(s) for updates...")
    added = add_workshop(ids)
    if added:
        maybe_restart(flags, "the updated maps")
    else:
        print("All Workshop maps are up to date.")
    return added


def cmd_update(_args):
    """Returns 0 when everything updated, 1 when any step failed."""
    problems = []
    print("Stopping the server and updating game files (this can take a few minutes)...")
    systemctl("stop")
    os.chdir("/tmp")
    steam = subprocess.run(as_user([STEAMCMD, "+force_install_dir", DIR, "+login", "anonymous",
                                    "+app_update", "222860", "validate", "+quit"])).returncode
    if steam != 0 or not os.path.exists(os.path.join(DIR, "srcds_run")):
        problems.append(f"SteamCMD did not finish cleanly (exit {steam}) - the game files may be out of date")
    try:
        cmd_updatemaps(["--no-restart"])
    except Exception as e:
        problems.append(f"Workshop maps were not checked ({e})")
    if mods_installed():
        try:
            install_mods(update=True)
        except PermissionError:
            raise                     # not a download problem: main() explains the fix
        except Exception as e:
            problems.append(f"mods were not refreshed ({e})")
    if systemctl("start") != 0:
        problems.append("the server did not start - check: journalctl -u l4d2 -n 30")
    if problems:
        print("\nUpdate finished WITH PROBLEMS:")
        for p in problems:
            print(f"  ! {p}")
        print("Run 'l4d2 update' again once the cause is fixed.")
        return 1
    print("Update finished, server started.")
    return 0


def status_text():
    active = subprocess.run(["systemctl", "is-active", "l4d2"], capture_output=True, text=True).stdout.strip()
    running = "running" if server_pid() else "NOT running"
    idx = load_index()
    pend = pending_addons()
    lines = [f"Service       : {active}", f"Game process  : {running}",
             f"Default map   : {ENV.get('L4D2_MAP')}  ({ENV.get('L4D2_MODE')})",
             f"Custom addons : {len(idx)} installed",
             f"Co-op slots   : {current_slots()}" + (" (MultiSlots)" if current_slots() > 4 else "")]
    if pend:
        lines.append(f"Pending       : {len(pend)} new addon(s) - restart to load")
    return "\n".join(lines)


def cmd_logs(_args):
    if not os.path.exists(CONLOG):
        print("No log yet - start the server first.")
        return
    os.execvp("tail", ["tail", "-n", "50", "-f", CONLOG])


def cmd_console(_args):
    if not server_pid():
        die("the server isn't running.")
    print("Attaching to the live server console.")
    print("Detach with Ctrl+B then D.  Do NOT type 'quit' (that stops the server).")
    time.sleep(2)
    os.execvp("sudo", as_user(["tmux", "attach", "-t", "l4d2"]))


def cmd_config(_args):
    # The editor runs as the game user: server.cfg is theirs, and an editor
    # running as root would follow a link planted at that path.
    editor = os.environ.get("EDITOR", "nano").split()
    subprocess.run(as_user(editor + [CFG]))
    if ask_yes("Restart the server to apply the config?"):
        restart_server()


# ----------------------------------------------------------------- player slots (mods)
def fetch(url, timeout=60):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def verified(data, sha256, what):
    """Return data only if it is byte-for-byte the version pinned at the top of this file."""
    actual = hashlib.sha256(data).hexdigest()
    if actual != sha256:
        raise IOError(f"{what} does not match its pinned checksum "
                      f"(expected {sha256[:16]}..., got {actual[:16]}...). Nothing was installed from it.")
    return data


def fetch_pinned(bases, name, sha256):
    last = None
    for base in bases:
        try:
            return verified(fetch(f"{base}/{SM_BRANCH}/{name}"), sha256, name)
        except Exception as e:
            last = e
    raise IOError(f"could not download {name} ({last})")


def safe_members(t, update):
    for m in t.getmembers():
        name = m.name[2:] if m.name.startswith("./") else m.name
        parts = name.split("/")
        if name.startswith("/") or ".." in parts or not (m.isfile() or m.isdir()):
            continue
        # on updates keep the user's configs (admins, cvars)
        if update and (name.startswith("cfg/") or name.startswith("addons/sourcemod/configs/")):
            continue
        yield m


def extract_tar(data, dest, update):
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as t:
        members = list(safe_members(t, update))
        try:
            t.extractall(dest, members=members, filter="data")
        except TypeError:          # Python without extraction filters
            t.extractall(dest, members=members)


def write_file(path, data, mode=0o644):
    """Replace path atomically, through a fresh temp file - never by opening path itself."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    try:
        info = os.lstat(path)
        if stat.S_ISREG(info.st_mode):
            mode = stat.S_IMODE(info.st_mode)
    except OSError:
        pass
    fd, tmp = tempfile.mkstemp(prefix=".l4d2-", dir=os.path.dirname(path))
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            os.fchmod(f.fileno(), mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def install_metamod(update):
    print(f"  Metamod:Source   {MM_FILE}")
    extract_tar(fetch_pinned(MM_BASES, MM_FILE, MM_SHA256), GAME, update)
    # L4D2 needs this exact loader path; drop the 64-bit loader (L4D2 is a 32-bit server)
    write_file(os.path.join(ADDONS, "metamod.vdf"),
               b'"Plugin"\n{\n\t"file"\t"../left4dead2/addons/metamod/bin/server"\n}\n')
    for extra in ("metamod_x64.vdf",):
        try:
            os.remove(os.path.join(ADDONS, extra))
        except FileNotFoundError:
            pass
    if not os.path.exists(os.path.join(ADDONS, "metamod", "bin", "server.so")):
        print("  ! Warning: addons/metamod/bin/server.so not found - Metamod may not load.")


def install_sourcemod(update):
    print(f"  SourceMod        {SM_FILE}")
    extract_tar(fetch_pinned(SM_BASES, SM_FILE, SM_SHA256), GAME, update)


def install_l4dtoolz():
    print(f"  L4DToolZ         {L4DTOOLZ_ZIP.rsplit('/', 2)[-2]}")
    data = verified(fetch(L4DTOOLZ_ZIP), L4DTOOLZ_SHA256, "L4DToolZ")
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        for n in ("l4dtoolz.so", "l4dtoolz.vdf"):
            write_file(os.path.join(ADDONS, n), z.read(n))


def install_left4dhooks():
    print(f"  Left4DHooks      {L4DHOOKS_COMMIT[:10]}")
    with zipfile.ZipFile(io.BytesIO(fetch(L4DHOOKS_ZIP))) as z:
        found = {}
        for n in z.namelist():
            rel = n.split("/", 2)[-1]          # drop "<repo>-<commit>/sourcemod/"
            if n.split("/")[1:2] == ["sourcemod"] and rel in L4DHOOKS_FILES:
                found[rel] = verified(z.read(n), L4DHOOKS_FILES[rel], f"Left4DHooks {rel}")
    missing = sorted(set(L4DHOOKS_FILES) - set(found))
    if missing:
        raise IOError(f"Left4DHooks archive is missing {', '.join(missing)}")
    for rel, data in found.items():            # only after every file checked out
        write_file(os.path.join(SMDIR, *rel.split("/")), data)


def install_harry_plugins():
    """Install or refresh the MultiSlots plugins without switching them on or off.

    Each one lands in plugins/ when the slot setting is above 4 and in
    plugins/disabled/ otherwise; only switch_slot_plugins() moves them.
    """
    print(f"  MultiSlots + 5+ survivor fixes   {HARRY_COMMIT[:10]}")
    files = [(dest, verified(fetch(HARRY_RAW + src), sha, os.path.basename(src)))
             for src, dest, sha in HARRY_FILES]
    plugins = os.path.join(SMDIR, "plugins")
    live = current_slots() > 4
    for dest, data in files:                   # only after every file checked out
        if not dest.endswith(".smx"):
            write_file(os.path.join(SMDIR, dest), data)
            continue
        name = os.path.basename(dest)
        on, off = os.path.join(plugins, name), os.path.join(plugins, "disabled", name)
        write_file(on if live else off, data)
        stale = off if live else on
        if os.path.exists(stale):
            os.remove(stale)


def switch_slot_plugins(on):
    """Move the MultiSlots plugins between plugins/ and plugins/disabled/."""
    plugins = os.path.join(SMDIR, "plugins")
    if not os.path.isdir(plugins):
        return
    os.makedirs(os.path.join(plugins, "disabled"), exist_ok=True)
    for p in SLOT_PLUGINS:
        live, parked = os.path.join(plugins, p), os.path.join(plugins, "disabled", p)
        src, dst = (parked, live) if on else (live, parked)
        if os.path.exists(src):
            os.replace(src, dst)


# Plugins of our own, built on the server with the compiler that ships in the
# pinned SourceMod (so there is no extra download to trust). Their sources are
# installed next to this command by l4d2-setup.sh, from cli/plugins/:
#
#   l4d2_precache_survivors  A bot can be handed any of the eight survivors.
#       The game only loads the four that belong to the campaign, and
#       "sm_cvar precache_all_survivors 1" in server.cfg takes effect *after*
#       the first map has loaded - so right after a start, the first Bill/Zoey/
#       Francis/Louis bot crashed the server ("UTIL_SetModel: not precached").
#       This loads all eight on every map, the first included.
#   l4d2_admin_extras        Chat commands for admins: !extras, !difficulty,
#       !mode, !tank, !witch, !horde, !tankrun, !restartmap.
PLUGIN_SRC = "/usr/local/share/l4d2/plugins"
OWN_PLUGINS = ("l4d2_precache_survivors", "l4d2_admin_extras")


def install_own_plugins():
    print("  Toolkit plugins  (built here with SourceMod's compiler)")
    scripting = os.path.join(SMDIR, "scripting")
    compiler = next((p for p in (os.path.join(scripting, "spcomp64"), os.path.join(scripting, "spcomp"))
                     if os.path.isfile(p)), None)
    if not compiler:
        raise RuntimeError("SourceMod's plugin compiler (scripting/spcomp) is missing")
    if not os.access(compiler, os.X_OK):
        os.chmod(compiler, 0o755)
    sources = {}
    for name in OWN_PLUGINS:
        try:
            with open(os.path.join(PLUGIN_SRC, name + ".sp"), "rb") as f:
                sources[name] = f.read()
        except OSError:
            raise RuntimeError(f"{PLUGIN_SRC}/{name}.sp is missing. Refresh the install with: "
                               "sudo bash l4d2-setup.sh --upgrade")
    tmp = new_tmp()
    try:
        built = {}
        for name, source in sources.items():
            path = os.path.join(scripting, name + ".sp")
            write_file(path, source)
            out = os.path.join(tmp, name + ".smx")
            cmd = [compiler, path, "-o", out]
            result = subprocess.run(as_user(cmd) if os.getuid() == 0 else cmd, cwd=scripting,
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=120)
            if result.returncode != 0 or not os.path.isfile(out):
                tail = " | ".join(result.stdout.strip().splitlines()[-3:])
                raise RuntimeError(f"could not build the {name} plugin: {tail or 'no output'}")
            with open(out, "rb") as f:
                built[name] = f.read()
        for name, data in built.items():           # only after every plugin built
            write_file(os.path.join(SMDIR, "plugins", name + ".smx"), data)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def mods_installed():
    return os.path.isdir(SMDIR) and os.path.isdir(os.path.join(ADDONS, "metamod"))


def install_mods(update=False):
    """Download/refresh everything. update=True keeps existing SourceMod configs."""
    print("Installing server mods (SourceMod, Metamod, L4DToolZ, MultiSlots)...")
    sm_present = mods_installed()
    install_metamod(update or sm_present)
    install_sourcemod(update or sm_present)
    install_l4dtoolz()
    install_left4dhooks()
    install_harry_plugins()
    install_own_plugins()
    print("  Mods installed.")


def set_cfg_block(lines_or_none):
    text = read_file(CFG)
    text = re.sub(re.escape(CFG_BEGIN) + r".*?" + re.escape(CFG_END) + r"\n?", "", text, flags=re.S)
    if lines_or_none:
        if text and not text.endswith("\n"):
            text += "\n"
        text += "\n".join([CFG_BEGIN] + lines_or_none + [CFG_END]) + "\n"
    write_file(CFG, text.encode(), 0o640)


def set_cvars_in_file(path, values):
    text = read_file(path)
    for k, v in values.items():
        line = f'{k} "{v}"'
        pat = re.compile(r'^[ \t]*' + re.escape(k) + r'[ \t]+.*$', re.M)
        text = pat.sub(line, text) if pat.search(text) else (text + ("" if text.endswith("\n") or not text else "\n") + line + "\n")
    write_file(path, text.encode())


# ----------------------------------------------------------------- server.cfg (used by l4d2-setup.sh)
def clean_cvar(value, limit=64):
    """Strip what would end a quoted value or start another command."""
    return re.sub(r'[\x00-\x1f\x7f"\\;]', "", value or "").strip()[:limit]


def set_cvar(text, name, value, comment=""):
    """Set one cvar in server.cfg text, replacing the line that sets it now.

    That line may be quoted or not, several words, CRLF-terminated or spelled
    in another case (Source ignores case in cvar names). Its trailing // comment,
    anything chained after a ';' and its line ending are kept.
    """
    head = re.compile(r'([ \t]*)' + re.escape(name) + r'(?=[ \t"]|//|$)', re.I)
    line = f'{name} "{value}"'
    out, found = [], False
    for raw in re.split(r"(?<=\n)", text):      # not splitlines(): only \n ends a line for the game
        body = raw.rstrip("\r\n")
        m = head.match(body)
        if not m:
            out.append(raw)
            continue
        found = True
        rest, quoted, cut = body[m.end():], False, None
        for i, ch in enumerate(rest):             # where the value ends, outside quotes:
            if ch == '"':                         # at a // comment or a ';' (next command)
                quoted = not quoted
            elif not quoted and (ch == ";" or rest.startswith("//", i)):
                cut = i
                break
        tail = ""
        if cut is not None:
            while cut > 0 and rest[cut - 1] in " \t":
                cut -= 1
            tail = rest[cut:]
        out.append(m.group(1) + line + tail + raw[len(body):])
    if found:
        return "".join(out)
    if text and not text.endswith("\n"):
        text += "\n"
    return text + line + (f"          // {comment}" if comment else "") + "\n"


def backup_cfg():
    """Keep a timestamped copy of server.cfg, and only the newest few of them."""
    text = read_file(CFG, None)
    if text is None:
        return
    write_file(f"{CFG}.bak.{int(time.time())}", text.encode(), 0o600)
    folder, prefix = os.path.dirname(CFG), os.path.basename(CFG) + ".bak."
    stamps = sorted((int(n[len(prefix):]), n) for n in os.listdir(folder)
                    if n.startswith(prefix) and n[len(prefix):].isdigit())
    for _, name in stamps[:-KEEP_BACKUPS]:
        try:
            os.remove(os.path.join(folder, name))
        except OSError:
            pass


def cmd_init_config(_args):
    """Write the answers from l4d2-setup.sh into server.cfg.

    Values arrive in the environment, not as arguments, so passwords never show
    in the process list. An existing server.cfg keeps everything else in it:
    only these four settings are changed, and a backup is taken first. A
    password variable that is not set at all (as opposed to set but empty)
    leaves that password's line untouched.
    """
    wanted = [("hostname", clean_cvar(os.environ.get("L4D2_SET_HOSTNAME")) or "My L4D2 Server", ""),
              ("sv_password", os.environ.get("L4D2_SET_PASSWORD"), ""),
              ("rcon_password", os.environ.get("L4D2_SET_RCON"), "empty = remote admin disabled"),
              ("z_difficulty", clean_cvar(os.environ.get("L4D2_SET_DIFFICULTY")) or "Normal", "")]
    text = read_file(CFG, None)
    if text is None or not text.strip():
        text = ("// Created by l4d2-setup.sh on " + time.strftime("%Y-%m-%d") + "\n"
                "sv_allow_lobby_connect_only 0       // allow joining directly by IP\n"
                'sv_gametypes "coop,realism,versus,survival,scavenge"\n'
                "sv_lan 0\n"
                "sv_region 255\n")
        print("Writing a new server.cfg.")
    else:
        backup_cfg()
        print("server.cfg already exists - updating the name, passwords and difficulty, keeping the rest.")
    for name, value, comment in wanted:
        if value is None:             # a password not passed at all: keep the line as it is
            continue
        text = set_cvar(text, name, clean_cvar(value), comment)
    write_file(CFG, text.encode(), 0o640)
    os.chmod(CFG, 0o640)


def current_slots():
    try:
        return int(ENV.get("L4D2_SLOTS", "4"))
    except ValueError:
        return 4


def cmd_slots(args):
    flags = [a for a in args if a.startswith("--")]
    rest = [a for a in args if not a.startswith("--")]
    if not rest:
        n = current_slots()
        print(f"Co-op player slots: {n}" + ("  (vanilla, no mods)" if n <= 4 else "  (MultiSlots)"))
        print(f"Change with: l4d2 slots <{MIN_SLOTS}-{MAX_SLOTS}>")
        return
    try:
        n = int(rest[0])
    except ValueError:
        die(f"slots must be a number from {MIN_SLOTS} to {MAX_SLOTS}.")
    if not MIN_SLOTS <= n <= MAX_SLOTS:
        die(f"slots must be from {MIN_SLOTS} to {MAX_SLOTS} (more than {MAX_SLOTS} gets unstable).")

    if n > 4:
        try:
            install_mods(update=mods_installed())
        except PermissionError:
            raise                     # not a download problem: main() explains the fix
        except Exception as e:
            die(f"mod install failed: {e}\nNothing was switched on. If it was a download, check the server's internet connection and try again.")
        switch_slot_plugins(True)     # only now that every part installed
        set_cfg_block([
            f"sv_maxplayers {n}               // L4DToolZ: max human players",
            "sv_force_unreserved 1          // L4DToolZ: no lobby reservation (lobbies cap co-op at 4)",
            "sm_cvar precache_all_survivors 1  // load all survivor models (prevents crashes with 5+)",
            "sv_consistency 0",
        ])
        set_cvars_in_file(os.path.join(GAME, "cfg", "sourcemod", "l4dmultislots.cfg"), {
            "l4d_multislots_max_survivors": n,
            "l4d_multislots_min_survivors": 4,     # extra survivors only appear when 5+ people join
        })
        set_env("L4D2_SLOTS", str(n))
        set_env("L4D2_MAXCLIENTS", "31")   # +sv_setmax in the service: the plugins need 31 clients
        print(f"\nCo-op now allows {n} players. Survivors 5-{n} spawn automatically as people join.")
        print("Anyone who ends up spectating can type !join in chat.")
    else:
        switch_slot_plugins(False)
        set_cfg_block(None)
        set_env("L4D2_SLOTS", "4")
        set_env("L4D2_MAXCLIENTS", "18")   # the engine's own limit
        print("Back to the normal 4-player co-op (mods stay installed but MultiSlots is switched off).")
    maybe_restart(flags, "the new player limit")


STEAMID_RE = re.compile(r"^(STEAM_[0-5]:[01]:\d{1,10}|\[U:1:\d{1,10}\]|7656119\d{10})$")


def cmd_admin(args):
    if not mods_installed():
        die("SourceMod isn't installed. Enable it first with: l4d2 slots 8   (or any number above 4)")
    ini = os.path.join(SMDIR, "configs", "admins_simple.ini")
    if not args:
        print("Usage: l4d2 admin <SteamID>     e.g. STEAM_1:0:12345678 or 76561198000000000")
        print("Find your SteamID at https://steamid.io (paste your Steam profile link).\n")
        if os.path.exists(ini):
            ids = [l.split('"')[1] for l in read_file(ini).splitlines() if l.strip().startswith('"')]
            print("Current admins: " + (", ".join(ids) if ids else "(none)"))
        return
    sid = args[0].strip()
    if not STEAMID_RE.match(sid):
        die("that doesn't look like a SteamID. Use the STEAM_1:... or 7656119... form from steamid.io")
    text = read_file(ini)
    if f'"{sid}"' in text:
        print(f"{sid} is already an admin.")
        return
    text += ("" if text.endswith("\n") or not text else "\n") + f'"{sid}" "99:z"\n'
    write_file(ini, text.encode())
    if server_pid():
        send_console("sm_reloadadmins")
    print(f"{sid} is now a full admin. In game, type in chat:")
    print("   !admin                     open the admin menu")
    print("   !extras                    difficulty, game mode, tanks, hordes")
    print("   !map from_the_roof_01      change map (works for custom maps too)")


def cmd_modcheck(_args):
    if not server_pid():
        die("the server isn't running.")
    start = os.path.getsize(CONLOG) if os.path.exists(CONLOG) else 0
    for c in ("plugin_print", "meta list", "sm plugins list", "sv_maxplayers"):
        send_console(c)
    time.sleep(3)
    with open(CONLOG, errors="replace") as f:
        f.seek(start)
        print(f.read() or "(no output - is the server fully started?)")


# ----------------------------------------------------------------- whiptail UI
def wt(args):
    cols, rows = shutil.get_terminal_size((80, 24))
    r = subprocess.run(["whiptail", "--title", WT_TITLE] + args, stderr=subprocess.PIPE, text=True)
    return r.returncode == 0, r.stderr.strip()


def wt_size(lines=10):
    cols, rows = shutil.get_terminal_size((80, 24))
    return str(max(10, min(rows - 2, lines + 8))), str(max(40, min(cols - 4, 78)))


def wt_msg(text):
    h, w = wt_size(text.count("\n") + 2)
    wt(["--scrolltext", "--msgbox", text, h, w])


def wt_yesno(text):
    if not sys.stdin.isatty():
        return False
    ok, _ = wt(["--yesno", text, "10", "70"])
    return ok


def wt_input(text, init=""):
    ok, val = wt(["--inputbox", text, "12", "76", init])
    return val if ok else None


def wt_menu(text, items, default=None):
    h, w = wt_size(len(items) + 2)
    list_h = str(max(1, min(len(items), int(h) - 8)))
    args = (["--default-item", default] if default else []) + ["--menu", text, h, w, list_h]
    for tag, desc in items:
        args += [tag, desc[:60]]
    ok, val = wt(args)
    return val if ok else None


def pick_map():
    items = []
    for m in sorted(load_index().values(), key=lambda m: (m.get("campaign") or "").lower()):
        if m.get("first_map"):
            items.append((m["first_map"], f"[custom] {m.get('campaign')}"))
    items += list(OFFICIAL)
    seen, uniq = set(), []
    for tag, desc in items:
        if tag not in seen:
            seen.add(tag)
            uniq.append((tag, desc))
    return wt_menu("Pick a campaign to switch to:", uniq, ENV.get("L4D2_MAP"))


def pause():
    input("\nPress Enter to return to the menu...")


def in_terminal(fn, *a, lock=None):
    subprocess.run(["clear"])
    try:
        if lock:
            with locked(lock):
                fn(*a)
        else:
            fn(*a)
    except SystemExit:
        pass
    except PermissionError as e:
        print(permission_hint(e), file=sys.stderr)
    pause()


def cmd_menu(_args=None):
    if not sys.stdin.isatty():
        die("the menu needs an interactive terminal.")
    choice = None
    while True:
        items = [("status", "Server status"),
                 ("add", "Add maps from the Steam Workshop"),
                 ("list", "List installed custom maps"),
                 ("change", "Change map now"),
                 ("share", "Map links to send your friends"),
                 ("remove", "Remove a custom map"),
                 ("updatemaps", "Check Workshop maps for updates"),
                 ("slots", "Co-op player slots (4 to 12)"),
                 ("admin", "Make someone an in-game admin"),
                 ("restart", "Restart the server"),
                 ("logs", "Show recent server log"),
                 ("console", "Open the live server console"),
                 ("update", "Update the game server"),
                 ("exit", "Exit")]
        choice = wt_menu(status_text() + "\n\nWhat do you want to do?", items, choice)
        if choice in (None, "exit"):
            subprocess.run(["clear"])
            return
        if choice == "status":
            wt_msg(status_text() + "\n\n" + read_file(INFO))
        elif choice == "add":
            val = wt_input("Paste Steam Workshop links or IDs (map or collection).\n"
                           "Separate several with spaces. A path to a .vpk/.zip on this server also works.")
            if val and val.strip():
                in_terminal(cmd_addmap, [os.path.abspath(os.path.expanduser(v)) if admin_isfile(os.path.expanduser(v)) else v
                                         for v in val.split()], lock="addmap")
        elif choice == "list":
            in_terminal(cmd_maps)
        elif choice == "change":
            in_terminal(cmd_changemap, [], True, lock="changemap")
        elif choice == "share":
            wt_msg(share_text())
        elif choice == "remove":
            idx = load_index()
            if not idx:
                wt_msg("No custom maps installed.")
                continue
            opts = [(k, m.get("campaign") or m.get("title") or k) for k, m in idx.items()]
            key = wt_menu("Remove which addon?", opts)
            if key and wt_yesno(f"Remove {dict(opts)[key]}?"):
                in_terminal(cmd_removemap, [key], lock="removemap")
        elif choice == "slots":
            opts = [(str(n), "Normal L4D2, no mods" if n == 4 else ("Recommended" if n == 8 else ""))
                    for n in range(MIN_SLOTS, MAX_SLOTS + 1)]
            n = wt_menu(f"How many players in co-op? (now: {current_slots()})\n"
                        "Above 4 installs SourceMod + MultiSlots automatically.", opts, str(current_slots()))
            if n:
                in_terminal(cmd_slots, [n], lock="slots")
        elif choice == "admin":
            sid = wt_input("SteamID of the new admin (find it at steamid.io):\n"
                           "e.g. STEAM_1:0:12345678 or 76561198000000000")
            if sid:
                in_terminal(cmd_admin, [sid], lock="admin")
        elif choice == "updatemaps":
            in_terminal(cmd_updatemaps, [], lock="updatemaps")
        elif choice == "restart":
            if wt_yesno("Restart the server? Anyone playing will be disconnected."):
                in_terminal(restart_server, lock="restart")
        elif choice == "logs":
            text = "\n".join(read_file(CONLOG, "No log yet.").splitlines()[-40:])
            wt_msg(text)
        elif choice == "console":
            subprocess.run(["clear"])
            cmd_console([])
        elif choice == "update":
            if wt_yesno("Stop the server and update game files + Workshop maps?"):
                in_terminal(cmd_update, [], lock="update")


# ----------------------------------------------------------------- main
HELP = """Usage: l4d2 <command>

  menu                    Interactive menu (same as running 'l4d2' with no command)
  status                  Is the server running?
  start | stop | restart  Control the server
  console                 Live server console (detach: Ctrl+B then D)
  logs                    Follow the server log
  config                  Edit server.cfg
  update                  Update game files and Workshop maps
  info                    Connection details for friends

  addmap <link|id|file>   Install maps: Workshop links/IDs, collections, .vpk or .zip
                          options: --restart  --no-restart  --force
  maps                    List installed custom maps
  changemap [map]         Switch map now (menu if no name); --default to keep it
  removemap <id|file|map> Uninstall a custom map
  updatemaps              Re-download Workshop maps that changed
  share                   Map links to send your friends

  slots [4-12]            Co-op player limit (above 4 installs SourceMod + MultiSlots)
  admin [SteamID]         Make someone an in-game admin (!admin, !map in chat)
  modcheck                Show which mods/plugins the running server loaded
"""


# Commands that change the server or its files: one at a time (see locked()).
# Not "config": it sits in an editor for as long as the admin likes.
LOCKING = {"start", "stop", "restart", "addmap", "add", "changemap", "change", "map", "removemap",
           "remove", "updatemaps", "update", "slots", "admin", "init-config"}


def permission_hint(e):
    # Everything under the game folder is written as the game user. A file or
    # folder an older version of this tool left owned by root ends up here.
    return (f"Error: {e}\nSomething in {HOME} doesn't belong to the game user. "
            f"Hand it back and retry:\n  sudo chown -R -h {USER}: {HOME}")


def dispatch(cmd, rest):
    """Run one command. Returns its exit status (None means success)."""
    if cmd in ("start", "stop"):
        code = systemctl(cmd)
        print(f"Server: {cmd} OK" if code == 0
              else f"Server: {cmd} FAILED (systemctl exit {code}). Check: journalctl -u l4d2 -n 30")
        return code
    if cmd == "restart":
        return 0 if restart_server() else 1
    if cmd == "status":
        print(status_text())
    elif cmd in ("addmap", "add"):
        cmd_addmap(rest)
    elif cmd in ("maps", "list"):
        cmd_maps(rest)
    elif cmd in ("changemap", "change", "map"):
        cmd_changemap(rest)
    elif cmd in ("removemap", "remove"):
        cmd_removemap(rest)
    elif cmd == "updatemaps":
        cmd_updatemaps(rest)
    elif cmd == "share":
        print(share_text())
    elif cmd == "update":
        return cmd_update(rest)
    elif cmd == "slots":
        cmd_slots(rest)
    elif cmd == "admin":
        cmd_admin(rest)
    elif cmd == "modcheck":
        cmd_modcheck(rest)
    elif cmd == "logs":
        cmd_logs(rest)
    elif cmd == "console":
        cmd_console(rest)
    elif cmd == "config":
        cmd_config(rest)
    elif cmd == "init-config":
        cmd_init_config(rest)
    elif cmd == "info":
        print(read_file(INFO, "No info file yet."))
    elif cmd == "menu":
        cmd_menu(rest)
    elif cmd in ("help", "-h", "--help"):
        print(HELP)
    else:
        print(f"Unknown command: {cmd!r}. Run 'l4d2 help' for the list.", file=sys.stderr)
        return 2


def main():
    if os.geteuid() != 0:
        os.execvp("sudo", ["sudo", sys.executable, os.path.abspath(__file__)] + sys.argv[1:])
    args = sys.argv[1:]
    # addmap takes local files: resolve them against the folder the user ran us
    # from, before leaving it. Nothing else is touched, the command word least of
    # all (a file called "init-config" in the current folder must not change it).
    if args and args[0] in ("addmap", "add"):
        args = args[:1] + [os.path.abspath(a) if not a.startswith("-") and os.path.isfile(a) else a
                           for a in args[1:]]
    os.chdir("/tmp")
    if not os.path.exists(ENV_FILE):
        die(f"{ENV_FILE} not found - run l4d2-setup.sh first.")
    init_paths()
    drop_privileges()
    cmd = args[0] if args else ("menu" if sys.stdin.isatty() else "help")
    try:
        if cmd in LOCKING:
            with locked(cmd):
                return dispatch(cmd, args[1:])
        return dispatch(cmd, args[1:])
    except PermissionError as e:
        print(permission_hint(e), file=sys.stderr)
        return 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nCancelled.")
        sys.exit(130)
