"""Tests for the public stats site and the database the l4d2_stats plugin writes."""

import http.client
import io
import json
import os
import re
import sqlite3
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse

from stats import config as config_mod, db as db_mod, maps as maps_mod, server as server_mod, steam as steam_mod

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PLUGIN = os.path.join(REPO, "cli", "plugins", "l4d2_stats.sp")
NOW = 1_800_000_000
DAY = 86400
A, B, C, D = "76561197960000001", "76561197960000002", "76561197960000003", "76561197960000004"


def plugin_schema():
    """The CREATE statements exactly as the plugin runs them."""
    with open(PLUGIN, encoding="utf-8") as fh:
        source = fh.read()
    block = re.search(r"g_sSchema\[\]\[\] =\n\{\n(.*?)\n\};", source, re.S).group(1)
    return [re.match(r'\s*"(.*)",?$', line).group(1) for line in block.splitlines()]


class Fixture:
    """A stats database like the plugin leaves it."""

    def __init__(self, path):
        self.con = sqlite3.connect(path)
        for statement in plugin_schema():
            self.con.execute(statement)
        self.next_id = 1

    def player(self, steamid, name, first=NOW - 30 * DAY, last=NOW - DAY, visits=3):
        self.con.execute("INSERT INTO players VALUES (?, ?, ?, ?, ?)", (steamid, name, first, last, visits))

    def round(self, players, map="c1m1_hotel", difficulty=1, started=NOW - DAY, result="cleared",
              boosted=0, left_safe=None, ended=None, mode="coop"):
        rid = self.next_id
        self.next_id += 1
        ended = ended if ended is not None else started + 900
        self.con.execute(
            "INSERT INTO rounds (id, map, mode, difficulty, started, left_safe, ended, result, boosted, team_size, humans) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 4, ?)",
            (rid, map, mode, difficulty, started, left_safe, ended, result, boosted, len(players)))
        for steamid, stats in players.items():
            stats = {"playtime": 900, **stats}
            cols = ", ".join(stats)
            self.con.execute(f"INSERT INTO round_players (round_id, steamid, {cols}) VALUES (?, ?{', ?' * len(stats)})",
                             (rid, steamid, *stats.values()))
        return rid

    def live(self, updated, map="c2m3_coaster", online=()):
        for k, v in {"map": map, "mode": "coop", "difficulty": "2", "max_players": "8", "updated": str(updated)}.items():
            self.con.execute("INSERT OR REPLACE INTO server_state VALUES (?, ?)", (k, v))
        for steamid, name in online:
            self.con.execute("INSERT INTO online VALUES (?, ?, ?)", (steamid, name, updated - 600))

    def close(self):
        self.con.commit()
        self.con.close()


class StatsTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "l4d2_stats.sq3")
        self.maps_path = os.path.join(self.tmp.name, "maps.json")
        self.fx = Fixture(self.path)

    def stats(self, hidden=()):
        self.fx.close()
        names = maps_mod.MapNames(self.maps_path, clock=lambda: NOW)
        return db_mod.StatsDB(self.path, names, hidden=frozenset(hidden), clock=lambda: NOW)


class SchemaTests(StatsTestCase):
    def test_points_follow_the_formula_and_difficulty(self):
        self.fx.player(A, "Ann")
        # Expert finale: (100 commons + 2 hunters*5 + tank 10 + 4000 dmg/50 + 1 heal*10 + 25 + 75
        #                 - 10 ff/2 - 1 incap*5 - 0 deaths) * 2
        self.fx.round({A: {"commons": 100, "hunters": 2, "tanks": 1, "tank_damage": 4000, "heals": 1,
                           "ff_damage": 10, "incaps": 1, "survived": 1}}, difficulty=3, result="won")
        # Easy chapter, died: (10 - 20) * 0.5
        self.fx.round({A: {"commons": 10, "deaths": 1}}, difficulty=0, result="lost")
        self.fx.close()
        con = sqlite3.connect(self.path)
        points = [r[0] for r in con.execute("SELECT points FROM scored ORDER BY round_id")]
        con.close()
        self.assertEqual(points, [(100 + 10 + 10 + 80 + 10 + 25 + 75 - 5 - 5) * 2, -5])

    def test_schema_can_be_run_again(self):
        for statement in plugin_schema():       # the plugin runs it on every load
            self.fx.con.execute(statement)
        self.assertIn("scored", [r[0] for r in self.fx.con.execute("SELECT name FROM sqlite_master WHERE type = 'view'")])


