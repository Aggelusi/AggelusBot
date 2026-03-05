from __future__ import annotations

import re

import discord
from discord import app_commands
from discord.ext import commands

from bot.database import db
from bot.permissions import interaction_user_has_bot_access, member_has_bot_access


_NATION_FULL_NAMES = {
	"USA": "United States",
	"UK": "United Kingdom",
	"FRA": "France",
	"POL": "Poland",
	"RAJ": "British Raj",
	"CAN": "Canada",
	"SAF": "South Africa",
	"AST": "Australia",
	"BRA": "Brazil",
	"MEX": "Mexico",
	"NET": "Netherlands",
	"GER": "Germany",
	"ITA": "Italy",
	"ROM": "Romania",
	"HUN": "Hungary",
	"BUL": "Bulgaria",
	"FIN": "Finland",
	"SPN": "Spain",
	"YUG": "Yugoslavia",
	"DEN": "Denmark",
	"VICHY": "Vichy France",
	"SOV": "Soviet Union",
	"MON": "Mongolia",
	"JAPAN": "Japan",
	"MAN": "Manchukuo",
	"SIA": "Siam",
}


class ReservationsCog(commands.Cog):
	def __init__(self, bot: commands.Bot) -> None:
		self.bot = bot

	def _is_public_prefix_command(self, ctx: commands.Context) -> bool:
		command_name = ctx.command.qualified_name if ctx.command else ""
		return command_name in {"reserve", "unreserve"}

	def _is_public_slash_command(self, interaction: discord.Interaction) -> bool:
		command_name = interaction.command.name if interaction.command else ""
		return command_name in {"reserve", "unreserve"}

	async def cog_check(self, ctx: commands.Context) -> bool:
		if ctx.guild is None:
			raise commands.CheckFailure("Use this command in a server.")
		if self._is_public_prefix_command(ctx):
			return True
		if isinstance(ctx.author, discord.Member) and await member_has_bot_access(ctx.author):
			return True
		raise commands.CheckFailure("Only server administrators or members with the bot access role can use this bot.")

	async def interaction_check(self, interaction: discord.Interaction) -> bool:
		if interaction.guild is None:
			if interaction.response.is_done():
				await interaction.followup.send("Use this command in a server.", ephemeral=True)
			else:
				await interaction.response.send_message("Use this command in a server.", ephemeral=True)
			return False

		if self._is_public_slash_command(interaction):
			return True

		if await interaction_user_has_bot_access(interaction):
			return True

		if interaction.response.is_done():
			await interaction.followup.send("Only server administrators or members with the bot access role can use this bot.", ephemeral=True)
		else:
			await interaction.response.send_message("Only server administrators or members with the bot access role can use this bot.", ephemeral=True)
		return False

	async def _safe_defer(self, interaction: discord.Interaction) -> bool:
		"""Defer interaction safely. Returns False if interaction token is invalid."""
		if interaction.response.is_done():
			return True
		try:
			await interaction.response.defer(ephemeral=True, thinking=False)
			return True
		except (discord.NotFound, discord.HTTPException):
			return False

	def _choice_label(self, nation: str) -> str:
		# Keep autocomplete clean: use stable tag labels (no flag fallback text like "de").
		tag = nation.split()[0].upper()
		coop_match = re.search(r"\(Co-op\s+\d+\)", nation, flags=re.IGNORECASE)
		if coop_match:
			return f"{tag} coop"
		return tag

	def _reserve_choice_label(self, nation: str) -> str:
		base = nation.split(" (Co-op", 1)[0]
		tag = base.split()[0].upper()
		full_name = _NATION_FULL_NAMES.get(tag, tag)
		display_base = full_name

		coop_match = re.search(r"\(Co-op\s+\d+\)", nation, flags=re.IGNORECASE)
		if coop_match:
			return f"{display_base} {coop_match.group(0)}"
		return display_base

	def _preferences_country_list(self) -> list[str]:
		"""Return clean country options for draft preferences.

		Includes one co-op option per major nation.
		"""
		nations = db.build_nation_pool_for_preset("normal")
		seen: set[str] = set()
		result: list[str] = []
		for nation in nations:
			label = self._choice_label(nation)
			if label in seen:
				continue
			seen.add(label)
			result.append(label)
		return result

	def _tag_display_map(self) -> dict[str, str]:
		"""Map tag -> nation label including flag, e.g. GER -> GER 🇩🇪."""
		mapping: dict[str, str] = {}
		for nation in db.build_nation_pool_for_preset("normal"):
			base = nation.split(" (Co-op", 1)[0]
			tag = base.split()[0].upper()
			mapping.setdefault(tag, base)
		return mapping

	def _format_preference_choice(self, raw_choice: str) -> str:
		"""Format stored preference value to include flag and co-op marker."""
		choice = raw_choice.strip()
		if not choice:
			return choice

		tag = choice.split()[0].upper()
		tag_map = self._tag_display_map()
		base = tag_map.get(tag, tag)

		is_coop = bool(
			re.search(r"co\s*-?\s*op", choice, flags=re.IGNORECASE)
			or "(Co-op" in choice
		)
		if is_coop:
			return f"{base} (Co-op)"
		return base

	async def _preferences_autocomplete(
		self,
		interaction: discord.Interaction,
		current: str,
	) -> list[app_commands.Choice[str]]:
		countries = self._preferences_country_list()
		return [
			app_commands.Choice(name=country[:100], value=country)
			for country in countries
			if current.lower() in country.lower()
		][:25]

	async def _notify_admin_unreserve(
		self,
		guild: discord.Guild | None,
		player_name: str,
		nation_name: str,
		game_title: str,
	) -> None:
		if guild is None:
			return
		channel_id = await db.get_admin_notify_channel(guild.id)
		if channel_id is None:
			return
		channel = guild.get_channel(channel_id)
		if isinstance(channel, discord.TextChannel):
			await channel.send(
				f"Unreserve notice: {player_name} unreserved {nation_name} in '{game_title}'."
			)

	async def _refresh_sheet_message(self, game_id: int) -> None:
		game = await db.get_game(game_id)
		if game is None:
			return

		thread_id = game["reservation_thread_id"]
		sheet_message_id = game["reservation_sheet_message_id"]
		if not thread_id:
			return

		channel = self.bot.get_channel(int(thread_id))
		if not isinstance(channel, discord.Thread):
			return

		if str(game["preset"]) == "no_sheet":
			pref_rows = await db.list_game_preferences(game_id)
			lines = [f"## Draft Preferences — {game['title']}", ""]
			if not pref_rows:
				lines.append("No preferences submitted yet.")
			else:
				for row in pref_rows:
					choices = [self._format_preference_choice(str(x)) for x in row["choices"]]
					lines.append(f"<@{int(row['user_id'])}>: {', '.join(choices)}")
			lines.extend(["", "Use `/preferences` to update your top 5 countries."])
		else:
			rows = await db.list_sheet(game_id)
			lines = db.build_sheet_display_lines(str(game["title"]), rows)

		content = "\n".join(lines)
		message: discord.Message | None = None

		if sheet_message_id:
			try:
				message = await channel.fetch_message(int(sheet_message_id))
			except (discord.NotFound, discord.Forbidden):
				message = None

		if message is None:
			try:
				message = await channel.send(content)
			except discord.Forbidden:
				return
			await db.set_game_reservation_sheet_message(game_id, message.id)
		else:
			await message.edit(content=content)

		if str(game["preset"]) == "no_sheet":
			try:
				await message.pin(reason="Keep draft preferences visible")
			except (discord.Forbidden, discord.HTTPException):
				pass

	async def _available_nation_autocomplete(
		self,
		interaction: discord.Interaction,
		current: str,
	) -> list[app_commands.Choice[str]]:
		if not isinstance(interaction.channel, discord.Thread):
			return []

		game = await db.get_game_by_thread_id(interaction.channel.id)
		if game is None:
			return []

		available = await db.list_available_nations(int(game["id"]))
		choices: list[app_commands.Choice[str]] = []
		query = current.strip().lower()
		seen_coop_tags: set[str] = set()
		for nation in available:
			nation_text = str(nation)
			base = nation_text.split(" (Co-op", 1)[0]
			tag = base.split()[0].upper()
			coop_match = re.search(r"\(Co-op\s+\d+\)", nation_text, flags=re.IGNORECASE)

			if coop_match:
				if tag in seen_coop_tags:
					continue
				seen_coop_tags.add(tag)
				label = self._reserve_choice_label(base) + " (Co-op)"
				value = f"{tag} coop"
			else:
				label = self._reserve_choice_label(nation_text)
				value = nation_text

			if query and query not in label.lower() and query not in str(nation).lower():
				continue
			choices.append(app_commands.Choice(name=label[:100], value=value[:100]))
		return choices[:25]

	@commands.command(name="sheet_create")
	async def sheet_create(self, ctx: commands.Context, game_id: int) -> None:
		"""Create an empty reservation sheet for a game."""
		game = await db.get_game(game_id)
		if game is None:
			await ctx.send(f"Game with ID {game_id} was not found.")
			return

		inserted = await db.create_reservation_sheet(game_id)
		if inserted == 0:
			await ctx.send("Reservation sheet already exists for this game.")
			return

		await ctx.send(f"Reservation sheet created for game **{game_id}** with {inserted} nations.")

	@commands.command(name="sheet")
	async def sheet(self, ctx: commands.Context, game_id: int) -> None:
		"""Show current reservations for one game."""
		rows = await db.list_sheet(game_id)
		if not rows:
			await ctx.send("No reservation sheet found. Run `!sheet_create <game_id>` first.")
			return

		lines = [f"Reservation sheet for game {game_id}:"]
		for row in rows:
			if row["reserved_by"] is None:
				lines.append(f"- {row['nation_name']}:")
			else:
				lines.append(f"- {row['nation_name']}: <@{int(row['reserved_by'])}>")

		await ctx.send("\n".join(lines))

	@commands.command(name="reserve")
	async def reserve(self, ctx: commands.Context, game_id: int, *, nation_name: str) -> None:
		"""Reserve an available nation in a game."""
		game = await db.get_game(game_id)
		if game is None:
			await ctx.send(f"Game with ID {game_id} was not found.")
			return

		success = await db.reserve_nation(
			game_id=game_id,
			nation_name=nation_name,
			user_id=ctx.author.id,
			user_name=ctx.author.display_name,
		)
		if success:
			await ctx.send(f"{ctx.author.mention} reserved **{nation_name}** for game **{game_id}**.")
			return

		reservation = await db.get_nation_reservation(game_id, nation_name)
		if reservation is None:
			await ctx.send("Nation not found in sheet. Check spelling or create the sheet first.")
			return

		if reservation["reserved_by"] is not None:
			await ctx.send(
				f"{reservation['nation_name']}: <@{int(reservation['reserved_by'])}>"
			)
			return

		await ctx.send("Could not reserve nation due to an unknown issue. Try again.")

	@app_commands.command(name="reserve", description="Reserve an available nation in this game thread")
	@app_commands.describe(nation="Nation to reserve")
	async def reserve_slash(
		self,
		interaction: discord.Interaction,
		nation: str,
	) -> None:
		if not await self._safe_defer(interaction):
			return

		if not isinstance(interaction.channel, discord.Thread):
			await interaction.followup.send(
				"Use `/reserve` inside a game reservation thread.",
				ephemeral=True,
			)
			return

		game = await db.get_game_by_thread_id(interaction.channel.id)
		if game is None:
			await interaction.followup.send(
				"This thread is not linked to an active game.",
				ephemeral=True,
			)
			return

		game_id = int(game["id"])
		if str(game["preset"]) == "no_sheet":
			await interaction.followup.send(
				"This game uses no-sheet draft mode. Use `/preferences` instead.",
				ephemeral=True,
			)
			return

		if interaction.guild and int(game["guild_id"]) != int(interaction.guild.id):
			await interaction.followup.send("Game/server mismatch.", ephemeral=True)
			return

		my_reservations = await db.get_user_reserved_nations(game_id, interaction.user.id)
		if my_reservations:
			choices = "\n".join(f"- {row['nation_name']}" for row in my_reservations)
			await interaction.followup.send(
				"You already reserved a nation in this game. Unreserve first before picking another:\n"
				+ choices,
				ephemeral=True,
			)
			return

		resolved_nation = await db.resolve_nation_name(game_id, nation)
		coop_pick = re.match(r"^([A-Za-z]+)\s*(?:\(\s*)?co\s*-?\s*op(?:\s*\))?$", nation.strip(), flags=re.IGNORECASE)
		if coop_pick:
			major_tag = coop_pick.group(1).upper()
			resolved_nation = await db.get_first_available_coop_slot(game_id, major_tag)
			if resolved_nation is None:
				await interaction.followup.send(
					f"No free {major_tag} co-op slots are available.",
					ephemeral=True,
				)
				return

		if resolved_nation is None:
			available = await db.list_available_nations(game_id)
			preview = "\n".join(f"- {nation}" for nation in available[:20]) or "- none"
			await interaction.followup.send(
				"Nation not found. Available nations right now:\n" + preview,
				ephemeral=True,
			)
			return

		if bool(game["majors_locked"]) and db.is_major_non_coop_nation(resolved_nation):
			role_id = await db.get_major_lock_role(int(game["guild_id"]))
			if role_id and isinstance(interaction.user, discord.Member):
				has_role = any(role.id == role_id for role in interaction.user.roles)
				if not has_role:
					role_obj = interaction.guild.get_role(role_id) if interaction.guild else None
					role_label = role_obj.mention if role_obj else f"role ID {role_id}"
					await interaction.followup.send(
						f"You need {role_label} to reserve major main slots.",
						ephemeral=True,
					)
					return

		success = await db.reserve_nation(
			game_id=game_id,
			nation_name=resolved_nation,
			user_id=interaction.user.id,
			user_name=interaction.user.display_name,
		)
		if success:
			await self._refresh_sheet_message(game_id)
			await interaction.followup.send(
				f"{interaction.user.mention} reserved **{resolved_nation}**."
			)
			return

		reservation = await db.get_nation_reservation(game_id, resolved_nation)
		if reservation is None:
			await interaction.followup.send(
				"Nation not found in sheet. Check spelling or create the sheet first.",
				ephemeral=True,
			)
			return

		await interaction.followup.send(
			f"{reservation['nation_name']}: <@{int(reservation['reserved_by'])}>",
			ephemeral=True,
		)

	@commands.command(name="unreserve")
	async def unreserve(self, ctx: commands.Context, game_id: int, *, nation_name: str) -> None:
		"""Remove a reservation (owner or game host)."""
		reservation = await db.get_nation_reservation(game_id, nation_name)
		if reservation is None:
			await ctx.send("Nation not found in this sheet.")
			return

		if reservation["reserved_by"] is None:
			await ctx.send(f"{reservation['nation_name']} is already available.")
			return

		is_owner = int(reservation["reserved_by"]) == int(ctx.author.id)
		is_host = await db.is_game_host(game_id, ctx.author.id)

		# Allowing host override keeps lobby flow moving if someone is offline.
		if not is_owner and not is_host:
			await ctx.send("Only the player who reserved this nation or the game host can unreserve it.")
			return

		success = await db.unreserve_nation(game_id, reservation["nation_name"])
		if success:
			await ctx.send(f"{reservation['nation_name']} is now available again.")
		else:
			await ctx.send("Could not unreserve nation due to an unknown issue. Try again.")

	@app_commands.command(name="unreserve", description="Unreserve your nation in this game thread")
	async def unreserve_slash(
		self,
		interaction: discord.Interaction,
	) -> None:
		if not await self._safe_defer(interaction):
			return

		if not isinstance(interaction.channel, discord.Thread):
			await interaction.followup.send(
				"Use `/unreserve` inside a game reservation thread.",
				ephemeral=True,
			)
			return

		game = await db.get_game_by_thread_id(interaction.channel.id)
		if game is None:
			await interaction.followup.send(
				"This thread is not linked to an active game.",
				ephemeral=True,
			)
			return

		game_id = int(game["id"])
		if str(game["preset"]) == "no_sheet":
			removed = await db.clear_game_preferences(game_id, interaction.user.id)
			if removed:
				await self._refresh_sheet_message(game_id)
				await self._notify_admin_unreserve(
					interaction.guild,
					interaction.user.display_name,
					"draft preferences",
					str(game["title"]),
				)
				await interaction.followup.send(
					"Your draft preferences were removed.",
					ephemeral=True,
				)
			else:
				await interaction.followup.send(
					"You do not currently have saved draft preferences in this game.",
					ephemeral=True,
				)
			return

		if interaction.guild and int(game["guild_id"]) != int(interaction.guild.id):
			await interaction.followup.send("Game/server mismatch.", ephemeral=True)
			return

		my_reservations = await db.get_user_reserved_nations(game_id, interaction.user.id)
		if not my_reservations:
			await interaction.followup.send("You do not currently have a reserved nation in this game.", ephemeral=True)
			return

		if len(my_reservations) > 1:
			choices = "\n".join(f"- {row['nation_name']}" for row in my_reservations)
			await interaction.followup.send(
				"You have multiple reserved nations. Ask an admin to use `/game edit` to unreserve a specific one:\n"
				+ choices,
				ephemeral=True,
			)
			return

		reservation = my_reservations[0]
		success = await db.unreserve_nation(game_id, str(reservation["nation_name"]))
		if success:
			await self._refresh_sheet_message(game_id)
			await self._notify_admin_unreserve(
				interaction.guild,
				interaction.user.display_name,
				str(reservation["nation_name"]),
				str(game["title"]),
			)
			await interaction.followup.send(
				f"{reservation['nation_name']} is now available again."
			)
		else:
			await interaction.followup.send(
				"Could not unreserve nation due to an unknown issue. Try again.",
				ephemeral=True,
			)

	@reserve_slash.autocomplete("nation")
	async def reserve_nation_autocomplete(
		self,
		interaction: discord.Interaction,
		current: str,
	) -> list[app_commands.Choice[str]]:
		return await self._available_nation_autocomplete(interaction, current)



	@app_commands.command(name="preferences", description="Set top 5 country preferences for no-sheet draft games")
	@app_commands.describe(
		top1="Top choice",
		top2="Second choice",
		top3="Third choice",
		top4="Fourth choice",
		top5="Fifth choice",
	)
	async def preferences_slash(
		self,
		interaction: discord.Interaction,
		top1: str,
		top2: str,
		top3: str,
		top4: str,
		top5: str,
	) -> None:
		if not await self._safe_defer(interaction):
			return

		if not isinstance(interaction.channel, discord.Thread):
			await interaction.followup.send("Use `/preferences` in the game thread.", ephemeral=True)
			return

		game = await db.get_game_by_thread_id(interaction.channel.id)
		if game is None:
			await interaction.followup.send("This thread is not linked to an active game.", ephemeral=True)
			return

		if str(game["preset"]) != "no_sheet":
			await interaction.followup.send(
				"This game is not in no-sheet mode. Use `/reserve` instead.",
				ephemeral=True,
			)
			return

		choices = [top1.strip(), top2.strip(), top3.strip(), top4.strip(), top5.strip()]
		await db.set_game_preferences(
			game_id=int(game["id"]),
			user_id=interaction.user.id,
			user_name=interaction.user.display_name,
			choices=choices,
		)
		await self._refresh_sheet_message(int(game["id"]))
		await interaction.followup.send("Saved your top 5 preferences.", ephemeral=True)

	@preferences_slash.autocomplete("top1")
	@preferences_slash.autocomplete("top2")
	@preferences_slash.autocomplete("top3")
	@preferences_slash.autocomplete("top4")
	@preferences_slash.autocomplete("top5")
	async def preferences_country_autocomplete(
		self,
		interaction: discord.Interaction,
		current: str,
	) -> list[app_commands.Choice[str]]:
		return await self._preferences_autocomplete(interaction, current)


async def setup(bot: commands.Bot) -> None:
	await bot.add_cog(ReservationsCog(bot))
