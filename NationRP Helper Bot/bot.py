import discord
from discord.ext import commands
import os
from dotenv import load_dotenv
from cogs.utils import initialize_database

# --- Setup ---
load_dotenv()
TOKEN = os.getenv('DISCORD_TOKEN')

intents = discord.Intents.default()
intents.members = True
intents.message_content = True

class NationRPBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="!", intents=intents)

    async def setup_hook(self) -> None:
        # Load all cogs from the cogs directory
        for filename in os.listdir('./cogs'):
            if filename.endswith('.py') and filename != 'utils.py':
                try:
                    await self.load_extension(f'cogs.{filename[:-3]}')
                    print(f'Loaded extension: {filename}')
                except Exception as e:
                    print(f'Failed to load extension {filename}: {e}')
        
        # Sync slash commands
        await self.tree.sync()

bot = NationRPBot()

@bot.event
async def on_ready():
    print(f'{bot.user} has connected to Discord!')
    print("Bot is ready and commands are synced.")

if __name__ == "__main__":
    initialize_database()
    bot.run(TOKEN)