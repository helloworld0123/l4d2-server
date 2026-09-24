"""Bot commands: what each one does and which transport it uses.

Rule of thumb: shell out to the existing `l4d2` CLI when it owns state
(maps.json, /etc/l4d2.env, systemd, steamcmd); use RCON when we need an answer
back. Never shell out to `logs`, `console`, `config` or `menu` - the first
three exec into tail/tmux/$EDITOR and would never return.
"""

import os

from . import jobs, players, rcon, sanitize

MAX_LOG_LINES = 60


def _tail(text, lines=14):
    return "\n".join(text.strip().splitlines()[-lines:])


def _online(ctx):
    """Humans currently connected, or None when RCON can't tell us."""
    ok, out = ctx.try_rcon("status")
    if not ok:
        return None
    _, everyone = players.parse_status(out)
    return players.humans(everyone)


def _disruptive(ctx, user_id, label, fn):
    """Confirm before kicking people off - but only if anyone is actually on."""
    online = _online(ctx)
    if online is None:
        ctx.confirm(user_id, label, fn, "Couldn't check who's online (RCON unavailable).")
    elif not online:
        ctx.send(f"{label} - nobody is playing, going ahead.")
        fn()
    else:
        names = ", ".join(sanitize.display(p.name) for p in online)
        ctx.confirm(user_id, label, fn, f"{len(online)} player(s) connected: {names}")


def _run_job(ctx, name, user_id, argv, timeout, heading):
    """Queue a long-running CLI command, streaming progress into one message."""
    def job():
        message = ctx.send(f"{heading}\nStarting...", preformatted=True)
        throttle = jobs.Throttle(8)

        def on_output(_line, accumulated):
            if throttle.ready():
                ctx.bot.edit(ctx.cfg.chat_id, message["message_id"],
                             f"{heading}\n\n{_tail(accumulated)}", preformatted=True)

        code, out = ctx.cli(*argv, timeout=timeout, on_output=on_output)
        status = "Done." if code == 0 else f"Finished with errors (exit {code})."
        ctx.bot.edit(ctx.cfg.chat_id, message["message_id"],
                     f"{heading}\n{status}\n\n{_tail(out, 25)}", preformatted=True)

    accepted, why = ctx.runner.submit(name, user_id, job)
    if not accepted:
        ctx.send(why)


# --------------------------------------------------------------------- info
def cmd_status(ctx, args, msg):
    code, out = ctx.cli("status")
    online = _online(ctx)
    if online is not None:
        out += f"\nPlayers       : {len(online)} connected"
    ctx.send(out, preformatted=True)


def cmd_players(ctx, args, msg):
    ok, out = ctx.try_rcon("status")
    if not ok:
        ctx.send(f"Can't read the player list.\n{out}")
        return
    info, everyone = players.parse_status(out)
    if not info and not everyone:
        # Never claim "nobody is playing" when we simply failed to parse.
        ctx.send("Couldn't parse the server's reply. Raw output:\n\n" + out, preformatted=True)
        return
    for p in everyone:
        p.name = sanitize.display(p.name)
    ctx.send(players.format_players(info, everyone), preformatted=True)


def cmd_map(ctx, args, msg):
    ok, out = ctx.try_rcon("status")
    if not ok:
        ctx.send(f"Can't reach the server.\n{out}")
        return
    info, _ = players.parse_status(out)
    current = info.get("map", "unknown").split(" at:")[0].strip()
    ctx.send(f"Current map: {current}\nDefault on boot: {ctx.cfg.server_env.get('L4D2_MAP')}")


def cmd_maps(ctx, args, msg):
    ctx.cli_send("maps")


def cmd_info(ctx, args, msg):
    code, out = ctx.cli("info")
    ctx.send(sanitize.redact_info(out), preformatted=True)


def cmd_share(ctx, args, msg):
    ctx.cli_send("share")


def cmd_logs(ctx, args, msg):
    """Read console.log directly. `l4d2 logs` execs `tail -f` and never returns."""
    try:
        count = min(int(args[0]), MAX_LOG_LINES) if args else 25
    except ValueError:
        count = 25
    path = os.path.join(ctx.cfg.server_dir, "left4dead2", "console.log")
    try:
        with open(path, "r", errors="replace") as fh:
            fh.seek(0, os.SEEK_END)
            fh.seek(max(0, fh.tell() - 64 * 1024))
            tail = fh.read().splitlines()[-count:]
    except OSError as e:
        ctx.send(f"Can't read the log: {e}")
        return
    ctx.send("\n".join(tail) or "(log is empty)", preformatted=True)


