#!/usr/bin/env bash
# =============================================================================
#  l4d2-setup.sh - One-shot installer for a Left 4 Dead 2 dedicated server
#
#  Tested target: Ubuntu 22.04 / 24.04 (x86_64), home PC or VPS.
#  Usage:         sudo bash l4d2-setup.sh
#
#  What it does:
#    1. Asks a few questions (server name, passwords, map, difficulty, network)
#    2. Installs SteamCMD + 32-bit libraries
#    3. Creates a locked-down "l4d2" user and downloads the server as that user
#    4. Writes server.cfg, a systemd service (auto-start + auto-restart)
#    5. Configures the firewall (Tailscale-only, public, or LAN-only)
#    6. Installs the 'l4d2' command: interactive menu, Workshop map installer,
#       map switching, updates, logs, console  (run 'l4d2' after setup)
#
#  Safe to re-run: it updates the existing install and backs up server.cfg.
#  Full log: /var/log/l4d2-setup.log
# =============================================================================
set -uo pipefail

APP_ID=222860
SRV_USER=l4d2
SRV_HOME=/home/$SRV_USER
INSTALL_DIR=$SRV_HOME/l4d2server
CFG_FILE=$INSTALL_DIR/left4dead2/cfg/server.cfg
GAME_PORT=27015
STEAMCMD=/usr/games/steamcmd
LOG=/var/log/l4d2-setup.log
INFO_FILE=$SRV_HOME/SERVER-INFO.txt
EST_BYTES=$((10 * 1024 * 1024 * 1024))   # rough download size, only used for the progress bar
TITLE="Left 4 Dead 2 Server Setup"
APT="apt-get -y -o DPkg::Lock::Timeout=600"

# ---------------------------------------------------------------- pre-checks
if [[ $EUID -ne 0 ]]; then
  echo "This script needs root. Re-running with sudo..."
  exec sudo bash "$0" "$@"
fi

# shellcheck disable=SC1091
. /etc/os-release
if [[ "${ID:-}" != "ubuntu" && "${ID_LIKE:-}" != *ubuntu* ]]; then
  echo "Sorry, this script only supports Ubuntu (detected: ${PRETTY_NAME:-unknown})."
  exit 1
fi
if [[ "$(uname -m)" != "x86_64" ]]; then
  echo "Sorry, the L4D2 server only runs on x86_64 (Intel/AMD). Detected: $(uname -m)."
  echo "ARM servers (e.g. Oracle Ampere, Raspberry Pi) are not supported."
  exit 1
fi

if ! command -v whiptail >/dev/null 2>&1; then
  echo "Installing whiptail for the setup screens..."
  apt-get update -qq && $APT -qq install whiptail >/dev/null || { echo "Could not install whiptail."; exit 1; }
fi

cd /tmp || exit 1   # l4d2 user can't read /root; avoid cwd errors in sudo -u commands
FAIL_FILE=$(mktemp)
trap 'rm -f "$FAIL_FILE"' EXIT
echo "=== L4D2 setup started $(date) ===" >>"$LOG"
rm -f "$LOG.warnings"

# ---------------------------------------------------------------- UI helpers
cancelled() {
  whiptail --title "$TITLE" --msgbox "Setup cancelled. Nothing was changed." 8 50
  exit 0
}
ask_input() { whiptail --title "$TITLE" --inputbox "$1" 12 72 "$2" 3>&1 1>&2 2>&3; }
clean() { tr -d '"\\' <<<"$1"; }   # strip characters that would break server.cfg

# ---------------------------------------------------------------- questions
whiptail --title "$TITLE" --yesno \
"This will set up a Left 4 Dead 2 dedicated server on this machine.

It will:
  - install SteamCMD and required libraries
  - create a separate '$SRV_USER' user to run the server safely
  - download the server (roughly 10 GB, can take a while)
  - set it to start automatically and restart if it crashes
  - configure the firewall

You'll answer a few questions first. Continue?" 20 72 || cancelled

avail_gb=$(df -BG --output=avail /home | tail -1 | tr -dc '0-9')
if (( avail_gb < 20 )); then
  whiptail --title "$TITLE" --yesno \
"Only ${avail_gb} GB of free disk space on /home.
The server needs about 15-20 GB (more with custom maps).

Continue anyway?" 11 64 || cancelled
fi

