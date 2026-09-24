"""Shared plumbing handed to every command handler."""

import secrets
import time

from . import config as config_mod
from . import jobs, rcon

CONFIRM_TTL = 120  # seconds a confirmation button stays live


class Pending:
    def __init__(self, user_id, label, fn):
        self.user_id = user_id
        self.label = label
        self.fn = fn
        self.created = time.time()

    def expired(self):
        return time.time() - self.created > CONFIRM_TTL


class Context:
    def __init__(self, bot, cfg, runner, log=print):
        self.bot = bot
        self.cfg = cfg
        self.runner = runner
        self.log = log
        self.pending = {}

    # ------------------------------------------------------------- messaging
    def send(self, text, preformatted=False, buttons=None):
        return self.bot.send(self.cfg.chat_id, text, preformatted=preformatted, buttons=buttons)

    # ------------------------------------------------------------------- CLI
    def cli(self, *args, timeout=120, on_output=None):
        """Run the existing l4d2 management CLI and capture its output.

        Always pass explicit arguments: with no arguments the CLI falls back to
        `help` on a non-tty, and its interactive prompts self-answer "no".
        """
        return jobs.run([config_mod.L4D2_BIN, *args], timeout=timeout, on_output=on_output)

    def cli_send(self, *args, timeout=120):
        code, text = self.cli(*args, timeout=timeout)
        self.send(text or f"(no output, exit {code})", preformatted=True)
        return code

    # ------------------------------------------------------------------ RCON
    def rcon(self, command):
        """Run a console command and return its output. Raises RconError."""
        return rcon.execute(
            command,
            self.cfg.rcon_password(),
            host=self.cfg.rcon_host,
            port=self.cfg.rcon_port,
        )

    def try_rcon(self, command):
        """Best-effort variant: returns (ok, text_or_reason)."""
        try:
            return True, self.rcon(command)
        except rcon.RconError as e:
            return False, str(e)

    # ---------------------------------------------------------- confirmation
    def confirm(self, user_id, label, fn, warning=""):
        """Ask for a button press before doing something disruptive."""
        token = secrets.token_hex(4)
        self.pending[token] = Pending(user_id, label, fn)
        self._reap()
        text = f"{label}?"
        if warning:
            text += f"\n\n{warning}"
        self.send(text, buttons=[[
            {"text": "Yes, do it", "callback_data": f"ok:{token}"},
            {"text": "Cancel", "callback_data": f"no:{token}"},
        ]])

    def resolve(self, data, user_id):
        """Handle a button press. Returns a short note for answerCallbackQuery."""
        action, _, token = data.partition(":")
        entry = self.pending.get(token)
        if not entry or entry.expired():
            self.pending.pop(token, None)
            return "That confirmation expired."
        # Only the person who asked may confirm, so one press can't be hijacked.
        if entry.user_id != user_id:
            return "Only the person who ran the command can confirm it."

        self.pending.pop(token, None)
        if action != "ok":
            return "Cancelled."
        entry.fn()
        return "Running..."

    def _reap(self):
        for token in [t for t, p in self.pending.items() if p.expired()]:
            self.pending.pop(token, None)
