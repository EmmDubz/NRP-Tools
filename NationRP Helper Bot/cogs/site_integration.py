"""
site_integration.py — FNS Bot cog for FNRRP Website integration.

  • Posts pending calendar suggestions to Discord once (no role ping).
  • Accept / Deny buttons on each suggestion message.
"""

import discord
from discord.ext import tasks, commands
import sqlite3
import os
import datetime

SITE_DB_PATH = os.environ.get("SITE_DB_PATH", "/opt/FNRRPSite/data/site.db")

SUGGESTIONS_CHANNEL_ID = 808776137186082887
LOG_CHANNEL_ID = 836184138847092746
POLITBURO_ROLE_ID = 858506693038047274


def get_site_db():
    return sqlite3.connect(SITE_DB_PATH)


def _ensure_schema():
    try:
        with get_site_db() as con:
            con.execute(
                "ALTER TABLE calendar_events ADD COLUMN bot_notified INTEGER NOT NULL DEFAULT 0"
            )
            con.commit()
    except sqlite3.OperationalError:
        pass


def _fetch_pending_unnotified():
    with get_site_db() as con:
        cur = con.cursor()
        cur.execute(
            """
            SELECT id, title, description, irp_date, recurrence, suggested_by_name
            FROM calendar_events
            WHERE status = 'pending' AND bot_notified = 0
            ORDER BY created_at ASC
            """
        )
        return cur.fetchall()


def _mark_notified(event_id: int):
    with get_site_db() as con:
        con.execute(
            "UPDATE calendar_events SET bot_notified = 1 WHERE id = ?",
            (event_id,),
        )
        con.commit()


def _get_event(event_id: int):
    with get_site_db() as con:
        cur = con.cursor()
        cur.execute(
            """
            SELECT id, title, irp_date, status, suggested_by_name
            FROM calendar_events WHERE id = ?
            """,
            (event_id,),
        )
        return cur.fetchone()


def _approve_event(event_id: int, approver_id: str, approver_name: str) -> bool:
    with get_site_db() as con:
        cur = con.cursor()
        cur.execute(
            """
            UPDATE calendar_events
            SET status = 'approved',
                approved_by_discord_id = ?,
                approved_by_name = ?,
                updated_at = datetime('now')
            WHERE id = ? AND status = 'pending'
            """,
            (approver_id, approver_name, event_id),
        )
        con.commit()
        return cur.rowcount > 0


def _reject_event(event_id: int) -> bool:
    with get_site_db() as con:
        cur = con.cursor()
        cur.execute(
            """
            UPDATE calendar_events
            SET status = 'rejected', updated_at = datetime('now')
            WHERE id = ? AND status = 'pending'
            """,
            (event_id,),
        )
        con.commit()
        return cur.rowcount > 0


def _suggestion_view(event_id: int) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(
        discord.ui.Button(
            label="Accept",
            style=discord.ButtonStyle.success,
            custom_id=f"cal_accept_{event_id}",
        )
    )
    view.add_item(
        discord.ui.Button(
            label="Deny",
            style=discord.ButtonStyle.danger,
            custom_id=f"cal_deny_{event_id}",
        )
    )
    return view


