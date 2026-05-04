# Configuration reference (`config.json`)

Values are read from **`config.json`** in the same directory as **`bot.py`**. Integer IDs are Discord snowflakes. **`database_path`**: if relative, it is resolved against the **directory containing `config.json`** (same folder as `bot.py`); absolute paths are used as given.

## Branding and display

| Key | Default | Meaning |
|-----|---------|--------|
| `short_name` | `RP Server` | Short name in `/help` and RP admin embed footer. |
| `resolution_prefix` | `RES` | Prefix for resolution labels (e.g. `RES-001`). |
| `proposer_role_label` | `Comrade` | Label shown in `/help` and propose denial text; access still uses `comrade_role_id`. |
| `date_channel_name_template` | `Current Date - {month_year}` | Pattern for renaming the configured date channel. Placeholder: `{month_year}` (e.g. `June 2005`). |
| `rp_date_format` | `%d %B %Y` | `strftime` pattern for `/time` date. |
| `rp_time_format` | `%H:%M` | `strftime` pattern for `/time` time. |
| `database_path` | `fns_bot.db` | SQLite database for nations, resolutions, votes, reminders (relative → next to `config.json`). |

## Discord routing

| Key | Meaning |
|-----|--------|
| `proposals_channel_id` | If set and valid, `/propose` posts there; otherwise the current channel. |
| `results_channel_id` | Where concluded votes are posted; falls back to the proposal’s channel. |
| `ping_role_id` | Role mentioned on new proposals and on results. |
| `admin_role_id` | Role allowed to use all **`/admin`** subcommands (including **`rptimemanage`**). |
| `comrade_role_id` | If set, only members with this role may `/propose`. |
| `date_channel_id` | If set, this channel is renamed on a schedule to match `date_channel_name_template`. |
| `activity_check_categories` | Category IDs scanned by `/admin checkactivity`. |

## RP time mathematics

Stored values:

- **`rp_anchor_real_ms`** — real-world UTC instant, epoch **milliseconds**.
- **`rp_anchor_game_ms`** — in-character instant that aligned with that real instant, epoch ms on a UTC timeline.
- **`real_days_per_rp_year`** — real-world days per in-character year (smaller values mean faster in-character years).

Current RP instant (UTC):

1. `real_delta = now_seconds - (rp_anchor_real_ms / 1000)`
2. `factor = 31556952 / (real_days_per_rp_year * 86400)` (mean Gregorian year in seconds)
3. `rp_delta = real_delta * factor`
4. RP datetime = `rp_anchor_game_ms / 1000 + rp_delta` as UTC.

### `SYNC` (snap IRL anchor to now)

When an admin submits **`SYNC`** in the speed modal, the bot:

1. Computes the current RP instant using the existing configuration.
2. Sets **`rp_anchor_real_ms`** to the current UTC time in ms.
3. Adjusts **`rp_anchor_game_ms`** so the formula still returns the **same** RP instant at that moment.

The on-screen RP clock does not jump; only the baseline for future progression changes.

## Environment variables (`.env`)

| Variable | Required | Meaning |
|----------|----------|--------|
| `DISCORD_TOKEN` | Yes | Bot token from the Developer Portal. |
| `API_HOST` | No | HTTP bind address (default `0.0.0.0`). |
| `API_PORT` | No | If unset or `0`, the HTTP API is disabled. |
| `API_SECRET` | For all routes | Bearer secret for all `/api/` endpoints. |

### API Endpoints

- `GET /api/time`: Current RP date and configuration.
- `GET /api/nations`: Summary list of all registered nations.
- `GET /api/nations/{name}`: Full details for a specific nation, including organizations.
- `GET /api/organizations`: Summary list of all organizations.
- `GET /api/organizations/{id}`: Full details for an organization, including member list.
- `GET /api/voting/proposals`: History and current status of all resolution proposals.

**Authentication**: Include `Authorization: Bearer <API_SECRET>` in your request headers.

See **`.env.example`** for a template.
