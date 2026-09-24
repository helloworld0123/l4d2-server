"""Configuration, read from the two env files the installers write."""

import os

from . import rcon

BOT_ENV = "/etc/l4d2-telegram.env"
SERVER_ENV = "/etc/l4d2.env"
L4D2_BIN = "/usr/local/bin/l4d2"


class ConfigError(Exception):
    pass


def load_env_file(path):
    """Parse a shell-style KEY=VALUE file. Ignores comments and blank lines."""
    values = {}
    try:
        with open(path, "r", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                values[key.strip()] = value.strip().strip('"').strip("'")
    except OSError:
        pass
    return values


class Config:
    def __init__(self, token, chat_id, server_env, rcon_host, rcon_port):
        self.token = token
        self.chat_id = chat_id
        self.server_env = server_env
        self.rcon_host = rcon_host
        self.rcon_port = rcon_port

    @property
    def server_dir(self):
        return self.server_env.get("L4D2_DIR", "/home/l4d2/l4d2server")

    @property
    def cfg_path(self):
        return os.path.join(self.server_dir, "left4dead2", "cfg", "server.cfg")

    def rcon_password(self):
        """Re-read every time, so `l4d2 config` edits apply without a bot restart."""
        return rcon.read_password(self.cfg_path)

    @classmethod
    def load(cls):
        env = load_env_file(BOT_ENV)
        token = env.get("TELEGRAM_BOT_TOKEN", "").strip()
        raw_chat = env.get("TELEGRAM_CHAT_ID", "").strip()

        if not token:
            raise ConfigError(f"TELEGRAM_BOT_TOKEN missing from {BOT_ENV}. Run telegram-bot-setup.sh.")
        try:
            chat_id = int(raw_chat)
        except ValueError:
            raise ConfigError(f"TELEGRAM_CHAT_ID in {BOT_ENV} is not a number: {raw_chat!r}")

        server_env = load_env_file(SERVER_ENV)
        if not server_env:
            raise ConfigError(f"{SERVER_ENV} not found. Run l4d2-setup.sh first.")

        return cls(
            token=token,
            chat_id=chat_id,
            server_env=server_env,
            rcon_host=env.get("L4D2_RCON_HOST", "127.0.0.1"),
            rcon_port=int(env.get("L4D2_RCON_PORT", server_env.get("L4D2_PORT", "27015"))),
        )