class LeaderboardTests(StatsTestCase):
    def setUp(self):
        super().setUp()
        for steamid, name in ((A, "Ann"), (B, "Bob"), (C, "Cy"), (D, "Dee")):
            self.fx.player(steamid, name)
        self.fx.round({A: {"commons": 50}, B: {"commons": 30}})
        self.fx.round({B: {"commons": 500}}, boosted=1)                       # cheats on
        self.fx.round({C: {"commons": 40, "playtime": 4000}}, started=NOW - 20 * DAY)
        self.fx.round({D: {"commons": 5}}, started=NOW - 2 * DAY)

    def names(self, board):
        return [p["name"] for p in board["players"]]

    def test_ranked_leaves_boosted_rounds_out(self):
        stats = self.stats()
        self.assertEqual(self.names(stats.leaderboard()), ["Ann", "Cy", "Bob", "Dee"])
        self.assertEqual(self.names(stats.leaderboard(scope="all")), ["Bob", "Ann", "Cy", "Dee"])

    def test_period_and_ranks(self):
        board = self.stats().leaderboard(period="week")
        self.assertEqual(self.names(board), ["Ann", "Bob", "Dee"])
        self.assertEqual([p["rank"] for p in board["players"]], [1, 2, 3])

    def test_per_hour_needs_an_hour_played(self):
        board = self.stats().leaderboard(sort="per_hour")
        self.assertEqual(self.names(board), ["Cy"])
        self.assertEqual(board["players"][0]["per_hour"], round(40 * 3600 / 4000))

    def test_hidden_players_are_left_out_and_ranks_close_up(self):
        board = self.stats(hidden={A}).leaderboard()
        self.assertEqual(self.names(board), ["Cy", "Bob", "Dee"])
        self.assertEqual(board["players"][0]["rank"], 1)
        self.assertEqual(board["total"], 3)

    def test_unknown_options_are_refused(self):
        stats = self.stats()
        for bad in ({"period": "year"}, {"scope": "x"}, {"sort": "name; DROP TABLE players"}):
            with self.assertRaises(ValueError):
                stats.leaderboard(**bad)

    def test_no_database_yet(self):
        stats = self.stats()
        os.remove(self.path)
        self.assertEqual(stats.leaderboard(), {"ready": False, "players": []})
        self.assertEqual(stats.summary(), {"ready": False})
        self.assertIsNone(stats.player(A))
        self.assertEqual(stats.known_players(), [])


class PlayerTests(StatsTestCase):
    def test_profile(self):
        self.fx.player(A, "Ann")
        self.fx.player(B, "Bob")
        self.fx.round({A: {"commons": 50, "survived": 1}, B: {"commons": 60}}, map="c2m1_highway")
        self.fx.round({A: {"commons": 999}}, boosted=1, started=NOW - 3600)
        p = self.stats().player(A)
        self.assertEqual(p["name"], "Ann")
        self.assertEqual(p["rank"], {"place": 1, "of": 2})     # 50 + 25 for the saferoom beats 60
        self.assertEqual(p["ranked"]["commons"], 50)
        self.assertEqual(p["all"]["commons"], 1049)
        self.assertEqual([c["kills"] for c in p["best"]], [50])           # boosted rounds aren't "best"
        self.assertEqual([c["kills"] for c in p["recent"]], [999, 50])
        self.assertEqual(p["best"][0]["campaign"], "Dark Carnival")
        self.assertEqual(p["best"][0]["chapter"], 1)

    def test_unknown_and_hidden(self):
        self.fx.player(A, "Ann")
        stats = self.stats(hidden={A})
        self.assertIsNone(stats.player(A))
        self.assertIsNone(stats.player(B))

    def test_never_ranked(self):
        self.fx.player(A, "Ann")
        self.fx.round({A: {"commons": 5}}, boosted=1)
        self.assertIsNone(self.stats().player(A)["rank"])


