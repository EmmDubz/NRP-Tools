import discord
from discord.ext import tasks, commands
from discord import app_commands
import datetime
import os
import sqlite3
from dotenv import load_dotenv
from enum import Enum
import json
import traceback
import tempfile
from aiohttp import web

# --- Setup ---
load_dotenv()
TOKEN = os.getenv('DISCORD_TOKEN')
API_HOST = os.getenv("API_HOST", "127.0.0.1")
_api_port_raw = os.getenv("API_PORT", "").strip()
API_PORT = int(_api_port_raw) if _api_port_raw.isdigit() else 0
API_SECRET = os.getenv("API_SECRET", "").strip()
CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")

_config_cache: dict | None = None
_config_mtime: float | None = None

intents = discord.Intents.default()
intents.members = True


class NationRPBot(commands.Bot):
    async def setup_hook(self) -> None:
        if API_PORT > 0:
            self.loop.create_task(start_http_server())


bot = NationRPBot(command_prefix="!", intents=intents)

# --- Config File Loader (Hardened) ---
def load_config():
    """Loads config.json with mtime cache (invalidated after save_config)."""
    global _config_cache, _config_mtime
    try:
        mtime = os.path.getmtime(CONFIG_PATH)
    except OSError:
        print("ERROR: config.json not found.")
        _config_cache, _config_mtime = None, None
        return {}
    if _config_cache is not None and _config_mtime == mtime:
        return _config_cache
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except json.JSONDecodeError as e:
        print(f"ERROR: config.json is malformed (missing comma?): {e}")
        return {}
    _config_cache = data
    _config_mtime = mtime
    return data


def invalidate_config_cache():
    global _config_cache, _config_mtime
    _config_cache, _config_mtime = None, None


def save_config(updates: dict) -> None:
    """Merge updates into config.json and atomically replace the file."""
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
            try:
                os.remove(tmp_path)
            except OSError:
                pass
        raise
    invalidate_config_cache()


def get_db_file() -> str:
    """SQLite path: absolute paths as-is; relative paths are next to config.json."""
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
        "date_channel_name_template": config.get(
            "date_channel_name_template", "Current Date - {month_year}"
        ),
        "rp_date_format": config.get("rp_date_format", "%d %B %Y"),
        "rp_time_format": config.get("rp_time_format", "%H:%M"),
    }


def resolution_label(resolution_id: int, config: dict | None = None) -> str:
    cfg = config if config is not None else load_config()
    p = branding_from_config(cfg)["resolution_prefix"]
    return f"{p}-{resolution_id:03d}"


def format_date_channel_name(rp_now: datetime.datetime, config: dict) -> str:
    b = branding_from_config(config)
    tpl = b["date_channel_name_template"]
    return tpl.format(month_year=rp_now.strftime("%B %Y"))


def parse_utc_datetime(s: str) -> datetime.datetime:
    s = s.strip()
    if not s:
        raise ValueError("Empty datetime")
    fmts = (
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%d",
    )
    for fmt in fmts:
        try:
            dt = datetime.datetime.strptime(s, fmt)
            return dt.replace(tzinfo=datetime.timezone.utc)
        except ValueError:
            continue
    try:
        dt = datetime.datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        else:
            dt = dt.astimezone(datetime.timezone.utc)
        return dt
    except ValueError as e:
        raise ValueError(f"Could not parse datetime: {e}") from e


def snap_irl_anchor_to_now_preserving_rp() -> dict:
    """
    Set rp_anchor_real_ms to current UTC instant and rp_anchor_game_ms so that
    get_current_rp_date() returns the same value as before the change.
    """
    rp_now = get_current_rp_date()
    now = datetime.datetime.now(datetime.timezone.utc)
    updates = {
        "rp_anchor_real_ms": int(now.timestamp() * 1000),
        "rp_anchor_game_ms": int(rp_now.timestamp() * 1000),
    }
    return updates

# --- Admin Permission Check ---
def is_admin():
    """A check function to see if the user has the admin role from config."""
    async def predicate(interaction: discord.Interaction) -> bool:
        config = load_config()
        admin_role_id = config.get("admin_role_id")
        if not admin_role_id:
            return False

        if not isinstance(interaction.user, discord.Member):
            return False
        # Check if the user's roles include the admin role
        return interaction.user.get_role(admin_role_id) is not None
    return app_commands.check(predicate)

# --- Database Initialization ---
def _resolutions_column_names(cur: sqlite3.Cursor) -> set[str]:
    cur.execute("PRAGMA table_info(resolutions)")
    return {row[1] for row in cur.fetchall()}


def _ensure_resolutions_schema(cur: sqlite3.Cursor) -> None:
    """Add columns introduced after early installs (replaces old one-off migrate scripts)."""
    cur.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='resolutions' LIMIT 1"
    )
    if cur.fetchone() is None:
        return
    cols = _resolutions_column_names(cur)
    if "proposer_nation_name" not in cols:
        cur.execute("ALTER TABLE resolutions ADD COLUMN proposer_nation_name TEXT")
    if "was_force_closed" not in cols:
        cur.execute(
            "ALTER TABLE resolutions ADD COLUMN was_force_closed INTEGER NOT NULL DEFAULT 0"
        )
    if "result_status" not in cols:
        cur.execute("ALTER TABLE resolutions ADD COLUMN result_status TEXT")


def initialize_database():
    """Creates the database and tables if they don't exist; upgrades older schemas in place."""
    dbf = get_db_file()
    with sqlite3.connect(dbf) as con:
        cur = con.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        # Nations Table
        cur.execute('''
            CREATE TABLE IF NOT EXISTS nations (
                user_id INTEGER NOT NULL,
                nation_name TEXT NOT NULL,
                PRIMARY KEY (user_id, nation_name)
            )
        ''')
        # Resolutions (full schema for new installs)
        cur.execute('''
            CREATE TABLE IF NOT EXISTS resolutions (
                resolution_id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                text TEXT NOT NULL,
                proposer_name TEXT NOT NULL,
                proposer_nation_name TEXT,
                deadline_iso TEXT NOT NULL,
                original_channel_id INTEGER NOT NULL,
                is_active INTEGER NOT NULL DEFAULT 1,
                was_force_closed INTEGER NOT NULL DEFAULT 0,
                result_status TEXT
            )
        ''')
        _ensure_resolutions_schema(cur)
        # Votes Table
        cur.execute('''
            CREATE TABLE IF NOT EXISTS votes (
                resolution_id INTEGER NOT NULL,
                nation_name TEXT NOT NULL,
                vote_choice TEXT NOT NULL,
                PRIMARY KEY (resolution_id, nation_name)
            )
        ''')
        cur.execute('''
            CREATE TABLE IF NOT EXISTS reminders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                target_date_iso TEXT NOT NULL,
                message TEXT NOT NULL
            )
        ''')
        con.commit()

