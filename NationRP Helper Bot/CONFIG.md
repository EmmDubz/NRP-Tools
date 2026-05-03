# Configuration (`config.json`)

All IDs are Discord snowflakes (integers). Paths are relative to the process **current working directory** unless you use an absolute path.

## Branding & display

| Key | Default | Meaning |
|-----|---------|--------|
| `short_name` | `RP Server` | Short server name in `/help` and RP admin embed footer. |
| `resolution_prefix` | `RES` | Prefix for resolution labels (e.g. `RES-001`). |
| `proposer_role_label` | `Comrade` | Human-readable name in `/help` and propose denial text (actual permission still uses `comrade_role_id`). |
| `date_channel_name_template` | `Current Date - {month_year}` | Voice/text channel rename pattern. Placeholder: `{month_year}` → e.g. `June 2005` (`%B %Y`). |
| `rp_date_format` | `%d %B %Y` | `strftime` for `/time` date line. |
| `rp_time_format` | `%H:%M` | `strftime` for `/time` time line. |
| `database_path` | `fns_bot.db` | SQLite filename for nations, resolutions, votes, reminders. |

## Discord routing

| Key | Meaning |
|-----|--------|
| `proposals_channel_id` | Where `/propose` posts if set and the channel exists. |
| `results_channel_id` | Where concluded votes are announced; falls back to proposal channel. |
| `ping_role_id` | Role mentioned on new proposals and results. |
| `admin_role_id` | Role for `/admin` and `/rptimemanage`. |
| `comrade_role_id` | Role allowed to `/propose` (if set). |
| `date_channel_id` | Channel renamed every reminder tick to match `date_channel_name_template`. |
| `activity_check_categories` | List of category IDs scanned by `/admin checkactivity`. |

## RP time math

Stored values:

- **`rp_anchor_real_ms`** — real-world (UTC) instant in epoch **milliseconds**.
- **`rp_anchor_game_ms`** — in-character instant that **coincided** with that real instant, also as epoch ms (UTC timeline).
- **`real_days_per_rp_year`** — number of **real** days that span **one in-character year** (dilation). Smaller ⇒ faster RP years.

Current RP instant (UTC):

1. `real_delta = now_seconds - (rp_anchor_real_ms / 1000)`
2. `factor = 31556952 / (real_days_per_rp_year * 86400)`  
   (`31556952` = mean Gregorian year in seconds.)
3. `rp_delta = real_delta * factor`
4. RP datetime = `rp_anchor_game_ms / 1000 + rp_delta` as UTC.

### “SYNC” (snap IRL anchor to now)

When an admin sets **SYNC** in the speed modal, the bot:

1. Computes current RP time **before** changing anything.
2. Sets **`rp_anchor_real_ms`** to the current UTC “now” in ms.
3. Sets **`rp_anchor_game_ms`** so that the formula above still yields **that same** RP instant at “now”.

So the visible RP clock does not jump; only the baseline for future progression moves.

## Environment (`.env`)

| Variable | Required | Meaning |
|----------|----------|--------|
| `DISCORD_TOKEN` | Yes | Bot token. |
| `API_HOST` | No | Bind address (default `127.0.0.1`). |
| `API_PORT` | No | If missing or `0`, HTTP API is **off**. |
| `API_SECRET` | For reminder API | Bearer token for `/api/reminders` routes. |

See **`.env.example`**.