class RecordTests(StatsTestCase):
    def test_records_skip_hidden_holders(self):
        for steamid, name in ((A, "Ann"), (B, "Bob"), (C, "Cy")):
            self.fx.player(steamid, name)
        self.fx.round({A: {"commons": 300}}, left_safe=NOW - DAY + 100, ended=NOW - DAY + 400, map="c1m2_streets")
        self.fx.round({B: {"commons": 200}, C: {"commons": 10}}, left_safe=NOW - DAY + 100,
                      ended=NOW - DAY + 700, map="c1m2_streets")
        self.fx.round({C: {"commons": 900}}, boosted=1, left_safe=NOW - DAY, ended=NOW - DAY + 50, map="c1m2_streets")
        records = self.stats(hidden={A}).records()
        commons = next(r for r in records["chapter"] if r["key"] == "commons")
        self.assertEqual((commons["name"], commons["value"]), ("Bob", 200))
        self.assertEqual(len(records["fastest"]), 1)
        fastest = records["fastest"][0]
        # Ann's 300 s clear is the fastest, but she's hidden and ran it alone: the next one shows.
        self.assertEqual((fastest["seconds"], [t["name"] for t in fastest["team"]]), (600, ["Bob", "Cy"]))

    def test_boosted_records_only_with_scope_all(self):
        self.fx.player(A, "Ann")
        self.fx.round({A: {"commons": 900}}, boosted=1, left_safe=NOW - DAY, ended=NOW - DAY + 50)
        stats = self.stats()
        self.assertEqual(stats.records()["chapter"], [])
        self.assertEqual(stats.records("all")["chapter"][0]["value"], 900)
        self.assertEqual(stats.records("all")["fastest"][0]["seconds"], 50)


class SummaryTests(StatsTestCase):
    def test_live_and_recent(self):
        self.fx.player(A, "Ann")
        self.fx.player(B, "Bob")
        self.fx.round({A: {"commons": 5}, B: {}}, result="playing", ended=NOW - 30)
        self.fx.round({A: {}}, result="playing", started=NOW - 5 * DAY, ended=NOW - 5 * DAY + 60)
        self.fx.live(NOW - 30, online=[(A, "Ann"), (B, "Bob")])
        s = self.stats(hidden={B}).summary()
        self.assertTrue(s["live"]["online"])
        # Bob is hidden: not shown, and not counted either.
        self.assertEqual((s["live"]["count"], [p["name"] for p in s["live"]["players"]]), (1, ["Ann"]))
        self.assertEqual((s["live"]["campaign"], s["live"]["chapter"], s["live"]["difficulty"]),
                         ("Dark Carnival", 3, "Advanced"))
        self.assertEqual([r["result"] for r in s["recent"]], ["playing", "unfinished"])
        self.assertEqual(s["recent"][0]["players"], ["Ann"])
        self.assertEqual(s["totals"]["players"], 1)

    def test_offline_when_the_plugin_stops_reporting(self):
        self.fx.live(NOW - 600, online=[(A, "Ann")])
        self.assertEqual(self.stats().summary()["live"], {"online": False, "updated": None})


class MapNameTests(unittest.TestCase):
    def test_official_and_custom(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "maps.json")
            names = maps_mod.MapNames(path, clock=lambda: 0)
            self.assertEqual(names.describe("c8m5_rooftop"), {"campaign": "No Mercy", "chapter": 5})
            self.assertEqual(names.describe("odd_map"), {"campaign": "odd_map", "chapter": None})
            with open(path, "w") as fh:
                json.dump({"1": {"title": "Diescraper  Redux", "maps": ["l4d2_ds1", "l4d2_ds2"]},
                           "2": {"title": "Solo", "maps": ["solo1"]}, "3": "junk", "4": {"title": 5, "maps": []}}, fh)
            names = maps_mod.MapNames(path, clock=lambda: 0)
            self.assertEqual(names.describe("L4D2_DS2"), {"campaign": "Diescraper Redux", "chapter": 2})
            self.assertEqual(names.describe("solo1"), {"campaign": "Solo", "chapter": None})
            with open(path, "w") as fh:
                fh.write("{not json")
            self.assertEqual(maps_mod.MapNames(path).describe("solo1")["chapter"], None)


