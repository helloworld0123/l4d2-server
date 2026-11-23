#!/usr/bin/env bash
# Installs a Telegram control bot for the Left 4 Dead 2 server.
# Run this AFTER l4d2-setup.sh has finished.
set -uo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
TITLE="L4D2 Telegram Bot Setup"
INSTALL_DIR=/opt/l4d2-telegram
CONF=/etc/l4d2-telegram.env
UNIT=/etc/systemd/system/l4d2-telegram.service
STATE_DIR=/var/lib/l4d2-telegram
SERVER_ENV=/etc/l4d2.env
L4D2_BIN=/usr/local/bin/l4d2
LOG=/var/log/l4d2-telegram-setup.log

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

echo "=== L4D2 telegram bot setup $(date) ===" >>"$LOG"

# ---------------------------------------------------------------- UI helpers
cancelled() {
  whiptail --title "$TITLE" --msgbox "Setup cancelled. Nothing was changed." 8 50
  exit 0
}
# "--" matters: whiptail reads a leading-dash value as a flag, and group
# chat ids are negative (e.g. -5361866764).
ask_input() { whiptail --title "$TITLE" --inputbox "$1" 12 72 -- "$2" 3>&1 1>&2 2>&3; }
msg() { whiptail --title "$TITLE" --msgbox "$1" "${2:-14}" 72; }
die_box() { whiptail --title "$TITLE" --msgbox "$1" "${2:-12}" 72; exit 1; }
# Drop anything typed while a long step was running, so stray keypresses
# don't silently answer the next dialog.
flush_input() { while read -r -t 0.05 -n 256 _ 2>/dev/null; do :; done; }

# ---------------------------------------------------------------- uninstall
if [[ "${1:-}" == "--uninstall" ]]; then
  whiptail --title "$TITLE" --yesno \
"Remove the Telegram bot?

This deletes:
  $INSTALL_DIR
  $CONF   (your bot token)
  $UNIT
  $STATE_DIR

The game server itself is left alone." 16 72 || cancelled
  systemctl disable --now l4d2-telegram >/dev/null 2>&1
  rm -rf "$INSTALL_DIR" "$STATE_DIR"
  rm -f "$CONF" "$UNIT"
  systemctl daemon-reload
  msg "The Telegram bot has been removed.

Remember to delete the bot itself in Telegram:
open @BotFather and send /deletebot" 12
  exit 0
fi

# ---------------------------------------------------------------- preflight
[[ -f "$SERVER_ENV" && -x "$L4D2_BIN" ]] \
  || die_box "The L4D2 server isn't set up on this machine yet.

Run l4d2-setup.sh first, then come back."

[[ -f "$SCRIPT_DIR/bot/main.py" ]] \
  || die_box "Can't find the bot source next to this script.

Expected: $SCRIPT_DIR/bot/main.py

Copy the whole repository folder to this machine, not just
telegram-bot-setup.sh, then run it again."

command -v python3 >/dev/null 2>&1 || die_box "python3 is missing. Install it with:
  sudo apt install python3"

# shellcheck disable=SC1090
. "$SERVER_ENV"
CFG_FILE="${L4D2_DIR:-/home/l4d2/l4d2server}/left4dead2/cfg/server.cfg"

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

# Pre-fill from an existing install so re-runs are quick.
OLD_TOKEN=""; OLD_CHAT=""
if [[ -f "$CONF" ]]; then
  OLD_TOKEN=$(grep -oP '^TELEGRAM_BOT_TOKEN=\K.*' "$CONF" 2>/dev/null || true)
  OLD_CHAT=$(grep -oP '^TELEGRAM_CHAT_ID=\K.*' "$CONF" 2>/dev/null || true)
fi

# A running bot would fight us for getUpdates during chat discovery.
systemctl stop l4d2-telegram >/dev/null 2>&1

# ---------------------------------------------------------------- intro
whiptail --title "$TITLE" --yesno \
"This adds a Telegram bot that controls your L4D2 server from a
group chat: check who's playing, change maps, install Workshop
maps, restart, and run admin commands.

