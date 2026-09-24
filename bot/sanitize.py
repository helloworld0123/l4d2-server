"""Cleaning of untrusted text in both directions.

Two distinct threats:
  - Text going *to* the game server. The Source console splits commands on
    ';', so an unsanitised `say` argument is arbitrary command execution:
    `/say hi; quit` would stop the server.
  - Text coming *from* the game server. Player names are chosen by whoever
    joins, and end up rendered in a chat message.
"""

import re

MAPNAME_RE = re.compile(r"^[A-Za-z0-9_\-]{1,64}$")
DIFFICULTIES = ("easy", "normal", "hard", "impossible")

# Zero-width and bidirectional-override characters: a standard trick for
# making a player name display as something other than what it is.
_INVISIBLE_RE = re.compile(r"[​-‏‪-‮⁦-⁩﻿]")
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")

# Commands that would break the bot's own access or the server's config.
RCON_REFUSED = re.compile(r"^\s*(rcon_password|sv_rcon\w*)\b", re.IGNORECASE)
RCON_CONFIRM = re.compile(r"^\s*(quit|exit|_restart|sv_password)\b", re.IGNORECASE)


def console_arg(text, limit=200):
    """Make user text safe to embed in a quoted console argument."""
    text = _CONTROL_RE.sub("", text or "")
    text = text.replace("\n", " ").replace("\r", " ")
    # ';' ends a console command; '"' ends our quoted argument.
    text = text.replace(";", ",").replace('"', "'")
    return text.strip()[:limit]


def display(text, limit=48):
    """Make server-provided text safe and sane to show in chat.

    HTML escaping happens separately at send time; this handles the things
    escaping does not, such as invisible characters and absurd lengths.
    """
    text = _CONTROL_RE.sub("", text or "")
    text = _INVISIBLE_RE.sub("", text)
    text = text.strip()
    if len(text) > limit:
        text = text[: limit - 1] + "…"
    return text or "(unnamed)"


def valid_map(name):
    return bool(MAPNAME_RE.match(name or ""))


_RCON_LINE_RE = re.compile(r"^(\s*RCON\s*:\s*).*$", re.MULTILINE)


def redact_info(text):
    """Strip the RCON password out of `l4d2 info` output.

    SERVER-INFO.txt holds the admin password in plaintext; the server password
    stays, since handing it to friends is what /info is for.
    """
    return _RCON_LINE_RE.sub(r"\1(hidden - run 'l4d2 info' on the server)", text or "")