# --- Vote Choice Enum ---
class VoteChoice(Enum):
    aye = "Aye"
    nay = "Nay"
    abstain = "Abstain"

# --- RP Time Helper ---
def get_current_rp_date():
    """Calculates the current RP date based on config constants."""
    config = load_config()
    # Constants from config (matching your website settings)
    real_anchor_ms = config.get("rp_anchor_real_ms", 1752192000000)
    rp_anchor_ms = config.get("rp_anchor_game_ms", 978307200000)
    real_days_per_year = config.get("real_days_per_rp_year", 40)

    # Convert to seconds
    real_anchor = real_anchor_ms / 1000
    rp_anchor = rp_anchor_ms / 1000

    # Get current real time
    now = datetime.datetime.now(datetime.timezone.utc).timestamp()
    
    # Calculate elapsed real time
    real_delta = now - real_anchor

    # Calculate Time Dilation Factor
    # (Gregorian Year in Seconds / Real Seconds per RP Year)
    # 31556952 is 365.2425 days in seconds
    factor = 31556952 / (real_days_per_year * 86400)
    
    # Calculate RP Delta
    rp_delta = real_delta * factor
    
    # Result
    return datetime.datetime.fromtimestamp(rp_anchor + rp_delta, datetime.timezone.utc)


def rp_time_admin_embed() -> discord.Embed:
    cfg = load_config()
    b = branding_from_config(cfg)
    rp_now = get_current_rp_date()
    real_ms = cfg.get("rp_anchor_real_ms", 1752192000000)
    game_ms = cfg.get("rp_anchor_game_ms", 978307200000)
    days_per = cfg.get("real_days_per_rp_year", 40)
    real_dt = datetime.datetime.fromtimestamp(real_ms / 1000, tz=datetime.timezone.utc)
    game_dt = datetime.datetime.fromtimestamp(game_ms / 1000, tz=datetime.timezone.utc)
    embed = discord.Embed(
        title="RP time (baseline)",
        description=f"Current RP instant: **{rp_now.isoformat()}**",
        color=discord.Color.dark_teal(),
    )
    embed.add_field(name="IRL anchor (UTC)", value=f"`{real_dt.isoformat()}`\n`{real_ms}` ms", inline=False)
    embed.add_field(name="RP anchor (UTC)", value=f"`{game_dt.isoformat()}`\n`{game_ms}` ms", inline=False)
    embed.add_field(
        name="Dilation",
        value=f"`real_days_per_rp_year` = **{days_per}** (IRL days per in-character year)",
        inline=False,
    )
    embed.set_footer(text=f"{b['short_name']} — admin only")
    return embed


# --- HTTP API (aiohttp) ---
async def handle_rp_time(_request: web.Request) -> web.Response:
    cfg = load_config()
    rp_now = get_current_rp_date()
    real_ms = cfg.get("rp_anchor_real_ms", 1752192000000)
    game_ms = cfg.get("rp_anchor_game_ms", 978307200000)
    return web.json_response(
        {
            "rp_datetime_utc": rp_now.isoformat(),
            "rp_unix_ms": int(rp_now.timestamp() * 1000),
            "rp_unix_seconds": int(rp_now.timestamp()),
            "rp_anchor_real_ms": real_ms,
            "rp_anchor_game_ms": game_ms,
            "real_days_per_rp_year": cfg.get("real_days_per_rp_year", 40),
        }
    )


def _api_bearer_ok(request: web.Request) -> bool:
    if not API_SECRET:
        return False
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return False
    return auth[7:].strip() == API_SECRET


async def handle_reminders_get(request: web.Request) -> web.Response:
    if not _api_bearer_ok(request):
        return web.Response(status=401, text="Unauthorized")
    uid = request.rel_url.query.get("user_id")
    dbf = get_db_file()
    with sqlite3.connect(dbf) as con:
        cur = con.cursor()
        if uid and uid.isdigit():
            cur.execute(
                "SELECT id, user_id, target_date_iso, message FROM reminders WHERE user_id = ? ORDER BY target_date_iso",
                (int(uid),),
            )
        else:
            cur.execute(
                "SELECT id, user_id, target_date_iso, message FROM reminders ORDER BY target_date_iso"
            )
        rows = cur.fetchall()
    return web.json_response(
        [{"id": r[0], "user_id": r[1], "target_date_iso": r[2], "message": r[3]} for r in rows]
    )


async def handle_reminders_post(request: web.Request) -> web.Response:
    if not _api_bearer_ok(request):
        return web.Response(status=401, text="Unauthorized")
    try:
        data = await request.json()
    except (json.JSONDecodeError, TypeError, ValueError):
        return web.Response(status=400, text="Invalid JSON body")
    user_id = data.get("user_id")
    target_date = data.get("target_date", "").strip()
    message = (data.get("message") or "").strip()
    if user_id is None or not str(user_id).isdigit():
        return web.Response(status=400, text="user_id required")
    if not target_date or not message:
        return web.Response(status=400, text="target_date and message required")
    uid = int(user_id)
    target_iso = None
    for fmt in ("%Y-%m-%d", "%d/%m/%Y"):
        try:
            td = datetime.datetime.strptime(target_date, fmt)
            target_iso = td.strftime("%Y-%m-%d")
            break
        except ValueError:
            continue
    if not target_iso:
        return web.Response(status=400, text="target_date use YYYY-MM-DD or DD/MM/YYYY")
    target_dt = datetime.datetime.strptime(target_iso, "%Y-%m-%d").replace(
        tzinfo=datetime.timezone.utc
    )
    rp_now = get_current_rp_date()
    if target_dt < rp_now:
        return web.Response(status=400, text="target date is in the past for RP time")
    dbf = get_db_file()
    with sqlite3.connect(dbf) as con:
        cur = con.cursor()
        cur.execute(
            "INSERT INTO reminders (user_id, target_date_iso, message) VALUES (?, ?, ?)",
            (uid, target_iso, message),
        )
        rid = cur.lastrowid
        con.commit()
    return web.json_response({"id": rid, "user_id": uid, "target_date_iso": target_iso, "message": message})


async def handle_reminders_delete(request: web.Request) -> web.Response:
    if not _api_bearer_ok(request):
        return web.Response(status=401, text="Unauthorized")
    try:
        rid = int(request.match_info["id"])
    except (KeyError, ValueError):
        return web.Response(status=400, text="Invalid id")
    dbf = get_db_file()
    with sqlite3.connect(dbf) as con:
        cur = con.cursor()
        cur.execute("DELETE FROM reminders WHERE id = ?", (rid,))
        n = cur.rowcount
        con.commit()
    if not n:
        return web.Response(status=404, text="Not found")
    return web.Response(status=204, body=None)


