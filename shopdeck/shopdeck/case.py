"""A job ticket: the car, what the owner said, what was read and measured, how the diagnosis went, and the
repairs with their quote. Saved as JSON after every change, one file per ticket."""
import json
import os
import time
from dataclasses import dataclass, field, asdict

from .repair import Job

HOME = os.path.expanduser(os.environ.get("SHOPDECK_HOME", "~/.shopdeck"))

# What the factory "Check List for Interview" asks the owner (engine section), as plain fields.
INTERVIEW = [
    ("complaint", "What's wrong, in the owner's words"),
    ("when", "When does it happen (cold start, warm, idle, accelerating, cruising, braking)"),
    ("since", "Since when / how often (always, sometimes, once)"),
    ("lights", "Warning lights on (check engine steady or flashing, others)"),
    ("recent_work", "Recent work, parts replaced, or anything disconnected"),
    ("non_genuine", "Non-genuine or aftermarket parts fitted"),
    ("fuel", "Fuel level / recent fill-up / fuel brand"),
    ("conditions", "Weather, temperature, road (rain, cold, highway, hills)"),
    ("noise", "Noises, smells, smoke, vibration"),
]


@dataclass
class Note:
    """A fact the mechanic knows that the manual can't (reused gasket, aftermarket part, a hunch)."""
    text: str
    kind: str = "note"            # history | reused | hypothesis | note
    t: float = field(default_factory=time.time)


@dataclass
class Case:
    id: str = field(default_factory=lambda: time.strftime("%Y%m%d-%H%M%S"))
    customer: str = ""
    model: str = ""
    year: int = 0
    engine: str = ""
    vin: str = ""
    mileage: str = ""
    interview: dict = field(default_factory=dict)
    codes: list = field(default_factory=list)        # stored trouble codes
    pending: list = field(default_factory=list)      # pending codes (mode 07), not yet confirmed
    code_source: str = ""                            # "typed" / "OBD-II read 10-19 14:02" ...
    live: dict = field(default_factory=dict)         # name -> (value, unit), last read
    notes: list = field(default_factory=list)        # Note
    walks: dict = field(default_factory=dict)        # chart code -> [answers as dicts] + result text
    jobs: list = field(default_factory=list)         # Job
    labor_rate: float = 0.0
    tax_rate: float = 0.0
    supplies_rate: float = 0.0
    created: float = field(default_factory=time.time)

    @property
    def car(self):
        return " ".join(str(x) for x in (self.year or "", self.model, self.engine) if x).strip() or "no car picked"

    @property
    def empty(self):
        """Nothing entered yet: not worth a file in the ticket list."""
        return not (self.model or self.customer or self.vin or self.codes or self.notes or self.jobs
                    or any(self.interview.values()))

    @property
    def path(self):
        return os.path.join(HOME, "tickets", f"{self.id}.json")

    def save(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        d = asdict(self)
        d["jobs"] = [j.to_dict() for j in self.jobs]
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(d, f, indent=1)
        os.replace(tmp, self.path)        # never a half-written ticket

    @classmethod
    def load(cls, path):
        with open(path) as f:
            d = json.load(f)
        jobs = [Job.from_dict(j) for j in d.pop("jobs", [])]
        notes = [Note(**n) for n in d.pop("notes", [])]
        known = {k: v for k, v in d.items() if k in cls.__dataclass_fields__}
        c = cls(**known)
        c.jobs, c.notes = jobs, notes
        return c

    @classmethod
    def recent(cls, n=20):
        folder = os.path.join(HOME, "tickets")
        if not os.path.isdir(folder):
            return []
        files = sorted((f for f in os.listdir(folder) if f.endswith(".json")), reverse=True)[:n]
        out = []
        for f in files:
            try:
                out.append(cls.load(os.path.join(folder, f)))
            except (OSError, ValueError, TypeError, KeyError):
                continue
        return out
