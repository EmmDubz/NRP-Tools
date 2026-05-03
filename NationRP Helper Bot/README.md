# Nation RP Discord bot

A Discord bot for communities that run **in-character resolutions** and votes, track an **RP calendar** against real time, send **RP-date reminders** by DM, and optionally expose a small **HTTP API** for a website or other automation.

This folder is usually used as part of the **NRP-Tools** monorepo (see the parent directory’s **README**). Open a terminal **inside this folder** (`NationRP Helper Bot`) before running the commands below.

## Prerequisites

- **Python 3.10+**
- A **Discord application** with a bot user and token ([Discord Developer Portal](https://discord.com/developers/applications))
- **Privileged intents** as needed: the bot uses the **Server Members** intent for role-based checks. Enable it in the portal and when generating the invite URL.

## Install

**Windows (PowerShell):**

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

**macOS / Linux:**

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Configure

1. Copy **[`.env.example`](.env.example)** to **`.env`**. Set **`DISCORD_TOKEN`** to your bot token. Optionally set **`API_HOST`**, **`API_PORT`**, and **`API_SECRET`** if you use the HTTP API (see below).

2. Copy **[`config.example.json`](config.example.json)** to **`config.json`**. Fill in channel and role IDs and RP time settings. Every key is described in **[`CONFIG.md`](CONFIG.md)**.

3. **Invite URL:** include the **`applications.commands`** scope so slash commands work. Grant permissions your server needs (e.g. Send Messages, Embed Links, Manage Roles if you use the ping role, **Manage Channels** only if you rely on automatic channel renaming for the date display).

## Run

```bash
python bot.py
```

Slash commands are synced when the bot connects.

## Features (summary)

| Area | Notes |
|------|--------|
| **RP time** | Derived from two UTC anchors and `real_days_per_rp_year`; see **CONFIG.md** for the formula. |
| **`/time`** | Anyone can see the current RP date/time. |
| **`/rptimemanage`** | Users with the configured **admin** role get a panel to edit anchors and dilation; optional **`SYNC`** aligns the IRL anchor to “now” without jumping the RP clock. |
| **HTTP API** | If **`API_PORT`** in `.env` is a positive integer, **`GET /api/rp-time`** is public JSON. Reminder routes require **`Authorization: Bearer <API_SECRET>`**. Use a free port (avoid conflicting with existing services on the host). |

## Branding

Display strings (server short name, resolution prefix, proposer role label, date formats, date channel rename template) are set in **`config.json`** so one codebase can serve different communities.

## Database

SQLite defaults to **`fns_bot.db`**. Set **`database_path`** in `config.json` to change the file name; relative paths are resolved **next to `config.json`** (the bot folder), not the shell’s current directory. On first startup the bot creates tables and **adds any missing columns** on older databases automatically.

## License

The repository root should contain a **LICENSE** file chosen by the maintainer; until then, treat usage as unspecified and ask the author for terms.
