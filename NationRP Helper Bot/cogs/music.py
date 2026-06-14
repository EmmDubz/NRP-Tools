import discord
from discord.ext import commands
import os
import random
import aiohttp

class Music(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.username = os.getenv("DISCOGS_USERNAME")
        self.token = os.getenv("DISCOGS_TOKEN")

    @commands.command(name="randomalbum", aliases=["random", "album"])
    async def random_album(self, ctx):
        if not self.username or not self.token:
            await ctx.send("❌ Discogs credentials are not configured in the bot's environment variables.")
            return

        headers = {
            "Authorization": f"Discogs token={self.token}",
            "User-Agent": "FNSMusicBot/1.0"
        }

        # 1. Fetch the total count of releases (per_page=1 to keep it lightweight)
        url = f"https://api.discogs.com/users/{self.username}/collection/folders/0/releases"
        async with aiohttp.ClientSession() as session:
            try:
                async with session.get(url, headers=headers, params={"page": 1, "per_page": 1}) as response:
                    if response.status != 200:
                        await ctx.send(f"❌ Failed to fetch collection from Discogs API (Status: {response.status}).")
                        return
                    data = await response.json()
                    
                    pagination = data.get("pagination", {})
                    total_items = pagination.get("items", 0)
                    if total_items == 0:
                        await ctx.send("❌ Your Discogs collection is empty!")
                        return

                    # 2. Pick a random release page
                    random_index = random.randint(1, total_items)
                    
                    # 3. Retrieve the specific release
                    async with session.get(url, headers=headers, params={"page": random_index, "per_page": 1}) as resp2:
                        if resp2.status != 200:
                            await ctx.send(f"❌ Failed to fetch random album (Status: {resp2.status}).")
                            return
                        data2 = await resp2.json()
                        releases = data2.get("releases", [])
                        if not releases:
                            await ctx.send("❌ Could not pick a random release.")
                            return
                        
                        release = releases[0]
                        basic_info = release.get("basic_information", {})
                        
                        # Extract metadata
                        release_id = basic_info.get("id")
                        title = basic_info.get("title", "Unknown Title")
                        artists_list = basic_info.get("artists", [])
                        artist = ", ".join(art.get("name", "Unknown Artist") for art in artists_list) if artists_list else "Unknown Artist"
                        year = basic_info.get("year", "Unknown")
                        genres = ", ".join(basic_info.get("genres", [])) or "N/A"
                        styles = ", ".join(basic_info.get("styles", [])) or "N/A"
                        
                        # Labels
                        labels_list = basic_info.get("labels", [])
                        label = labels_list[0].get("name", "Unknown Label") if labels_list else "Unknown Label"
                        catno = labels_list[0].get("catno", "") if labels_list else ""
                        
                        # Formats
                        formats_list = basic_info.get("formats", [])
                        format_str = "Unknown Format"
                        if formats_list:
                            fmt = formats_list[0]
                            fmt_name = fmt.get("name", "")
                            descriptions = fmt.get("descriptions", [])
                            if descriptions:
                                format_str = f"{fmt_name} ({', '.join(descriptions)})"
                            else:
                                format_str = fmt_name
                                
                        cover_url = basic_info.get("cover_image") or basic_info.get("thumb")
                        discogs_link = f"https://www.discogs.com/release/{release_id}"
                        
                        # Format Embed
                        embed = discord.Embed(
                            title=f"💿 Random Album from {self.username}'s Collection",
                            url=discogs_link,
                            color=discord.Color.blurple()
                        )
                        embed.add_field(name="Title", value=f"**{title}**", inline=True)
                        embed.add_field(name="Artist", value=f"**{artist}**", inline=True)
                        embed.add_field(name="Released", value=str(year), inline=True)
                        embed.add_field(name="Label", value=f"{label} ({catno})" if catno else label, inline=True)
                        embed.add_field(name="Format", value=format_str, inline=True)
                        embed.add_field(name="Genre / Style", value=f"{genres} / {styles}" if styles != "N/A" else genres, inline=False)
                        
                        if cover_url:
                            embed.set_image(url=cover_url)
                            
                        embed.set_footer(text=f"Discogs Release #{release_id} • Pick {random_index} of {total_items}")
                        await ctx.send(embed=embed)
                        
            except Exception as e:
                await ctx.send(f"⚠️ An error occurred while communicating with Discogs: {e}")

async def setup(bot):
    await bot.add_cog(Music(bot))
