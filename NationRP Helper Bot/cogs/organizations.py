import discord
from discord import app_commands
from discord.ext import commands
import sqlite3
from .utils import get_db_file

class CreateOrgModal(discord.ui.Modal, title="Create New Organization"):
    name_input = discord.ui.TextInput(label="Organization Name", placeholder="e.g. United Nations, SEATO", required=True, min_length=3, max_length=50)
    privacy_input = discord.ui.TextInput(label="Privacy (Open/Invite)", placeholder="Type 'Open' or 'Invite'", required=True, default="Open")
    async def on_submit(self, interaction: discord.Interaction):
        is_invite = 1 if self.privacy_input.value.lower() == "invite" else 0
        with sqlite3.connect(get_db_file()) as con:
            try:
                con.execute("INSERT INTO organizations (name, owner_id, is_invite_only) VALUES (?, ?, ?)", (self.name_input.value, interaction.user.id, is_invite))
                con.commit()
                await interaction.response.send_message(f"✅ Organization **{self.name_input.value}** created!", ephemeral=True)
            except sqlite3.IntegrityError:
                await interaction.response.send_message("❌ An organization with that name already exists.", ephemeral=True)

class OrgReviewRequestsView(discord.ui.View):
    def __init__(self, org_id: int, org_name: str):
        super().__init__(timeout=300)
        self.org_id = org_id; self.org_name = org_name
        self.update_select()
    def update_select(self):
        with sqlite3.connect(get_db_file()) as con:
            requests = con.execute("SELECT nation_name FROM organization_requests WHERE org_id = ?", (self.org_id,)).fetchall()
        if not requests: self.clear_items(); return
        options = [discord.SelectOption(label=n[0], value=n[0]) for n in requests]
        select = discord.ui.Select(placeholder="Select request to review...", options=options, custom_id="req_sel")
        select.callback = self.request_callback
        for item in self.children:
            if getattr(item, "custom_id", None) == "req_sel": self.remove_item(item); break
        self.add_item(select)
    async def request_callback(self, interaction: discord.Interaction):
        self.selected_nation = interaction.data["values"][0]
        # Clear old buttons
        for i in [i for i in self.children if isinstance(i, discord.ui.Button)]: self.remove_item(i)
        btn_appr = discord.ui.Button(label="Approve", style=discord.ButtonStyle.success)
        btn_appr.callback = self.approve_callback
        btn_rej = discord.ui.Button(label="Reject", style=discord.ButtonStyle.danger)
        btn_rej.callback = self.reject_callback
        self.add_item(btn_appr); self.add_item(btn_rej)
        await interaction.response.edit_message(content=f"Reviewing: **{self.selected_nation}**", view=self)
    async def approve_callback(self, interaction: discord.Interaction):
        with sqlite3.connect(get_db_file()) as con:
            con.execute("INSERT INTO organization_members (org_id, nation_name) VALUES (?, ?)", (self.org_id, self.selected_nation))
            con.execute("DELETE FROM organization_requests WHERE org_id = ? AND nation_name = ?", (self.org_id, self.selected_nation))
            con.commit()
        await interaction.response.send_message(f"✅ Approved **{self.selected_nation}**.", ephemeral=True)
        self.update_select(); await interaction.edit_original_response(content="Reviewing...", view=self)
    async def reject_callback(self, interaction: discord.Interaction):
        with sqlite3.connect(get_db_file()) as con:
            con.execute("DELETE FROM organization_requests WHERE org_id = ? AND nation_name = ?", (self.org_id, self.selected_nation))
            con.commit()
        await interaction.response.send_message(f"❌ Rejected **{self.selected_nation}**.", ephemeral=True)
        self.update_select(); await interaction.edit_original_response(content="Reviewing...", view=self)

