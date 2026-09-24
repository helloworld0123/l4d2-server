"""End-to-end through the real RCON client, parser and handlers.

Only the Telegram transport and the game server are faked.
"""

import socketserver
import struct
import tempfile
import threading
import unittest

from bot import commands, config as config_mod, context, rcon

STATUS_REPLY = '''hostname: Test Server
map     : c5m1_waterfront at: 0 x, 0 y, 0 z
players : 1 humans, 1 bots (8 max)
# userid name uniqueid connected ping loss state rate adr
#  2 1 "Enrico <tank>" STEAM_1:0:12345678 07:02 48 0 active 30000 10.0.0.5:27005
#  3 2 "Rochelle" BOT active
'''

SENT = []


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
            reply = STATUS_REPLY if text == "status" else f"ack: {text}"
            self.request.sendall(rcon.encode_packet(pid, 0, reply))

    def _read(self, n):
        buf = b""
        while len(buf) < n:
            chunk = self.request.recv(n - len(buf))
            if not chunk:
                return None
            buf += chunk
        return buf


class RecordingBot:
    def __init__(self):
        self.sent = []

    def send(self, chat_id, text, preformatted=False, buttons=None, reply_to=None):
        self.sent.append(text)
        return {"message_id": len(self.sent)}

    def edit(self, *a, **k):
        return {}

    def answer_callback(self, *a, **k):
        return None


class InlineRunner:
    def submit(self, name, who, fn):
        fn()
        return True, None


class IntegrationTests(unittest.TestCase):
    def setUp(self):
        SENT.clear()
        rcon._auth_blocked_until = 0.0
        self.server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), GameServer)
        host, port = self.server.server_address
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

        self.cfg_file = tempfile.NamedTemporaryFile("w", suffix=".cfg", delete=False)
        self.cfg_file.write('hostname "x"\nrcon_password "secret"\n')
        self.cfg_file.close()

        cfg = config_mod.Config("t", -1001, {"L4D2_MAP": "c1m1_hotel"}, host, port)
        cfg.__class__.cfg_path = property(lambda _self, p=self.cfg_file.name: p)

        self.bot = RecordingBot()
        self.ctx = context.Context(self.bot, cfg, InlineRunner(), log=lambda *_: None)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def _msg(self, text):
        import time
        return {"text": text, "date": int(time.time()),
                "chat": {"id": -1001}, "from": {"id": 7, "is_bot": False}}

    def test_players_end_to_end(self):
        commands.cmd_players(self.ctx, [], self._msg("/players"))
        out = self.bot.sent[-1]
        self.assertIn("Enrico", out)
        self.assertIn("1 player(s) connected", out)
        self.assertIn("1 bots", out)
        self.assertNotIn("Rochelle", out)  # bots aren't listed as players

    def test_say_reaches_the_server_sanitised(self):
        commands.cmd_say(self.ctx, ["hi", "all;", "quit"], self._msg("/say"))
        self.assertEqual(SENT[-1], 'say "hi all, quit"')

    def test_cheats_toggle(self):
        commands.cmd_cheats(self.ctx, ["on"], self._msg("/cheats on"))
        self.assertEqual(SENT[-1], "sv_cheats 1")

    def test_map_reports_the_live_map(self):
        commands.cmd_map(self.ctx, [], self._msg("/map"))
        self.assertIn("c5m1_waterfront", self.bot.sent[-1])

    def test_password_is_read_from_server_cfg_each_time(self):
        # Rewriting server.cfg must take effect without restarting the bot.
        with open(self.cfg_file.name, "w") as fh:
            fh.write('rcon_password "changed"\n')
        self.assertEqual(self.ctx.cfg.rcon_password(), "changed")


if __name__ == "__main__":
    unittest.main()