class SiteIntegration(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self._notified_ids: set[int] = set()
        self.check_pending_events.start()

    def cog_unload(self):
        self.check_pending_events.cancel()

    @commands.Cog.listener()
    async def on_ready(self):
        _ensure_schema()

    @tasks.loop(minutes=2)
    async def check_pending_events(self):
        await self.bot.wait_until_ready()
        channel = self.bot.get_channel(SUGGESTIONS_CHANNEL_ID)
        if not channel:
            return

        try:
            for (
                event_id,
                title,
                description,
                irp_date,
                recurrence,
                suggested_by_name,
            ) in _fetch_pending_unnotified():
                if event_id in self._notified_ids:
                    continue

                embed = discord.Embed(
                    title="📋 New Calendar Suggestion",
                    description=f"**{title}** is awaiting Politburo approval.",
                    color=0xE8C460,
                    timestamp=datetime.datetime.now(datetime.timezone.utc),
                )
                embed.add_field(name="Date (IRP)", value=irp_date, inline=True)
                embed.add_field(
                    name="Recurrence",
                    value="Annual" if recurrence == "yearly" else "One-off",
                    inline=True,
                )
                embed.add_field(name="Suggested by", value=suggested_by_name, inline=True)
                if description:
                    embed.add_field(
                        name="Description", value=description[:300], inline=False
                    )
                embed.set_footer(
                    text="Accept or deny below · Also at fnrrp.org/admin"
                )

                try:
                    await channel.send(embed=embed, view=_suggestion_view(event_id))
                    self._notified_ids.add(event_id)
                    _mark_notified(event_id)
                except Exception as e:
                    print(f"[SiteIntegration] Failed to notify event {event_id}: {e}")

        except Exception as e:
            print(f"[SiteIntegration] check_pending_events error: {e}")

    @check_pending_events.before_loop
    async def before_check(self):
        await self.bot.wait_until_ready()

    @commands.Cog.listener()
    async def on_interaction(self, interaction: discord.Interaction):
        if interaction.type != discord.InteractionType.component:
            return
        cid = interaction.data.get("custom_id", "")
        if cid.startswith("cal_accept_"):
            action = "approve"
        elif cid.startswith("cal_deny_"):
            action = "reject"
        else:
            return

        try:
            event_id = int(cid.rsplit("_", 1)[-1])
        except ValueError:
            return

        member = interaction.user
        if not isinstance(member, discord.Member):
            await interaction.response.send_message("Guild only.", ephemeral=True)
            return

        if POLITBURO_ROLE_ID not in [r.id for r in member.roles]:
            await interaction.response.send_message(
                "Only Politburo can accept or deny calendar suggestions.",
                ephemeral=True,
            )
            return

        row = _get_event(event_id)
        if not row or row[3] != "pending":
            await interaction.response.send_message(
                "This suggestion was already handled.", ephemeral=True
            )
            return

        _id, title, irp_date, _status, suggested_by = row
        approver_name = str(member.display_name)
        approver_id = str(member.id)

        if action == "approve":
            if not _approve_event(event_id, approver_id, approver_name):
                await interaction.response.send_message("Already handled.", ephemeral=True)
                return
            log_title = "✅ Calendar Event Approved"
            log_desc = f"**{title}** has been approved."
            color = 0x57F287
            status_line = f"Accepted by {approver_name}"
        else:
            if not _reject_event(event_id):
                await interaction.response.send_message("Already handled.", ephemeral=True)
                return
            log_title = "❌ Calendar Suggestion Denied"
            log_desc = f"**{title}** was denied."
            color = 0xED4245
            status_line = f"Denied by {approver_name}"

        embed = interaction.message.embeds[0] if interaction.message and interaction.message.embeds else None
        if embed:
            embed.color = discord.Color(color)
            embed.description = f"**{title}** — {status_line}"
            await interaction.response.edit_message(embed=embed, view=None)
        else:
            await interaction.response.send_message(status_line, ephemeral=True)

        log_channel = self.bot.get_channel(LOG_CHANNEL_ID)
        if log_channel:
            log_embed = discord.Embed(
                title=log_title,
                description=log_desc,
                color=color,
                timestamp=datetime.datetime.now(datetime.timezone.utc),
            )
            log_embed.add_field(name="Date (IRP)", value=irp_date, inline=True)
            log_embed.add_field(name="Suggested by", value=suggested_by, inline=True)
            log_embed.add_field(name="Handled by", value=approver_name, inline=True)
            try:
                await log_channel.send(embed=log_embed)
            except Exception as e:
                print(f"[SiteIntegration] log send failed: {e}")


async def setup(bot):
    await bot.add_cog(SiteIntegration(bot))
