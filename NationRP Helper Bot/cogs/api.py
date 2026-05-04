import discord
from discord.ext import commands, tasks
from aiohttp import web
import sqlite3
import os
import json
from .utils import get_db_file, load_config, get_rp_time

class APICog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.app = web.Application()
        self.setup_routes()
        self.runner = None
        self.port = int(os.getenv("API_PORT", 0))
        self.host = os.getenv("API_HOST", "0.0.0.0")
        self.secret = os.getenv("API_SECRET")
        
        if self.port > 0:
            self.web_server.start()

    def setup_routes(self):
        self.app.router.add_get('/api/time', self.get_time)
        self.app.router.add_get('/api/nations', self.get_nations)
        self.app.router.add_get('/api/nations/{name}', self.get_nation_detail)
        self.app.router.add_get('/api/organizations', self.get_organizations)
        self.app.router.add_get('/api/organizations/{id}', self.get_organization_detail)
        self.app.router.add_get('/api/voting/proposals', self.get_proposals)

    def authenticate(self, request):
        if not self.secret: return True # If no secret set, allow all (not recommended)
        auth_header = request.headers.get('Authorization')
        if not auth_header or auth_header != f"Bearer {self.secret}":
            return False
        return True

    async def get_time(self, request):
        if not self.authenticate(request): return web.Response(status=401, text="Unauthorized")
        config = load_config()
        rp_time = get_rp_time()
        data = {
            "rp_time_formatted": rp_time.strftime(config.get("rp_date_format", "%d %B %Y")),
            "rp_timestamp": rp_time.timestamp(),
            "config": {
                "real_days_per_rp_year": config.get("real_days_per_rp_year"),
                "short_name": config.get("short_name")
            }
        }
        return web.json_response(data)

    async def get_nations(self, request):
        if not self.authenticate(request): return web.Response(status=401, text="Unauthorized")
        with sqlite3.connect(get_db_file()) as con:
            cur = con.cursor()
            cur.execute("SELECT nation_name, user_id, currency_symbol, currency_name FROM nations ORDER BY nation_name")
            rows = cur.fetchall()
        
        nations = [{"name": r[0], "owner_id": r[1], "currency": f"{r[2]} {r[3]}" if r[2] else "None"} for r in rows]
        return web.json_response(nations)

    async def get_nation_detail(self, request):
        if not self.authenticate(request): return web.Response(status=401, text="Unauthorized")
        name = request.match_info['name']
        with sqlite3.connect(get_db_file()) as con:
            cur = con.cursor()
            cur.execute("SELECT * FROM nations WHERE nation_name = ?", (name,))
            row = cur.fetchone()
            if not row: return web.Response(status=404, text="Nation not found")
            
            # Fetch column names
            cols = [d[0] for d in cur.description]
            data = dict(zip(cols, row))
            
            # Fetch orgs
            cur.execute("SELECT name FROM organizations o JOIN organization_members om ON o.org_id = om.org_id WHERE om.nation_name = ?", (name,))
            data["organizations"] = [r[0] for r in cur.fetchall()]
            
        return web.json_response(data)

    async def get_organizations(self, request):
        if not self.authenticate(request): return web.Response(status=401, text="Unauthorized")
        with sqlite3.connect(get_db_file()) as con:
            cur = con.cursor()
            cur.execute("SELECT org_id, name, type, is_invite_only FROM organizations ORDER BY name")
            rows = cur.fetchall()
        
        orgs = [{"id": r[0], "name": r[1], "type": r[2], "is_invite_only": bool(r[3])} for r in rows]
        return web.json_response(orgs)

    async def get_organization_detail(self, request):
        if not self.authenticate(request): return web.Response(status=401, text="Unauthorized")
        org_id = request.match_info['id']
        with sqlite3.connect(get_db_file()) as con:
            cur = con.cursor()
            cur.execute("SELECT * FROM organizations WHERE org_id = ?", (org_id,))
            row = cur.fetchone()
            if not row: return web.Response(status=404, text="Organization not found")
            
            cols = [d[0] for d in cur.description]
            data = dict(zip(cols, row))
            
            # Fetch members
            cur.execute("SELECT nation_name, rank FROM organization_members WHERE org_id = ?", (org_id,))
            data["members"] = [{"nation": r[0], "rank": r[1]} for r in cur.fetchall()]
            
        return web.json_response(data)

    async def get_proposals(self, request):
        if not self.authenticate(request): return web.Response(status=401, text="Unauthorized")
        with sqlite3.connect(get_db_file()) as con:
            cur = con.cursor()
            cur.execute("SELECT * FROM proposals ORDER BY created_at DESC")
            rows = cur.fetchall()
            cols = [d[0] for d in cur.description]
            proposals = [dict(zip(cols, r)) for r in rows]
            
        return web.json_response(proposals)

    @tasks.loop(count=1)
    async def web_server(self):
        self.runner = web.AppRunner(self.app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, self.host, self.port)
        await site.start()
        print(f"API Server started at http://{self.host}:{self.port}")

    def cog_unload(self):
        if self.web_server.is_running():
            self.web_server.cancel()
        # runner.cleanup is async, tricky in cog_unload but we'll try
        import asyncio
        if self.runner:
            asyncio.create_task(self.runner.cleanup())

async def setup(bot):
    await bot.add_cog(APICog(bot))
