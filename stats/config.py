"""Settings for the stats site, from the environment (/etc/l4d2-stats.env via systemd)."""

import os
import re

STEAMID_RE = re.compile(r"[0-9]{15,20}")
DEFAULT_GAME_HOME = "/home/l4d2"


class ConfigError(Exception):
    pass


class StatsConfig:
    def __init__(self, db_path, maps_path="", bind="127.0.0.1", port=8090, title="L4D2 stats",
                 hidden=(), steam_key="", state_dir=""):
        self.db_path = db_path
        self.maps_path = maps_path
        self.bind = bind
        self.port = port
        self.title = title
        self.hidden = frozenset(hidden)
        self.steam_key = steam_key
        self.state_dir = state_dir

    @classmethod
    def load(cls, environ=None):
        env = os.environ if environ is None else environ
        home = env.get("L4D2_HOME", DEFAULT_GAME_HOME)
        game_dir = env.get("L4D2_DIR", os.path.join(home, "l4d2server"))
        try:
            port = int(env.get("L4D2_STATS_PORT", "8090"))
        except ValueError:
            raise ConfigError("L4D2_STATS_PORT must be a number.")
        if not 1 <= port <= 65535:
            raise ConfigError("L4D2_STATS_PORT must be from 1 to 65535.")
        hidden = [part.strip() for part in env.get("L4D2_STATS_HIDE", "").replace(";", ",").split(",")]
        bad = [part for part in hidden if part and not STEAMID_RE.fullmatch(part)]
        if bad:
            raise ConfigError(f"L4D2_STATS_HIDE holds something that isn't a SteamID64: {bad[0]!r}")
        key = env.get("STEAM_API_KEY", "").strip()
        if key and not re.fullmatch(r"[0-9A-Fa-f]{32}", key):
            raise ConfigError("STEAM_API_KEY doesn't look like a Steam Web API key (32 hex characters).")
        return cls(
            db_path=env.get("L4D2_STATS_DB") or os.path.join(
                game_dir, "left4dead2", "addons", "sourcemod", "data", "sqlite", "l4d2_stats.sq3"),
            maps_path=env.get("L4D2_STATS_MAPS") or os.path.join(home, "maps.json"),
            bind=env.get("L4D2_STATS_BIND", "127.0.0.1"),
            port=port,
            title=(env.get("L4D2_STATS_TITLE") or "L4D2 stats").strip()[:80],
            hidden=[part for part in hidden if part],
            steam_key=key,
            state_dir=env.get("L4D2_STATS_STATE") or env.get("STATE_DIRECTORY", ""),
        )
