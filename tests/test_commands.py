import unittest

from bot import commands, config as config_mod, context, main as main_mod

FORBIDDEN = {"logs", "console", "config", "menu"}


class FakeBot:
    def __init__(self):
        self.sent = []
        self.edits = []
        self.answered = []

    def send(self, chat_id, text, preformatted=False, buttons=None, reply_to=None):
        self.sent.append({"text": text, "buttons": buttons})
        return {"message_id": len(self.sent)}

    def edit(self, chat_id, message_id, text, preformatted=False, buttons=None):
        self.edits.append(text)
        return {}

    def answer_callback(self, callback_id, text=None):
        self.answered.append(text)


class FakeRunner:
    """Runs jobs inline so tests stay deterministic."""
    def submit(self, name, who, fn):
        fn()
        return True, None


class StubContext(context.Context):
    def __init__(self, bot, players_online=0, rcon_ok=True):
        cfg = config_mod.Config(
            token="t", chat_id=-1001, server_env={"L4D2_DIR": "/tmp/srv", "L4D2_MAP": "c1m1_hotel"},
            rcon_host="127.0.0.1", rcon_port=27015,
        )
        super().__init__(bot, cfg, FakeRunner(), log=lambda *_: None)
        self.calls = []
        self.rcon_calls = []
        self.players_online = players_online
        self.rcon_ok = rcon_ok

    def cli(self, *args, timeout=120, on_output=None):
        self.calls.append(list(args))
        return 0, "ok"

    def try_rcon(self, command):
        self.rcon_calls.append(command)
        if not self.rcon_ok:
            return False, "RCON unavailable"
        if command == "status":
            rows = "".join(
                f'#  {i + 2} 1 "P{i}" STEAM_1:0:{i} 01:00 40 0 active\n'
                for i in range(self.players_online)
            )
            return True, f"map     : c1m1_hotel at: 0 x\n{rows}"
        return True, ""


def message(text, user_id=42, chat_id=-1001, is_bot=False):
    import time
    return {
        "text": text,
        "date": int(time.time()),
        "chat": {"id": chat_id},
        "from": {"id": user_id, "is_bot": is_bot, "username": "tester"},
    }


class CliInvariantTests(unittest.TestCase):
    """These pin behaviour the bot's correctness depends on."""

    def _exercise_all_cli_commands(self):
        bot = FakeBot()
        ctx = StubContext(bot, players_online=0)
        samples = {
            "status": [], "maps": [], "info": [], "share": [],
            "start": [], "stop": [], "restart": [],
            "changemap": ["c2m1_highway"], "update": [],
            "addmap": ["12345"], "removemap": ["12345"], "updatemaps": [],
        }
        for name, args in samples.items():
            commands.HANDLERS[name](ctx, args, message(f"/{name}"))
        return ctx

    def test_never_invokes_a_cli_command_that_would_hang(self):
        # l4d2 logs/console exec into tail -f and tmux attach; config runs $EDITOR.
        ctx = self._exercise_all_cli_commands()
        for argv in ctx.calls:
            self.assertNotIn(argv[0], FORBIDDEN, f"{argv} would never return")

    def test_map_mutations_always_pass_an_explicit_restart_flag(self):
        # The CLI only skips its prompt because ask_yes() is tty-guarded.
        # Never depend on that accident.
        ctx = self._exercise_all_cli_commands()
        for argv in ctx.calls:
            if argv[0] in ("addmap", "removemap", "updatemaps", "changemap"):
                self.assertIn("--no-restart", argv, f"{argv} lacks an explicit flag")


class ConfirmationTests(unittest.TestCase):
    def test_no_confirmation_when_nobody_is_playing(self):
        bot = FakeBot()
        ctx = StubContext(bot, players_online=0)
        commands.cmd_stop(ctx, [], message("/stop"))
        self.assertIn(["stop"], ctx.calls)
        self.assertFalse(any(m["buttons"] for m in bot.sent))

    def test_confirmation_required_when_players_are_connected(self):
        bot = FakeBot()
        ctx = StubContext(bot, players_online=3)
        commands.cmd_stop(ctx, [], message("/stop"))
        self.assertNotIn(["stop"], ctx.calls, "stopped without asking")
        self.assertTrue(any(m["buttons"] for m in bot.sent))
        self.assertIn("3 player(s) connected", bot.sent[-1]["text"])

    def test_confirmation_runs_the_action_when_accepted(self):
        bot = FakeBot()
        ctx = StubContext(bot, players_online=2)
        commands.cmd_stop(ctx, [], message("/stop"))
        token = next(iter(ctx.pending))
        ctx.resolve(f"ok:{token}", 42)
        self.assertIn(["stop"], ctx.calls)

    def test_cancel_does_not_run_the_action(self):
        bot = FakeBot()
        ctx = StubContext(bot, players_online=2)
        commands.cmd_stop(ctx, [], message("/stop"))
        token = next(iter(ctx.pending))
        self.assertEqual(ctx.resolve(f"no:{token}", 42), "Cancelled.")
        self.assertNotIn(["stop"], ctx.calls)

    def test_another_user_cannot_confirm(self):
        bot = FakeBot()
        ctx = StubContext(bot, players_online=2)
        commands.cmd_stop(ctx, [], message("/stop", user_id=42))
        token = next(iter(ctx.pending))
        ctx.resolve(f"ok:{token}", 999)
        self.assertNotIn(["stop"], ctx.calls)

    def test_confirmation_asked_when_rcon_cannot_answer(self):
        bot = FakeBot()
        ctx = StubContext(bot, rcon_ok=False)
        commands.cmd_stop(ctx, [], message("/stop"))
        self.assertNotIn(["stop"], ctx.calls, "assumed empty server when RCON was down")


