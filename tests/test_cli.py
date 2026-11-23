"""The `l4d2` command (cli/l4d2.py), without a game server, systemd or root.

The module is loaded from its file and pointed at a temp directory; network
fetches and systemctl are stubbed. Running unprivileged also exercises the
"never had root" path of root()/drop_privileges().
"""

import contextlib
import fcntl
import hashlib
import importlib.util
import io
import json
import os
import re
import stat
import struct
import tempfile
import unittest
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("l4d2cli", os.path.join(HERE, "..", "cli", "l4d2.py"))
cli = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cli)


def read(path):
    with open(path) as fh:
        return fh.read()


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as fh:
        fh.write(text)


def make_vpk(path, map_name="testmap"):
    """The smallest valid VPK v1: one empty maps/<name>.bsp entry."""
    tree = (b"bsp\0maps\0" + map_name.encode() + b"\0"
            + struct.pack("<IHHIIH", 0, 0, 0x7FFF, 0, 0, 0xFFFF) + b"\0\0\0")
    with open(path, "wb") as fh:
        fh.write(struct.pack("<III", 0x55AA1234, 1, len(tree)) + tree)


def sha(data):
    return hashlib.sha256(data).hexdigest()


class CliCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = tmp.name
        self.saved = {k: getattr(cli, k) for k in (
            "ENV", "ENV_FILE", "USER", "HOME", "DIR", "GAME", "ADDONS", "SMDIR", "CFG", "CONLOG",
            "INDEX", "INFO", "STEAMCMD", "LOCK_FILE", "HARRY_FILES", "L4DHOOKS_FILES", "fetch",
            "systemctl", "server_pid")}
        self.addCleanup(lambda: [setattr(cli, k, v) for k, v in self.saved.items()])

        cli.ENV_FILE = os.path.join(self.root, "etc", "l4d2.env")
        write(cli.ENV_FILE, "L4D2_MODE=coop\nL4D2_MAP=c1m1_hotel\nL4D2_SLOTS=4\n")
        cli.ENV = cli.load_env(cli.ENV_FILE)
        cli.USER = "no-such-user-l4d2"
        cli.HOME = os.path.join(self.root, "home")
        cli.DIR = os.path.join(cli.HOME, "l4d2server")
        cli.GAME = os.path.join(cli.DIR, "left4dead2")
        cli.ADDONS = os.path.join(cli.GAME, "addons")
        cli.SMDIR = os.path.join(cli.ADDONS, "sourcemod")
        cli.CFG = os.path.join(cli.GAME, "cfg", "server.cfg")
        cli.CONLOG = os.path.join(cli.GAME, "console.log")
        cli.INDEX = os.path.join(cli.HOME, "maps.json")
        cli.INFO = os.path.join(cli.HOME, "SERVER-INFO.txt")
        cli.LOCK_FILE = os.path.join(self.root, "l4d2.lock")
        os.makedirs(cli.ADDONS)
        os.makedirs(os.path.dirname(cli.CFG))
        cli.server_pid = lambda: None
        self.systemctl_calls = []
        self.systemctl_result = 0
        cli.systemctl = lambda action: self.systemctl_calls.append(action) or self.systemctl_result

    def run_quiet(self, fn, *args):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            result = fn(*args)
        return result, out.getvalue()


class AddonPathTests(CliCase):
    def test_only_plain_vpk_names_are_addons(self):
        self.assertEqual(cli.addon_path("123.vpk"), os.path.join(cli.ADDONS, "123.vpk"))
        for bad in ("/etc/passwd", "../../etc/passwd", "sub/x.vpk", "../x.vpk", "x.txt", ".hidden.vpk",
                    "a\x00.vpk", "", None, 5, ["x.vpk"]):
            self.assertIsNone(cli.addon_path(bad), bad)

    def test_removemap_cannot_delete_outside_addons(self):
        victim = os.path.join(self.root, "victim.txt")
        write(victim, "precious")
        real = os.path.join(cli.ADDONS, "111.vpk")
        make_vpk(real)
        write(cli.INDEX, json.dumps({
            "evil-abs": {"campaign": "Evil", "file": victim},
            "evil-rel": {"campaign": "Evil 2", "file": "../../../../victim.txt"},
            "111": {"campaign": "Real", "file": "111.vpk", "maps": ["testmap"]},
        }))
        _, out = self.run_quiet(cli.cmd_removemap, ["evil-abs", "evil-rel", "111", "--no-restart"])
        self.assertEqual(read(victim), "precious")
        self.assertFalse(os.path.exists(real))
        self.assertEqual(cli.load_index(), {})
        self.assertIn("nothing was deleted from disk", out)

    def test_removing_the_default_map_resets_it(self):
        make_vpk(os.path.join(cli.ADDONS, "5.vpk"))
        write(cli.INDEX, json.dumps({"5": {"campaign": "X", "file": "5.vpk", "maps": ["deadcity_m1"]}}))
        cli.set_env("L4D2_MAP", "deadcity_m1")
        self.run_quiet(cli.cmd_removemap, ["5", "--no-restart"])
        self.assertIn("L4D2_MAP=c1m1_hotel", read(cli.ENV_FILE))


