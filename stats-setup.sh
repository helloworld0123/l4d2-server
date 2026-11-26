#!/usr/bin/env bash
# Installs the public stats site for the Left 4 Dead 2 server, and optionally
# a Cloudflare Tunnel that puts it on the internet.
# Run this AFTER l4d2-setup.sh. The numbers come from the l4d2_stats plugin,
# which 'l4d2 slots' / 'l4d2 update' build together with the other mods.
#
#   sudo bash stats-setup.sh               install or change settings
#   sudo bash stats-setup.sh --uninstall   remove the site and the tunnel (the stats are kept)
set -uo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
TITLE="L4D2 Stats Site Setup"
INSTALL_DIR=/opt/l4d2-stats
CONF=/etc/l4d2-stats.env
TUNNEL_CONF=/etc/l4d2-stats-tunnel.env
UNIT=/etc/systemd/system/l4d2-stats.service
TUNNEL_UNIT=/etc/systemd/system/l4d2-stats-tunnel.service
MAPS_PATH_UNIT=/etc/systemd/system/l4d2-stats-maps.path
MAPS_RELOAD_UNIT=/etc/systemd/system/l4d2-stats-maps.service
SERVER_ENV=/etc/l4d2.env
LOG=/var/log/l4d2-stats-setup.log

# ---------------------------------------------------------------- pre-checks
if [[ $EUID -ne 0 ]]; then
  echo "This script needs root. Re-running with sudo..."
  exec sudo bash "$0" "$@"
fi

if ! command -v whiptail >/dev/null 2>&1; then
  echo "Installing whiptail for the setup screens..."
  apt-get update -qq && apt-get -y -qq install whiptail >/dev/null \
    || { echo "Could not install whiptail."; exit 1; }
fi

echo "=== L4D2 stats site setup $(date) ===" >>"$LOG"

cancelled() {
  whiptail --title "$TITLE" --msgbox "Setup cancelled. Nothing was changed." 8 50
  exit 0
}
ask_input() { whiptail --title "$TITLE" --inputbox "$1" "${3:-12}" 74 -- "$2" 3>&1 1>&2 2>&3; }
ask_secret() { whiptail --title "$TITLE" --passwordbox "$1" "${2:-12}" 74 3>&1 1>&2 2>&3; }
msg() { whiptail --title "$TITLE" --msgbox "$1" "${2:-14}" 74; }
die_box() { whiptail --title "$TITLE" --msgbox "$1" "${2:-12}" 74; exit 1; }

# One value from an env file this script wrote (KEY=value or KEY="value").
old_value() {
  [[ -f "$2" ]] || return 0
  grep -oP "^$1=\"?\K[^\"]*" "$2" 2>/dev/null | tail -1
}

OLD_PORT=$(old_value L4D2_STATS_PORT "$CONF")
OLD_TITLE=$(old_value L4D2_STATS_TITLE "$CONF")
OLD_KEY=$(old_value STEAM_API_KEY "$CONF")
OLD_HIDE=$(old_value L4D2_STATS_HIDE "$CONF")
OLD_URL=$(old_value L4D2_STATS_URL "$CONF")
OLD_TOKEN=$(old_value TUNNEL_TOKEN "$TUNNEL_CONF")

# ---------------------------------------------------------------- uninstall
if [[ "${1:-}" == "--uninstall" ]]; then
  whiptail --title "$TITLE" --yesno \
"Remove the stats site?

This deletes:
  $INSTALL_DIR
  $CONF and $TUNNEL_CONF
  the l4d2-stats, l4d2-stats-tunnel and l4d2-stats-maps services