class LiveAdminTests(unittest.TestCase):
    def test_say_is_quoted_and_sanitised(self):
        ctx = StubContext(FakeBot())
        commands.cmd_say(ctx, ["hello;", "quit"], message("/say"))
        sent = ctx.rcon_calls[-1]
        self.assertTrue(sent.startswith('say "') and sent.endswith('"'))
        self.assertNotIn(";", sent)

    def test_changemap_rejects_an_injected_name(self):
        ctx = StubContext(FakeBot())
        commands.cmd_changemap(ctx, ["c1m1;quit"], message("/changemap"))
        self.assertEqual(ctx.rcon_calls, [])

    def test_kick_uses_userid_not_name(self):
        bot = FakeBot()
        ctx = StubContext(bot, players_online=1)
        commands.cmd_kick(ctx, ["P0"], message("/kick"))
        token = next(iter(ctx.pending))
        ctx.resolve(f"ok:{token}", 42)
        self.assertIn("kickid 2", ctx.rcon_calls)

    def test_rcon_refuses_to_change_its_own_credentials(self):
        ctx = StubContext(FakeBot())
        commands.cmd_rcon(ctx, ["rcon_password", "hunter2"], message("/rcon"))
        self.assertEqual(ctx.rcon_calls, [])

    def test_rcon_passthrough_is_otherwise_unrestricted(self):
        ctx = StubContext(FakeBot())
        commands.cmd_rcon(ctx, ["z_spawn", "tank"], message("/rcon"))
        self.assertIn("z_spawn tank", ctx.rcon_calls)

    def test_info_never_leaks_the_rcon_password(self):
        class Leaky(StubContext):
            def cli(self, *args, timeout=120, on_output=None):
                return 0, "Password    : joinpw\nRCON        : topsecret\n"
        bot = FakeBot()
        ctx = Leaky(bot)
        commands.cmd_info(ctx, [], message("/info"))
        self.assertNotIn("topsecret", bot.sent[-1]["text"])


class RouterTests(unittest.TestCase):
    def _router(self, ctx=None, bot=None):
        bot = bot or FakeBot()
        ctx = ctx or StubContext(bot)
        return main_mod.Router(bot, ctx.cfg, ctx, "myl4d2bot", {}), ctx, bot

    def test_strips_the_bot_suffix_groups_add(self):
        router, _, _ = self._router()
        self.assertEqual(router.parse("/status@myl4d2bot"), ("status", []))
        self.assertEqual(router.parse("/changemap@myl4d2bot c2m1"), ("changemap", ["c2m1"]))

    def test_ignores_commands_aimed_at_another_bot(self):
        router, _, _ = self._router()
        self.assertEqual(router.parse("/status@someotherbot"), (None, []))

    def test_ignores_non_commands(self):
        router, _, _ = self._router()
        self.assertEqual(router.parse("just chatting"), (None, []))

    def test_ignores_other_chats(self):
        router, ctx, _ = self._router()
        router.on_message(message("/status", chat_id=-9999))
        self.assertEqual(ctx.calls, [])

    def test_ignores_messages_from_bots(self):
        # A leaked token lets an attacker post as the bot; we must not obey.
        router, ctx, _ = self._router()
        router.on_message(message("/stop", is_bot=True))
        self.assertEqual(ctx.calls, [])

    def test_ignores_stale_messages(self):
        router, ctx, _ = self._router()
        stale = message("/status")
        stale["date"] = 0
        router.on_message(stale)
        self.assertEqual(ctx.calls, [])

    def test_rate_limit_eventually_drops_commands(self):
        router, ctx, _ = self._router()
        for _ in range(40):
            router.on_message(message("/status"))
        self.assertLess(len(ctx.calls), 40, "rate limiter never engaged")

    def test_every_handler_has_a_description(self):
        self.assertEqual(set(commands.HANDLERS), set(commands.DESCRIPTIONS))

    def test_every_handler_appears_in_a_help_group(self):
        grouped = {n for _, names in commands.GROUPS for n in names}
        self.assertEqual(grouped | {"help", "id"}, set(commands.HANDLERS))


if __name__ == "__main__":
    unittest.main()
