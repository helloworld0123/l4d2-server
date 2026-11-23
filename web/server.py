"""Entry point: python3 -m web.server

A small JSON API plus three static files, on the standard library's HTTP
server. It is meant to be reached over Tailscale, a LAN or an SSH tunnel -
never bound to a public address: there is no TLS here.
"""

import http.cookies
import ipaddress
import json
import os
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from bot import rcon

from . import auth, config as config_mod, panel as panel_mod

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
STATIC = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/app.css": ("app.css", "text/css; charset=utf-8"),
    "/favicon.svg": ("favicon.svg", "image/svg+xml"),
}
COOKIE = "l4d2_session"
CSRF_HEADER_VALUE = "l4d2-panel"
MAX_BODY = 256 * 1024
SECURITY_HEADERS = {
    "Content-Security-Policy": "default-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
}


def log(message):
    print(f"[{time.strftime('%H:%M:%S')}] {message}", flush=True)


class App:
    def __init__(self, cfg, log=log):
        self.cfg = cfg
        self.log = log
        self.panel = panel_mod.Panel(cfg, log=log)
        self.sessions = auth.Sessions()
        self.throttle = auth.Throttle()
        # Each check costs ~0.3s of CPU; strangers must not be able to queue up hundreds.
        self.hashing = threading.BoundedSemaphore(2)
        self.static = {}
        for route, (name, content_type) in STATIC.items():
            with open(os.path.join(STATIC_DIR, name), "rb") as fh:
                self.static[route] = (fh.read(), content_type)


def _first_word(command):
    """For the audit log: the command name only, never its arguments (passwords)."""
    return (command or "").strip().split(" ")[0][:40]


# Route tables: path -> function(panel, body_or_query) returning a JSON-able dict.
GET_ROUTES = {
    "/api/overview": lambda p, q: p.overview(),
    "/api/maps": lambda p, q: p.maps(),
    "/api/settings": lambda p, q: p.settings(),
    "/api/config": lambda p, q: p.raw_config(),
    "/api/job": lambda p, q: p.job(),
    "/api/logs": lambda p, q: p.logs(_int(q.get("lines", ["100"])[0], 100)),
}
POST_ROUTES = {
    "/api/control": (lambda p, b: p.control(b.get("action")), lambda b: b.get("action")),
    "/api/map": (lambda p, b: p.change_map(b.get("name"), bool(b.get("default"))), lambda b: b.get("name")),
    "/api/kick": (lambda p, b: p.kick(b.get("userid"), b.get("reason") or ""), lambda b: b.get("userid")),
    "/api/say": (lambda p, b: p.say(b.get("text")), lambda b: ""),
    "/api/rcon": (lambda p, b: p.console(b.get("command")), lambda b: _first_word(b.get("command"))),
    "/api/maps/add": (lambda p, b: p.add_maps(b.get("links")), lambda b: b.get("links")),
    "/api/maps/remove": (lambda p, b: p.remove_map(b.get("key")), lambda b: b.get("key")),
    "/api/maps/update": (lambda p, b: p.update_maps(), lambda b: ""),
    "/api/slots": (lambda p, b: p.set_slots(b.get("slots")), lambda b: b.get("slots")),
    "/api/settings": (lambda p, b: p.save_settings(b), lambda b: ", ".join(sorted(b))),
    "/api/config": (lambda p, b: p.save_raw_config(b.get("text"), bool(b.get("apply"))), lambda b: ""),
}


def _int(value, default):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


