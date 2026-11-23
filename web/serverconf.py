"""Reading and rewriting the files the game server is configured by.

The panel runs as root but these files live in directories the unprivileged
game user can write to, so every write goes through a fresh O_EXCL temp file
and a rename: we never open an existing path for writing and never follow a
symlink the game user might have planted.
"""

import glob
import os
import re
import stat
import tempfile
import time

MAX_CFG_BYTES = 64 * 1024
KEEP_BACKUPS = 10

_UNSAFE_RE = re.compile(r'[\x00-\x1f\x7f"\\;]')


def clean_value(text, limit):
    """Make text safe inside a quoted cvar value, in server.cfg and on the console."""
    return _UNSAFE_RE.sub("", text or "").strip()[:limit].strip()


def _cvar_re(name):
    return re.compile(
        r'^(?P<indent>[ \t]*)' + re.escape(name)
        + r'[ \t]+(?:"(?P<quoted>[^"\n]*)"|(?P<bare>[^\s/]+))(?P<tail>[ \t]*(?://.*)?)$',
        re.MULTILINE,
    )


def read_cvar(text, name):
    """Value of the last `name "value"` line, or None when it isn't set."""
    value = None
    for match in _cvar_re(name).finditer(text or ""):
        value = match.group("quoted") if match.group("quoted") is not None else match.group("bare")
    return value


def set_cvar(text, name, value):
    """Return text with every `name ...` line set to value, appending if absent."""
    pattern = _cvar_re(name)
    line = f'{name} "{value}"'
    if pattern.search(text or ""):
        return pattern.sub(lambda m: m.group("indent") + line + m.group("tail"), text)
    text = text or ""
    if text and not text.endswith("\n"):
        text += "\n"
    return text + line + "\n"


def open_regular(path):
    """Open path for reading only if it is a plain file.

    The game user owns these directories: a symlink there must not make root
    read some other file, and a FIFO or device must not hang or flood us.
    """
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError(f"{path} is not a regular file")
        return os.fdopen(fd, "r", errors="replace")
    except BaseException:
        os.close(fd)
        raise


def read_text(path, limit=MAX_CFG_BYTES * 4):
    try:
        with open_regular(path) as fh:
            return fh.read(limit)
    except OSError:
        return ""


def read_tail(path, limit=256 * 1024):
    """The last `limit` bytes of a file, as text. Raises OSError."""
    with open_regular(path) as fh:
        fh.seek(0, os.SEEK_END)
        fh.seek(max(0, fh.tell() - limit))
        return fh.read(limit)


def atomic_write(path, text):
    """Replace path with text, keeping its owner and mode."""
    directory = os.path.dirname(path)
    try:
        # lstat, and permissions set through the descriptor below: the game
        # user can swap names in this directory at any moment, so nothing
        # here may follow a link.
        info = os.lstat(path)
        if stat.S_ISREG(info.st_mode):
            mode, uid, gid = info.st_mode & 0o777, info.st_uid, info.st_gid
        else:
            mode, uid, gid = 0o640, None, None
    except OSError:
        mode, uid, gid = 0o640, None, None
    fd, tmp = tempfile.mkstemp(prefix=".panel-", dir=directory)
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(text)
            fh.flush()
            os.fchmod(fh.fileno(), mode)
            if uid is not None and os.geteuid() == 0:
                os.fchown(fh.fileno(), uid, gid)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def backup(path):
    """Copy path to path.bak.<time> and keep only the newest few backups."""
    text = read_text(path)
    if not text:
        return
    target = f"{path}.bak.{int(time.time())}"
    if not os.path.lexists(target):
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write(text)
    backups = sorted(glob.glob(glob.escape(path) + ".bak.*"), key=_backup_time)
    for old in backups[:-KEEP_BACKUPS]:
        try:
            os.unlink(old)
        except OSError:
            pass


def _backup_time(path):
    suffix = path.rsplit(".", 1)[-1]
    return int(suffix) if suffix.isdigit() else 0


def set_env_value(path, key, value):
    """Set KEY=value in a shell-style env file (the format /etc/l4d2.env uses)."""
    # bash sources this file as root, so values are allowlisted, not escaped.
    if not re.fullmatch(r"[A-Z0-9_]+", key) or not re.fullmatch(r"[A-Za-z0-9_.:/-]*", value):
        raise ValueError(f"refusing to write {key}={value!r}")
    lines = read_text(path).splitlines()
    for index, line in enumerate(lines):
        if line.startswith(key + "="):
            lines[index] = f"{key}={value}"
            break
    else:
        lines.append(f"{key}={value}")
    atomic_write(path, "\n".join(lines) + "\n")


def update_info(path, hostname=None, password=None):
    """Keep `l4d2 info` truthful after the name or password changes. Best effort."""
    text = read_text(path)
    if not text:
        return
    if hostname is not None:
        text = re.sub(r"^(Server name\s*: ).*$", lambda m: m.group(1) + hostname, text, flags=re.M)
    if password is not None:
        text = re.sub(r"^(Password\s*: ).*$", lambda m: m.group(1) + (password or "(none)"),
                      text, flags=re.M)
        # The copy-paste block: "In-game console:  password X" followed by "connect ...".
        text = re.sub(r"^(In-game console:[ \t]*)password [^\n]*\n[ \t]*(?=connect )",
                      lambda m: m.group(1), text, flags=re.M)
        if password:
            text = re.sub(r"^(In-game console:[ \t]*)(?=connect )",
                          lambda m: f"{m.group(1)}password {password}\n{' ' * len(m.group(1))}",
                          text, flags=re.M)
    try:
        atomic_write(path, text)
    except OSError:
        pass
