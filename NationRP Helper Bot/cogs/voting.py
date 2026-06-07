import discord
from discord import app_commands
from discord.ext import tasks, commands
import datetime
import sqlite3
from enum import Enum
from typing import Optional
from .utils import load_config, get_db_file, resolution_label, PaginationView, is_admin_check, all_nations_autocomplete, user_nations_autocomplete

def format_proposer_line(proposer_name: str, proposing_country: Optional[str] = None) -> str:
    if proposing_country:
        return f"Proposed by {proposer_name} ({proposing_country})"
    return f"Proposed by {proposer_name}"

def build_proposal_embed(res_id: int, title: str, text: str, deadline_iso: str, proposer_name: str, proposing_country: Optional[str], config: dict) -> discord.Embed:
    rl = resolution_label(res_id, config)
    deadline = datetime.datetime.fromisoformat(deadline_iso)
    if deadline.tzinfo is None:
        deadline = deadline.replace(tzinfo=datetime.timezone.utc)
    deadline_ts = int(deadline.timestamp())
    embed = discord.Embed(
        title=f"Resolution {rl}: {title}",
        description=text,
        color=discord.Color.blue(),
    )
    embed.add_field(
        name="Deadline",
        value=f"Closes <t:{deadline_ts}:R> (at <t:{deadline_ts}:F>)",
        inline=False,
    )
    embed.set_footer(text=format_proposer_line(proposer_name, proposing_country))
    return embed

class VoteChoice(Enum):
    aye = "Aye"
    nay = "Nay"
    abstain = "Abstain"

