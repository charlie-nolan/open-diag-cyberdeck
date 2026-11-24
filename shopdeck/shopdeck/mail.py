"""Email the customer their estimate, invoice or receipt from the shop's own address.

Gmail by default: the account needs 2-step verification and an app password (Google account > Security >
App passwords). The address goes in Settings; the app password goes in ~/.shopdeck/mail_password (only the
owner can read it) or the SHOPDECK_MAIL_PASSWORD environment variable. It is never written to a ticket or
to config.json.

The deck is often offline, so a message that can't be sent right away waits in ~/.shopdeck/outbox/ and goes
out with "send waiting emails" when there's a connection."""
import os
import smtplib
import socket
import ssl
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from email.utils import formatdate, make_msgid

from .case import HOME

OUTBOX = os.path.join(HOME, "outbox")
PASSWORD_FILE = os.path.join(HOME, "mail_password")
DEFAULTS = {"host": "smtp.gmail.com", "port": 587, "copy_me": True}


def settings(cfg):
    m = dict(DEFAULTS)
    m.update(cfg.get("mail") or {})
    m["user"] = m.get("user") or (cfg.get("business") or {}).get("email", "")
    return m


def password():
    pw = os.environ.get("SHOPDECK_MAIL_PASSWORD")
    if pw:
        return pw.strip()
    try:
        with open(PASSWORD_FILE) as f:
            return f.read().strip()
    except OSError:
        return ""


def save_password(pw):
    os.makedirs(HOME, exist_ok=True)
    fd = os.open(PASSWORD_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(pw.replace(" ", "").strip())
    os.chmod(PASSWORD_FILE, 0o600)


def build(cfg, to, subject, body, html_path=None):
    """The message: the plain-text summary as the body, the printable page attached and as the HTML part."""
    biz = cfg.get("business") or {}
    m = settings(cfg)
    msg = EmailMessage()
    sender = m["user"]
    msg["From"] = f"{biz['name']} <{sender}>" if biz.get("name") else sender
    msg["To"] = to
    if m.get("copy_me") and sender and sender.lower() != to.lower():
        msg["Bcc"] = sender
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=sender.split("@")[-1] if "@" in sender else None)
    msg.set_content(body)
    if html_path:
        with open(html_path, "rb") as f:
            page = f.read()
        msg.add_alternative(page.decode("utf-8"), subtype="html")
        msg.add_attachment(page, maintype="text", subtype="html", filename=os.path.basename(html_path))
    return msg


def queue(msg):
    os.makedirs(OUTBOX, exist_ok=True)
    name = msg["Message-ID"].strip("<>").replace("/", "_").replace("@", "_") + ".eml"
    path = os.path.join(OUTBOX, name)
    with open(path, "wb") as f:
        f.write(msg.as_bytes(policy=policy.SMTP))
    return path


def waiting():
    if not os.path.isdir(OUTBOX):
        return []
    return sorted(os.path.join(OUTBOX, f) for f in os.listdir(OUTBOX) if f.endswith(".eml"))


def _smtp_send(cfg, msgs):
    m = settings(cfg)
    pw = password()
    if not m.get("user") or not pw:
        raise PermissionError("email isn't set up: add the shop's address and app password in Settings")
    with smtplib.SMTP(m["host"], int(m["port"]), timeout=20) as s:
        s.starttls(context=ssl.create_default_context())
        s.login(m["user"], pw)
        for msg in msgs:
            s.send_message(msg)


def send(cfg, msg, smtp=None):
    """Send now, or keep it in the outbox when there's no connection. Returns ('sent'|'queued', detail).
    Wrong password or no setup raises PermissionError and keeps nothing, so it can be fixed and retried."""
    smtp = smtp or _smtp_send
    try:
        smtp(cfg, [msg])
        return "sent", msg["To"]
    except smtplib.SMTPAuthenticationError as e:
        raise PermissionError("Gmail refused the login: check the address and the app password") from e
    except (OSError, socket.timeout, smtplib.SMTPException) as e:
        if isinstance(e, PermissionError):
            raise
        return "queued", queue(msg)


def send_waiting(cfg, smtp=None):
    """Send everything in the outbox. Returns (sent, still waiting)."""
    smtp = smtp or _smtp_send
    files = waiting()
    sent = 0
    for path in files:
        with open(path, "rb") as f:
            msg = BytesParser(policy=policy.default).parse(f)
        try:
            smtp(cfg, [msg])
        except smtplib.SMTPAuthenticationError as e:
            raise PermissionError("Gmail refused the login: check the address and the app password") from e
        except (OSError, smtplib.SMTPException) as e:
            if isinstance(e, PermissionError):
                raise
            break
        os.remove(path)
        sent += 1
    return sent, len(files) - sent