class IndexTests(CliCase):
    def test_load_index_tolerates_junk(self):
        self.assertEqual(cli.load_index(), {})
        for junk in ("not json", "[1, 2]", '"text"'):
            write(cli.INDEX, junk)
            self.assertEqual(cli.load_index(), {})
        write(cli.INDEX, '{"a": "string", "b": {"campaign": "ok"}}')
        self.assertEqual(cli.load_index(), {"b": {"campaign": "ok"}})

    def test_save_index_replaces_a_link_instead_of_following_it(self):
        victim = os.path.join(self.root, "victim")
        write(victim, "untouched")
        os.symlink(victim, cli.INDEX)
        cli.save_index({"1": {"campaign": "A"}})
        self.assertEqual(read(victim), "untouched")
        self.assertFalse(os.path.islink(cli.INDEX))
        self.assertEqual(cli.load_index(), {"1": {"campaign": "A"}})
        self.assertEqual([n for n in os.listdir(cli.HOME) if n.startswith(".l4d2-")], [])

    def test_write_file_keeps_mode_and_cleans_up_on_failure(self):
        path = os.path.join(cli.HOME, "f")
        cli.write_file(path, b"a", 0o640)
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o640)
        cli.write_file(path, b"b")
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o640)
        with self.assertRaises(TypeError):
            cli.write_file(path, "not bytes")
        self.assertEqual(read(path), "b")
        self.assertEqual(os.listdir(cli.HOME), ["f", "l4d2server"])


class InstallTests(CliCase):
    def test_local_vpk_and_zip_install(self):
        src = os.path.join(self.root, "My Map (v2).vpk")
        make_vpk(src, "mymap_m1")
        archive = os.path.join(self.root, "pack.zip")
        inner = os.path.join(self.root, "inner.vpk")
        make_vpk(inner, "zipmap")
        with zipfile.ZipFile(archive, "w") as z:
            z.write(inner, "folder/Inner Map.vpk")
            z.writestr("readme.txt", "hi")
        added, _ = self.run_quiet(cli.add_targets, [src, archive, "/nope/missing.vpk"])
        self.assertEqual(sorted(m["first_map"] for m in added), ["mymap_m1", "zipmap"])
        self.assertEqual(sorted(os.listdir(cli.ADDONS)), ["Inner_Map.vpk", "My_Map__v2_.vpk"])
        self.assertEqual(stat.S_IMODE(os.stat(os.path.join(cli.ADDONS, "Inner_Map.vpk")).st_mode), 0o644)
        self.assertEqual(sorted(cli.load_index()), ["file:inner_map.vpk", "file:my_map__v2_.vpk"])
        self.assertTrue(os.path.exists(src))  # the admin's file is copied, not moved
        self.assertEqual([n for n in os.listdir(cli.HOME) if n.startswith(".l4d2-tmp")], [])

    def test_non_vpk_content_is_rejected(self):
        fake = os.path.join(self.root, "fake.vpk")
        write(fake, "this is not a vpk")
        added, out = self.run_quiet(cli.add_targets, [fake])
        self.assertEqual(added, [])
        self.assertIn("not a valid .vpk", out)
        self.assertEqual(os.listdir(cli.ADDONS), [])

    def test_workshop_id_parsing(self):
        self.assertEqual(cli.parse_workshop_id("https://steamcommunity.com/sharedfiles/filedetails/?id=123456789"), "123456789")
        self.assertEqual(cli.parse_workshop_id("987654"), "987654")
        for bad in ("abc", "12", "--force", "/etc/passwd"):
            self.assertIsNone(cli.parse_workshop_id(bad))


