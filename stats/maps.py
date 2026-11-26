"""Readable names for map files: official campaigns by their code, custom ones from maps.json."""

import json
import os
import re
import time

CAMPAIGNS = {
    "c1": "Dead Center", "c2": "Dark Carnival", "c3": "Swamp Fever", "c4": "Hard Rain",
    "c5": "The Parish", "c6": "The Passing", "c7": "The Sacrifice", "c8": "No Mercy",
    "c9": "Crash Course", "c10": "Death Toll", "c11": "Dead Air", "c12": "Blood Harvest",
    "c13": "Cold Stream", "c14": "The Last Stand",
}
_OFFICIAL_RE = re.compile(r"(c\d{1,2})m(\d)_")
_WORKSHOP_ID_RE = re.compile(r"[0-9]{1,20}")
_FILE_RE = re.compile(r"[\w .()+\-]{1,100}\.vpk", re.IGNORECASE)
MAX_INDEX_BYTES = 2 * 1024 * 1024


def workshop_links(workshop_id):
    """Where to subscribe: the page in the Steam app, and the same page on the web."""
    return {"steam": f"steam://url/CommunityFilePage/{workshop_id}",
            "web": f"https://steamcommunity.com/sharedfiles/filedetails/?id={workshop_id}"}


class MapNames:
    """Looks names up in maps.json, re-read at most once a minute (the game user writes it)."""

    def __init__(self, path, clock=time.time):
        self.path = path
        self.clock = clock
        self._names = {}
        self._addons = []
        self._loaded = None
        self._stamp = None

    def _custom(self):
        now = self.clock()
        if self._loaded is not None and now - self._loaded < 60:
            return self._names
        self._loaded = now
        try:
            stat = os.stat(self.path)
            stamp = (stat.st_mtime, stat.st_size)
            if stamp == self._stamp:
                return self._names
            if stat.st_size > MAX_INDEX_BYTES:
                raise ValueError("too big")
            with open(self.path, encoding="utf-8") as fh:
                index = json.load(fh)
        except (OSError, ValueError, RecursionError):
            # The game user's file: unreadable, malformed or nested too deep to
            # parse, the last good list stays.
            return self._names
        names, addons = {}, []
        if isinstance(index, dict):
            for item in index.values():
                if not isinstance(item, dict):
                    continue
                title = item.get("campaign") or item.get("title")
                maps = item.get("maps")
                if not isinstance(title, str) or not title.strip():
                    continue
                title = " ".join(title.split())[:80]
                chapters = [m for m in maps if isinstance(m, str)] if isinstance(maps, list) else []
                # maps.json is the game user's file: the link is built here from a
                # checked id, never taken from its "url" field.
                workshop = str(item.get("id") or "")
                if item.get("source") != "workshop" or not _WORKSHOP_ID_RE.fullmatch(workshop):
                    workshop = None
                file = os.path.basename(str(item.get("file") or ""))
                addons.append({"title": title, "chapters": len(chapters), "workshop": workshop,
                               "file": file if not workshop and _FILE_RE.fullmatch(file) else None})
                for number, name in enumerate(chapters, start=1):
                    names[name.lower()] = (title, number if len(chapters) > 1 else None, workshop)
        addons.sort(key=lambda a: a["title"].lower())
        self._names, self._addons, self._stamp = names, addons, stamp
        return names

    def describe(self, name):
        """{"campaign": ..., "chapter": n or None} for a map file name."""
        match = _OFFICIAL_RE.match(name or "")
        if match and match.group(1) in CAMPAIGNS:
            return {"campaign": CAMPAIGNS[match.group(1)], "chapter": int(match.group(2))}
        found = self._custom().get((name or "").lower())
        if found:
            return {"campaign": found[0], "chapter": found[1]}
        return {"campaign": name or "?", "chapter": None}

    def addons(self):
        """The custom maps players need, with where to subscribe to each."""
        self._custom()
        return [{**a, "links": workshop_links(a["workshop"]) if a["workshop"] else None} for a in self._addons]

    def workshop_for(self, name):
        """The Workshop id of the custom map a map file belongs to, if it came from there."""
        found = self._custom().get((name or "").lower())
        return found[2] if found else None
