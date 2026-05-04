import discord
from discord import app_commands
from discord.ext import tasks, commands
import datetime
import sqlite3
from .utils import load_config, branding_from_config, get_rp_time, get_db_file, format_date_channel_name

class TimeManagement(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.check_reminders.start()

    def cog_unload(self):
        self.check_reminders.cancel()

    @app_commands.command(name="time", description="Displays the current Roleplay Date.")
    async def rp_time(self, interaction: discord.Interaction):
        cfg = load_config()
        b = branding_from_config(cfg)
        rp_now = get_rp_time()
        date_str = rp_now.strftime(b["rp_date_format"])
        time_str = rp_now.strftime(b["rp_time_format"])
        
        embed = discord.Embed(title="🕰️ Current RP Time", color=discord.Color.light_grey())
        embed.add_field(name="Date", value=f"**{date_str}**", inline=True)
        embed.add_field(name="Time", value=f"{time_str}", inline=True)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="remindme", description="Set a DM reminder for a specific RP Date.")
    @app_commands.describe(date="Format: DD/MM/YYYY", message="What to remind you about.")
    async def remindme(self, interaction: discord.Interaction, date: str, message: str):
        try:
            target_dt = datetime.datetime.strptime(date, "%d/%m/%Y").replace(tzinfo=datetime.timezone.utc)
        except ValueError:
            await interaction.response.send_message("❌ Invalid format. Please use `DD/MM/YYYY` (e.g., 25/12/2005).", ephemeral=True)
            return

        rp_now = get_rp_time()
        if target_dt < rp_now:
            await interaction.response.send_message(f"❌ That date ({date}) has already passed in RP time!", ephemeral=True)
            return

        target_iso = target_dt.strftime("%Y-%m-%d")
        with sqlite3.connect(get_db_file()) as con:
            cur = con.cursor()
            cur.execute("INSERT INTO reminders (user_id, target_date_iso, message) VALUES (?, ?, ?)",
                        (interaction.user.id, target_iso, message))
            con.commit()
        await interaction.response.send_message(f"✅ I will DM you when the RP date reaches **{date}** to remind you: '{message}'", ephemeral=True)

    @app_commands.command(name="reminderlist", description="See your active RP reminders.")
    async def reminder_list(self, interaction: discord.Interaction):
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
            try:
                display_date = datetime.datetime.strptime(date, "%Y-%m-%d").strftime("%d/%m/%Y")
            except ValueError:
                display_date = date
            description += f"**ID {r_id}** (`{display_date}`): {msg}\n"
        embed.description = description
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="removereminder", description="Delete a specific reminder.")
    @app_commands.describe(reminder_id="The ID number found in /reminderlist.")
    async def remove_reminder(self, interaction: discord.Interaction, reminder_id: int):
        with sqlite3.connect(get_db_file()) as con:
            cur = con.cursor()
            cur.execute("DELETE FROM reminders WHERE id = ? AND user_id = ?", (reminder_id, interaction.user.id))
            if cur.rowcount == 0:
                await interaction.response.send_message(f"❌ Could not find reminder ID `{reminder_id}` (or it doesn't belong to you).", ephemeral=True)
            else:
                con.commit()
                await interaction.response.send_message(f"✅ Reminder `{reminder_id}` deleted.", ephemeral=True)

    @tasks.loop(minutes=10)
    async def check_reminders(self):
        await self.bot.wait_until_ready()
        try:
            config = load_config()
            rp_now = get_rp_time()
            date_channel_id = config.get("date_channel_id")
            if date_channel_id:
                channel = self.bot.get_channel(date_channel_id)
                if channel:
                    new_name = format_date_channel_name(rp_now, config)
                    if channel.name != new_name:
                        await channel.edit(name=new_name)
            current_iso = rp_now.strftime("%Y-%m-%d")
            with sqlite3.connect(get_db_file()) as con:
                cur = con.cursor()
                cur.execute("SELECT id, user_id, message FROM reminders WHERE target_date_iso <= ?", (current_iso,))
                due = cur.fetchall()
                for rid, uid, msg in due:
                    user = self.bot.get_user(uid)
                    if user:
                        try: await user.send(f"📅 **RP Reminder:** {msg}")
                        except: pass
                    cur.execute("DELETE FROM reminders WHERE id = ?", (rid,))
                con.commit()
        except Exception as e:
            print(f"Error in check_reminders: {e}")

async def setup(bot):
    await bot.add_cog(TimeManagement(bot))
