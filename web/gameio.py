"""File access under the game folder, done with the game user's identity.

The panel runs as root, but server.cfg, console.log, maps.json and
SERVER-INFO.txt live in directories the unprivileged game user owns. Any
component of those paths - the server dir, left4dead2/, cfg/ or the file
itself - can be swapped for a symlink at any moment, so no amount of lstat or
O_NOFOLLOW in a root process makes them safe. Instead every such access runs in
a short-lived child process started as the game user (uid, gid and
supplementary groups set by subprocess before exec, so no other panel thread is
affected): the kernel then refuses anything the game user could not already do
themselves, and a server dir that is legitimately a symlink to another disk
keeps working.

When the panel is not root (tests, or run by hand as the game user) there is
no privilege to drop and the same operations run in-process.
"""

import errno
import json
import os
import pwd
import subprocess
import sys

from . import serverconf

CHILD_TIMEOUT = 15
_PACKAGE_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# -I: ignore PYTHON* variables, the user site dir and the current directory;
# the package root is added explicitly (it is root-owned and world-readable).
_CHILD_CODE = "import sys; sys.path.insert(0, sys.argv[1]); from web import gameio; gameio.child_main()"


def _save(path, text):
    """Back up the current file, then replace it. Nothing is written when it can't be read."""
    serverconf.backup(path)
    serverconf.atomic_write(path, text)


def _exists(paths):
    return [os.path.exists(path) for path in paths]


def _vpk_mtimes(directory):
    with os.scandir(directory) as entries:
        return [entry.stat().st_mtime for entry in entries if entry.name.lower().endswith(".vpk")]


OPS = {
    "read": serverconf.read_text,
    "tail": serverconf.read_tail,
    "save": _save,
    "update_info": serverconf.update_info,
    "exists": _exists,
    "vpk_mtimes": _vpk_mtimes,
}


def identity(user, euid=None):
    """(uid, gid, groups) to do game-file I/O as, or None to do it in-process.

    Raises OSError when the panel is root but the game user doesn't exist:
    it must never fall back to doing the I/O as root.
    """
    if (os.geteuid() if euid is None else euid) != 0:
        return None
    try:
        pw = pwd.getpwnam(user)
    except KeyError:
        raise OSError(errno.EPERM, f"game user '{user}' does not exist") from None
    return pw.pw_uid, pw.pw_gid, os.getgrouplist(pw.pw_name, pw.pw_gid)


def child_invocation(ident):
    """argv and subprocess.run keyword arguments for one child, as ident (or as ourselves for None)."""
    argv = [sys.executable, "-I", "-c", _CHILD_CODE, _PACKAGE_ROOT]
    kwargs = {"cwd": "/", "env": {"PATH": "/usr/bin:/bin", "LC_ALL": "C.UTF-8"},
              "umask": 0o022, "close_fds": True}
    if ident is not None:
        uid, gid, groups = ident
        kwargs.update(user=uid, group=gid, extra_groups=groups)
    return argv, kwargs


def run_child(ident, op, args):
    """Run OPS[op](**args) in a child process; return its result or raise its OSError."""
    argv, kwargs = child_invocation(ident)
    request = json.dumps({"op": op, "args": args})
    try:
        done = subprocess.run(argv, input=request, capture_output=True, text=True,
                              encoding="utf-8", timeout=CHILD_TIMEOUT, **kwargs)
        reply = json.loads(done.stdout)
    except (OSError, subprocess.SubprocessError, ValueError) as e:
        raise OSError(errno.EIO, f"file helper failed: {e}") from None
    if not isinstance(reply, dict):
        raise OSError(errno.EIO, "file helper failed")
    if "error" in reply:
        try:
            code, message, filename = reply["error"]
            code, message = int(code), str(message)
        except (TypeError, ValueError):
            code, message, filename = errno.EIO, "file helper failed", None
        raise OSError(code, message, filename)
    return reply.get("result")


def child_main():
    """Entry point of the child: one JSON request on stdin, one JSON reply on stdout."""
    try:
        request = json.loads(sys.stdin.read())
        reply = {"result": OPS[request["op"]](**request["args"])}
    except OSError as e:
        reply = {"error": [e.errno or errno.EIO, e.strerror or str(e), e.filename]}
    except Exception as e:
        reply = {"error": [errno.EIO, f"{type(e).__name__}: {e}", None]}
    sys.stdout.write(json.dumps(reply))
    sys.stdout.flush()
    os._exit(0)


def call(user, op, **args):
    """Do one game-file operation as the game user `user`. Raises OSError."""
    ident = identity(user)
    if ident is None:
        return OPS[op](**args)
    return run_child(ident, op, args)