async def start_http_server() -> None:
    await bot.wait_until_ready()
    app = web.Application()
    app.router.add_get("/api/rp-time", handle_rp_time)
    app.router.add_get("/api/reminders", handle_reminders_get)
    app.router.add_post("/api/reminders", handle_reminders_post)
    app.router.add_delete("/api/reminders/{id}", handle_reminders_delete)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, API_HOST, API_PORT)
    await site.start()
    print(f"HTTP API listening on http://{API_HOST}:{API_PORT}/ (GET /api/rp-time, /api/reminders with Bearer token)")


# --- Bot Ready Event ---
@bot.event
async def on_ready():
    print(f'{bot.user} has connected to Discord!')
    try:
        # 1. Start Proposal Loop
        if not check_proposals.is_running():
            check_proposals.start()
        
        # 2. Start Reminder/Renaming Loop (CRITICAL FIX)
        if not check_reminders.is_running():
            check_reminders.start()
        
        synced = await bot.tree.sync()
        print(f"Synced {len(synced)} command(s)")
    except Exception as e:
        print(f"Error in on_ready: {e}")

# --- Autocomplete Functions ---
async def nation_autocomplete(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
    with sqlite3.connect(get_db_file()) as con:
        cur = con.cursor()
        cur.execute("SELECT nation_name FROM nations WHERE user_id = ?", (interaction.user.id,))
        user_nations = [row[0] for row in cur.fetchall()]
    
    return [
        app_commands.Choice(name=nation, value=nation)
        for nation in user_nations if current.lower() in nation.lower()
    ]

async def all_nations_autocomplete(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
    with sqlite3.connect(get_db_file()) as con:
        cur = con.cursor()
        cur.execute("SELECT nation_name FROM nations ORDER BY nation_name")
        all_nations = [row[0] for row in cur.fetchall()]
    
    return [
        app_commands.Choice(name=nation, value=nation)
        for nation in all_nations if current.lower() in nation.lower()
    ]

@bot.tree.command(name="time", description="Displays the current Roleplay Date.")
async def rp_time(interaction: discord.Interaction):
    cfg = load_config()
    b = branding_from_config(cfg)
    rp_now = get_current_rp_date()
    date_str = rp_now.strftime(b["rp_date_format"])
    time_str = rp_now.strftime(b["rp_time_format"])
    
    embed = discord.Embed(title="🕰️ Current RP Time", color=discord.Color.light_grey())
    embed.add_field(name="Date", value=f"**{date_str}**", inline=True)
    embed.add_field(name="Time", value=f"{time_str}", inline=True)
    
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="remindme", description="Set a DM reminder for a specific RP Date.")
@app_commands.describe(date="Format: DD/MM/YYYY", message="What to remind you about.")
async def remindme(interaction: discord.Interaction, date: str, message: str):
    # 1. Parse the input date
    try:
        # We assume 00:00 time for the target date
        target_dt = datetime.datetime.strptime(date, "%d/%m/%Y")
        # Make it UTC aware for comparison
        target_dt = target_dt.replace(tzinfo=datetime.timezone.utc)
    except ValueError:
        await interaction.response.send_message("❌ Invalid format. Please use `DD/MM/YYYY` (e.g., 25/12/2005).", ephemeral=True)
        return

    # 2. Check if date is in the past
    rp_now = get_current_rp_date()
    if target_dt < rp_now:
        await interaction.response.send_message(f"❌ That date ({date}) has already passed in RP time!", ephemeral=True)
        return

    # 3. Save to Database
    # We store as ISO format (YYYY-MM-DD) for easy sorting/comparison
    target_iso = target_dt.strftime("%Y-%m-%d")
    
    with sqlite3.connect(get_db_file()) as con:
        cur = con.cursor()
        cur.execute(
            "INSERT INTO reminders (user_id, target_date_iso, message) VALUES (?, ?, ?)",
            (interaction.user.id, target_iso, message)
        )
        con.commit()

    await interaction.response.send_message(f"✅ I will DM you when the RP date reaches **{date}** to remind you: '{message}'", ephemeral=True)

@bot.tree.command(name="reminderlist", description="See your active RP reminders.")
async def reminder_list(interaction: discord.Interaction):
    with sqlite3.connect(get_db_file()) as con:
        cur = con.cursor()
        cur.execute("SELECT id, target_date_iso, message FROM reminders WHERE user_id = ? ORDER BY target_date_iso", (interaction.user.id,))
        rows = cur.fetchall()
    
    if not rows:
        await interaction.response.send_message("You have no active reminders.", ephemeral=True)
        return
        
    embed = discord.Embed(title="📅 Your Active Reminders", color=discord.Color.blue())
    description = ""
    for r_id, date, msg in rows:
        # Reformat date from YYYY-MM-DD to DD/MM/YYYY for display
        try:
            display_date = datetime.datetime.strptime(date, "%Y-%m-%d").strftime("%d/%m/%Y")
        except ValueError:
            display_date = date
        description += f"**ID {r_id}** (`{display_date}`): {msg}\n"
    
    embed.description = description
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="removereminder", description="Delete a specific reminder.")
@app_commands.describe(reminder_id="The ID number found in /reminderlist.")
async def remove_reminder(interaction: discord.Interaction, reminder_id: int):
    with sqlite3.connect(get_db_file()) as con:
        cur = con.cursor()
        # Ensure we only delete if it belongs to the user
        cur.execute("DELETE FROM reminders WHERE id = ? AND user_id = ?", (reminder_id, interaction.user.id))
        
        if cur.rowcount == 0:
            await interaction.response.send_message(f"❌ Could not find reminder ID `{reminder_id}` (or it doesn't belong to you).", ephemeral=True)
        else:
            con.commit()
            await interaction.response.send_message(f"✅ Reminder `{reminder_id}` deleted.", ephemeral=True)

@bot.tree.command(name="clearreminders", description="Delete ALL your active reminders.")
async def clear_reminders(interaction: discord.Interaction):
    with sqlite3.connect(get_db_file()) as con:
        cur = con.cursor()
        cur.execute("DELETE FROM reminders WHERE user_id = ?", (interaction.user.id,))
        count = cur.rowcount
        con.commit()
    
    if count == 0:
        await interaction.response.send_message("You didn't have any reminders to clear.", ephemeral=True)
    else:
        await interaction.response.send_message(f"✅ Cleared **{count}** reminders.", ephemeral=True)

