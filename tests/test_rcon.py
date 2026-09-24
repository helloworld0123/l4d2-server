import socket
import socketserver
import struct
import threading
import unittest

from bot import rcon


class CodecTests(unittest.TestCase):
    def test_round_trip(self):
        raw = rcon.encode_packet(7, rcon.SERVERDATA_EXECCOMMAND, "status")
        size = struct.unpack("<i", raw[:4])[0]
        self.assertEqual(size, len(raw) - 4)
        # size covers id + type + body + two NULs
        self.assertEqual(size, 4 + 4 + len("status") + 2)
        self.assertEqual(rcon.decode_packet(raw[4:]), (7, 2, "status"))

    def test_empty_body(self):
        raw = rcon.encode_packet(1, 0, "")
        self.assertEqual(struct.unpack("<i", raw[:4])[0], 10)
        self.assertEqual(rcon.decode_packet(raw[4:]), (1, 0, ""))

    def test_utf8_survives(self):
        raw = rcon.encode_packet(2, 0, "café ✓")
        self.assertEqual(rcon.decode_packet(raw[4:])[2], "café ✓")


class FakeRcon(socketserver.BaseRequestHandler):
    password = "secret"
    reply = "pong"
    chunks = 1

    def handle(self):
        while True:
            header = self._read(4)
            if not header:
                return
            size = struct.unpack("<i", header)[0]
            body = self._read(size)
            if body is None:
                return
            pid, ptype = struct.unpack("<ii", body[:8])
            text = body[8:-2].decode()

            if ptype == rcon.SERVERDATA_AUTH:
                # Real servers send an empty RESPONSE_VALUE before the verdict.
                self.request.sendall(rcon.encode_packet(pid, 0, ""))
                ok = text == self.password
                self.request.sendall(
                    rcon.encode_packet(pid if ok else -1, rcon.SERVERDATA_AUTH_RESPONSE, "")
                )
                if not ok:
                    return
            else:
                for part in self._split():
                    self.request.sendall(rcon.encode_packet(pid, 0, part))

    def _split(self):
        if self.chunks == 1:
            return [self.reply]
        step = max(1, len(self.reply) // self.chunks)
        return [self.reply[i:i + step] for i in range(0, len(self.reply), step)]

    def _read(self, n):
        buf = b""
        while len(buf) < n:
            chunk = self.request.recv(n - len(buf))
            if not chunk:
                return None
            buf += chunk
        return buf


class ServerTests(unittest.TestCase):
    def setUp(self):
        rcon._auth_blocked_until = 0.0
        self.server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), FakeRcon)
        self.host, self.port = self.server.server_address
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        rcon._auth_blocked_until = 0.0

    def test_auth_and_command(self):
        out = rcon.execute("status", "secret", self.host, self.port, timeout=3)
        self.assertEqual(out, "pong")

    def test_multi_packet_response(self):
        FakeRcon.reply = "x" * 9000
        FakeRcon.chunks = 4
        try:
            out = rcon.execute("status", "secret", self.host, self.port, timeout=3)
            self.assertEqual(len(out), 9000, "multi-packet reply was truncated")
        finally:
            FakeRcon.reply, FakeRcon.chunks = "pong", 1

    def test_bad_password_raises_and_blocks_retry(self):
        with self.assertRaises(rcon.RconAuthError):
            rcon.execute("status", "wrong", self.host, self.port, timeout=3)
        # A second attempt must not touch the network: repeated failures get
        # this host rcon-banned by the game server.
        with self.assertRaises(rcon.RconAuthError) as caught:
            rcon.execute("status", "wrong", self.host, self.port, timeout=3)
        self.assertIn("not retrying", str(caught.exception))

    def test_empty_password_refused_before_connecting(self):
        with self.assertRaises(rcon.RconAuthError):
            rcon.execute("status", "", self.host, self.port, timeout=3)

    def test_null_byte_rejected(self):
        client = rcon.RconClient("secret", self.host, self.port, timeout=3).connect()
        try:
            with self.assertRaises(rcon.RconError):
                client.command("say hi\x00quit")
        finally:
            client.close()

    def test_unreachable_port_raises_rcon_error(self):
        spare = socket.socket()
        spare.bind(("127.0.0.1", 0))
        dead_port = spare.getsockname()[1]
        spare.close()
        with self.assertRaises(rcon.RconError):
            rcon.execute("status", "secret", "127.0.0.1", dead_port, timeout=2)


class HostDiscoveryTests(unittest.TestCase):
    """srcds binds RCON to the hostname's address, often 127.0.1.1, not localhost."""

    def setUp(self):
        rcon._auth_blocked_until = 0.0
        rcon._preferred_host = None
        # Bind ONLY to 127.0.1.1, so a connect to 127.0.0.1 is refused.
        self.server = socketserver.ThreadingTCPServer(("127.0.1.1", 0), FakeRcon)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        rcon._preferred_host = None

    def test_candidates_include_the_debian_hostname_address(self):
        self.assertIn("127.0.1.1", rcon.candidate_hosts("127.0.0.1"))

    def test_falls_back_when_localhost_is_refused(self):
        # Configured as 127.0.0.1 (refused) but the server is on 127.0.1.1.
        out = rcon.execute("status", "secret", "127.0.0.1", self.port, timeout=3)
        self.assertEqual(out, "pong")
        self.assertEqual(rcon._preferred_host, "127.0.1.1")

    def test_working_host_is_remembered(self):
        rcon.execute("status", "secret", "127.0.0.1", self.port, timeout=3)
        self.assertEqual(rcon._preferred_host, "127.0.1.1")
        self.assertEqual(rcon.execute("status", "secret", "127.0.0.1", self.port, timeout=3), "pong")

    def test_error_names_every_address_tried(self):
        self.server.shutdown()
        self.server.server_close()  # shutdown() alone leaves the port listening
        with self.assertRaises(rcon.RconError) as caught:
            rcon.execute("status", "secret", "127.0.0.1", self.port, timeout=2)
        message = str(caught.exception)
        self.assertIn("127.0.0.1", message)
        self.assertIn("127.0.1.1", message)


class PasswordFileTests(unittest.TestCase):
    def test_reads_quoted_password(self):
        import tempfile, os
        fd, path = tempfile.mkstemp()
        os.write(fd, b'hostname "x"\nrcon_password "s3cr3t"   // comment\nsv_lan 0\n')
        os.close(fd)
        try:
            self.assertEqual(rcon.read_password(path), "s3cr3t")
        finally:
            os.unlink(path)

    def test_empty_password_reads_as_empty(self):
        import tempfile, os
        fd, path = tempfile.mkstemp()
        os.write(fd, b'rcon_password ""          // empty = disabled\n')
        os.close(fd)
        try:
            self.assertEqual(rcon.read_password(path), "")
        finally:
            os.unlink(path)

    def test_missing_file_is_empty_not_an_error(self):
        self.assertEqual(rcon.read_password("/nonexistent/server.cfg"), "")


if __name__ == "__main__":
    unittest.main()