class OrgDetailsView(discord.ui.View):
    def __init__(self, org_id: int, user_id: int):
        super().__init__(timeout=600)
        self.org_id = org_id; self.user_id = user_id
        self.refresh()
    def refresh(self):
        self.clear_items()
        with sqlite3.connect(get_db_file()) as con:
            cur = con.cursor()
            self.org_data = cur.execute("SELECT name, owner_id, is_invite_only FROM organizations WHERE org_id = ?", (self.org_id,)).fetchone()
            self.user_nations_in = [r[0] for r in cur.execute("SELECT om.nation_name FROM organization_members om JOIN nations n ON om.nation_name = n.nation_name WHERE om.org_id = ? AND n.user_id = ?", (self.org_id, self.user_id)).fetchall()]
            self.pending = [r[0] for r in cur.execute("SELECT nation_name FROM organization_requests WHERE org_id = ? AND user_id = ?", (self.org_id, self.user_id)).fetchall()]
        name, owner_id, is_invite = self.org_data
        if self.user_id == owner_id:
            btn_disband = discord.ui.Button(label="Disband", style=discord.ButtonStyle.danger, row=1)
            btn_disband.callback = self.disband_callback
            btn_priv = discord.ui.Button(label="Toggle Privacy", style=discord.ButtonStyle.secondary, row=1)
            btn_priv.callback = self.toggle_privacy_callback
            btn_rev = discord.ui.Button(label="Review Requests", style=discord.ButtonStyle.primary, row=1)
            btn_rev.callback = self.review_callback
            self.add_item(btn_disband); self.add_item(btn_priv); self.add_item(btn_rev)
        btn_manage = discord.ui.Button(label="Join/Leave", style=discord.ButtonStyle.success, row=2)
        btn_manage.callback = self.manage_callback
        self.add_item(btn_manage)
    async def disband_callback(self, interaction: discord.Interaction):
        with sqlite3.connect(get_db_file()) as con:
            con.execute("DELETE FROM organizations WHERE org_id = ?", (self.org_id,))
            con.commit()
        await interaction.response.send_message("🗑️ Disbanded.", ephemeral=True)
    async def toggle_privacy_callback(self, interaction: discord.Interaction):
        new_val = 1 - self.org_data[2]
        with sqlite3.connect(get_db_file()) as con:
            con.execute("UPDATE organizations SET is_invite_only = ? WHERE org_id = ?", (new_val, self.org_id))
            con.commit()
        self.refresh(); await interaction.response.send_message("🔒 Privacy updated.", ephemeral=True)
    async def review_callback(self, interaction: discord.Interaction):
        view = OrgReviewRequestsView(self.org_id, self.org_data[0])
        if not any(isinstance(i, discord.ui.Select) for i in view.children):
            await interaction.response.send_message("No requests.", ephemeral=True)
            return
        await interaction.response.send_message("Reviewing requests...", view=view, ephemeral=True)
    async def manage_callback(self, interaction: discord.Interaction):
        with sqlite3.connect(get_db_file()) as con:
            nations = [r[0] for r in con.execute("SELECT nation_name FROM nations WHERE user_id = ?", (self.user_id,)).fetchall()]
        if not nations: await interaction.response.send_message("No nations.", ephemeral=True); return
        view = discord.ui.View()
        options = []
        for n in nations:
            st = " (Member)" if n in self.user_nations_in else (" (Pending)" if n in self.pending else "")
            options.append(discord.SelectOption(label=f"{n}{st}", value=n))
        sel = discord.ui.Select(options=options)
        async def sub(inter: discord.Interaction):
            nat = inter.data["values"][0]
            with sqlite3.connect(get_db_file()) as con:
                cur = con.cursor()
                if nat in self.user_nations_in: cur.execute("DELETE FROM organization_members WHERE org_id = ? AND nation_name = ?", (self.org_id, nat)); msg = "Left."
                elif nat in self.pending: msg = "Already pending."
                elif self.org_data[2]: cur.execute("INSERT INTO organization_requests (org_id, nation_name, user_id) VALUES (?, ?, ?)", (self.org_id, nat, self.user_id)); msg = "Applied."
                else: cur.execute("INSERT INTO organization_members (org_id, nation_name) VALUES (?, ?)", (self.org_id, nat)); msg = "Joined."
                con.commit()
            await inter.response.send_message(msg, ephemeral=True)
        sel.callback = sub; view.add_item(sel)
        await interaction.response.send_message("Manage Membership:", view=view, ephemeral=True)

class OrgBrowserView(discord.ui.View):
    def __init__(self, user_id: int):
        super().__init__(timeout=600)
        self.user_id = user_id; self.update()
    def update(self):
        with sqlite3.connect(get_db_file()) as con:
            orgs = con.execute("SELECT org_id, name FROM organizations ORDER BY name").fetchall()
        if not orgs: return
        options = [discord.SelectOption(label=n, value=str(i)) for i, n in orgs]
        sel = discord.ui.Select(placeholder="Select an org...", options=options, custom_id="org_sel")
        sel.callback = self.select_callback
        for item in self.children:
            if getattr(item, "custom_id", None) == "org_sel": self.remove_item(item); break
        self.add_item(sel)
    async def select_callback(self, interaction: discord.Interaction):
        oid = int(interaction.data["values"][0])
        with sqlite3.connect(get_db_file()) as con:
            cur = con.cursor()
            name, owner_id, is_invite = cur.execute("SELECT name, owner_id, is_invite_only FROM organizations WHERE org_id = ?", (oid,)).fetchone()
            members = [r[0] for r in cur.execute("SELECT nation_name FROM organization_members WHERE org_id = ?", (oid,)).fetchall()]
        embed = discord.Embed(title=f"Org: {name}", color=discord.Color.blue())
        embed.add_field(name="Owner", value=f"<@{owner_id}>", inline=True)
        embed.add_field(name="Type", value="Invite-Only" if is_invite else "Open", inline=True)
        embed.add_field(name="Members", value=", ".join(members) if members else "None", inline=False)
        await interaction.response.edit_message(embed=embed, view=OrgDetailsView(oid, self.user_id))
    @discord.ui.button(label="Create New Org", style=discord.ButtonStyle.primary, row=1)
    async def create_org(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(CreateOrgModal())

class Organizations(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
    @app_commands.command(name="orgs", description="Browse and manage organizations.")
    async def orgs(self, interaction: discord.Interaction):
        view = OrgBrowserView(interaction.user.id)
        embed = discord.Embed(title="🏢 Organization Browser", description="Select an organization below.", color=discord.Color.dark_grey())
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

async def setup(bot):
    await bot.add_cog(Organizations(bot))
