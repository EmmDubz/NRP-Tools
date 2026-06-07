import os
import json
import sqlite3
import datetime
import tempfile
import discord
from typing import Optional, Union
from discord import app_commands

CONFIG_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config.json")

_config_cache: Optional[dict] = None
_config_mtime: Optional[float] = None

def load_config():
    global _config_cache, _config_mtime
    try:
        mtime = os.path.getmtime(CONFIG_PATH)
    except OSError:
        return {}
    if _config_cache is not None and _config_mtime == mtime:
        return _config_cache
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except json.JSONDecodeError:
        return {}
    _config_cache = data
    _config_mtime = mtime
    return data

def save_config(updates: dict) -> None:
    path = CONFIG_PATH
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    data.update(updates)
    d = os.path.dirname(path) or "."
    fd, tmp_path = tempfile.mkstemp(suffix=".json", prefix="config_", dir=d)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as tmp:
            json.dump(data, tmp, indent=2)
        os.replace(tmp_path, path)
    except Exception:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        raise
    global _config_cache, _config_mtime
    _config_cache, _config_mtime = None, None

class PaginationView(discord.ui.View):
    def __init__(self, embeds):
        super().__init__(timeout=120)
        self.embeds = embeds
        self.current_page = 0

    @discord.ui.button(label="Previous", style=discord.ButtonStyle.grey, emoji="⬅️")
    async def previous_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.current_page = (self.current_page - 1) % len(self.embeds)
        await interaction.response.edit_message(embed=self.embeds[self.current_page])

    @discord.ui.button(label="Next", style=discord.ButtonStyle.grey, emoji="➡️")
    async def next_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.current_page = (self.current_page + 1) % len(self.embeds)
        await interaction.response.edit_message(embed=self.embeds[self.current_page])

def get_db_file() -> str:
    cfg = load_config()
    name = cfg.get("database_path", "fns_bot.db")
    if os.path.isabs(name):
        return name
    return os.path.join(os.path.dirname(CONFIG_PATH), name)

def branding_from_config(config: dict) -> dict:
    return {
        "short_name": config.get("short_name", "RP Server"),
        "resolution_prefix": config.get("resolution_prefix", "RES"),
        "proposer_role_label": config.get("proposer_role_label", "Comrade"),
        "date_channel_name_template": config.get("date_channel_name_template", "Current Date - {month_year}"),
        "rp_date_format": config.get("rp_date_format", "%d %B %Y"),
        "rp_time_format": config.get("rp_time_format", "%H:%M"),
    }

def resolution_label(resolution_id: int, config: Optional[dict] = None) -> str:
    cfg = config if config is not None else load_config()
    p = branding_from_config(cfg)["resolution_prefix"]
    return f"{p}-{resolution_id:03d}"

def get_time_factor(real_days_per_rp_year: float) -> float:
    return 31556952 / (real_days_per_rp_year * 86400)

def get_rp_time_from_irl(irl_dt: datetime.datetime) -> datetime.datetime:
    config = load_config()
    real_anchor = config.get("rp_anchor_real_ms", 1752192000000) / 1000
    rp_anchor = config.get("rp_anchor_game_ms", 978307200000) / 1000
    factor = get_time_factor(config.get("real_days_per_rp_year", 40))
    real_delta = irl_dt.timestamp() - real_anchor
    rp_delta = real_delta * factor
    return datetime.datetime.fromtimestamp(rp_anchor + rp_delta, datetime.timezone.utc)

def get_irl_time_from_rp(rp_dt: datetime.datetime) -> datetime.datetime:
    config = load_config()
    real_anchor = config.get("rp_anchor_real_ms", 1752192000000) / 1000
    rp_anchor = config.get("rp_anchor_game_ms", 978307200000) / 1000
    factor = get_time_factor(config.get("real_days_per_rp_year", 40))
    rp_delta = rp_dt.timestamp() - rp_anchor
    real_delta = rp_delta / factor
    return datetime.datetime.fromtimestamp(real_anchor + real_delta, datetime.timezone.utc)

def get_rp_time():
    return get_rp_time_from_irl(datetime.datetime.now(datetime.timezone.utc))

def format_date_channel_name(rp_now: datetime.datetime, config: dict) -> str:
    b = branding_from_config(config)
    tpl = b["date_channel_name_template"]
    return tpl.format(month_year=rp_now.strftime("%B %Y"))

def is_admin_check(interaction: discord.Interaction):
    config = load_config()
    rid = config.get("admin_role_id")
    if rid and isinstance(interaction.user, discord.Member) and interaction.user.get_role(rid):
        return True
    return False

