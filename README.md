# Paris-Versailles dossard monitor

Watches a race's *bourse aux dossards* (bib resale) page and emails you the
moment a bib ("dossard") becomes available for resale. Bibs are first-come with
**no waiting list**, so the container polls every 60s.

The page URL is not committed to the repo — set `PV_URL` in `secrets.env`
(copy `secrets.env.example` to start).

## How detection works

The page has no status tag/API — it just swaps a text block. While empty it shows
*"Il n'y a actuellement pas de dossard disponible à la revente."* A bib is
considered available when that `pas de dossard disponible` text is gone.

## Setup

1. **Gmail App Password** (required): enable 2FA on your Google account, then
   create an App Password (Google Account → Security → App passwords). Your normal
   password will **not** work over SMTP.
2. Edit `secrets.env`:
   ```
   PV_SMTP_USER=you@gmail.com
   PV_SMTP_PASS=your_16_char_app_password
   PV_MAIL_TO=where.to.alert@example.com
   ```
3. Build:
   ```
   docker compose build
   ```

## Test before relying on it

```bash
# 1. Prove email works end-to-end (should land in your inbox):
docker compose run --rm pv-dossard python3 monitor.py sendMail "test 123"

# 2. Check the live page once (no email), prints STATUS: NONE / AVAILABLE:
docker compose run --rm pv-dossard python3 monitor.py check --dry-run

# 3. Exercise the alert path against a forced-available page:
docker compose run --rm -e PV_FORCE_AVAILABLE=1 pv-dossard python3 monitor.py check

# 4. Exercise the failure path — red log + failure email to a 2nd address:
docker compose run --rm -e PV_URL=http://127.0.0.1:9/ \
  -e PV_MAIL_TO_ERRORS=you@example.com pv-dossard python3 monitor.py check
```

Run `python3 monitor.py help` for full built-in docs (commands, recipes, env vars):
```bash
docker compose run --rm pv-dossard python3 monitor.py help
```

## Run for real

```bash
docker compose up -d       # start the 60s poll loop, auto-restarts
docker compose logs -f     # watch checks live
docker compose down        # stop
```

Emails fire once on NONE→AVAILABLE, then at most every 30 min while a bib stays
listed (state kept in `./data/state.json`, survives restarts).

## Logs

Every check is logged with a timestamp. **Failed requests** (any HTTP status
outside 200–299, or a network error) are printed in **red**.

Logs persist to `./logs/monitor.log` (bind-mounted), so history survives a
container crash/restart. The file is trimmed to the last `PV_LOG_MAX_LINES`
lines (default **2880** ≈ 2 days at one check/minute). Color codes appear only on
stdout (`docker logs`); the file stays plain text.

```bash
tail -f ./logs/monitor.log     # follow the persisted log from the host
```

## Failure alerts to a second address

Set `PV_MAIL_TO_ERRORS` in `secrets.env` to get an email whenever the site
request fails (bad HTTP status or network error). This is **separate** from the
`PV_MAIL_TO` bib-available alert. Leave it unset to disable failure emails (you
still get the red log line). Throttled to at most one email per 30 min while the
site stays down.

## Config (env vars, set in `secrets.env`)

Commented in `secrets.env` = variable unset = the default below is used.

| Var | Default | Behaviour when set / commented |
|-----|---------|--------------------------------|
| `PV_SMTP_USER` | — (required) | Gmail address that sends alerts. |
| `PV_SMTP_PASS` | — (required) | Gmail **app password** (2FA required; normal password fails). |
| `PV_MAIL_TO`   | — (required) | Recipient of the "bib available" alert. |
| `PV_MAIL_TO_ERRORS` | unset → **failure emails off** | 2nd address, emailed **only** on request failure (≤1/30 min). |
| `PV_MAIL_FROM` | = `PV_SMTP_USER` | `From:` header. Gmail usually rewrites it to your account regardless. |
| `PV_SMTP_HOST` | `smtp.gmail.com` | SMTP server. Change only for a non-Gmail provider. |
| `PV_SMTP_PORT` | `587` | SMTP port (STARTTLS). |
| `PV_INTERVAL`  | `60` | Time between checks (only affects `run`). Accepts `10s`, `1m`, `2h`, or a plain number (= seconds). Lower = faster + more load. |
| `PV_LOG_MAX_LINES` | `2880` | How many log lines the rotating file keeps. |

Testing-only helpers (normally unset): `PV_URL` (override page URL, used to test
the failure path), `PV_FORCE_AVAILABLE=1` (pretend a bib is available),
`PV_STATE_FILE`, `PV_LOG_FILE`.

> Note: `docker-compose.yml` runs the container as `user: "1000:1000"` so it can
> write the `./data` and `./logs` bind mounts. If your host uid/gid differ
> (`id -u; id -g`), change that line and re-`chown` the two folders.