class PinnedModTests(CliCase):
    def test_every_download_is_pinned(self):
        hex64, hex40 = re.compile(r"^[0-9a-f]{64}$"), re.compile(r"^[0-9a-f]{40}$")
        for value in (cli.SM_SHA256, cli.MM_SHA256, cli.L4DTOOLZ_SHA256, *cli.L4DHOOKS_FILES.values(),
                      *(s for _, _, s in cli.HARRY_FILES)):
            self.assertRegex(value, hex64)
        self.assertRegex(cli.L4DHOOKS_COMMIT, hex40)
        self.assertRegex(cli.HARRY_COMMIT, hex40)
        for url in (cli.L4DHOOKS_ZIP, cli.HARRY_RAW, cli.L4DTOOLZ_ZIP, cli.SM_FILE, cli.MM_FILE):
            for moving in ("/master", "/main", "refs/heads", "latest"):
                self.assertNotIn(moving, url)
        self.assertIn("plugins/left4dhooks.smx", cli.L4DHOOKS_FILES)
        self.assertEqual(len(cli.SLOT_PLUGINS), 6)

    def test_own_plugins_are_built_with_the_bundled_compiler(self):
        repo_plugins = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "cli", "plugins")
        saved = cli.PLUGIN_SRC
        self.addCleanup(lambda: setattr(cli, "PLUGIN_SRC", saved))
        cli.PLUGIN_SRC = repo_plugins
        self.assertEqual(sorted(n[:-3] for n in os.listdir(repo_plugins) if n.endswith(".sp")),
                         sorted(cli.OWN_PLUGINS))
        scripting = os.path.join(cli.SMDIR, "scripting")
        plugins = os.path.join(cli.SMDIR, "plugins")
        with self.assertRaisesRegex(RuntimeError, "compiler"):   # no compiler: say so, install nothing
            self.run_quiet(cli.install_own_plugins)
        # A compiler that fails on the second plugin: the first must not be installed either.
        write(os.path.join(scripting, "spcomp64"),
              '#!/bin/sh\ncase "$1" in *extras*) echo "error: nope"; exit 1;; esac\ncp "$1" "$3"\n')
        with self.assertRaisesRegex(RuntimeError, "nope"):
            self.run_quiet(cli.install_own_plugins)
        self.assertFalse(os.path.exists(plugins))
        write(os.path.join(scripting, "spcomp64"), '#!/bin/sh\ncp "$1" "$3"\n')   # stand-in compiler
        self.run_quiet(cli.install_own_plugins)
        for name in cli.OWN_PLUGINS:
            self.assertEqual(read(os.path.join(plugins, name + ".smx")), read(os.path.join(repo_plugins, name + ".sp")))
        precache = read(os.path.join(repo_plugins, "l4d2_precache_survivors.sp"))
        for model in ("namvet", "teenangst", "biker", "manager", "gambler", "producer", "coach", "mechanic"):
            self.assertIn(f"models/survivors/survivor_{model}.mdl", precache)
        self.assertEqual([n for n in os.listdir(cli.HOME) if n.startswith(".l4d2-tmp-")], [])
        cli.PLUGIN_SRC = os.path.join(cli.HOME, "nowhere")
        with self.assertRaisesRegex(RuntimeError, "--upgrade"):
            self.run_quiet(cli.install_own_plugins)

    def test_verified_rejects_changed_bytes(self):
        self.assertEqual(cli.verified(b"abc", sha(b"abc"), "x"), b"abc")
        with self.assertRaises(IOError):
            cli.verified(b"abd", sha(b"abc"), "x")

    def test_plugins_install_all_or_nothing(self):
        good = {"a/plugins/one.smx": b"ONE", "b/gamedata/two.txt": b"TWO"}
        cli.HARRY_FILES = [("a/plugins/one.smx", "plugins/one.smx", sha(b"ONE")),
                           ("b/gamedata/two.txt", "gamedata/two.txt", sha(b"TWO"))]
        served = dict(good)
        cli.fetch = lambda url, timeout=60: served[url[len(cli.HARRY_RAW):]]

        served["b/gamedata/two.txt"] = b"TAMPERED"
        with self.assertRaises(IOError):
            self.run_quiet(cli.install_harry_plugins)
        self.assertFalse(os.path.exists(cli.SMDIR))  # the good first file was not written either

        served.update(good)
        write(os.path.join(cli.SMDIR, "plugins", "disabled", "one.smx"), "old")
        self.run_quiet(cli.install_harry_plugins)
        self.assertEqual(read(os.path.join(cli.SMDIR, "plugins", "one.smx")), "ONE")
        self.assertEqual(read(os.path.join(cli.SMDIR, "gamedata", "two.txt")), "TWO")
        self.assertFalse(os.path.exists(os.path.join(cli.SMDIR, "plugins", "disabled", "one.smx")))

    def test_left4dhooks_checks_the_files_inside_the_archive(self):
        def archive(files):
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w") as z:
                for name, data in files.items():
                    z.writestr(f"Left4DHooks-abc/{name}", data)
            return buf.getvalue()

        cli.L4DHOOKS_FILES = {"plugins/left4dhooks.smx": sha(b"SMX"), "gamedata/left4dhooks.l4d2.txt": sha(b"GD")}
        target = os.path.join(cli.SMDIR, "plugins", "left4dhooks.smx")

        for bad in ({"sourcemod/plugins/left4dhooks.smx": b"EVIL", "sourcemod/gamedata/left4dhooks.l4d2.txt": b"GD"},
                    {"sourcemod/plugins/left4dhooks.smx": b"SMX"},
                    # right bytes, wrong folder: must not count as the pinned file
                    {"other/plugins/left4dhooks.smx": b"SMX", "sourcemod/gamedata/left4dhooks.l4d2.txt": b"GD"}):
            cli.fetch = lambda url, timeout=60, bad=bad: archive(bad)
            with self.assertRaises(IOError):
                self.run_quiet(cli.install_left4dhooks)
            self.assertFalse(os.path.exists(target))

        cli.fetch = lambda url, timeout=60: archive({
            "sourcemod/plugins/left4dhooks.smx": b"SMX", "sourcemod/gamedata/left4dhooks.l4d2.txt": b"GD",
            "sourcemod/scripting/extra.sp": b"ignored", "README.md": b"ignored"})
        self.run_quiet(cli.install_left4dhooks)
        self.assertEqual(read(target), "SMX")
        self.assertFalse(os.path.exists(os.path.join(cli.SMDIR, "scripting")))


