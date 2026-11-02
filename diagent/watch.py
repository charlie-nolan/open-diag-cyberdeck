"""watch_live: sample live values while the user does something, judged in Python sample by sample.

Every sample goes through app/telemetry_ref.py (the shared module): normalize() into canonical units, then
judge() against the zone for this car under the condition the readings show. Triggers ('wait until <param> is
above <value> for <n> s', 'flag a change in <param> larger than <value>') are evaluated here, so the screen can
say "hold it... got it" without a model call per sample. Each session is stored in the case DB (table watch)
so it can be replayed offline: replay() feeds the stored raw readings through the same path again."""
import json
import math
import time

import telemetry_ref as T

CONTEXT = ("rpm", "ect", "speed")      # what telemetry_ref.conditions() needs to know the operating condition


UNTIL = __import__("re").compile(
    r"until (?:the )?(.+?) (?:is|reaches|becomes|rises to|drops to)\s+(above|below|more than|less than|over|under|"
    r"higher than|lower than|greater than|smaller than)\s+([+-]?\d+(?:\.\d+)?)\s*(°C|°F|kPa|rpm|V|%|km/h)", __import__("re").I)


def until_trigger(step_text, tdb):
    """A trigger from the step's own words ('... until <param> is above <value>'), in the param's canonical
    unit, or None. The value is the manual's, so it is cited by the step."""
    import re
    from . import live, units
    m = UNTIL.search(step_text or "")
    if not m:
        return None
    key = live.param_for(m.group(1), tdb)
    if not key:
        return None
    u = units.norm_unit(m.group(4))
    canon = T.CANON.get(key, (None, None))[1]
    cu = units.norm_unit(canon) if canon else None
    if not u or not cu or u[0] != cu[0]:
        return None
    op = "above" if re.match(r"above|more|over|higher|greater", m.group(2), re.I) else "below"
    return {"key": key, "op": op, "value": float(m.group(3)) * u[1] / cu[1], "for_s": 0, "quote": m.group(0)}


def tcar(car):
    """The car dict telemetry_ref expects."""
    return {"make": car.get("make"), "model": car.get("model"), "year": car.get("year"), "engine": car.get("engine"),
            "vin": car.get("vin"), "ecu_id": car.get("ecu") or "", "db": car.get("telemetry_db") or ""}


def raw_for(key, value, unit):
    """(source, raw reading) for a canonical key: its J1979 PID when it has one, else its SSM id."""
    if key in T.CANON:
        _, _, pid, ssm, _ = T.CANON[key]
        if pid:
            return "obd01", {"id": pid, "value": value, "unit": unit}
        if ssm:
            return "ssm", {"id": ssm[0], "value": value, "unit": unit}
    if "." in key:
        src, ident = key.split(".", 1)
        return src, {"id": ident, "value": value, "unit": unit}
    return None, None


def _normalize(groups, t, db):
    out = []
    for source, raws in groups.items():
        out += T.normalize(raws, source, t=t, db=db)
    return out


class Trigger:
    """{"key", "op": "above" | "below" | "change", "value", "for_s"} in the key's canonical unit."""

    def __init__(self, spec):
        self.s = spec or {}
        self.since, self.base, self.met_at = None, None, None

    def update(self, t, values):
        k = self.s.get("key")
        if not k or k not in values or self.met_at is not None:
            return False
        v = values[k]
        if self.base is None:
            self.base = v
        op, x = self.s.get("op"), float(self.s.get("value", 0))
        ok = (v > x if op == "above" else v < x if op == "below" else abs(v - self.base) > x if op == "change" else False)
        if not ok:
            self.since = None
            return False
        self.since = t if self.since is None else self.since
        if t - self.since >= float(self.s.get("for_s") or 0):
            self.met_at = t
            return True
        return False


def run(car, reader, keys, seconds, trigger=None, human=None, period=0.5, db=None):
    """Sample until seconds pass or the trigger is met. Returns (samples, summary). samples: [{t, raw, judged}]."""
    db = db or car.get("telemetry_db")
    want = list(dict.fromkeys(list(keys) + [k for k in CONTEXT if k not in keys]))
    trig = Trigger(trigger)
    samples, t0 = [], reader.clock()
    while reader.clock() - t0 <= seconds:
        t = reader.clock()
        groups = reader.sample(want)
        if groups is None:
            break                                    # the recording / the car stopped answering
        norm = _normalize(groups, t, db)
        judged = T.judge(norm, tcar(car), None, db)
        values = {j["key"]: j["value"] for j in judged}
        met = trig.update(t - t0, values)
        samples.append({"t": round(t - t0, 3), "raw": {s: r for s, r in groups.items()},
                        "judged": [{k: j[k] for k in ("key", "value", "unit", "status", "low", "high", "citation",
                                                      "condition")} for j in judged]})
        if human is not None:
            human.strip(samples[-1]["t"], [j for j in judged if j["key"] in keys], trig.s if trigger else None, met)
        if met:
            break
        reader.wait(period)
    return samples, summarize(samples, keys, trig)