IMPORTANT: everyone in that group gets full control of the game
server, including cheats and raw console commands. Use a private
group and don't share the invite link.

You'll need a bot token from @BotFather. Continue?" 18 72 || cancelled

# ---------------------------------------------------------------- token
# The token is a secret, so it never goes on a command line (whiptail's or
# python's), where any local user could read it from the process list. An
# existing one is not pre-filled either: only its last few characters are shown.
TOKEN=""
if [[ -n "$OLD_TOKEN" ]] && whiptail --title "$TITLE" --yesno \
"A bot token is already set up (ending in ...${OLD_TOKEN: -4}).

Keep using it?   (No = paste a new one)" 11 72; then
  TOKEN="$OLD_TOKEN"
else
  msg "First, create the bot:

 1. Open Telegram and search for  @BotFather
 2. Send  /newbot
 3. Give it a name (anything) and a username ending in 'bot'
 4. BotFather replies with a token that looks like
      123456789:AAxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
 5. Copy it - you'll paste it on the next screen" 18
fi

BOT_USERNAME=""
while :; do
  if [[ -z "$TOKEN" ]]; then
    TOKEN=$(ask_input "Paste the bot token from @BotFather:" "") || cancelled
    TOKEN=$(tr -d ' \t\r\n' <<<"$TOKEN")
    [[ -z "$TOKEN" ]] && { msg "The token can't be empty." 8; continue; }
  fi

  BOT_USERNAME=$(TG_TOKEN="$TOKEN" python3 -I - <<'PY'
import json, os, urllib.request
try:
    with urllib.request.urlopen(
        f"https://api.telegram.org/bot{os.environ['TG_TOKEN']}/getMe", timeout=20
    ) as r:
        print(json.load(r)["result"]["username"])
except Exception:
    pass
PY
)
  [[ -n "$BOT_USERNAME" ]] && break
  TOKEN=""
  whiptail --title "$TITLE" --yesno \
"Telegram didn't accept that token.

Check you copied all of it, with no extra spaces.

Try again?" 12 72 || cancelled
done

msg "Connected to @$BOT_USERNAME" 8

# ---------------------------------------------------------------- chat id
# Telegram sends a my_chat_member update when the bot is added to a group, and
# getUpdates delivers that by default - so adding the bot is usually enough and
# nothing has to be typed in the chat.
CHAT_ID=""
while [[ -z "$CHAT_ID" ]]; do
  msg "Now point the bot at your group:

 1. Create a Telegram group (or open the one you want to use)
 2. Add  @$BOT_USERNAME  to it

That's normally all it takes - the bot notices when it's added.

If the bot is already in the group, it's found straight away
and the bar closes immediately - that's normal.

Press OK and a progress bar will wait for it." 20

  RESULT=$(mktemp)
  TG_TOKEN="$TOKEN" python3 -I - "$RESULT" 2>>"$LOG" <<'PY' | whiptail --title "$TITLE" --gauge \
    "Watching for @$BOT_USERNAME to appear in a chat...\n\nAdd it to your group now. This closes as soon as it shows up." 10 72 0
import json, os, sys, time, urllib.parse, urllib.request

token, out_path = os.environ["TG_TOKEN"], sys.argv[1]
WINDOW = 180.0
deadline = time.time() + WINDOW
fresh, stale, offset = {}, {}, None


def api(method, **params):
    url = f"https://api.telegram.org/bot{token}/{method}?" + urllib.parse.urlencode(params)
    with urllib.request.urlopen(url, timeout=30) as r:
        return json.load(r).get("result", [])


def collect(update, into):
    global offset
    offset = update["update_id"] + 1
    for key in ("message", "my_chat_member", "edited_message"):
        chat = (update.get(key) or {}).get("chat") or {}
        if chat.get("id") is not None:
            into[chat["id"]] = (
                chat.get("type") or "?",
                chat.get("title") or chat.get("username") or "direct chat",
            )


