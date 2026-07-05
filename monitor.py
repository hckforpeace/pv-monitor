#!/usr/bin/env python3
"""Paris-Versailles bourse aux dossards availability monitor.

Polls the bourse page and emails an alert (Gmail SMTP) when a bib
("dossard") becomes available for resale. No waiting list on their side:
availability is first-come, so we poll frequently.

Availability signal: the page shows the sentence
    "Il n'y a actuellement pas de dossard disponible a la revente."
while empty. A bib is available when that "pas de dossard disponible"
text is gone. There is no dedicated status tag/attribute in the HTML.

Subcommands:
    monitor.py run                 long-lived poll loop (container default)
    monitor.py check [--dry-run]   single check; exit 0 none / 10 available / 1 error
    monitor.py sendMail "<text>"   send a test email via the alert SMTP path
"""
import argparse
import json
import os
import smtplib
import sys
import time
import unicodedata
import urllib.error
import urllib.request
from email.message import EmailMessage
from email.utils import formatdate

# The page to poll. Required at runtime via env (set in secrets.env); no URL is
# committed to the repo. `check`/`run` abort early with a clear error if unset.
URL = os.environ.get("PV_URL")
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
# Normalized (accent-stripped, lowercased, whitespace-collapsed) marker that is
# present ONLY while no bib is available.
UNAVAILABLE_MARKER = "pas de dossard disponible"

STATE_FILE = os.environ.get("PV_STATE_FILE", "/data/state.json")
RENOTIFY_SECONDS = 30 * 60        # re-send bib alert at most every 30 min
ERROR_RENOTIFY_SECONDS = 30 * 60  # re-send request-failure alert at most every 30 min

# Persistent, rotating log file (mounted volume). Keeps the last MAX_LOG_LINES
# timestamped lines so history survives a container crash/restart.
LOG_FILE = os.environ.get("PV_LOG_FILE", "/logs/monitor.log")
MAX_LOG_LINES = int(os.environ.get("PV_LOG_MAX_LINES", "2880"))  # 2 days @ 1/min

# ANSI colors for stdout (docker logs). Stripped from the file.
RED = "\033[31m"
RESET = "\033[0m"


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
def env(name, default=None, required=False):
    val = os.environ.get(name, default)
    if required and not val:
        raise SystemExit(f"ERROR: required env var {name} is not set")
    return val


def parse_duration(val, name="duration"):
    """Parse a duration like '10s', '1m', '90' into seconds (int).

    A bare number is treated as seconds. Suffixes: 's' seconds, 'm' minutes,
    'h' hours. Raises SystemExit on a malformed or non-positive value.
    """
    s = str(val).strip().lower()
    units = {"s": 1, "m": 60, "h": 3600}
    unit = 1
    if s and s[-1] in units:
        unit = units[s[-1]]
        s = s[:-1].strip()
    try:
        num = float(s)
    except ValueError:
        raise SystemExit(
            f"ERROR: invalid {name} {val!r}; use e.g. '10s', '1m', '2h', or a "
            f"plain number of seconds")
    seconds = int(num * unit)
    if seconds <= 0:
        raise SystemExit(f"ERROR: {name} must be positive, got {val!r}")
    return seconds


# ---------------------------------------------------------------------------
# Fetch + detection
# ---------------------------------------------------------------------------
class FetchError(Exception):
    """Raised when the site request fails (bad HTTP status or network error)."""


def fetch(url=URL, timeout=20):
    """GET the page, one retry on failure. Returns HTML text.

    Raises FetchError on network error or any HTTP status outside 200-299.
    """
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    last_err = None
    for attempt in range(2):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                status = getattr(resp, "status", None) or resp.getcode()
                raw = resp.read()
            if not (200 <= status < 300):
                raise FetchError(f"HTTP {status} {getattr(resp, 'reason', '')}".strip())
            return raw.decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            last_err = FetchError(f"HTTP {e.code} {e.reason}")
        except FetchError as e:
            last_err = e
        except Exception as e:  # noqa: BLE001 - URLError, timeout, DNS, etc.
            last_err = FetchError(f"network error: {e}")
        if attempt == 0:
            time.sleep(3)
    raise last_err