class SubscribeLinkTests(unittest.TestCase):
    def index(self, tmp, data):
        path = os.path.join(tmp, "maps.json")
        with open(path, "w") as fh:
            json.dump(data, fh)
        return maps_mod.MapNames(path, clock=lambda: 0)

    def test_links_are_built_from_checked_ids(self):
        with tempfile.TemporaryDirectory() as tmp:
            names = self.index(tmp, {
                "123": {"source": "workshop", "id": "123", "title": "Zed Campaign", "maps": ["z1", "z2"],
                        "url": "javascript:alert(1)"},                        # never used
                "bad": {"source": "workshop", "id": "12; rm", "title": "Odd id", "maps": ["o1"]},
                "file:x.vpk": {"source": "file", "title": "Alpha", "file": "alpha_map.vpk", "maps": ["a1"]},
                "file:y": {"source": "file", "title": "Beta", "file": "../../etc/passwd", "maps": []},
            })
            addons = names.addons()
        self.assertEqual([a["title"] for a in addons], ["Alpha", "Beta", "Odd id", "Zed Campaign"])
        zed = addons[3]
        self.assertEqual(zed["links"], {"steam": "steam://url/CommunityFilePage/123",
                                        "web": "https://steamcommunity.com/sharedfiles/filedetails/?id=123"})
        self.assertEqual(zed["chapters"], 2)
        self.assertIsNone(addons[2]["links"])                  # not a plain number: no link at all
        self.assertEqual((addons[0]["file"], addons[0]["links"]), ("alpha_map.vpk", None))
        self.assertEqual(addons[1]["file"], None)               # "passwd" isn't a .vpk name
        self.assertEqual(names.workshop_for("Z2"), "123")
        self.assertIsNone(names.workshop_for("a1"))
        self.assertIsNone(names.workshop_for("c1m1_hotel"))

    def test_a_hostile_index_does_not_break_the_site(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "maps.json")
            with open(path, "w") as fh:
                fh.write("[" * 200000)                   # deep enough to blow the parser's stack
            names = maps_mod.MapNames(path, clock=lambda: 0)
            self.assertEqual(names.addons(), [])
            self.assertEqual(names.describe("odd")["campaign"], "odd")

    def test_no_index_means_no_custom_maps(self):
        self.assertEqual(maps_mod.MapNames("/nonexistent/maps.json").addons(), [])


class ConfigTests(unittest.TestCase):
    def test_defaults_follow_the_game_install(self):
        cfg = config_mod.StatsConfig.load({"L4D2_DIR": "/srv/game", "L4D2_HOME": "/srv"})
        self.assertEqual(cfg.db_path, "/srv/game/left4dead2/addons/sourcemod/data/sqlite/l4d2_stats.sq3")
        self.assertEqual(cfg.maps_path, "/srv/maps.json")
        self.assertEqual((cfg.bind, cfg.port, cfg.steam_key), ("127.0.0.1", 8090, ""))

    def test_bad_values(self):
        for env in ({"L4D2_STATS_PORT": "x"}, {"L4D2_STATS_PORT": "70000"}, {"L4D2_STATS_HIDE": "123,abc"},
                    {"STEAM_API_KEY": "not-a-key"}):
            with self.assertRaises(config_mod.ConfigError):
                config_mod.StatsConfig.load(env)
        cfg = config_mod.StatsConfig.load({"L4D2_STATS_HIDE": f" {A} ; {B},", "STEAM_API_KEY": "A" * 32})
        self.assertEqual(cfg.hidden, {A, B})


class FakeSteam:
    def __init__(self, players=(), private=(), fail=None, fail_on=""):
        self.fail_on = fail_on
        self.calls = []
        self.players = {p["steamid"]: p for p in players}
        self.private = set(private)
        self.fail = fail

    def __call__(self, url):
        self.calls.append(url)
        if self.fail and self.fail_on in url:
            raise steam_mod.SteamError("Steam answered %d" % self.fail, self.fail)
        query = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
        if "GetPlayerSummaries" in url:
            ids = query["steamids"].split(",")
            return {"response": {"players": [self.players[i] for i in ids if i in self.players]}}
        if query.get("steamid") in self.private:
            raise steam_mod.SteamError("Steam answered 403", 403)
        if "GetOwnedGames" in url:
            return {"response": {"games": [{"appid": 550, "playtime_forever": 6030}]}}
        return {"playerstats": {"achievements": [{"achieved": 1}, {"achieved": 0}, {"achieved": 1}]}}