# --- RP time admin (config role + UI) ---
class AnchorModal(discord.ui.Modal, title="IRL / RP anchors (UTC)"):
    irl_input = discord.ui.TextInput(
        label="IRL anchor instant (UTC)",
        style=discord.TextStyle.short,
        placeholder="e.g. 2026-01-15 18:00 or 2026-01-15T18:00:00Z",
        max_length=80,
        required=True,
    )
    rp_input = discord.ui.TextInput(
        label="RP clock instant at that IRL time (UTC)",
        style=discord.TextStyle.short,
        placeholder="e.g. 2005-03-01 00:00",
        max_length=80,
        required=True,
    )

    async def on_submit(self, interaction: discord.Interaction):
        try:
            irl = parse_utc_datetime(self.irl_input.value)
            rp = parse_utc_datetime(self.rp_input.value)
        except ValueError as e:
            await interaction.response.send_message(f"Could not parse: {e}", ephemeral=True)
            return
        save_config(
            {
                "rp_anchor_real_ms": int(irl.timestamp() * 1000),
                "rp_anchor_game_ms": int(rp.timestamp() * 1000),
            }
        )
        await interaction.response.send_message(
            "Anchor pair saved. `/rptimemanage` again to refresh the summary.",
            ephemeral=True,
        )


class SpeedModal(discord.ui.Modal, title="Dilation & IRL anchor sync"):
    days_input = discord.ui.TextInput(
        label="IRL days per RP year (blank = no change)",
        style=discord.TextStyle.short,
        placeholder="e.g. 40",
        max_length=20,
        required=False,
    )
    sync_input = discord.ui.TextInput(
        label="Type SYNC to snap IRL anchor to now",
        style=discord.TextStyle.short,
        placeholder="blank = skip; SYNC = keep current RP readout",
        max_length=10,
        required=False,
    )

    async def on_submit(self, interaction: discord.Interaction):
        updates: dict = {}
        raw_days = self.days_input.value.strip()
        if raw_days:
            try:
                d = float(raw_days)
                if d <= 0:
                    raise ValueError
                updates["real_days_per_rp_year"] = d
            except ValueError:
                await interaction.response.send_message(
                    "Invalid IRL days per RP year (need a positive number).",
                    ephemeral=True,
                )
                return
        if "SYNC" in self.sync_input.value.upper():
            updates.update(snap_irl_anchor_to_now_preserving_rp())
        if not updates:
            await interaction.response.send_message(
                "Nothing to change: set days and/or type SYNC in the second field.",
                ephemeral=True,
            )
            return
        save_config(updates)
        await interaction.response.send_message(
            "RP time settings saved. `/rptimemanage` again to refresh the summary.",
            ephemeral=True,
        )


class RpTimeManageView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=600)

    @discord.ui.button(label="Anchors (IRL ↔ RP)", style=discord.ButtonStyle.primary, row=0)
    async def anchors_btn(self, interaction: discord.Interaction, _btn: discord.ui.Button):
        await interaction.response.send_modal(AnchorModal())

    @discord.ui.button(label="Speed & IRL sync", style=discord.ButtonStyle.secondary, row=0)
    async def speed_btn(self, interaction: discord.Interaction, _btn: discord.ui.Button):
        await interaction.response.send_modal(SpeedModal())


@bot.tree.command(name="rptimemanage", description="Admin: view/edit global RP clock (anchors & dilation).")
@is_admin()
async def rp_time_manage(interaction: discord.Interaction):
    if not interaction.guild:
        await interaction.response.send_message("Use this command in a server.", ephemeral=True)
        return
    await interaction.response.send_message(
        embed=rp_time_admin_embed(), view=RpTimeManageView(), ephemeral=True
    )


# --- Admin Group ---
admin_group = app_commands.Group(name="admin", description="Admin-only commands for managing the bot.")

# --- Bot Commands ---

@bot.tree.command(name="register", description="Register a new nation under your name.")
@app_commands.describe(nation_name="The unique name of your nation")
async def register(interaction: discord.Interaction, nation_name: str):
    with sqlite3.connect(get_db_file()) as con:
        cur = con.cursor()
        try:
            cur.execute("INSERT INTO nations (user_id, nation_name) VALUES (?, ?)", (interaction.user.id, nation_name))
            con.commit()
            await interaction.response.send_message(
                f"Nation '{nation_name}' has been registered for {interaction.user.mention}.\n"
                f"Consider running `/notifications` to be pinged when votes conclude!",
                ephemeral=True
            )
        except sqlite3.IntegrityError:
            await interaction.response.send_message(f"Error: You have already registered a nation named '{nation_name}'.", ephemeral=True)

@bot.tree.command(name="propose", description="Propose a new resolution.")
@app_commands.autocomplete(nation=nation_autocomplete)
@app_commands.describe(nation="The nation proposing.", title="Resolution title.", text="Resolution text.")
async def propose(interaction: discord.Interaction, nation: str, title: str, text: str):
    config = load_config()
    ping_role_id = config.get("ping_role_id")
    comrade_role_id = config.get("comrade_role_id")
    proposals_channel_id = config.get("proposals_channel_id")

    # 1. Role Check (Must be a Comrade)
    label = branding_from_config(config)["proposer_role_label"]
    if comrade_role_id:
        if not interaction.user.get_role(comrade_role_id):
            await interaction.response.send_message(
                f"❌ You must have the **{label}** role (or configured proposer role) to propose resolutions.",
                ephemeral=True,
            )
            return

    # 2. Validate Nation Ownership
    with sqlite3.connect(get_db_file()) as con:
        cur = con.cursor()
        cur.execute("SELECT 1 FROM nations WHERE user_id = ? AND nation_name = ?", (interaction.user.id, nation))
        if cur.fetchone() is None:
            await interaction.response.send_message(f"❌ You do not own the nation '{nation}'.", ephemeral=True)
            return

        # Determine target channel (Configured channel OR current channel)
        target_channel = interaction.channel
        if proposals_channel_id:
            fetched_channel = bot.get_channel(proposals_channel_id)
            if fetched_channel:
                target_channel = fetched_channel

        deadline = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=36)
        
        cur.execute(
            "INSERT INTO resolutions (title, text, proposer_name, proposer_nation_name, deadline_iso, original_channel_id) VALUES (?, ?, ?, ?, ?, ?)",
            (title, text, interaction.user.display_name, nation, deadline.isoformat(), target_channel.id)
        )
        resolution_id = cur.lastrowid
        con.commit()

    # 3. Output (Timestamp format fixed here)
    unix_timestamp = int(deadline.timestamp())
    rl = resolution_label(resolution_id, config)
    embed = discord.Embed(title=f"Resolution {rl}: {title}", description=text, color=discord.Color.gold())
    embed.add_field(name="Deadline", value=f"Closes <t:{unix_timestamp}:R> (at <t:{unix_timestamp}:F>)", inline=False)
    embed.add_field(name="Instructions", value="Use `/vote` to cast your vote.", inline=False)
    embed.set_footer(text=f"Proposed by {nation} ({interaction.user.display_name})")
    
    # 4. Ping Logic
    ping_content = ""
    if ping_role_id:
        ping_role = interaction.guild.get_role(ping_role_id)
        if ping_role:
            ping_content = ping_role.mention

    # Send to the SPECIFIC proposals channel with the ping
    await target_channel.send(content=ping_content, embed=embed)
    
    # Confirm to user
    if target_channel != interaction.channel:
        await interaction.response.send_message(
            f"✅ Resolution `{rl}` posted in {target_channel.mention}.", ephemeral=True
        )
    else:
        await interaction.response.send_message(f"✅ Resolution `{rl}` posted.", ephemeral=True)

