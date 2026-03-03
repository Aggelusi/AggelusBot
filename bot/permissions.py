from __future__ import annotations

import discord

from bot.database import db


async def member_has_bot_access(member: discord.Member) -> bool:
	"""Return True if member is admin or has the configured bot access role."""
	if member.guild_permissions.administrator:
		return True

	role_id = await db.get_bot_access_role(member.guild.id)
	if role_id is None:
		return False

	return any(role.id == role_id for role in member.roles)


async def interaction_user_has_bot_access(interaction: discord.Interaction) -> bool:
	"""Return True if interaction user is a member with bot access in this guild."""
	if interaction.guild is None:
		return False

	member = (
		interaction.user
		if isinstance(interaction.user, discord.Member)
		else interaction.guild.get_member(interaction.user.id)
	)
	if not isinstance(member, discord.Member):
		return False

	return await member_has_bot_access(member)