class SteamTests(unittest.TestCase):
    def summary(self, steamid, avatar="https://avatars.steamstatic.com/x_medium.jpg", public=3):
        return {"steamid": steamid, "personaname": "Persona", "avatarmedium": avatar,
                "avatarfull": avatar, "communityvisibilitystate": public}

    def test_refresh_profiles_and_details(self):
        fake = FakeSteam([self.summary(A), self.summary(B, avatar="https://evil.example/x.jpg"),
                          self.summary(C, public=1)], private={B})
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "steam.json")
            cache = steam_mod.SteamCache("K" * 32, path, fetch=fake, clock=lambda: NOW, log=lambda m: None)
            self.assertEqual(cache.refresh([A, B, C]), 3)
            a, b, c = cache.get(A), cache.get(B), cache.get(C)
            self.assertEqual((a["hours"], a["achievements"]), (100.5, {"done": 2, "total": 3}))
            self.assertIsNone(b["avatar"])                       # not one of Steam's image hosts
            self.assertEqual((b["hours"], b["achievements"]), (None, None))   # private game details
            self.assertNotIn("hours", c)                         # private profile: no detail calls
            self.assertEqual(sum("GetPlayerSummaries" in u for u in fake.calls), 1)
            calls = len(fake.calls)
            self.assertEqual(cache.refresh([A, B, C]), 0)         # fresh: nothing asked again
            self.assertEqual(len(fake.calls), calls)
            again = steam_mod.SteamCache("K" * 32, path, fetch=fake, clock=lambda: NOW, log=lambda m: None)
            self.assertEqual(again.get(A)["persona"], "Persona")   # kept on disk

    def test_batches_of_a_hundred(self):
        ids = [str(76561197960000000 + i) for i in range(150)]
        fake = FakeSteam()
        cache = steam_mod.SteamCache("K" * 32, "", fetch=fake, clock=lambda: NOW, log=lambda m: None)
        cache.refresh(ids)
        self.assertEqual(len(fake.calls), 2)

    def test_bad_key_backs_off(self):
        logs = []
        fake = FakeSteam(fail=401)
        cache = steam_mod.SteamCache("K" * 32, "", fetch=fake, clock=lambda: NOW, log=logs.append)
        cache.refresh([A])
        cache.refresh([A])
        self.assertEqual(len(fake.calls), 1)
        self.assertTrue(logs and "K" * 32 not in logs[0])

    def test_no_key_does_nothing(self):
        fake = FakeSteam()
        cache = steam_mod.SteamCache("", "", fetch=fake)
        self.assertFalse(cache.enabled)
        self.assertEqual(cache.refresh([A]), 0)
        self.assertEqual(fake.calls, [])

    def test_errors_never_carry_the_key(self):
        url = "https://api.steampowered.com/x/?key=SECRETSECRETSECRETSECRETSECRET12"

        def boom(request, timeout):
            raise urllib.error.HTTPError(request.full_url, 403, "Forbidden", {}, io.BytesIO(b""))
        saved = steam_mod.urllib.request.urlopen
        steam_mod.urllib.request.urlopen = boom
        try:
            with self.assertRaises(steam_mod.SteamError) as caught:
                steam_mod.http_get_json(url)
        finally:
            steam_mod.urllib.request.urlopen = saved
        self.assertNotIn("SECRET", str(caught.exception))
        self.assertIsNone(caught.exception.__cause__)
        self.assertEqual(caught.exception.status, 403)

    def test_safe_avatar(self):
        self.assertEqual(steam_mod.safe_avatar("https://avatars.steamstatic.com/a.jpg"), "https://avatars.steamstatic.com/a.jpg")
        for bad in ("http://avatars.steamstatic.com/a.jpg", "https://avatars.steamstatic.com.evil.io/a.jpg",
                    "https://user@avatars.steamstatic.com/a.jpg", "javascript:alert(1)", None, 5):
            self.assertIsNone(steam_mod.safe_avatar(bad))