class ConfigTests(CliCase):
    def init(self, **env):
        keys = {"hostname": "L4D2_SET_HOSTNAME", "password": "L4D2_SET_PASSWORD",
                "rcon": "L4D2_SET_RCON", "difficulty": "L4D2_SET_DIFFICULTY"}
        old = {v: os.environ.get(v) for v in keys.values()}
        for name, var in keys.items():
            os.environ[var] = env.get(name, "")
        try:
            return self.run_quiet(cli.cmd_init_config, [])[1]
        finally:
            for var, value in old.items():
                os.environ.pop(var, None) if value is None else os.environ.__setitem__(var, value)

    def test_fresh_config(self):
        self.init(hostname='Bob\'s "Den"; quit', password="pw", rcon="secret", difficulty="Hard")
        text = read(cli.CFG)
        self.assertIn('hostname "Bob\'s Den quit"', text)
        self.assertIn('sv_password "pw"', text)
        self.assertIn('rcon_password "secret"', text)
        self.assertIn('z_difficulty "Hard"', text)
        self.assertIn("sv_allow_lobby_connect_only 0", text)
        self.assertEqual(stat.S_IMODE(os.stat(cli.CFG).st_mode), 0o640)
        self.assertEqual([n for n in os.listdir(os.path.dirname(cli.CFG)) if ".bak." in n], [])

    def test_rerun_keeps_everything_else(self):
        block = f'{cli.CFG_BEGIN}\nsv_maxplayers 8\n{cli.CFG_END}\n'
        write(cli.CFG, 'hostname "Old"\nsv_password "old"\nrcon_password "keep"   // note\n'
                       'z_difficulty "Normal"\nsv_steamgroup 12345   // mine\nexec banned_user.cfg\n' + block)
        out = self.init(hostname="New", password="", rcon="keep", difficulty="Impossible")
        text = read(cli.CFG)
        self.assertIn("keeping the rest", out)
        self.assertIn('hostname "New"', text)
        self.assertIn('sv_password ""', text)
        self.assertIn('rcon_password "keep"   // note', text)
        self.assertIn('z_difficulty "Impossible"', text)
        self.assertIn("sv_steamgroup 12345   // mine", text)
        self.assertIn("exec banned_user.cfg", text)
        self.assertIn(block, text)
        self.assertEqual(text.count("hostname"), 1)
        backups = [n for n in os.listdir(os.path.dirname(cli.CFG)) if ".bak." in n]
        self.assertEqual(len(backups), 1)
        self.assertIn('hostname "Old"', read(os.path.join(os.path.dirname(cli.CFG), backups[0])))

    def test_backups_are_pruned(self):
        write(cli.CFG, 'hostname "x"\n')
        for stamp in range(1000, 1000 + cli.KEEP_BACKUPS + 5):
            write(f"{cli.CFG}.bak.{stamp}", "old\n")
        write(cli.CFG + ".bak.notes", "mine\n")
        cli.backup_cfg()
        names = sorted(n for n in os.listdir(os.path.dirname(cli.CFG)) if ".bak." in n)
        self.assertEqual(len([n for n in names if n.rsplit(".", 1)[1].isdigit()]), cli.KEEP_BACKUPS)
        self.assertNotIn("server.cfg.bak.1000", names)
        self.assertIn("server.cfg.bak.notes", names)

    def test_slots_block_round_trip(self):
        write(cli.CFG, 'hostname "x"\n')
        cli.set_cfg_block(["sv_maxplayers 8"])
        self.assertIn("sv_maxplayers 8", read(cli.CFG))
        cli.set_cfg_block(["sv_maxplayers 10"])
        self.assertEqual(read(cli.CFG).count("sv_maxplayers"), 1)
        cli.set_cfg_block(None)
        self.assertEqual(read(cli.CFG), 'hostname "x"\n')

    def test_set_env_rejects_shell_syntax(self):
        cli.set_env("L4D2_MAP", "c2m1_highway")
        self.assertIn("L4D2_MAP=c2m1_highway\n", read(cli.ENV_FILE))
        for bad in ("$(id)", "a;b", "a b", "a|b", "`x`", "a\nb"):
            with self.assertRaises(SystemExit):
                self.run_quiet(cli.set_env, "L4D2_MAP", bad)
        self.assertIn("L4D2_MAP=c2m1_highway\n", read(cli.ENV_FILE))
        self.assertEqual(os.listdir(os.path.dirname(cli.ENV_FILE)), ["l4d2.env"])

    def test_admin_is_added_once(self):
        os.makedirs(os.path.join(cli.SMDIR, "configs"))
        os.makedirs(os.path.join(cli.ADDONS, "metamod"))
        ini = os.path.join(cli.SMDIR, "configs", "admins_simple.ini")
        write(ini, '// admins\n"STEAM_1:0:1" "99:z"')
        self.run_quiet(cli.cmd_admin, ["STEAM_1:0:12345678"])
        self.run_quiet(cli.cmd_admin, ["STEAM_1:0:12345678"])
        self.assertEqual(read(ini), '// admins\n"STEAM_1:0:1" "99:z"\n"STEAM_1:0:12345678" "99:z"\n')
        with self.assertRaises(SystemExit):
            self.run_quiet(cli.cmd_admin, ['x" "99:z"\n"evil'])