def tick():
    left = max(0.0, deadline - time.time())
    print(min(99, int(100 * (1 - left / WINDOW))), flush=True)


# Telegram keeps undelivered updates for 24h, so a previous run's messages are
# still queued. Drain them first: the wait below must react only to what the
# user does from now on, not to something they did ten minutes ago.
try:
    while True:
        batch = api("getUpdates", timeout=0, **({"offset": offset} if offset else {}))
        if not batch:
            break
        for update in batch:
            collect(update, stale)
except Exception:
    pass

# If the bot was already sitting in a group before setup ran, there is nothing
# to wait for.
already_in_group = any(kind in ("group", "supergroup") for kind, _ in stale.values())

while not already_in_group and not fresh and time.time() < deadline:
    tick()
    try:
        # Short polls so the bar keeps moving and we stop as soon as it appears.
        params = {"timeout": 5}
        if offset is not None:
            params["offset"] = offset
        for update in api("getUpdates", **params):
            collect(update, fresh)
    except Exception:
        time.sleep(2)

print(100, flush=True)

rows = dict(stale)
rows.update(fresh)
with open(out_path, "w") as fh:
    for chat_id, (chat_type, title) in rows.items():
        fh.write(f"{chat_id}\t{chat_type}\t{title}\n")
PY

  mapfile -t FOUND <"$RESULT"
  rm -f "$RESULT"

  # Anything typed at the blank screen would otherwise dismiss the next dialog.
  flush_input

  MENU_ARGS=(); IDS=(); GROUP_COUNT=0; PRIVATE_COUNT=0; N=0
  for row in "${FOUND[@]}"; do
    IFS=$'\t' read -r c_id c_type c_title <<<"$row"
    case "$c_type" in
      group|supergroup) label="$c_title"; GROUP_COUNT=$((GROUP_COUNT + 1)) ;;
      private)          label="$c_title (direct chat, just you)"; PRIVATE_COUNT=$((PRIVATE_COUNT + 1)) ;;
      *) continue ;;
    esac
    N=$((N + 1))
    IDS[$N]="$c_id"
    # Index the entries rather than using the id as the menu tag: whiptail
    # treats a leading '-' as a command-line flag and group ids are negative.
    MENU_ARGS+=("$N" "$label  [$c_id]")
  done

  if ((${#MENU_ARGS[@]} == 0)); then
    whiptail --title "$TITLE" --yesno \
"Didn't see the bot appear in any chat.

Things to check:
  - @$BOT_USERNAME was actually added to the group
  - you're looking at the right Telegram account
  - if it was already in the group, send  /start@$BOT_USERNAME

Try again?   (No = type the chat id in by hand)" 17 72 && continue

    while :; do
      CHAT_ID=$(ask_input "Chat id.

Group ids are negative, e.g. -1001234567890.
Leave blank to go back." "$OLD_CHAT") || { CHAT_ID=""; break; }
      [[ -z "$CHAT_ID" ]] && break
      [[ "$CHAT_ID" =~ ^-?[0-9]+$ ]] && break
      msg "That's not a number." 8
    done
    continue
  fi

  if ((GROUP_COUNT == 0 && PRIVATE_COUNT > 0)); then
    whiptail --title "$TITLE" --yesno \
"The bot only saw a direct chat with you, not a group.

That usually means you messaged the bot privately instead of
adding it to a group.

A direct chat works fine (only you can control the server).
Use it?   (No = go back and try the group again)" 16 72 || continue
  fi

  CHOICE=$(whiptail --title "$TITLE" --menu \
    "Which chat should control the server?" 16 72 6 "${MENU_ARGS[@]}" 3>&1 1>&2 2>&3) || CHOICE=""
  [[ -n "$CHOICE" ]] && CHAT_ID="${IDS[$CHOICE]}"
done

# ---------------------------------------------------------------- RCON
RCON_NOW=$("${AS_GAME_USER[@]}" cat -- "$CFG_FILE" 2>/dev/null \
  | grep -oP '^\s*rcon_password\s+"?\K[^"]*' | head -1)
if [[ -z "${RCON_NOW// }" ]]; then
  if whiptail --title "$TITLE" --yesno \
"Remote admin (RCON) is currently off.

The bot needs it for the live commands:
  /players  /kick  /say  /cheats  /difficulty

Turn it on now with a randomly generated password?
(Nothing is exposed to the internet - it stays on this machine.)" 17 72; then
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
    RCON_NOTE="RCON stayed off - /players, /kick, /say and /cheats will not work."
  fi
else
  RCON_NOTE="RCON was already enabled."
fi

# ---------------------------------------------------------------- install
# srcds binds its RCON socket to whatever the hostname resolves to, not to
# localhost. On Debian and Ubuntu that is usually 127.0.1.1, so 127.0.0.1 gets
# "connection refused" even with the server running. Ask the kernel where the
# socket actually is rather than guessing. (The bot also probes for this at
# runtime, so an empty answer here is not fatal.)
RCON_HOST=$(ss -lnt "sport = :${L4D2_PORT:-27015}" 2>/dev/null \
  | awk 'NR>1 {sub(/:[0-9]+$/, "", $4); print $4; exit}')
case "$RCON_HOST" in
  ""|"0.0.0.0"|"*"|"[::]") RCON_HOST=127.0.0.1 ;;
esac

install -d -m 0755 "$INSTALL_DIR"
rm -rf "$INSTALL_DIR/bot"
install -d -m 0755 "$INSTALL_DIR/bot"
install -m 0644 "$SCRIPT_DIR"/bot/*.py "$INSTALL_DIR/bot/"
install -d -m 0750 "$STATE_DIR"

# Written via a temp file so the token is never briefly world-readable.
TMP_CONF=$(mktemp); chmod 600 "$TMP_CONF"
cat >"$TMP_CONF" <<EOF
# Telegram control bot for the L4D2 server.
# Contains a secret - keep this file mode 600.
TELEGRAM_BOT_TOKEN=$TOKEN
TELEGRAM_CHAT_ID=$CHAT_ID
L4D2_RCON_HOST=$RCON_HOST
L4D2_RCON_PORT=${L4D2_PORT:-27015}
EOF
install -m 0600 -o root -g root "$TMP_CONF" "$CONF"; rm -f "$TMP_CONF"

cat >"$UNIT" <<EOF
[Unit]
Description=L4D2 Telegram control bot
After=network-online.target l4d2.service
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$INSTALL_DIR
Environment=PYTHONUNBUFFERED=1
ExecStart=/usr/bin/python3 -m bot.main
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

systemctl daemon-reload
systemctl enable l4d2-telegram >>"$LOG" 2>&1
systemctl restart l4d2-telegram >>"$LOG" 2>&1

# ---------------------------------------------------------------- verify
sleep 4
if systemctl is-active --quiet l4d2-telegram; then
  whiptail --title "Telegram bot is running" --scrolltext --msgbox \
"The bot is live as @$BOT_USERNAME.

$RCON_NOTE

It should have posted 'Server bot is online' in your group.
Send /help there to see everything it can do.

Anyone in that group can control the server, including cheats
and raw console commands. Keep the group private.

Manage the bot:
  systemctl status l4d2-telegram
  journalctl -u l4d2-telegram -f
  sudo bash telegram-bot-setup.sh --uninstall" 22 72
else
  # The log can hold anything the service printed, so it is shown from a
  # private file rather than passed to whiptail on its command line.
  LOG_VIEW=$(mktemp)
  { printf '%s\n\n' "The service failed to start. Recent log:"; journalctl -u l4d2-telegram -n 25 --no-pager 2>&1; } >"$LOG_VIEW"
  whiptail --title "Bot didn't start" --scrolltext --textbox "$LOG_VIEW" 24 90
  rm -f "$LOG_VIEW"
  exit 1
fi