# ------------------------------------------------------------------ control
def cmd_start(ctx, args, msg):
    ctx.cli_send("start")


def cmd_stop(ctx, args, msg):
    _disruptive(ctx, msg["from"]["id"], "Stop the server",
                lambda: ctx.cli_send("stop"))


def cmd_restart(ctx, args, msg):
    _disruptive(ctx, msg["from"]["id"], "Restart the server",
                lambda: _run_job(ctx, "restart", msg["from"]["id"], ["restart"], 240, "Restarting"))


def cmd_changemap(ctx, args, msg):
    if not args:
        ctx.send("Usage: /changemap <map name> [default]\nSee /maps for installed campaigns.")
        return
    name = args[0]
    if not sanitize.valid_map(name):
        ctx.send(f"'{sanitize.display(name)}' doesn't look like a map name.")
        return
    make_default = len(args) > 1 and args[1].lower() in ("default", "--default")

    def go():
        if make_default:
            _run_job(ctx, "changemap", msg["from"]["id"],
                     ["changemap", name, "--default", "--no-restart"], 240,
                     f"Switching to {name} and making it the default")
        else:
            ok, out = ctx.try_rcon(f"changelevel {name}")
            ctx.send(f"Switching to {name}..." if ok else f"Couldn't switch map.\n{out}")

    _disruptive(ctx, msg["from"]["id"], f"Change map to {name}", go)


def cmd_update(ctx, args, msg):
    _disruptive(ctx, msg["from"]["id"], "Update game files (server goes down, ~10 GB)",
                lambda: _run_job(ctx, "update", msg["from"]["id"], ["update"], 5400,
                                 "Updating game files"))


# --------------------------------------------------------------------- maps
def cmd_addmap(ctx, args, msg):
    if not args:
        ctx.send("Usage: /addmap <workshop link or id> [more ids...]")
        return
    _run_job(ctx, "addmap", msg["from"]["id"],
             ["addmap", *args, "--no-restart"], 1800, "Installing map(s)")


def cmd_removemap(ctx, args, msg):
    if not args:
        ctx.send("Usage: /removemap <workshop id | map name>\nSee /maps.")
        return
    ctx.confirm(msg["from"]["id"], f"Remove {sanitize.display(' '.join(args))}",
                lambda: _run_job(ctx, "removemap", msg["from"]["id"],
                                 ["removemap", *args, "--no-restart"], 300, "Removing"))


def cmd_updatemaps(ctx, args, msg):
    _run_job(ctx, "updatemaps", msg["from"]["id"],
             ["updatemaps", "--no-restart"], 1800, "Updating Workshop maps")


# -------------------------------------------------------------- live admin
def cmd_cheats(ctx, args, msg):
    if not args or args[0].lower() not in ("on", "off"):
        ok, out = ctx.try_rcon("sv_cheats")
        ctx.send(out if ok else f"Can't reach the server.\n{out}")
        return
    value = "1" if args[0].lower() == "on" else "0"
    ok, out = ctx.try_rcon(f"sv_cheats {value}")
    if not ok:
        ctx.send(f"Failed.\n{out}")
        return
    ctx.send(f"Cheats {args[0].lower()} (sv_cheats {value}).")


def cmd_difficulty(ctx, args, msg):
    if not args or args[0].lower() not in sanitize.DIFFICULTIES:
        ctx.send("Usage: /difficulty " + " | ".join(sanitize.DIFFICULTIES))
        return
    ok, out = ctx.try_rcon(f"z_difficulty {args[0].capitalize()}")
    ctx.send(f"Difficulty set to {args[0].capitalize()}." if ok else f"Failed.\n{out}")


def cmd_say(ctx, args, msg):
    text = sanitize.console_arg(" ".join(args))
    if not text:
        ctx.send("Usage: /say <message>")
        return
    ok, out = ctx.try_rcon(f'say "{text}"')
    ctx.send("Sent." if ok else f"Failed.\n{out}")


