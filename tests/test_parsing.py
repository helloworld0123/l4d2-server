import unittest

from bot import players, sanitize, telegram

STATUS = '''hostname: My L4D2 Server
version : 2.2.3.6 6501 secure
udp/ip  : 0.0.0.0:27015  (public ip: 100.116.142.115)
os      :  Linux
map     : c1m1_hotel at: 0 x, 0 y, 0 z
players : 2 humans, 2 bots (8 max) (not hibernating)
# userid name uniqueid connected ping loss state rate adr
#  2 1 "Enrico" STEAM_1:0:12345678 04:21 52 0 active 30000 100.101.208.126:27005
#  3 2 "Coach" BOT active
#  4 3 "<b>grief;quit</b>" STEAM_1:1:999 00:05 31 0 active 30000 10.0.0.2:27005
#  5 4 "Nick" BOT active
'''


class StatusParserTests(unittest.TestCase):
    def setUp(self):
        self.info, self.players = players.parse_status(STATUS)

    def test_header_fields(self):
        self.assertEqual(self.info["hostname"], "My L4D2 Server")
        self.assertTrue(self.info["map"].startswith("c1m1_hotel"))

    def test_counts_humans_and_bots(self):
        self.assertEqual(len(self.players), 4)
        self.assertEqual(len(players.humans(self.players)), 2)

    def test_header_row_is_not_a_player(self):
        self.assertNotIn("userid", [p.userid for p in self.players])

    def test_fields_extracted(self):
        enrico = self.players[0]
        self.assertEqual(enrico.userid, "2")
        self.assertEqual(enrico.name, "Enrico")
        self.assertEqual(enrico.connected, "04:21")
        self.assertEqual(enrico.ping, "52")
        self.assertFalse(enrico.is_bot)

    def test_bots_flagged(self):
        self.assertTrue(self.players[1].is_bot)

    def test_empty_and_garbage_input_never_raises(self):
        self.assertEqual(players.parse_status("")[1], [])
        self.assertEqual(players.parse_status(None)[1], [])
        self.assertEqual(players.parse_status("#### nonsense ###")[1], [])

    def test_empty_server_message(self):
        info, plist = players.parse_status(
            "map     : c2m1_highway at: 0 x\nplayers : 0 humans, 4 bots (8 max)\n"
        )
        self.assertIn("Nobody is playing", players.format_players(info, plist))


class SanitizeTests(unittest.TestCase):
    def test_semicolon_cannot_chain_a_second_command(self):
        # The Source console splits on ';' - this is command injection.
        self.assertNotIn(";", sanitize.console_arg("hello; quit"))

    def test_quote_cannot_escape_the_argument(self):
        self.assertNotIn('"', sanitize.console_arg('bye" ; quit ; say "'))

    def test_newlines_and_control_chars_stripped(self):
        cleaned = sanitize.console_arg("a\nb\r\x00c\x1b")
        self.assertNotIn("\n", cleaned)
        self.assertNotIn("\x00", cleaned)
        self.assertNotIn("\x1b", cleaned)

    def test_length_capped(self):
        self.assertLessEqual(len(sanitize.console_arg("x" * 5000)), 200)

    def test_map_names_validated(self):
        self.assertTrue(sanitize.valid_map("c1m1_hotel"))
        self.assertTrue(sanitize.valid_map("deadcity_m1"))
        self.assertFalse(sanitize.valid_map("c1m1; quit"))
        self.assertFalse(sanitize.valid_map("../../etc/passwd"))
        self.assertFalse(sanitize.valid_map(""))

    def test_display_strips_invisibles_and_caps(self):
        self.assertEqual(sanitize.display("ev​il"), "evil")
        self.assertLessEqual(len(sanitize.display("n" * 500)), 48)
        self.assertEqual(sanitize.display("   "), "(unnamed)")

    def test_redact_hides_rcon_password_only(self):
        info = (
            "Server name : Test\n"
            "Password    : joinpw\n"
            "RCON        : sup3rs3cret\n"
            "Mode / map  : coop / c1m1_hotel\n"
        )
        out = sanitize.redact_info(info)
        self.assertNotIn("sup3rs3cret", out)
        # The join password is the point of /info, so it stays.
        self.assertIn("joinpw", out)

    def test_rcon_denylist(self):
        self.assertTrue(sanitize.RCON_REFUSED.match("rcon_password newpass"))
        self.assertTrue(sanitize.RCON_REFUSED.match("sv_rcon_banpenalty 0"))
        self.assertIsNone(sanitize.RCON_REFUSED.match("sv_cheats 1"))
        self.assertTrue(sanitize.RCON_CONFIRM.match("quit"))


class TelegramFormattingTests(unittest.TestCase):
    def test_escapes_html_metacharacters(self):
        self.assertEqual(telegram.esc("<b>&</b>"), "&lt;b&gt;&amp;&lt;/b&gt;")

    def test_player_name_cannot_inject_markup(self):
        # A name of "</pre><a href=...>" must not survive as markup.
        evil = '</pre><a href="http://evil">click</a>'
        self.assertNotIn("<a href", telegram.esc(evil))

    def test_chunk_keeps_short_text_whole(self):
        self.assertEqual(telegram.chunk("hello"), ["hello"])

    def test_chunk_splits_on_line_boundaries(self):
        text = "\n".join(f"line {i}" for i in range(2000))
        pieces = telegram.chunk(text, limit=500)
        self.assertGreater(len(pieces), 1)
        self.assertTrue(all(len(p) <= 500 for p in pieces))
        self.assertEqual("".join(pieces), text)

    def test_chunk_hard_splits_a_monster_line(self):
        pieces = telegram.chunk("x" * 9000, limit=1000)
        self.assertTrue(all(len(p) <= 1000 for p in pieces))
        self.assertEqual("".join(pieces), "x" * 9000)


if __name__ == "__main__":
    unittest.main()
