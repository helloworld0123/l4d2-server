"""Parse the Source engine `status` console output.

Everything in here is untrusted input: player names are chosen by whoever joins
the server. Parsing stays tolerant (never raise on a weird line) and callers are
responsible for escaping before display.
"""

import re

# "#  2 1 "Name" STEAM_1:0:123 05:12 45 0 active 30000"
# The optional second integer is present on some engine builds and absent on others.
_PLAYER_RE = re.compile(
    r'^#\s*(?P<userid>\d+)\s+(?:\d+\s+)?"(?P<name>.*)"\s+(?P<uniqueid>\S+)(?P<rest>.*)$'
)
_TIME_RE = re.compile(r"\b(\d{1,3}:\d{2}(?::\d{2})?)\b")
_FIELD_RE = re.compile(r"^(hostname|map|players|version)\s*:\s*(.*)$", re.IGNORECASE)


class Player:
    __slots__ = ("userid", "name", "uniqueid", "connected", "ping", "is_bot")

    def __init__(self, userid, name, uniqueid, connected, ping, is_bot):
        self.userid = userid
        self.name = name
        self.uniqueid = uniqueid
        self.connected = connected
        self.ping = ping
        self.is_bot = is_bot


def parse_status(text):
    """Return (info: dict, players: list[Player]). Never raises on malformed input."""
    info = {}
    players = []

    for line in (text or "").splitlines():
        line = line.rstrip()

        field = _FIELD_RE.match(line.strip())
        if field and not line.lstrip().startswith("#"):
            info[field.group(1).lower()] = field.group(2).strip()
            continue

        match = _PLAYER_RE.match(line)
        if not match:
            continue

        uniqueid = match.group("uniqueid")
        rest = match.group("rest")
        is_bot = uniqueid.upper().startswith("BOT")

        time_match = _TIME_RE.search(rest)
        connected = time_match.group(1) if time_match else ""

        ping = ""
        tail = rest[time_match.end():] if time_match else rest
        numbers = re.findall(r"\b(\d+)\b", tail)
        if numbers:
            ping = numbers[0]

        players.append(Player(
            userid=match.group("userid"),
            name=match.group("name"),
            uniqueid=uniqueid,
            connected=connected,
            ping=ping,
            is_bot=is_bot,
        ))

    return info, players


def humans(players):
    return [p for p in players if not p.is_bot]


def format_players(info, players):
    """Plain-text summary. The caller escapes before sending."""
    people = humans(players)
    bots = len(players) - len(people)

    lines = []
    if info.get("map"):
        lines.append(f"Map: {info['map'].split(' at:')[0].strip()}")
    if not people:
        lines.append("Nobody is playing right now." + (f" ({bots} bots)" if bots else ""))
        return "\n".join(lines)

    lines.append(f"{len(people)} player(s) connected" + (f", {bots} bots" if bots else ""))
    lines.append("")
    for p in people:
        bits = [f"  {p.name}"]
        if p.connected:
            bits.append(f"for {p.connected}")
        if p.ping:
            bits.append(f"{p.ping}ms")
        lines.append("  ".join(bits))
    return "\n".join(lines)