The recorded stats stay in the game folder (l4d2_stats.sq3),
and the l4d2_stats plugin keeps recording them." 17 74 || cancelled
  # One at a time: naming a unit that isn't there (no tunnel was set up) would
  # make systemctl refuse the whole list and leave the site running.
  for unit in l4d2-stats-tunnel.service l4d2-stats-maps.path l4d2-stats-maps.service l4d2-stats.service; do
    systemctl disable --now "$unit" >/dev/null 2>&1
  done
  systemctl clean --what=state l4d2-stats >/dev/null 2>&1   # the cached Steam details
  # !rank and !stats stop pointing players at the removed site.
  if [[ -f "$SERVER_ENV" ]]; then
    # shellcheck disable=SC1090
    . "$SERVER_ENV"
    printf '// Written by stats-setup.sh --uninstall\nl4d2_stats_url ""\n' \
      | sudo -u "${L4D2_USER:-l4d2}" -- sh -c '[ -d "$1" ] && cat >"$1/l4d2_stats.cfg"' sh \
          "${L4D2_DIR:-${L4D2_HOME:-/home/${L4D2_USER:-l4d2}}/l4d2server}/left4dead2/cfg/sourcemod" >/dev/null 2>&1
  fi
  rm -rf "$INSTALL_DIR"
  rm -f "$CONF" "$TUNNEL_CONF" "$UNIT" "$TUNNEL_UNIT" "$MAPS_PATH_UNIT" "$MAPS_RELOAD_UNIT"
  systemctl daemon-reload
  msg "The stats site has been removed. In the Cloudflare dashboard you can
delete the tunnel too (Networks > Tunnels). cloudflared itself stays
installed: sudo apt remove cloudflared removes it." 12
  exit 0
fi

# ---------------------------------------------------------------- preflight
[[ -f "$SERVER_ENV" ]] || die_box "The L4D2 server isn't set up on this machine yet.

Run l4d2-setup.sh first, then come back."

[[ -f "$SCRIPT_DIR/stats/server.py" && -f "$SCRIPT_DIR/stats/static/index.html" ]] \
  || die_box "Can't find the stats site next to this script.

Expected: $SCRIPT_DIR/stats/

Copy the whole repository folder to this machine, then run it again."

python3 -c 'import sys, sqlite3; sys.exit(sys.version_info < (3, 8))' 2>/dev/null \
  || die_box "python3 (3.8 or newer, with sqlite3) is missing. Install it with:
  sudo apt install python3"

# shellcheck disable=SC1090
. "$SERVER_ENV"
GAME_USER="${L4D2_USER:-l4d2}"
GAME_HOME="${L4D2_HOME:-/home/$GAME_USER}"
GAME_DIR="${L4D2_DIR:-$GAME_HOME/l4d2server}"
SQLITE_DIR="$GAME_DIR/left4dead2/addons/sourcemod/data/sqlite"
SM_CFG_DIR="$GAME_DIR/left4dead2/cfg/sourcemod"
MAPS_FILE="$GAME_HOME/maps.json"
AS_GAME_USER=(sudo -u "$GAME_USER" --)
id "$GAME_USER" >/dev/null 2>&1 || die_box "The game user '$GAME_USER' doesn't exist."

# ---------------------------------------------------------------- intro
whiptail --title "$TITLE" --yesno \
"This adds a public web page with your server's stats:
a leaderboard, player pages, records and map stats, built
from every game played here since the stats plugin went in.

It is read-only: no sign-in, nothing on it can change the
server. Visitors see player names, playtime and kills, and
with a Steam key, avatars and L4D2 hours from public Steam
profiles. The admin panel stays private.

Continue?" 18 74 || cancelled

# ---------------------------------------------------------------- settings
while :; do
  PORT=$(ask_input "Local port for the site (only this machine listens on it;
the tunnel connects to it):" "${OLD_PORT:-8090}") || cancelled
  if [[ "$PORT" =~ ^[0-9]+$ ]] && ((PORT >= 1024 && PORT <= 65535 && PORT != ${L4D2_PORT:-27015})); then
    if [[ "$PORT" == "$OLD_PORT" ]] || ! ss -lnt "sport = :$PORT" 2>/dev/null | grep -q LISTEN; then
      break
    fi
    msg "Something else is already using port $PORT. Pick another." 8
  else
    msg "Pick a number from 1024 to 65535 (not the game port)." 8
  fi
done

