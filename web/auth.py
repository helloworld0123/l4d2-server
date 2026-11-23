"""Password hashing, sessions and login throttling. Standard library only.

    python3 -m web.auth      reads a password on stdin, prints its hash
"""

import hashlib
import hmac
import secrets
import sys
import threading
import time

ITERATIONS = 600_000
SCHEME = "pbkdf2_sha256"
MIN_PASSWORD = 10


def hash_password(password, salt=None, iterations=ITERATIONS):
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt), iterations)
    return f"{SCHEME}:{iterations}:{salt}:{digest.hex()}"


def verify_password(password, stored):
    try:
        scheme, iterations, salt, digest = (stored or "").split(":")
        if scheme != SCHEME:
            return False
        candidate = hashlib.pbkdf2_hmac(
            "sha256", (password or "").encode("utf-8"), bytes.fromhex(salt), int(iterations))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(candidate.hex(), digest)


class Sessions:
    """In-memory session tokens. A panel restart signs everyone out, by design."""

    def __init__(self, idle=2 * 3600, lifetime=24 * 3600, limit=32):
        self.idle = idle
        self.lifetime = lifetime
        self.limit = limit
        self._lock = threading.Lock()
        self._sessions = {}  # token -> (created, last_seen)

    def create(self):
        token = secrets.token_urlsafe(32)
        now = time.time()
        with self._lock:
            self._reap(now)
            while len(self._sessions) >= self.limit:
                oldest = min(self._sessions, key=lambda t: self._sessions[t][1])
                self._sessions.pop(oldest)
            self._sessions[token] = (now, now)
        return token

    def valid(self, token):
        if not token:
            return False
        now = time.time()
        with self._lock:
            entry = self._sessions.get(token)
            if not entry:
                return False
            created, seen = entry
            if now - seen > self.idle or now - created > self.lifetime:
                self._sessions.pop(token, None)
                return False
            self._sessions[token] = (created, now)
            return True

    def destroy(self, token):
        with self._lock:
            self._sessions.pop(token, None)

    def _reap(self, now):
        dead = [t for t, (created, seen) in self._sessions.items()
                if now - seen > self.idle or now - created > self.lifetime]
        for token in dead:
            self._sessions.pop(token, None)


class Throttle:
    """Lock an address out after repeated wrong passwords."""

    def __init__(self, attempts=5, window=300):
        self.attempts = attempts
        self.window = window
        self._lock = threading.Lock()
        self._failures = {}  # address -> [timestamps]

    def attempt(self, address):
        """Claim one try. Returns 0 when allowed, else seconds to wait.

        The try is counted *before* the password is checked, so a burst of
        parallel requests can't all slip past the limit while hashing runs.
        """
        now = time.time()
        with self._lock:
            for other in [a for a, stamps in self._failures.items() if now - stamps[-1] >= self.window]:
                self._failures.pop(other)
            stamps = [t for t in self._failures.get(address, []) if now - t < self.window]
            if len(stamps) >= self.attempts:
                self._failures[address] = stamps
                return int(self.window - (now - stamps[0])) + 1
            stamps.append(now)
            self._failures[address] = stamps
            return 0

    def succeeded(self, address):
        with self._lock:
            self._failures.pop(address, None)


if __name__ == "__main__":
    secret = sys.stdin.readline().rstrip("\r\n")
    if len(secret) < MIN_PASSWORD:
        sys.exit(f"Password must be at least {MIN_PASSWORD} characters.")
    print(hash_password(secret))
