# Nation RP bot (Discord)

Discord bot for nation-style roleplay: resolutions, votes, RP-relative calendar time, reminders, and an optional HTTP API for websites or tooling.

## Quick start

1. **Python 3.10+** recommended.
2. Clone the repo and create a virtual environment:

   ```bash
   python -m venv .venv
   .venv\Scripts\activate
   pip install -r requirements.txt
   ```

3. Copy **[`.env.example`](.env.example)** to **`.env`** and set `DISCORD_TOKEN` (and optional API variables — see below).
4. Copy **[`config.example.json`](config.example.json)** to **`config.json`** and set channel/role IDs and RP time settings. See **[`CONFIG.md`](CONFIG.md)** for every key.
5. Invite the bot with **applications.commands** and required intents (members if you use role checks).
6. Run:

   ```bash
   python bot.py
   ```

Slash commands sync on startup.

## RP time

RP time is derived from two UTC **anchors** (an IRL instant and the in-character instant that aligned with it) plus **`real_days_per_rp_year`** (how many real-world days equal one in-character year). See **CONFIG.md** for the formula.

- **`/time`** — current RP date/time for anyone.
- **`/rptimemanage`** — **admin role only** (same role as `/admin`). Shows a panel with two buttons:
  - **Anchors (IRL ↔ RP)** — modal to set both anchor datetimes (UTC). Use this for backdating or full realignments.
  - **Speed & IRL sync** — optional new `real_days_per_rp_year`, and/or type **`SYNC`** in the sync field to set the IRL anchor to “now” while keeping the **current RP readout** unchanged.

## HTTP API (optional)

If **`API_PORT`** in `.env` is a positive integer, the bot starts an **aiohttp** server on **`API_HOST`:`API_PORT`**.

| Endpoint | Auth | Description |
|----------|------|-------------|
| `GET /api/rp-time` | None | JSON with current RP instant and anchor settings. |
| `GET /api/reminders` | `Authorization: Bearer <API_SECRET>` | List reminders; optional query `user_id`. |
| `POST /api/reminders` | Bearer | JSON body: `user_id`, `target_date` (`YYYY-MM-DD` or `DD/MM/YYYY`), `message`. |
| `DELETE /api/reminders/{id}` | Bearer | Remove one reminder row. |

Set a strong random **`API_SECRET`** for any reminder endpoint. **`GET /api/rp-time`** does not use the secret.

**Choosing a port:** use any free TCP port on your host. If you already use **80, 443, 7830, 8080, 8081, 8082**, pick something else (e.g. **8090** or **8765**).

## Branding

Server-specific strings come from **`config.json`**: `short_name`, `resolution_prefix`, `proposer_role_label`, date/time formats, and the date channel name template. Defaults match a generic RP server if keys are omitted.

## Database migrations

Older deployments may need the standalone migration scripts in the repo (`migrate.py`, `migrate2.py`) once, if columns were added before `CREATE TABLE` was updated.

## License

Add your preferred license when publishing to GitHub.
