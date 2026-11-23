"""The web panel, end to end over real HTTP.

Only the game server (a fake RCON listener) and the `l4d2` CLI (a stub script)
are faked; auth, routing, config rewriting and the RCON client are the real ones.
"""

import http.client
import socket
import json
import os
import socketserver
import stat
import struct
import tempfile
import threading
import time
import unittest

from bot import rcon, sanitize
from web import auth, config as config_mod, panel as panel_mod, server, serverconf

PASSWORD = "correct horse battery"
# Hashing at full strength would add seconds to every test class.
PASSWORD_HASH = auth.hash_password(PASSWORD, iterations=1000)

STATUS_REPLY = '''hostname: Test Server
map     : c5m1_waterfront
players : 1 humans, 1 bots (8 max) (not hibernating) (unreserved)
# userid name uniqueid connected ping loss state rate adr
#  2 1 "Enrico <img src=x>" STEAM_1:0:12345678 07:02 48 0 active 30000 10.0.0.5:27005
#  3 2 "Rochelle" BOT active
'''
CVAR_REPLY = '"z_difficulty" = "Hard"\n"sv_cheats" = "0"\n"mp_gamemode" = "coop"\n'

SENT = []


def read(path):
    with open(path) as fh:
        return fh.read()


class GameServer(socketserver.BaseRequestHandler):
    def handle(self):
        while True:
            head = self._read(4)
            if not head:
                return
            body = self._read(struct.unpack("<i", head)[0])
            if body is None:
                return
            pid, ptype = struct.unpack("<ii", body[:8])
            text = body[8:-2].decode()
            if ptype == rcon.SERVERDATA_AUTH:
                self.request.sendall(rcon.encode_packet(pid, rcon.SERVERDATA_AUTH_RESPONSE, ""))
                continue
            SENT.append(text)
            reply = STATUS_REPLY if text == "status" else CVAR_REPLY if text.startswith("z_difficulty;") else f"ack: {text}"
            self.request.sendall(rcon.encode_packet(pid, 0, reply))

    def _read(self, n):
        buf = b""
        while len(buf) < n:
            chunk = self.request.recv(n - len(buf))
            if not chunk:
                return None
            buf += chunk
        return buf


