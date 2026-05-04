import discord
from discord import app_commands
from discord.ext import commands
import sqlite3
from .utils import get_db_file, load_config, branding_from_config, PaginationView, all_nations_autocomplete

class BioModal(discord.ui.Modal, title="Update Nation Bio"):
    bio_input = discord.ui.TextInput(label="Nation Biography", style=discord.TextStyle.paragraph, placeholder="Describe your nation...", required=True, max_length=1000)
    def __init__(self, nation_name, current_bio):
        super().__init__()
        self.nation_name = nation_name
        if current_bio: self.bio_input.default = current_bio
    async def on_submit(self, interaction: discord.Interaction):
        with sqlite3.connect(get_db_file()) as con:
            con.execute("UPDATE nations SET bio = ? WHERE nation_name = ? COLLATE NOCASE", (self.bio_input.value, self.nation_name))
            con.commit()
        await interaction.response.send_message(f"✅ Bio updated for **{self.nation_name}**.", ephemeral=True)

class WikiModal(discord.ui.Modal, title="Update Wiki Link"):
    wiki_input = discord.ui.TextInput(label="Wiki URL", placeholder="https://wiki.example.com/MyNation", required=True, max_length=255)
    def __init__(self, nation_name, current_wiki):
        super().__init__()
        self.nation_name = nation_name
        if current_wiki: self.wiki_input.default = current_wiki
    async def on_submit(self, interaction: discord.Interaction):
        with sqlite3.connect(get_db_file()) as con:
            con.execute("UPDATE nations SET wiki_link = ? WHERE nation_name = ? COLLATE NOCASE", (self.wiki_input.value, self.nation_name))
            con.commit()
        await interaction.response.send_message(f"✅ Wiki link updated for **{self.nation_name}**.", ephemeral=True)

class CurrencyModal(discord.ui.Modal, title="Basic Currency Details"):
    name_input = discord.ui.TextInput(label="Currency Name", placeholder="e.g. Credits, Dollars", required=True)
    symbol_input = discord.ui.TextInput(label="Currency Symbol", placeholder="e.g. ¤, $", required=True, max_length=5)
    def __init__(self, nation_name, cur_name, cur_sym):
        super().__init__()
        self.nation_name = nation_name
        if cur_name: self.name_input.default = cur_name
        if cur_sym: self.symbol_input.default = cur_sym
    async def on_submit(self, interaction: discord.Interaction):
        with sqlite3.connect(get_db_file()) as con:
            con.execute("UPDATE nations SET currency_name = ?, currency_symbol = ? WHERE nation_name = ? COLLATE NOCASE", (self.name_input.value, self.symbol_input.value, self.nation_name))
            con.commit()
        await interaction.response.send_message(f"✅ Currency basic details updated for **{self.nation_name}**.", ephemeral=True)

class PegRateModal(discord.ui.Modal):
    rate_input = discord.ui.TextInput(label="Exchange Rate", placeholder="e.g. 1.0, 0.5, 12.0", required=True, default="1.0")
    def __init__(self, nation_name, source_currency_name, target_name):
        super().__init__(title=f"Rate: 1 {source_currency_name} = X {target_name}")
        self.nation_name = nation_name
        self.target_name = target_name
    async def on_submit(self, interaction: discord.Interaction):
        try:
            rate = float(self.rate_input.value)
        except ValueError:
            await interaction.response.send_message("❌ Invalid rate. Use a number.", ephemeral=True)
            return
        with sqlite3.connect(get_db_file()) as con:
            con.execute("UPDATE nations SET currency_peg_target = ?, currency_peg_rate = ? WHERE nation_name = ? COLLATE NOCASE", (self.target_name, rate, self.nation_name))
            con.commit()
        await interaction.response.send_message(f"✅ **{self.nation_name}** is now pegged to **{self.target_name}** at a rate of **{rate}**.", ephemeral=True)

