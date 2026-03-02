from __future__ import annotations

import discord
from discord.ext import commands

from bot.database import db


class PingCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    async def cog_check(self, ctx: commands.Context) -> bool:
        if ctx.guild is None:
            raise commands.CheckFailure("Use this command in a server.")
        if isinstance(ctx.author, discord.Member) and ctx.author.guild_permissions.administrator:
            return True
        raise commands.CheckFailure("Only server administrators can use this bot.")

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
