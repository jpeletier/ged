# ged — GlobalEduca from the command line

A self-contained CLI for **Python 3.10 or newer**, implemented in `ged.py` using only the Python standard library. Query school information with structured subcommands, readable terminal output, or JSON for agents and automation.

## Quick start

```bash
python3 ged.py --help
python3 ged.py horario list --help

export GED_CENTRO='your_school_identifier'
export GED_USERNAME='your_username'
read -rs -p 'Password: ' GED_PASSWORD
export GED_PASSWORD
python3 ged.py auth login
python3 ged.py resumen
```

Run `./ged.py` directly, or define `alias ged='python3 /path/to/ged.py'`. No account credentials are embedded in the application. No dependency installation is required.

Command and option names retain the Spanish terminology used by the portal. Help text and project documentation are in English; information returned by the school is preserved in its original language.

## Configuration and authentication

| Environment variable | Purpose |
|---|---|
| `GED_USERNAME` | Account username; `GED_USER` is also accepted |
| `GED_PASSWORD` | Account password; `GED_PASS` is also accepted |
| `GED_CENTRO` | Required school identifier; there is no default |
| `GED_STATE_DIR` | Cookie directory; defaults to `$XDG_STATE_HOME/ged` or `~/.local/state/ged` |

`GED_CENTRO` must be set for every query or authentication command. A missing or empty value is an error, even when a cookie is already available. Use the school identifier from your portal URL: `https://portal.globaleduca.com/<school-identifier>`. Help and version commands do not require configuration.

Global options include `--state-dir`, `--timeout`, and `--json`. Place configuration options before the subcommand.

Cookies are stored in `<state-directory>/<school>/cookies.lwp` with private permissions. Each request reuses the stored session and checks the server's response. If the session is invalid or expired, the application authenticates through CAS using the environment variables and retries **once**. Session cookies survive separate CLI invocations. Passwords are never saved.

```bash
ged auth status   # Check the session without submitting credentials
ged auth login    # Reuse a valid session or authenticate if necessary
ged auth clear    # Remove only the local cookie file
```

The portal allows only one active session per account. Authenticating again can invalidate the session in your browser. Use separate state directories for separate accounts using the same school identifier.

## Commands

Use IDs and document URLs returned by your own queries. IDs in these examples are illustrative, not account records.

```bash
# Dashboard, announcements, and attachments
ged resumen
ged anuncios list
ged anuncios search 'menu' --desde 2026-09-01 --hasta 2026-12-31
ged anuncios view 123
ged anuncios download 123 --adjunto 456 --output attachment.pdf

# Class schedule, holidays, attendance, observations, meetings, and activities
ged horario list --desde 2026-10-12 --hasta 2026-10-19
ged horario view 123 --fecha 2026-10-14
ged festivos view 123
ged ausencias list
ged ausencias view 123
ged incidencias list
ged incidencias view 123
ged entrevistas list
ged entrevistas view 123
ged extraescolares list
ged extraescolares view 123

# Report cards, interim grades, and authorizations
ged boletines list
ged parciales list
ged boletines download --url "/${GED_CENTRO}/DOCUMENT_PATH" --output report.pdf
ged autorizaciones list
ged autorizaciones list --pasadas
ged autorizaciones view 123

# Receipts and payment confirmations
ged recibos list --desde 2026-09-01 --hasta 2026-12-31
ged recibos download 123 --output receipt.pdf

# Shared folders and documents
ged archivos list
ged archivos view -- -3
ged archivos download --output document.bin -- -4

# Account information, preferences, and students
ged perfil view
ged perfil view --area notas
ged perfil view --area notificaciones
ged alumnos list

# Combined query
ged snapshot --json
```

Virtual folders and documents can have **negative IDs**. Use `--` to prevent these IDs from being interpreted as options. Downloads never overwrite existing files. Only document URLs from supported sections are accepted; there is no arbitrary HTTP request command.

### Date ranges

Dates use ISO format: `YYYY-MM-DD`. By default, the class schedule queries the next seven days; other calendars and receipts query the current academic year, from September to September. Calendar end dates are exclusive; receipt end dates are inclusive. Large receipt ranges are split automatically to respect the server's request limit.

Schedules can contain recurring events with `dow` values (0 = Sunday through 6 = Saturday), rather than dated occurrences. Holiday entries may use `date` instead of `start`. The JSON preserves these server-provided fields; terminal output identifies recurring weekdays explicitly. A recurring class is not automatically treated as an exam, assignment, or confirmed occurrence on a holiday.

## Agent integration

```bash
ged anuncios list --json
ged --json recibos list --desde 2026-09-01 --hasta 2026-12-31
ged snapshot --json > snapshot.json
```

JSON responses include `schema_version`, `center`, `fetched_at`, `command`, and `data`. Collection timestamps use UTC; school event times are interpreted in the school's local time zone. HTML sections expose text, tables, links, entries, and visible field values without executing JavaScript or exposing password and hidden token fields.

`--html` returns the original HTML for HTML-based sections. Raw HTML can contain temporary tokens and should not be used as routine stored agent output. `--html` and `--json` cannot be combined.

`snapshot` queries the main sections, past authorizations, preferences, and the announcement search within the requested range. It does not open every detail or download documents; use `view` and `download` explicitly. It includes `errors`, `limitations`, and `complete`, preserving successful results when another section fails.

`anuncios list` follows pagination until results are exhausted. `--max-pages` limits requests and marks the result incomplete if the limit is reached. `anuncios search` decodes Rails UJS HTML responses without executing JavaScript, follows the portal's “load more” AJAX requests, and marks the result incomplete when a page limit or repeated results prevent further progress.

On Unix, simultaneous CLI invocations share a local lock to avoid losing a renewed cookie. Avoid overlapping invocations on platforms without `fcntl` support. Periodic queries can be scheduled with cron or invoked by an agent using the same state directory; the application does not run a polling loop itself.

### Exit codes

| Code | Meaning |
|---|---|
| `0` | Success |
| `1` | Error, invalid session in `auth status`, or an incomplete result |
| `2` | Invalid CLI usage |
| `130` | Interrupted |

With `--json`, application errors are returned as JSON.

## Read-only scope and limitations

All information collectors use GET. The only POST is the CAS authentication form. The CLI provides no payment, registration, signature, editing, messaging, or preference-update actions.

**The server can mark announcements or other details as read when they are opened with GET.** No equivalent detail endpoint that avoids this side effect has been identified. The CLI therefore avoids editing actions but cannot guarantee that opening a detail preserves its “unread” status.

The CLI queries the student currently selected in the portal session and does not change that selection. Some accounts return HTTP 500 from `/alumnos`, even with `modal=true`. In that case, `alumnos list` identifies the visible student from the dashboard and returns `complete=false`; `snapshot` reports the same limitation.

Published grades and active authorizations were unavailable during development. Their text, tables, and links are extracted generically, but populated record layouts still need validation.

## Tests

```bash
python3 -m unittest -v
```

Local tests do not access the portal or require real credentials. They cover authentication retry behavior, trusted CAS form validation, cookie persistence and permissions, HTML extraction, receipt range splitting, virtual file IDs, Rails UJS decoding, and protection against download overwrites.

For a live session check, run `ged auth login`, unset the credential environment variables, and run `ged resumen` using the same state directory.

## Repository contents

Only application source, synthetic tests, and documentation belong in this repository. `.investigacion/`, local state, cookies, environment files, downloaded documents, and snapshots are excluded from version control. Do not add real portal responses or credentials to commits or issue reports.