def normalize(html):
    """Lowercase, strip accents, collapse whitespace for robust matching."""
    text = unicodedata.normalize("NFKD", html)
    text = text.encode("ascii", "ignore").decode("ascii")
    return " ".join(text.lower().split())


def is_available(html):
    """True when the 'no bib available' marker is absent from the page."""
    if os.environ.get("PV_FORCE_AVAILABLE") == "1":
        return True
    return UNAVAILABLE_MARKER not in normalize(html)


def snippet(html, width=260):
    """Context slice around the bib-status text, for logs/email.

    Targets the resale/status paragraph (keywords 'revente'/'rachat'/'disponible')
    rather than the first 'dossard' mention, which is the page <title>.
    """
    low = html.lower()
    for needle in ("revente", "rachat", "disponible", "dossard"):
        pos = low.find(needle)
        if pos >= 0:
            start = max(0, pos - width)
            end = min(len(html), pos + width)
            return " ".join(html[start:end].split())
    return "(no status text found on page)"


# ---------------------------------------------------------------------------
# Email
# ---------------------------------------------------------------------------
def send_email(subject, body, to=None):
    """Send via SMTP+STARTTLS using PV_* env config. Returns recipient on success.

    `to` overrides the recipient (used for failure alerts to a second address);
    defaults to PV_MAIL_TO.
    """
    user = env("PV_SMTP_USER", required=True)
    password = env("PV_SMTP_PASS", required=True)
    mail_to = to or env("PV_MAIL_TO", required=True)
    mail_from = env("PV_MAIL_FROM", default=user)
    host = env("PV_SMTP_HOST", default="smtp.gmail.com")
    port = int(env("PV_SMTP_PORT", default="587"))

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = mail_from
    msg["To"] = mail_to
    msg["Date"] = formatdate(localtime=True)
    msg.set_content(body)

    with smtplib.SMTP(host, port, timeout=30) as server:
        server.starttls()
        server.login(user, password)
        server.send_message(msg)
    return mail_to


# ---------------------------------------------------------------------------
# State (dedup / re-notify throttle)
# ---------------------------------------------------------------------------
def load_state():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"status": "NONE", "last_email_ts": 0}


def save_state(state):
    try:
        os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f)
    except OSError as e:
        log(f"WARN: could not persist state to {STATE_FILE}: {e}")


def now():
    return int(time.time())


def _append_log_file(line):
    """Append one line to LOG_FILE and trim to the last MAX_LOG_LINES."""
    if not LOG_FILE:
        return
    try:
        os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
        with open(LOG_FILE, "r", encoding="utf-8") as f:
            lines = f.readlines()
        if len(lines) > MAX_LOG_LINES:
            with open(LOG_FILE, "w", encoding="utf-8") as f:
                f.writelines(lines[-MAX_LOG_LINES:])
    except OSError as e:
        # Don't recurse through log(); write the warning raw.
        print(f"WARN: log file write failed: {e}", flush=True)


def log(msg, level="info"):
    """Timestamped log to stdout (red if level='error') and the rotating file."""
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    if level == "error":
        print(f"{RED}{line}{RESET}", flush=True)
    else:
        print(line, flush=True)
    _append_log_file(line)  # file stays plain (no color codes)


def describe_fetch_error(err):
    """Human-readable one-liner for a fetch failure."""
    if isinstance(err, FetchError):
        return str(err)
    return f"{type(err).__name__}: {err}"


def maybe_email_fetch_failure(detail, dry_run):
    """Email the failure to PV_MAIL_TO_ERRORS (throttled). No-op if unset."""
    to = os.environ.get("PV_MAIL_TO_ERRORS")
    if not to or dry_run:
        return
    state = load_state()
    if now() - state.get("last_error_email_ts", 0) < ERROR_RENOTIFY_SECONDS:
        return
    try:
        send_email(
            "⚠️ Paris-Versailles monitor: site request FAILED",
            f"The GET request to the bourse page failed.\n\n"
            f"URL: {URL}\nDetail: {detail}\n\n"
            f"The monitor keeps retrying; you'll get at most one such mail "
            f"per 30 min while it stays down.\n",
            to=to,
        )
        state["last_error_email_ts"] = now()
        save_state(state)
        log(f"Failure alert email sent to {to}")
    except Exception as e:  # noqa: BLE001
        log(f"ERROR sending failure email: {e}", level="error")