class Handler(BaseHTTPRequestHandler):
    server_version = "l4d2-panel"
    sys_version = ""
    timeout = 30  # a stalled client must not pin a thread forever

    @property
    def app(self):
        return self.server.app

    def log_message(self, fmt, *args):
        pass  # the dashboard polls every few seconds; only actions are logged

    # -------------------------------------------------------------- responses
    def _send(self, status, body, content_type, extra=()):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for name, value in SECURITY_HEADERS.items():
            self.send_header(name, value)
        for name, value in extra:
            self.send_header(name, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, status, payload, extra=()):
        self._send(status, json.dumps(payload).encode("utf-8"), "application/json", extra)

    def _error(self, status, message, extra=()):
        self._json(status, {"error": message}, extra)

    # ------------------------------------------------------------------- auth
    def _token(self):
        try:
            cookie = http.cookies.SimpleCookie(self.headers.get("Cookie", ""))
        except http.cookies.CookieError:
            return None
        morsel = cookie.get(COOKIE)
        return morsel.value if morsel else None

    def _authed(self):
        return self.app.sessions.valid(self._token())

    def _cookie(self, token, max_age):
        parts = [f"{COOKIE}={token}", "Path=/", "HttpOnly", "SameSite=Strict", f"Max-Age={max_age}"]
        if self.app.cfg.secure_cookie:
            parts.append("Secure")
        return ("Set-Cookie", "; ".join(parts))

    def _known_host(self):
        """Refuse names we don't answer to.

        DNS rebinding points an attacker's hostname at this address, so the
        victim's browser treats the panel as the attacker's own site. An IP
        literal can't be rebound; names must be ones the installer listed.
        """
        host = self.headers.get("Host", "")
        name = host.rsplit(":", 1)[0] if not host.endswith("]") else host
        name = name.strip("[]").lower()
        if name == "localhost" or name in self.app.cfg.hostnames:
            return True
        try:
            ipaddress.ip_address(name)
            return True
        except ValueError:
            return False

    def _same_origin(self):
        """Browsers send Origin on cross-site POSTs; ours must match the Host we were asked for."""
        origin = self.headers.get("Origin")
        if not origin:
            return True
        return urllib.parse.urlsplit(origin).netloc == self.headers.get("Host", "")

    def _body(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = -1
        if length < 0 or length > MAX_BODY:
            raise panel_mod.PanelError("Request too large.", 413)
        try:
            data = json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, UnicodeDecodeError):
            raise panel_mod.PanelError("Request body must be JSON.")
        if not isinstance(data, dict):
            raise panel_mod.PanelError("Request body must be a JSON object.")
        return data

    # ----------------------------------------------------------------- routes
    def do_GET(self):
        url = urllib.parse.urlsplit(self.path)
        path = url.path
        if not self._known_host():
            return self._error(421, "Open the panel by its IP address, or add this name to "
                                    "L4D2_WEB_HOSTNAMES in /etc/l4d2-web.env.")
        if path in self.app.static:
            body, content_type = self.app.static[path]
            return self._send(200, body, content_type)
        if path == "/healthz":
            return self._send(200, b"ok\n", "text/plain")
        if path == "/api/session":
            return self._json(200, {"authenticated": self._authed()})
        route = GET_ROUTES.get(path)
        if not route:
            return self._error(404, "Not found.")
        if not self._authed():
            return self._error(401, "Sign in first.")
        self._run(lambda: route(self.app.panel, urllib.parse.parse_qs(url.query)))

    do_HEAD = do_GET

    def do_POST(self):
        path = urllib.parse.urlsplit(self.path).path
        # A custom header can't be sent cross-site without a CORS preflight,
        # which we never answer - so this, with SameSite cookies, stops CSRF.
        if (self.headers.get("X-Requested-With") != CSRF_HEADER_VALUE
                or not self._same_origin() or not self._known_host()):
            return self._error(403, "Cross-site request refused.")
        if path == "/api/login":
            return self._login()
        if path == "/api/logout":
            self.app.sessions.destroy(self._token())
            return self._json(200, {"ok": True}, [self._cookie("", 0)])
        route = POST_ROUTES.get(path)
        if not route:
            return self._error(404, "Not found.")
        if not self._authed():
            return self._error(401, "Sign in first.")
        action, describe = route

        def go():
            body = self._body()
            detail = "".join(c if c.isprintable() else " " for c in str(describe(body) or ""))[:160]
            self.app.log(f"{self.client_address[0]} {path} {detail}".rstrip())
            return action(self.app.panel, body)

        self._run(go)

    def _run(self, fn):
        try:
            result = fn()
        except panel_mod.PanelError as e:
            return self._error(e.status, str(e))
        except rcon.RconError as e:
            return self._error(502, str(e))
        except Exception as e:  # one bad request must not kill the panel
            self.app.log(f"{self.path} failed: {e!r}")
            return self._error(500, "Something went wrong. Check: journalctl -u l4d2-web")
        self._json(200, result if isinstance(result, dict) else {"ok": True})

    def _login(self):
        address = self.client_address[0]
        try:
            password = self._body().get("password")
        except panel_mod.PanelError as e:
            return self._error(e.status, str(e))
        wait = self.app.throttle.attempt(address)
        if wait:
            return self._error(429, f"Too many wrong passwords. Try again in {wait}s.",
                               [("Retry-After", str(wait))])
        if not self.app.hashing.acquire(timeout=5):
            return self._error(429, "Busy checking other sign-ins. Try again in a moment.",
                               [("Retry-After", "5")])
        try:
            correct = isinstance(password, str) and auth.verify_password(password, self.app.cfg.password_hash)
        finally:
            self.app.hashing.release()
        if not correct:
            self.app.log(f"{address} failed login")
            return self._error(401, "Wrong password.")
        self.app.throttle.succeeded(address)
        self.app.log(f"{address} signed in")
        token = self.app.sessions.create()
        self._json(200, {"ok": True}, [self._cookie(token, self.app.sessions.lifetime)])


def make_server(cfg, log=log):
    httpd = ThreadingHTTPServer((cfg.bind, cfg.port), Handler)
    httpd.daemon_threads = True
    httpd.app = App(cfg, log=log)
    return httpd


def main():
    try:
        cfg = config_mod.WebConfig.load()
    except config_mod.ConfigError as e:
        log(str(e))
        return 1
    try:
        httpd = make_server(cfg)
    except OSError as e:
        # Typically the Tailscale address isn't up yet at boot; systemd retries.
        log(f"can't listen on {cfg.bind}:{cfg.port}: {e}")
        return 1
    if not cfg.rcon_password():
        log("warning: rcon_password is empty in server.cfg - players and live settings will not work")
    log(f"panel listening on http://{cfg.bind}:{cfg.port}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
