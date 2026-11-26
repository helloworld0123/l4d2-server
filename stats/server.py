"""Entry point: python3 -m stats.server

A public, read-only site: GET only, no sign-in, no cookies. It listens on
localhost and is published by a Cloudflare Tunnel (stats-setup.sh), which
passes the visitor's address in CF-Connecting-IP.
"""

import collections
import ipaddress
import json
import os
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import config as config_mod, db as db_mod, maps as maps_mod, steam as steam_mod
from .config import STEAMID_RE

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
STATIC = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/app.css": ("app.css", "text/css; charset=utf-8"),
    "/favicon.svg": ("favicon.svg", "image/svg+xml"),
}
IMG_HOSTS = " ".join(f"https://{h}" for h in sorted(steam_mod.AVATAR_HOSTS))
SECURITY_HEADERS = {
    "Content-Security-Policy": (f"default-src 'self'; img-src 'self' {IMG_HOSTS}; frame-ancestors 'none'; "
                                "base-uri 'none'; form-action 'none'"),
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cross-Origin-Opener-Policy": "same-origin",
}
CACHE_SECONDS = 30
CACHE_ENTRIES = 512
RATE = 2.0                 # requests per second each visitor may average...
BURST = 60                 # ...after a burst of this many
MAX_VISITORS = 20000


def log(message):
    print(f"[{time.strftime('%H:%M:%S')}] {message}", flush=True)


class BadRequest(Exception):
    pass


class Limiter:
    """A token bucket per visitor address."""

    def __init__(self, rate=RATE, burst=BURST, clock=time.monotonic):
        self.rate, self.burst, self.clock = rate, burst, clock
        self.buckets = collections.OrderedDict()
        self.lock = threading.Lock()

    def allow(self, who):
        now = self.clock()
        with self.lock:
            tokens, last = self.buckets.pop(who, (self.burst, now))
            tokens = min(self.burst, tokens + (now - last) * self.rate)
            allowed = tokens >= 1
            self.buckets[who] = (tokens - 1 if allowed else tokens, now)
            while len(self.buckets) > MAX_VISITORS:
                self.buckets.popitem(last=False)
            return allowed


class Cache:
    """Answers kept for a few seconds, so a crowd costs a handful of queries."""

    def __init__(self, seconds=CACHE_SECONDS, clock=time.monotonic):
        self.seconds, self.clock = seconds, clock
        self.items = collections.OrderedDict()
        self.lock = threading.Lock()

    def get(self, key, make):
        now = self.clock()
        with self.lock:
            hit = self.items.get(key)
            if hit and hit[0] > now:
                return hit[1]
        value = make()
        if value is None:
            return None         # "not found" isn't kept: made-up ids must not crowd out real answers
        with self.lock:
            self.items[key] = (now + self.seconds, value)
            self.items.move_to_end(key)
            while len(self.items) > CACHE_ENTRIES:
                self.items.popitem(last=False)
        return value


def _one(query, name, allowed, default):
    value = (query.get(name) or [default])[0]
    if value not in allowed:
        raise BadRequest(f"{name} must be one of: {', '.join(allowed)}")
    return value


class App:
    def __init__(self, cfg, log=log, steam=None, clock=time.time):
        self.cfg = cfg
        self.log = log
        self.maps = maps_mod.MapNames(cfg.maps_path, clock=clock)
        self.db = db_mod.StatsDB(cfg.db_path, self.maps, hidden=cfg.hidden, clock=clock)
        cache_file = os.path.join(cfg.state_dir, "steam.json") if cfg.state_dir else ""
        self.steam = steam if steam is not None else steam_mod.SteamCache(cfg.steam_key, cache_file, log=log)
        self.cache = Cache()
        # Player pages get their own cache, so asking for many players can't push
        # the leaderboard and records out of the main one.
        self.player_cache = Cache()
        self.limiter = Limiter()
        self.static = {}
        for route, (name, content_type) in STATIC.items():
            with open(os.path.join(STATIC_DIR, name), "rb") as fh:
                self.static[route] = (fh.read(), content_type)

    def _with_steam(self, player):
        entry = self.steam.get(player["steamid"]) if self.steam.enabled else None
        player["avatar"] = (entry or {}).get("avatar")
        return player

    # Each returns a JSON-able dict, or None for "not found".
    def summary(self, q):
        data = self.db.summary()
        data.update(title=self.cfg.title, steam=self.steam.enabled, addons=len(self.maps.addons()))
        live = data.get("live") or {}
        if live.get("online"):
            workshop = self.maps.workshop_for(live.get("map"))
            live["links"] = maps_mod.workshop_links(workshop) if workshop else None
        for p in (data.get("live") or {}).get("players", []):
            self._with_steam(p)
        return data

    def leaderboard(self, q):
        data = self.db.leaderboard(_one(q, "period", db_mod.PERIODS, "all"),
                                   _one(q, "scope", db_mod.SCOPES, "ranked"),
                                   _one(q, "sort", db_mod.SORTS, "points"))
        for p in data["players"]:
            self._with_steam(p)
        return data

    def player(self, q):
        steamid = (q.get("id") or [""])[0]
        if not STEAMID_RE.fullmatch(steamid):
            raise BadRequest("id must be a SteamID64.")
        data = self.db.player(steamid)
        if data is None:
            return None
        entry = self.steam.get(steamid) if self.steam.enabled else None
        public = bool(entry and entry.get("public"))
        # Lifetime figures only while the profile is public: someone who has made
        # theirs private since doesn't keep showing their old hours.
        data["steam"] = None if entry is None else {
            "persona": entry.get("persona"), "avatar": entry.get("avatar_full") or entry.get("avatar"),
            "public": public, "hours": entry.get("hours") if public else None,
            "achievements": entry.get("achievements") if public else None}
        data["profile"] = f"https://steamcommunity.com/profiles/{steamid}"
        return data

    def records(self, q):
        data = self.db.records(_one(q, "scope", db_mod.SCOPES, "ranked"))
        for group in ("chapter", "career"):
            for item in data.get(group, []):
                self._with_steam(item)
        return data

    def map_stats(self, q):
        data = self.db.map_stats()
        data["addons"] = self.maps.addons()    # shown even before any game is recorded
        return data


