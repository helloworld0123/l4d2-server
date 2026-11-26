"""Read-only queries over the database the l4d2_stats plugin writes.

Points come from the plugin's "scored" view, one row per player per round
(a chapter attempt). "Ranked" leaves out boosted rounds: ones played with
cheats, infinite ammo, a tank run, double loot or admin help.
"""

import contextlib
import os
import sqlite3
import time
import urllib.parse

PERIODS = {"all": None, "month": 30 * 86400, "week": 7 * 86400}
SCOPES = ("ranked", "all")
SORTS = ("points", "per_hour", "kills", "specials", "tank_damage", "teamwork", "survived")
MIN_PER_HOUR = 3600          # seconds played before a points-per-hour figure counts
BOARD_SIZE = 100
LIVE_FOR = 180               # the plugin saves every minute; older than this means it's not running
CLEARED = ("cleared", "won")
SPECIALS = ("smokers", "boomers", "hunters", "spitters", "jockeys", "chargers")
SUMS = ("points", "playtime", "commons", "headshots", *SPECIALS, "specials", "witches", "tanks",
        "tank_damage", "heals", "revives", "defibs", "rescues", "gives", "teamwork", "ff_damage",
        "incaps", "deaths", "survived")
DIFFICULTIES = ("Easy", "Normal", "Advanced", "Expert")


class NoData(Exception):
    """The plugin hasn't created its database yet."""


def _sum_columns(prefix=""):
    return ", ".join(f"COALESCE(SUM({prefix}{c}), 0) AS {c}" for c in SUMS)


