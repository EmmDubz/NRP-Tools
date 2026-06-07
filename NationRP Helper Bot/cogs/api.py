import discord
from discord.ext import commands, tasks
from aiohttp import web
import sqlite3
import os
import datetime
from .utils import get_db_file, load_config, get_rp_time

GUILD_ID = int(os.getenv("DISCORD_GUILD_ID", "809068023046299668"))


class APICog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.app = web.Application()
        self.setup_routes()
        self.runner = None
        self.port = int(os.getenv("API_PORT", 0))
        self.host = os.getenv("API_HOST", "0.0.0.0")
        self.secret = os.getenv("API_SECRET")

    def setup_routes(self):
        self.app.router.add_get("/api/time", self.get_time)
        self.app.router.add_get("/api/nations", self.get_nations)
        self.app.router.add_get("/api/nations/{name}", self.get_nation_detail)
        self.app.router.add_get("/api/organizations", self.get_organizations)
        self.app.router.add_get("/api/organizations/{id}", self.get_organization_detail)
        self.app.router.add_get("/api/voting/proposals", self.get_proposals)
        self.app.router.add_get("/api/convert", self.convert_currency)
        # FNRRP Website integration endpoints
        self.app.router.add_get("/api/guilds", self.get_guilds)
        self.app.router.add_get("/api/member/{user_id}/roles", self.get_member_roles)
        self.app.router.add_post("/api/send-message", self.send_message)
        self.app.router.add_get("/api/channel/{channel_id}/latest-image", self.get_latest_image)
        self.app.router.add_get("/api/channel/{channel_id}/recent-messages", self.get_recent_messages)

    def authenticate(self, request):
        if not self.secret:
            return True
        auth_header = request.headers.get("Authorization")
        return auth_header == f"Bearer {self.secret}"

    async def cog_load(self):
        if self.port > 0:
            self.web_server.start()

    async def cog_unload(self):
        self.web_server.cancel()
        if self.runner:
            await self.runner.cleanup()

    @tasks.loop(count=1)
    async def web_server(self):
        self.runner = web.AppRunner(self.app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, self.host, self.port)
        await site.start()
        print(f"[API] Web server started on {self.host}:{self.port}", flush=True)

    # --- Debug: list guilds ---------------------------------------------------
    async def get_guilds(self, request):
        if not self.authenticate(request):
            return web.json_response({"error": "Unauthorized"}, status=401)
        guilds = [{"id": str(g.id), "name": g.name, "members": g.member_count} for g in self.bot.guilds]
        return web.json_response({"guilds": guilds})

    # --- Member role lookup ---------------------------------------------------
    async def get_member_roles(self, request):
        if not self.authenticate(request):
            return web.json_response({"error": "Unauthorized"}, status=401)
        try:
            user_id = int(request.match_info["user_id"])
        except ValueError:
            return web.json_response({"roles": []})
        guild = self.bot.get_guild(GUILD_ID)
        if not guild:
            print(f"[API] Guild {GUILD_ID} not found", flush=True)
            return web.json_response({"roles": [], "error": "guild_not_found"})
        member = guild.get_member(user_id)
        if not member:
            try:
                member = await guild.fetch_member(user_id)
            except Exception as e:
                print(f"[API] fetch_member({user_id}) error: {e}", flush=True)
                return web.json_response({"roles": [], "error": str(e)})
        if not member:
            print(f"[API] Member {user_id} not found after fetch", flush=True)
            return web.json_response({"roles": [], "error": "member_not_found"})
        role_ids = [str(r.id) for r in member.roles]
        print(f"[API] Member {user_id} roles: {role_ids}", flush=True)
        return web.json_response({"roles": role_ids})

    # --- Send Discord message/embed ------------------------------------------
    async def send_message(self, request):
        if not self.authenticate(request):
            return web.json_response({"error": "Unauthorized"}, status=401)
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "Invalid JSON"}, status=400)
        channel_id = int(body.get("channel_id", 0))
        content = body.get("content") or None
        embed_data = body.get("embed")
        channel = self.bot.get_channel(channel_id)
        if not channel:
            return web.json_response({"error": "Channel not found"}, status=404)
        embed = None
        if embed_data:
            embed = discord.Embed(
                title=embed_data.get("title"),
                description=embed_data.get("description"),
                color=embed_data.get("color", 0),
            )
            for field in embed_data.get("fields", []):
                embed.add_field(name=field["name"], value=field["value"], inline=field.get("inline", False))
            if embed_data.get("footer"):
                embed.set_footer(text=embed_data["footer"]["text"])
            if embed_data.get("timestamp"):
                try:
                    embed.timestamp = datetime.datetime.fromisoformat(
                        embed_data["timestamp"].replace("Z", "+00:00"))
                except Exception:
                    pass
        try:
            msg = await channel.send(content=content, embed=embed)
            return web.json_response({"message_id": str(msg.id), "ok": True})
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)

    # --- Latest image in a channel -------------------------------------------
    async def get_latest_image(self, request):
        if not self.authenticate(request):
            return web.json_response({"error": "Unauthorized"}, status=401)
        try:
            channel_id = int(request.match_info["channel_id"])
        except ValueError:
            return web.json_response({"url": None, "content": None})
        channel = self.bot.get_channel(channel_id)
        if not channel:
            return web.json_response({"url": None, "content": None})
        try:
            async for msg in channel.history(limit=50):
                for att in msg.attachments:
                    is_img = (
                        (att.content_type and att.content_type.startswith("image/"))
                        or att.filename.lower().endswith((".png", ".jpg", ".jpeg", ".gif", ".webp"))
                    )
                    if is_img:
                        best_url = att.proxy_url or att.url
                        return web.json_response({"url": best_url, "content": msg.content or None})
        except Exception as e:
            print(f"[API] get_latest_image error: {e}")
        return web.json_response({"url": None, "content": None})

    # --- Recent messages for news feed (backfill + incremental) --------------
    async def get_recent_messages(self, request):
        if not self.authenticate(request):
            return web.json_response({"error": "Unauthorized"}, status=401)
        channel_id = request.match_info["channel_id"]
        limit = int(request.rel_url.query.get("limit", "40"))
        limit = max(1, min(limit, 80))
        before_id = request.rel_url.query.get("before", None)
        after_id  = request.rel_url.query.get("after",  None)
        channel = self.bot.get_channel(int(channel_id))
        if channel is None:
            return web.json_response({"error": "channel not found"}, status=404)
        try:
            kwargs = {"limit": limit}
            if before_id:
                kwargs["before"] = discord.Object(id=int(before_id))
            if after_id:
                kwargs["after"] = discord.Object(id=int(after_id))
            messages = []
            async for msg in channel.history(**kwargs):
                messages.append({
                    "id": str(msg.id),
                    "author_name": msg.author.display_name,
                    "author_id": str(msg.author.id),
                    "author_avatar": str(msg.author.display_avatar.url) if msg.author.display_avatar else None,
                    "is_webhook": msg.webhook_id is not None,
                    "webhook_id": str(msg.webhook_id) if msg.webhook_id else None,
                    "content": msg.content,
                    "timestamp": int(msg.created_at.timestamp()),
                })
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)
        return web.json_response({"messages": messages})

    # --- Original bot data endpoints -----------------------------------------
    async def get_time(self, request):
        cfg = load_config()
        rp = get_rp_time()
        return web.json_response({
            "rp_anchor_real_ms": cfg.get("rp_anchor_real_ms"),
            "rp_anchor_game_ms": cfg.get("rp_anchor_game_ms"),
            "real_days_per_rp_year": cfg.get("real_days_per_rp_year"),
            "short_name": cfg.get("short_name", ""),
            "rp_now": rp.isoformat() if rp else None,
        })

    async def get_nations(self, request):
        db_file = get_db_file()
        try:
            conn = sqlite3.connect(db_file)
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()
            cur.execute("SELECT * FROM nations ORDER BY nation_name ASC")
            rows = [dict(r) for r in cur.fetchall()]
            conn.close()
            for r in rows:
                if "user_id" in r and r["user_id"] is not None:
                    r["user_id"] = str(r["user_id"])
            return web.json_response({"nations": rows})
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)

    async def get_nation_detail(self, request):
        name = request.match_info["name"]
        db_file = get_db_file()
        try:
            conn = sqlite3.connect(db_file)
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()
            cur.execute("SELECT * FROM nations WHERE nation_name = ? COLLATE NOCASE", (name,))
            row = cur.fetchone()
            conn.close()
            if not row:
                return web.json_response({"error": "not found"}, status=404)
            r = dict(row)
            if "user_id" in r and r["user_id"] is not None:
                r["user_id"] = str(r["user_id"])
            return web.json_response(r)
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)

    async def get_organizations(self, request):
        db_file = get_db_file()
        try:
            conn = sqlite3.connect(db_file)
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()
            cur.execute("SELECT * FROM organizations ORDER BY name ASC")
            rows = [dict(r) for r in cur.fetchall()]
            conn.close()
            return web.json_response({"organizations": rows})
        except Exception as e:
            return web.json_response({"organizations": []})

    async def get_organization_detail(self, request):
        org_id = request.match_info["id"]
        db_file = get_db_file()
        try:
            conn = sqlite3.connect(db_file)
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()
            cur.execute("SELECT * FROM organizations WHERE id = ?", (org_id,))
            row = cur.fetchone()
            conn.close()
            if not row:
                return web.json_response({"error": "not found"}, status=404)
            return web.json_response(dict(row))
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)

    async def get_proposals(self, request):
        db_file = get_db_file()
        try:
            conn = sqlite3.connect(db_file)
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()
            cur.execute("SELECT * FROM proposals ORDER BY created_at DESC")
            rows = [dict(r) for r in cur.fetchall()]
            conn.close()
            for r in rows:
                if "proposer_id" in r and r["proposer_id"] is not None:
                    r["proposer_id"] = str(r["proposer_id"])
            return web.json_response({"proposals": rows})
        except Exception as e:
            return web.json_response({"proposals": []})

    async def convert_currency(self, request):
        from_curr = request.rel_url.query.get("from", "")
        to_curr   = request.rel_url.query.get("to", "")
        amount    = float(request.rel_url.query.get("amount", "1"))
        db_file = get_db_file()
        try:
            conn = sqlite3.connect(db_file)
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()
            cur.execute(
                "SELECT * FROM nations WHERE currency_symbol = ? OR currency_name = ? COLLATE NOCASE LIMIT 1",
                (from_curr, from_curr))
            src = cur.fetchone()
            cur.execute(
                "SELECT * FROM nations WHERE currency_symbol = ? OR currency_name = ? COLLATE NOCASE LIMIT 1",
                (to_curr, to_curr))
            dst = cur.fetchone()
            conn.close()
            if not src or not dst:
                return web.json_response({"error": "currency not found"}, status=404)
            src_rate = float(src["currency_peg_rate"] or 1)
            dst_rate = float(dst["currency_peg_rate"] or 1)
            result = amount * (src_rate / dst_rate)
            return web.json_response({"result": result, "from": from_curr, "to": to_curr, "amount": amount})
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)


async def setup(bot):
    await bot.add_cog(APICog(bot))
