"""Panel configuration, read from the env files the installers write."""

import os

from bot.config import load_env_file

from . import serverconf

WEB_ENV = os.environ.get("L4D2_WEB_ENV", "/etc/l4d2-web.env")
SERVER_ENV = os.environ.get("L4D2_ENV", "/etc/l4d2.env")
L4D2_BIN = os.environ.get("L4D2_BIN", "/usr/local/bin/l4d2")
LOCK_FILE = "/run/l4d2.lock"  # the same file the l4d2 command locks


class ConfigError(Exception):
    pass


class WebConfig:
    def __init__(self, bind, port, password_hash, server_env_path=SERVER_ENV,
                 rcon_host="127.0.0.1", rcon_port=None, l4d2_bin=L4D2_BIN,
                 secure_cookie=False, hostnames=(), lock_path=LOCK_FILE):
        self.bind = bind
        self.port = port
        self.password_hash = password_hash
        self.server_env_path = server_env_path
        self.rcon_host = rcon_host
        self._rcon_port = rcon_port
        self.l4d2_bin = l4d2_bin
        self.secure_cookie = secure_cookie
        self.hostnames = {h.lower() for h in hostnames if h}
        self.lock_path = lock_path

    @property
    def server_env(self):
        """Re-read every time: the l4d2 CLI rewrites this file (map, mode, slots)."""
        return load_env_file(self.server_env_path)

    @property
    def server_dir(self):
        return self.server_env.get("L4D2_DIR", "/home/l4d2/l4d2server")

    @property
    def home(self):
        return self.server_env.get("L4D2_HOME", "/home/l4d2")

    @property
    def cfg_path(self):
        return os.path.join(self.server_dir, "left4dead2", "cfg", "server.cfg")

    @property
    def log_path(self):
        return os.path.join(self.server_dir, "left4dead2", "console.log")

    @property
    def addons_dir(self):
        return os.path.join(self.server_dir, "left4dead2", "addons")

    @property
    def index_path(self):
        return os.path.join(self.home, "maps.json")

    @property
    def info_path(self):
        return os.path.join(self.home, "SERVER-INFO.txt")

    @property
    def rcon_port(self):
        if self._rcon_port:
            return self._rcon_port
        try:
            return int(self.server_env.get("L4D2_PORT", "27015"))
        except ValueError:
            return 27015

    def rcon_password(self):
        """Re-read every time, so config edits apply without a panel restart."""
        return (serverconf.read_cvar(serverconf.read_text(self.cfg_path), "rcon_password") or "").strip()

    @classmethod
    def load(cls, path=WEB_ENV, server_env_path=SERVER_ENV):
        env = load_env_file(path)
        if not env:
            raise ConfigError(f"{path} not found or empty. Run web-panel-setup.sh.")
        password_hash = env.get("L4D2_WEB_PASSWORD_HASH", "").strip()
        if not password_hash:
            raise ConfigError(f"L4D2_WEB_PASSWORD_HASH missing from {path}. Run web-panel-setup.sh.")
        if not load_env_file(server_env_path):
            raise ConfigError(f"{server_env_path} not found. Run l4d2-setup.sh first.")
        try:
            port = int(env.get("L4D2_WEB_PORT", "8080"))
            rcon_port = int(env["L4D2_RCON_PORT"]) if env.get("L4D2_RCON_PORT") else None
        except ValueError as e:
            raise ConfigError(f"Bad port number in {path}: {e}")
        return cls(
            bind=env.get("L4D2_WEB_BIND", "127.0.0.1"),
            port=port,
            password_hash=password_hash,
            server_env_path=server_env_path,
            rcon_host=env.get("L4D2_RCON_HOST", "127.0.0.1"),
            rcon_port=rcon_port,
            secure_cookie=env.get("L4D2_WEB_SECURE_COOKIE", "") == "1",
            hostnames=env.get("L4D2_WEB_HOSTNAMES", "").replace(",", " ").split(),
        )