def summarize(samples, keys, trig=None):
    """Per key: n, min/max/mean, first/last, change vs. baseline, statuses seen, worst zone + citation."""
    out = {"keys": {}, "trigger": None}
    for k in keys:
        vals = [(s["t"], j) for s in samples for j in s["judged"] if j["key"] == k]
        if not vals:
            out["keys"][k] = {"n": 0}
            continue
        nums = [j["value"] for _, j in vals]
        statuses = {}
        for _, j in vals:
            statuses[j["status"]] = statuses.get(j["status"], 0) + 1
        worst = next((j for _, j in vals if j["status"] in ("low", "high")), vals[-1][1])
        out["keys"][k] = {"n": len(nums), "unit": vals[0][1]["unit"], "min": min(nums), "max": max(nums),
                          "mean": round(sum(nums) / len(nums), 4), "first": nums[0], "last": nums[-1],
                          "change": round(nums[-1] - nums[0], 4), "statuses": statuses,
                          "zone": {"low": worst["low"], "high": worst["high"], "citation": worst["citation"],
                                   "condition": worst["condition"], "status": worst["status"]}}
    if trig is not None and trig.s:
        out["trigger"] = {"spec": trig.s, "met_at_s": trig.met_at}
    return out


def text(summary):
    lines = []
    for k, s in summary["keys"].items():
        if not s.get("n"):
            lines.append(f"{k}: no samples")
            continue
        z = s["zone"]
        zone = ("no zone" if z["status"] == "no zone" else
                f"zone {_f(z['low'])}..{_f(z['high'])} {s['unit']} ({z['condition'] or 'any'}; {z['citation']})")
        lines.append(f"{k}: {s['n']} samples, {_f(s['min'])}..{_f(s['max'])} {s['unit']} (mean {_f(s['mean'])}, "
                     f"first {_f(s['first'])}, last {_f(s['last'])}, change {s['change']:+g}); statuses "
                     + ", ".join(f"{a} {b}" for a, b in s["statuses"].items()) + f"; {zone}")
    tr = summary.get("trigger")
    if tr:
        sp = tr["spec"]
        lines.append(f"trigger {sp.get('key')} {sp.get('op')} {sp.get('value')} for {sp.get('for_s') or 0} s: "
                     + (f"met at {tr['met_at_s']:g} s" if tr["met_at_s"] is not None else "not met"))
    return "\n".join(lines)


def _f(x):
    return "?" if x is None or (isinstance(x, float) and math.isinf(x)) else f"{x:g}"


def store(db, case_id, step, keys, samples, summary):
    cur = db.execute("INSERT INTO watch (case_id, ts, step, params, samples, summary) VALUES (?,?,?,?,?,?)",
                     (case_id, time.time(), step, json.dumps(keys), json.dumps(samples), json.dumps(summary)))
    db.commit()
    return cur.lastrowid


class ReplayCar:
    """A stored watch session played back through the same normalize + judge path (offline tests)."""

    def __init__(self, samples):
        self.samples, self.i, self.cable = samples, 0, "replay"
        self.now = 0.0

    def clock(self):
        return self.samples[self.i]["t"] if self.i < len(self.samples) else self.now + 1e9

    def wait(self, s):
        self.i += 1

    def sample(self, keys):
        if self.i >= len(self.samples):
            return None
        return self.samples[self.i]["raw"]


def replay(db, watch_id, car):
    """Re-run a stored session; returns (stored summary, replayed summary)."""
    row = db.execute("SELECT params, samples, summary FROM watch WHERE id=?", (watch_id,)).fetchone()
    keys, samples, summary = json.loads(row[0]), json.loads(row[1]), json.loads(row[2])
    trig = (summary.get("trigger") or {}).get("spec")
    _, again = run(car, ReplayCar(samples), keys, seconds=1e9, trigger=trig, period=0)
    return summary, again
