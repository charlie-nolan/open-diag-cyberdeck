"""
What the scanned numbers mean, worked out in Python (the models misread raw numbers: a 12 C coolant
reading came back as "overheating"). Ranges are generic OBD-II / common-practice values for a gasoline
engine, not any one car. Each line says what the number is and whether it's in the normal band.
"""
import re

NAMES = {"rpm": "rpm", "idle": "rpm", "ect": "ect", "coolant": "ect", "iat": "iat", "stft1": "stft", "stft": "stft",
         "ltft1": "ltft", "ltft": "ltft", "map": "map", "timing": "timing", "knock corr": "knock",
         "tps": "tps", "o2 front": "o2", "o2 b1s1": "o2", "o2 b1s2": "o2r", "o2 b1s1 lambda": "lam",
         "volts": "volts", "battery": "volts", "load": "load", "maf": "maf", "speed": "speed", "stft2": "stft2",
         "ltft2": "ltft2"}
NUM = re.compile(r"\s*[:=]?\s*([+-]?\d+(?:\.\d+)?)")


def parse(text):
    """{'ect': 12.0, ...} from 'ECT 12 C | STFT1 -3.9 percent' or typed 'LTFT +12%, idle 650'."""
    vals = {}
    for seg in re.split(r"[|;,]", text or ""):
        seg = seg.strip().lower()
        for name in sorted(NAMES, key=len, reverse=True):  # 'stft1' before 'stft', 'o2 front' whole
            if seg.startswith(name):
                m = NUM.match(seg[len(name):])
                if m and NAMES[name] not in vals:
                    vals[NAMES[name]] = float(m.group(1))
                break
    return vals


def for_prompt(text, budget=650):
    """The live line for the models: raw hex dropped, cut at a whole reading (obd_all puts the key values first).
    A car can offer 60+ PIDs and the case must still fit the small model's context."""
    keep, used = [], 0
    segs = [s.strip() for s in (text or "").split("|")]
    segs = [s for s in segs if s and not s.startswith("PID ")]
    for s in segs:
        if used + len(s) + 3 > budget:
            break
        keep.append(s)
        used += len(s) + 3
    return " | ".join(keep) + (f" | ({len(segs) - len(keep)} more readings not shown)" if len(keep) < len(segs) else "")


def _trim(name, v):
    a = abs(v)
    side = "adding fuel (it sees lean)" if v > 0 else "removing fuel (it sees rich)"
    if a <= 5:
        return f"{name} {v:+.1f} %: near zero, normal"
    if a <= 10:
        return f"{name} {v:+.1f} %: ECU slightly {side}, still within normal"
    if a <= 20:
        return f"{name} {v:+.1f} %: ECU {side}, outside the usual ±10 %"
    return f"{name} {v:+.1f} %: ECU {side}, near the limit of what it can correct"


