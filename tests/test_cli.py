"""The `l4d2` command (cli/l4d2.py), without a game server, systemd or root.

The module is loaded from its file and pointed at a temp directory; network
fetches and systemctl are stubbed. Running unprivileged also exercises the
"never had root" path of root()/drop_privileges().
"""

import contextlib
import errno
import fcntl
import hashlib
import importlib.util
import io
import json
import os
import re
import stat
import struct
import tarfile
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
            "systemctl", "server_pid", "install_mods")}
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
        self.assertEqual(sorted(os.listdir(cli.HOME)), ["f", "l4d2server"])


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

    def test_every_extra_plugin_download_is_pinned(self):
        hex64 = re.compile(r"^[0-9a-f]{64}$")
        for value in (cli.ACTIONS_SHA256, cli.CLEANER_SHA256, *(s for _, s in cli.EXTRA_HARRY_FILES),
                      *(s for _, _, s in cli.EXTRA_FILES)):
            self.assertRegex(value, hex64)
        for url in (cli.ACTIONS_ZIP, cli.CLEANER_TGZ, *(u for u, _, _ in cli.EXTRA_FILES)):
            self.assertTrue(url.startswith("https://"), url)
            for moving in ("/master", "/main", "refs/heads", "latest"):
                self.assertNotIn(moving, url)
        # Each plugin comes with everything it refuses to start without.
        harry = {src for src, _ in cli.EXTRA_HARRY_FILES}
        for needed in ("l4dinfectedbots/plugins/l4dinfectedbots.smx", "l4dinfectedbots/gamedata/l4dinfectedbots.txt",
                       "l4dinfectedbots/data/l4dinfectedbots/coop.cfg", "l4dinfectedbots/data/l4dinfectedbots/realism.cfg",
                       "spawn_infected_nolimit/plugins/spawn_infected_nolimit.smx",
                       "l4d_afk_commands/plugins/l4d_afk_commands.smx", "l4d2_skill_detect/plugins/l4d2_skill_detect.smx"):
            self.assertIn(needed, harry)
        self.assertIn("extensions/actions.ext.2.l4d2.so", cli.ACTIONS_MEMBERS)
        placed = [dest for _, dest, _ in cli.EXTRA_FILES]
        self.assertEqual(len(placed), len(set(placed)))
        # The reverse friendly fire switch starts off, and never kicks or bans anyone.
        reverse_ff = dict(cli.EXTRA_CONFIGS)["cfg/sourcemod/l4d2_reverse_ff.cfg"]
        for line in ('reverseff_enabled "0"', 'reverseff_survivormaxdmg "0"'):
            self.assertIn(line, reverse_ff)

    def stub_extras(self):
        """install_extras() against a made-up set of downloads. Returns {url: bytes}."""
        names = ("EXTRA_HARRY_FILES", "EXTRA_FILES", "ACTIONS_SHA256", "CLEANER_SHA256")
        saved = {n: getattr(cli, n) for n in names}
        self.addCleanup(lambda: [setattr(cli, n, v) for n, v in saved.items()])
        actions = io.BytesIO()
        with zipfile.ZipFile(actions, "w") as z:
            for name in cli.ACTIONS_MEMBERS:
                z.writestr(name, "actions " + name)
            z.writestr("extensions/actions.ext.2.tf2.so", "another game")
        cleaner = io.BytesIO()
        with tarfile.open(fileobj=cleaner, mode="w:gz") as t:
            info = tarfile.TarInfo(cli.CLEANER_MEMBER)
            info.size = 7
            t.addfile(info, io.BytesIO(b"CLEANER"))
        served = {cli.HARRY_RAW + "bots/plugins/bots.smx": b"BOTS", cli.HARRY_RAW + "bots/data/bots/coop.cfg": b"STOCK",
                  "https://example.invalid/addon.vpk": b"VPK", cli.ACTIONS_ZIP: actions.getvalue(),
                  cli.CLEANER_TGZ: cleaner.getvalue()}
        cli.EXTRA_HARRY_FILES = [("bots/plugins/bots.smx", sha(b"BOTS")), ("bots/data/bots/coop.cfg", sha(b"STOCK"))]
        cli.EXTRA_FILES = [("https://example.invalid/addon.vpk", "addon.vpk", sha(b"VPK"))]
        cli.ACTIONS_SHA256, cli.CLEANER_SHA256 = sha(served[cli.ACTIONS_ZIP]), sha(served[cli.CLEANER_TGZ])
        cli.fetch = lambda url, timeout=60: served[url]
        # What SourceMod's own package leaves behind.
        for name in cli.VOTE_PLUGINS:
            write(os.path.join(cli.SMDIR, "plugins", "disabled", name), "shipped " + name)
        write(os.path.join(cli.SMDIR, "configs", "maplists.cfg"),
              '"MapLists"\n{\n\t"default"\n\t{\n\t\t"target"\t\t"mapcyclefile"\n\t}\n'
              '\t"mapchooser"\n\t{\n\t\t"target"\t\t"default"\n\t}\n}\n')
        return served

    def test_extras_install_all_or_nothing(self):
        served = self.stub_extras()
        good = served["https://example.invalid/addon.vpk"]
        served["https://example.invalid/addon.vpk"] = b"TAMPERED"
        with self.assertRaises(IOError):
            self.run_quiet(cli.install_extras)
        self.assertFalse(os.path.exists(os.path.join(cli.SMDIR, "plugins", "bots.smx")))
        self.assertFalse(os.path.exists(os.path.join(cli.SMDIR, "plugins", "rockthevote.smx")))

        served["https://example.invalid/addon.vpk"] = good
        cli.save_index({"1": {"first_map": "Custom_Start", "maps": ["custom_start", "custom_end"]},
                    "2": {"first_map": "bad name; quit"}})
        self.run_quiet(cli.install_extras)
        sm = cli.SMDIR
        self.assertEqual(read(os.path.join(sm, "plugins", "bots.smx")), "BOTS")
        self.assertEqual(read(os.path.join(sm, "data", "bots", "coop.cfg")), "STOCK")
        self.assertEqual(read(os.path.join(cli.ADDONS, "addon.vpk")), "VPK")
        self.assertEqual(read(os.path.join(sm, "extensions", "actions.ext.2.l4d2.so")),
                         "actions extensions/actions.ext.2.l4d2.so")
        self.assertFalse(os.path.exists(os.path.join(sm, "extensions", "actions.ext.2.tf2.so")))
        self.assertEqual(read(os.path.join(sm, "extensions", "cleaner.ext.2.l4d2.so")), "CLEANER")
        self.assertEqual(read(os.path.join(sm, "extensions", "cleaner.autoload")), "")
        self.assertIn("unable to find gesture", read(os.path.join(sm, "configs", "cleaner.cfg")))
        for name in cli.VOTE_PLUGINS:              # switched on, and still there for SourceMod's updater
            self.assertEqual(read(os.path.join(sm, "plugins", name)), "shipped " + name)
            self.assertTrue(os.path.exists(os.path.join(sm, "plugins", "disabled", name)))
        # Votes offer campaigns, not every chapter: official ones, then what is installed.
        maplists = read(os.path.join(sm, "configs", "maplists.cfg"))
        self.assertIn(f'"file"\t\t"{cli.CAMPAIGN_LIST}"', maplists)
        self.assertIn('"target"\t\t"default"', maplists)
        campaigns = read(os.path.join(cli.GAME, *cli.CAMPAIGN_LIST.split("/"))).split()
        self.assertEqual(campaigns, [m for m, _ in cli.OFFICIAL] + ["custom_start"])

    def test_extras_update_keeps_edited_settings(self):
        served = self.stub_extras()
        self.run_quiet(cli.install_extras)
        reverse_ff = os.path.join(cli.GAME, "cfg", "sourcemod", "l4d2_reverse_ff.cfg")
        self.assertIn('reverseff_enabled "0"', read(reverse_ff))
        edited = {reverse_ff: 'reverseff_enabled "1"\n',
                  os.path.join(cli.SMDIR, "data", "bots", "coop.cfg"): "TUNED",
                  os.path.join(cli.SMDIR, "configs", "cleaner.cfg"): "my own line\n",
                  os.path.join(cli.SMDIR, "configs", "maplists.cfg"): '"MapLists"\n{\n}\n'}
        for path, text in edited.items():
            write(path, text)
        served[cli.HARRY_RAW + "bots/plugins/bots.smx"] = b"BOTS v2"
        cli.EXTRA_HARRY_FILES[0] = ("bots/plugins/bots.smx", sha(b"BOTS v2"))
        self.run_quiet(cli.install_extras)
        self.assertEqual(read(os.path.join(cli.SMDIR, "plugins", "bots.smx")), "BOTS v2")
        for path, text in edited.items():
            self.assertEqual(read(path), text)

    def test_extras_install_without_the_cleaner_download(self):
        served = self.stub_extras()
        del served[cli.CLEANER_TGZ]                # gone upstream
        _, out = self.run_quiet(cli.install_extras)
        self.assertIn("Cleaner", out)
        self.assertIn("was not installed", out)
        self.assertEqual(read(os.path.join(cli.SMDIR, "plugins", "bots.smx")), "BOTS")
        self.assertFalse(os.path.exists(os.path.join(cli.SMDIR, "extensions", "cleaner.autoload")))

    def test_campaign_list_follows_the_installed_maps(self):
        cli.save_index({"1": {"first_map": "one_start"}})          # no SourceMod: nothing to list for
        self.assertFalse(os.path.exists(os.path.join(cli.GAME, *cli.CAMPAIGN_LIST.split("/"))))
        os.makedirs(os.path.join(cli.SMDIR, "configs"))
        cli.save_index({"1": {"first_map": "one_start"}, "2": {"maps": ["two_a", "two_b"]}, "3": {"first_map": "C1M1_Hotel"}})
        campaigns = read(os.path.join(cli.GAME, *cli.CAMPAIGN_LIST.split("/"))).split()
        self.assertEqual(campaigns, [m for m, _ in cli.OFFICIAL] + ["one_start", "two_a"])

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
        on = os.path.join(cli.SMDIR, "plugins", "one.smx")
        off = os.path.join(cli.SMDIR, "plugins", "disabled", "one.smx")
        write(off, "old")
        self.run_quiet(cli.install_harry_plugins)     # 4 slots: refreshed, but stays switched off
        self.assertEqual(read(off), "ONE")
        self.assertFalse(os.path.exists(on))
        self.assertEqual(read(os.path.join(cli.SMDIR, "gamedata", "two.txt")), "TWO")

        cli.set_env("L4D2_SLOTS", "8")                # more slots: refreshed where it is live
        write(on, "old")
        self.run_quiet(cli.install_harry_plugins)
        self.assertEqual(read(on), "ONE")
        self.assertFalse(os.path.exists(off))

    def stub_mod_installs(self, own_fails=False):
        """install_mods() without downloads: the MultiSlots plugins are real, the rest no-ops."""
        names = ("install_metamod", "install_sourcemod", "install_l4dtoolz", "install_left4dhooks",
                 "install_extras", "install_own_plugins")
        saved = {n: getattr(cli, n) for n in names}
        self.addCleanup(lambda: [setattr(cli, n, f) for n, f in saved.items()])
        for n in names:
            setattr(cli, n, lambda *a, **k: None)
        if own_fails:
            def fail():
                raise RuntimeError("could not build the l4d2_admin_extras plugin")
            cli.install_own_plugins = fail
        served = {src: f"new {dest}".encode() for src, dest, _ in cli.HARRY_FILES}
        cli.HARRY_FILES = [(src, dest, sha(served[src])) for src, dest, _ in cli.HARRY_FILES]
        cli.fetch = lambda url, timeout=60: served[url[len(cli.HARRY_RAW):]]
        os.makedirs(os.path.join(cli.ADDONS, "metamod"), exist_ok=True)
        os.makedirs(os.path.join(cli.GAME, "cfg", "sourcemod"), exist_ok=True)

    def plugin_states(self):
        plugins = os.path.join(cli.SMDIR, "plugins")
        return {p: ("on" if os.path.exists(os.path.join(plugins, p)) else "")
                + ("off" if os.path.exists(os.path.join(plugins, "disabled", p)) else "")
                for p in cli.SLOT_PLUGINS}

    def test_update_keeps_multislots_off_on_a_four_player_server(self):
        self.stub_mod_installs()
        self.run_quiet(cli.cmd_slots, ["8", "--no-restart"])
        self.assertEqual(set(self.plugin_states().values()), {"on"})
        self.run_quiet(cli.cmd_slots, ["4", "--no-restart"])
        self.assertEqual(set(self.plugin_states().values()), {"off"})

        saved = cli.subprocess.run
        self.addCleanup(lambda: setattr(cli.subprocess, "run", saved))
        cli.subprocess.run = lambda *a, **k: type("R", (), {"returncode": 0})()
        saved_maps = cli.cmd_updatemaps
        self.addCleanup(lambda: setattr(cli, "cmd_updatemaps", saved_maps))
        cli.cmd_updatemaps = lambda flags: []
        write(os.path.join(cli.DIR, "srcds_run"), "")
        code, _ = self.run_quiet(cli.cmd_update, [])
        self.assertEqual(code, 0)
        self.assertEqual(set(self.plugin_states().values()), {"off"})
        some = cli.SLOT_PLUGINS[0]
        self.assertEqual(read(os.path.join(cli.SMDIR, "plugins", "disabled", some)), f"new plugins/{some}")

    def test_failed_install_switches_nothing_on(self):
        self.stub_mod_installs(own_fails=True)
        with self.assertRaises(SystemExit):
            self.run_quiet(cli.cmd_slots, ["8", "--no-restart"])
        self.assertNotIn("on", "".join(self.plugin_states().values()))
        self.assertIn("L4D2_SLOTS=4", read(cli.ENV_FILE))

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
            if env.get(name) is None and name in ("password", "rcon"):
                os.environ.pop(var, None)       # not passed: keep what server.cfg has
            else:
                os.environ[var] = env.get(name) or ""
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

    def test_set_cvar_replaces_any_spelling_of_the_line(self):
        cases = [
            ("hostname My Server\n", 'hostname "New"\n'),
            ("hostname My Server   // mine\n", 'hostname "New"   // mine\n'),
            ("HostName \"Old\"\r\nsv_lan 0\r\n", 'hostname "New"\r\nsv_lan 0\r\n'),
            ("\thostname\t\"Old\" // x\n", '\thostname "New" // x\n'),
            ("hostname\n", 'hostname "New"\n'),
            # A command chained on the same line is kept, not swallowed.
            ("hostname \"a\";sv_lan 0\n", 'hostname "New";sv_lan 0\n'),
            ("hostname a ; sv_lan 0 // x\n", 'hostname "New" ; sv_lan 0 // x\n'),
            ('hostname"Old"\n', 'hostname "New"\n'),
        ]
        for before, after in cases:
            self.assertEqual(cli.set_cvar(before, "hostname", "New"), after, before)
        # Other cvars and commented-out lines are not the same setting.
        text = "hostname_extra 1\n// hostname Old\n"
        self.assertEqual(cli.set_cvar(text, "hostname", "New"), text + 'hostname "New"\n')

    def test_unpassed_passwords_are_left_alone(self):
        original = ("hostname My Server\r\nsv_password hunter2 // friends\r\n"
                    "rcon_password secret123\r\n")
        write(cli.CFG, original)
        self.init(hostname="New", difficulty="Hard")
        text = read(cli.CFG)
        self.assertIn("sv_password hunter2 // friends", text)
        self.assertIn("rcon_password secret123", text)
        self.assertNotIn('rcon_password ""', text)
        self.assertEqual(text.lower().count("hostname"), 1)
        self.assertIn('hostname "New"', text)
        # Passing them (even empty) still changes them.
        self.init(hostname="New", password="", rcon="")
        text = read(cli.CFG)
        self.assertIn('sv_password ""', text)
        self.assertIn('rcon_password ""', text)
        self.assertEqual(text.count("rcon_password"), 1)

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

    def test_slots_set_the_quick_rescue_cvars_once(self):
        cli.install_mods = lambda update=False: None
        os.makedirs(os.path.join(cli.SMDIR, "plugins", "disabled"), exist_ok=True)
        os.makedirs(os.path.join(cli.GAME, "cfg", "sourcemod"), exist_ok=True)
        wanted = ["sm_cvar rescue_min_dead_time 0", "sm_cvar rescue_distance 0", "sm_cvar rescue_interval 0.5",
                  "sm_cvar rescue_range 3000", "sm_cvar rescue_spawn_range 5000", "sm_cvar rescue_spawn_flow 2000"]
        for count in ("8", "10"):                       # applying again must not stack the lines
            self.run_quiet(cli.cmd_slots, [count, "--no-restart"])
            lines = read(cli.CFG).splitlines()
            self.assertEqual([line for line in lines if "rescue_" in line], wanted)
        self.run_quiet(cli.cmd_slots, ["4", "--no-restart"])
        self.assertNotIn("rescue_", read(cli.CFG))      # sm_cvar needs the mods; gone with the block

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

    def run_main(self, argv, cwd_files=()):
        """main() as if started by root in a folder holding cwd_files."""
        here = os.path.join(self.root, "cwd")
        os.makedirs(here, exist_ok=True)
        for name in cwd_files:
            write(os.path.join(here, name), "")
        real_chdir, old_cwd = os.chdir, os.getcwd()
        saved = (cli.sys.argv, cli.os.geteuid, cli.init_paths, cli.drop_privileges)
        self.addCleanup(lambda: (setattr(cli.sys, "argv", saved[0]), setattr(cli.os, "geteuid", saved[1]),
                                 setattr(cli, "init_paths", saved[2]), setattr(cli, "drop_privileges", saved[3]),
                                 setattr(cli.os, "chdir", real_chdir), real_chdir(old_cwd)))
        real_chdir(here)
        cli.os.chdir = lambda path: None
        cli.os.geteuid = lambda: 0
        cli.init_paths = cli.drop_privileges = lambda: None
        cli.sys.argv = ["l4d2"] + argv
        return self.run_quiet(cli.main)

    def test_files_in_the_current_folder_do_not_change_the_command(self):
        seen = {}
        for name in ("cmd_slots", "cmd_addmap", "cmd_init_config", "status_text"):
            saved = getattr(cli, name)
            self.addCleanup(lambda name=name, saved=saved: setattr(cli, name, saved))
            setattr(cli, name, lambda args=None, name=name: seen.__setitem__(name, args) or "")
        files = ("init-config", "slots", "addmap", "8", "status", "map.vpk")
        self.run_main(["init-config"], files)
        self.assertEqual(seen.pop("cmd_init_config"), [])
        self.run_main(["slots", "8", "--no-restart"], files)
        self.assertEqual(seen.pop("cmd_slots"), ["8", "--no-restart"])
        code, _ = self.run_main(["status"], files)
        self.assertIsNone(code)
        self.assertIn("status_text", seen)
        # addmap still takes files from the folder it was run in.
        here = os.path.join(self.root, "cwd")
        self.run_main(["addmap", "map.vpk", "123456", "--no-restart"], files)
        self.assertEqual(seen.pop("cmd_addmap"), [os.path.join(here, "map.vpk"), "123456", "--no-restart"])

    def test_unknown_command_fails(self):
        code, out = self.run_quiet(cli.dispatch, "/tmp/init-config", [])
        self.assertEqual(code, 2)
        self.assertIn("Unknown command", out)
        self.assertNotIn("Usage:", out)
        for word in ("help", "--help", "-h"):
            code, out = self.run_quiet(cli.dispatch, word, [])
            self.assertFalse(code)
            self.assertIn("Usage:", out)
        code, _ = self.run_main(["no-such-command"])
        self.assertEqual(code, 2)

    def test_every_documented_command_is_dispatched(self):
        source = read(os.path.join(HERE, "..", "cli", "l4d2.py"))
        body = source[source.index("def dispatch("):source.index("def main(")]
        for name in ("status", "start", "stop", "restart", "console", "logs", "config", "update", "info",
                     "addmap", "maps", "changemap", "removemap", "updatemaps", "share", "slots", "admin",
                     "modcheck", "menu"):
            self.assertIn(f'"{name}"', body, name)
            self.assertIn(name, cli.HELP)


