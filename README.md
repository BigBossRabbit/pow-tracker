# POW Tracker

Multi-client **proof-of-work time tracking** for the Hermes dashboard — live
timers, file-activity **auto-capture**, **invoicing** and per-client SQLite
billing.

Every client gets its own SQLite time DB inside its own folder under the
configured clients root (`<clients-root>/<client-slug>/.pow-time/time.db`),
so billable data travels with the client folder. Time entries are either
**OPEN** (a timer running) or **CLOSED** (duration fixed). An optional
background auto-capture thread watches client folders and opens/closes
`auto` entries from file activity. Invoices aggregate billable time per task
with per-entry rate overrides, exportable as CSV (and as PDF when the
optional `reportlab` dependency is installed).

- **Dashboard UI** — a dashboard tab (`dashboard/plugin.js`), loaded by the web
  dashboard as a plain script and registered through its plugin registry, with
  the track / entries / tasks / invoice / rate views and live updates over a
  WebSocket.
- **Backend API** — a FastAPI router (`dashboard/plugin_api.py`) mounted at
  `/api/plugins/pow-tracker/` by the dashboard plugin system.
- **Plugin root** — `__init__.py` with a pass-only `register(ctx)`; the
  plugin registers no tools, hooks or middleware.

The tab is a web-dashboard bundle: a plain IIFE, no build step, no ESM. It uses
only the host plugin SDK (`window.__HERMES_PLUGIN_SDK__`) for React, the UI
primitives and the auth helpers, and registers itself through
`window.__HERMES_PLUGINS__.register`. It makes no Desktop-SDK bridge calls.

## Disclosure

What a user would want to know before installing (catalog rule 13):

- **Watches a configurable clients-root folder — only when switched on.** The
  auto-capture feature walks the configured `clients_root` tree (file **mtimes**
  only — file names and contents are never read, recorded or sent anywhere) to
  open and close time entries on folder activity. **It is off by default, per
  client:** every client — whether created in the UI or discovered as a folder —
  starts with `auto_capture` off, and is switched on per client in the plugin's
  Rate tab. The folders it walks can be limited per client via the client DB's
  own settings.
- **Runs a background capture thread.** A daemon thread inside the
  dashboard/gateway process polls every few seconds while the plugin is
  loaded. It can be switched off per client (each client DB's
  `auto_capture` setting).
- **All data is local.** Everything the plugin stores — the client registry
  and every per-client time DB — lives in plain SQLite files inside your
  clients tree. Nothing is uploaded anywhere.
- **No network calls, no telemetry, no credentials.** The plugin makes no
  outbound network requests of any kind, collects no usage data, and uses,
  stores or reads no secrets.
- **Exports download through an authenticated fetch.** Invoice PDF/CSV and the
  full entry CSV are fetched with the host's own auth helper and saved from a
  Blob, so no connection token is ever placed in a URL (previously it was).
- **The live events socket is authenticated.** `/events` checks the dashboard
  credential on the upgrade (the same gate the core dashboard sockets use) and
  refuses an unauthenticated local process with close code 4401 before it
  accepts — client names and time entries are never served to an anonymous
  connection. It is a read-only stream; writes stay on the ordinary REST routes.

**No client data ships with the plugin.** The tree contains only source;
there are no databases, exports or client names in the repository.

## Settings

Everything deployment-specific is a plugin setting (Desktop:
**Settings → Plugins → POW Tracker**), stored under
`plugins.entries.pow-tracker.settings` in the active profile's `config.yaml`
and read at plugin load — restart the gateway (or reload the plugin) after
saving. All defaults are generic.

| Setting | Type | Default | What it does |
| --- | --- | --- | --- |
| `clients_root` | str | `~/Documents/Clients` | Root folder holding one sub-directory per tracked client. `~` is expanded. Required (a generic default is provided). |
| `legacy_clients_root` | str | *(empty)* | Optional previous clients root to migrate the client registry from (its `.pow-tracker/clients.db` is copied to the current clients root). A migrated client whose folder already holds a pre-1.0 `.spd-time/time.db` keeps that DB in place. |
| `ignore_dirs` | list | `Docs, Archive, Research, Templates` | Organisational folders under the clients root that client auto-discovery ignores (not billable clients). |
| `default_rate` | float | `100.0` | Hourly rate (in `currency`) seeded into a new client's DB at first creation; each client's rate is then its own DB setting. |
| `currency` | str | `USD` | Currency label seeded into new client DBs and shown on amounts, invoices and exports (existing per-client DB settings win). |
| `company` | str | `My Company` | Company name seeded into new client DBs and printed on invoices (existing per-client DB settings win). |

Per-client behaviour (rate/currency/company, auto-capture on/off + idle
window, the default auto-capture task) is stored **per client** in that
client's own DB `settings` table and edited from the plugin's Rate tab.

**Auto-capture is per client and OFF by default.** A newly created client —
and equally a folder that client auto-discovery registered as a client — is
seeded with `auto_capture = "0"`. Switch it on per client in the Rate tab; that
toggle is the consent gesture. A client with capture off still tracks manual
timers normally.

## How a client folder is found

- The client registry is `<clients-root>/.pow-tracker/clients.db`.
- Any sub-directory of the clients root that is not ignored (`ignore_dirs`)
  or hidden (leading `.`) is auto-registered as a client; creating a client
  from the UI makes `<clients-root>/<slug>/`. Discovered folders arrive with
  auto-capture off (see above).
- Each client's time DB is `<client folder>/.pow-time/time.db`.

## Dependencies

Declared in `plugin.yaml` (`python_dependencies`), resolved under the
Hermes core constraints:

- `fastapi>=0.104.0,<1`
- `pydantic>=2.0,<3`

`reportlab` (PDF invoices) is an **optional** extra: it is imported lazily
by the `/invoice.pdf` route only, which answers a clean HTTP 500 error when
it is not installed. CSV invoice/entry export needs nothing extra.

## Development

- Local validation (the same check the plugin-catalog CI runs):

  ```bash
  hermes plugins validate /path/to/pow-tracker --install-deps
  hermes plugins doctor /path/to/pow-tracker
  ```

- Tests:

  ```bash
  python -m unittest discover -s tests -v
  ```

  (Run with a Python that has the plugin's dependencies; see
  `tests/`.)

## License

MIT — see [LICENSE](LICENSE).
