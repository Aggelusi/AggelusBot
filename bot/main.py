from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import discord
from discord.ext import commands

from bot.config import settings
from bot.database import db


logging.basicConfig(
	level=logging.INFO,
	format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)


class HOI4Bot(commands.Bot):
	async def setup_hook(self) -> None:
		# setup_hook runs before login completes, which is the safest place
		# for startup tasks that should happen exactly once.
		await db.connect(settings.database_url)
		await self._load_cogs()

		if settings.dev_guild_id:
			guild = discord.Object(id=settings.dev_guild_id)
			self.tree.copy_global_to(guild=guild)
			synced = await self.tree.sync(guild=guild)
			logging.info("Synced %s app commands to DEV_GUILD_ID=%s", len(synced), settings.dev_guild_id)
		else:
			synced = await self.tree.sync()
			logging.info("Synced %s global app commands", len(synced))

	async def close(self) -> None:
		# We close DB pool first so pending command work fails fast and cleanly
		# before the Discord connection is fully gone.
		await db.close()
		await super().close()

	async def _load_cogs(self) -> None:
		commands_dir = Path(__file__).parent / "commands"

		for file in commands_dir.glob("*.py"):
			if file.name.startswith("_") or file.stem == "__init__":
				continue

			extension = f"bot.commands.{file.stem}"
			await self.load_extension(extension)
			logging.info("Loaded cog: %s", extension)


async def main() -> None:
	intents = discord.Intents.default()
	intents.message_content = True

	bot = HOI4Bot(command_prefix=settings.command_prefix, intents=intents)
	cleaned_stale_guild_commands = False

	@bot.event
	async def on_ready() -> None:
		nonlocal cleaned_stale_guild_commands

		# If DEV_GUILD_ID was used in the past, stale guild-scoped command copies
		# can remain and appear as duplicates beside global commands.
		# We clear guild scopes once at startup when not in dev-guild mode.
		if not settings.dev_guild_id and not cleaned_stale_guild_commands:
			for guild in bot.guilds:
				bot.tree.clear_commands(guild=guild)
				await bot.tree.sync(guild=guild)
				logging.info("Cleared stale guild app commands in guild=%s (%s)", guild.name, guild.id)
			cleaned_stale_guild_commands = True

		logging.info("Logged in as %s (%s)", bot.user, bot.user.id if bot.user else "unknown")

	@bot.event
	async def on_command_error(ctx: commands.Context, error: commands.CommandError) -> None:
		# Keep expected failures quiet and actionable instead of printing long traces.
		if isinstance(error, commands.CommandNotFound):
			return

		root_error = getattr(error, "original", error)
		if isinstance(root_error, discord.Forbidden):
			logging.warning(
				"Missing permissions for command '%s' in #%s (guild=%s)",
				ctx.command.qualified_name if ctx.command else "unknown",
				getattr(ctx.channel, "name", "unknown"),
				getattr(ctx.guild, "name", "DM"),
			)
			return

		logging.exception("Unhandled command error", exc_info=error)

	@bot.tree.error
	async def on_app_command_error(
		interaction: discord.Interaction,
		error: discord.app_commands.AppCommandError,
	) -> None:
		logging.exception("Unhandled app command error", exc_info=error)
		if isinstance(error, discord.app_commands.CommandSignatureMismatch):
			message = "Command schema is updating. Reload Discord and retry in a few seconds."
		else:
			message = "Something went wrong while processing this slash command."
		try:
			if interaction.response.is_done():
				await interaction.followup.send(message, ephemeral=True)
			else:
				await interaction.response.send_message(message, ephemeral=True)
		except (discord.HTTPException, discord.NotFound):
			# Interaction token may already be invalid/acknowledged; avoid noisy loops.
			pass

	await bot.start(settings.discord_token)


if __name__ == "__main__":
	asyncio.run(main())
