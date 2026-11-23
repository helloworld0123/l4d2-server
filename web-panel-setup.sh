#!/usr/bin/env bash
# Installs the web admin panel for the Left 4 Dead 2 server.
# Run this AFTER l4d2-setup.sh has finished.
set -uo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
TITLE="L4D2 Web Panel Setup"
INSTALL_DIR=/opt/l4d2-web
CONF=/etc/l4d2-web.env
UNIT=/etc/systemd/system/l4d2-web.service
SERVER_ENV=/etc/l4d2.env
L4D2_BIN=/usr/local/bin/l4d2
LOG=/var/log/l4d2-web-setup.log
PRIVATE_NETS=(10.0.0.0/8 172.16.0.0/12 192.168.0.0/16)

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

echo "=== L4D2 web panel setup $(date) ===" >>"$LOG"

# ---------------------------------------------------------------- UI helpers
cancelled() {
  whiptail --title "$TITLE" --msgbox "Setup cancelled. Nothing was changed." 8 50
  exit 0
}
ask_input() { whiptail --title "$TITLE" --inputbox "$1" 12 72 -- "$2" 3>&1 1>&2 2>&3; }
ask_secret() { whiptail --title "$TITLE" --passwordbox "$1" 12 72 3>&1 1>&2 2>&3; }
msg() { whiptail --title "$TITLE" --msgbox "$1" "${2:-14}" 72; }
die_box() { whiptail --title "$TITLE" --msgbox "$1" "${2:-12}" 72; exit 1; }

# Remove every firewall rule a previous run could have added for this port, so
# switching access mode never leaves the old door open.
fw_clear() {
  local port=$1 net
  [[ -n "$port" ]] || return 0
  command -v ufw >/dev/null 2>&1 || return 0
  ufw delete allow in on tailscale0 to any port "$port" proto tcp >>"$LOG" 2>&1 || true
  for net in "${PRIVATE_NETS[@]}"; do
    ufw delete allow from "$net" to any port "$port" proto tcp >>"$LOG" 2>&1 || true
  done
}

OLD_PORT=""; OLD_HASH=""
if [[ -f "$CONF" ]]; then
  OLD_PORT=$(grep -oP '^L4D2_WEB_PORT=\K[0-9]+' "$CONF" 2>/dev/null || true)
  OLD_HASH=$(grep -oP '^L4D2_WEB_PASSWORD_HASH=\K.*' "$CONF" 2>/dev/null || true)
fi

# ---------------------------------------------------------------- uninstall
if [[ "${1:-}" == "--uninstall" ]]; then
  whiptail --title "$TITLE" --yesno \
"Remove the web panel?

This deletes:
  $INSTALL_DIR
  $CONF
  $UNIT
and closes its firewall rule.

The game server itself is left alone." 16 72 || cancelled
  systemctl disable --now l4d2-web >/dev/null 2>&1
  fw_clear "$OLD_PORT"
  rm -rf "$INSTALL_DIR"
  rm -f "$CONF" "$UNIT"
  systemctl daemon-reload
  msg "The web panel has been removed." 8
  exit 0
fi

# ---------------------------------------------------------------- preflight
[[ -f "$SERVER_ENV" && -x "$L4D2_BIN" ]] \
  || die_box "The L4D2 server isn't set up on this machine yet.

Run l4d2-setup.sh first, then come back."

[[ -f "$SCRIPT_DIR/web/server.py" && -f "$SCRIPT_DIR/bot/rcon.py" ]] \
  || die_box "Can't find the panel source next to this script.

Expected: $SCRIPT_DIR/web/ and $SCRIPT_DIR/bot/

Copy the whole repository folder to this machine, not just
web-panel-setup.sh, then run it again."

command -v python3 >/dev/null 2>&1 || die_box "python3 is missing. Install it with:
  sudo apt install python3"

# shellcheck disable=SC1090
. "$SERVER_ENV"
CFG_FILE="${L4D2_DIR:-/home/l4d2/l4d2server}/left4dead2/cfg/server.cfg"
GAME_PORT="${L4D2_PORT:-27015}"

GAME_USER="${L4D2_USER:-l4d2}"
# server.cfg sits in a folder the unprivileged game user owns, so they can swap
# the file, or any folder above it, for a link to something of root's at any
# moment. Checking first and then writing as root can't close that race, so
# every read, edit and backup of server.cfg runs as the game user instead and
# the kernel enforces the boundary. (sudo also resets the environment, so an
# admin's own $TMUX does not leak into the game user's tmux calls.)
AS_GAME_USER=(sudo -u "$GAME_USER" --)

