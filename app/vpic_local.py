"""NHTSA's standalone vPIC database, restored into a local PostgreSQL by tools/vpic_setup.sh: decodes any
1981+ US-market VIN offline with NHTSA's own vpic.spVinDecode. Uses the psql command, so no Python driver is
needed. Returns the same dict as vehicle_id.nhtsa_decode, or None when the database isn't there."""
import json
import re
import shutil
import subprocess

VIN_RE = re.compile(r"^[A-HJ-NPR-Z0-9]{17}$")


def _rows(vin, db):
    out = subprocess.run(["psql", "-d", db, "-X", "-At", "-v", "ON_ERROR_STOP=1", "-c",
                          f"SELECT row_to_json(t) FROM vpic.spVinDecode('{vin}') t"],
                         capture_output=True, text=True, timeout=20)
    if out.returncode:
        return None
    vals = {}
    for line in out.stdout.splitlines():
        try:
            r = {k.lower(): v for k, v in json.loads(line).items()}
        except ValueError:
            continue
        name, value = (r.get("variable") or "").strip(), r.get("value")
        if name and value not in (None, "", "Not Applicable"):
            vals[name] = str(value).strip()
    return vals


def vpic_decode(vin, db="vpic"):
    vin = (vin or "").strip().upper()
    if not VIN_RE.match(vin) or not shutil.which("psql"):    # the VIN check also keeps the SQL literal safe
        return None
    try:
        v = _rows(vin, db)
    except (OSError, subprocess.SubprocessError):
        return None
    if not v or not v.get("Make"):
        return None
    from vehicle_id import canon_make
    disp = v.get("Displacement (L)", "")
    disp = f"{float(disp):.1f}L" if re.fullmatch(r"\d+(\.\d+)?", disp) else ""
    layout = {"V-Shaped": "V", "In-Line": "I", "Horizontally opposed (boxer)": "H"}.get(
        v.get("Engine Configuration", ""), "")
    cyl = f"{layout}{v['Engine Number of Cylinders']}" if v.get("Engine Number of Cylinders") else ""
    engine = " ".join(x for x in (disp, cyl, v.get("Engine Model", ""), "turbo" if v.get("Turbo") == "Yes" else "")
                      if x)
    speeds = f"{v['Transmission Speeds']}-speed " if v.get("Transmission Speeds") else ""
    year = v.get("Model Year", "")
    return {"make": canon_make(v["Make"]), "model": v.get("Model", ""), "year": int(year) if year.isdigit() else None,
            "trim": v.get("Trim", ""), "engine": engine, "trans": (speeds + v.get("Transmission Style", "")).strip(),
            "drive": v.get("Drive Type", ""), "body": v.get("Body Class", "")}
