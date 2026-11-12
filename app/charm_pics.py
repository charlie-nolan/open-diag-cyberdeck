"""
CHARM page pictures, fetched from the home PC (tools/charm_pic_server.py, over Tailscale) and kept in
cache/charm/ so a page seen once shows its pictures offline afterwards. CHARM sections list their pictures
in section.images as the page's own "/images/..." src; FSM sections list plain file names instead.
"""
import hashlib
import json
import os
import sqlite3
import time
import urllib.parse
import urllib.request

PREFIX = "/images/"
_down = {"until": 0.0, "why": ""}   # after a failed fetch, don't try the PC again for a minute


def is_charm(name):
    return isinstance(name, str) and name.startswith(PREFIX)


def cache_path(cache_dir, name):
    h = hashlib.sha1(name.encode("utf-8")).hexdigest()
    return os.path.join(cache_dir, "charm", h[:2], h + ".img")


def _db(db_path):
    """The knowledge DB, read-only, when it stores pictures inside it (a one-car DB: kb/car_db.py images)."""
    if not db_path or not os.path.isfile(os.path.expanduser(db_path)):
        return None
    try:
        c = sqlite3.connect(f"file:{os.path.expanduser(db_path)}?mode=ro", uri=True)
        if c.execute("SELECT 1 FROM sqlite_master WHERE name='picture'").fetchone():
            return c
        c.close()
    except sqlite3.Error:
        pass
    return None


def from_db(db_path, cache_dir, key, dest=None):
    """Write a picture stored in the DB to the cache (or dest) and return its path, else None.
    key: a CHARM "/images/..." name, or "fsm/<doc id>/<stem>" for a factory-manual figure."""
    c = _db(db_path)
    if c is None:
        return None
    try:
        row = c.execute("SELECT data FROM picture WHERE key=?", (key,)).fetchone()
    finally:
        c.close()
    if not row:
        return None
    p = dest or cache_path(cache_dir, key)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p + ".part", "wb") as f:
        f.write(row[0])
    os.replace(p + ".part", p)
    return p


def fsm_figure(db_path, cache_dir, doc_id, name):
    """A factory-manual figure stored in the DB, unpacked to the cache once. Path, or None."""
    key = f"fsm/{doc_id}/{os.path.splitext(name)[0]}"
    p = cache_path(cache_dir, key)
    return p if os.path.isfile(p) else from_db(db_path, cache_dir, key)


def pc_down():
    """Why the PC can't be asked right now, or ''."""
    return _down["why"] if time.time() < _down["until"] else ""


def fetch(url, cache_dir, names, timeout=8, db=None):
    """Download the pictures not cached yet (from the DB itself when it stores them, else the PC).
    Returns (paths that exist now, problem or '')."""
    have, why = [], ""
    for n in names:
        p = cache_path(cache_dir, n)
        if os.path.isfile(p) or from_db(db, cache_dir, n):
            have.append(p)
            continue
        if not url:
            why = "picture not in this car's DB, and no picture server set"
            continue
        if pc_down():
            why = pc_down()
            continue
        try:
            q = urllib.parse.urlencode({"k": n})
            with urllib.request.urlopen(f"{url.rstrip('/')}/img?{q}", timeout=timeout) as r:
                data = r.read()
            os.makedirs(os.path.dirname(p), exist_ok=True)
            tmp = p + ".part"
            with open(tmp, "wb") as f:
                f.write(data)
            os.replace(tmp, p)
            have.append(p)
        except urllib.error.HTTPError as e:
            why = "picture not on the PC" if e.code == 404 else f"PC said {e.code}"
        except OSError:
            why = "PC not reachable"
            _down.update(until=time.time() + 60, why=why)
    return have, why


def reading(url, name, timeout=8, db=None):
    """The PC's reading of one chart picture: dict(state=done|skipped|queued|reading|error, ...).
    A reading stored in a one-car DB answers first. Asking the PC queues it there (ahead of the background
    list). Raises OSError when the PC can't be reached."""
    c = _db(db)
    if c is not None:
        try:
            row = c.execute("SELECT state, text, unverified, checked, model FROM picture_read WHERE key=?",
                            (name,)).fetchone()
        except sqlite3.Error:
            row = None
        finally:
            c.close()
        if row:
            state, text, unverified, checked, model = row
            return {"state": state, "text": text, "model": model, "checked": bool(checked),
                    "unverified": None if unverified is None else json.loads(unverified)}
    if not url:
        raise OSError("no picture server set")
    q = urllib.parse.urlencode({"k": name})
    with urllib.request.urlopen(f"{url.rstrip('/')}/read?{q}", timeout=timeout) as r:
        return json.loads(r.read())