class NationManagerView(discord.ui.View):
    def __init__(self, user_id: int, initial_nation: str = None):
        super().__init__(timeout=600)
        self.user_id = user_id
        self.selected_nation = initial_nation
        self.update_ui()

    def update_ui(self):
        self.clear_items()
        with sqlite3.connect(get_db_file()) as con:
            cur = con.cursor()
            cur.execute("SELECT nation_name FROM nations WHERE user_id = ?", (self.user_id,))
            nations = [r[0] for r in cur.fetchall()]
        
        if not nations: return

        if len(nations) > 1:
            options = [discord.SelectOption(label=n, value=n, default=(n == self.selected_nation)) for n in nations]
            select = discord.ui.Select(placeholder="Select a nation to manage...", options=options, custom_id="nat_select")
            select.callback = self.select_callback
            self.add_item(select)
        elif not self.selected_nation:
            self.selected_nation = nations[0]

        if self.selected_nation:
            with sqlite3.connect(get_db_file()) as con:
                row = con.execute("SELECT currency_name FROM nations WHERE nation_name = ? COLLATE NOCASE", (self.selected_nation,)).fetchone()
            has_currency = row and row[0]

            btn_bio = discord.ui.Button(label="Edit Bio", style=discord.ButtonStyle.secondary, row=1)
            btn_bio.callback = self.bio_callback
            btn_wiki = discord.ui.Button(label="Edit Wiki", style=discord.ButtonStyle.secondary, row=1)
            btn_wiki.callback = self.wiki_callback
            btn_curr = discord.ui.Button(label="Edit Currency", style=discord.ButtonStyle.secondary, row=1)
            btn_curr.callback = self.currency_callback
            self.add_item(btn_bio); self.add_item(btn_wiki); self.add_item(btn_curr)

            if has_currency:
                btn_peg = discord.ui.Button(label="Set Peg", style=discord.ButtonStyle.primary, row=1)
                btn_peg.callback = self.peg_callback
                self.add_item(btn_peg)

    async def select_callback(self, interaction: discord.Interaction):
        self.selected_nation = interaction.data["values"][0]
        self.update_ui()
        embed = discord.Embed(title=f"Managing: {self.selected_nation}", color=discord.Color.blue())
        await interaction.response.edit_message(embed=embed, view=self)

    async def bio_callback(self, interaction: discord.Interaction):
        with sqlite3.connect(get_db_file()) as con:
            bio = con.execute("SELECT bio FROM nations WHERE nation_name = ? COLLATE NOCASE", (self.selected_nation,)).fetchone()[0]
        await interaction.response.send_modal(BioModal(self.selected_nation, bio))

    async def wiki_callback(self, interaction: discord.Interaction):
        with sqlite3.connect(get_db_file()) as con:
            wiki = con.execute("SELECT wiki_link FROM nations WHERE nation_name = ? COLLATE NOCASE", (self.selected_nation,)).fetchone()[0]
        await interaction.response.send_modal(WikiModal(self.selected_nation, wiki))

    async def currency_callback(self, interaction: discord.Interaction):
        with sqlite3.connect(get_db_file()) as con:
            row = con.execute("SELECT currency_name, currency_symbol FROM nations WHERE nation_name = ? COLLATE NOCASE", (self.selected_nation,)).fetchone()
        await interaction.response.send_modal(CurrencyModal(self.selected_nation, row[0], row[1]))

    async def peg_callback(self, interaction: discord.Interaction):
        with sqlite3.connect(get_db_file()) as con:
            row = con.execute("SELECT currency_name FROM nations WHERE nation_name = ? COLLATE NOCASE", (self.selected_nation,)).fetchone()
            source_cur_name = row[0] if row else self.selected_nation
            targets = con.execute("SELECT currency_symbol, currency_name, nation_name FROM nations WHERE currency_name IS NOT NULL AND nation_name != ? COLLATE NOCASE ORDER BY currency_symbol", (self.selected_nation,)).fetchall()
        
        options = [discord.SelectOption(label="USD (Gold Standard)", value="USD")]
        for sym, name, nat in targets[:24]:
            label = f"{sym} - {name} ({nat})"
            options.append(discord.SelectOption(label=label[:100], value=nat))
        
        view = discord.ui.View()
        sel = discord.ui.Select(placeholder="Peg against...", options=options)
        async def sub(inter: discord.Interaction):
            target = inter.data["values"][0]
            await inter.response.send_modal(PegRateModal(self.selected_nation, source_cur_name, target))
        sel.callback = sub; view.add_item(sel)
        await interaction.response.send_message(f"Choose a target for **{self.selected_nation}** to peg against:", view=view, ephemeral=True)

