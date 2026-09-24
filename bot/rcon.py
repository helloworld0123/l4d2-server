"""Minimal Source RCON client, standard library only.

Packet layout (all integers little-endian int32):

    size | id | type | body (ASCII, NUL-terminated) | NUL

`size` counts everything after itself, i.e. 4 (id) + 4 (type) + len(body) + 2.
"""

import re
import socket
import struct
import time

SERVERDATA_RESPONSE_VALUE = 0
SERVERDATA_EXECCOMMAND = 2
SERVERDATA_AUTH_RESPONSE = 2
SERVERDATA_AUTH = 3

_AUTH_ID = 0x1234
_CMD_ID = 0x2345

# How long to keep reading after the first response packet, to catch
# multi-packet replies (a full `status` exceeds one packet).
_DRAIN_TIMEOUT = 0.35

# Source bans an address after sv_rcon_maxfailures bad attempts
# (sv_rcon_banpenalty minutes), so never hammer a rejected password.
AUTH_COOLDOWN = 600
_auth_blocked_until = 0.0


class RconError(Exception):
    """RCON could not be reached or the exchange went wrong."""


class RconAuthError(RconError):
    """The RCON password was rejected (or is not set on the server)."""


def encode_packet(packet_id, packet_type, body):
    """Serialize one RCON packet. Exposed for testing."""
    payload = struct.pack("<ii", packet_id, packet_type) + body.encode("utf-8", "replace") + b"\x00\x00"
    return struct.pack("<i", len(payload)) + payload


def decode_packet(raw):
    """Inverse of encode_packet, taking the bytes *after* the size field."""
    packet_id, packet_type = struct.unpack("<ii", raw[:8])
    body = raw[8:-2].decode("utf-8", "replace")
    return packet_id, packet_type, body


class RconClient:
    def __init__(self, password, host="127.0.0.1", port=27015, timeout=8.0):
        if not password:
            raise RconAuthError(
                "No RCON password is set. The bot needs one - re-run telegram-bot-setup.sh."
            )
        self.password = password
        self.host = host
        self.port = port
        self.timeout = timeout
        self.sock = None

    # ------------------------------------------------------------- low level
    def _recv_exactly(self, n):
        """recv() is a stream, not messages - loop until we have all n bytes."""
        buf = b""
        while len(buf) < n:
            chunk = self.sock.recv(n - len(buf))
            if not chunk:
                raise RconError("Server closed the RCON connection.")
            buf += chunk
        return buf

    def _read_packet(self):
        size = struct.unpack("<i", self._recv_exactly(4))[0]
        if not 10 <= size <= 8192:
            raise RconError(f"Bogus RCON packet size: {size}")
        return decode_packet(self._recv_exactly(size))

    def _send(self, packet_id, packet_type, body):
        self.sock.sendall(encode_packet(packet_id, packet_type, body))

    # ------------------------------------------------------------ public API
    def connect(self):
        global _auth_blocked_until
        if time.time() < _auth_blocked_until:
            remaining = int(_auth_blocked_until - time.time())
            raise RconAuthError(
                f"RCON password was rejected; not retrying for another {remaining}s "
                "(repeated failures get this host banned by the game server). "
                "Check rcon_password in server.cfg."
            )
        try:
            self.sock = socket.create_connection((self.host, self.port), self.timeout)
        except OSError as e:
            raise RconError(f"Can't reach RCON on {self.host}:{self.port} ({e}).") from e
        self.sock.settimeout(self.timeout)
        self._send(_AUTH_ID, SERVERDATA_AUTH, self.password)

        # The server may emit an empty RESPONSE_VALUE before the auth verdict.
        while True:
            packet_id, packet_type, _ = self._read_packet()
            if packet_type != SERVERDATA_AUTH_RESPONSE:
                continue
            if packet_id == -1:
                _auth_blocked_until = time.time() + AUTH_COOLDOWN
                self.close()
                raise RconAuthError("RCON password rejected by the server.")
            return self

    def command(self, cmd):
        """Run one console command and return its full output.

        Responses over ~4 KB arrive as several packets with no end marker, so
        we read the first with the normal timeout and then drain at a short
        one until the server goes quiet. The alternative (probing with a bogus
        empty packet) risks having old Source builds drop the connection.
        """
        if "\x00" in cmd:
            raise RconError("Command contains a null byte.")
        self._send(_CMD_ID, SERVERDATA_EXECCOMMAND, cmd)

        parts = []
        self.sock.settimeout(self.timeout)
        while True:
            try:
                _, _, body = self._read_packet()
            except socket.timeout:
                break
            parts.append(body)
            self.sock.settimeout(_DRAIN_TIMEOUT)
        return "".join(parts).strip()

    def close(self):
        if self.sock:
            try:
                self.sock.close()
            finally:
                self.sock = None

    def __enter__(self):
        return self.connect()

    def __exit__(self, *_exc):
        self.close()


# Remembers which address actually worked, so we only pay for the search once.
_preferred_host = None


def candidate_hosts(configured="127.0.0.1"):
    """Addresses srcds might have bound its RCON socket to.

    srcds binds RCON to whatever the machine's hostname resolves to rather than
    to localhost. On Debian and Ubuntu /etc/hosts usually maps the hostname to
    127.0.1.1, so connecting to 127.0.0.1 is refused even though the server is
    running perfectly.
    """
    hosts = [configured, "127.0.0.1", "127.0.1.1"]
    try:
        hosts.append(socket.gethostbyname(socket.gethostname()))
    except OSError:
        pass
    ordered, seen = [], set()
    for host in hosts:
        if host and host not in seen:
            seen.add(host)
            ordered.append(host)
    return ordered


def execute(cmd, password, host="127.0.0.1", port=27015, timeout=8.0):
    """One-shot helper. A fresh connection per command - srcds drops idle ones."""
    global _preferred_host

    order = candidate_hosts(host)
    if _preferred_host in order:
        order = [_preferred_host] + [h for h in order if h != _preferred_host]

    last_error = None
    for candidate in order:
        try:
            with RconClient(password, candidate, port, timeout) as client:
                output = client.command(cmd)
            _preferred_host = candidate
            return output
        except RconAuthError:
            # Right address, wrong password. Trying more addresses would only
            # burn failed-auth attempts and risk an rcon ban.
            raise
        except (RconError, OSError) as e:
            # OSError covers TimeoutError: a port that accepts the connection
            # but never answers must fall through to the next candidate.
            last_error = e

    raise RconError(
        f"Can't reach RCON on port {port}. Tried {', '.join(order)}. "
        f"Last error: {last_error}. Is the server running? Check: l4d2 status"
    )


_RCON_PW_RE = re.compile(r'^\s*rcon_password\s+"?([^"\r\n]*)"?', re.MULTILINE)


def read_password(cfg_path):
    """Pull rcon_password out of server.cfg. Returns '' when unset or unreadable."""
    try:
        with open(cfg_path, "r", errors="replace") as fh:
            match = _RCON_PW_RE.search(fh.read())
    except OSError:
        return ""
    return match.group(1).strip() if match else ""
