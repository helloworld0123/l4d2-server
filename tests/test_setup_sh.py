"""Helpers of l4d2-setup.sh that work without root or whiptail.

Each function is cut out of the script and run under bash on its own, so the
installer itself (which needs root) is never started.
"""

import os
import re
import subprocess
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "..", "l4d2-setup.sh")


def function(name):
    with open(SCRIPT) as fh:
        source = fh.read()
    m = re.search(r"^" + re.escape(name) + r"\(\) \{\n.*?^\}\n", source, re.M | re.S)
    if not m:
        raise AssertionError(f"{name}() not found in l4d2-setup.sh")
    return m.group(0)


def bash(names, body, stdin="", **env):
    script = "set -uo pipefail\n" + "".join(function(n) for n in names) + body
    result = subprocess.run(["bash", "-c", script], input=stdin, capture_output=True, text=True,
                            env={"PATH": os.environ["PATH"], **env}, timeout=30)
    if result.returncode != 0:
        raise AssertionError(f"bash failed ({result.returncode}): {result.stderr}")
    return result.stdout


class OldCvarTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.cfg = os.path.join(tmp.name, "server.cfg")

    def old(self, text, name):
        with open(self.cfg, "w", newline="") as fh:
            fh.write(text)
        return bash(["old_cvar"], f'old_cvar "{name}"', CFG_FILE=self.cfg).rstrip("\n")

    def test_quoted_and_unquoted_values(self):
        self.assertEqual(self.old('rcon_password "secret123"\n', "rcon_password"), "secret123")
        self.assertEqual(self.old("rcon_password secret123\n", "rcon_password"), "secret123")
        self.assertEqual(self.old("hostname My Server\n", "hostname"), "My Server")
        self.assertEqual(self.old('hostname "My  Server"\n', "hostname"), "My  Server")

    def test_crlf_comments_and_case(self):
        self.assertEqual(self.old("rcon_password secret123\r\n", "rcon_password"), "secret123")
        self.assertEqual(self.old('rcon_password "s3cret" // admin\r\n', "rcon_password"), "s3cret")
        self.assertEqual(self.old("rcon_password s3cret   // admin\n", "rcon_password"), "s3cret")
        self.assertEqual(self.old('\tRCON_Password "x"\n', "rcon_password"), "x")
        self.assertEqual(self.old("z_difficulty hard", "z_difficulty"), "hard")   # no final newline

    def test_last_line_wins_and_lookalikes_do_not_count(self):
        text = ('// rcon_password "commented"\nrcon_password_x "other"\n'
                'rcon_password "first"\nrcon_password second\n')
        self.assertEqual(self.old(text, "rcon_password"), "second")
        self.assertEqual(self.old("// rcon_password old\nsv_lan 0\n", "rcon_password"), "")
        self.assertEqual(self.old('sv_password ""\n', "sv_password"), "")

    def test_no_file_or_a_link_reads_nothing(self):
        self.assertEqual(bash(["old_cvar"], "old_cvar hostname", CFG_FILE=self.cfg + ".missing"), "")
        os.symlink("/etc/hostname", self.cfg)
        self.assertEqual(bash(["old_cvar"], "old_cvar hostname", CFG_FILE=self.cfg), "")


class MenuDefaultTests(unittest.TestCase):
    def default(self, current, *choices):
        args = " ".join(f"'{c}'" for c in choices)
        return bash(["menu_default"], f"menu_default '{current}' {args}").rstrip("\n")

    def test_current_value_is_preselected(self):
        self.assertEqual(self.default("8", "4", "6", "8", "10", "12"), "8")
        self.assertEqual(self.default("c8m1_apartment", "c1m1_hotel", "c8m1_apartment"), "c8m1_apartment")
        self.assertEqual(self.default("hard", "Easy", "Normal", "Hard", "Impossible"), "Hard")

    def test_value_outside_the_menu_is_kept(self):
        self.assertEqual(self.default("7", "4", "6", "8"), "keep")
        self.assertEqual(self.default("l4d2_ff01_woods", "c1m1_hotel", "c2m1_highway"), "keep")

    def test_no_current_value_means_the_usual_default(self):
        self.assertEqual(self.default("", "4", "6"), "")


class EnvAndNetworkTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.env = os.path.join(tmp.name, "l4d2.env")

    def test_env_val(self):
        with open(self.env, "w") as fh:
            fh.write("L4D2_MODE=coop\nL4D2_MAP=c2m1_highway\nL4D2_SLOTS=8\nL4D2_SLOTS2=1\n")
        out = bash(["env_val"], "env_val L4D2_SLOTS; env_val L4D2_MAP; env_val L4D2_NET; echo end",
                   L4D2_ENV=self.env)
        self.assertEqual(out, "8\nc2m1_highway\nend\n")
        self.assertEqual(bash(["env_val"], "env_val L4D2_MAP; echo end", L4D2_ENV=self.env + ".x"), "end\n")

    def ufw(self, status):
        return bash(["net_from_ufw"], "net_from_ufw", stdin=status, GAME_PORT="27015").rstrip("\n")

    HEAD = "Status: active\n\nTo                         Action      From\n--                         ------      ----\n" \
           "22/tcp                     ALLOW       Anywhere\n"

    def test_network_mode_is_read_back_from_the_firewall(self):
        self.assertEqual(self.ufw(self.HEAD + "27015/udp                  ALLOW       Anywhere\n"
                                  "27015/udp (v6)             ALLOW       Anywhere (v6)\n"), "public")
        self.assertEqual(self.ufw(self.HEAD + "27015/udp on tailscale0    ALLOW       Anywhere\n"), "tailscale")
        self.assertEqual(self.ufw(self.HEAD + "27015/udp                  ALLOW       10.0.0.0/8\n"
                                  "27015/udp                  ALLOW       192.168.0.0/16\n"), "lan")
        self.assertEqual(self.ufw(self.HEAD), "")
        self.assertEqual(self.ufw("Status: inactive\n"), "")

    def test_recorded_network_mode_wins(self):
        with open(self.env, "w") as fh:
            fh.write("L4D2_NET=lan\n")
        out = bash(["env_val", "net_from_ufw", "current_net"], "current_net", L4D2_ENV=self.env)
        self.assertEqual(out, "lan\n")


FAKE_WHIPTAIL = r"""#!/bin/bash
# Logs its arguments and answers like an admin pressing Enter on every screen.
printf '%s\n' "$@" >>"$WT_LOG"
args=("$@"); default=""; mode=""; first=""; init=""
for ((i = 0; i < ${#args[@]}; i++)); do
  case "${args[i]}" in
    --default-item) default=${args[i+1]} ;;
    --menu)         mode=menu; first=${args[i+5]} ;;
    --inputbox)     mode=input; [[ "${args[i+4]:-}" == -- ]] && init=${args[i+5]:-} ;;
    --passwordbox)  mode=input ;;
  esac
done
case "$mode" in
  menu)  printf '%s' "${default:-$first}" >&2 ;;
  input) printf '%s' "$init" >&2 ;;
esac
exit 0
"""


