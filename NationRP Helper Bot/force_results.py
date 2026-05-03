import datetime
import json
import os
import sqlite3

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

# --- CONFIGURATION ---
RES_ID = 1  # <--- REPLACE THIS WITH YOUR STUCK RESOLUTION ID


def _db_path() -> str:
    cfg_path = os.path.join(SCRIPT_DIR, "config.json")
    try:
        with open(cfg_path, encoding="utf-8") as f:
            cfg = json.load(f)
    except (OSError, json.JSONDecodeError):
        cfg = {}
    name = cfg.get("database_path", "fns_bot.db")
    if os.path.isabs(name):
        return name
    return os.path.join(SCRIPT_DIR, name)


def force_revive():
    print(f"Attempting to revive Resolution ID {RES_ID}...")

    con = sqlite3.connect(_db_path())
    cur = con.cursor()
    
    # Check if it exists first
    cur.execute("SELECT title FROM resolutions WHERE resolution_id = ?", (RES_ID,))
    row = cur.fetchone()
    if not row:
        print("Error: Resolution ID not found in database.")
        con.close()
        return

    print(f"Found resolution: '{row[0]}'")

    # Set deadline to 10 minutes ago
    past_time = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=10)).isoformat()

    # We set:
    # 1. is_active = 1 (So the bot sees it)
    # 2. was_force_closed = 0 (In case it was accidentally admin-closed)
    # 3. deadline = Past (So the bot triggers the conclusion immediately)
    cur.execute("""
        UPDATE resolutions 
        SET is_active = 1, was_force_closed = 0, deadline_iso = ? 
        WHERE resolution_id = ?
    """, (past_time, RES_ID))
    
    con.commit()
    con.close()
    print("Success! Resolution marked as active and expired.")
    print("The bot should auto-post the results within 60 seconds.")

if __name__ == "__main__":
    force_revive()