class ServerTests(StatsTestCase):
    def setUp(self):
        super().setUp()
        self.fx.player(A, "<b>Ann</b>")
        self.fx.round({A: {"commons": 50}})
        self.fx.close()
        cfg = config_mod.StatsConfig(self.path, self.maps_path, port=0, title="Our stats")
        self.httpd = server_mod.make_server(cfg, log=lambda m: None)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.addCleanup(self.httpd.server_close)
        self.addCleanup(self.httpd.shutdown)

    def request(self, path, method="GET", headers=None):
        con = http.client.HTTPConnection("127.0.0.1", self.httpd.server_address[1], timeout=5)
        con.request(method, path, headers=headers or {})
        reply = con.getresponse()
        body = reply.read()
        con.close()
        return reply, body

    def test_pages_and_headers(self):
        reply, body = self.request("/")
        self.assertEqual(reply.status, 200)
        self.assertIn(b"app.js", body)
        csp = reply.getheader("Content-Security-Policy")
        self.assertIn("default-src 'self'", csp)
        self.assertIn("https://avatars.steamstatic.com", csp)
        self.assertNotIn("unsafe", csp)
        self.assertIsNone(reply.getheader("Set-Cookie"))
        reply, body = self.request("/api/leaderboard?period=week&junk=1")
        self.assertEqual(reply.status, 200)
        data = json.loads(body)
        self.assertEqual(data["players"][0]["name"], "<b>Ann</b>")      # data, escaped by the page
        reply, body = self.request("/api/summary")
        self.assertEqual(json.loads(body)["title"], "Our stats")
        for path in ("/api/records", "/api/maps", f"/api/player?id={A}"):
            self.assertEqual(self.request(path)[0].status, 200, path)

    def test_maps_page_lists_what_to_subscribe_to(self):
        with open(self.maps_path, "w") as fh:
            json.dump({"9": {"source": "workshop", "id": "9", "title": "Nine", "maps": ["n1"]}}, fh)
        self.httpd.app.maps._loaded = None             # don't wait out the minute between re-reads
        data = json.loads(self.request("/api/maps")[1])
        self.assertEqual([a["title"] for a in data["addons"]], ["Nine"])
        os.remove(self.path)                           # before any game is recorded, still listed
        self.httpd.app.cache.items.clear()
        data = json.loads(self.request("/api/maps")[1])
        self.assertEqual((data["ready"], len(data["addons"])), (False, 1))
        self.assertEqual(json.loads(self.request("/api/summary")[1])["addons"], 1)

    def test_bad_requests(self):
        self.assertEqual(self.request("/api/leaderboard?sort=evil")[0].status, 400)
        self.assertEqual(self.request("/api/player?id=12ab")[0].status, 400)
        self.assertEqual(self.request(f"/api/player?id={B}")[0].status, 404)
        self.assertEqual(self.request("/nope")[0].status, 404)
        self.assertEqual(self.request("/api/leaderboard?" + "&".join(f"a{i}=1" for i in range(20)))[0].status, 400)
        reply, _ = self.request("/api/summary", method="POST")
        self.assertEqual((reply.status, reply.getheader("Allow")), (405, "GET, HEAD"))

    def test_answers_are_cached_briefly(self):
        self.request("/api/leaderboard")
        con = sqlite3.connect(self.path)
        con.execute("UPDATE players SET name = 'Changed'")
        con.commit()
        con.close()
        _, body = self.request("/api/leaderboard?unknown=param")    # same answer: junk doesn't bypass the cache
        self.assertEqual(json.loads(body)["players"][0]["name"], "<b>Ann</b>")

    def test_rate_limit_per_visitor(self):
        self.httpd.app.limiter = server_mod.Limiter(rate=0.0001, burst=3)
        statuses = [self.request("/app.css", headers={"CF-Connecting-IP": "203.0.113.5"})[0].status for _ in range(4)]
        self.assertEqual(statuses, [200, 200, 200, 429])
        # Another visitor behind the same tunnel has their own allowance.
        self.assertEqual(self.request("/app.css", headers={"CF-Connecting-IP": "203.0.113.6"})[0].status, 200)
        self.assertEqual(self.request("/healthz")[0].status, 200)       # never limited

    def test_parameters_a_page_ignores_do_not_reach_the_cache(self):
        self.request("/api/records")
        before = len(self.httpd.app.cache.items)
        for n in range(5):
            self.assertEqual(self.request(f"/api/records?id={A}&sort=junk{n}&period=x{n}")[0].status, 200)
        self.assertEqual(len(self.httpd.app.cache.items), before)

    def test_forwarded_address_is_only_trusted_from_the_tunnel(self):
        handler = server_mod.Handler.__new__(server_mod.Handler)
        handler.headers = {"CF-Connecting-IP": "203.0.113.5"}
        handler.client_address = ("127.0.0.1", 1)
        self.assertEqual(handler._visitor(), "203.0.113.5")
        handler.client_address = ("192.0.2.9", 1)
        self.assertEqual(handler._visitor(), "192.0.2.9")
        handler.client_address = ("127.0.0.1", 1)
        handler.headers = {"CF-Connecting-IP": "not an address"}
        self.assertEqual(handler._visitor(), "127.0.0.1")
        # One IPv6 visitor holds a whole /64: every address in it shares one allowance.
        handler.headers = {"CF-Connecting-IP": "2001:db8:1:2:aaaa::1"}
        self.assertEqual(handler._visitor(), "2001:db8:1:2::/64")
        handler.headers = {"CF-Connecting-IP": "2001:db8:1:2:ffff:1:2:3"}
        self.assertEqual(handler._visitor(), "2001:db8:1:2::/64")
        handler.headers = {"CF-Connecting-IP": "::ffff:203.0.113.5"}
        self.assertEqual(handler._visitor(), "203.0.113.5")


