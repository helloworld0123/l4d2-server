"""Steam profile details for the stats site: avatar, Steam name, L4D2 hours, achievements.

Fetched in the background with the site owner's Steam Web API key and kept in
a JSON file, so visitors never wait on Steam and never see the key. Players
whose Steam game details are private get an avatar and nothing else.
"""

import json
import os
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

API = "https://api.steampowered.com"
APP_ID = 550                       # Left 4 Dead 2
AVATAR_HOSTS = frozenset({"avatars.steamstatic.com", "avatars.akamai.steamstatic.com",
                          "avatars.cloudflare.steamstatic.com", "avatars.fastly.steamstatic.com",
                          "steamcdn-a.akamaihd.net"})
PROFILE_TTL = 6 * 3600             # avatar and name
DETAIL_TTL = 24 * 3600             # hours and achievements
DETAILS_PER_ROUND = 40             # at most this many players' details per refresh
REFRESH_EVERY = 15 * 60
MAX_REPLY = 2 * 1024 * 1024
PUBLIC = 3                         # communityvisibilitystate of a public profile


class SteamError(Exception):
    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


def http_get_json(url, timeout=10):
    """GET a Steam API URL. Errors never carry the URL: it holds the key."""
    request = urllib.request.Request(url, headers={"User-Agent": "l4d2-stats"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as reply:
            body = reply.read(MAX_REPLY + 1)
    except urllib.error.HTTPError as e:
        raise SteamError(f"Steam answered {e.code}", e.code) from None
    except (urllib.error.URLError, OSError) as e:
        raise SteamError(f"Steam is unreachable ({type(e).__name__})") from None
    if len(body) > MAX_REPLY:
        raise SteamError("Steam's reply was too large")
    try:
        return json.loads(body)
    except ValueError:
        raise SteamError("Steam's reply wasn't JSON") from None


def safe_avatar(url):
    """Only https images from Steam's own hosts reach visitors' browsers."""
    if not isinstance(url, str) or len(url) > 300:
        return None
    parts = urllib.parse.urlsplit(url)
    if parts.scheme != "https" or parts.hostname not in AVATAR_HOSTS or parts.username or parts.port:
        return None
    return url


class SteamCache:
    def __init__(self, key, path, fetch=http_get_json, clock=time.time, log=print):
        self.key = key
        self.path = path
        self.fetch = fetch
        self.clock = clock
        self.log = log
        self.lock = threading.Lock()
        self.data = {}
        self.blocked_until = 0
        self._load()

    @property
    def enabled(self):
        return bool(self.key)

    def _load(self):
        if not self.path:
            return
        try:
            with open(self.path, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return
        if isinstance(data, dict):
            self.data = {k: v for k, v in data.items() if isinstance(v, dict)}

    def _save(self):
        if not self.path:
            return
        with self.lock:
            text = json.dumps(self.data, separators=(",", ":"))
        directory = os.path.dirname(self.path) or "."
        try:
            fd, tmp = tempfile.mkstemp(prefix=".steam-", dir=directory)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(text)
            os.replace(tmp, self.path)
        except OSError as e:
            self.log(f"steam: could not save the cache: {e}")

    def get(self, steamid):
        with self.lock:
            entry = self.data.get(steamid)
            return dict(entry) if entry else None

    def _url(self, interface, method, version, **params):
        query = urllib.parse.urlencode({"key": self.key, **params})
        return f"{API}/{interface}/{method}/v{version}/?{query}"

    # --------------------------------------------------------------- fetching
    def refresh(self, steamids):
        """Brings stale entries up to date. Returns how many players were looked up."""
        if not self.enabled or self.clock() < self.blocked_until:
            return 0
        now = self.clock()
        with self.lock:
            stale = [s for s in steamids if now - self.data.get(s, {}).get("fetched", 0) > PROFILE_TTL]
        looked_up = 0
        for start in range(0, len(stale), 100):
            batch = stale[start:start + 100]
            try:
                reply = self.fetch(self._url("ISteamUser", "GetPlayerSummaries", 2, steamids=",".join(batch)))
            except SteamError as e:
                self._failed(e)
                return looked_up
            found = {}
            players = ((reply or {}).get("response") or {}).get("players")
            for p in players if isinstance(players, list) else []:
                if isinstance(p, dict) and str(p.get("steamid")) in batch:
                    found[str(p["steamid"])] = p
            with self.lock:
                for steamid in batch:
                    entry = self.data.setdefault(steamid, {})
                    p = found.get(steamid, {})
                    entry.update({
                        "fetched": now,
                        "persona": str(p.get("personaname") or "")[:64] or None,
                        "avatar": safe_avatar(p.get("avatarmedium")),
                        "avatar_full": safe_avatar(p.get("avatarfull")),
                        "public": p.get("communityvisibilitystate") == PUBLIC,
                    })
            looked_up += len(batch)
        self._details(steamids, now)
        self._save()
        return looked_up

    def _details(self, steamids, now):
        with self.lock:
            due = [s for s in steamids if self.data.get(s, {}).get("public")
                   and now - self.data[s].get("details_fetched", 0) > DETAIL_TTL][:DETAILS_PER_ROUND]
        for steamid in due:
            try:
                hours = self._hours(steamid)
                achievements = self._achievements(steamid)
            except SteamError as e:
                # Steam itself is down, busy or refusing the key: keep what we
                # had and try again on a later round instead of wiping it.
                self._failed(e)
                return
            with self.lock:
                entry = self.data[steamid]
                entry["details_fetched"] = now
                # A refusal (say, a key that stopped working) never wipes what was
                # known; whether it is shown follows the profile's public flag,
                # which every profile refresh brings up to date.
                if hours is not None or "hours" not in entry:
                    entry["hours"] = hours
                if achievements is not None or "achievements" not in entry:
                    entry["achievements"] = achievements

    def _private(self, call):
        """A call's answer, or None when Steam has nothing to give for this player.

        403 means their game details are private; 400 and 404 come back for
        profiles without L4D2 stats (a game borrowed through Family Sharing,
        say). Either way it's about this player only, so the round goes on;
        anything else means Steam itself is in trouble.
        """
        try:
            return call()
        except SteamError as e:
            if e.status in (400, 403, 404):
                return None
            raise

    def _hours(self, steamid):
        owned = self._private(lambda: self.fetch(self._url(
            "IPlayerService", "GetOwnedGames", 1, steamid=steamid,
            include_played_free_games=1, **{"appids_filter[0]": APP_ID})))
        games = ((owned or {}).get("response") or {}).get("games")
        for game in games if isinstance(games, list) else []:
            if isinstance(game, dict) and game.get("appid") == APP_ID:
                minutes = game.get("playtime_forever")
                if isinstance(minutes, int) and minutes >= 0:
                    return round(minutes / 60, 1)
        return None

    def _achievements(self, steamid):
        stats = self._private(lambda: self.fetch(self._url(
            "ISteamUserStats", "GetPlayerAchievements", 1, steamid=steamid, appid=APP_ID)))
        listed = ((stats or {}).get("playerstats") or {}).get("achievements")
        if isinstance(listed, list) and listed:
            done = sum(1 for a in listed if isinstance(a, dict) and a.get("achieved") == 1)
            return {"done": done, "total": len(listed)}
        return None

    def _failed(self, error):
        # A bad key or too many calls: stop asking for a while instead of hammering Steam.
        wait = 3600 if error.status in (401, 403, 429) else 300
        self.blocked_until = self.clock() + wait
        self.log(f"steam: {error}; trying again in {wait // 60} min")

    def run_forever(self, list_players, stop):
        while not stop.is_set():
            try:
                self.refresh(list_players())
            except Exception as e:          # the site must keep running whatever Steam does
                self.log(f"steam: refresh failed: {e!r}")
            stop.wait(REFRESH_EVERY)