def get_usd_value(nation_name, visited=None):
    if visited is None: visited = set()
    if nation_name == "USD": return 1.0
    if nation_name in visited: return 1.0
    visited.add(nation_name)
    
    with sqlite3.connect(get_db_file()) as con:
        row = con.execute("SELECT currency_peg_target, currency_peg_rate FROM nations WHERE nation_name = ? COLLATE NOCASE", (nation_name,)).fetchone()
    
    if not row or not row[0] or row[1] is None: return 1.0
    target, rate = row
    return rate * get_usd_value(target, visited)

class Nations(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    async def currency_autocomplete(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        choices = []
        usd_label = "USD - United States Dollar (International)"
        if current.lower() in usd_label.lower():
            choices.append(app_commands.Choice(name=usd_label, value="USD"))
        
        try:
            with sqlite3.connect(get_db_file()) as con:
                rows = con.execute("SELECT currency_symbol, currency_name, nation_name FROM nations WHERE currency_name IS NOT NULL AND currency_symbol IS NOT NULL ORDER BY currency_symbol").fetchall()
            for sym, name, nat in rows:
                label = f"{sym} - {name} ({nat})"
                if current.lower() in label.lower():
                    choices.append(app_commands.Choice(name=label, value=nat))
        except: pass
        return choices[:25]

    @app_commands.command(name="register", description="Register your nation's name.")
    async def register(self, interaction: discord.Interaction, name: str):
        with sqlite3.connect(get_db_file()) as con:
            cur = con.cursor()
            cur.execute("SELECT 1 FROM nations WHERE nation_name = ? COLLATE NOCASE", (name,))
            if cur.fetchone():
                await interaction.response.send_message(f"❌ '{name}' is already taken.", ephemeral=True)
                return
            cur.execute("INSERT INTO nations (user_id, nation_name) VALUES (?, ?)", (interaction.user.id, name))
            con.commit()
        await interaction.response.send_message(f"✅ Nation '{name}' registered!", ephemeral=True)

    @app_commands.command(name="mynations", description="Manage your registered nations.")
    async def my_nations(self, interaction: discord.Interaction):
        with sqlite3.connect(get_db_file()) as con:
            cur = con.cursor()
            cur.execute("SELECT nation_name FROM nations WHERE user_id = ?", (interaction.user.id,))
            nations = [r[0] for r in cur.fetchall()]
        
        if not nations:
            await interaction.response.send_message("You have no nations registered.", ephemeral=True)
            return
        
        initial = nations[0] if len(nations) == 1 else None
        view = NationManagerView(interaction.user.id, initial)
        title = f"Managing: {initial}" if initial else "### 🌍 Nation Management Hub"
        embed = discord.Embed(title=title, color=discord.Color.blue())
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

    @app_commands.command(name="convert", description="Convert currency between two nations.")
    @app_commands.describe(amount="Amount to convert", from_currency="Source currency", to_currency="Target currency")
    @app_commands.autocomplete(from_currency=currency_autocomplete, to_currency=currency_autocomplete)
    async def convert(self, interaction: discord.Interaction, amount: float, from_currency: str, to_currency: str):
        with sqlite3.connect(get_db_file()) as con:
            cur = con.cursor()
            if from_currency == "USD": f_row = ("United States Dollar", "$")
            else: f_row = cur.execute("SELECT currency_name, currency_symbol FROM nations WHERE nation_name = ? COLLATE NOCASE", (from_currency,)).fetchone()
            
            if to_currency == "USD": t_row = ("United States Dollar", "$")
            else: t_row = cur.execute("SELECT currency_name, currency_symbol FROM nations WHERE nation_name = ? COLLATE NOCASE", (to_currency,)).fetchone()
        
        if not f_row or not t_row:
            await interaction.response.send_message("❌ One or both currencies not found.", ephemeral=True)
            return
        
        f_val = get_usd_value(from_currency)
        t_val = get_usd_value(to_currency)
        result = (amount * f_val) / t_val
        
        embed = discord.Embed(title="💱 Currency Conversion", color=discord.Color.green())
        embed.add_field(name=f"From: {from_currency}", value=f"{f_row[1]}{amount:,.2f} {f_row[0]}", inline=True)
        embed.add_field(name=f"To: {to_currency}", value=f"{t_row[1]}{result:,.2f} {t_row[0]}", inline=True)
        embed.set_footer(text=f"USD Baselines: 1 {f_row[1]} = {f_val:,.4f} | 1 {t_row[1]} = {t_val:,.4f}")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="overview", description="Full snapshot of a nation's data.")
    @app_commands.autocomplete(nation=all_nations_autocomplete)
    async def overview(self, interaction: discord.Interaction, nation: str):
        with sqlite3.connect(get_db_file()) as con:
            cur = con.cursor()
            cur.execute("SELECT user_id, bio, currency_name, currency_symbol, wiki_link, currency_peg_target, currency_peg_rate FROM nations WHERE nation_name = ? COLLATE NOCASE", (nation,))
            row = cur.fetchone()
            if not row:
                await interaction.response.send_message("Nation not found.", ephemeral=True)
                return
            uid, bio, cname, csym, wiki, ptarg, prate = row
            cur.execute("SELECT name FROM organizations o JOIN organization_members om ON o.org_id = om.org_id WHERE om.nation_name = ? COLLATE NOCASE", (nation,))
            orgs = [r[0] for r in cur.fetchall()]
        
        embed = discord.Embed(title=f"Nation Overview: {nation}", color=discord.Color.gold())
        embed.add_field(name="Owner", value=f"<@{uid}>", inline=True)
        
        curr_val = f"{csym} {cname}" if cname else "Not Set"
        if ptarg and prate:
            t_sym = "USD" if ptarg == "USD" else ""
            if not t_sym:
                with sqlite3.connect(get_db_file()) as con:
                    t_row = con.execute("SELECT currency_symbol FROM nations WHERE nation_name = ? COLLATE NOCASE", (ptarg,)).fetchone()
                    t_sym = t_row[0] if t_row else ptarg
            curr_val += f"\n(Peg: 1 {csym} = {prate} {t_sym})"
        embed.add_field(name="Currency", value=curr_val, inline=True)
        
        if wiki: embed.add_field(name="Wiki", value=f"[Link]({wiki})", inline=True)
        embed.add_field(name="Organizations", value=", ".join(orgs) if orgs else "None", inline=False)
        embed.add_field(name="Biography", value=bio or "No bio set.", inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="nations", description="Lists all registered nations.")
    async def nations_list(self, interaction: discord.Interaction):
        with sqlite3.connect(get_db_file()) as con:
            nations = con.execute("SELECT nation_name, user_id FROM nations ORDER BY nation_name").fetchall()
        if not nations:
            await interaction.response.send_message("No nations registered.", ephemeral=True)
            return
        items_per_page = 20
        embeds = []
        for i in range(0, len(nations), items_per_page):
            chunk = nations[i:i+items_per_page]
            embed = discord.Embed(title=f"Nations (Page {len(embeds)+1})", color=discord.Color.blue())
            embed.description = "\n".join(f"**{n}** (<@{u}>)" for n, u in chunk)
            embeds.append(embed)
        await interaction.response.send_message(embed=embeds[0], view=PaginationView(embeds), ephemeral=True)

async def setup(bot):
    await bot.add_cog(Nations(bot))