@bot.tree.command(name="vote", description="Vote on an active resolution.")
@app_commands.autocomplete(nation=nation_autocomplete)
@app_commands.describe(
    nation="The nation you are voting with.",
    choice="Your vote: Aye, Nay, or Abstain.",
    resolution_id="The ID (number only). Optional if only one vote is active."
)
async def vote(interaction: discord.Interaction, nation: str, choice: VoteChoice, resolution_id: int = None):
    cfg = load_config()
    with sqlite3.connect(get_db_file()) as con:
        cur = con.cursor()
        
        # 1. Validate Nation Ownership
        cur.execute("SELECT 1 FROM nations WHERE user_id = ? AND nation_name = ?", (interaction.user.id, nation))
        if cur.fetchone() is None:
            await interaction.response.send_message(f"You do not own the nation '{nation}'. Please register it or choose a valid nation.", ephemeral=True)
            return

        # 2. Determine Target Resolution
        cur.execute("SELECT resolution_id, title FROM resolutions WHERE is_active = 1")
        active_proposals = cur.fetchall()
        
        if not active_proposals:
            await interaction.response.send_message("There are no active resolutions to vote on.", ephemeral=True)
            return

        target_id = None
        if resolution_id is None:
            if len(active_proposals) == 1:
                target_id = active_proposals[0][0]
            else:
                active_ids_str = ", ".join([resolution_label(pid, cfg) for pid, title in active_proposals])
                await interaction.response.send_message(f"There are multiple active resolutions. Please specify the `resolution_id`.\nActive IDs: `{active_ids_str}`", ephemeral=True)
                return
        else:
            if any(res[0] == resolution_id for res in active_proposals):
                target_id = resolution_id
            else:
                await interaction.response.send_message(f"Resolution ID '{resolution_id}' not found or is no longer active.", ephemeral=True)
                return

        # 3. Cast the vote
        cur.execute(
            "INSERT INTO votes (resolution_id, nation_name, vote_choice) VALUES (?, ?, ?) ON CONFLICT(resolution_id, nation_name) DO UPDATE SET vote_choice = excluded.vote_choice",
            (target_id, nation, choice.name)
        )
        con.commit()
    
    await interaction.response.send_message(
        f"Vote of **{choice.value}** cast for **{nation}** on resolution **{resolution_label(target_id, cfg)}**.",
        ephemeral=True,
    )

@bot.tree.command(name="notifications", description="Turn vote result pings on or off.")
@app_commands.describe(choice="Do you want to receive pings?")
@app_commands.choices(choice=[
    app_commands.Choice(name="Yes - Ping me", value=1),
    app_commands.Choice(name="No - Stop pings", value=0)
])
async def notifications(interaction: discord.Interaction, choice: app_commands.Choice[int]):
    config = load_config()
    ping_role_id = config.get("ping_role_id")

    if not ping_role_id:
        await interaction.response.send_message("Config error: No ping role set.", ephemeral=True)
        return

    role = interaction.guild.get_role(ping_role_id)
    if not role:
        await interaction.response.send_message("Config error: Ping role not found on server.", ephemeral=True)
        return
    
    try:
        if choice.value == 1:
            await interaction.user.add_roles(role)
            await interaction.response.send_message(f"✅ Success! You received the {role.mention} role.", ephemeral=True)
        else:
            await interaction.user.remove_roles(role)
            await interaction.response.send_message(f"🔕 Success! You removed the {role.name} role.", ephemeral=True)
    except discord.Forbidden:
        await interaction.response.send_message("Error: I don't have permission to manage roles.", ephemeral=True)
# --- Info Commands (Pagination) ---
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