DEFAULT_TITLE="$OLD_TITLE"
if [[ -z "$DEFAULT_TITLE" ]]; then
  DEFAULT_TITLE=$("${AS_GAME_USER[@]}" cat -- "$GAME_DIR/left4dead2/cfg/server.cfg" 2>/dev/null \
    | grep -oP '^\s*hostname\s+"?\K[^"]*' | head -1)
fi
SITE_TITLE=$(ask_input "Name shown at the top of the site:" "${DEFAULT_TITLE:-L4D2 stats}") || cancelled
# Kept to plain printable text: it goes into an environment file.
SITE_TITLE=$(printf '%s' "$SITE_TITLE" | tr -d '"\\`$' | LC_ALL=C tr -d '\000-\037\177')
SITE_TITLE=$(printf '%s' "$SITE_TITLE" | python3 -I -c 'import sys; print(sys.stdin.buffer.read().decode("utf-8", "ignore")[:80])')
[[ -n "${SITE_TITLE// }" ]] || SITE_TITLE="L4D2 stats"

KEY_PROMPT="Steam Web API key (optional): adds avatars, Steam names,
L4D2 hours and achievements for players with public profiles.
Get one free at https://steamcommunity.com/dev/apikey

Leave blank to"
if [[ -n "$OLD_KEY" ]]; then KEY_PROMPT="$KEY_PROMPT keep the current key. Type 'none' to remove it."
else KEY_PROMPT="$KEY_PROMPT go without Steam details."; fi
while :; do
  KEY=$(ask_secret "$KEY_PROMPT" 15) || cancelled
  KEY=${KEY//[[:space:]]/}
  [[ -z "$KEY" ]] && { KEY="$OLD_KEY"; break; }
  [[ "${KEY,,}" == "none" ]] && { KEY=""; break; }
  [[ "$KEY" =~ ^[0-9A-Fa-f]{32}$ ]] && break
  msg "That doesn't look like a Steam Web API key (32 letters and digits, 0-9 and A-F)." 8
done

while :; do
  HIDE=$(ask_input "Players to leave off the site, as SteamID64s separated by commas
(for anyone who asked not to be shown). Leave blank for nobody.

A player's SteamID64 is the number in their page address on the site." "$OLD_HIDE" 14) || cancelled
  HIDE=${HIDE//[[:space:]]/}
  [[ -z "$HIDE" || "$HIDE" =~ ^[0-9]{15,20}(,[0-9]{15,20})*,?$ ]] && break
  msg "Use SteamID64 numbers (17 digits) separated by commas." 8
done

while :; do
  URL=$(ask_input "The site's public address, if you have one yet, for example
https://stats.example.com - players see it after !rank in chat.
Leave blank if you don't know it yet." "$OLD_URL" 13) || cancelled
  URL=${URL//[[:space:]]/}
  [[ -z "$URL" || "$URL" =~ ^https?://[A-Za-z0-9.-]+(:[0-9]{1,5})?(/[A-Za-z0-9._~/-]*)?$ ]] && break
  msg "Use a plain address like https://stats.example.com" 8
done

# ---------------------------------------------------------------- tunnel
TOKEN=""
TUNNEL_MENU=("tunnel" "Put it on the internet with a Cloudflare Tunnel (recommended)"
             "local" "Only on this machine for now (no tunnel)")
[[ -n "$OLD_TOKEN" ]] && TUNNEL_MENU=("keep" "Keep the current Cloudflare Tunnel" "${TUNNEL_MENU[@]}")
TUNNEL_CHOICE=$(whiptail --title "$TITLE" --menu \
"How should people reach the site?

A Cloudflare Tunnel connects out from this machine to Cloudflare, so
no router port is opened and your home address stays hidden. You
need a free Cloudflare account and a domain on it." 18 78 3 "${TUNNEL_MENU[@]}" 3>&1 1>&2 2>&3) || cancelled

case "$TUNNEL_CHOICE" in
  keep) TOKEN="$OLD_TOKEN" ;;
  tunnel)
    whiptail --title "$TITLE" --scrolltext --msgbox \
"In the Cloudflare dashboard (dash.cloudflare.com):

 1. Zero Trust > Networks > Tunnels > Create a tunnel.
 2. Pick 'Cloudflared', name it (e.g. l4d2-stats), Save.
 3. On the install page, copy the long token: it's the text after
    '--token' in any of the commands shown. Don't run those commands.
 4. Next > Public hostname: pick a subdomain and your domain
    (e.g. stats.example.com). Service: HTTP, URL: localhost:$PORT
 5. Save the tunnel.

Then paste the token on the next screen." 22 78
    while :; do
      PASTED=$(ask_secret "Paste the tunnel token:" 9) || cancelled
      # A whole pasted command works too: the token is its last word.
      read -r -a WORDS <<<"$PASTED"
      TOKEN=""
      (( ${#WORDS[@]} )) && TOKEN=${WORDS[${#WORDS[@]}-1]}
      [[ "$TOKEN" =~ ^eyJ[A-Za-z0-9+/=_-]{40,}$ ]] && break
      msg "That doesn't look like a tunnel token. It is the long text starting
with eyJ after --token in the commands Cloudflare shows." 10
    done ;;
esac

# ---------------------------------------------------------------- install
systemctl stop l4d2-stats >/dev/null 2>&1

install -d -m 0755 "$INSTALL_DIR"
rm -rf "$INSTALL_DIR/stats"
install -d -m 0755 "$INSTALL_DIR/stats" "$INSTALL_DIR/stats/static"
install -m 0644 "$SCRIPT_DIR"/stats/*.py "$INSTALL_DIR/stats/"
install -m 0644 "$SCRIPT_DIR"/stats/static/* "$INSTALL_DIR/stats/static/"

# The site reads the stats through a read-only view of this one folder, so it
# has to exist before the service starts (SourceMod makes it on first use).
# The site runs as a user of its own, so the folder, the stats file and
# maps.json must be readable by others - the server's usual umask makes them
# so; this only puts it back if something tightened it. Their contents are
# what the site publishes anyway.
"${AS_GAME_USER[@]}" mkdir -p -- "$SQLITE_DIR" 2>>"$LOG"
"${AS_GAME_USER[@]}" chmod o+rx -- "$SQLITE_DIR" 2>>"$LOG"
"${AS_GAME_USER[@]}" sh -c 'for f in "$@"; do [ -f "$f" ] && chmod o+r -- "$f"; done; exit 0' sh \
  "$SQLITE_DIR/l4d2_stats.sq3" "$MAPS_FILE" 2>>"$LOG"

TMP_CONF=$(mktemp); chmod 600 "$TMP_CONF"
cat >"$TMP_CONF" <<EOF
# Public stats site for the L4D2 server. Change it by re-running stats-setup.sh.
# Keep this file mode 600: it holds the Steam Web API key.
L4D2_STATS_BIND=127.0.0.1
L4D2_STATS_PORT=$PORT
L4D2_STATS_TITLE="$SITE_TITLE"
L4D2_STATS_DB=$SQLITE_DIR/l4d2_stats.sq3
L4D2_STATS_MAPS=$MAPS_FILE
L4D2_STATS_HIDE=$HIDE
L4D2_STATS_URL=$URL
STEAM_API_KEY=$KEY
EOF
install -m 0600 -o root -g root "$TMP_CONF" "$CONF"; rm -f "$TMP_CONF"

# The site faces the internet, so it runs as a throwaway user of its own
# (DynamicUser): not the game user, whose files include server.cfg and its
# RCON password, and whose game process it could otherwise reach. /home is
# replaced by an empty folder with only the stats folder and maps.json mounted
# back in, read-only.
cat >"$UNIT" <<EOF
[Unit]
Description=L4D2 public stats site
After=network-online.target l4d2.service
Wants=network-online.target
# Restarted after map installs; Restart=always must not run into a start limit.
StartLimitIntervalSec=0

[Service]
Type=simple
DynamicUser=yes
WorkingDirectory=$INSTALL_DIR
EnvironmentFile=$CONF
Environment=PYTHONUNBUFFERED=1
ExecStart=/usr/bin/python3 -m stats.server
Restart=always
RestartSec=5
StateDirectory=l4d2-stats
StateDirectoryMode=0700
UMask=0077
ProtectHome=tmpfs
TemporaryFileSystem=$GAME_HOME:ro
BindReadOnlyPaths=$SQLITE_DIR
BindReadOnlyPaths=-$MAPS_FILE
ProtectSystem=strict
PrivateTmp=yes
PrivateDevices=yes
NoNewPrivileges=yes
CapabilityBoundingSet=
AmbientCapabilities=
ProtectKernelTunables=yes
ProtectKernelModules=yes
ProtectKernelLogs=yes
ProtectControlGroups=yes
ProtectClock=yes
ProtectHostname=yes
ProtectProc=invisible
RestrictRealtime=yes
RestrictSUIDSGID=yes
RestrictNamespaces=yes
LockPersonality=yes
SystemCallArchitectures=native
SystemCallFilter=@system-service
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX
MemoryMax=300M
TasksMax=256

[Install]
WantedBy=multi-user.target
EOF

# A file mounted on its own stays the version that was there when the service
# started, and 'l4d2 addmap' replaces maps.json with a new file. So the site is
# restarted whenever maps.json changes (or first appears), to show new maps' names.
cat >"$MAPS_PATH_UNIT" <<EOF
[Unit]
Description=Watch maps.json for the L4D2 stats site
StartLimitIntervalSec=0

[Path]
PathChanged=$MAPS_FILE
Unit=l4d2-stats-maps.service

[Install]
WantedBy=multi-user.target
EOF
cat >"$MAPS_RELOAD_UNIT" <<EOF
[Unit]
Description=Restart the L4D2 stats site after a map install
# Installing a map pack rewrites maps.json once per file: never give up on it.
StartLimitIntervalSec=0

[Service]
Type=oneshot
# Wait for the rest of a burst of changes; those arriving meanwhile are folded into this run.
ExecStartPre=sleep 3
ExecStart=systemctl try-restart l4d2-stats.service
EOF

# !rank and !stats tell players where the site is. AutoExecConfig only writes
# its own default file when there is none, so this one is kept. Written even
# when the address is blank, so clearing it here clears it in game too.
printf '// Written by stats-setup.sh\nl4d2_stats_url "%s"\n' "$URL" \
  | "${AS_GAME_USER[@]}" sh -c 'mkdir -p -- "$1" && cat >"$1/l4d2_stats.cfg"' sh "$SM_CFG_DIR" 2>>"$LOG" \
  || echo "could not write $SM_CFG_DIR/l4d2_stats.cfg" >>"$LOG"

# The site starts before anything is done about the tunnel, so a failed
# cloudflared install can't leave it stopped.
systemctl daemon-reload
systemctl enable l4d2-stats l4d2-stats-maps.path >>"$LOG" 2>&1
systemctl restart l4d2-stats >>"$LOG" 2>&1
systemctl restart l4d2-stats-maps.path >>"$LOG" 2>&1

# ---------------------------------------------------------------- cloudflared
TUNNEL_NOTE="No tunnel: the site is only reachable on this machine at http://127.0.0.1:$PORT"
if [[ -n "$TOKEN" ]]; then
  if ! command -v cloudflared >/dev/null 2>&1; then
    echo "Installing cloudflared from Cloudflare's package repository..."
    # Cloudflare's documented steps for any Debian-based system (pkg.cloudflare.com).
    { mkdir -p --mode=0755 /usr/share/keyrings \
      && curl -fsSL https://pkg.cloudflare.com/cloudflare-main.gpg -o /usr/share/keyrings/cloudflare-main.gpg \
      && echo 'deb [signed-by=/usr/share/keyrings/cloudflare-main.gpg] https://pkg.cloudflare.com/cloudflared any main' \
           >/etc/apt/sources.list.d/cloudflared.list \
      && apt-get update -qq && apt-get -y -qq install cloudflared; } >>"$LOG" 2>&1 \
      || die_box "Could not install cloudflared. See $LOG

The site itself is installed; run this again to retry the tunnel."
  fi
  CLOUDFLARED=$(command -v cloudflared)

  TMP_CONF=$(mktemp); chmod 600 "$TMP_CONF"
  printf '# Cloudflare Tunnel token for the stats site. Keep this file mode 600.\nTUNNEL_TOKEN=%s\n' "$TOKEN" >"$TMP_CONF"
  install -m 0600 -o root -g root "$TMP_CONF" "$TUNNEL_CONF"; rm -f "$TMP_CONF"

  # The token reaches cloudflared through the environment, never the command
  # line, where every user on the machine could read it.
  cat >"$TUNNEL_UNIT" <<EOF
[Unit]
Description=Cloudflare Tunnel for the L4D2 stats site
After=network-online.target l4d2-stats.service
Wants=network-online.target

[Service]
Type=simple
EnvironmentFile=$TUNNEL_CONF
ExecStart=$CLOUDFLARED --no-autoupdate tunnel run
Restart=always
RestartSec=10
DynamicUser=yes
ProtectHome=yes
ProtectSystem=strict
PrivateTmp=yes
PrivateDevices=yes
NoNewPrivileges=yes
CapabilityBoundingSet=
ProtectKernelTunables=yes
ProtectKernelModules=yes
ProtectKernelLogs=yes
ProtectControlGroups=yes
ProtectClock=yes
ProtectHostname=yes
RestrictRealtime=yes
RestrictSUIDSGID=yes
RestrictNamespaces=yes
LockPersonality=yes
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX AF_NETLINK

[Install]
WantedBy=multi-user.target
EOF
  TUNNEL_NOTE="Cloudflare Tunnel: on (systemctl status l4d2-stats-tunnel).
Open the public hostname you set in the Cloudflare dashboard."
else
  systemctl disable --now l4d2-stats-tunnel >/dev/null 2>&1
  rm -f "$TUNNEL_CONF" "$TUNNEL_UNIT"
  systemctl daemon-reload
fi

if [[ -n "$TOKEN" ]]; then
  systemctl daemon-reload
  systemctl enable l4d2-stats-tunnel >>"$LOG" 2>&1
  systemctl restart l4d2-stats-tunnel >>"$LOG" 2>&1
fi

# ---------------------------------------------------------------- verify
UP=""
for _ in 1 2 3 4 5 6 7 8; do
  sleep 1
  if python3 -I -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:$PORT/healthz', timeout=3)" 2>>"$LOG"; then
    UP=1; break
  fi
done

if [[ -z "$UP" ]]; then
  LOG_VIEW=$(mktemp)
  { printf '%s\n\n' "The stats site isn't answering on 127.0.0.1:$PORT. Recent log:"; journalctl -u l4d2-stats -n 25 --no-pager 2>&1; } >"$LOG_VIEW"
  whiptail --title "Stats site didn't start" --scrolltext --textbox "$LOG_VIEW" 24 90
  rm -f "$LOG_VIEW"
  exit 1
fi

PLUGIN_NOTE="Stats are recorded by the l4d2_stats plugin."
if ! "${AS_GAME_USER[@]}" test -f "$GAME_DIR/left4dead2/addons/sourcemod/plugins/l4d2_stats.smx"; then
  PLUGIN_NOTE="The l4d2_stats plugin isn't installed yet, so nothing is recorded.
Install it with:
  sudo bash l4d2-setup.sh --upgrade
  sudo l4d2 update
(SourceMod is needed; 'l4d2 slots' installs it.)"
fi

whiptail --title "Stats site is running" --scrolltext --msgbox \
"The stats site is running.

$TUNNEL_NOTE

$PLUGIN_NOTE

Players can type !rank, !top and !stats in chat.

Manage it:
  systemctl status l4d2-stats
  journalctl -u l4d2-stats -f
  sudo bash stats-setup.sh               change settings, Steam key, tunnel
  sudo bash stats-setup.sh --uninstall" 26 78
