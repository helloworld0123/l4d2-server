"""Entry point: python3 -m bot.main"""

import json
import os
import sys
import time

from . import commands, config as config_mod, context, jobs, telegram

STATE_DIR = "/var/lib/l4d2-telegram"
STATE_FILE = os.path.join(STATE_DIR, "state.json")
STALE_AFTER = 120  # ignore commands queued while the bot was down


def log(message):
    print(f"[{time.strftime('%H:%M:%S')}] {message}", flush=True)


def load_state():
    try:
        with open(STATE_FILE) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def save_state(state):
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(state, fh)
        os.replace(tmp, STATE_FILE)
    except OSError as e:
        log(f"could not save state: {e}")


class Router:
    def __init__(self, bot, cfg, ctx, username, state):
        self.bot = bot
        self.cfg = cfg
        self.ctx = ctx
        self.username = (username or "").lower()
        self.state = state
        self.buckets = {}

    def allowed(self, user_id, limit=10, per=60.0):
        """Token bucket per user, so one person can't flood the group."""
        now = time.time()
        tokens, stamp = self.buckets.get(user_id, (limit, now))
        tokens = min(limit, tokens + (now - stamp) * (limit / per))
        if tokens < 1:
            self.buckets[user_id] = (tokens, now)
            return False
        self.buckets[user_id] = (tokens - 1, now)
        return True

    def parse(self, text):
        """Split '/cmd@BotName arg arg' into (cmd, args). Groups add the suffix."""
        parts = (text or "").strip().split()
        if not parts or not parts[0].startswith("/"):
            return None, []
        name = parts[0][1:].lower()
        if "@" in name:
            name, _, target = name.partition("@")
            if target != self.username:
                return None, []  # addressed to a different bot in the group
        return name, parts[1:]

    def handle(self, update):
        if "callback_query" in update:
            return self.on_callback(update["callback_query"])
        message = update.get("message") or update.get("edited_message")
        if message:
            return self.on_message(message)

    def on_message(self, message):
        chat = message.get("chat", {})
        sender = message.get("from", {})

        # A leaked token lets someone post AS the bot; never obey ourselves.
        if sender.get("is_bot"):
            return
        if chat.get("id") != self.cfg.chat_id:
            log(f"ignoring message from chat {chat.get('id')}")
            return
        if time.time() - message.get("date", 0) > STALE_AFTER:
            return

        name, args = self.parse(message.get("text", ""))
        if not name:
            return
        handler = commands.HANDLERS.get(name)
        if not handler:
            return
        if not self.allowed(sender.get("id")):
            return  # silently drop; replying would amplify the flood

        log(f"{sender.get('username') or sender.get('id')}: /{name} {' '.join(args)}")
        try:
            handler(self.ctx, args, message)
        except Exception as e:
            log(f"/{name} failed: {e!r}")
            self.ctx.send(f"/{name} failed: {e}")

    def on_callback(self, callback):
        sender = callback.get("from", {})
        # Answer first: an unanswered callback leaves a spinner on the button.
        try:
            note = self.ctx.resolve(callback.get("data", ""), sender.get("id"))
        except Exception as e:
            log(f"callback failed: {e!r}")
            note = "Something went wrong."
        self.bot.answer_callback(callback["id"], note)
        message = callback.get("message") or {}
        if message.get("message_id"):
            # Drop the buttons so they can't be tapped twice.
            self.bot.edit(self.cfg.chat_id, message["message_id"],
                          (message.get("text") or "").split("\n")[0] + f"\n\n[{note}]")


def main():
    try:
        cfg = config_mod.Config.load()
    except config_mod.ConfigError as e:
        log(str(e))
        return 1

    bot = telegram.Bot(cfg.token)
    try:
        me = bot.get_me()
        bot.delete_webhook()  # getUpdates returns 409 forever if one is set
    except telegram.TelegramError as e:
        log(f"Telegram rejected the token: {e}")
        return 1
    log(f"connected as @{me.get('username')}")

    runner = jobs.JobRunner(log=log).start()
    ctx = context.Context(bot, cfg, runner, log=log)
    state = load_state()
    router = Router(bot, cfg, ctx, me.get("username"), state)

    offset = state.get("offset")
    if offset is None:
        # First run: skip anything queued while we were not listening.
        try:
            backlog = bot.get_updates(offset=-1, timeout=0)
            offset = backlog[-1]["update_id"] + 1 if backlog else None
        except telegram.TelegramError:
            offset = None

    try:
        bot.set_commands([(n, commands.DESCRIPTIONS[n]) for n in commands.HANDLERS])
    except telegram.TelegramError as e:
        log(f"setMyCommands failed (not fatal): {e}")

    if not cfg.rcon_password():
        log("warning: rcon_password is empty in server.cfg - live commands will not work")

    try:
        bot.send(cfg.chat_id, "Server bot is online. Try /help")
    except telegram.TelegramError as e:
        log(f"could not post to the group: {e}")
        return 1

    def handle(update):
        # Persist the offset before handling: a crash mid-/update should not
        # replay /update on restart.
        state["offset"] = update["update_id"] + 1
        save_state(state)
        router.handle(update)

    telegram.poll(bot, handle, offset=offset, log=log)
    return 0


if __name__ == "__main__":
    sys.exit(main())