def explain(text):
    """Plain-language lines for the live data string, or [] when nothing is recognised."""
    v = parse(text)
    out = []
    ect = v.get("ect")
    cold = ect is not None and ect < 70
    if ect is not None:
        state = ("cold (engine recently started)" if ect < 40 else "warming up, not at operating temperature"
                 if ect < 80 else "normal operating temperature" if ect <= 105 else "hot" if ect <= 115
                 else "overheating")
        out.append(f"Coolant {ect:.0f} C: {state}")
        if cold:
            out.append("Engine not warm: fuel trims, O2 readings and idle speed aren't at their warm values yet "
                       "(the ECU may still be in open loop). Re-read warm before judging them.")
    if "rpm" in v:
        r = v["rpm"]
        if cold and 900 <= r <= 1800:
            out.append(f"Engine speed {r:.0f} rpm: cold fast idle, normal while warming up")
        elif not cold and ect is not None and v.get("tps", 0) < 3:
            out.append(f"Engine speed {r:.0f} rpm: " + ("normal warm idle" if 550 <= r <= 1000 else
                       "high for a warm idle" if r > 1000 else "low for an idle"))
        else:
            out.append(f"Engine speed {r:.0f} rpm")
    for key, name in (("stft", "Short-term fuel trim"), ("ltft", "Long-term fuel trim"),
                      ("stft2", "Short-term fuel trim bank 2"), ("ltft2", "Long-term fuel trim bank 2")):
        if key in v:
            out.append(_trim(name, v[key]))
    if "stft" in v and "ltft" in v:
        out.append(_trim("Total fuel trim", v["stft"] + v["ltft"]))
    if "stft" in v and v["stft"] == 0 and cold:
        out.append("Short-term trim exactly 0 while cold usually means open loop (O2 sensor not used yet)")
    if "map" in v and v.get("tps", 0) < 3:
        m = v["map"]
        out.append(f"Manifold pressure {m:.0f} kPa at idle: " + ("strong vacuum, normal" if m <= 40 else
                   "weak vacuum (can come from a vacuum leak, misfire, late timing or a cold engine)"
                   if m <= 55 else "very weak vacuum for an idle"))
    if "knock" in v:
        k = v["knock"]
        out.append(f"Knock correction {k:+.1f} deg: " + ("none" if k == 0 else
                   "ECU pulling timing for detected knock" if k < 0 else "advance added"))
    if "o2" in v:
        o = v["o2"]
        if cold:
            out.append(f"Front O2 {o:.2f} V: sensor may not be active yet on a cold engine")
        else:
            out.append(f"Front O2 {o:.2f} V: " + ("stuck near 0 V (dead sensor, wiring, or very lean)" if o < 0.05
                       else "in the switching range" if o <= 0.95 else "high (rich)") +
                       " (narrowband sensors switch 0.1-0.9 V when warm; wideband A/F sensors read differently)")
    if "lam" in v:
        lam = v["lam"]
        out.append(f"Front wideband lambda {lam:.2f}: " + ("about stoichiometric" if 0.97 <= lam <= 1.03 else
                   "rich (below 1.00)" if lam < 1 else "lean (above 1.00)"))
    if "o2r" in v:
        out.append(f"Rear O2 {v['o2r']:.2f} V: downstream sensor, read it with the front sensor and catalyst condition")
    if "volts" in v:
        b = v["volts"]
        running = v.get("rpm", 0) > 300
        out.append(f"System voltage {b:.1f} V: " + (("charging normally" if 13.3 <= b <= 14.9 else
                   "low: alternator not charging fully" if b < 13.3 else "high: overcharging") if running else
                   ("battery charged" if b >= 12.4 else "battery low")))
    if "tps" in v:
        out.append(f"Throttle {v['tps']:.0f} %: " + ("closed" if v["tps"] < 3 else "open"))
    return out


# ---------------------------------------------------------------------------- test drive
# The order and labels used in a drive log row (only what was read is shown).
ROW = [("speed", "Speed"), ("rpm", "RPM"), ("load", "Load"), ("tps", "TPS"), ("map", "MAP"), ("maf", "MAF"),
       ("ect", "ECT"), ("iat", "IAT"), ("stft", "STFT"), ("ltft", "LTFT"), ("timing", "Timing"),
       ("knock", "Knock"), ("o2", "O2"), ("lam", "Lambda"), ("o2r", "O2 rear"), ("volts", "Volts")]
UNIT = {"speed": " km/h", "ect": " C", "iat": " C", "stft": "%", "ltft": "%", "load": "%", "tps": "%",
        "map": " kPa", "maf": " g/s", "timing": " deg", "knock": " deg", "o2": " V", "o2r": " V", "volts": " V"}
CONDITIONS = ("idle", "light / cruise", "high load")


def condition(v):
    """Which part of the drive a reading is from, from its own numbers (None when they can't tell)."""
    speed, rpm, tps, load = v.get("speed"), v.get("rpm"), v.get("tps"), v.get("load")
    if rpm is not None and rpm < 300:
        return None  # engine off
    if (load is not None and load >= 60) or (tps is not None and tps >= 40):
        return "high load"
    if (speed is not None and speed < 3) or (speed is None and tps is not None and tps < 3
                                             and rpm is not None and rpm < 1100):
        return "idle"
    if speed is not None or tps is not None or load is not None:
        return "light / cruise"
    return None


def _fmt(key, x):
    return f"{x:+.1f}" if key in ("stft", "ltft", "knock") else f"{x:.2f}" if key in ("o2", "o2r", "lam") else f"{x:.0f}" \
        if abs(x) >= 10 or key in ("speed", "rpm", "ect", "iat", "map") else f"{x:.1f}"