@bot.tree.command(name="nations", description="Lists all registered nations in the database.")
async def nations(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    with sqlite3.connect(get_db_file()) as con:
        cur = con.cursor()
        cur.execute("SELECT nation_name, user_id FROM nations ORDER BY nation_name")
        all_nations = cur.fetchall()

    if not all_nations:
        await interaction.followup.send("There are no registered nations yet.", ephemeral=True)
        return

    items_per_page = 20
    embeds = []
    for i in range(0, len(all_nations), items_per_page):
        chunk = all_nations[i:i+items_per_page]
        embed = discord.Embed(title=f"Registered Nations (Page {len(embeds) + 1})", color=discord.Color.blue())
        description = ""
        for nation, user_id in chunk:
            description += f"**{nation}** - Owned by <@{user_id}>\n"
        embed.description = description
        embeds.append(embed)
    
    view = PaginationView(embeds)
    await interaction.followup.send(embed=embeds[0], view=view, ephemeral=True)

@bot.tree.command(name="mynations", description="Lists all nations you have registered.")
async def my_nations(interaction: discord.Interaction):
    with sqlite3.connect(get_db_file()) as con:
        cur = con.cursor()
        cur.execute("SELECT nation_name FROM nations WHERE user_id = ? ORDER BY nation_name", (interaction.user.id,))
        nations = [row[0] for row in cur.fetchall()]

    if not nations:
        await interaction.response.send_message("You have not registered any nations yet.", ephemeral=True)
        return

    embed = discord.Embed(
        title=f"Your Registered Nations",
        description="\n".join(f"- {name}" for name in nations),
        color=discord.Color.green()
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="votingrecord", description="Shows the voting history for a specific nation.")
@app_commands.autocomplete(nation=all_nations_autocomplete)
@app_commands.describe(nation="The nation whose record you want to see.")
async def votingrecord(interaction: discord.Interaction, nation: str):
    await interaction.response.defer(ephemeral=True)
    with sqlite3.connect(get_db_file()) as con:
        cur = con.cursor()
        # Validation
        cur.execute("SELECT 1 FROM nations WHERE nation_name = ?", (nation,))
        if cur.fetchone() is None:
            await interaction.followup.send(f"The nation '{nation}' could not be found in the database.", ephemeral=True)
            return

        cur.execute("""
            SELECT r.resolution_id, r.title, v.vote_choice 
            FROM votes v 
            JOIN resolutions r ON v.resolution_id = r.resolution_id 
            WHERE v.nation_name = ? 
            ORDER BY r.resolution_id DESC
        """, (nation,))
        records = cur.fetchall()

    if not records:
        await interaction.followup.send(f"The nation '{nation}' has not voted on any resolutions yet.", ephemeral=True)
        return

    cfg = load_config()
    items_per_page = 10
    embeds = []
    for i in range(0, len(records), items_per_page):
        chunk = records[i:i+items_per_page]
        embed = discord.Embed(title=f"Voting Record for {nation} (Page {len(embeds) + 1})", color=discord.Color.purple())
        description = ""
        for res_id, title, choice in chunk:
            description += f"**{resolution_label(res_id, cfg)}:** {title} - **Voted {choice.capitalize()}**\n"
        embed.description = description
        embeds.append(embed)

    view = PaginationView(embeds)
    await interaction.followup.send(embed=embeds[0], view=view, ephemeral=True)

@bot.tree.command(name="resolutions", description="Lists past and active resolutions.")
async def resolutions(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    with sqlite3.connect(get_db_file()) as con:
        cur = con.cursor()
        # Fetch status and result text
        cur.execute("SELECT resolution_id, title, is_active, result_status FROM resolutions ORDER BY resolution_id DESC")
        all_res = cur.fetchall()

    if not all_res:
        await interaction.followup.send("No resolutions found.", ephemeral=True)
        return

    cfg = load_config()
    items_per_page = 15
    embeds = []
    for i in range(0, len(all_res), items_per_page):
        chunk = all_res[i:i+items_per_page]
        embed = discord.Embed(title=f"Resolutions List (Page {len(embeds) + 1})", color=discord.Color.teal())
        description = ""
        for res_id, title, is_active, result_status in chunk:
            if is_active:
                status_icon = "🟢 **Active**"
            else:
                # Use the saved result, or 'Concluded' if empty (for old legacy votes)
                final = result_status if result_status else "Concluded"
                if "Accepted" in final: icon = "✅"
                elif "Rejected" in final: icon = "❌"
                elif "Repealed" in final: icon = "🗑️"
                else: icon = "⚫"
                status_icon = f"{icon} {final}"
            
            description += f"`{resolution_label(res_id, cfg)}`: {title} — {status_icon}\n"
        embed.description = description
        embeds.append(embed)
    
    view = PaginationView(embeds)
    await interaction.followup.send(embed=embeds[0], view=view, ephemeral=True)

@bot.tree.command(name="lookup", description="Looks up the full details and votes for a specific resolution.")
@app_commands.describe(resolution_id="The ID number of the resolution (e.g., 1, 2, 3).")
async def lookup(interaction: discord.Interaction, resolution_id: int):
    await interaction.response.defer(ephemeral=True)
    with sqlite3.connect(get_db_file()) as con:
        cur = con.cursor()
        cur.execute("SELECT title, text, proposer_nation_name, deadline_iso, is_active, was_force_closed FROM resolutions WHERE resolution_id = ?", (resolution_id,))
        resolution_data = cur.fetchone()

        if not resolution_data:
            await interaction.followup.send(f"Could not find a resolution with ID `{resolution_id}`.", ephemeral=True)
            return

        title, text, proposer, deadline, is_active, was_force_closed = resolution_data
        
        # Vote Tally
        cur.execute("SELECT nation_name, vote_choice FROM votes WHERE resolution_id = ?", (resolution_id,))
        votes = cur.fetchall()

    status = "Active" if is_active else "Concluded"
    if was_force_closed:
        status = "Force Closed (Admin)"
    
    color = discord.Color.green() if is_active else discord.Color.dark_grey()
    cfg = load_config()
    embed = discord.Embed(
        title=f"Details for {resolution_label(resolution_id, cfg)}: {title}", description=text, color=color
    )
    embed.add_field(name="Status", value=status, inline=True)
    embed.add_field(name="Proposer", value=proposer or "N/A", inline=True)
    
    aye = [n for n, c in votes if c == 'aye']
    nay = [n for n, c in votes if c == 'nay']
    abstain = [n for n, c in votes if c == 'abstain']
    
    embed.add_field(name=f"Ayes ({len(aye)})", value="\n".join(f"- {n}" for n in aye) or "None", inline=False)
    embed.add_field(name=f"Nays ({len(nay)})", value="\n".join(f"- {n}" for n in nay) or "None", inline=False)
    embed.add_field(name=f"Abstains ({len(abstain)})", value="\n".join(f"- {n}" for n in abstain) or "None", inline=False)
    
    await interaction.followup.send(embed=embed, ephemeral=True)

# --- Admin Commands ---

@admin_group.command(name="close", description="Forcibly closes an active resolution without posting results.")
@app_commands.describe(resolution_id="The ID number of the resolution to close.")
@is_admin()
async def admin_close(interaction: discord.Interaction, resolution_id: int):
    with sqlite3.connect(get_db_file()) as con:
        cur = con.cursor()
        past_time = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=1)).isoformat()
        cur.execute(
            "UPDATE resolutions SET deadline_iso = ?, was_force_closed = 1 WHERE resolution_id = ? AND is_active = 1",
            (past_time, resolution_id)
        )
        if cur.rowcount == 0:
            await interaction.response.send_message(f"Could not close resolution `{resolution_id}`. It may not exist or is already inactive.", ephemeral=True)
        else:
            con.commit()
            await interaction.response.send_message(f"Resolution `{resolution_id}` has been forcibly closed. It will be cleaned up on the next cycle.", ephemeral=True)

@admin_group.command(name="deregister", description="Forcibly unregisters a nation from any user.")
@app_commands.autocomplete(nation=all_nations_autocomplete)
@app_commands.describe(nation="The nation to forcibly remove.")
@is_admin()
async def admin_deregister(interaction: discord.Interaction, nation: str):
    with sqlite3.connect(get_db_file()) as con:
        cur = con.cursor()
        cur.execute("DELETE FROM votes WHERE nation_name = ?", (nation,))
        cur.execute("DELETE FROM nations WHERE nation_name = ?", (nation,))
        
        if cur.rowcount == 0:
            await interaction.response.send_message(f"Could not find the nation '{nation}' to deregister.", ephemeral=True)
        else:
            con.commit()
            await interaction.response.send_message(f"The nation '{nation}' and all its associated votes have been successfully deregistered.", ephemeral=True)

@admin_group.command(name="transfer", description="Transfers ownership of a nation to a new user.")
@app_commands.autocomplete(nation=all_nations_autocomplete)
@app_commands.describe(nation="The nation to transfer.", new_owner="The new owner of the nation.")
@is_admin()
async def admin_transfer(interaction: discord.Interaction, nation: str, new_owner: discord.Member):
    with sqlite3.connect(get_db_file()) as con:
        cur = con.cursor()
        cur.execute("UPDATE nations SET user_id = ? WHERE nation_name = ?", (new_owner.id, nation))
        
        if cur.rowcount == 0:
            await interaction.response.send_message(f"Could not find the nation '{nation}' to transfer.", ephemeral=True)
        else:
            con.commit()
            await interaction.response.send_message(f"Ownership of '{nation}' has been transferred to {new_owner.mention}.", ephemeral=True)

