"""
CHARM page pictures, fetched from the home PC (tools/charm_pic_server.py, over Tailscale) and kept in
cache/charm/ so a page seen once shows its pictures offline afterwards. CHARM sections list their pictures
in section.images as the page's own "/images/..." src; FSM sections list plain file names instead.
"""
import hashlib
import os
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


def pc_down():
    """Why the PC can't be asked right now, or ''."""
    return _down["why"] if time.time() < _down["until"] else ""


def fetch(url, cache_dir, names, timeout=8):
    """Download the pictures not cached yet. Returns (paths that exist now, problem or '')."""
    have, why = [], ""
    for n in names:
        p = cache_path(cache_dir, n)
        if os.path.isfile(p):
            have.append(p)
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


def reading(url, name, timeout=8):
    """The PC's reading of one chart picture: dict(state=done|skipped|queued|reading|error, ...).
    Asking queues it on the PC (ahead of the background list). Raises OSError when the PC can't be reached."""
    import json
    q = urllib.parse.urlencode({"k": name})
    with urllib.request.urlopen(f"{url.rstrip('/')}/read?{q}", timeout=timeout) as r:
        return json.loads(r.read())