def drive_summary(samples, max_rows=10, max_chars=1500):
    """Plain text of a whole test drive for the models: what changed across speeds and loads.
    samples: [(seconds since start, codes, live text)], oldest first. Python works out every number."""
    rows = [(t, codes, parse(live)) for t, codes, live in samples]
    rows = [r for r in rows if r[2]]
    if not rows:
        return ""
    span = rows[-1][0] - rows[0][0]
    gap = span / (len(rows) - 1) if len(rows) > 1 else 0
    tagged = [(t, codes, v, condition(v)) for t, codes, v in rows]
    count = {c: sum(1 for *_, k in tagged if k == c) for c in CONDITIONS}
    out = [f"{len(rows)} readings over {span / 60:.0f} min, about every {gap:.0f} s. Judge each value against its own "
           "speed, load and temperature, and compare idle, cruise and high load; no single reading is the car.",
           "Readings per condition: " + ", ".join(f"{c} {n}" for c, n in count.items() if n)
           + (f", unclear {len(rows) - sum(count.values())}" if len(rows) > sum(count.values()) else "")]
    ects = [v["ect"] for *_, v, _ in tagged if "ect" in v]
    if ects:
        out.append((f"Coolant {min(ects):.0f} C" if min(ects) == max(ects) else
                    f"Coolant {min(ects):.0f} to {max(ects):.0f} C") + " over the drive"
                   + (" (engine warmed up during it: early readings are from a cold engine)"
                      if min(ects) < 70 <= max(ects) else ""))
    for c in CONDITIONS:
        vs = [v for *_, v, k in tagged if k == c]
        if not vs:
            continue
        bits = []
        for key, label in ROW:
            xs = [v[key] for v in vs if key in v]
            if not xs or key == "ect":
                continue
            lo, hi = min(xs), max(xs)
            rng = _fmt(key, lo) if lo == hi else f"{_fmt(key, lo)} to {_fmt(key, hi)}"
            bits.append(f"{label} {rng}{UNIT.get(key, '')}")
        line = f"{c.capitalize()} ({len(vs)}): " + ", ".join(bits)
        tot = [v["stft"] + v["ltft"] for v in vs if "stft" in v and "ltft" in v]
        if tot:
            avg = sum(tot) / len(tot)
            line += f"; average total trim {avg:+.1f}% (" + ("normal" if abs(avg) <= 10 else
                    "adding fuel: ECU sees lean" if avg > 0 else "pulling fuel: ECU sees rich") + ")"
        out.append(line)
    # codes: when each first showed up and whether it was still there at the end
    first, last_codes = {}, set()
    for i, (t, codes, _, _) in enumerate(tagged):
        now = set(re.findall(r"\b[PBCU]\d{4}\b", (codes or "").upper()))
        for c in now:
            first.setdefault(c, (i, t))
        last_codes = now
    if first:
        parts = []
        for c, (i, t) in sorted(first.items(), key=lambda x: x[1]):
            when = "from the first reading" if i == 0 else f"first set at {(t - rows[0][0]) / 60:.0f} min ({tagged[i][3] or 'unclear'})"
            parts.append(f"{c} {when}" + ("" if c in last_codes else ", gone by the end"))
        out.append("Codes during the drive: " + "; ".join(parts))
    else:
        out.append("Codes during the drive: none")
    pick = list(range(len(tagged)))
    if len(pick) > max_rows:  # evenly spread, always keeping the first and last
        pick = sorted({round(i * (len(tagged) - 1) / (max_rows - 1)) for i in range(max_rows)})
    out.append("Readings (min:sec | condition | values):")
    head = "\n".join(out)
    lines = []
    for i in pick:
        t, _, v, k = tagged[i]
        dt = t - rows[0][0]
        vals = " ".join(f"{label} {_fmt(key, v[key])}" for key, label in ROW if key in v)
        lines.append(f"{int(dt // 60):02d}:{int(dt % 60):02d} | {k or 'unclear'} | {vals}")
    while len(lines) > 2 and len(head) + sum(len(x) + 1 for x in lines) > max_chars:
        lines.pop(len(lines) // 2)  # thin the middle first: the start and end matter most
    return head + "\n" + "\n".join(lines)