SV_NAME=$(ask_input "Server name (shown in the server browser):" "My L4D2 Server") || cancelled
SV_NAME=$(clean "$SV_NAME"); [[ -z "$SV_NAME" ]] && SV_NAME="My L4D2 Server"

SV_PASS=$(ask_input "Server password (friends need this to join).
Leave blank for no password." "") || cancelled
SV_PASS=$(clean "$SV_PASS")

RCON_DEFAULT=$(tr -dc 'A-Za-z0-9' </dev/urandom | head -c 16)
RCON_PASS=$(ask_input "RCON (remote admin) password.
A random one is filled in. Clear it to disable remote admin entirely." "$RCON_DEFAULT") || cancelled
RCON_PASS=$(clean "$RCON_PASS")

GAME_MODE=$(whiptail --title "$TITLE" --menu "Default game mode:" 14 60 3 \
  "coop"    "Campaign co-op (4 survivors)" \
  "realism" "Realism co-op (harder, no outlines)" \
  "versus"  "Versus (4 vs 4)" 3>&1 1>&2 2>&3) || cancelled

START_MAP=$(whiptail --title "$TITLE" --menu "Starting campaign:" 22 60 14 \
  "c1m1_hotel"        "Dead Center" \
  "c2m1_highway"      "Dark Carnival" \
  "c3m1_plankcountry" "Swamp Fever" \
  "c4m1_milltown_a"   "Hard Rain" \
  "c5m1_waterfront"   "The Parish" \
  "c6m1_riverbank"    "The Passing" \
  "c7m1_docks"        "The Sacrifice" \
  "c8m1_apartment"    "No Mercy" \
  "c9m1_alleys"       "Crash Course" \
  "c10m1_caves"       "Death Toll" \
  "c11m1_greenhouse"  "Dead Air" \
  "c12m1_hilltop"     "Blood Harvest" \
  "c13m1_alpinecreek" "Cold Stream" \
  "c14m1_junkyard"    "The Last Stand" 3>&1 1>&2 2>&3) || cancelled

DIFFICULTY=$(whiptail --title "$TITLE" --default-item "Normal" --menu "Difficulty:" 14 50 4 \
  "Easy" "" "Normal" "" "Hard" "Advanced" "Impossible" "Expert" 3>&1 1>&2 2>&3) || cancelled

NET_MODE=$(whiptail --title "$TITLE" --menu \
"How should friends reach the server?

Tailscale is the safest: nothing is exposed to the internet, and it
works even if your ISP uses CGNAT (common in the Philippines)." 18 78 3 \
  "tailscale" "Friends only, via Tailscale (recommended)" \
  "public"    "Open to the internet (VPS, or home + port forward)" \
  "lan"       "Local network only (same Wi-Fi / router)" 3>&1 1>&2 2>&3) || cancelled

WS_MAPS=$(ask_input "Custom maps from the Steam Workshop (optional).

Paste map or collection links/IDs, separated by spaces.
Leave blank to skip - you can add maps any time later with:  l4d2 menu" "") || cancelled
WS_MAPS=$(tr -s ' \t' ' ' <<<"$WS_MAPS" | sed 's/^ //;s/ $//')

whiptail --title "$TITLE" --yesno \
"Please confirm:

  Server name : $SV_NAME
  Password    : ${SV_PASS:-(none)}
  RCON        : ${RCON_PASS:-(disabled)}
  Mode / map  : $GAME_MODE / $START_MAP
  Difficulty  : $DIFFICULTY
  Network     : $NET_MODE
  Port        : $GAME_PORT/udp
  Workshop    : ${WS_MAPS:-(none)}

Start installing now?" 19 64 || cancelled

# ---------------------------------------------------------------- install steps
progress() { printf 'XXX\n%d\n%b\nXXX\n' "$1" "$2"; }
run()      { echo "+ $*" >>"$LOG"; "$@" >>"$LOG" 2>&1; }
fail()     { echo "$1" >"$FAIL_FILE"; exit 1; }

# A broken third-party repo (e.g. an old PPA) makes 'apt-get update' exit non-zero even
# though Ubuntu's own repos updated fine. Warn instead of aborting; if a package we need
# really can't be found, the install step right after will fail with a clear message.
apt_update() {
  if ! run apt-get update; then
    echo "WARNING: apt-get update reported errors (often an unrelated broken repo). Continuing." >>"$LOG"
    grep -E '^(E|W): ' "$LOG" | tail -n 3 >>"$LOG.warnings" 2>/dev/null || true
  fi
}

# Add Tailscale's apt repo ourselves (what their install.sh does) so our tolerant
# apt_update is used - their script aborts if ANY unrelated repo on the system is broken.
install_tailscale() {
  local codename="${UBUNTU_CODENAME:-${VERSION_CODENAME:-}}"
  if [[ -n "$codename" ]] \
     && run curl -fsSL "https://pkgs.tailscale.com/stable/ubuntu/${codename}.noarmor.gpg" \
            -o /usr/share/keyrings/tailscale-archive-keyring.gpg \
     && run curl -fsSL "https://pkgs.tailscale.com/stable/ubuntu/${codename}.tailscale-keyring.list" \
            -o /etc/apt/sources.list.d/tailscale.list; then
    chmod 644 /usr/share/keyrings/tailscale-archive-keyring.gpg /etc/apt/sources.list.d/tailscale.list
    apt_update
    if run $APT install tailscale; then
      run systemctl enable --now tailscaled
      return 0
    fi
  fi
  echo "Direct Tailscale install failed; trying the official install script..." >>"$LOG"
  run sh -c 'curl -fsSL https://tailscale.com/install.sh | sh'
}

steam_update() {   # $1 = optional platform override (windows|linux)
  local plat=() pid bytes pct gb
  [[ -n "${1:-}" ]] && plat=(+@sSteamCmdForcePlatformType "$1")
  echo "+ steamcmd ${plat[*]} app_update $APP_ID" >>"$LOG"
  sudo -u "$SRV_USER" -H "$STEAMCMD" "${plat[@]}" +force_install_dir "$INSTALL_DIR" \
    +login anonymous +app_update "$APP_ID" validate +quit >>"$LOG" 2>&1 &
  pid=$!
  while kill -0 "$pid" 2>/dev/null; do
    bytes=$(du -sb "$INSTALL_DIR" 2>/dev/null | cut -f1); bytes=${bytes:-0}
    pct=$(( 25 + bytes * 55 / EST_BYTES )); (( pct > 80 )) && pct=80
    gb=$(awk -v b="$bytes" 'BEGIN{printf "%.1f", b/1073741824}')
    progress "$pct" "Downloading server files${1:+ ($1 pass)}...\n\n${gb} GB downloaded so far (roughly 10 GB total).\nThis is the slow part - feel free to grab a coffee."
    sleep 4
  done
  wait "$pid"
}

install_all() {
  progress 2 "Updating package lists..."
  apt_update

  progress 5 "Enabling the multiverse repository and 32-bit packages..."
  run $APT install software-properties-common ca-certificates curl ufw || fail "Could not install base packages."
  run add-apt-repository -y multiverse || fail "Could not enable the multiverse repository."
  run dpkg --add-architecture i386     || fail "Could not enable 32-bit packages."
  apt_update

  progress 10 "Installing SteamCMD and 32-bit libraries..."
  echo steam steam/question select "I AGREE" | debconf-set-selections >>"$LOG" 2>&1
  echo steam steam/license note '' | debconf-set-selections >>"$LOG" 2>&1
  DEBIAN_FRONTEND=noninteractive run $APT install steamcmd lib32gcc-s1 lib32stdc++6 tmux python3 \
    || fail "Could not install SteamCMD."

  progress 18 "Creating the '$SRV_USER' user..."
  if ! id "$SRV_USER" >/dev/null 2>&1; then
    run useradd -m -s /bin/bash "$SRV_USER" || fail "Could not create user $SRV_USER."
  fi
  run install -d -o "$SRV_USER" -g "$SRV_USER" "$INSTALL_DIR"

  progress 22 "Preparing SteamCMD (first run updates itself)..."
  steam_update
  if [[ ! -x "$INSTALL_DIR/srcds_run" ]]; then
    # Known quirk: the Linux depot sometimes only downloads fully after a Windows pass.
    progress 50 "Applying the known SteamCMD workaround (Windows pass, then Linux)..."
    steam_update windows
    steam_update linux
  fi
  [[ -x "$INSTALL_DIR/srcds_run" ]] || fail "The server download did not complete (srcds_run missing)."

  progress 82 "Linking steamclient.so..."
  local sc
  sc=$(find "$SRV_HOME" -path '*linux32/steamclient.so' 2>/dev/null | head -1)
  if [[ -n "$sc" ]]; then
    run sudo -u "$SRV_USER" mkdir -p "$SRV_HOME/.steam/sdk32"
    run sudo -u "$SRV_USER" ln -sf "$sc" "$SRV_HOME/.steam/sdk32/steamclient.so"
  fi

  progress 85 "Writing server.cfg..."
  [[ -f "$CFG_FILE" ]] && run cp "$CFG_FILE" "$CFG_FILE.bak.$(date +%s)"
  cat >"$CFG_FILE" <<EOF
// Generated by l4d2-setup.sh on $(date)
hostname "$SV_NAME"
sv_password "$SV_PASS"
rcon_password "$RCON_PASS"          // empty = remote admin disabled
sv_allow_lobby_connect_only 0       // allow joining directly by IP
sv_gametypes "coop,realism,versus,survival,scavenge"
z_difficulty "$DIFFICULTY"
sv_lan 0
sv_region 255
EOF
  chown "$SRV_USER:$SRV_USER" "$CFG_FILE"; chmod 640 "$CFG_FILE"

  progress 88 "Creating the systemd service (auto-start on boot)..."
  cat >/etc/systemd/system/l4d2.service <<EOF
[Unit]
Description=Left 4 Dead 2 Dedicated Server
After=network-online.target
Wants=network-online.target

[Service]
Type=forking
User=$SRV_USER
WorkingDirectory=$INSTALL_DIR
ExecStartPre=-/usr/bin/tmux kill-session -t l4d2
EnvironmentFile=/etc/l4d2.env
ExecStart=/usr/bin/tmux new-session -d -s l4d2 "$INSTALL_DIR/srcds_run -game left4dead2 -console -condebug -port \${L4D2_PORT} +mp_gamemode \${L4D2_MODE} +map \${L4D2_MAP} +exec server.cfg"
ExecStop=/usr/bin/tmux kill-session -t l4d2
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF
  run systemctl daemon-reload

  progress 90 "Installing the 'l4d2' helper command..."
  write_helper

  if [[ "$NET_MODE" == "tailscale" ]] && ! command -v tailscale >/dev/null 2>&1; then
    progress 92 "Installing Tailscale..."
    install_tailscale || fail "Could not install Tailscale."
  fi

  progress 95 "Configuring the firewall..."
  configure_firewall || fail "Firewall configuration failed."

  if [[ -n "$WS_MAPS" ]]; then
    progress 96 "Downloading Workshop maps...\n\n$WS_MAPS"
    # shellcheck disable=SC2086
    if ! run /usr/local/bin/l4d2 addmap $WS_MAPS --no-restart; then
      echo "Some Workshop maps could not be installed - see $LOG" >>"$LOG.warnings"
    fi
  fi

  progress 97 "Starting the server..."
  run systemctl enable l4d2
  run systemctl restart l4d2
  local i
  for i in $(seq 1 20); do
    pgrep -u "$SRV_USER" -f srcds_linux >/dev/null && break
    progress 97 "Starting the server... (waiting for the game process, ${i}/20)"
    sleep 3
  done
  pgrep -u "$SRV_USER" -f srcds_linux >/dev/null \
    || fail "The server process did not start. Check: $INSTALL_DIR/left4dead2/console.log and journalctl -u l4d2"

  progress 100 "Done!"
  sleep 1
}

configure_firewall() {
  local ssh_port=22
  if command -v sshd >/dev/null 2>&1; then
    ssh_port=$(sshd -T 2>/dev/null | awk '/^port /{print $2; exit}'); ssh_port=${ssh_port:-22}
  fi
  run ufw default deny incoming
  run ufw default allow outgoing
  run ufw allow "$ssh_port/tcp"          # never lock yourself out of SSH
  # clear rules from any previous run so modes don't stack
  ufw delete allow $GAME_PORT/udp >>"$LOG" 2>&1 || true
  ufw delete allow $GAME_PORT/tcp >>"$LOG" 2>&1 || true
  ufw delete allow in on tailscale0 to any port $GAME_PORT proto udp >>"$LOG" 2>&1 || true
  for net in 10.0.0.0/8 172.16.0.0/12 192.168.0.0/16; do
    ufw delete allow from "$net" to any port $GAME_PORT proto udp >>"$LOG" 2>&1 || true
  done
  case "$NET_MODE" in
    tailscale) run ufw allow in on tailscale0 to any port $GAME_PORT proto udp ;;
    public)    run ufw allow $GAME_PORT/udp ;;
    lan)       for net in 10.0.0.0/8 172.16.0.0/12 192.168.0.0/16; do
                 run ufw allow from "$net" to any port $GAME_PORT proto udp
               done ;;
  esac
  run ufw --force enable
}

write_helper() {
  # Settings shared by the systemd service and the l4d2 command
  cat >/etc/l4d2.env <<EOF
L4D2_USER=$SRV_USER
L4D2_HOME=$SRV_HOME
L4D2_DIR=$INSTALL_DIR
L4D2_PORT=$GAME_PORT
L4D2_MODE=$GAME_MODE
L4D2_MAP=$START_MAP
L4D2_STEAMCMD=$STEAMCMD
EOF
  chmod 644 /etc/l4d2.env

  cat >/usr/local/bin/l4d2 <<'L4D2_HELPER_EOF'
#!/usr/bin/env python3
# =============================================================================
#  l4d2 - manage the Left 4 Dead 2 dedicated server (installed by l4d2-setup.sh)
#
#  Run "l4d2" or "l4d2 menu" for the interactive menu, "l4d2 help" for commands.
#  Settings shared with the systemd service live in /etc/l4d2.env
# =============================================================================
import json
import os
import pwd
import re
import shutil
import struct
import subprocess
import sys
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
    lines = open(ENV_FILE).read().splitlines()
    for i, line in enumerate(lines):
        if line.startswith(key + "="):
            lines[i] = f"{key}={value}"
            break
    else:
        lines.append(f"{key}={value}")
    with open(ENV_FILE, "w") as f:
        f.write("\n".join(lines) + "\n")
    ENV[key] = value


ENV = {}
USER = HOME = DIR = ADDONS = CFG = CONLOG = INDEX = INFO = STEAMCMD = ""


def init_paths():
    global ENV, USER, HOME, DIR, ADDONS, CFG, CONLOG, INDEX, INFO, STEAMCMD
    ENV = load_env()
    USER = ENV.get("L4D2_USER", "l4d2")
    HOME = ENV.get("L4D2_HOME", f"/home/{USER}")
    DIR = ENV.get("L4D2_DIR", f"{HOME}/l4d2server")
    STEAMCMD = ENV.get("L4D2_STEAMCMD", "/usr/games/steamcmd")
    ADDONS = os.path.join(DIR, "left4dead2", "addons")
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


def chown_user(path):
    pw = pwd.getpwnam(USER)
    os.chown(path, pw.pw_uid, pw.pw_gid)


def ask_yes(prompt):
    if not sys.stdin.isatty():
        return False
    return input(f"{prompt} [y/N] ").strip().lower() in ("y", "yes")


def tmp_base():
    p = os.path.join(HOME, ".l4d2-tmp")   # same filesystem as addons -> atomic moves
    os.makedirs(p, exist_ok=True)
    return p


def safe_name(name):
    name = re.sub(r"[^A-Za-z0-9._-]", "_", os.path.basename(name))
    return name if name.lower().endswith(".vpk") else name + ".vpk"


def load_index():
    try:
        with open(INDEX) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_index(idx):
    tmp = INDEX + ".tmp"
    with open(tmp, "w") as f:
        json.dump(idx, f, indent=2, sort_keys=True)
    os.replace(tmp, INDEX)


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
    subprocess.run(["systemctl", "restart", "l4d2"])
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
    dest = os.path.join(ADDONS, dest_name)
    shutil.move(src, dest)
    os.utime(dest, None)          # mark as "newer than the running server"
    os.chmod(dest, 0o644)
    chown_user(dest)
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
    tmpd = tempfile.mkdtemp(dir=tmp_base())
    try:
        srcs = []
        low = path.lower()
        if low.endswith(".zip"):
            with zipfile.ZipFile(path) as z:
                for n in z.namelist():
                    if n.lower().endswith(".vpk") and not n.endswith("/"):
                        target = os.path.join(tmpd, safe_name(n))
                        with z.open(n) as s, open(target, "wb") as d:
                            shutil.copyfileobj(s, d)
                        srcs.append(target)
            if not srcs:
                print(f"x {path}: no .vpk files inside this zip.")
        elif low.endswith(".vpk"):
            target = os.path.join(tmpd, safe_name(path))
            shutil.copyfile(path, target)
            srcs.append(target)
        else:
            print(f"x {path}: only .vpk or .zip files are supported.")
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
                and os.path.exists(os.path.join(ADDONS, old.get("file", "")))):
            print(f"= {title}: already installed and up to date.")
            continue
        print(f"+ {title}  ({wid})")
        tmpd = tempfile.mkdtemp(dir=tmp_base())
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
        if os.path.isfile(t):
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
        missing = "" if os.path.exists(os.path.join(ADDONS, m.get("file", ""))) else "   [FILE MISSING]"
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
            try:
                os.remove(os.path.join(ADDONS, m.get("file", "")))
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
    print("Stopping the server and updating game files (this can take a few minutes)...")
    subprocess.run(["systemctl", "stop", "l4d2"])
    os.chdir("/tmp")
    subprocess.run(as_user([STEAMCMD, "+force_install_dir", DIR, "+login", "anonymous",
                            "+app_update", "222860", "validate", "+quit"]))
    cmd_updatemaps(["--no-restart"])
    subprocess.run(["systemctl", "start", "l4d2"])
    print("Update finished, server started.")


def status_text():
    active = subprocess.run(["systemctl", "is-active", "l4d2"], capture_output=True, text=True).stdout.strip()
    running = "running" if server_pid() else "NOT running"
    idx = load_index()
    pend = pending_addons()
    lines = [f"Service       : {active}", f"Game process  : {running}",
             f"Default map   : {ENV.get('L4D2_MAP')}  ({ENV.get('L4D2_MODE')})",
             f"Custom addons : {len(idx)} installed"]
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
    subprocess.run([os.environ.get("EDITOR", "nano"), CFG])
    if ask_yes("Restart the server to apply the config?"):
        restart_server()


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


def in_terminal(fn, *a):
    subprocess.run(["clear"])
    try:
        fn(*a)
    except SystemExit:
        pass
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
            wt_msg(status_text() + "\n\n" + (open(INFO).read() if os.path.exists(INFO) else ""))
        elif choice == "add":
            val = wt_input("Paste Steam Workshop links or IDs (map or collection).\n"
                           "Separate several with spaces. A path to a .vpk/.zip on this server also works.")
            if val and val.strip():
                in_terminal(cmd_addmap, [os.path.abspath(os.path.expanduser(v)) if os.path.isfile(os.path.expanduser(v)) else v
                                         for v in val.split()])
        elif choice == "list":
            in_terminal(cmd_maps)
        elif choice == "change":
            in_terminal(cmd_changemap, [], True)
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
                in_terminal(cmd_removemap, [key])
        elif choice == "updatemaps":
            in_terminal(cmd_updatemaps, [])
        elif choice == "restart":
            if wt_yesno("Restart the server? Anyone playing will be disconnected."):
                in_terminal(restart_server)
        elif choice == "logs":
            text = "".join(open(CONLOG, errors="replace").readlines()[-40:]) if os.path.exists(CONLOG) else "No log yet."
            wt_msg(text)
        elif choice == "console":
            subprocess.run(["clear"])
            cmd_console([])
        elif choice == "update":
            if wt_yesno("Stop the server and update game files + Workshop maps?"):
                in_terminal(cmd_update, [])


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
"""


def main():
    if os.geteuid() != 0:
        os.execvp("sudo", ["sudo", sys.executable, os.path.abspath(__file__)] + sys.argv[1:])
    # resolve file arguments against the directory the user ran us from, before leaving it
    args = [os.path.abspath(a) if not a.startswith("-") and os.path.isfile(a) else a for a in sys.argv[1:]]
    os.chdir("/tmp")
    if not os.path.exists(ENV_FILE):
        die(f"{ENV_FILE} not found - run l4d2-setup.sh first.")
    init_paths()
    cmd = args[0] if args else ("menu" if sys.stdin.isatty() else "help")
    rest = args[1:]
    simple = {"start": "start", "stop": "stop"}
    if cmd in simple:
        subprocess.run(["systemctl", simple[cmd], "l4d2"])
        print(f"Server: {cmd} OK")
    elif cmd == "restart":
        restart_server()
    elif cmd == "status":
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
        cmd_update(rest)
    elif cmd == "logs":
        cmd_logs(rest)
    elif cmd == "console":
        cmd_console(rest)
    elif cmd == "config":
        cmd_config(rest)
    elif cmd == "info":
        print(open(INFO).read() if os.path.exists(INFO) else "No info file yet.")
    elif cmd == "menu":
        cmd_menu(rest)
    else:
        print(HELP)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nCancelled.")
        sys.exit(130)
L4D2_HELPER_EOF
  chmod 755 /usr/local/bin/l4d2
}

: >"$FAIL_FILE"
install_all | whiptail --title "$TITLE" --gauge "Starting..." 12 76 0

if [[ -s "$FAIL_FILE" ]]; then
  whiptail --title "Setup failed" --scrolltext --msgbox \
"$(cat "$FAIL_FILE")

Last lines of the log ($LOG):
$(tail -n 20 "$LOG")" 24 90
  exit 1
fi

# ---------------------------------------------------------------- Tailscale login
TS_IP=""
if [[ "$NET_MODE" == "tailscale" ]]; then
  if ! tailscale status >/dev/null 2>&1; then
    clear
    echo "=================================================================="
    echo "  Tailscale login"
    echo "  Open the link below in any browser and sign in (free account)."
    echo "  This screen continues automatically once you've signed in."
    echo "=================================================================="
    tailscale up || echo "Tailscale login did not complete. Run 'sudo tailscale up' later."
  fi
  TS_IP=$(tailscale ip -4 2>/dev/null | head -1)
fi

# ---------------------------------------------------------------- summary
LAN_IP=$(hostname -I 2>/dev/null | awk '{print $1}')
PUB_IP=""
[[ "$NET_MODE" == "public" ]] && PUB_IP=$(curl -s --max-time 5 https://api.ipify.org || true)

case "$NET_MODE" in
  tailscale) CONNECT="connect ${TS_IP:-<tailscale-ip>}:$GAME_PORT"
             HOWTO="Share this machine with friends:
  Tailscale admin console > Machines > ... next to this server > Share.
  Each friend installs Tailscale, accepts the invite, then connects." ;;
  public)    CONNECT="connect ${PUB_IP:-<your-public-ip>}:$GAME_PORT"
             HOWTO="On a VPS this works right away.
At home: forward UDP $GAME_PORT on your router to $LAN_IP.
(If your ISP uses CGNAT, port forwarding won't work - re-run and pick Tailscale.)" ;;
  lan)       CONNECT="connect $LAN_IP:$GAME_PORT"
             HOWTO="Only devices on the same network can join." ;;
esac

cat >"$INFO_FILE" <<EOF
Left 4 Dead 2 server - set up $(date)
Server name : $SV_NAME
Password    : ${SV_PASS:-(none)}
RCON        : ${RCON_PASS:-(disabled)}
Mode / map  : $GAME_MODE / $START_MAP ($DIFFICULTY)
Network     : $NET_MODE

In-game console:  ${SV_PASS:+password $SV_PASS
                  }$CONNECT

$HOWTO

Players must enable: Options > Keyboard/Mouse > Allow Developer Console,
then press ~ and type the connect command above.

Manage the server - just type:  l4d2
  (menu: add Workshop maps, change map, links for friends, restart, logs...)
Or directly:
  l4d2 addmap <workshop link>   l4d2 changemap <map>   l4d2 share
  l4d2 status | restart | console | logs | update | help
Files: $INSTALL_DIR
Setup log: $LOG
EOF
if [[ -n "$WS_MAPS" ]]; then
  printf '\nFriends need the same maps. Links to send them:  l4d2 share\n' >>"$INFO_FILE"
fi
if [[ -s "$LOG.warnings" ]]; then
  printf '\nNote: apt reported problems with other software sources on this machine\n(not needed for L4D2). Details: %s\n' "$LOG.warnings" >>"$INFO_FILE"
fi
chown "$SRV_USER:$SRV_USER" "$INFO_FILE"; chmod 600 "$INFO_FILE"

rows=$(tput lines 2>/dev/null || echo 24); cols=$(tput cols 2>/dev/null || echo 80)
(( rows > 32 )) && rows=32; (( cols > 86 )) && cols=86
whiptail --title "Your server is running!" --scrolltext --msgbox "$(cat "$INFO_FILE")" $((rows-2)) $((cols-2))
clear
cat "$INFO_FILE"
echo
echo "(This info is saved - view it anytime with:  l4d2 info)"
