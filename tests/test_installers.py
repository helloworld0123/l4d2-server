"""Tests for the server.cfg edit in telegram-bot-setup.sh and web-panel-setup.sh.

The installers run as root and need a real system, so these tests pull the
set_rcon_password function out of each script and run it as the current user
against a scratch server.cfg. AS_GAME_USER (normally `sudo -u <game user> --`)
is swapped for small wrappers, which lets the tests check that every file
operation goes through that wrapper and that the password never reaches argv.
"""

import os
import re
import shutil
import stat
import subprocess
import tempfile
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INSTALLERS = ("telegram-bot-setup.sh", "web-panel-setup.sh")
PASSWORD = "Secr3tRconPassw0rd"


def extract_function(script, name):
    with open(os.path.join(ROOT, script)) as fh:
        text = fh.read()
    match = re.search(rf"^{name}\(\) \{{\n.*?^\}}\n", text, re.S | re.M)
    if not match:
        raise AssertionError(f"{name} not found in {script}")
    return match.group(0)


class SetRconPasswordTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.cfg_dir = os.path.join(self.tmp, "left4dead2", "cfg")
        os.makedirs(self.cfg_dir)
        self.cfg = os.path.join(self.cfg_dir, "server.cfg")
        self.argv_log = os.path.join(self.tmp, "argv.log")
        # Records its arguments, then runs them: stands in for sudo -u.
        self.recorder = os.path.join(self.tmp, "recorder")
        with open(self.recorder, "w") as fh:
            fh.write('#!/bin/sh\nprintf "%s\\n" "$@" >>"$ARGV_LOG"\nexec "$@"\n')
        # Records its arguments and does nothing else.
        self.noop = os.path.join(self.tmp, "noop")
        with open(self.noop, "w") as fh:
            fh.write('#!/bin/sh\nprintf "%s\\n" "$@" >>"$ARGV_LOG"\n')
        os.chmod(self.recorder, 0o755)
        os.chmod(self.noop, 0o755)

    def write_cfg(self, text):
        with open(self.cfg, "w") as fh:
            fh.write(text)

    def read(self, path):
        with open(path) as fh:
            return fh.read()

    def run_fn(self, script, runner):
        code = (extract_function(script, "set_rcon_password")
                + f'AS_GAME_USER=("{runner}")\n'
                + 'printf "%s\\n" "$PW" | set_rcon_password "$1"\n')
        return subprocess.run(
            ["bash", "-c", code, "test", self.cfg],
            env={"PATH": os.environ["PATH"], "PW": PASSWORD, "ARGV_LOG": self.argv_log},
            capture_output=True, text=True, timeout=30)

    def backups(self):
        return sorted(n for n in os.listdir(self.cfg_dir) if n.startswith("server.cfg.bak."))

    def test_replaces_existing_line_and_keeps_backup(self):
        for script in INSTALLERS:
            with self.subTest(script=script):
                for name in os.listdir(self.cfg_dir):
                    os.remove(os.path.join(self.cfg_dir, name))
                original = 'hostname "x"\n  rcon_password ""\nsv_password "p"\n'
                self.write_cfg(original)
                result = self.run_fn(script, self.recorder)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(self.read(self.cfg),
                                 f'hostname "x"\nrcon_password "{PASSWORD}"\nsv_password "p"\n')
                self.assertEqual(stat.S_IMODE(os.stat(self.cfg).st_mode), 0o640)
                [backup] = self.backups()
                backup = os.path.join(self.cfg_dir, backup)
                self.assertEqual(self.read(backup), original)
                self.assertEqual(stat.S_IMODE(os.stat(backup).st_mode), 0o600)
                # Nothing but the cfg and its backup is left behind.
                self.assertEqual(len(os.listdir(self.cfg_dir)), 2)

    def test_appends_when_missing(self):
        for script in INSTALLERS:
            with self.subTest(script=script):
                for name in os.listdir(self.cfg_dir):
                    os.remove(os.path.join(self.cfg_dir, name))
                self.write_cfg('hostname "x"')   # no trailing newline
                result = self.run_fn(script, self.recorder)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(self.read(self.cfg),
                                 f'hostname "x"\n\nrcon_password "{PASSWORD}"\n')

    def test_password_never_in_argv(self):
        for script in INSTALLERS:
            with self.subTest(script=script):
                for name in os.listdir(self.cfg_dir):
                    os.remove(os.path.join(self.cfg_dir, name))
                self.write_cfg('rcon_password ""\n')
                if os.path.exists(self.argv_log):
                    os.remove(self.argv_log)
                result = self.run_fn(script, self.recorder)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn(PASSWORD, self.read(self.cfg))
                self.assertTrue(os.path.exists(self.argv_log))
                self.assertNotIn(PASSWORD, self.read(self.argv_log))

    def test_does_nothing_outside_the_game_user_wrapper(self):
        # With the wrapper turned into a no-op, the calling (root) side must
        # not have touched the folder at all: every operation went through it.
        for script in INSTALLERS:
            with self.subTest(script=script):
                self.write_cfg('rcon_password ""\n')
                before = sorted(os.listdir(self.cfg_dir))
                mtime = os.stat(self.cfg).st_mtime_ns
                self.run_fn(script, self.noop)
                self.assertEqual(sorted(os.listdir(self.cfg_dir)), before)
                self.assertEqual(os.stat(self.cfg).st_mtime_ns, mtime)
                self.assertEqual(self.read(self.cfg), 'rcon_password ""\n')

    def test_planted_backup_link_is_not_written_through(self):
        for script in INSTALLERS:
            with self.subTest(script=script):
                for name in os.listdir(self.cfg_dir):
                    os.remove(os.path.join(self.cfg_dir, name))
                victim = os.path.join(self.tmp, "victim")
                with open(victim, "w") as fh:
                    fh.write("untouched\n")
                dangling = os.path.join(self.tmp, "does-not-exist")
                now = int(time.time())
                for i, ts in enumerate(range(now - 2, now + 30)):
                    target = victim if i % 2 else dangling
                    os.symlink(target, f"{self.cfg}.bak.{ts}")
                self.write_cfg('rcon_password ""\n')
                result = self.run_fn(script, self.recorder)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(self.read(victim), "untouched\n")
                self.assertFalse(os.path.exists(dangling))
                # Refused before anything else happened.
                self.assertEqual(self.read(self.cfg), 'rcon_password ""\n')


if __name__ == "__main__":
    unittest.main()
