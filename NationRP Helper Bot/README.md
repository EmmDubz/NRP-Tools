# Nation RP Discord bot

A modular Discord bot for roleplay communities. It manages **in-character resolutions**, tracks an **RP calendar**, handles **nation profiles**, and facilitates **organizational management**.

## Features

- **RP Time**: Derived from real-world UTC anchors. Supports automatic voice channel renaming to display the current date.
- **Nations**: Register and manage nation profiles with custom bios and currency settings.
- **Organizations**: Create and join organizations (open or invite-only) with automated member role synchronization.
- **Voting Hub**: Propose, vote on, and track resolutions. Automatic conclusion of votes based on duration.
- **Admin Tools**: Comprehensive tools for managing nations, organizations, and activity checks across categories.
- **HTTP API**: Optional API for exposing RP time and managing reminders.

## Project Structure

The bot is organized into **Cogs** for modularity:
- `cogs/time_mgmt.py`: RP time calculation, `/time`, and DM reminders.
- `cogs/voting.py`: Voting system, `/propose`, and results processing.
- `cogs/nations.py`: Nation registration and `/mynations` management GUI.
- `cogs/organizations.py`: Organization browser and membership management.
- `cogs/admin.py`: Administrative oversight and role synchronization tasks.
- `cogs/utils.py`: Shared utilities and database logic.

## Prerequisites

- **Python 3.9+**
- **discord.py 2.0+**
- A Discord bot token with **Server Members Intent** enabled.

## Install

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

## Configure

1. Copy **[`.env.example`](.env.example)** to **`.env`** and set your token.
2. Copy **[`config.example.json`](config.example.json)** to **`config.json`**. See **[`CONFIG.md`](CONFIG.md)** for key details.

## Run

```bash
python bot.py
```

## Database

Uses SQLite (**`fns_bot.db`** by default). Schema migrations are handled automatically on startup.

## Tools

- **`force_results.py`**: A manual utility to revive or conclude a stuck resolution by ID.
