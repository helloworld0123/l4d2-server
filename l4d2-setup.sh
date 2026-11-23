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
#  Safe to re-run: it updates the existing install, keeps your server.cfg
#  (only the settings you answer here are changed) and backs it up first.
#
#  sudo bash l4d2-setup.sh --upgrade   refreshes just the 'l4d2' command, the
#                                      systemd service and the log rotation,
#                                      asking nothing.
#  Full log: /var/log/l4d2-setup.log
# =============================================================================
set -uo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)

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

if [[ ! -f "$SCRIPT_DIR/cli/l4d2.py" ]]; then
  echo "Can't find cli/l4d2.py next to this script ($SCRIPT_DIR)."
  echo "Copy the whole project folder to this machine, not just l4d2-setup.sh."
  exit 1
fi

# The 'l4d2' command and the log rotation rule: the two things an upgrade refreshes.
install_helper() {
  install -m 0755 -o root -g root "$SCRIPT_DIR/cli/l4d2.py" /usr/local/bin/l4d2
  # Sources of the toolkit's own SourceMod plugins; 'l4d2 slots' builds them.
  install -d -m 0755 -o root -g root /usr/local/share/l4d2/plugins
  install -m 0644 -o root -g root "$SCRIPT_DIR"/cli/plugins/*.sp /usr/local/share/l4d2/plugins/
}
# Older versions of the l4d2 command worked in the game folder as root and could
# leave things there owned by root. The command now works as the game user, so
# hand everything back. -h and no -L: links are re-owned, never followed.
fix_ownership() {
  rm -rf "$SRV_HOME/.l4d2-tmp"
  chown -R -h "$SRV_USER:" "$SRV_HOME"
}
install_logrotate() {
  # -condebug makes the game append to console.log forever. copytruncate,
  # because srcds keeps the file open; "su", because the folder belongs to the
  # game user and logrotate refuses to work there as root.
  cat >/etc/logrotate.d/l4d2 <<EOF
$INSTALL_DIR/left4dead2/console.log {
    su $SRV_USER $SRV_USER
    weekly
    maxsize 20M
    rotate 4
    compress
    delaycompress
    missingok
    notifempty
    copytruncate
}
EOF
  chmod 644 /etc/logrotate.d/l4d2
}

# The systemd service. +sv_setmax is read by L4DToolZ (installed by 'l4d2 slots'
# for more than four players): the engine allows 18 clients, bots included,
# and the survivor plugins need 31. Without L4DToolZ the game ignores it.
install_unit() {
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
ExecStart=/usr/bin/tmux new-session -d -s l4d2 "$INSTALL_DIR/srcds_run -game left4dead2 -console -condebug -port \${L4D2_PORT} +sv_setmax \${L4D2_MAXCLIENTS} +mp_gamemode \${L4D2_MODE} +map \${L4D2_MAP} +exec server.cfg"
ExecStop=/usr/bin/tmux kill-session -t l4d2
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF
}
# Client limit that goes with a slot count: the engine's own 18 for a normal
# four-player server, 31 once the extra-survivor mods are on.
maxclients_for() { if (( ${1:-4} > 4 )); then echo 31; else echo 18; fi; }

if [[ "${1:-}" == "--upgrade" ]]; then
  if [[ ! -f /etc/l4d2.env ]]; then
    echo "No existing install found (/etc/l4d2.env is missing). Run without --upgrade."
    exit 1
  fi
  # shellcheck disable=SC1091
  . /etc/l4d2.env
  SRV_USER=${L4D2_USER:-$SRV_USER}; INSTALL_DIR=${L4D2_DIR:-$INSTALL_DIR}; SRV_HOME=${L4D2_HOME:-$SRV_HOME}
  # Installs from before the client limit existed have no L4D2_MAXCLIENTS yet.
  if ! grep -q '^L4D2_MAXCLIENTS=' /etc/l4d2.env; then
    [[ -z $(tail -c1 /etc/l4d2.env) ]] || echo >>/etc/l4d2.env   # file may not end in a newline
    echo "L4D2_MAXCLIENTS=$(maxclients_for "${L4D2_SLOTS:-4}")" >>/etc/l4d2.env
  fi
  if ! { install_helper && install_logrotate && fix_ownership && install_unit && systemctl daemon-reload; }; then
    echo "Upgrade failed."
    exit 1
  fi
  # Earlier versions copied the RCON password into SERVER-INFO.txt. Done as the
  # game user, so a link left at that path can't redirect it.
  sudo -u "$SRV_USER" sed -i 's/^\(RCON[[:space:]]*: \).*/\1enabled (the password is rcon_password in server.cfg)/' \
    "$SRV_HOME/SERVER-INFO.txt" 2>/dev/null || true
  echo "Updated /usr/local/bin/l4d2, /etc/logrotate.d/l4d2 and the l4d2 service."
  echo "The game server keeps running; the new service settings apply at its"
  echo "next restart (l4d2 restart)."
  echo "If you use them, refresh the Telegram bot and web panel too:"
  echo "  sudo bash telegram-bot-setup.sh     sudo bash web-panel-setup.sh"
  exit 0
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
ask_input() { whiptail --title "$TITLE" --inputbox "$1" 12 72 -- "$2" 3>&1 1>&2 2>&3; }
clean() { tr -d '"\\;' <<<"$1"; }   # strip characters that would break server.cfg
# Current value of a setting in an existing server.cfg, so a re-run starts from
# what you already have instead of from the defaults.
old_cvar() {
  [[ -f "$CFG_FILE" && ! -L "$CFG_FILE" ]] || return 0
  grep -oP "^\\s*$1\\s+\"\\K[^\"]*" "$CFG_FILE" 2>/dev/null | tail -1
}
RERUN=""; [[ -f "$CFG_FILE" && ! -L "$CFG_FILE" ]] && RERUN=1

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

OLD_NAME=$(old_cvar hostname)
SV_NAME=$(ask_input "Server name (shown in the server browser):" "${OLD_NAME:-My L4D2 Server}") || cancelled
SV_NAME=$(clean "$SV_NAME"); [[ -z "$SV_NAME" ]] && SV_NAME="My L4D2 Server"

SV_PASS=$(ask_input "Server password (friends need this to join).
Leave blank for no password." "$(old_cvar sv_password)") || cancelled
SV_PASS=$(clean "$SV_PASS")

if [[ -n "$RERUN" ]]; then
  RCON_DEFAULT=$(old_cvar rcon_password)     # keep it, or keep it disabled
  RCON_HINT="Your current one is filled in."
else
  RCON_DEFAULT=$(tr -dc 'A-Za-z0-9' </dev/urandom | head -c 16)
  RCON_HINT="A random one is filled in."
fi
RCON_PASS=$(ask_input "RCON (remote admin) password.
$RCON_HINT Clear it to disable remote admin entirely." "$RCON_DEFAULT") || cancelled
RCON_PASS=$(clean "$RCON_PASS")

GAME_MODE=$(whiptail --title "$TITLE" --menu "Default game mode:" 14 60 3 \
  "coop"    "Campaign co-op (4 survivors)" \
  "realism" "Realism co-op (harder, no outlines)" \
  "versus"  "Versus (4 vs 4)" 3>&1 1>&2 2>&3) || cancelled

SLOTS=4
if [[ "$GAME_MODE" != "versus" ]]; then
  SLOTS=$(whiptail --title "$TITLE" --default-item "4" --menu \
"How many players in co-op?

L4D2 normally allows 4. More than 4 installs SourceMod + MultiSlots
(free server mods) which add a survivor for each extra player.
Players don't need to install anything. You can change this later: l4d2 slots" 20 74 5 \
    "4"  "Normal L4D2 - no mods" \
    "6"  "6 survivors" \
    "8"  "8 survivors (most popular)" \
    "10" "10 survivors" \
    "12" "12 survivors (chaotic)" 3>&1 1>&2 2>&3) || cancelled
fi

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
  RCON        : $([[ -n "$RCON_PASS" ]] && echo "enabled" || echo "(disabled)")
  Mode / map  : $GAME_MODE / $START_MAP
  Players     : $SLOTS$( ((SLOTS > 4)) && echo " (installs SourceMod + MultiSlots)")
  Difficulty  : $DIFFICULTY
  Network     : $NET_MODE
  Port        : $GAME_PORT/udp
  Workshop    : ${WS_MAPS:-(none)}

Start installing now?" 21 74 || cancelled

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
  run sudo -u "$SRV_USER" mkdir -p "$INSTALL_DIR" || fail "Could not create $INSTALL_DIR."

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

  progress 84 "Installing the 'l4d2' helper command..."
  write_helper || fail "Could not install the l4d2 command."
  run fix_ownership || fail "Could not hand $SRV_HOME back to the $SRV_USER user."

  progress 85 "Writing server.cfg..."
  # Done by the l4d2 command, as the game user: an existing server.cfg keeps
  # everything you added to it. The values travel in the environment so the
  # passwords never appear in the process list or in $LOG.
  echo "+ l4d2 init-config" >>"$LOG"
  L4D2_SET_HOSTNAME="$SV_NAME" L4D2_SET_PASSWORD="$SV_PASS" L4D2_SET_RCON="$RCON_PASS" \
    L4D2_SET_DIFFICULTY="$DIFFICULTY" /usr/local/bin/l4d2 init-config >>"$LOG" 2>&1 \
    || fail "Could not write server.cfg."

  progress 88 "Creating the systemd service (auto-start on boot)..."
  install_unit || fail "Could not write the systemd service."
  run systemctl daemon-reload

  progress 90 "Setting up log rotation..."
  install_logrotate || fail "Could not install the log rotation rule."

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

  if (( SLOTS > 4 )) || [[ -d "$INSTALL_DIR/left4dead2/addons/sourcemod" ]]; then
    progress 96 "Setting co-op player slots to $SLOTS...\n\n(downloads SourceMod, Metamod, L4DToolZ and MultiSlots if needed)"
    if ! run /usr/local/bin/l4d2 slots "$SLOTS" --no-restart; then
      echo "Could not set player slots to $SLOTS - see $LOG. Try later with: l4d2 slots $SLOTS" >>"$LOG.warnings"
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
  # Settings shared by the systemd service and the l4d2 command.
  # On a re-run the slot count is carried over: 'l4d2 slots' adjusts it later,
  # and until then it must keep matching what server.cfg already says.
  local old_slots=4
  if [[ -f /etc/l4d2.env ]]; then
    old_slots=$(grep -oP '^L4D2_SLOTS=\K[0-9]+$' /etc/l4d2.env 2>/dev/null | tail -1)
    old_slots=${old_slots:-4}
  fi
  cat >/etc/l4d2.env <<EOF
L4D2_USER=$SRV_USER
L4D2_HOME=$SRV_HOME
L4D2_DIR=$INSTALL_DIR
L4D2_PORT=$GAME_PORT
L4D2_MODE=$GAME_MODE
L4D2_MAP=$START_MAP
L4D2_SLOTS=$old_slots
L4D2_MAXCLIENTS=$(maxclients_for "$old_slots")
L4D2_STEAMCMD=$STEAMCMD
EOF
  chmod 644 /etc/l4d2.env
  install_helper
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

# Built in a root-only temp file, then written into the game user's home *as
# that user*, so nothing root does follows a link someone left there.
INFO_TMP=$(mktemp)
cat >"$INFO_TMP" <<EOF
Left 4 Dead 2 server - set up $(date)
Server name : $SV_NAME
Password    : ${SV_PASS:-(none)}
RCON        : $([[ -n "$RCON_PASS" ]] && echo "enabled (the password is rcon_password in server.cfg)" || echo "(disabled)")
Mode / map  : $GAME_MODE / $START_MAP ($DIFFICULTY)
Players     : $SLOTS
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
if (( SLOTS > 4 )); then
  printf '\n%s-player co-op is on. Extra survivors spawn as people join; spectators type !join.\nWant in-game admin (!map, !admin)? Run:  l4d2 admin <your SteamID>\n' "$SLOTS" >>"$INFO_TMP"
fi
if [[ -n "$WS_MAPS" ]]; then
  printf '\nFriends need the same maps. Links to send them:  l4d2 share\n' >>"$INFO_TMP"
fi
if [[ -s "$LOG.warnings" ]]; then
  printf '\nNote: apt reported problems with other software sources on this machine\n(not needed for L4D2). Details: %s\n' "$LOG.warnings" >>"$INFO_TMP"
fi
sudo -u "$SRV_USER" sh -c 'umask 077; rm -f "$1"; cat >"$1"' sh "$INFO_FILE" <"$INFO_TMP"
INFO_TEXT=$(cat "$INFO_TMP"); rm -f "$INFO_TMP"

rows=$(tput lines 2>/dev/null || echo 24); cols=$(tput cols 2>/dev/null || echo 80)
(( rows > 32 )) && rows=32; (( cols > 86 )) && cols=86
whiptail --title "Your server is running!" --scrolltext --msgbox "$INFO_TEXT" $((rows-2)) $((cols-2))
clear
echo "$INFO_TEXT"
echo
echo "(This info is saved - view it anytime with:  l4d2 info)"