ROUTES = {
    "/api/summary": App.summary,
    "/api/leaderboard": App.leaderboard,
    "/api/player": App.player,
    "/api/records": App.records,
    "/api/maps": App.map_stats,
}
# The options each page actually reads. Anything else is dropped before the
# cache key is made, so made-up parameters can't force a recompute or crowd out
# real answers.
ROUTE_PARAMS = {
    "/api/summary": (),
    "/api/leaderboard": ("period", "scope", "sort"),
    "/api/player": ("id",),
    "/api/records": ("scope",),
    "/api/maps": (),
}


class Handler(BaseHTTPRequestHandler):
    server_version = "l4d2-stats"
    sys_version = ""
    timeout = 20

    @property
    def app(self):
        return self.server.app

    def log_message(self, fmt, *args):
        pass

    def _send(self, status, body, content_type, cache=None, extra=()):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", f"public, max-age={cache}" if cache else "no-store")
        for name, value in SECURITY_HEADERS.items():
            self.send_header(name, value)
        for name, value in extra:
            self.send_header(name, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, status, payload, cache=None, extra=()):
        self._send(status, json.dumps(payload, separators=(",", ":")).encode("utf-8"),
                   "application/json", cache, extra)

    def _visitor(self):
        """Who to rate-limit: the visitor behind the tunnel, which only a local peer may name.

        IPv6 visitors are counted by their /64: one home or server gets a whole
        /64, so going by single addresses would let anyone hop past the limit.
        """
        peer = self.client_address[0]
        forwarded = (self.headers.get("CF-Connecting-IP") or "").strip()
        who = peer
        try:
            if forwarded and ipaddress.ip_address(peer).is_loopback:
                who = forwarded
            address = ipaddress.ip_address(who)
        except ValueError:
            return peer
        if address.version == 6:
            if address.ipv4_mapped:
                return str(address.ipv4_mapped)
            return str(ipaddress.ip_network(f"{address}/64", strict=False))
        return str(address)

    def do_GET(self):
        url = urllib.parse.urlsplit(self.path)
        if url.path == "/healthz":
            return self._send(200, b"ok\n", "text/plain")
        if not self.app.limiter.allow(self._visitor()):
            return self._json(429, {"error": "Too many requests. Slow down a little."}, extra=[("Retry-After", "10")])
        if url.path in self.app.static:
            body, content_type = self.app.static[url.path]
            return self._send(200, body, content_type, cache=60)
        route = ROUTES.get(url.path)
        if route is None:
            return self._json(404, {"error": "Not found."})
        try:
            query = urllib.parse.parse_qs(url.query, max_num_fields=8)
        except ValueError:
            return self._json(400, {"error": "Too many parameters."})
        query = {k: v[:1] for k, v in query.items() if k in ROUTE_PARAMS[url.path]}
        key = (url.path, tuple(sorted((k, v[0]) for k, v in query.items())))
        try:
            cache = self.app.player_cache if route is App.player else self.app.cache
            body = cache.get(key, lambda: self._render(route, query))
        except BadRequest as e:
            return self._json(400, {"error": str(e)})
        except Exception as e:      # one bad request must not take the site down
            self.app.log(f"{url.path} failed: {e!r}")
            return self._json(500, {"error": "Something went wrong. Try again in a minute."})
        if body is None:
            return self._json(404, {"error": "No such player."})
        self._send(200, body, "application/json", cache=CACHE_SECONDS)

    def _render(self, route, query):
        result = route(self.app, query)
        return None if result is None else json.dumps(result, separators=(",", ":")).encode("utf-8")

    do_HEAD = do_GET

    def _refuse(self):
        self._json(405, {"error": "This site is read-only."}, extra=[("Allow", "GET, HEAD")])

    do_POST = do_PUT = do_DELETE = do_PATCH = do_OPTIONS = _refuse


def make_server(cfg, log=log, steam=None):
    httpd = ThreadingHTTPServer((cfg.bind, cfg.port), Handler)
    httpd.daemon_threads = True
    httpd.app = App(cfg, log=log, steam=steam)
    return httpd


def main():
    try:
        cfg = config_mod.StatsConfig.load()
    except config_mod.ConfigError as e:
        log(str(e))
        return 1
    try:
        httpd = make_server(cfg)
    except OSError as e:
        log(f"can't listen on {cfg.bind}:{cfg.port}: {e}")
        return 1
    app = httpd.app
    stop = threading.Event()
    if app.steam.enabled:
        threading.Thread(target=app.steam.run_forever, args=(app.db.known_players, stop), daemon=True).start()
    else:
        log("no STEAM_API_KEY: the site runs without Steam avatars and hours")
    if not os.path.exists(cfg.db_path):
        log(f"no stats yet: {cfg.db_path} appears once the l4d2_stats plugin has loaded")
    log(f"stats site listening on http://{cfg.bind}:{cfg.port}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
    return 0


if __name__ == "__main__":
    sys.exit(main())
