from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from bot.database import db


class SettingsCog(commands.Cog):
	settings = app_commands.Group(name="settings", description="Server-specific bot settings")

	def __init__(self, bot: commands.Bot) -> None:
		self.bot = bot

	@settings.command(name="announce_channel", description="Set the channel where game announcements are posted")
	@app_commands.describe(channel="Target channel for @everyone game announcements")
	@app_commands.default_permissions(manage_guild=True)
	async def set_announce_channel(
		self, interaction: discord.Interaction, channel: discord.TextChannel
	) -> None:
		if interaction.guild is None:
			await interaction.response.send_message("Use this in a server.", ephemeral=True)
			return

		await db.set_announce_channel(interaction.guild.id, channel.id)
		await interaction.response.send_message(
			f"Announcement channel set to {channel.mention}.",
			ephemeral=True,
		)

	@settings.command(name="log_channel", description="Set channel where closed game logs are posted")
	@app_commands.describe(channel="Target channel for end-of-game logs")
	@app_commands.default_permissions(manage_guild=True)
	async def set_log_channel(
		self, interaction: discord.Interaction, channel: discord.TextChannel
	) -> None:
		if interaction.guild is None:
			await interaction.response.send_message("Use this in a server.", ephemeral=True)
			return

		await db.set_log_channel(interaction.guild.id, channel.id)
		await interaction.response.send_message(
			f"Log channel set to {channel.mention}.",
			ephemeral=True,
		)

	@settings.command(name="major_lock_role", description="Set role required to reserve major nations")
	@app_commands.describe(role="Role that can reserve major main slots (not co-ops)")
	@app_commands.default_permissions(manage_guild=True)
	async def set_major_lock_role(
		self, interaction: discord.Interaction, role: discord.Role
	) -> None:
		if interaction.guild is None:
			await interaction.response.send_message("Use this in a server.", ephemeral=True)
			return

		await db.set_major_lock_role(interaction.guild.id, role.id)
		await interaction.response.send_message(
			f"Major nation lock role set to {role.mention}.",
			ephemeral=True,
		)

	@settings.command(name="admin_notify_channel", description="Set channel for unreserve/admin activity notices")
	@app_commands.describe(channel="Channel for admin activity messages (no role ping)")
	@app_commands.default_permissions(manage_guild=True)
	async def set_admin_notify_channel(
		self, interaction: discord.Interaction, channel: discord.TextChannel
	) -> None:
		if interaction.guild is None:
			await interaction.response.send_message("Use this in a server.", ephemeral=True)
			return

		await db.set_admin_notify_channel(interaction.guild.id, channel.id)
		await interaction.response.send_message(
			f"Admin notify channel set to {channel.mention}.",
			ephemeral=True,
		)


async def setup(bot: commands.Bot) -> None:
	await bot.add_cog(SettingsCog(bot))