def is_admin():
    async def predicate(interaction: discord.Interaction) -> bool:
        return is_admin_check(interaction)
    return app_commands.check(predicate)

async def all_nations_autocomplete(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
    dbf = get_db_file()
    with sqlite3.connect(dbf) as con:
        cur = con.cursor()
        cur.execute("SELECT nation_name FROM nations ORDER BY nation_name")
        all_nations = [row[0] for row in cur.fetchall()]
    return [app_commands.Choice(name=n, value=n) for n in all_nations if current.lower() in n.lower()][:25]

async def user_nations_autocomplete(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
    dbf = get_db_file()
    with sqlite3.connect(dbf) as con:
        cur = con.cursor()
        cur.execute("SELECT nation_name FROM nations WHERE user_id = ? ORDER BY nation_name", (interaction.user.id,))
        nations = [row[0] for row in cur.fetchall()]
    return [app_commands.Choice(name=n, value=n) for n in nations if current.lower() in n.lower()][:25]

def initialize_database():
    dbf = get_db_file()
    with sqlite3.connect(dbf) as con:
        cur = con.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("CREATE TABLE IF NOT EXISTS nations (user_id INTEGER NOT NULL, nation_name TEXT NOT NULL, PRIMARY KEY (user_id, nation_name))")
        cur.execute("CREATE TABLE IF NOT EXISTS resolutions (resolution_id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL, text TEXT NOT NULL, proposer_name TEXT NOT NULL, proposer_nation_name TEXT, deadline_iso TEXT NOT NULL, original_channel_id INTEGER NOT NULL, is_active INTEGER NOT NULL DEFAULT 1, was_force_closed INTEGER NOT NULL DEFAULT 0, result_status TEXT, message_id INTEGER)")
        
        # Schema upgrades
        cur.execute("PRAGMA table_info(nations)")
        nat_cols = {row[1] for row in cur.fetchall()}
        for col, col_type in [("bio", "TEXT"), ("currency_name", "TEXT"), ("currency_symbol", "TEXT"), ("wiki_link", "TEXT"), ("currency_peg_target", "TEXT"), ("currency_peg_rate", "REAL")]:
            if col not in nat_cols:
                cur.execute(f"ALTER TABLE nations ADD COLUMN {col} {col_type}")

        cur.execute("PRAGMA table_info(resolutions)")
        res_cols = {row[1] for row in cur.fetchall()}
        if "proposer_nation_name" not in res_cols: cur.execute("ALTER TABLE resolutions ADD COLUMN proposer_nation_name TEXT")
        if "proposing_country" not in res_cols: cur.execute("ALTER TABLE resolutions ADD COLUMN proposing_country TEXT")
        if "was_force_closed" not in res_cols: cur.execute("ALTER TABLE resolutions ADD COLUMN was_force_closed INTEGER NOT NULL DEFAULT 0")
        if "result_status" not in res_cols: cur.execute("ALTER TABLE resolutions ADD COLUMN result_status TEXT")
        if "message_id" not in res_cols: cur.execute("ALTER TABLE resolutions ADD COLUMN message_id INTEGER")

        cur.execute("CREATE TABLE IF NOT EXISTS votes (resolution_id INTEGER NOT NULL, nation_name TEXT NOT NULL, vote_choice TEXT NOT NULL, PRIMARY KEY (resolution_id, nation_name))")
        cur.execute("CREATE TABLE IF NOT EXISTS reminders (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, target_date_iso TEXT NOT NULL, message TEXT NOT NULL)")
        cur.execute("CREATE TABLE IF NOT EXISTS organizations (org_id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT UNIQUE NOT NULL, owner_id INTEGER NOT NULL, role_id INTEGER, is_invite_only INTEGER DEFAULT 0)")
        cur.execute("CREATE TABLE IF NOT EXISTS organization_members (org_id INTEGER NOT NULL, nation_name TEXT NOT NULL, PRIMARY KEY (org_id, nation_name), FOREIGN KEY (org_id) REFERENCES organizations(org_id) ON DELETE CASCADE)")
        cur.execute("CREATE TABLE IF NOT EXISTS organization_requests (org_id INTEGER NOT NULL, nation_name TEXT NOT NULL, user_id INTEGER NOT NULL, PRIMARY KEY (org_id, nation_name), FOREIGN KEY (org_id) REFERENCES organizations(org_id) ON DELETE CASCADE)")
        con.commit()