class ReviewFixTests(CliCase):
    def patch(self, obj, name, value):
        old = getattr(obj, name)
        self.addCleanup(setattr, obj, name, old)
        setattr(obj, name, value)

    def test_update_starts_the_server_again_after_a_permission_error(self):
        class Result:
            returncode = 0

        def refuse(update=False):
            raise PermissionError(13, "Permission denied", "addons/metamod")

        self.patch(cli.subprocess, "run", lambda *a, **k: Result())
        self.patch(cli, "cmd_updatemaps", lambda flags: [])
        self.patch(cli, "mods_installed", lambda: True)
        cli.install_mods = refuse
        write(os.path.join(cli.DIR, "srcds_run"), "")
        code, out = self.run_quiet(cli.cmd_update, [])
        self.assertEqual(code, 1)
        self.assertEqual(self.systemctl_calls, ["stop", "start"])
        self.assertIn("sudo chown -R -h", out)

    def test_an_unreadable_server_cfg_is_not_replaced(self):
        os.symlink(cli.CFG, cli.CFG)              # opening it fails, and not with "no such file"
        for change in (lambda: cli.set_cfg_block(["sv_maxplayers 8"]), lambda: cli.set_cfg_block(None),
                       lambda: cli.cmd_init_config([]), lambda: cli.set_cvars_in_file(cli.CFG, {"a": 1})):
            with self.assertRaises(OSError):
                self.run_quiet(change)
            self.assertTrue(os.path.islink(cli.CFG))
        os.remove(cli.CFG)
        cli.set_cfg_block(["sv_maxplayers 8"])    # a missing file is still simply created
        self.assertIn("sv_maxplayers 8", read(cli.CFG))

    def test_addmap_works_when_addons_is_on_another_disk(self):
        real = os.replace

        def replace(src, dest):
            if os.path.dirname(src) != os.path.dirname(dest):
                raise OSError(errno.EXDEV, "Invalid cross-device link")
            return real(src, dest)

        self.patch(cli.os, "replace", replace)
        src = os.path.join(self.root, "far.vpk")
        make_vpk(src, "far_m1")
        added, _ = self.run_quiet(cli.add_targets, [src])
        self.assertEqual([m["first_map"] for m in added], ["far_m1"])
        self.assertEqual(os.listdir(cli.ADDONS), ["far.vpk"])
        self.assertEqual(stat.S_IMODE(os.stat(os.path.join(cli.ADDONS, "far.vpk")).st_mode), 0o644)
        self.assertEqual([n for n in os.listdir(cli.HOME) if n.startswith(".l4d2-tmp")], [])

    def test_removemap_with_the_same_map_named_twice(self):
        make_vpk(os.path.join(cli.ADDONS, "123456.vpk"))
        write(cli.INDEX, json.dumps({"123456": {"campaign": "X", "id": "123456", "file": "123456.vpk",
                                                "maps": ["testmap"]}}))
        _, out = self.run_quiet(cli.cmd_removemap, ["123456", "123456.vpk", "--no-restart"])
        self.assertEqual(cli.load_index(), {})
        self.assertFalse(os.path.exists(os.path.join(cli.ADDONS, "123456.vpk")))
        self.assertIn("123456.vpk: not found", out)

    def test_changemap_no_restart_never_restarts(self):
        sent = []
        cli.server_pid = lambda: 1
        self.patch(cli, "pending_addons", lambda: ["new.vpk"])
        self.patch(cli, "send_console", lambda command: sent.append(command) or True)
        _, out = self.run_quiet(cli.cmd_changemap, ["c2m1_highway", "--default", "--no-restart"])
        self.assertEqual(self.systemctl_calls, [])
        self.assertEqual(sent, ["changelevel c2m1_highway"])
        self.assertIn("L4D2_MAP=c2m1_highway", read(cli.ENV_FILE))
        self.assertIn("l4d2 restart", out)
        # A map that is itself waiting for a restart can't be switched to: say so, and fail.
        write(cli.INDEX, json.dumps({"9": {"campaign": "New", "file": "new.vpk", "maps": ["New_M1"]}}))
        code, out = self.run_quiet(cli.cmd_changemap, ["new_m1", "--default", "--no-restart"])
        self.assertEqual(code, 1)
        self.assertIn("Not switched", out)
        self.assertEqual(len(sent), 1)
        self.assertIn("L4D2_MAP=new_m1", read(cli.ENV_FILE))
        self.run_quiet(cli.cmd_changemap, ["c3m1_plankcountry", "--default"])   # without the flag: as before
        self.assertEqual(self.systemctl_calls, ["restart"])


if __name__ == "__main__":
    unittest.main()
