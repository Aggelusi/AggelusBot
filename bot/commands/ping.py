from __future__ import annotations

from discord.ext import commands

from bot.database import db


class PingCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @commands.command(name="ping")
    async def ping(self, ctx: commands.Context) -> None:
        """Simple health-check command.

        We include DB time in the response so one command confirms both
        Discord connectivity and database availability.
        """
        db_time = await db.get_database_time()
        latency_ms = round(self.bot.latency * 1000)
        await ctx.send(f"Pong! Latency: {latency_ms}ms | DB time: {db_time}")


async def setup(bot: commands.Bot) -> None:
    # discord.py uses this entrypoint when loading an extension module.
    await bot.add_cog(PingCog(bot))