class AuthTests(unittest.TestCase):
    def test_hash_round_trip(self):
        self.assertTrue(auth.verify_password(PASSWORD, PASSWORD_HASH))
        self.assertFalse(auth.verify_password("wrong", PASSWORD_HASH))

    def test_garbage_hash_never_verifies(self):
        for stored in ("", "plain", "pbkdf2_sha256:x:y:z", "md5:1:00:00", None):
            self.assertFalse(auth.verify_password(PASSWORD, stored))

    def test_sessions_expire_when_idle(self):
        sessions = auth.Sessions(idle=0.05)
        token = sessions.create()
        self.assertTrue(sessions.valid(token))
        time.sleep(0.1)
        self.assertFalse(sessions.valid(token))
        self.assertFalse(sessions.valid("made-up"))
        self.assertFalse(sessions.valid(None))

    def test_session_limit_evicts_oldest(self):
        sessions = auth.Sessions(limit=2)
        first, second, third = sessions.create(), sessions.create(), sessions.create()
        self.assertFalse(sessions.valid(first))
        self.assertTrue(sessions.valid(second) and sessions.valid(third))

    def test_throttle_locks_out_then_clears(self):
        throttle = auth.Throttle(attempts=3, window=60)
        for _ in range(3):
            self.assertEqual(throttle.attempt("1.2.3.4"), 0)
        self.assertGreater(throttle.attempt("1.2.3.4"), 0)
        self.assertEqual(throttle.attempt("5.6.7.8"), 0)
        throttle.succeeded("1.2.3.4")
        self.assertEqual(throttle.attempt("1.2.3.4"), 0)

    def test_throttle_counts_parallel_attempts(self):
        throttle = auth.Throttle(attempts=5, window=60)
        allowed = []
        threads = [threading.Thread(target=lambda: allowed.append(throttle.attempt("9.9.9.9") == 0))
                   for _ in range(40)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(sum(allowed), 5)


class ServerConfTests(unittest.TestCase):
    CFG = ('// Generated\nhostname "Old Name"\nsv_password "pw"\n'
           'rcon_password "secret"          // empty = remote admin disabled\nz_difficulty "Normal"\n')

    def test_read_cvar(self):
        self.assertEqual(serverconf.read_cvar(self.CFG, "hostname"), "Old Name")
        self.assertEqual(serverconf.read_cvar(self.CFG, "rcon_password"), "secret")
        self.assertIsNone(serverconf.read_cvar(self.CFG, "sv_cheats"))
        self.assertEqual(serverconf.read_cvar("sv_maxplayers 8   // note\n", "sv_maxplayers"), "8")
        self.assertIsNone(serverconf.read_cvar('// hostname "commented out"\n', "hostname"))

    def test_set_cvar_replaces_keeps_comment_and_appends(self):
        text = serverconf.set_cvar(self.CFG, "rcon_password", "new")
        self.assertIn('rcon_password "new"          // empty = remote admin disabled', text)
        self.assertEqual(text.count("rcon_password"), 1)
        text = serverconf.set_cvar("hostname \"x\"", "sv_password", "p")
        self.assertEqual(text, 'hostname "x"\nsv_password "p"\n')

    def test_set_cvar_does_not_touch_longer_names(self):
        text = serverconf.set_cvar('sv_password_extra "1"\n', "sv_password", "p")
        self.assertIn('sv_password_extra "1"', text)
        self.assertIn('sv_password "p"', text)

    def test_clean_value_removes_what_would_break_out(self):
        self.assertEqual(serverconf.clean_value('My "Server"; quit\n\\', 64), "My Server quit")

    def test_atomic_write_keeps_mode_and_backup_prunes(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "server.cfg")
            with open(path, "w") as fh:
                fh.write("a\n")
            os.chmod(path, 0o640)
            for stamp in range(100, 100 + serverconf.KEEP_BACKUPS + 3):
                with open(f"{path}.bak.{stamp}", "w") as fh:
                    fh.write("old\n")
            serverconf.backup(path)
            serverconf.atomic_write(path, "b\n")
            self.assertEqual(read(path), "b\n")
            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o640)
            backups = [n for n in os.listdir(tmp) if ".bak." in n]
            self.assertEqual(len(backups), serverconf.KEEP_BACKUPS)
            self.assertNotIn("server.cfg.bak.100", backups)
            self.assertFalse([n for n in os.listdir(tmp) if n.startswith(".panel-")])

    def test_atomic_write_replaces_a_symlink_instead_of_following_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            victim = os.path.join(tmp, "victim")
            with open(victim, "w") as fh:
                fh.write("untouched")
            link = os.path.join(tmp, "server.cfg")
            os.symlink(victim, link)
            serverconf.atomic_write(link, "new\n")
            self.assertEqual(read(victim), "untouched")
            self.assertFalse(os.path.islink(link))

    def test_atomic_write_never_chowns_through_a_swapped_symlink(self):
        # The game user owns the directory and can replace our temp file with a
        # link at any moment; permissions must be applied to the descriptor.
        with tempfile.TemporaryDirectory() as tmp:
            victim = os.path.join(tmp, "victim")
            with open(victim, "w") as fh:
                fh.write("untouched")
            os.chmod(victim, 0o600)
            path = os.path.join(tmp, "server.cfg")
            with open(path, "w") as fh:
                fh.write("a\n")
            os.chmod(path, 0o666)
            real_replace = os.replace

            def swap_then_replace(src, dst):
                os.unlink(src)
                os.symlink(victim, src)
                real_replace(src, dst)

            serverconf.os.replace = swap_then_replace
            try:
                serverconf.atomic_write(path, "b\n")
            finally:
                serverconf.os.replace = real_replace
            self.assertEqual(stat.S_IMODE(os.stat(victim).st_mode), 0o600)
            self.assertEqual(read(victim), "untouched")

    def test_readers_refuse_links_and_fifos(self):
        with tempfile.TemporaryDirectory() as tmp:
            secret = os.path.join(tmp, "secret")
            with open(secret, "w") as fh:
                fh.write("root only")
            link, fifo = os.path.join(tmp, "server.cfg"), os.path.join(tmp, "console.log")
            os.symlink(secret, link)
            os.mkfifo(fifo)
            self.assertEqual(serverconf.read_text(link), "")
            self.assertEqual(serverconf.read_text(fifo), "")  # and does not hang
            with self.assertRaises(OSError):
                serverconf.read_tail(fifo)
            serverconf.backup(link)
            self.assertEqual(os.listdir(tmp).count("server.cfg.bak"), 0)

    def test_set_env_value(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "l4d2.env")
            with open(path, "w") as fh:
                fh.write("L4D2_MODE=coop\nL4D2_MAP=c1m1_hotel\n")
            serverconf.set_env_value(path, "L4D2_MODE", "versus")
            serverconf.set_env_value(path, "L4D2_NEW", "1")
            self.assertEqual(read(path), "L4D2_MODE=versus\nL4D2_MAP=c1m1_hotel\nL4D2_NEW=1\n")
            with self.assertRaises(ValueError):
                serverconf.set_env_value(path, "L4D2_MAP", "x; rm -rf /")
            for bad in ("$(id)", "a;b", "a|b", "a&b", "a#b", "a\nb", "a>b", "(x)"):
                with self.assertRaises(ValueError):
                    serverconf.set_env_value(path, "L4D2_MAP", bad)

    def test_update_info_rewrites_password_block(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "SERVER-INFO.txt")
            original = ("Server name : Old\nPassword    : pw\nRCON        : secret\n\n"
                        "In-game console:  password pw\n                  connect 100.1.2.3:27015\n")
            with open(path, "w") as fh:
                fh.write(original)
            serverconf.update_info(path, hostname="New", password="fresh")
            text = read(path)
            self.assertIn("Server name : New", text)
            self.assertIn("Password    : fresh", text)
            self.assertIn("In-game console:  password fresh\n                  connect 100.1.2.3:27015", text)
            serverconf.update_info(path, password="")
            text = read(path)
            self.assertIn("Password    : (none)", text)
            self.assertIn("In-game console:  connect 100.1.2.3:27015", text)
            serverconf.update_info(path, password="back")
            self.assertIn("In-game console:  password back\n                  connect 100.1.2.3:27015",
                          read(path))