class ReviewRegressionTests(StatsTestCase):
    def test_a_broken_name_does_not_break_the_site(self):
        self.fx.player(A, "ok")
        self.fx.con.execute("UPDATE players SET name = CAST(X'4A6F73C3' AS TEXT) WHERE steamid = ?", (A,))
        self.fx.round({A: {"commons": 5}})
        stats = self.stats()
        self.assertEqual(stats.leaderboard()["players"][0]["name"], "Jos\ufffd")
        self.assertTrue(stats.summary()["ready"])
        self.assertTrue(stats.records()["ready"])

    def test_passing_items_earns_nothing(self):
        self.fx.player(A, "Ann")
        self.fx.round({A: {"gives": 500}})
        self.assertEqual(self.stats().leaderboard()["players"][0]["points"], 0)

    def test_periods_go_by_when_the_chapter_ended(self):
        # A hibernating server loaded the map long ago; the chapter was played today.
        self.fx.player(A, "Ann")
        self.fx.round({A: {"commons": 5}}, started=NOW - 20 * DAY, ended=NOW - 3600)
        self.assertEqual(len(self.stats().leaderboard(period="week")["players"]), 1)

    def test_recent_games_of_hidden_players_are_left_out(self):
        self.fx.player(A, "Ann")
        self.fx.player(B, "Bob")
        self.fx.round({B: {}}, ended=NOW - 10)
        self.fx.round({A: {}, B: {}}, ended=NOW - 20)
        recent = self.stats(hidden={B}).summary()["recent"]
        self.assertEqual([r["players"] for r in recent], [["Ann"]])

    def test_profile_rank_matches_the_board(self):
        for steamid, name in ((A, "Ann"), (B, "Bob"), (C, "Cy")):
            self.fx.player(steamid, name)
        self.fx.round({A: {"commons": 10}, B: {"commons": 10}, C: {"commons": 10}})
        stats = self.stats(hidden={A})
        board = {p["steamid"]: p["rank"] for p in stats.leaderboard()["players"]}
        for steamid in (B, C):
            self.assertEqual(stats.player(steamid)["rank"], {"place": board[steamid], "of": 2})


class SecondReviewTests(StatsTestCase):
    def test_hidden_record_holders_cannot_erase_a_record(self):
        self.fx.player(A, "Ann")
        self.fx.player(B, "Bob")
        for _ in range(30):                         # more than any LIMIT a filter could hide behind
            self.fx.round({A: {"commons": 500}})
        self.fx.round({B: {"commons": 10}})
        records = self.stats(hidden={A}).records()
        commons = next(r for r in records["chapter"] if r["key"] == "commons")
        self.assertEqual((commons["name"], commons["value"]), ("Bob", 10))
        self.assertEqual(next(r for r in records["career"] if r["key"] == "commons")["name"], "Bob")

    def test_maps_page_leaves_out_rounds_only_hidden_players_played(self):
        self.fx.player(A, "Ann")
        self.fx.player(B, "Bob")
        self.fx.round({A: {}}, map="c1m1_hotel", left_safe=NOW - DAY, ended=NOW - DAY + 60)
        self.fx.round({B: {}}, map="c1m1_hotel", left_safe=NOW - 2 * DAY, ended=NOW - 2 * DAY + 600)
        maps = self.stats(hidden={A}).map_stats()["maps"]
        self.assertEqual([(m["rounds"], m["fastest"], m["last_played"]) for m in maps],
                         [(1, 600, NOW - 2 * DAY + 600)])