# Sets rcon_password in the server.cfg at $1 to the password read from stdin,
# after saving a backup next to it. Run entirely as the game user, and the
# password comes in on stdin so it never shows up in the process list.
set_rcon_password() {
  "${AS_GAME_USER[@]}" bash -c '
    cfg=$1
    IFS= read -r pw || exit 1
    cd / || exit 1
    [[ -f $cfg ]] || exit 1
    # noclobber: a name planted ahead of time (even a link) is refused, not
    # written through.
    ( umask 077; set -C; cat -- "$cfg" >"$cfg.bak.$(date +%s)" ) || exit 1
    tmp=$(mktemp "$cfg.XXXXXX") || exit 1
    found=""
    # Any case, like the game reads it, and only this cvar (not rcon_password_x).
    shopt -s nocasematch
    re="^[[:space:]]*rcon_password([[:space:]\"]|\$)"
    while IFS= read -r line || [[ -n $line ]]; do
      if [[ $line =~ $re ]]; then
        printf "rcon_password \"%s\"\n" "$pw"; found=1
      else
        printf "%s\n" "$line"
      fi
    done <"$cfg" >"$tmp" || { rm -f -- "$tmp"; exit 1; }
    [[ -n $found ]] || printf "\nrcon_password \"%s\"\n" "$pw" >>"$tmp"
    { chmod 640 -- "$tmp" && mv -f -- "$tmp" "$cfg"; } || { rm -f -- "$tmp"; exit 1; }
  ' set_rcon_password "$1"
}

[[ -L "$CFG_FILE" ]] || ! "${AS_GAME_USER[@]}" test -f "$CFG_FILE" \
  && die_box "$CFG_FILE is missing or is not a regular file.

Refusing to touch it. Check that file, then run this again."

# ---------------------------------------------------------------- intro
whiptail --title "$TITLE" --yesno \
"This adds a web page for running your L4D2 server: see who's
playing, kick players, switch maps, install Workshop maps, change
the server name, password, difficulty and player slots, restart,
and use the console - from any browser, including your phone.

Whoever knows the panel password gets full control of the game
server, so it is never opened to the public internet.

Continue?" 17 72 || cancelled

# ---------------------------------------------------------------- access
TS_IP=""
command -v tailscale >/dev/null 2>&1 && TS_IP=$(tailscale ip -4 2>/dev/null | head -1)

# Home-network mode listens on every interface and relies on the firewall to
# keep the internet out, so it is only offered while the firewall is on.
UFW_ON=""
command -v ufw >/dev/null 2>&1 && ufw status 2>/dev/null | grep -q '^Status: active' && UFW_ON=1

ACCESS_ITEMS=()
[[ -n "$TS_IP" ]] && ACCESS_ITEMS+=("tailscale" "Your Tailscale devices only (recommended)")
[[ -n "$UFW_ON" ]] && ACCESS_ITEMS+=("lan" "Devices on the same home network")
ACCESS_ITEMS+=("local" "This machine only (reach it through an SSH tunnel)")

ACCESS=$(whiptail --title "$TITLE" --menu \
"Who should be able to open the panel?

The panel uses plain http, so it is only offered on networks that
are private already. On a public VPS without Tailscale, pick
'This machine only' and use an SSH tunnel." 19 76 3 "${ACCESS_ITEMS[@]}" 3>&1 1>&2 2>&3) || cancelled

while :; do
  PORT=$(ask_input "Port for the panel:" "${OLD_PORT:-8080}") || cancelled
  if [[ "$PORT" =~ ^[0-9]+$ ]] && ((PORT >= 1024 && PORT <= 65535 && PORT != GAME_PORT)); then
    # Our own running panel is allowed to be on the port already.
    if [[ "$PORT" == "$OLD_PORT" ]] || ! ss -lnt "sport = :$PORT" 2>/dev/null | grep -q LISTEN; then
      break
    fi
    msg "Something else is already using port $PORT. Pick another." 8
  else
    msg "Pick a number from 1024 to 65535 (not the game port $GAME_PORT)." 8
  fi
done

case "$ACCESS" in
  tailscale) BIND="$TS_IP" ;;
  lan)       BIND="0.0.0.0" ;;   # the firewall rule below limits who can reach it
  local)     BIND="127.0.0.1" ;;
esac

# ---------------------------------------------------------------- password
HASH=""
if [[ -n "$OLD_HASH" ]] && whiptail --title "$TITLE" --yesno \
"A panel password is already set.

Keep the current password?" 10 60; then
  HASH="$OLD_HASH"
fi