@admin_group.command(name="checkactivity", description="List channels in configured categories inactive for >14 days.")
@is_admin()
async def check_activity(interaction: discord.Interaction):
    # This operation can take a while, so we defer immediately
    await interaction.response.defer(ephemeral=True)
    
    config = load_config()
    category_ids = config.get("activity_check_categories", [])
    
    if not category_ids:
        await interaction.followup.send("❌ No categories configured in 'activity_check_categories'. Check config.json.", ephemeral=True)
        return

    inactive_channels = []
    # Set threshold to 14 days ago (UTC)
    threshold = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=14)
    
    # Iterate through configured categories
    for cat_id in category_ids:
        category = bot.get_channel(cat_id)
        if not category or not isinstance(category, discord.CategoryChannel):
            continue
            
        for channel in category.channels:
            # We only care about Text Channels
            if isinstance(channel, discord.TextChannel):
                try:
                    # Fetch only the very last message
                    last_msg = None
                    # We use history(limit=1) to be fast
                    async for msg in channel.history(limit=1):
                        last_msg = msg
                    
                    if last_msg:
                        if last_msg.created_at < threshold:
                            time_str = last_msg.created_at.strftime("%Y-%m-%d")
                            inactive_channels.append(f"{channel.mention} - Last active: **{time_str}**")
                    else:
                        # Channel has 0 messages
                        inactive_channels.append(f"{channel.mention} - **Never active / Empty**")
                        
                except discord.Forbidden:
                    # Bot can't read this channel
                    continue
                except Exception as e:
                    print(f"Error reading channel {channel.id}: {e}")

    if not inactive_channels:
        await interaction.followup.send("✅ All checked channels have been active within the last 14 days!", ephemeral=True)
        return

    # Pagination logic (using the existing PaginationView class)
    items_per_page = 15
    embeds = []
    for i in range(0, len(inactive_channels), items_per_page):
        chunk = inactive_channels[i:i+items_per_page]
        embed = discord.Embed(
            title=f"⚠️ Inactive Channels (>14 Days) - Page {len(embeds) + 1}", 
            color=discord.Color.orange()
        )
        embed.description = "\n".join(chunk)
        embeds.append(embed)

    view = PaginationView(embeds)
    await interaction.followup.send(embed=embeds[0], view=view, ephemeral=True)


@admin_group.command(name="debug", description="Check the status of active resolutions.")
@is_admin()
async def admin_debug(interaction: discord.Interaction):
    with sqlite3.connect(get_db_file()) as con:
        cur = con.cursor()
        now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
        
        cur.execute("SELECT resolution_id, title, deadline_iso, is_active FROM resolutions WHERE is_active = 1")
        active_res = cur.fetchall()

    if not active_res:
        await interaction.response.send_message(f"No active resolutions found.\nCurrent Server Time: `{now_iso}`", ephemeral=True)
        return

    cfg = load_config()
    msg = f"**Current Server Time:** `{now_iso}`\n\n"
    for res_id, title, deadline, active in active_res:
        status = "EXPIRED (Should Close)" if deadline < now_iso else "Running"
        msg += f"**{resolution_label(res_id, cfg)}**: {title}\n- Deadline: `{deadline}`\n- Status: **{status}**\n\n"

    await interaction.response.send_message(msg, ephemeral=True)


bot.tree.add_command(admin_group)

# --- Background Task (Hardened) ---
@tasks.loop(minutes=1)
async def check_proposals():
    await bot.wait_until_ready()
    try:
        config = load_config()
        if not config: return

        results_channel_id = config.get("results_channel_id")
        ping_role_id = config.get("ping_role_id")

        with sqlite3.connect(get_db_file()) as con:
            cur = con.cursor()
            now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
            
            # Select Active, Expired Resolutions
            cur.execute("SELECT resolution_id, title, original_channel_id, was_force_closed FROM resolutions WHERE is_active = 1 AND deadline_iso < ?", (now_iso,))
            concluded_resolutions = cur.fetchall()

            for res_id, title, original_channel_id, was_force_closed in concluded_resolutions:
                # Handle Admin Close
                if was_force_closed:
                    cur.execute("UPDATE resolutions SET is_active = 0, result_status = 'Force Closed' WHERE resolution_id = ?", (res_id,))
                    con.commit()
                    continue 

                # Determine Channel
                target_channel_id = results_channel_id or original_channel_id
                channel = bot.get_channel(target_channel_id)
                
                # Calculate Result
                cur.execute("SELECT nation_name, vote_choice FROM votes WHERE resolution_id = ?", (res_id,))
                votes = cur.fetchall()
                aye = [n for n, c in votes if c == 'aye']
                nay = [n for n, c in votes if c == 'nay']
                abstain = [n for n, c in votes if c == 'abstain']
                
                result_str = "Accepted" if len(aye) > len(nay) else "Rejected"
                color = discord.Color.green() if result_str == "Accepted" else discord.Color.red()
                rl = resolution_label(res_id, config)
                # Build Embed
                embed = discord.Embed(
                    title=f"Vote Concluded on {rl}: {result_str}",
                    description=f"**{title}**",
                    color=color,
                )
                embed.add_field(name=f"Ayes ({len(aye)})", value="\n".join(aye) or "None", inline=True)
                embed.add_field(name=f"Nays ({len(nay)})", value="\n".join(nay) or "None", inline=True)
                embed.add_field(name=f"Abstains ({len(abstain)})", value="\n".join(abstain) or "None", inline=True)

                # Send with Ping
                if channel:
                    ping_content = ""
                    if ping_role_id:
                        ping_role = channel.guild.get_role(ping_role_id)
                        if ping_role:
                            ping_content = ping_role.mention
                    
                    await channel.send(content=ping_content, embed=embed)

                # Save Result to DB and Close
                cur.execute("UPDATE resolutions SET is_active = 0, result_status = ? WHERE resolution_id = ?", (result_str, res_id))
                con.commit()

    except Exception as e:
        print(f"Error in task: {e}")
        traceback.print_exc()