class SanitizeTests(unittest.TestCase):
    def test_rcon_guard_sees_past_a_semicolon(self):
        self.assertTrue(sanitize.rcon_refused("rcon_password x"))
        self.assertTrue(sanitize.rcon_refused("echo hi; rcon_password x"))
        self.assertTrue(sanitize.rcon_refused("status;sv_rcon_banpenalty 0"))
        self.assertFalse(sanitize.rcon_refused("status; sv_cheats 1"))
        self.assertTrue(sanitize.rcon_needs_confirm("echo; quit"))
        self.assertFalse(sanitize.rcon_needs_confirm("status"))


class PanelHttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.game = socketserver.ThreadingTCPServer(("127.0.0.1", 0), GameServer)
        cls.game.daemon_threads = True
        threading.Thread(target=cls.game.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.game.shutdown()
        cls.game.server_close()

    def setUp(self):
        SENT.clear()
        rcon._auth_blocked_until = 0.0
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = self.tmp.name
        self.cfg_dir = os.path.join(root, "srv", "left4dead2", "cfg")
        os.makedirs(self.cfg_dir)
        os.makedirs(os.path.join(root, "srv", "left4dead2", "addons"))
        self.cfg_path = os.path.join(self.cfg_dir, "server.cfg")
        with open(self.cfg_path, "w") as fh:
            fh.write('hostname "Test Server"\nsv_password "joinme"\nrcon_password "secret"\nz_difficulty "Normal"\n')
        with open(os.path.join(root, "srv", "left4dead2", "console.log"), "w") as fh:
            fh.write("line one\nline two\nline three\n")
        with open(os.path.join(root, "maps.json"), "w") as fh:
            json.dump({"123456": {"campaign": "Dead City", "first_map": "deadcity_m1",
                                  "maps": ["deadcity_m1", "deadcity_m2"], "file": "123456.vpk",
                                  "source": "workshop", "id": "123456",
                                  "url": "https://steamcommunity.com/sharedfiles/filedetails/?id=123456"}}, fh)
        with open(os.path.join(root, "SERVER-INFO.txt"), "w") as fh:
            fh.write("Server name : Test Server\nPassword    : joinme\n\n"
                     "In-game console:  password joinme\n                  connect 100.1.2.3:27015\n")
        self.env_path = os.path.join(root, "l4d2.env")
        with open(self.env_path, "w") as fh:
            fh.write(f"L4D2_USER=no-such-user-l4d2\nL4D2_HOME={root}\nL4D2_DIR={root}/srv\n"
                     "L4D2_PORT=27015\nL4D2_MODE=coop\nL4D2_MAP=c1m1_hotel\nL4D2_SLOTS=4\n")
        self.cli_log = os.path.join(root, "cli.log")
        cli = os.path.join(root, "l4d2")
        with open(cli, "w") as fh:
            fh.write(f'#!/bin/sh\necho "$@" >> "{self.cli_log}"\necho "ran: $@"\n')
        os.chmod(cli, 0o755)

        cfg = config_mod.WebConfig(
            bind="127.0.0.1", port=0, password_hash=PASSWORD_HASH, server_env_path=self.env_path,
            rcon_host="127.0.0.1", rcon_port=self.game.server_address[1], l4d2_bin=cli,
            lock_path=os.path.join(root, "l4d2.lock"))
        self.httpd = server.make_server(cfg, log=lambda _message: None)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.addCleanup(self.httpd.server_close)
        self.addCleanup(self.httpd.shutdown)
        self.cookie = None

    # ---------------------------------------------------------------- helpers
    def request(self, method, path, body=None, headers=None, cookie=True):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=15)
        send = {"X-Requested-With": "l4d2-panel"} if method == "POST" else {}
        if body is not None:
            send["Content-Type"] = "application/json"
        if cookie and self.cookie:
            send["Cookie"] = self.cookie
        send.update(headers or {})
        conn.request(method, path, json.dumps(body) if body is not None else None, send)
        response = conn.getresponse()
        raw = response.read()
        conn.close()
        try:
            data = json.loads(raw)
        except ValueError:
            data = raw
        return response, data

    def login(self):
        response, _ = self.request("POST", "/api/login", {"password": PASSWORD})
        self.assertEqual(response.status, 200)
        self.cookie = response.getheader("Set-Cookie").split(";")[0]
        return response

    def wait_for_job(self):
        for _ in range(100):
            _, data = self.request("GET", "/api/job")
            if data["job"] and not data["job"]["running"]:
                return data["job"]
            time.sleep(0.05)
        self.fail("job never finished")

    def cli_calls(self):
        try:
            return read(self.cli_log).splitlines()
        except OSError:
            return []

    # ------------------------------------------------------------------ auth
    def test_api_requires_a_session(self):
        for path in ("/api/overview", "/api/maps", "/api/settings", "/api/config", "/api/logs", "/api/job"):
            response, _ = self.request("GET", path)
            self.assertEqual(response.status, 401, path)
        response, _ = self.request("POST", "/api/control", {"action": "stop"})
        self.assertEqual(response.status, 401)
        response, _ = self.request("POST", "/api/rcon", {"command": "status"}, headers={"Cookie": "l4d2_session=forged"})
        self.assertEqual(response.status, 401)
        self.assertEqual(SENT, [])
        self.assertEqual(self.cli_calls(), [])

    def test_login_logout(self):
        _, data = self.request("GET", "/api/session")
        self.assertFalse(data["authenticated"])
        response = self.login()
        cookie = response.getheader("Set-Cookie")
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Strict", cookie)
        _, data = self.request("GET", "/api/session")
        self.assertTrue(data["authenticated"])
        self.request("POST", "/api/logout", {})
        response, _ = self.request("GET", "/api/overview")
        self.assertEqual(response.status, 401)

    def test_wrong_password_is_throttled(self):
        for _ in range(5):
            response, _ = self.request("POST", "/api/login", {"password": "nope"})
            self.assertEqual(response.status, 401)
        response, _ = self.request("POST", "/api/login", {"password": PASSWORD})
        self.assertEqual(response.status, 429)
        self.assertTrue(response.getheader("Retry-After"))

    def test_cross_site_posts_are_refused(self):
        self.login()
        response, _ = self.request("POST", "/api/control", {"action": "stop"}, headers={"X-Requested-With": ""})
        self.assertEqual(response.status, 403)
        response, _ = self.request("POST", "/api/control", {"action": "stop"}, headers={"Origin": "http://evil.example"})
        self.assertEqual(response.status, 403)
        self.assertEqual(self.cli_calls(), [])

    def test_parallel_wrong_passwords_cannot_outrun_the_throttle(self):
        statuses = []
        threads = [threading.Thread(target=lambda: statuses.append(
            self.request("POST", "/api/login", {"password": "nope"})[0].status)) for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(statuses.count(401), 5)
        self.assertEqual(statuses.count(429), 15)

    def test_unknown_hostnames_are_refused(self):
        # DNS rebinding: the browser sends the attacker's name as Host.
        for method, path in (("GET", "/"), ("GET", "/api/session"), ("POST", "/api/login")):
            response, _ = self.request(method, path, {"password": PASSWORD} if method == "POST" else None,
                                       headers={"Host": f"evil.example:{self.port}"})
            self.assertIn(response.status, (403, 421), path)
        self.httpd.app.cfg.hostnames.add("gamebox")
        self.assertEqual(self.request("GET", "/", headers={"Host": f"gamebox:{self.port}"})[0].status, 200)
        self.assertEqual(self.request("GET", "/", headers={"Host": "[::1]:8080"})[0].status, 200)

    def test_broken_maps_index_does_not_break_the_page(self):
        self.login()
        with open(os.path.join(self.tmp.name, "maps.json"), "w") as fh:
            fh.write('{"1": "not an object", "2": ["nor this"], "3": {"campaign": "Fine"}}')
        response, maps = self.request("GET", "/api/maps")
        self.assertEqual(response.status, 200)
        self.assertEqual([m["name"] for m in maps["custom"]], ["Fine"])
        self.assertEqual(self.request("GET", "/api/overview")[0].status, 200)

    def test_static_and_headers(self):
        response, body = self.request("GET", "/")
        self.assertEqual(response.status, 200)
        self.assertIn(b"L4D2 Server Panel", body)
        self.assertIn("default-src 'self'", response.getheader("Content-Security-Policy"))
        self.assertEqual(response.getheader("X-Frame-Options"), "DENY")
        for path in ("/app.js", "/app.css", "/favicon.svg", "/healthz"):
            self.assertEqual(self.request("GET", path)[0].status, 200, path)
        for path in ("/../etc/passwd", "/static/../server.py", "/app.js/..", "/api/nope"):
            self.assertEqual(self.request("GET", path)[0].status, 404, path)

    def test_bad_bodies(self):
        self.login()
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=15)
        conn.request("POST", "/api/say", "not json", {"X-Requested-With": "l4d2-panel", "Cookie": self.cookie})
        self.assertEqual(conn.getresponse().status, 400)
        conn.close()
        response, _ = self.request("POST", "/api/say", ["a list"])
        self.assertEqual(response.status, 400)
        response, _ = self.request("POST", "/api/say", {"text": "x"}, headers={"Content-Length": str(10**9)})
        self.assertEqual(response.status, 413)

    # -------------------------------------------------------------- reading
    def test_overview_lists_players_without_addresses(self):
        self.login()
        response, data = self.request("GET", "/api/overview")
        self.assertEqual(response.status, 200)
        self.assertTrue(data["game"]["reachable"])
        self.assertEqual(data["game"]["hostname"], "Test Server")
        self.assertEqual(data["game"]["map"], "c5m1_waterfront")
        self.assertEqual(data["game"]["campaign"], "The Parish")
        self.assertEqual(data["game"]["campaign_map"], "c5m1_waterfront")
        self.assertEqual((data["game"]["humans"], data["game"]["bots"], data["game"]["max_players"]), (1, 1, 8))
        self.assertEqual(data["game"]["difficulty"], "Hard")
        self.assertFalse(data["game"]["cheats"])
        self.assertEqual(len(data["players"]), 1)
        self.assertEqual(data["players"][0]["name"], "Enrico <img src=x>")  # escaped by the page, not here
        self.assertEqual(data["players"][0]["userid"], "2")
        self.assertNotIn("10.0.0.5", json.dumps(data))
        self.assertEqual(data["join"], {"connect": "connect 100.1.2.3:27015", "password": "joinme"})
        self.assertNotIn("secret", json.dumps(data))

    def test_campaign_is_found_from_any_chapter(self):
        panel = self.httpd.app.panel
        self.assertEqual(panel._campaign("c2m3_coaster"), ("c2m1_highway", "Dark Carnival"))
        self.assertEqual(panel._campaign("C14M2_Lighthouse"), ("c14m1_junkyard", "The Last Stand"))
        self.assertEqual(panel._campaign("c1m1_hotel"), ("c1m1_hotel", "Dead Center"))
        self.assertEqual(panel._campaign("deadcity_m2"), ("deadcity_m1", "Dead City"))
        self.assertEqual(panel._campaign("unknown_map"), ("", ""))
        self.assertEqual(panel._campaign("c99m1_nothing"), ("", ""))
        self.assertEqual(panel._campaign(""), ("", ""))

    def test_console_explains_shell_commands_and_a_dead_game(self):
        self.login()
        response, data = self.request("POST", "/api/rcon", {"command": "l4d2 status"})
        self.assertEqual(response.status, 400)
        self.assertIn("shell command", data["error"])
        self.assertEqual(SENT, [])
        with socket.socket() as probe:              # a port nothing listens on
            probe.bind(("127.0.0.1", 0))
            dead = probe.getsockname()[1]
        with self.assertRaises(rcon.RconError) as caught:
            rcon.execute("status", "secret", port=dead, timeout=2)
        self.assertIn("isn't listening for RCON", str(caught.exception))
        self.assertNotIn("..", str(caught.exception))

    def test_campaign_survives_a_damaged_map_list(self):
        with open(os.path.join(os.path.dirname(self.env_path), "maps.json"), "w") as fh:
            json.dump({"1": {"maps": 3, "first_map": 5}, "2": {"maps": "abc", "campaign": {"x": 1}},
                       "3": {"maps": [7, "c2m5_remake", "remake_m2"], "first_map": ["x"], "campaign": ["y"]},
                       "4": {"maps": ["solo_m1"], "title": "Solo"}}, fh)
        panel = self.httpd.app.panel
        self.assertEqual(panel._campaign("a"), ("", ""))
        self.assertEqual(panel._campaign("5"), ("", ""))
        # A custom campaign that reuses the official naming wins over the official one.
        self.assertEqual(panel._campaign("c2m5_remake"), ("c2m5_remake", ""))
        self.assertEqual(panel._campaign("solo_m1"), ("solo_m1", "Solo"))
        self.assertEqual(panel._campaign("c2m3_coaster"), ("c2m1_highway", "Dark Carnival"))
        self.login()
        self.assertEqual(self.request("GET", "/api/overview")[0].status, 200)

    def test_overview_when_the_game_is_down(self):
        self.login()
        self.httpd.app.cfg._rcon_port = 1  # nothing listens there
        response, data = self.request("GET", "/api/overview")
        self.assertEqual(response.status, 200)
        self.assertFalse(data["game"]["reachable"])
        self.assertFalse(data["server"]["running"])
        self.assertEqual(data["players"], [])
        self.assertEqual(data["game"]["hostname"], "Test Server")  # falls back to server.cfg

    def test_maps_settings_logs(self):
        self.login()
        _, maps = self.request("GET", "/api/maps")
        self.assertEqual(len(maps["official"]), 14)
        self.assertEqual(maps["custom"][0]["first_map"], "deadcity_m1")
        self.assertTrue(maps["custom"][0]["missing"])
        _, settings = self.request("GET", "/api/settings")
        self.assertEqual(settings["hostname"], "Test Server")
        self.assertEqual(settings["mode"], "coop")
        self.assertTrue(settings["rcon_enabled"])
        self.assertNotIn("secret", json.dumps(settings))
        _, logs = self.request("GET", "/api/logs?lines=2")
        self.assertEqual(logs["lines"], ["line two", "line three"])
        self.assertEqual(self.request("GET", "/api/logs?lines=banana")[0].status, 200)

    # --------------------------------------------------------------- actions
    def test_kick_say_and_console(self):
        self.login()
        self.assertEqual(self.request("POST", "/api/kick", {"userid": "2"})[0].status, 200)
        self.assertEqual(self.request("POST", "/api/kick", {"userid": "2; quit"})[0].status, 400)
        self.assertEqual(self.request("POST", "/api/say", {"text": 'hi"; quit'})[0].status, 200)
        response, data = self.request("POST", "/api/rcon", {"command": "sv_cheats 1"})
        self.assertEqual(data["output"], "ack: sv_cheats 1")
        self.assertEqual(self.request("POST", "/api/rcon", {"command": "echo; rcon_password x"})[0].status, 400)
        self.assertEqual(self.request("POST", "/api/rcon", {"command": "a\nb"})[0].status, 400)
        self.assertEqual(SENT, ["kickid 2", "say \"hi', quit\"", "sv_cheats 1"])

    def test_change_map(self):
        self.login()
        self.assertEqual(self.request("POST", "/api/map", {"name": "c2m1_highway"})[0].status, 200)
        self.assertEqual(SENT, ["changelevel c2m1_highway"])
        self.assertEqual(self.request("POST", "/api/map", {"name": "x; quit"})[0].status, 400)
        self.assertEqual(self.request("POST", "/api/map", {"name": "--default", "default": True})[0].status, 400)
        self.assertEqual(self.request("POST", "/api/map", {"name": "deadcity_m1", "default": True})[0].status, 200)
        self.assertEqual(self.wait_for_job()["exit_code"], 0)
        self.assertEqual(self.cli_calls(), ["changemap deadcity_m1 --default --no-restart"])

    def test_map_installs_only_take_workshop_ids(self):
        self.login()
        for bad in ("/etc/passwd", "--force", "", "abc"):
            self.assertEqual(self.request("POST", "/api/maps/add", {"links": bad})[0].status, 400, bad)
        links = "https://steamcommunity.com/sharedfiles/filedetails/?id=123456789 987654321 987654321"
        self.assertEqual(self.request("POST", "/api/maps/add", {"links": links})[0].status, 200)
        job = self.wait_for_job()
        self.assertIn("ran: addmap", job["output"])
        self.assertEqual(self.cli_calls(), ["addmap 123456789 987654321 --no-restart"])

    def test_remove_map_must_be_installed(self):
        self.login()
        self.assertEqual(self.request("POST", "/api/maps/remove", {"key": "--force"})[0].status, 404)
        self.assertEqual(self.request("POST", "/api/maps/remove", {"key": "123456"})[0].status, 200)
        self.wait_for_job()
        self.assertEqual(self.cli_calls(), ["removemap 123456 --no-restart"])
        _, data = self.request("GET", "/api/overview")
        self.assertEqual(data["server"]["restart_needed"], ["a map was removed"])

    def test_one_job_at_a_time(self):
        self.login()
        with open(self.httpd.app.cfg.l4d2_bin, "w") as fh:
            fh.write("#!/bin/sh\nsleep 0.5\n")
        self.assertEqual(self.request("POST", "/api/maps/update", {})[0].status, 200)
        response, data = self.request("POST", "/api/control", {"action": "restart"})
        self.assertEqual(response.status, 409)
        self.assertIn("Busy", data["error"])
        self.wait_for_job()

    def test_control_and_slots(self):
        self.login()
        self.assertEqual(self.request("POST", "/api/control", {"action": "stop"})[0].status, 200)
        self.assertEqual(self.request("POST", "/api/control", {"action": "format-disk"})[0].status, 400)
        for bad in (3, 13, "many", None):
            self.assertEqual(self.request("POST", "/api/slots", {"slots": bad})[0].status, 400, bad)
        self.assertEqual(self.request("POST", "/api/slots", {"slots": 8})[0].status, 200)
        self.wait_for_job()
        self.assertEqual(self.cli_calls(), ["stop", "slots 8 --no-restart"])

    def test_save_settings_writes_files_and_applies_live(self):
        self.login()
        response, data = self.request("POST", "/api/settings", {
            "hostname": 'Zombie "Night"; quit', "password": "newpass", "difficulty": "impossible",
            "mode": "versus", "default_map": "c8m1_apartment"})
        self.assertEqual(response.status, 200, data)
        self.assertEqual(data["changed"], ["server name", "password", "difficulty", "game mode", "default map"])
        self.assertEqual(data["live_failed"], [])
        self.assertTrue(data["restart_needed"])

        cfg = read(self.cfg_path)
        self.assertIn('hostname "Zombie Night quit"', cfg)
        self.assertIn('sv_password "newpass"', cfg)
        self.assertIn('z_difficulty "Impossible"', cfg)
        self.assertIn('rcon_password "secret"', cfg)
        self.assertEqual(SENT, ['hostname "Zombie Night quit"', 'sv_password "newpass"', "z_difficulty Impossible"])
        env = read(self.env_path)
        self.assertIn("L4D2_MODE=versus\n", env)
        self.assertIn("L4D2_MAP=c8m1_apartment\n", env)
        self.assertIn("password newpass", read(os.path.join(self.tmp.name, "SERVER-INFO.txt")))
        self.assertEqual(len([n for n in os.listdir(self.cfg_dir) if ".bak." in n]), 1)

        _, overview = self.request("GET", "/api/overview")
        self.assertIn("game mode changed", overview["server"]["restart_needed"])

        SENT.clear()
        _, again = self.request("POST", "/api/settings", {"hostname": "Zombie Night quit", "mode": "versus"})
        self.assertEqual(again["changed"], [])
        self.assertEqual(SENT, [])

    def test_save_settings_rejects_bad_values_without_writing(self):
        self.login()
        before = read(self.cfg_path)
        for body in ({"hostname": '";'}, {"difficulty": "nightmare"}, {"mode": "survival; quit"},
                     {"default_map": "../../etc"}, {"password": "has space"},
                     {"hostname": "Fine", "mode": "bogus"}):
            self.assertEqual(self.request("POST", "/api/settings", body)[0].status, 400, body)
        self.assertEqual(read(self.cfg_path), before)
        self.assertIn("L4D2_MODE=coop", read(self.env_path))

    def test_settings_still_save_when_rcon_is_down(self):
        self.login()
        self.httpd.app.cfg._rcon_port = 1
        response, data = self.request("POST", "/api/settings", {"hostname": "Offline Edit"})
        self.assertEqual(response.status, 200)
        self.assertEqual(data["live_failed"], ["server name"])
        self.assertIn('hostname "Offline Edit"', read(self.cfg_path))
        self.assertEqual(self.request("POST", "/api/settings", {"cheats": True})[0].status, 502)

    def test_saves_wait_their_turn_behind_the_l4d2_command(self):
        import fcntl
        self.login()
        before = read(self.cfg_path)
        with open(self.httpd.app.cfg.lock_path, "a+") as held:
            fcntl.flock(held, fcntl.LOCK_EX)
            held.write("update")
            held.flush()
            response, data = self.request("POST", "/api/settings", {"hostname": "Blocked"})
            self.assertEqual(response.status, 409)
            self.assertIn("'update' is still running", data["error"])
            self.assertEqual(self.request("POST", "/api/config", {"text": "x\n"})[0].status, 409)
        self.assertEqual(read(self.cfg_path), before)
        self.assertEqual(self.request("POST", "/api/settings", {"hostname": "Free"})[0].status, 200)
        self.assertEqual(read(self.httpd.app.cfg.lock_path), "")

    def test_cheats_toggle(self):
        self.login()
        self.assertEqual(self.request("POST", "/api/settings", {"cheats": True})[0].status, 200)
        self.assertEqual(SENT, ["sv_cheats 1"])

    def test_raw_config(self):
        self.login()
        _, raw = self.request("GET", "/api/config")
        self.assertIn('rcon_password "secret"', raw["text"])
        new = 'hostname "Raw"\r\nrcon_password "secret"'
        response, data = self.request("POST", "/api/config", {"text": new, "apply": True})
        self.assertEqual(response.status, 200)
        self.assertTrue(data["applied"])
        self.assertEqual(data["warnings"], [])
        self.assertEqual(read(self.cfg_path), 'hostname "Raw"\nrcon_password "secret"\n')
        self.assertEqual(SENT, ["exec server.cfg"])

        _, data = self.request("POST", "/api/config", {"text": 'hostname "No Rcon"\n'})
        self.assertIn("rcon_password is empty", data["warnings"][0])
        self.assertEqual(self.request("POST", "/api/config", {"text": "x" * 70000})[0].status, 400)
        self.assertEqual(self.request("POST", "/api/config", {"text": None})[0].status, 400)


class ConfigLoadTests(unittest.TestCase):
    def test_load_requires_a_password_hash(self):
        with tempfile.TemporaryDirectory() as tmp:
            web_env, server_env = os.path.join(tmp, "web.env"), os.path.join(tmp, "l4d2.env")
            with open(server_env, "w") as fh:
                fh.write("L4D2_PORT=27016\n")
            with self.assertRaises(config_mod.ConfigError):
                config_mod.WebConfig.load(web_env, server_env)
            with open(web_env, "w") as fh:
                fh.write("L4D2_WEB_PORT=9000\n")
            with self.assertRaises(config_mod.ConfigError):
                config_mod.WebConfig.load(web_env, server_env)
            with open(web_env, "w") as fh:
                fh.write(f"L4D2_WEB_BIND=100.1.2.3\nL4D2_WEB_PORT=9000\nL4D2_WEB_PASSWORD_HASH={PASSWORD_HASH}\n")
            cfg = config_mod.WebConfig.load(web_env, server_env)
            self.assertEqual((cfg.bind, cfg.port, cfg.rcon_port), ("100.1.2.3", 9000, 27016))
            self.assertTrue(auth.verify_password(PASSWORD, cfg.password_hash))


if __name__ == "__main__":
    unittest.main()