class QuestionsTests(unittest.TestCase):
    """The whole question section of the installer, with a fake whiptail."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = tmp.name
        bindir = os.path.join(self.tmp, "bin")
        os.makedirs(bindir)
        for name, body in (("whiptail", FAKE_WHIPTAIL), ("ufw", "#!/bin/sh\nexit 0\n")):
            path = os.path.join(bindir, name)
            with open(path, "w") as fh:
                fh.write(body)
            os.chmod(path, 0o755)
        self.path = bindir + ":" + os.environ["PATH"]
        self.cfg = os.path.join(self.tmp, "server.cfg")
        self.env = os.path.join(self.tmp, "l4d2.env")
        self.log = os.path.join(self.tmp, "whiptail.log")

    def answers(self):
        with open(SCRIPT) as fh:
            source = fh.read()
        start = source.index("# ---------------------------------------------------------------- UI helpers")
        end = source.index("# ---------------------------------------------------------------- install steps")
        names = ("SV_NAME", "SV_PASS", "SV_PASS_KEEP", "RCON_PASS", "RCON_KEEP", "RCON_NEW", "GAME_MODE",
                 "SLOTS", "START_MAP", "DIFFICULTY", "NET_MODE")
        script = ("set -uo pipefail\nTITLE=t SRV_USER=l4d2 GAME_PORT=27015\n"
                  f"CFG_FILE='{self.cfg}' L4D2_ENV='{self.env}'\n" + source[start:end]
                  + "".join(f'printf "%s=%s\\n" {n} "${{{n}:-}}"\n' for n in names))
        result = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=60,
                                env={"PATH": self.path, "WT_LOG": self.log, "HOME": self.tmp})
        self.assertEqual(result.returncode, 0, result.stderr)
        values = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
        with open(self.log) as fh:
            return values, fh.read()

    def test_enter_through_a_rerun_changes_nothing(self):
        with open(self.cfg, "w", newline="") as fh:
            fh.write('hostname My Server\r\nsv_password hunter2\r\nrcon_password secret123 // admin\r\n'
                     'z_difficulty "Impossible"\r\n')
        with open(self.env, "w") as fh:
            fh.write("L4D2_MODE=realism\nL4D2_MAP=l4d2_custom_m1\nL4D2_SLOTS=8\nL4D2_NET=public\n")
        values, argv = self.answers()
        self.assertEqual(values["SV_NAME"], "My Server")
        self.assertEqual((values["SV_PASS"], values["SV_PASS_KEEP"]), ("hunter2", "1"))
        self.assertEqual((values["RCON_PASS"], values["RCON_KEEP"]), ("secret123", "1"))
        self.assertEqual((values["GAME_MODE"], values["SLOTS"], values["START_MAP"], values["DIFFICULTY"],
                          values["NET_MODE"]), ("realism", "8", "l4d2_custom_m1", "Impossible", "public"))
        self.assertIn("Keep current (l4d2_custom_m1)", argv)
        for secret in ("hunter2", "secret123"):
            self.assertNotIn(secret, argv)

    def test_odd_slot_count_and_network_from_an_older_install(self):
        with open(self.cfg, "w") as fh:
            fh.write('hostname "x"\n')
        with open(self.env, "w") as fh:
            fh.write("L4D2_MODE=coop\nL4D2_MAP=c8m1_apartment\nL4D2_SLOTS=7\n")
        with open(os.path.join(self.tmp, "bin", "ufw"), "w") as fh:
            fh.write("#!/bin/sh\necho '27015/udp                  ALLOW       192.168.0.0/16'\n")
        values, argv = self.answers()
        self.assertEqual((values["SLOTS"], values["START_MAP"], values["NET_MODE"]), ("7", "c8m1_apartment", "lan"))
        self.assertEqual((values["RCON_PASS"], values["RCON_KEEP"]), ("", "1"))   # stays disabled
        self.assertEqual(values["DIFFICULTY"], "Normal")

    def test_fresh_install_generates_rcon_without_showing_it(self):
        values, argv = self.answers()
        self.assertEqual(values["RCON_NEW"], "1")
        self.assertRegex(values["RCON_PASS"], r"^[A-Za-z0-9]{16}$")
        self.assertNotIn(values["RCON_PASS"], argv)
        self.assertEqual((values["GAME_MODE"], values["SLOTS"], values["START_MAP"], values["DIFFICULTY"],
                          values["NET_MODE"]), ("coop", "4", "c1m1_hotel", "Normal", "tailscale"))
        self.assertEqual(values["SV_PASS"], "")


class SyntaxTests(unittest.TestCase):
    def test_script_parses(self):
        subprocess.run(["bash", "-n", SCRIPT], check=True)


if __name__ == "__main__":
    unittest.main()