@tasks.loop(minutes=10)
async def check_reminders():
    await bot.wait_until_ready()
    try:
        print("DEBUG: check_reminders task is running...") # Debug 1
        config = load_config()
        rp_now = get_current_rp_date()
        
        # --- 1. Voice Channel Renaming ---
        date_channel_id = config.get("date_channel_id")
        print(f"DEBUG: Configured channel ID: {date_channel_id}") # Debug 2
        
        if date_channel_id:
            channel = bot.get_channel(date_channel_id)
            if channel:
                new_name = format_date_channel_name(rp_now, config)
                print(f"DEBUG: Found channel '{channel.name}'. Attempting rename to '{new_name}'") # Debug 3
                
                if channel.name != new_name:
                    try:
                        await channel.edit(name=new_name)
                        print("DEBUG: Rename successful.")
                    except Exception as e:
                        print(f"ERROR: Failed to rename channel: {e}")
            else:
                print("ERROR: Bot could not find the channel. Check ID or Bot Permissions.")
        else:
            print("DEBUG: No date_channel_id in config.")

        # --- 2. Reminder Logic ---
        current_iso = rp_now.strftime("%Y-%m-%d")

        with sqlite3.connect(get_db_file()) as con:
            cur = con.cursor()
            cur.execute("SELECT id, user_id, target_date_iso, message FROM reminders WHERE target_date_iso <= ?", (current_iso,))
            due_reminders = cur.fetchall()

            for row_id, user_id, target_date, message in due_reminders:
                user = bot.get_user(user_id)
                if user:
                    try:
                        embed = discord.Embed(title="📅 RP Date Reminder!", color=discord.Color.magenta())
                        embed.description = f"The RP Date has reached **{target_date}**."
                        embed.add_field(name="Reminder", value=message)
                        await user.send(embed=embed)
                    except discord.Forbidden:
                        print(f"Could not DM user {user_id}")
                cur.execute("DELETE FROM reminders WHERE id = ?", (row_id,))
            con.commit()

    except Exception as e:
        print(f"CRITICAL ERROR in check_reminders: {e}")
        traceback.print_exc()

@bot.tree.command(name="amend", description="Modify a resolution you proposed (Wipes current votes).")
@app_commands.describe(resolution_id="ID of resolution", new_text="The new text for the resolution.")
async def amend(interaction: discord.Interaction, resolution_id: int, new_text: str):
    config = load_config()
    ping_role_id = config.get("ping_role_id")
    
    with sqlite3.connect(get_db_file()) as con:
        cur = con.cursor()
        
        # 1. Verify ownership and active status
        cur.execute("SELECT proposer_name, is_active, title, original_channel_id FROM resolutions WHERE resolution_id = ?", (resolution_id,))
        row = cur.fetchone()
        
        if not row:
            await interaction.response.send_message("Resolution not found.", ephemeral=True)
            return
            
        proposer, is_active, title, chan_id = row
        
        if not is_active:
            await interaction.response.send_message("You cannot amend a resolution that has already concluded.", ephemeral=True)
            return

        # Simple name check. For tighter security, store proposer_user_id in DB in future, 
        # but this works if they haven't changed Discord usernames.
        if interaction.user.display_name != proposer and not is_admin_check(interaction):
            await interaction.response.send_message("Only the original proposer can amend this resolution.", ephemeral=True)
            return

        # 2. Update Text and Wipe Votes
        cur.execute("UPDATE resolutions SET text = ? WHERE resolution_id = ?", (new_text, resolution_id))
        cur.execute("DELETE FROM votes WHERE resolution_id = ?", (resolution_id,))
        con.commit()

        # 3. Notify
        channel = bot.get_channel(chan_id)
        if channel:
            ping_content = ""
            if ping_role_id:
                role = interaction.guild.get_role(ping_role_id)
                if role: ping_content = role.mention
            
            rl = resolution_label(resolution_id, config)
            await channel.send(
                f"{ping_content} ⚠️ **AMENDMENT ALERT**\n"
                f"**Resolution {rl} ({title})** has been amended by the proposer.\n"
                f"All previous votes have been cleared. Please review the new text using `/lookup {resolution_id}` and **VOTE AGAIN**."
            )

    await interaction.response.send_message(
        f"Amendment successful. Votes reset for {resolution_label(resolution_id, config)}.", ephemeral=True
    )

@bot.tree.command(name="repeal", description="Mark a passed resolution as Repealed/Ended.")
@app_commands.describe(resolution_id="ID of resolution", reason="Reason for ending it.")
async def repeal(interaction: discord.Interaction, resolution_id: int, reason: str):
    # This acts as the "End a proposal" feature
    
    # Check if user is Admin OR the original Proposer
    # Note: We need a helper for checking logic inside the command since we can't use @is_admin on just half the users
    is_admin_user = False
    config = load_config()
    if config.get("admin_role_id") and interaction.user.get_role(config.get("admin_role_id")):
        is_admin_user = True

    with sqlite3.connect(get_db_file()) as con:
        cur = con.cursor()
        cur.execute("SELECT proposer_name, result_status FROM resolutions WHERE resolution_id = ?", (resolution_id,))
        row = cur.fetchone()
        
        if not row:
            await interaction.response.send_message("Resolution not found.", ephemeral=True)
            return
            
        proposer, current_status = row
        
        # Permission Check
        if interaction.user.display_name != proposer and not is_admin_user:
            await interaction.response.send_message("Only the Admin or original Proposer can repeal this.", ephemeral=True)
            return

        # Update Status
        new_status = f"Repealed ({reason})"
        cur.execute("UPDATE resolutions SET result_status = ? WHERE resolution_id = ?", (new_status, resolution_id))
        con.commit()

    await interaction.response.send_message(
        f"Resolution `{resolution_label(resolution_id, config)}` status updated to: **{new_status}**."
    )

@bot.tree.command(name="help", description="Guide on how to use this bot.")
async def help_command(interaction: discord.Interaction):
    cfg = load_config()
    b = branding_from_config(cfg)
    embed = discord.Embed(title=f"{b['short_name']} — Bot guide", color=discord.Color.blue())

    embed.add_field(name="1️⃣ Getting Started", value=
                    "`/register [nation_name]` - Register your nation.\n"
                    "`/notifications [Yes/No]` - Turn on pings for vote results.", inline=False)
    
    embed.add_field(name="2️⃣ Voting", value=
                    "`/vote [nation] [Aye/Nay] [ID]` - Cast your vote.\n"
                    "`/resolutions` - See list of all votes.\n"
                    "`/lookup [ID]` - See details of a specific vote.", inline=False)

    embed.add_field(name="3️⃣ Proposing", value=
                    f"*(Requires **{b['proposer_role_label']}** / proposer role)*\n"
                    "`/propose` - Start a new vote (36h deadline).\n"
                    "`/amend [ID] [Text]` - Change your active proposal (Wipes votes!).\n"
                    "`/repeal [ID]` - End/Archive a passed resolution.", inline=False)

    embed.add_field(name="4️⃣ Data", value=
                    "`/mynations` - See your list.\n"
                    "`/votingrecord [nation]` - See a nation's history.", inline=False)
    
    await interaction.response.send_message(embed=embed, ephemeral=True)

# Helper for the amend check (Put this near is_admin or at top)
def is_admin_check(interaction):
    config = load_config()
    rid = config.get("admin_role_id")
    if rid and interaction.user.get_role(rid): return True
    return False

initialize_database()
bot.run(TOKEN)