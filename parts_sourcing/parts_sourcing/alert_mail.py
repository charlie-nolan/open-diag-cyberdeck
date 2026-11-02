"""Read Row52 "new vehicles matching your saved search" emails and add those cars to yards.db.

This reads your own mailbox, so Row52 itself is never crawled. Gmail needs IMAP turned on and an app
password (Google Account > Security > 2-Step Verification > App passwords). The password is never stored
in this folder: put it in ~/.config/subaru_cyberdeck/mail_app_password (chmod 600) or the environment
variable ROW52_MAIL_PASSWORD, and your address in sourcing.json "alert_mail": {"user": ...}.

    python3 -m parts_sourcing.alert_mail                  # new Row52 alerts from the last 60 days
    python3 -m parts_sourcing.alert_mail --dry-run        # show what it would add
    python3 -m parts_sourcing.alert_mail --file alert.eml # one saved email (or pasted text), no mailbox

Emails are opened read-only (they stay unread) and each one is used once (its Message-ID is remembered).
Alert cars carry no VIN, so their engine comes from the model and year; they drop off after 45 days unless
a full yard paste confirms them.
"""
import argparse
import datetime as dt
import email
import email.policy
import html
import imaplib
import os
import re
import sys

from . import yards as Y
from .yard_paste import parse_text
from .yard_sync import load_settings, store

HERE = os.path.dirname(os.path.abspath(__file__))
PASSWORD_FILE = "~/.config/subaru_cyberdeck/mail_app_password"
SEEN_SCHEMA = "CREATE TABLE IF NOT EXISTS mail_seen (message_id TEXT PRIMARY KEY, seen_at TEXT NOT NULL, cars INTEGER)"


def body_text(msg):
    """Plain text of an email: its text/plain part, else its HTML with the tags stripped."""
    part = msg.get_body(preferencelist=("plain", "html")) if hasattr(msg, "get_body") else None
    if part is None:
        return msg.get_payload(decode=False) if not msg.is_multipart() else ""
    text = part.get_content()
    if part.get_content_type() == "text/html":
        text = re.sub(r"(?i)<br\s*/?>|</(p|div|tr|li|h\d)>", "\n", text)
        text = html.unescape(re.sub(r"<[^>]+>", " ", text))
    return "\n".join(" ".join(ln.split()) for ln in text.splitlines())


def cars_from_alert(text):
    """Cars listed in one alert. The greeting and footer have no 'YEAR MAKE MODEL' line, so they're skipped."""
    return [c for c in parse_text(text) if c["yard"]]


def handle(con, raw, run_at, dry_run=False):
    """Parse one raw email (bytes); returns (message_id, cars, new) and stores the cars unless dry_run."""
    msg = email.message_from_bytes(raw, policy=email.policy.default)
    mid = (msg.get("Message-ID") or "").strip() or None
    if mid and con.execute("SELECT 1 FROM mail_seen WHERE message_id = ?", (mid,)).fetchone():
        return mid, [], False
    cars = cars_from_alert(body_text(msg))
    if not dry_run:
        if cars:
            store(con, cars, run_at)
        if mid:
            con.execute("INSERT OR REPLACE INTO mail_seen VALUES (?,?,?)", (mid, run_at, len(cars)))
    return mid, cars, True


def fetch(cfg, days):
    """Raw Row52 emails from the last `days` days, opened read-only."""
    mc = cfg.get("alert_mail", {})
    user = mc.get("user")
    pw = os.environ.get("ROW52_MAIL_PASSWORD")
    pf = os.path.expanduser(PASSWORD_FILE)
    if not pw and os.path.exists(pf):
        pw = open(pf).read().strip()
    if not user or not pw:
        raise SystemExit(f"Set alert_mail.user in sourcing.json and put the app password in {PASSWORD_FILE}")
    box = imaplib.IMAP4_SSL(mc.get("imap_host", "imap.gmail.com"))
    try:
        box.login(user, pw)
        box.select(mc.get("folder", "INBOX"), readonly=True)
        since = (dt.date.today() - dt.timedelta(days=days)).strftime("%d-%b-%Y")
        _, data = box.search(None, "FROM", f'"{mc.get("from", "row52")}"', "SINCE", since)
        out = []
        for num in data[0].split():
            _, parts = box.fetch(num, "(BODY.PEEK[])")
            out += [p[1] for p in parts if isinstance(p, tuple)]
        return out
    finally:
        try:
            box.logout()
        except Exception:
            pass


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--file", help="one saved email (.eml) or pasted alert text, - for stdin; skips the mailbox")
    ap.add_argument("--days", type=int, default=60)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--config", default=os.path.join(HERE, "sourcing.json"))
    a = ap.parse_args(argv)
    cfg = load_settings(a.config)
    if a.file:
        raw = sys.stdin.buffer.read() if a.file == "-" else open(a.file, "rb").read()
        if not re.match(rb"(?im)^[\w-]+:", raw):              # pasted text, not a whole email
            raw = b"Content-Type: text/plain; charset=utf-8\n\n" + raw
        raws = [raw]
    else:
        raws = fetch(cfg, a.days)
    con = Y.connect(os.path.expanduser(cfg.get("yards_db", Y.DEFAULT_DB)), write=True)
    run_at = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    total = 0
    try:
        con.execute(SEEN_SCHEMA)
        for raw in raws:
            mid, cars, new = handle(con, raw, run_at, a.dry_run)
            for c in cars:
                print(f"  {c['year']} {c['make']} {c['model']} | {c['yard']} | row {c['row'] or '?'} | set {c['added'] or '?'}")
            total += len(cars)
        if not a.dry_run:
            con.execute("INSERT INTO sync_log VALUES (?,?,?,?,?,?,NULL)",
                        (run_at, "alert-mail", None, total, total, 0))
            con.commit()
    finally:
        con.close()
    print(f"{len(raws)} emails, {total} new cars {'found' if a.dry_run else 'saved'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
