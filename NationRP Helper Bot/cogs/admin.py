import discord
from discord import app_commands
from discord.ext import tasks, commands
import sqlite3
import datetime
import traceback
from .utils import get_db_file, load_config, save_config, is_admin, PaginationView, all_nations_autocomplete, get_rp_time

async def sync_org_roles_logic(bot):
    with sqlite3.connect(get_db_file()) as con:
        cur = con.cursor()
        cur.execute("SELECT org_id, role_id, name FROM organizations WHERE role_id IS NOT NULL")
        orgs = cur.fetchall()
        for oid, rid, name in orgs:
            cur.execute("SELECT DISTINCT n.user_id FROM organization_members om JOIN nations n ON om.nation_name = n.nation_name WHERE om.org_id = ?", (oid,))
            m_ids = {r[0] for r in cur.fetchall()}
            for guild in bot.guilds:
                role = guild.get_role(rid)
                if not role: continue
                for uid in m_ids:
                    m = guild.get_member(uid)
                    if m and role not in m.roles:
                        try: await m.add_roles(role)
                        except: pass
                for m in guild.members:
                    if role in m.roles and m.id not in m_ids:
                        try: await m.remove_roles(role)
                        except: pass

class AdminOrgView(discord.ui.View):
    def __init__(self, bot):
        super().__init__(timeout=600)
        self.bot = bot

    @discord.ui.button(label="Link Role to Org", style=discord.ButtonStyle.primary)
    async def link_role(self, interaction: discord.Interaction, button: discord.ui.Button):
        with sqlite3.connect(get_db_file()) as con:
            orgs = con.execute("SELECT org_id, name FROM organizations ORDER BY name").fetchall()
        if not orgs: await interaction.response.send_message("No organizations.", ephemeral=True); return
        view = discord.ui.View()
        options = [discord.SelectOption(label=n, value=str(i)) for i, n in orgs]
        sel = discord.ui.Select(placeholder="Select Organization...", options=options)
        async def callback(inter: discord.Interaction):
            oid = int(inter.data["values"][0])
            class RoleModal(discord.ui.Modal, title="Link Role ID"):
                rid_in = discord.ui.TextInput(label="Role ID", required=True)
                async def on_submit(self, sub: discord.Interaction):
                    if not self.rid_in.value.isdigit(): await sub.response.send_message("Invalid ID.", ephemeral=True); return
                    with sqlite3.connect(get_db_file()) as con:
                        con.execute("UPDATE organizations SET role_id = ? WHERE org_id = ?", (int(self.rid_in.value), oid))
                        con.commit()
                    await sub.response.send_message("✅ Linked.", ephemeral=True)
            await inter.response.send_modal(RoleModal())
        sel.callback = callback; view.add_item(sel)
        await interaction.response.send_message("Select org:", view=view, ephemeral=True)

    @discord.ui.button(label="Admin Delete Org", style=discord.ButtonStyle.danger)
    async def admin_delete(self, interaction: discord.Interaction, button: discord.ui.Button):
        with sqlite3.connect(get_db_file()) as con:
            orgs = con.execute("SELECT org_id, name FROM organizations ORDER BY name").fetchall()
        if not orgs: await interaction.response.send_message("No organizations.", ephemeral=True); return
        view = discord.ui.View()
        options = [discord.SelectOption(label=n, value=str(i)) for i, n in orgs]
        sel = discord.ui.Select(placeholder="Delete org...", options=options)
        async def callback(inter: discord.Interaction):
            oid = int(inter.data["values"][0])
            with sqlite3.connect(get_db_file()) as con:
                con.execute("DELETE FROM organizations WHERE org_id = ?", (oid,))
                con.commit()
            await inter.response.send_message("🗑️ Deleted.", ephemeral=True)
        sel.callback = callback; view.add_item(sel)
        await interaction.response.send_message("Choose org to delete:", view=view, ephemeral=True)

    @discord.ui.button(label="Global Sync Check", style=discord.ButtonStyle.secondary)
    async def global_sync(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        await sync_org_roles_logic(self.bot)
        await interaction.followup.send("✅ Role sync completed.", ephemeral=True)

class Admin(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.sync_org_roles.start()

    def cog_unload(self):
        self.sync_org_roles.cancel()

    admin_group = app_commands.Group(name="admin", description="Root group for admin actions.")

    @admin_group.command(name="org-manage", description="Admin hub for organization oversight.")
    @is_admin()
    async def admin_org_manage(self, interaction: discord.Interaction):
        await interaction.response.send_message("### 🏢 Admin Org Management", view=AdminOrgView(self.bot), ephemeral=True)

    @admin_group.command(name="checkactivity", description="List channels inactive for >14 days.")
    @is_admin()
    async def check_activity(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        config = load_config()
        cat_ids = config.get("activity_check_categories", [])
        if not cat_ids: await interaction.followup.send("No categories configured.", ephemeral=True); return
        inactive = []
        threshold = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=14)
        for cid in cat_ids:
            cat = self.bot.get_channel(cid)
            if not cat: continue
            for chan in cat.channels:
                if isinstance(chan, discord.TextChannel):
                    try:
                        last_msg = None
                        async for m in chan.history(limit=1): last_msg = m
                        if last_msg:
                            if last_msg.created_at < threshold:
                                inactive.append(f"{chan.mention} - Last: {last_msg.created_at.strftime('%Y-%m-%d')}")
                        else: inactive.append(f"{chan.mention} - Never active")
                    except: continue
        if not inactive: await interaction.followup.send("All channels active!", ephemeral=True); return
        items = 15
        embeds = []
        for i in range(0, len(inactive), items):
            embed = discord.Embed(title=f"Inactive Channels (Page {len(embeds)+1})", color=discord.Color.orange())
            embed.description = "\n".join(inactive[i:i+items])
            embeds.append(embed)
        await interaction.followup.send(embed=embeds[0], view=PaginationView(embeds), ephemeral=True)

    @admin_group.command(name="debug", description="Check status of active resolutions.")
    @is_admin()
    async def admin_debug(self, interaction: discord.Interaction):
        with sqlite3.connect(get_db_file()) as con:
            cur = con.cursor()
            now = datetime.datetime.now(datetime.timezone.utc).isoformat()
            active_res = cur.execute("SELECT resolution_id, title, deadline_iso FROM resolutions WHERE is_active = 1").fetchall()
        if not active_res: await interaction.response.send_message(f"No active resolutions.\nTime: `{now}`", ephemeral=True); return
        msg = f"**Time:** `{now}`\n\n"
        for rid, title, dl in active_res:
            st = "EXPIRED" if dl < now else "Running"
            msg += f"**{rid}**: {title} - {st} ({dl})\n"
        await interaction.response.send_message(msg, ephemeral=True)

    @admin_group.command(name="rptimemanage", description="Manage RP Time settings.")
    @is_admin()
    async def rptimemanage(self, interaction: discord.Interaction):
        class TimeModal(discord.ui.Modal, title="Manage RP Time"):
            real_ms = discord.ui.TextInput(label="Real Anchor (ms)", required=True)
            game_ms = discord.ui.TextInput(label="Game Anchor (ms)", required=True)
            days = discord.ui.TextInput(label="Real Days Per RP Year", required=True)
            async def on_submit(self, sub: discord.Interaction):
                try:
                    updates = {
                        "rp_anchor_real_ms": int(self.real_ms.value),
                        "rp_anchor_game_ms": int(self.game_ms.value),
                        "real_days_per_rp_year": float(self.days.value)
                    }
                    save_config(updates)
                    await sub.response.send_message("✅ Time settings updated.", ephemeral=True)
                except: await sub.response.send_message("❌ Invalid input.", ephemeral=True)
        
        cfg = load_config()
        modal = TimeModal()
        modal.real_ms.default = str(cfg.get("rp_anchor_real_ms", ""))
        modal.game_ms.default = str(cfg.get("rp_anchor_game_ms", ""))
        modal.days.default = str(cfg.get("real_days_per_rp_year", ""))
        await interaction.response.send_modal(modal)

    @admin_group.command(name="close", description="Force-close a resolution early.")
    async def admin_close(self, interaction: discord.Interaction, resolution_id: int):
        with sqlite3.connect(get_db_file()) as con:
            cur = con.cursor()
            cur.execute("UPDATE resolutions SET is_active = 0, was_force_closed = 1 WHERE resolution_id = ?", (resolution_id,))
            con.commit()
        await interaction.response.send_message(f"✅ Resolution {resolution_id} force-closed.", ephemeral=True)

    @admin_group.command(name="transfer", description="Transfer a nation to a new owner.")
    @app_commands.autocomplete(nation=all_nations_autocomplete)
    async def admin_transfer(self, interaction: discord.Interaction, nation: str, new_owner: discord.User):
        with sqlite3.connect(get_db_file()) as con:
            con.execute("UPDATE nations SET user_id = ? WHERE nation_name = ? COLLATE NOCASE", (new_owner.id, nation))
            con.commit()
        await interaction.response.send_message(f"✅ Nation **{nation}** transferred to <@{new_owner.id}>.", ephemeral=True)

    @admin_group.command(name="deregister", description="Delete a nation profile.")
    @app_commands.autocomplete(nation=all_nations_autocomplete)
    async def admin_deregister(self, interaction: discord.Interaction, nation: str):
        with sqlite3.connect(get_db_file()) as con:
            con.execute("DELETE FROM nations WHERE nation_name = ? COLLATE NOCASE", (nation,))
            con.commit()
        await interaction.response.send_message(f"🗑️ Nation **{nation}** deregistered.", ephemeral=True)

    @tasks.loop(minutes=10)
    async def sync_org_roles(self):
        await self.bot.wait_until_ready()
        try: await sync_org_roles_logic(self.bot)
        except Exception as e: print(f"Sync error: {e}")

async def setup(bot):
    await bot.add_cog(Admin(bot))