class VoteView(discord.ui.View):
    def __init__(self, resolution_id: int):
        super().__init__(timeout=None)
        self.resolution_id = resolution_id

    async def cast_vote(self, interaction: discord.Interaction, choice: str):
        with sqlite3.connect(get_db_file()) as con:
            cur = con.cursor()
            cur.execute("SELECT nation_name FROM nations WHERE user_id = ?", (interaction.user.id,))
            nations = [r[0] for r in cur.fetchall()]
            if not nations:
                await interaction.response.send_message("You need a registered nation to vote!", ephemeral=True)
                return
            
            # For simplicity, we use a dropdown if they have multiple nations
            if len(nations) > 1:
                view = discord.ui.View()
                select = discord.ui.Select(placeholder="Choose nation to vote with...")
                for n in nations: select.add_option(label=n, value=n)
                async def select_callback(inter: discord.Interaction):
                    chosen = inter.data["values"][0]
                    with sqlite3.connect(get_db_file()) as c2:
                        cur2 = c2.cursor()
                        cur2.execute("INSERT OR REPLACE INTO votes (resolution_id, nation_name, vote_choice) VALUES (?, ?, ?)",
                                     (self.resolution_id, chosen, choice))
                        c2.commit()
                    await inter.response.send_message(f"✅ Voted **{choice.upper()}** as **{chosen}**.", ephemeral=True)
                select.callback = select_callback
                view.add_item(select)
                await interaction.response.send_message("Select nation:", view=view, ephemeral=True)
            else:
                cur.execute("INSERT OR REPLACE INTO votes (resolution_id, nation_name, vote_choice) VALUES (?, ?, ?)",
                             (self.resolution_id, nations[0], choice))
                con.commit()
                await interaction.response.send_message(f"✅ Voted **{choice.upper()}** as **{nations[0]}**.", ephemeral=True)

    @discord.ui.button(label="Aye", style=discord.ButtonStyle.success, custom_id="vote_aye")
    async def aye(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.cast_vote(interaction, "aye")

    @discord.ui.button(label="Nay", style=discord.ButtonStyle.danger, custom_id="vote_nay")
    async def nay(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.cast_vote(interaction, "nay")

    @discord.ui.button(label="Abstain", style=discord.ButtonStyle.secondary, custom_id="vote_abstain")
    async def abstain(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.cast_vote(interaction, "abstain")

class Voting(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.check_proposals.start()

    def cog_unload(self):
        self.check_proposals.cancel()

    @app_commands.command(name="propose", description="Submit a new resolution for voting.")
    @app_commands.describe(
        title="Short title of the resolution",
        text="Full text of the proposal",
        duration_days="How many days to vote (default 3)",
        proposing_country="Nation proposing this resolution (required if you have multiple registered nations)",
    )
    @app_commands.autocomplete(proposing_country=user_nations_autocomplete)
    async def propose(self, interaction: discord.Interaction, title: str, text: str, duration_days: int = 3, proposing_country: str = None):
        with sqlite3.connect(get_db_file()) as con:
            cur = con.cursor()
            cur.execute("SELECT nation_name FROM nations WHERE user_id = ?", (interaction.user.id,))
            nations = [r[0] for r in cur.fetchall()]
        
        if not nations:
            await interaction.response.send_message("You need to `/register` a nation before you can propose a resolution.", ephemeral=True)
            return

        if len(nations) > 1:
            if not proposing_country:
                nation_list = ", ".join(f"**{n}**" for n in nations)
                await interaction.response.send_message(
                    f"You have multiple registered nations ({nation_list}). "
                    "Please specify `proposing_country` with the nation proposing this resolution.",
                    ephemeral=True,
                )
                return
            selected_nation = next((n for n in nations if n.lower() == proposing_country.lower()), None)
            if not selected_nation:
                nation_list = ", ".join(f"**{n}**" for n in nations)
                await interaction.response.send_message(
                    f"`{proposing_country}` is not one of your registered nations ({nation_list}). Please try again.",
                    ephemeral=True,
                )
                return
            display_country = selected_nation
        else:
            selected_nation = nations[0]
            display_country = proposing_country or selected_nation

        cfg = load_config()
        comrade_role_id = cfg.get("comrade_role_id")
        if comrade_role_id:
            role = interaction.guild.get_role(comrade_role_id)
            if role and role not in interaction.user.roles:
                await interaction.response.send_message(f"❌ You need the **{role.name}** role to propose resolutions.", ephemeral=True)
                return

        proposals_channel_id = cfg.get("proposals_channel_id")
        target_channel = self.bot.get_channel(proposals_channel_id) if proposals_channel_id else interaction.channel
        if not target_channel:
            await interaction.response.send_message("The configured proposals channel could not be found. Contact an admin.", ephemeral=True)
            return

        deadline_dt = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=duration_days)
        deadline = deadline_dt.isoformat()
        with sqlite3.connect(get_db_file()) as con:
            cur = con.cursor()
            cur.execute(
                "INSERT INTO resolutions (title, text, proposer_name, proposer_nation_name, proposing_country, deadline_iso, original_channel_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (title, text, interaction.user.display_name, selected_nation, display_country, deadline, target_channel.id),
            )
            res_id = cur.lastrowid
            con.commit()

        rl = resolution_label(res_id, cfg)
        embed = build_proposal_embed(res_id, title, text, deadline, interaction.user.display_name, display_country, cfg)
        ping_role_id = cfg.get("ping_role_id")
        ping = f"<@&{ping_role_id}>" if ping_role_id else ""

        msg = await target_channel.send(content=ping, embed=embed, view=VoteView(res_id))
        with sqlite3.connect(get_db_file()) as con:
            cur = con.cursor()
            cur.execute("UPDATE resolutions SET message_id = ? WHERE resolution_id = ?", (msg.id, res_id))
            con.commit()
        await interaction.response.send_message(f"✅ **{rl}** submitted.", ephemeral=True)

    @app_commands.command(name="resolutions", description="Lists past and active resolutions.")
    async def resolutions(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        with sqlite3.connect(get_db_file()) as con:
            cur = con.cursor()
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
            desc = ""
            for rid, title, active, res_status in chunk:
                if active: icon = "🟢 **Active**"
                else:
                    final = res_status or "Concluded"
                    if "Accepted" in final: icon = "✅"
                    elif "Rejected" in final: icon = "❌"
                    elif "Repealed" in final: icon = "🗑️"
                    else: icon = "⚫"
                    icon = f"{icon} {final}"
                desc += f"`{resolution_label(rid, cfg)}`: {title} — {icon}\n"
            embed.description = desc
            embeds.append(embed)
        
        await interaction.followup.send(embed=embeds[0], view=PaginationView(embeds), ephemeral=True)

    @app_commands.command(name="lookup", description="Looks up the full details and votes for a resolution.")
    async def lookup(self, interaction: discord.Interaction, resolution_id: int):
        await interaction.response.defer(ephemeral=True)
        with sqlite3.connect(get_db_file()) as con:
            cur = con.cursor()
            cur.execute("SELECT title, text, proposer_name, proposer_nation_name, proposing_country, deadline_iso, is_active, was_force_closed FROM resolutions WHERE resolution_id = ?", (resolution_id,))
            row = cur.fetchone()
            if not row:
                await interaction.followup.send(f"Resolution ID `{resolution_id}` not found.", ephemeral=True)
                return
            title, text, proposer_name, proposer_nation, proposing_country, deadline, active, forced = row
            cur.execute("SELECT nation_name, vote_choice FROM votes WHERE resolution_id = ?", (resolution_id,))
            votes = cur.fetchall()

        status = "Active" if active else ("Force Closed" if forced else "Concluded")
        cfg = load_config()
        embed = discord.Embed(title=f"Details for {resolution_label(resolution_id, cfg)}: {title}", description=text, color=discord.Color.green() if active else discord.Color.dark_grey())
        embed.add_field(name="Status", value=status, inline=True)
        proposer_display = format_proposer_line(proposer_name, proposing_country)
        if proposer_nation:
            proposer_display += f"\nRegistered nation: {proposer_nation}"
        embed.add_field(name="Proposer", value=proposer_display, inline=True)
        
        aye = [n for n, c in votes if c == 'aye']
        nay = [n for n, c in votes if c == 'nay']
        abst = [n for n, c in votes if c == 'abstain']
        embed.add_field(name=f"Ayes ({len(aye)})", value="\n".join(f"- {n}" for n in aye) or "None", inline=False)
        embed.add_field(name=f"Nays ({len(nay)})", value="\n".join(f"- {n}" for n in nay) or "None", inline=False)
        embed.add_field(name=f"Abstains ({len(abst)})", value="\n".join(f"- {n}" for n in abst) or "None", inline=False)
        
        if active:
            view = VoteView(resolution_id)
            await interaction.followup.send(embed=embed, view=view, ephemeral=True)
        else:
            await interaction.followup.send(embed=embed, ephemeral=True)

    @app_commands.command(name="repeal", description="Mark a passed resolution as Repealed/Ended.")
    async def repeal(self, interaction: discord.Interaction, resolution_id: int, reason: str):
        is_admin = is_admin_check(interaction)
        with sqlite3.connect(get_db_file()) as con:
            cur = con.cursor()
            cur.execute("SELECT proposer_name FROM resolutions WHERE resolution_id = ?", (resolution_id,))
            row = cur.fetchone()
            if not row:
                await interaction.response.send_message("Resolution not found.", ephemeral=True)
                return
            if interaction.user.display_name != row[0] and not is_admin:
                await interaction.response.send_message("Only Admin or Proposer can repeal.", ephemeral=True)
                return
            new_status = f"Repealed ({reason})"
            cur.execute("UPDATE resolutions SET result_status = ? WHERE resolution_id = ?", (new_status, resolution_id))
            con.commit()
        await interaction.response.send_message(f"Resolution `{resolution_label(resolution_id)}` updated to: **{new_status}**.")

    @app_commands.command(name="amend", description="Modify a resolution you proposed (Wipes current votes).")
    @app_commands.describe(resolution_id="ID of resolution", new_text="The new text for the resolution.")
    async def amend(self, interaction: discord.Interaction, resolution_id: int, new_text: str):
        await interaction.response.defer(ephemeral=True)
        with sqlite3.connect(get_db_file()) as con:
            cur = con.cursor()
            cur.execute("SELECT proposer_name, is_active, title, deadline_iso, proposing_country, original_channel_id, message_id FROM resolutions WHERE resolution_id = ?", (resolution_id,))
            row = cur.fetchone()
            if not row:
                await interaction.followup.send("Resolution not found.", ephemeral=True)
                return
            proposer_name, is_active, title, deadline_iso, proposing_country, original_channel_id, message_id = row
            if proposer_name != interaction.user.display_name and not is_admin_check(interaction):
                await interaction.followup.send("Only the original proposer can amend this.", ephemeral=True)
                return
            if not is_active:
                await interaction.followup.send("This resolution is already concluded.", ephemeral=True)
                return
            cur.execute("UPDATE resolutions SET text = ? WHERE resolution_id = ?", (new_text, resolution_id))
            cur.execute("DELETE FROM votes WHERE resolution_id = ?", (resolution_id,))
            con.commit()

        cfg = load_config()
        rl = resolution_label(resolution_id, cfg)
        
        target_channel = self.bot.get_channel(original_channel_id)
        if not target_channel:
            try:
                target_channel = await self.bot.fetch_channel(original_channel_id)
            except Exception:
                target_channel = None

        msg = None
        if target_channel:
            if message_id:
                try:
                    msg = await target_channel.fetch_message(message_id)
                except Exception:
                    msg = None
            
            if not msg:
                # Scan channel history to find the proposal message and edit it (working backwards to past posts)
                try:
                    async for m in target_channel.history(limit=100):
                        if m.author == self.bot.user and m.embeds:
                            first_embed = m.embeds[0]
                            if first_embed.title and f"Resolution {rl}:" in first_embed.title:
                                msg = m
                                with sqlite3.connect(get_db_file()) as con:
                                    cur = con.cursor()
                                    cur.execute("UPDATE resolutions SET message_id = ? WHERE resolution_id = ?", (msg.id, resolution_id))
                                    con.commit()
                                break
                except Exception as e:
                    print(f"Error scanning history for RES-{resolution_id:03d}: {e}")

        if msg:
            try:
                new_embed = build_proposal_embed(resolution_id, title, new_text, deadline_iso, proposer_name, proposing_country, cfg)
                await msg.edit(embed=new_embed, view=VoteView(resolution_id))
            except Exception as e:
                print(f"Failed to edit original message for RES-{resolution_id:03d}: {e}")

        await interaction.followup.send(f"✅ Resolution `{rl}` has been amended and votes have been reset.", ephemeral=True)

    @app_commands.command(name="votingrecord", description="See the full voting history for a nation.")
    @app_commands.autocomplete(nation=all_nations_autocomplete)
    async def voting_record(self, interaction: discord.Interaction, nation: str):
        with sqlite3.connect(get_db_file()) as con:
            cur = con.cursor()
            cur.execute("SELECT r.resolution_id, r.title, v.vote_choice FROM resolutions r JOIN votes v ON r.resolution_id = v.resolution_id WHERE v.nation_name = ? ORDER BY r.resolution_id DESC", (nation,))
            rows = cur.fetchall()
        
        if not rows:
            await interaction.response.send_message(f"No voting records found for **{nation}**.", ephemeral=True)
            return

        cfg = load_config()
        items = 15
        embeds = []
        for i in range(0, len(rows), items):
            embed = discord.Embed(title=f"Voting Record: {nation} (Page {len(embeds)+1})", color=discord.Color.blue())
            embed.description = "\n".join(f"`{resolution_label(rid, cfg)}`: {title} — **{choice.upper()}**" for rid, title, choice in rows[i:i+items])
            embeds.append(embed)
        await interaction.response.send_message(embed=embeds[0], view=PaginationView(embeds), ephemeral=True)

    @tasks.loop(minutes=1)
    async def check_proposals(self):
        await self.bot.wait_until_ready()
        try:
            config = load_config()
            results_channel_id = config.get("results_channel_id")
            ping_role_id = config.get("ping_role_id")
            with sqlite3.connect(get_db_file()) as con:
                cur = con.cursor()
                now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
                cur.execute("SELECT resolution_id, title, original_channel_id, was_force_closed FROM resolutions WHERE is_active = 1 AND deadline_iso < ?", (now_iso,))
                concluded = cur.fetchall()
                for res_id, title, chan_id, forced in concluded:
                    if forced:
                        cur.execute("UPDATE resolutions SET is_active = 0, result_status = 'Force Closed' WHERE resolution_id = ?", (res_id,))
                        con.commit()
                        continue
                    target_id = results_channel_id or chan_id
                    channel = self.bot.get_channel(target_id)
                    cur.execute("SELECT nation_name, vote_choice FROM votes WHERE resolution_id = ?", (res_id,))
                    votes = cur.fetchall()
                    aye = [n for n, c in votes if c == 'aye']
                    nay = [n for n, c in votes if c == 'nay']
                    abstain = [n for n, c in votes if c == 'abstain']
                    res_str = "Accepted" if len(aye) > len(nay) else "Rejected"
                    rl = resolution_label(res_id, config)
                    embed = discord.Embed(title=f"Vote Concluded on {rl}: {res_str}", description=f"**{title}**", color=discord.Color.green() if res_str == "Accepted" else discord.Color.red())
                    embed.add_field(name=f"Ayes ({len(aye)})", value="\n".join(aye) or "None", inline=True)
                    embed.add_field(name=f"Nays ({len(nay)})", value="\n".join(nay) or "None", inline=True)
                    embed.add_field(name=f"Abstains ({len(abstain)})", value="\n".join(abstain) or "None", inline=True)
                    if channel:
                        ping = f"<@&{ping_role_id}>" if ping_role_id else ""
                        await channel.send(content=ping, embed=embed)
                    cur.execute("UPDATE resolutions SET is_active = 0, result_status = ? WHERE resolution_id = ?", (res_str, res_id))
                    con.commit()
        except Exception as e:
            print(f"Error in check_proposals: {e}")

async def setup(bot):
    await bot.add_cog(Voting(bot))