class StatsDB:
    def __init__(self, path, maps, hidden=frozenset(), clock=time.time):
        self.path = path
        self.maps = maps
        self.hidden = frozenset(hidden)
        self.clock = clock

    @contextlib.contextmanager
    def _db(self):
        if not os.path.isfile(self.path):
            raise NoData()
        uri = "file:" + urllib.parse.quote(os.path.abspath(self.path)) + "?mode=ro"
        con = sqlite3.connect(uri, uri=True, timeout=5, isolation_level=None)
        try:
            con.row_factory = sqlite3.Row
            # Names come from game clients: a broken one must not take the site down.
            con.text_factory = lambda raw: raw.decode("utf-8", "replace")
            con.execute("PRAGMA query_only = 1")
            # The view's SQL is written by the game side: let it call only harmless functions.
            con.execute("PRAGMA trusted_schema = OFF")
            # One snapshot per page, so a save landing halfway can't make its numbers disagree.
            con.execute("BEGIN")
            yield con
        finally:
            try:
                con.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            con.close()

    def _visible(self, steamid):
        return steamid not in self.hidden

    def _not_hidden(self, column):
        """SQL leaving hidden players out, with its parameters: done in the query so
        a hidden player can't push everyone else past a LIMIT."""
        if not self.hidden:
            return "1", ()
        return f"{column} NOT IN ({', '.join('?' * len(self.hidden))})", tuple(sorted(self.hidden))

    def _seen_round(self, rounds_id):
        """SQL for "a visible player was in this round"."""
        clause, params = self._not_hidden("v.steamid")
        return f"EXISTS (SELECT 1 FROM round_players v WHERE v.round_id = {rounds_id} AND {clause})", params

    def _board_order(self, rows):
        """Visible players who have played, best first; ties broken by name, as on the board."""
        players = [dict(r) for r in rows if self._visible(r["steamid"]) and r["playtime"] > 0]
        players.sort(key=lambda p: (-p["points"], p["name"].lower()))
        return players

    def _since(self, period):
        span = PERIODS[period]
        return 0 if span is None else int(self.clock()) - span

    def _map(self, name):
        return {"map": name, **self.maps.describe(name)}

    def _result(self, row):
        result = row["result"]
        if result == "playing" and self.clock() - row["ended"] > LIVE_FOR:
            return "unfinished"       # the server stopped mid-chapter
        return result

    # ------------------------------------------------------------------ pages
    def summary(self):
        try:
            with self._db() as con:
                return self._summary(con)
        except NoData:
            return {"ready": False}

    def _summary(self, con):
        # Hidden players' play is left out here too: totals ticking up while only
        # a hidden player is on would give them away.
        visible, params = self._not_hidden("steamid")
        totals = dict(con.execute(
            "SELECT COALESCE(SUM(playtime), 0) AS playtime, COALESCE(SUM(commons), 0) AS commons, "
            "COALESCE(SUM(specials), 0) AS specials, COALESCE(SUM(tanks), 0) AS tanks, "
            f"COALESCE(SUM(witches), 0) AS witches FROM scored WHERE {visible}", params).fetchone())
        totals["players"] = sum(1 for (sid,) in con.execute("SELECT steamid FROM players") if self._visible(sid))
        seen, params = self._seen_round("rounds.id")
        totals["chapters"] = con.execute(
            f"SELECT COUNT(*) FROM rounds WHERE result IN ('cleared', 'won') AND {seen}", params).fetchone()[0]
        totals["campaigns"] = con.execute(
            f"SELECT COUNT(*) FROM rounds WHERE result = 'won' AND {seen}", params).fetchone()[0]
        return {"ready": True, "totals": totals, "live": self._live(con), "recent": self._recent(con, 8)}

    def _live(self, con):
        state = {k: v for k, v in con.execute("SELECT k, v FROM server_state")}
        try:
            updated = int(state.get("updated", "0"))
        except ValueError:
            updated = 0
        if self.clock() - updated > LIVE_FOR:
            return {"online": False, "updated": None}
        # Hidden players aren't shown or counted: not even that someone is on.
        visible = [{"steamid": r["steamid"], "name": r["name"], "since": r["since"]}
                   for r in con.execute("SELECT steamid, name, since FROM online ORDER BY since")
                   if self._visible(r["steamid"])]
        if not visible:
            # Nobody shown is on: the map and the time would say that a hidden player is.
            return {"online": False, "updated": None}
        try:
            difficulty = DIFFICULTIES[int(state.get("difficulty", "1"))]
        except (ValueError, IndexError):
            difficulty = None
        try:
            slots = int(state.get("max_players", "0")) or None
        except ValueError:
            slots = None
        return {"online": True, "updated": updated, **self._map(state.get("map", "")),
                "mode": state.get("mode", ""), "difficulty": difficulty, "count": len(visible),
                "slots": slots, "players": visible}

    def _recent(self, con, limit):
        rounds = con.execute(
            "SELECT id, map, difficulty, started, left_safe, ended, result, boosted, humans FROM rounds "
            "ORDER BY ended DESC LIMIT ?", (limit * 4,)).fetchall()
        out = []
        for r in rounds:
            names = [row["name"] for row in con.execute(
                "SELECT p.steamid, p.name FROM round_players rp JOIN players p ON p.steamid = rp.steamid "
                "WHERE rp.round_id = ? ORDER BY rp.playtime DESC", (r["id"],)) if self._visible(row["steamid"])]
            if not names:
                continue        # only hidden players were in it
            if len(out) == limit:
                break
            out.append({**self._map(r["map"]), "difficulty": DIFFICULTIES[min(max(r["difficulty"], 0), 3)],
                        "ended": r["ended"], "result": self._result(r), "boosted": bool(r["boosted"]),
                        "duration": (r["ended"] - r["left_safe"]) if r["left_safe"] else None,
                        "players": names})
        return out

    def leaderboard(self, period="all", scope="ranked", sort="points"):
        if period not in PERIODS or scope not in SCOPES or sort not in SORTS:
            raise ValueError("unknown option")
        try:
            with self._db() as con:
                rows = con.execute(
                    f"SELECT s.steamid, p.name, {_sum_columns('s.')}, COUNT(*) AS rounds, MAX(s.ended) AS last_played "
                    "FROM scored s JOIN players p ON p.steamid = s.steamid "
                    "WHERE s.ended >= ? AND (? OR s.boosted = 0) GROUP BY s.steamid HAVING SUM(s.playtime) > 0",
                    (self._since(period), scope == "all")).fetchall()
        except NoData:
            return {"ready": False, "players": []}
        players = []
        for row in rows:
            if not self._visible(row["steamid"]):
                continue
            item = dict(row)
            item["kills"] = item["commons"] + item["specials"]
            item["per_hour"] = (round(item["points"] * 3600 / item["playtime"])
                                if item["playtime"] >= MIN_PER_HOUR else None)
            players.append(item)
        if sort == "per_hour":
            players = [p for p in players if p["per_hour"] is not None]
        players.sort(key=lambda p: (-(p[sort] or 0), -p["points"], p["name"].lower()))
        for place, item in enumerate(players, start=1):
            item["rank"] = place
        return {"ready": True, "period": period, "scope": scope, "sort": sort,
                "total": len(players), "players": players[:BOARD_SIZE], "min_per_hour": MIN_PER_HOUR}

    def player(self, steamid):
        """One player's page, or None when there is no such (visible) player."""
        if not self._visible(steamid):
            return None
        try:
            with self._db() as con:
                return self._player(con, steamid)
        except NoData:
            return None

    def _player(self, con, steamid):
        info = con.execute("SELECT steamid, name, first_seen, last_seen, visits FROM players WHERE steamid = ?",
                           (steamid,)).fetchone()
        if info is None:
            return None
        cols = _sum_columns()
        ranked = dict(con.execute(f"SELECT {cols}, COUNT(*) AS rounds FROM scored WHERE steamid = ? AND boosted = 0",
                                  (steamid,)).fetchone())
        everything = dict(con.execute(f"SELECT {cols}, COUNT(*) AS rounds FROM scored WHERE steamid = ?",
                                      (steamid,)).fetchone())
        everything["campaigns"] = con.execute(
            "SELECT COUNT(*) FROM scored WHERE steamid = ? AND result = 'won' AND survived = 1", (steamid,)).fetchone()[0]
        rank = None
        board = self._board_order(con.execute(
            "SELECT s.steamid, p.name, SUM(s.points) AS points, SUM(s.playtime) AS playtime FROM scored s "
            "JOIN players p ON p.steamid = s.steamid WHERE s.boosted = 0 GROUP BY s.steamid"))
        place = next((i for i, row in enumerate(board, start=1) if row["steamid"] == steamid), None)
        if place:
            rank = {"place": place, "of": len(board)}

        def chapter(row):
            return {**self._map(row["map"]), "ended": row["ended"], "result": self._result(row),
                    "difficulty": DIFFICULTIES[min(max(row["difficulty"], 0), 3)], "boosted": bool(row["boosted"]),
                    "points": row["points"], "kills": row["commons"] + row["specials"], "specials": row["specials"],
                    "tank_damage": row["tank_damage"], "teamwork": row["teamwork"], "deaths": row["deaths"],
                    "survived": bool(row["survived"])}

        best = [chapter(r) for r in con.execute(
            "SELECT * FROM scored WHERE steamid = ? AND boosted = 0 ORDER BY points DESC, ended LIMIT 5", (steamid,))]
        recent = [chapter(r) for r in con.execute(
            "SELECT * FROM scored WHERE steamid = ? ORDER BY ended DESC LIMIT 15", (steamid,))]
        maps = [{**self._map(r["map"]), "rounds": r["n"], "cleared": r["cleared"]} for r in con.execute(
            "SELECT map, COUNT(*) AS n, SUM(result IN ('cleared', 'won') AND survived = 1) AS cleared "
            "FROM scored WHERE steamid = ? GROUP BY map ORDER BY n DESC, map LIMIT 6", (steamid,))]
        return {"steamid": info["steamid"], "name": info["name"], "first_seen": info["first_seen"],
                "last_seen": info["last_seen"], "visits": info["visits"], "rank": rank,
                "ranked": ranked, "all": everything, "best": best, "recent": recent, "maps": maps,
                "min_per_hour": MIN_PER_HOUR}

    def records(self, scope="ranked"):
        if scope not in SCOPES:
            raise ValueError("unknown option")
        try:
            with self._db() as con:
                return self._records(con, scope == "all")
        except NoData:
            return {"ready": False}

    def _first_visible(self, rows):
        return next((r for r in rows if self._visible(r["steamid"])), None)

    def _records(self, con, everything):
        hidden, params = self._not_hidden("steamid")
        where = ("1" if everything else "boosted = 0") + f" AND {hidden}"
        names = {r["steamid"]: r["name"] for r in con.execute("SELECT steamid, name FROM players")}
        chapter = []
        for key, label in (("points", "Points in one chapter"), ("commons", "Common infected in one chapter"),
                           ("specials", "Special infected in one chapter"), ("tank_damage", "Tank damage in one chapter"),
                           ("teamwork", "Heals, revives and rescues in one chapter"),
                           ("headshots", "Headshots in one chapter")):
            row = self._first_visible(con.execute(
                f"SELECT steamid, {key} AS value, map, ended, difficulty FROM scored WHERE {where} AND {key} > 0 "
                f"ORDER BY {key} DESC, ended LIMIT 1", params))
            if row:
                chapter.append({"key": key, "label": label, "value": row["value"], "steamid": row["steamid"],
                                "name": names.get(row["steamid"], "?"), **self._map(row["map"]), "ended": row["ended"],
                                "difficulty": DIFFICULTIES[min(max(row["difficulty"], 0), 3)]})
        career = []
        for key, label in (("playtime", "Time played"), ("commons", "Common infected killed"),
                           ("specials", "Special infected killed"), ("tanks", "Tanks finished off"),
                           ("witches", "Witches killed"), ("teamwork", "Heals, revives and rescues"),
                           ("survived", "Chapters survived"), ("deaths", "Most deaths"),
                           ("ff_damage", "Most friendly fire")):
            row = self._first_visible(con.execute(
                f"SELECT steamid, SUM({key}) AS value FROM scored WHERE {where} GROUP BY steamid "
                f"HAVING value > 0 ORDER BY value DESC, MIN(ended), steamid LIMIT 1", params))
            if row:
                career.append({"key": key, "label": label, "value": row["value"], "steamid": row["steamid"],
                               "name": names.get(row["steamid"], "?")})
        return {"ready": True, "scope": "all" if everything else "ranked", "chapter": chapter,
                "career": career, "fastest": self._fastest(con, everything)}

    def _fastest(self, con, everything):
        """Each map's quickest clear, from leaving the start area to the saferoom or rescue."""
        seen, params = self._seen_round("rounds.id")
        rows = con.execute(
            "SELECT id, map, ended - left_safe AS seconds, ended, difficulty FROM rounds "
            f"WHERE result IN ('cleared', 'won') AND left_safe IS NOT NULL AND ended > left_safe AND {seen}"
            + ("" if everything else " AND boosted = 0") + " ORDER BY seconds, ended", params).fetchall()
        best = {}
        for r in rows:
            if r["map"] in best:
                continue
            team = [{"steamid": p["steamid"], "name": p["name"]} for p in con.execute(
                "SELECT p.steamid, p.name FROM round_players rp JOIN players p ON p.steamid = rp.steamid "
                "WHERE rp.round_id = ? ORDER BY rp.playtime DESC", (r["id"],)) if self._visible(p["steamid"])]
            if team:            # a clear by hidden players alone isn't shown
                best[r["map"]] = (r, team)
        return [{**self._map(name), "seconds": r["seconds"], "ended": r["ended"],
                 "difficulty": DIFFICULTIES[min(max(r["difficulty"], 0), 3)], "team": team}
                for name, (r, team) in sorted(best.items())]

    def map_stats(self):
        # Only rounds a visible player was in: the times and dates would otherwise
        # show when a hidden player was on, and how fast they were.
        seen, params = self._seen_round("rounds.id")
        try:
            with self._db() as con:
                rows = con.execute(
                    "SELECT map, COUNT(*) AS rounds, SUM(result IN ('cleared', 'won')) AS cleared, "
                    "SUM(result = 'lost') AS lost, MAX(ended) AS last_played, "
                    "MIN(CASE WHEN result IN ('cleared', 'won') AND left_safe IS NOT NULL AND boosted = 0 "
                    "AND ended > left_safe THEN ended - left_safe END) AS fastest "
                    f"FROM rounds WHERE {seen} GROUP BY map ORDER BY rounds DESC, map", params).fetchall()
        except NoData:
            return {"ready": False, "maps": []}
        return {"ready": True, "maps": [{**self._map(r["map"]), "rounds": r["rounds"], "cleared": r["cleared"],
                                         "lost": r["lost"], "last_played": r["last_played"],
                                         "fastest": r["fastest"]} for r in rows]}

    def known_players(self, since_days=90, limit=500):
        """SteamIDs worth fetching Steam details for: the most recently seen first."""
        try:
            with self._db() as con:
                rows = con.execute("SELECT steamid FROM players WHERE last_seen >= ? ORDER BY last_seen DESC LIMIT ?",
                                   (int(self.clock()) - since_days * 86400, limit)).fetchall()
        except (NoData, sqlite3.Error):
            return []
        return [r[0] for r in rows if self._visible(r[0])]