while [[ -z "$HASH" ]]; do
  PASS=$(ask_secret "Choose a password for the panel (at least 10 characters).

Leave it blank to have a random one generated.") || cancelled
  if [[ -z "$PASS" ]]; then
    PASS=$(tr -dc 'A-Za-z0-9' </dev/urandom | head -c 20)
    # Shown from a private file, not a command-line argument, so other users
    # on this machine can't read it out of the process list.
    PASS_FILE=$(mktemp); chmod 600 "$PASS_FILE"
    printf 'Your panel password is:\n\n    %s\n\nWrite it down now - it is not stored anywhere readable.\n(Re-run this script to set a new one.)\n' "$PASS" >"$PASS_FILE"
    whiptail --title "$TITLE" --textbox "$PASS_FILE" 13 72
    rm -f "$PASS_FILE"
  else
    ((${#PASS} >= 10)) || { msg "That's too short - use at least 10 characters." 8; continue; }
    PASS2=$(ask_secret "Type the password again:") || cancelled
    [[ "$PASS" == "$PASS2" ]] || { msg "Those didn't match. Try again." 8; continue; }
  fi
  # The password goes in on stdin so it never shows up in the process list.
  HASH=$(cd "$SCRIPT_DIR" && printf '%s\n' "$PASS" | python3 -m web.auth 2>>"$LOG") || HASH=""
  [[ -n "$HASH" ]] || die_box "Could not hash the password. See $LOG"
done
unset PASS PASS2

# ---------------------------------------------------------------- RCON
RCON_NOW=$("${AS_GAME_USER[@]}" cat -- "$CFG_FILE" 2>/dev/null \
  | grep -oP '^\s*rcon_password\s+"?\K[^"]*' | head -1)
if [[ -z "${RCON_NOW// }" ]]; then
  if whiptail --title "$TITLE" --yesno \
"Remote admin (RCON) is currently off.

The panel needs it to show who's playing and to apply changes
without a restart.

Turn it on now with a randomly generated password?
(Nothing is exposed to the internet - it stays on this machine.)" 16 72; then
    NEW_RCON=$(tr -dc 'A-Za-z0-9' </dev/urandom | head -c 16)
    if printf '%s\n' "$NEW_RCON" | set_rcon_password "$CFG_FILE" 2>>"$LOG"; then
      # Apply to the running server without disconnecting anyone. The command
      # goes through a tmux buffer on stdin rather than send-keys arguments,
      # which anyone could read from the process list.
      printf 'rcon_password %s' "$NEW_RCON" | "${AS_GAME_USER[@]}" sh -c \
        'tmux load-buffer -b l4d2-setup - && tmux paste-buffer -d -b l4d2-setup -t l4d2 && tmux send-keys -t l4d2 Enter' \
        >/dev/null 2>&1
      RCON_NOTE="RCON was enabled and applied to the running server."
    else
      RCON_NOTE="Could not update $CFG_FILE as $GAME_USER, so RCON stayed off. See $LOG"
    fi
  else
    RCON_NOTE="RCON stayed off - the player list and live changes will not work."
  fi
else
  RCON_NOTE="RCON was already enabled."
fi

# ---------------------------------------------------------------- install
# srcds binds RCON to whatever the hostname resolves to (often 127.0.1.1), so
# ask the kernel where it actually listens. The panel also probes at runtime.
RCON_HOST=$(ss -lnt "sport = :$GAME_PORT" 2>/dev/null \
  | awk 'NR>1 {sub(/:[0-9]+$/, "", $4); print $4; exit}')
case "$RCON_HOST" in
  ""|"0.0.0.0"|"*"|"[::]") RCON_HOST=127.0.0.1 ;;
esac

systemctl stop l4d2-web >/dev/null 2>&1

# Names this machine may be opened by, besides its IP address. Anything else
# in the browser's address bar is refused (protects against DNS rebinding).
HOSTNAMES=$(hostname 2>/dev/null | tr -cd 'A-Za-z0-9.-')
if [[ "$ACCESS" == "tailscale" ]]; then
  TS_NAME=$(tailscale status --json 2>/dev/null \
    | python3 -I -c 'import json,sys; print(json.load(sys.stdin)["Self"]["DNSName"].rstrip("."))' 2>/dev/null \
    | tr -cd 'A-Za-z0-9.-')
  [[ -n "$TS_NAME" ]] && HOSTNAMES="$HOSTNAMES ${TS_NAME%%.*} $TS_NAME"
fi

install -d -m 0755 "$INSTALL_DIR"
rm -rf "$INSTALL_DIR/bot" "$INSTALL_DIR/web"
install -d -m 0755 "$INSTALL_DIR/bot" "$INSTALL_DIR/web" "$INSTALL_DIR/web/static"
install -m 0644 "$SCRIPT_DIR"/bot/*.py "$INSTALL_DIR/bot/"
install -m 0644 "$SCRIPT_DIR"/web/*.py "$INSTALL_DIR/web/"
install -m 0644 "$SCRIPT_DIR"/web/static/* "$INSTALL_DIR/web/static/"

# Written via a temp file so the hash is never briefly world-readable.
TMP_CONF=$(mktemp); chmod 600 "$TMP_CONF"
cat >"$TMP_CONF" <<EOF
# Web admin panel for the L4D2 server.
# Keep this file mode 600. Change the password by re-running web-panel-setup.sh.
L4D2_WEB_BIND=$BIND
L4D2_WEB_PORT=$PORT
L4D2_WEB_PASSWORD_HASH=$HASH
L4D2_WEB_HOSTNAMES=$HOSTNAMES
L4D2_RCON_HOST=$RCON_HOST
L4D2_RCON_PORT=$GAME_PORT
EOF
install -m 0600 -o root -g root "$TMP_CONF" "$CONF"; rm -f "$TMP_CONF"

cat >"$UNIT" <<EOF
[Unit]
Description=L4D2 web admin panel
After=network-online.target l4d2.service tailscaled.service
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$INSTALL_DIR
Environment=PYTHONUNBUFFERED=1
ExecStart=/usr/bin/python3 -m web.server
# Also covers boot, when the Tailscale address may not be up yet.
Restart=always
RestartSec=10
# Only light sandboxing: the l4d2 CLI this runs writes under /home/l4d2 and
# /etc, uses sudo, systemctl and tmux, and its children inherit all of this, so
# ProtectHome/ProtectSystem/PrivateTmp/NoNewPrivileges would break it. The
# settings below only take away things neither the CLI nor its children use.
ProtectKernelTunables=yes
ProtectKernelModules=yes
ProtectKernelLogs=yes
ProtectControlGroups=yes
ProtectClock=yes
ProtectHostname=yes
RestrictRealtime=yes
PrivateDevices=yes
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX AF_NETLINK

[Install]
WantedBy=multi-user.target
EOF

# ---------------------------------------------------------------- firewall
fw_clear "$OLD_PORT"
fw_clear "$PORT"
if command -v ufw >/dev/null 2>&1; then
  case "$ACCESS" in
    tailscale) ufw allow in on tailscale0 to any port "$PORT" proto tcp >>"$LOG" 2>&1 ;;
    lan)       for net in "${PRIVATE_NETS[@]}"; do
                 ufw allow from "$net" to any port "$PORT" proto tcp >>"$LOG" 2>&1
               done ;;
  esac
fi

systemctl daemon-reload
systemctl enable l4d2-web >>"$LOG" 2>&1
systemctl restart l4d2-web >>"$LOG" 2>&1

# ---------------------------------------------------------------- verify
case "$ACCESS" in
  tailscale) URL="http://$TS_IP:$PORT"; CHECK="$TS_IP"
             HOWTO="Open it from any device signed in to your Tailscale network." ;;
  lan)       LAN_IP=$(hostname -I 2>/dev/null | awk '{print $1}')
             URL="http://${LAN_IP:-THIS-MACHINE}:$PORT"; CHECK=127.0.0.1
             HOWTO="Open it from any device on the same home network." ;;
  local)     URL="http://localhost:$PORT"; CHECK=127.0.0.1
             HOWTO="From your own computer, open a tunnel first:
  ssh -L $PORT:localhost:$PORT <user>@<this server>
and then visit the address above." ;;
esac

UP=""
for _ in 1 2 3 4 5 6 7 8; do
  sleep 1
  if python3 -I -c "import sys, urllib.request; urllib.request.urlopen('http://$CHECK:$PORT/healthz', timeout=3)" 2>>"$LOG"; then
    UP=1; break
  fi
done

if [[ -n "$UP" ]]; then
  whiptail --title "Web panel is running" --scrolltext --msgbox \
"The panel is live at:

    $URL

$HOWTO

$RCON_NOTE

Sign in with the panel password. Anyone who has it can control
the game server, so don't reuse it and don't share it widely.

Manage the panel:
  systemctl status l4d2-web
  journalctl -u l4d2-web -f
  sudo bash web-panel-setup.sh              change password, port, access
  sudo bash web-panel-setup.sh --uninstall" 24 72
else
  # The log can hold anything the service printed, so it is shown from a
  # private file rather than passed to whiptail on its command line.
  LOG_VIEW=$(mktemp)
  { printf '%s\n\n' "The panel isn't answering on $CHECK:$PORT. Recent log:"; journalctl -u l4d2-web -n 25 --no-pager 2>&1; } >"$LOG_VIEW"
  whiptail --title "Panel didn't start" --scrolltext --textbox "$LOG_VIEW" 24 90
  rm -f "$LOG_VIEW"
  exit 1
fi