# ---------------------------------------------------------------------------
# Core check
# ---------------------------------------------------------------------------
def do_check(dry_run=False):
    """Run one check. Emails on NONE->AVAILABLE transition (throttled re-notify).

    Returns True if a bib is available, else False. Raises on fetch failure
    (after red-logging it and, if configured, emailing PV_MAIL_TO_ERRORS).
    """
    try:
        html = fetch()
    except Exception as e:  # noqa: BLE001 - already normalized to FetchError
        detail = describe_fetch_error(e)
        log(f"REQUEST FAILED: {detail}", level="error")
        maybe_email_fetch_failure(detail, dry_run)
        raise
    available = is_available(html)
    ctx = snippet(html)

    state = load_state()
    prev = state.get("status", "NONE")

    if not available:
        if prev != "NONE":
            log("State change: AVAILABLE -> NONE")
        else:
            log("STATUS: NONE (no bib available)")
        state["status"] = "NONE"
        save_state(state)
        return False

    # available
    log(f"STATUS: AVAILABLE — snippet: {ctx}")
    should_email = prev != "AVAILABLE" or (
        now() - state.get("last_email_ts", 0) >= RENOTIFY_SECONDS
    )
    if dry_run:
        log("dry-run: skipping email")
    elif should_email:
        subject = "🎟️ Dossard DISPONIBLE Paris-Versailles"
        body = (
            "A bib appears AVAILABLE on the Paris-Versailles bourse aux dossards.\n\n"
            f"Go now (first-come, no waiting list):\n{URL}\n\n"
            f"Detected page context:\n{ctx}\n"
        )
        try:
            to = send_email(subject, body)
            state["last_email_ts"] = now()
            log(f"Alert email sent to {to}")
        except Exception as e:  # noqa: BLE001 - surface but do not crash the loop
            log(f"ERROR sending alert email: {e}")
    else:
        log("Still available; within 30-min re-notify throttle, no email")

    state["status"] = "AVAILABLE"
    save_state(state)
    return True


# ---------------------------------------------------------------------------
# Subcommands
# ---------------------------------------------------------------------------
def cmd_run(_args):
    interval = parse_duration(env("PV_INTERVAL", default="60"), "PV_INTERVAL")
    log(f"Starting monitor loop: {URL} every {interval}s")
    while True:
        try:
            do_check()
        except FetchError:
            pass  # do_check already red-logged + emailed; keep the loop alive
        except Exception as e:  # noqa: BLE001 - any other error, keep looping
            log(f"ERROR during check: {e}", level="error")
        time.sleep(interval)


def cmd_check(args):
    try:
        available = do_check(dry_run=args.dry_run)
    except FetchError:
        return 1  # do_check already red-logged the failure
    except Exception as e:  # noqa: BLE001
        log(f"ERROR: {e}", level="error")
        return 1
    return 10 if available else 0


def cmd_sendmail(args):
    body = args.text or "Paris-Versailles monitor test email."
    try:
        to = send_email("Paris-Versailles monitor — test email", body)
    except Exception as e:  # noqa: BLE001
        print(f"FAILED: {e}", file=sys.stderr)
        return 1
    print(f"OK: sent to {to}")
    return 0