class HiddenTotalsTests(StatsTestCase):
    def test_a_hidden_player_alone_leaves_no_trace_on_the_front_page(self):
        self.fx.player(A, "Ann")
        self.fx.player(B, "Bob")
        self.fx.round({B: {"commons": 7}}, result="won")
        self.fx.round({A: {"commons": 50}}, result="playing", ended=NOW - 30)
        self.fx.live(NOW - 30, online=[(A, "Ann")])
        shown = self.stats(hidden={A}).summary()
        self.assertEqual(shown["live"], {"online": False, "updated": None})
        self.assertEqual((shown["totals"]["commons"], shown["totals"]["chapters"], shown["totals"]["campaigns"]), (7, 1, 1))


class SteamNoDataTests(unittest.TestCase):
    def test_a_refusal_does_not_wipe_known_figures(self):
        fake = FakeSteam([{"steamid": A, "communityvisibilitystate": 3}])
        clock = [NOW]
        cache = steam_mod.SteamCache("K" * 32, "", fetch=fake, clock=lambda: clock[0], log=lambda m: None)
        cache.refresh([A])
        clock[0] += steam_mod.DETAIL_TTL + 1           # details due, profile still fresh
        fake.private.add(A)                            # every details call now answers 403
        cache.refresh([A])
        self.assertEqual((cache.get(A)["hours"], cache.get(A)["achievements"]), (100.5, {"done": 2, "total": 3}))

    def test_a_player_without_stats_does_not_hold_up_the_others(self):
        class NoStats(FakeSteam):
            def __call__(self, url):
                if "GetPlayerAchievements" in url and A in url:
                    self.calls.append(url)
                    raise steam_mod.SteamError("Steam answered 400", 400)   # e.g. a borrowed game
                return super().__call__(url)
        fake = NoStats([{"steamid": sid, "communityvisibilitystate": 3} for sid in (A, B)])
        cache = steam_mod.SteamCache("K" * 32, "", fetch=fake, clock=lambda: NOW, log=lambda m: None)
        cache.refresh([A, B])
        self.assertEqual(cache.get(A)["achievements"], None)
        self.assertEqual(cache.get(A)["details_fetched"], NOW)
        self.assertEqual(cache.get(B)["achievements"], {"done": 2, "total": 3})


class SteamOutageTests(unittest.TestCase):
    def test_a_steam_outage_keeps_what_was_known(self):
        fake = FakeSteam([{"steamid": A, "personaname": "P", "communityvisibilitystate": 3}])
        clock = [NOW]
        cache = steam_mod.SteamCache("K" * 32, "", fetch=fake, clock=lambda: clock[0], log=lambda m: None)
        cache.refresh([A])
        self.assertEqual(cache.get(A)["hours"], 100.5)
        clock[0] += 2 * steam_mod.DETAIL_TTL
        fake.fail, fake.fail_on = 503, "GetOwnedGames"       # profiles answer; the hours call fails
        cache.refresh([A])
        self.assertTrue(any("GetOwnedGames" in u for u in fake.calls[-2:]))
        self.assertEqual(cache.get(A)["hours"], 100.5)                  # not wiped by the outage
        fake.fail = None
        clock[0] += 3600
        cache.refresh([A])
        self.assertEqual(cache.get(A)["details_fetched"], clock[0])    # and fetched again later


class CacheTests(unittest.TestCase):
    def test_not_found_is_not_kept(self):
        cache = server_mod.Cache()
        calls = []
        cache.get("x", lambda: calls.append(1))
        cache.get("x", lambda: calls.append(1))
        self.assertEqual(len(calls), 2)
        self.assertEqual(cache.items, {})


class FrontEndTests(unittest.TestCase):
    def test_server_text_is_never_parsed_as_html(self):
        with open(os.path.join(REPO, "stats", "static", "app.js"), encoding="utf-8") as fh:
            source = fh.read()
        for risky in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function"):
            self.assertNotIn(risky, source)


class LimiterTests(unittest.TestCase):
    def test_refills_over_time(self):
        now = [0.0]
        limiter = server_mod.Limiter(rate=1, burst=2, clock=lambda: now[0])
        self.assertEqual([limiter.allow("x") for _ in range(3)], [True, True, False])
        now[0] += 1.0
        self.assertTrue(limiter.allow("x"))


if __name__ == "__main__":
    unittest.main()