def cmd_kick(ctx, args, msg):
    """Kick by userid, never by name - names are attacker-chosen and quoted."""
    if not args:
        ctx.send("Usage: /kick <name or #userid>   (see /players)")
        return
    online = _online(ctx)
    if online is None:
        ctx.send("Can't reach RCON to look up players.")
        return

    wanted = args[0].lstrip("#").lower()
    matches = [p for p in online if p.userid == wanted or wanted in p.name.lower()]
    if not matches:
        ctx.send("No connected player matches that.")
        return
    if len(matches) > 1:
        names = ", ".join(f"#{p.userid} {sanitize.display(p.name)}" for p in matches)
        ctx.send(f"That matches several players - use the userid.\n{names}")
        return

    target = matches[0]
    label = f"Kick #{target.userid} {sanitize.display(target.name)}"
    ctx.confirm(msg["from"]["id"], label,
                lambda: ctx.send("Kicked." if ctx.try_rcon(f"kickid {target.userid}")[0]
                                 else "Kick failed."))


def cmd_rcon(ctx, args, msg):
    """Deliberately unrestricted, except for commands that break the bot itself."""
    raw = " ".join(args).strip()
    if not raw:
        ctx.send("Usage: /rcon <console command>")
        return
    if sanitize.RCON_REFUSED.match(raw):
        ctx.send("Refused: that would change the RCON settings the bot depends on. "
                 "Edit server.cfg on the server instead.")
        return

    def go():
        ok, out = ctx.try_rcon(raw)
        ctx.send(out if ok and out else ("(no output)" if ok else out), preformatted=True)

    if sanitize.RCON_CONFIRM.match(raw):
        _disruptive(ctx, msg["from"]["id"], f"Run: {sanitize.display(raw, 80)}", go)
    else:
        go()


# ----------------------------------------------------------------- registry
def cmd_help(ctx, args, msg):
    lines = ["L4D2 server bot", ""]
    for group, names in GROUPS:
        lines.append(group)
        for name in names:
            lines.append(f"  /{name:<12} {DESCRIPTIONS[name]}")
        lines.append("")
    ctx.send("\n".join(lines), preformatted=True)


def cmd_id(ctx, args, msg):
    ctx.send(f"chat id: {msg['chat']['id']}\nyour user id: {msg['from']['id']}")


HANDLERS = {
    "help": cmd_help, "id": cmd_id,
    "status": cmd_status, "players": cmd_players, "map": cmd_map, "maps": cmd_maps,
    "info": cmd_info, "share": cmd_share, "logs": cmd_logs,
    "start": cmd_start, "stop": cmd_stop, "restart": cmd_restart,
    "changemap": cmd_changemap, "update": cmd_update,
    "addmap": cmd_addmap, "removemap": cmd_removemap, "updatemaps": cmd_updatemaps,
    "cheats": cmd_cheats, "difficulty": cmd_difficulty, "say": cmd_say,
    "kick": cmd_kick, "rcon": cmd_rcon,
}

DESCRIPTIONS = {
    "help": "This list", "id": "Show this chat's id",
    "status": "Is the server up?", "players": "Who's playing",
    "map": "Current map", "maps": "Installed custom maps",
    "info": "How friends connect", "share": "Workshop links to send friends",
    "logs": "Last lines of the server log",
    "start": "Start the server", "stop": "Stop the server", "restart": "Restart the server",
    "changemap": "Switch map: /changemap <name> [default]",
    "update": "Update game files (slow)",
    "addmap": "Install a Workshop map", "removemap": "Uninstall a map",
    "updatemaps": "Re-download changed Workshop maps",
    "cheats": "/cheats on | off", "difficulty": "/difficulty easy|normal|hard|impossible",
    "say": "Send a message to players", "kick": "Kick a player",
    "rcon": "Run any console command",
}

GROUPS = [
    ("Info", ["status", "players", "map", "maps", "info", "share", "logs"]),
    ("Control", ["start", "stop", "restart", "changemap", "update"]),
    ("Maps", ["addmap", "removemap", "updatemaps"]),
    ("Live admin", ["cheats", "difficulty", "say", "kick", "rcon"]),
]
