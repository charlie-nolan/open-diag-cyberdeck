"""Row52 search links a person opens (QR code or button). Row52's terms forbid robots, so nothing here fetches it.

The search URL takes a make id, an optional model id, a year or a year range ("2002-2008"), a ZIP and a distance in
miles. Ids come from row52_ids.json (copied from searches made by hand). Without a model id the link lists every car of
that make in those years, which is how all donor cars fit in one link: Row52 takes one model per search.
"""
import json
import os
import urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = "https://row52.com/Search/"


def ids(path=None):
    try:
        with open(path or os.path.join(HERE, "row52_ids.json")) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {"makes": {}, "models": {}}


def search_url(make, zip_code, years=(), model=None, distance=50, table=None):
    """A Row52 search link, or None when the make's id isn't known (then use the plain home page)."""
    t = table or ids()
    make_id = next((v for k, v in t.get("makes", {}).items() if k.lower() == (make or "").lower()), None)
    if not make_id:
        return None
    models = next((v for k, v in t.get("models", {}).items() if k.lower() == (make or "").lower()), {})
    model_id = next((v for k, v in models.items() if model and k.lower() == model.lower()), "") if model else ""
    ys = sorted(set(int(y) for y in years))
    year = "" if not ys else str(ys[0]) if ys[0] == ys[-1] else f"{ys[0]}-{ys[-1]}"
    q = {"YMMorVin": "YMM", "Year": year, "ZipCode": zip_code, "Page": 1, "ModelId": model_id, "MakeId": make_id,
         "IsVin": "false", "Distance": distance}
    return f"{BASE}?{urllib.parse.urlencode(q)}"


def learn(link, make, model=None, path=None):
    """Save the MakeId (and ModelId, when the link has one) of a Row52 search link the user pasted, under make and
    model. Returns what was saved; ValueError when the link isn't a Row52 search with a MakeId. The VIN boxes and
    anything else in the link are ignored."""
    u = urllib.parse.urlparse((link or "").strip())
    if "row52.com" not in u.netloc.lower():
        raise ValueError("not a row52.com link")
    q = {k.lower(): v[0] for k, v in urllib.parse.parse_qs(u.query).items() if v and v[0]}
    if not q.get("makeid", "").isdigit():
        raise ValueError("the link has no MakeId: pick a make on Row52 before copying the link")
    path = path or os.path.join(HERE, "row52_ids.json")
    t = ids(path)
    t.setdefault("makes", {})[make] = int(q["makeid"])
    got = {"make": make, "MakeId": int(q["makeid"])}
    if model and q.get("modelid", "").isdigit():
        t.setdefault("models", {}).setdefault(make, {})[model] = int(q["modelid"])
        got.update(model=model, ModelId=int(q["modelid"]))
    with open(path, "w") as f:
        json.dump(t, f, indent=2)
        f.write("\n")
    return got