HELP_TEXT = """\
Paris-Versailles dossard monitor
================================
Polls the bourse aux dossards page and alerts (email) when a bib becomes
available for resale. First-come, no waiting list, so it polls frequently.

USAGE
  python3 monitor.py <command>

COMMANDS
  run                    Long-lived poll loop (the container default). Checks
                         every PV_INTERVAL forever; auto-restarts.
  check [--dry-run]      Run ONE check and exit. Exit code: 0 = no bib,
                         10 = bib available, 1 = request failed.
                         --dry-run: detect + log only, never send email.
  sendMail "<text>"      Send a test email (to PV_MAIL_TO) to prove SMTP works.
  help                   Show this text.

  Standard --help also works, e.g.  python3 monitor.py check --help

TESTING RECIPES
  Prove email works end-to-end:
    docker compose run --rm pv-dossard python3 monitor.py sendMail "test 123"
  Check the live page once, no email:
    docker compose run --rm pv-dossard python3 monitor.py check --dry-run
  Exercise the AVAILABLE alert path (pretend a bib is free):
    docker compose run --rm -e PV_FORCE_AVAILABLE=1 pv-dossard python3 monitor.py check
  Exercise the FAILURE path (red log + failure email to a 2nd address):
    docker compose run --rm -e PV_URL=http://127.0.0.1:9/ \\
      -e PV_MAIL_TO_ERRORS=you@example.com pv-dossard python3 monitor.py check

ENVIRONMENT VARIABLES  (set in secrets.env; commented = use the default)
  Required
    PV_URL               Page to poll for bib availability. No default is
                         committed to the repo; set it in secrets.env.
    PV_SMTP_USER         Gmail address that sends alerts.
    PV_SMTP_PASS         Gmail APP PASSWORD (needs 2FA; normal password fails).
    PV_MAIL_TO           Where the "bib available" alert is sent.
  Optional feature
    PV_MAIL_TO_ERRORS    2nd address, alerted ONLY when the site request fails
                         (non-2xx / network error). Unset = failure emails off
                         (you still get the red log + persisted log line).
                         Throttled to at most 1 email / 30 min while down.
  Optional tuning (default shown)
    PV_MAIL_FROM         From: header.            default = PV_SMTP_USER
                         (Gmail usually rewrites this to your account anyway.)
    PV_SMTP_HOST         SMTP server.             default = smtp.gmail.com
    PV_SMTP_PORT         SMTP port (STARTTLS).    default = 587
    PV_INTERVAL          Time between checks. Accepts '10s', '1m', '2h', or a
                         plain number (= seconds).      default = 60 (1m)
    PV_LOG_MAX_LINES     Rotating log line cap.   default = 2880 (~2 days @ 1/min)
  Testing helpers (normally unset)
    PV_FORCE_AVAILABLE=1 Pretend a bib is available (test the alert path).
    PV_STATE_FILE        State file path.         default = /data/state.json
    PV_LOG_FILE          Log file path.           default = /logs/monitor.log

BEHAVIOUR NOTES
  - Availability = the page no longer shows "pas de dossard disponible".
  - Bib alert emails once on NONE->AVAILABLE, then <=1/30 min while it stays up.
  - Logs: timestamped, red for failures on stdout; ./logs/monitor.log persists
    the last PV_LOG_MAX_LINES lines across container crashes/restarts.
"""


def cmd_help(_args):
    print(HELP_TEXT)
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Paris-Versailles dossard monitor. "
        "Run `monitor.py help` for full docs incl. env variables.")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("run", help="long-lived poll loop (default)")

    p_check = sub.add_parser("check", help="single check; exit 0 none / 10 available / 1 error")
    p_check.add_argument("--dry-run", action="store_true", help="do not send email")

    p_mail = sub.add_parser("sendMail", help="send a test email via the alert SMTP path")
    p_mail.add_argument("text", nargs="?", default=None, help="email body text")

    sub.add_parser("help", help="show full usage, testing recipes, and env vars")

    args = parser.parse_args(argv)
    command = args.command or "run"

    if command in ("run", "check") and not URL:
        raise SystemExit(
            "ERROR: required env var PV_URL is not set. "
            "Set it in secrets.env (see secrets.env.example).")

    if command == "run":
        return cmd_run(args)
    if command == "check":
        return cmd_check(args)
    if command == "sendMail":
        return cmd_sendmail(args)
    if command == "help":
        return cmd_help(args)
    parser.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