class LockAndStatusTests(CliCase):
    def test_second_command_is_turned_away(self):
        with cli.locked("update"):
            with cli.locked("restart"):      # the same process may re-enter (menu -> command)
                pass
            other = open(cli.LOCK_FILE, "a+")
            self.addCleanup(other.close)
            with self.assertRaises(OSError):
                fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)   # released afterwards
        other.seek(0)
        other.truncate()
        other.write("update")
        other.flush()
        with self.assertRaises(SystemExit) as caught:
            self.run_quiet(lambda: cli.locked("addmap").__enter__())
        self.assertEqual(caught.exception.code, 75)

    def test_permission_problems_explain_the_fix(self):
        def denied(_args):
            raise PermissionError(13, "Permission denied", "/somewhere/else")

        saved = (cli.install_mods, cli.sys.argv, cli.os.geteuid, cli.os.chdir, cli.init_paths,
                 cli.drop_privileges)
        self.addCleanup(lambda: (setattr(cli, "install_mods", saved[0]), setattr(cli.sys, "argv", saved[1]),
                                 setattr(cli.os, "geteuid", saved[2]), setattr(cli.os, "chdir", saved[3]),
                                 setattr(cli, "init_paths", saved[4]), setattr(cli, "drop_privileges", saved[5])))
        cli.drop_privileges = lambda: None
        cli.install_mods = lambda update=False: denied(None)
        with self.assertRaises(PermissionError):   # not reported as "mod download failed"
            self.run_quiet(cli.cmd_slots, ["8", "--no-restart"])

        cli.os.geteuid = lambda: 0                 # pretend we were started as root
        cli.os.chdir = lambda path: None
        cli.init_paths = lambda: None
        cli.sys.argv = ["l4d2", "slots", "8", "--no-restart"]
        code, out = self.run_quiet(cli.main)
        self.assertEqual(code, 1)
        self.assertIn(f"sudo chown -R -h {cli.USER}: {cli.HOME}", out)
        self.assertEqual(read(cli.LOCK_FILE), "")  # and the lock was let go

    def test_slots_set_the_client_limit_the_service_starts_with(self):
        cli.install_mods = lambda update=False: None
        os.makedirs(os.path.join(cli.SMDIR, "plugins", "disabled"), exist_ok=True)
        os.makedirs(os.path.join(cli.GAME, "cfg", "sourcemod"), exist_ok=True)
        self.run_quiet(cli.cmd_slots, ["8", "--no-restart"])
        self.assertIn("L4D2_SLOTS=8\n", read(cli.ENV_FILE))
        self.assertIn("L4D2_MAXCLIENTS=31\n", read(cli.ENV_FILE))
        self.run_quiet(cli.cmd_slots, ["4", "--no-restart"])
        self.assertIn("L4D2_SLOTS=4\n", read(cli.ENV_FILE))
        self.assertIn("L4D2_MAXCLIENTS=18\n", read(cli.ENV_FILE))

    def test_editing_config_does_not_hold_the_lock(self):
        self.assertNotIn("config", cli.LOCKING)
        for name in ("addmap", "removemap", "update", "slots", "restart", "changemap", "init-config"):
            self.assertIn(name, cli.LOCKING)

    def test_start_stop_report_failure(self):
        code, out = self.run_quiet(cli.dispatch, "start", [])
        self.assertEqual(code, 0)
        self.assertIn("start OK", out)
        self.systemctl_result = 5
        code, out = self.run_quiet(cli.dispatch, "stop", [])
        self.assertEqual(code, 5)
        self.assertIn("FAILED", out)
        code, _ = self.run_quiet(cli.dispatch, "restart", [])
        self.assertEqual(code, 1)

    def test_update_reports_a_failed_download(self):
        saved = (cli.subprocess.run, cli.cmd_updatemaps, cli.mods_installed)
        self.addCleanup(lambda: (setattr(cli.subprocess, "run", saved[0]),
                                 setattr(cli, "cmd_updatemaps", saved[1]),
                                 setattr(cli, "mods_installed", saved[2])))

        class Result:
            returncode = 0

        cli.cmd_updatemaps = lambda flags: []
        cli.mods_installed = lambda: False
        cli.subprocess.run = lambda *a, **k: Result()
        write(os.path.join(cli.DIR, "srcds_run"), "")

        code, out = self.run_quiet(cli.cmd_update, [])
        self.assertEqual(code, 0)
        self.assertIn("Update finished, server started.", out)
        self.assertEqual(self.systemctl_calls, ["stop", "start"])

        Result.returncode = 8
        code, out = self.run_quiet(cli.cmd_update, [])
        self.assertEqual(code, 1)
        self.assertIn("WITH PROBLEMS", out)
        self.assertIn("exit 8", out)
        self.assertEqual(self.systemctl_calls[-1], "start")  # the server is still brought back up

    def test_every_documented_command_is_dispatched(self):
        source = read(os.path.join(HERE, "..", "cli", "l4d2.py"))
        body = source[source.index("def dispatch("):source.index("def main(")]
        for name in ("status", "start", "stop", "restart", "console", "logs", "config", "update", "info",
                     "addmap", "maps", "changemap", "removemap", "updatemaps", "share", "slots", "admin",
                     "modcheck", "menu"):
            self.assertIn(f'"{name}"', body, name)
            self.assertIn(name, cli.HELP)


if __name__ == "__main__":
    unittest.main()
