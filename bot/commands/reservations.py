from __future__ import annotations

import re
import random

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
	"FILL": "Fill",
}


class ReservationsCog(commands.Cog):
	draft = app_commands.Group(name="draft", description="Draft reservations, captain voting, and team picks")

	def __init__(self, bot: commands.Bot) -> None:
		self.bot = bot

	def _is_public_prefix_command(self, ctx: commands.Context) -> bool:
		command_name = ctx.command.qualified_name if ctx.command else ""
		return command_name in {"reserve", "unreserve"}

	def _is_public_slash_command(self, interaction: discord.Interaction) -> bool:
		command_name = interaction.command.name if interaction.command else ""
		return command_name in {"reserve", "unreserve", "draft", "draft_preferences", "preferences"}

	def _format_draft_slot(self, slot_type: str) -> str:
		return db.draft_slot_label(slot_type)

	def _is_late_allowed_preference(self, choice: str) -> bool:
		normalized = choice.strip()
		if not normalized:
			return False
		tag = normalized.split()[0].upper()
		is_coop = bool(re.search(r"co\s*-?\s*op", normalized, flags=re.IGNORECASE))
		return is_coop or tag == "VICHY"

	def _nation_tag(self, nation_name: str) -> str:
		base = nation_name.split(" (Co-op", 1)[0]
		return base.split()[0].upper()

	def _select_top_two_captains(
		self,
		candidates: list[object],
		vote_totals: dict[int, int],
	) -> list[int]:
		sorted_ids = sorted(
			[int(candidate["user_id"]) for candidate in candidates],
			key=lambda user_id: (vote_totals.get(user_id, 0), random.random()),
			reverse=True,
		)
		return sorted_ids[:2]

	async def _build_draft_sheet_lines(self, game_id: int, title: str) -> list[str]:
		reservations = await db.list_draft_reservations(game_id)
		preferences = await db.list_game_preferences(game_id)
		vote_rows = await db.list_draft_captain_vote_totals(game_id)
		state = await db.get_draft_state(game_id)
		assignments = await db.list_draft_team_assignments(game_id)
		bans = await db.list_draft_player_bans(game_id)
		sheet_rows = await db.list_sheet(game_id)

		vote_totals = {int(row["user_id"]): int(row["vote_count"]) for row in vote_rows}
		nation_by_user: dict[int, str] = {}
		for row in sheet_rows:
			if row["reserved_by"] is None:
				continue
			nation_by_user[int(row["reserved_by"])] = str(row["nation_name"])
		total_slots = len(sheet_rows) if sheet_rows else len(db.build_nation_pool_for_preset("normal"))

		preferences_by_user: dict[int, list[str]] = {}
		for row in preferences:
			preferences_by_user[int(row["user_id"])] = [str(choice) for choice in row["choices"]]

		lines = [f"## Draft Lobby - {title}", ""]
		lines.append(f"### Player Pool ({len(reservations)}/{total_slots}):")
		if not reservations:
			lines.append("No draft reservations yet. Use `/draft reserve`.")
		else:
			for index, row in enumerate(reservations, start=1):
				user_id = int(row["user_id"])
				badges: list[str] = []
				if bool(row["captain_candidate"]):
					badges.append("🎯")
				if str(row["slot_type"]) == "late_hotjoin":
					badges.append("⏰ Late")

				line = f"{index}. <@{user_id}>"
				if badges:
					line += f" {' '.join(badges)}"

				raw_choices = preferences_by_user.get(user_id, [])
				if raw_choices:
					formatted_choices = [self._format_preference_choice(choice) for choice in raw_choices]
					line += f" — {', '.join(formatted_choices)}"
				lines.append(line)

		lines.extend(["", "### Captain Candidates"])
		captain_rows = [row for row in reservations if bool(row["captain_candidate"])]
		if not captain_rows:
			lines.append("No captain candidates yet. Use `/draft reserve` with `captain:True`.")
		else:
			captain_rows.sort(
				key=lambda row: (vote_totals.get(int(row["user_id"]), 0), -int(row["user_id"])),
				reverse=True,
			)
			for row in captain_rows:
				user_id = int(row["user_id"])
				lines.append(f"- <@{user_id}>: {vote_totals.get(user_id, 0)} votes")

		if state is not None:
			lines.extend(["", "### Draft State"])
			lines.append(f"- Phase: {str(state['phase']).replace('_', ' ').title()}")
			phase = str(state["phase"])
			if phase in {"choice_pending", "side_pending"}:
				if state["allies_captain_user_id"]:
					lines.append(f"- Captain A: <@{int(state['allies_captain_user_id'])}>")
				if state["axis_captain_user_id"]:
					lines.append(f"- Captain B: <@{int(state['axis_captain_user_id'])}>")
			else:
				if state["allies_captain_user_id"]:
					lines.append(f"- Allies Captain: <@{int(state['allies_captain_user_id'])}>")
				if state["axis_captain_user_id"]:
					lines.append(f"- Axis Captain: <@{int(state['axis_captain_user_id'])}>")
			if state["coin_winner_user_id"]:
				lines.append(f"- Coin Winner: <@{int(state['coin_winner_user_id'])}>")
			if state["next_picker_user_id"]:
				lines.append(f"- Next Pick: <@{int(state['next_picker_user_id'])}>")

		if assignments:
			allies = [row for row in assignments if str(row["team_side"]) == "allies"]
			axis = [row for row in assignments if str(row["team_side"]) == "axis"]

			lines.extend(["", "### Teams"])
			lines.append("Allies:")
			for row in allies:
				user_id = int(row["user_id"])
				nation = nation_by_user.get(user_id, "No nation assigned")
				pick_no = int(row["pick_number"])
				prefix = "C" if pick_no == 0 else str(pick_no)
				lines.append(f"- [{prefix}] <@{user_id}> - {nation}")
			lines.append("Axis:")
			for row in axis:
				user_id = int(row["user_id"])
				nation = nation_by_user.get(user_id, "No nation assigned")
				pick_no = int(row["pick_number"])
				prefix = "C" if pick_no == 0 else str(pick_no)
				lines.append(f"- [{prefix}] <@{user_id}> - {nation}")

		if bans:
			lines.extend(["", "### Draft Bans"])
			for row in bans:
				captain_id = int(row["captain_user_id"])
				target_id = int(row["target_user_id"])
				nation_tag = str(row["nation_tag"]).upper()
				lines.append(f"- <@{captain_id}> banned <@{target_id}> from **{nation_tag}** (incl. co-op slots)")

		if sheet_rows:
			lines.extend(["", "### Nation Sheet"])
			nation_sheet_lines = db.build_sheet_display_lines(title, sheet_rows)
			if nation_sheet_lines and nation_sheet_lines[0].startswith("## "):
				nation_sheet_lines = nation_sheet_lines[2:] if len(nation_sheet_lines) > 1 and nation_sheet_lines[1] == "" else nation_sheet_lines[1:]
			lines.extend(nation_sheet_lines)

		lines.extend(
			[
				"",
				"Reserve with `/draft reserve`.",
				"Set preferences with `/draft_preferences` (up to 5 nations).",
				"Captain signup: `/draft reserve` with `captain:True`.",
				"Use `/draft reserve` with `late:True` if you cannot be there early.",
				"Use `/draft vote` (up to 2 candidates, no self-vote), then host runs `/draft start`.",
				"Player draft: captains use `/draft pick` to pull players to Allies/Axis.",
				"Each captain can ban once with `/draft ban`, then run `/draft begin_assignments`.",
				"Assignment phase: captains use `/draft assign` to place their side on nations.",
			]
		)
		return lines

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

	async def _safe_defer(self, interaction: discord.Interaction, *, ephemeral: bool = True) -> bool:
		"""Defer interaction safely. Returns False if interaction token is invalid."""
		if interaction.response.is_done():
			return True
		try:
			await interaction.response.defer(ephemeral=ephemeral, thinking=False)
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
			lines = await self._build_draft_sheet_lines(game_id, str(game["title"]))
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
				await message.pin(reason="Keep draft lobby visible")
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
				"This game uses draft mode. Use `/draft reserve` instead.",
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
			removed = await db.clear_draft_reservation(game_id, interaction.user.id)
			if removed:
				await self._refresh_sheet_message(game_id)
				await self._notify_admin_unreserve(
					interaction.guild,
					interaction.user.display_name,
					"draft reservation",
					str(game["title"]),
				)
				await interaction.followup.send(
					"Your draft reservation was removed.",
					ephemeral=True,
				)
			else:
				await interaction.followup.send(
					"You do not currently have a draft reservation in this game.",
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

	async def _get_draft_game_in_thread(
		self,
		interaction: discord.Interaction,
	) -> tuple[discord.Thread | None, int | None, object | None]:
		if not isinstance(interaction.channel, discord.Thread):
			await interaction.followup.send("Use this command inside a game thread.", ephemeral=True)
			return None, None, None

		game = await db.get_game_by_thread_id(interaction.channel.id)
		if game is None:
			await interaction.followup.send("This thread is not linked to an active game.", ephemeral=True)
			return None, None, None

		if str(game["preset"]) != "no_sheet":
			await interaction.followup.send("This game is not a draft preset game.", ephemeral=True)
			return None, None, None

		if interaction.guild and int(game["guild_id"]) != int(interaction.guild.id):
			await interaction.followup.send("Game/server mismatch.", ephemeral=True)
			return None, None, None

		return interaction.channel, int(game["id"]), game

	@draft.command(name="reserve", description="Join the draft pool and optionally mark yourself captain/late")
	@app_commands.describe(
		captain="Also join captain candidates",
		late="Mark yourself as late (cannot be captain)",
	)
	async def draft_reserve_slash(
		self,
		interaction: discord.Interaction,
		captain: bool = False,
		late: bool = False,
	) -> None:
		if not await self._safe_defer(interaction):
			return

		_, game_id, game = await self._get_draft_game_in_thread(interaction)
		if game_id is None or game is None:
			return

		if captain and late:
			await interaction.followup.send(
				"Late players cannot be captain candidates.",
				ephemeral=True,
			)
			return

		if late:
			saved_preferences = await db.get_game_preferences_for_user(game_id, interaction.user.id)
			if saved_preferences is not None:
				choices = [str(choice) for choice in saved_preferences["choices"]]
				invalid = [choice for choice in choices if not self._is_late_allowed_preference(choice)]
				if invalid:
					await interaction.followup.send(
						"Your current preferences include major/minor picks. "
						"Late players can only keep co-op slots and VICHY. "
						"Update them first with `/draft_preferences`.",
						ephemeral=True,
					)
					return

		slot_value = "late_hotjoin" if late else "player"

		existing = await db.get_draft_reservation(game_id, interaction.user.id)
		if existing is not None and bool(existing["captain_candidate"]) and captain:
			# No-op path kept explicit so users can re-run with same captain preference.
			pass

		await db.set_draft_reservation(
			game_id=game_id,
			user_id=interaction.user.id,
			user_name=interaction.user.display_name,
			slot_type=slot_value,
			captain_candidate=captain,
		)
		await self._refresh_sheet_message(game_id)

		status_parts: list[str] = []
		if captain:
			status_parts.append("captain candidate")
		if late:
			status_parts.append("late")
		status = f" ({', '.join(status_parts)})" if status_parts else ""
		await interaction.followup.send(
			f"Saved draft reservation{status}.",
			ephemeral=True,
		)

	@draft.command(name="unreserve", description="Remove your draft reservation")
	async def draft_unreserve_slash(self, interaction: discord.Interaction) -> None:
		if not await self._safe_defer(interaction):
			return

		_, game_id, _ = await self._get_draft_game_in_thread(interaction)
		if game_id is None:
			return

		removed = await db.clear_draft_reservation(game_id, interaction.user.id)
		if not removed:
			await interaction.followup.send("You do not have a draft reservation in this game.", ephemeral=True)
			return

		await self._refresh_sheet_message(game_id)
		await interaction.followup.send("Removed your draft reservation.", ephemeral=True)

	@draft.command(name="vote", description="Vote for captain candidates (up to two total)")
	@app_commands.describe(candidate="Captain candidate to vote for")
	async def draft_vote_slash(self, interaction: discord.Interaction, candidate: discord.Member) -> None:
		if not await self._safe_defer(interaction):
			return

		_, game_id, _ = await self._get_draft_game_in_thread(interaction)
		if game_id is None:
			return

		voter_row = await db.get_draft_reservation(game_id, interaction.user.id)
		if voter_row is None:
			await interaction.followup.send("Reserve a draft slot first using `/draft reserve`.", ephemeral=True)
			return

		if candidate.id == interaction.user.id:
			await interaction.followup.send("Captain candidates cannot vote for themselves.", ephemeral=True)
			return

		candidate_row = await db.get_draft_reservation(game_id, candidate.id)
		if candidate_row is None or not bool(candidate_row["captain_candidate"]):
			await interaction.followup.send("That user is not a captain candidate in this draft.", ephemeral=True)
			return

		existing_votes = await db.list_draft_votes_by_voter(game_id, interaction.user.id)
		existing_candidate_ids = {int(row["candidate_user_id"]) for row in existing_votes}
		if int(candidate.id) in existing_candidate_ids:
			await interaction.followup.send(f"You already voted for {candidate.mention}.", ephemeral=True)
			return
		if len(existing_candidate_ids) >= 2:
			await interaction.followup.send("You already used both captain votes (max 2).", ephemeral=True)
			return

		await db.set_draft_captain_vote(game_id, interaction.user.id, candidate.id)
		await self._refresh_sheet_message(game_id)
		await interaction.followup.send(
			f"Vote saved for {candidate.mention}. You have used {len(existing_candidate_ids) + 1}/2 votes.",
			ephemeral=True,
		)

	@draft.command(name="start", description="Start draft using top-2 voted captain candidates")
	async def draft_start_slash(self, interaction: discord.Interaction) -> None:
		if not await self._safe_defer(interaction, ephemeral=False):
			return

		_, game_id, game = await self._get_draft_game_in_thread(interaction)
		if game_id is None or game is None:
			return

		is_host = int(game["host_discord_id"]) == int(interaction.user.id)
		has_access = await interaction_user_has_bot_access(interaction)
		if not is_host and not has_access:
			await interaction.followup.send("Only the host or bot staff can start the draft.", ephemeral=True)
			return

		candidates = await db.list_draft_captain_candidates(game_id)
		if len(candidates) < 2:
			await interaction.followup.send("Need exactly two captain candidates before starting.", ephemeral=True)
			return

		vote_rows = await db.list_draft_captain_vote_totals(game_id)
		vote_totals = {int(row["user_id"]): int(row["vote_count"]) for row in vote_rows}
		top_two = self._select_top_two_captains(candidates, vote_totals)
		if len(top_two) < 2:
			await interaction.followup.send("Could not determine the top two captains.", ephemeral=True)
			return

		captain_a, captain_b = top_two[0], top_two[1]
		coin_winner = random.choice([captain_a, captain_b])

		await db.create_reservation_sheet_for_preset(game_id, "normal")
		await db.clear_all_sheet_reservations(game_id)
		await db.reset_draft_team_assignments(game_id)
		await db.reset_draft_state(game_id)
		await db.clear_draft_bans(game_id)

		captain_a_name = interaction.guild.get_member(captain_a).display_name if interaction.guild and interaction.guild.get_member(captain_a) else f"user-{captain_a}"
		captain_b_name = interaction.guild.get_member(captain_b).display_name if interaction.guild and interaction.guild.get_member(captain_b) else f"user-{captain_b}"
		# Temporary sides until `/draft side` finalizes them.
		await db.add_draft_team_assignment(game_id, captain_a, captain_a_name, "allies", captain_a, 0)
		await db.add_draft_team_assignment(game_id, captain_b, captain_b_name, "axis", captain_b, 0)

		await db.update_draft_state(
			game_id,
			phase="choice_pending",
			coin_winner_user_id=coin_winner,
			allies_captain_user_id=captain_a,
			axis_captain_user_id=captain_b,
		)
		await db.clear_draft_votes(game_id)
		await self._refresh_sheet_message(game_id)

		await interaction.followup.send(
			"Draft started. Captains selected:\n"
			f"- Captain A: <@{captain_a}>\n"
			f"- Captain B: <@{captain_b}>\n"
			f"Coin winner: <@{coin_winner}>\n"
			"Coin winner must now choose with `/draft choose`.",
		)

	@draft.command(name="choose", description="Coin winner chooses first pick or side choice")
	@app_commands.describe(option="Choose first pick or choose side")
	@app_commands.choices(
		option=[
			app_commands.Choice(name="Take first pick", value="first_pick"),
			app_commands.Choice(name="Choose side (Axis/Allies)", value="choose_side"),
		]
	)
	async def draft_choose_slash(
		self,
		interaction: discord.Interaction,
		option: app_commands.Choice[str],
	) -> None:
		if not await self._safe_defer(interaction, ephemeral=False):
			return

		_, game_id, _ = await self._get_draft_game_in_thread(interaction)
		if game_id is None:
			return

		state = await db.get_draft_state(game_id)
		if state is None or str(state["phase"]) != "choice_pending":
			await interaction.followup.send("Draft is not waiting for the coin winner choice.", ephemeral=True)
			return

		coin_winner = int(state["coin_winner_user_id"]) if state["coin_winner_user_id"] else None
		if coin_winner is None or int(interaction.user.id) != coin_winner:
			await interaction.followup.send("Only the coin winner can use this command now.", ephemeral=True)
			return

		if option.value == "first_pick":
			await db.update_draft_state(
				game_id,
				phase="side_pending",
				starting_picker_user_id=coin_winner,
			)
			await self._refresh_sheet_message(game_id)
			allies_captain = int(state["allies_captain_user_id"]) if state["allies_captain_user_id"] else None
			axis_captain = int(state["axis_captain_user_id"]) if state["axis_captain_user_id"] else None
			if allies_captain is None or axis_captain is None:
				await interaction.followup.send("Draft state is incomplete. Restart with `/draft start`.", ephemeral=True)
				return
			other_captain = axis_captain if coin_winner == allies_captain else allies_captain
			await interaction.followup.send(
				"Choice locked: coin winner takes first pick. "
				f"Now <@{other_captain}> must choose side with `/draft side`.",
			)
			return

		await db.update_draft_state(game_id, phase="side_pending")
		await self._refresh_sheet_message(game_id)
		await interaction.followup.send(
			"Choice locked: pick a side now with `/draft side`.",
		)

	@draft.command(name="side", description="Choose Allies or Axis when side choice is pending")
	@app_commands.describe(team="Team side for the captain currently choosing side")
	@app_commands.choices(
		team=[
			app_commands.Choice(name="Allies", value="allies"),
			app_commands.Choice(name="Axis", value="axis"),
		]
	)
	async def draft_side_slash(
		self,
		interaction: discord.Interaction,
		team: app_commands.Choice[str],
	) -> None:
		if not await self._safe_defer(interaction):
			return

		_, game_id, _ = await self._get_draft_game_in_thread(interaction)
		if game_id is None:
			return

		state = await db.get_draft_state(game_id)
		if state is None or str(state["phase"]) != "side_pending":
			await interaction.followup.send("Draft is not waiting for side selection.", ephemeral=True)
			return

		coin_winner = int(state["coin_winner_user_id"]) if state["coin_winner_user_id"] else None
		allies_captain = int(state["allies_captain_user_id"]) if state["allies_captain_user_id"] else None
		axis_captain = int(state["axis_captain_user_id"]) if state["axis_captain_user_id"] else None
		starting_picker = int(state["starting_picker_user_id"]) if state["starting_picker_user_id"] else None
		if coin_winner is None or allies_captain is None or axis_captain is None:
			await interaction.followup.send("Draft state is incomplete. Restart with `/draft start`.", ephemeral=True)
			return

		first_pick_locked = starting_picker is not None and starting_picker == coin_winner
		side_chooser = coin_winner
		if first_pick_locked:
			side_chooser = axis_captain if coin_winner == allies_captain else allies_captain

		if int(interaction.user.id) != side_chooser:
			await interaction.followup.send(f"Only <@{side_chooser}> can pick side right now.", ephemeral=True)
			return

		if team.value == "allies" and side_chooser == axis_captain:
			allies_captain, axis_captain = axis_captain, allies_captain
		if team.value == "axis" and side_chooser == allies_captain:
			allies_captain, axis_captain = axis_captain, allies_captain

		await db.set_draft_team_side(game_id, allies_captain, "allies")
		await db.set_draft_team_side(game_id, axis_captain, "axis")

		if first_pick_locked:
			first_picker = coin_winner
		else:
			first_picker = axis_captain if side_chooser == allies_captain else allies_captain
		await db.update_draft_state(
			game_id,
			phase="drafting",
			allies_captain_user_id=allies_captain,
			axis_captain_user_id=axis_captain,
			starting_picker_user_id=first_picker,
			next_picker_user_id=first_picker,
		)
		await self._refresh_sheet_message(game_id)
		await interaction.followup.send(
			f"Sides locked. First pick goes to <@{first_picker}>. Use `/draft pick`.",
		)

	@draft.command(name="pick", description="Captain picks one player to their side")
	@app_commands.describe(player="Player to draft")
	async def draft_pick_slash(
		self,
		interaction: discord.Interaction,
		player: discord.Member,
	) -> None:
		if not await self._safe_defer(interaction):
			return

		_, game_id, _ = await self._get_draft_game_in_thread(interaction)
		if game_id is None:
			return

		state = await db.get_draft_state(game_id)
		if state is None or str(state["phase"]) != "drafting":
			await interaction.followup.send("Draft is not currently in picking phase.", ephemeral=True)
			return

		next_picker = int(state["next_picker_user_id"]) if state["next_picker_user_id"] else None
		allies_captain = int(state["allies_captain_user_id"]) if state["allies_captain_user_id"] else None
		axis_captain = int(state["axis_captain_user_id"]) if state["axis_captain_user_id"] else None
		if next_picker is None or allies_captain is None or axis_captain is None:
			await interaction.followup.send("Draft state is incomplete. Restart with `/draft start`.", ephemeral=True)
			return

		if int(interaction.user.id) != next_picker:
			await interaction.followup.send(f"It is not your turn. Next picker is <@{next_picker}>.", ephemeral=True)
			return

		picked_reservation = await db.get_draft_reservation(game_id, player.id)
		if picked_reservation is None:
			await interaction.followup.send("That player is not in the draft reservation pool.", ephemeral=True)
			return

		if str(picked_reservation["slot_type"]) == "late_hotjoin":
			await interaction.followup.send("Late Hotjoin players cannot be drafted now.", ephemeral=True)
			return

		if player.id in {allies_captain, axis_captain}:
			await interaction.followup.send("Captains are already assigned; pick another player.", ephemeral=True)
			return

		existing_pick = await db.get_draft_team_assignment(game_id, player.id)
		if existing_pick is not None:
			await interaction.followup.send("That player has already been drafted.", ephemeral=True)
			return

		team_side = "allies" if int(interaction.user.id) == allies_captain else "axis"
		pick_no = (await db.get_draft_pick_count(game_id)) + 1
		await db.add_draft_team_assignment(
			game_id=game_id,
			user_id=player.id,
			user_name=player.display_name,
			team_side=team_side,
			picked_by_user_id=interaction.user.id,
			pick_number=pick_no,
		)

		reservations = await db.list_draft_reservations(game_id)
		eligible_player_ids = {
			int(row["user_id"])
			for row in reservations
			if str(row["slot_type"]) != "late_hotjoin" and int(row["user_id"]) not in {allies_captain, axis_captain}
		}
		assigned_rows = await db.list_draft_team_assignments(game_id)
		assigned_ids = {int(row["user_id"]) for row in assigned_rows}
		remaining_ids = eligible_player_ids - assigned_ids

		if not remaining_ids:
			await db.update_draft_state(game_id, phase="ban_pending", next_picker_user_id=0)
			await self._refresh_sheet_message(game_id)
			await interaction.followup.send(
				f"Player draft complete. {player.mention} joined **{team_side.title()}**. "
				"Captains: use `/draft ban` once each, then `/draft begin_assignments`.",
			)
			return

		next_turn = axis_captain if int(interaction.user.id) == allies_captain else allies_captain
		await db.update_draft_state(game_id, next_picker_user_id=next_turn)
		await self._refresh_sheet_message(game_id)
		await interaction.followup.send(
			f"Pick locked: {player.mention} joined **{team_side.title()}**. Next picker: <@{next_turn}>.",
		)

	@draft.command(name="ban", description="Captain bans one opposing player from one nation tag")
	@app_commands.describe(player="Opposing drafted player to ban", nation="Nation tag/name to ban (major ban includes co-ops)")
	async def draft_ban_slash(
		self,
		interaction: discord.Interaction,
		player: discord.Member,
		nation: str,
	) -> None:
		if not await self._safe_defer(interaction):
			return

		_, game_id, _ = await self._get_draft_game_in_thread(interaction)
		if game_id is None:
			return

		state = await db.get_draft_state(game_id)
		if state is None or str(state["phase"]) != "ban_pending":
			await interaction.followup.send("Draft is not in ban phase.", ephemeral=True)
			return

		allies_captain = int(state["allies_captain_user_id"]) if state["allies_captain_user_id"] else None
		axis_captain = int(state["axis_captain_user_id"]) if state["axis_captain_user_id"] else None
		if allies_captain is None or axis_captain is None:
			await interaction.followup.send("Draft state is incomplete. Restart with `/draft start`.", ephemeral=True)
			return

		caller_id = int(interaction.user.id)
		if caller_id not in {allies_captain, axis_captain}:
			await interaction.followup.send("Only captains can use `/draft ban`.", ephemeral=True)
			return

		existing_ban = await db.get_draft_player_ban_by_captain(game_id, caller_id)
		if existing_ban is not None:
			await interaction.followup.send("You already used your ban for this draft.", ephemeral=True)
			return

		target_assignment = await db.get_draft_team_assignment(game_id, player.id)
		if target_assignment is None:
			await interaction.followup.send("That player is not drafted to a side yet.", ephemeral=True)
			return

		caller_side = "allies" if caller_id == allies_captain else "axis"
		target_side = str(target_assignment["team_side"])
		if target_side == caller_side:
			await interaction.followup.send("You can only ban players from the opposing side.", ephemeral=True)
			return

		resolved_nation = await db.resolve_nation_name(game_id, nation)
		if resolved_nation is None:
			await interaction.followup.send("Nation not found in draft nation pool.", ephemeral=True)
			return

		nation_tag = self._nation_tag(resolved_nation)
		inserted = await db.add_draft_player_ban(game_id, caller_id, player.id, nation_tag)
		if not inserted:
			await interaction.followup.send(
				"Could not save ban. Either you already used your ban or this exact player/nation ban already exists.",
				ephemeral=True,
			)
			return

		await self._refresh_sheet_message(game_id)
		await interaction.followup.send(
			f"Ban locked: {interaction.user.mention} banned {player.mention} from **{nation_tag}** (incl. co-op).",
		)

	@draft_ban_slash.autocomplete("nation")
	async def draft_ban_nation_autocomplete(
		self,
		interaction: discord.Interaction,
		current: str,
	) -> list[app_commands.Choice[str]]:
		return await self._available_nation_autocomplete(interaction, current)

	@draft.command(name="begin_assignments", description="Move from bans to nation assignments")
	async def draft_begin_assignments_slash(self, interaction: discord.Interaction) -> None:
		if not await self._safe_defer(interaction):
			return

		_, game_id, game = await self._get_draft_game_in_thread(interaction)
		if game_id is None or game is None:
			return

		state = await db.get_draft_state(game_id)
		if state is None or str(state["phase"]) != "ban_pending":
			await interaction.followup.send("Draft is not in ban phase.", ephemeral=True)
			return

		allies_captain = int(state["allies_captain_user_id"]) if state["allies_captain_user_id"] else None
		axis_captain = int(state["axis_captain_user_id"]) if state["axis_captain_user_id"] else None
		if allies_captain is None or axis_captain is None:
			await interaction.followup.send("Draft state is incomplete. Restart with `/draft start`.", ephemeral=True)
			return

		is_host = int(game["host_discord_id"]) == int(interaction.user.id)
		has_access = await interaction_user_has_bot_access(interaction)
		is_captain = int(interaction.user.id) in {allies_captain, axis_captain}
		if not (is_host or has_access or is_captain):
			await interaction.followup.send("Only host, bot staff, or captains can start assignments.", ephemeral=True)
			return

		bans = await db.list_draft_player_bans(game_id)
		captains_with_bans = {int(row["captain_user_id"]) for row in bans}
		if allies_captain not in captains_with_bans or axis_captain not in captains_with_bans:
			await interaction.followup.send("Both captains must use `/draft ban` before assignments can begin.", ephemeral=True)
			return

		await db.update_draft_state(game_id, phase="assigning", next_picker_user_id=0)
		await self._refresh_sheet_message(game_id)
		await interaction.followup.send(
			"Assignment phase started. Captains can now use `/draft assign` for their own side.",
		)

	@draft.command(name="assign", description="Assign one player on your side to a nation")
	@app_commands.describe(player="Player on your side", nation="Nation to assign")
	async def draft_assign_slash(
		self,
		interaction: discord.Interaction,
		player: discord.Member,
		nation: str,
	) -> None:
		if not await self._safe_defer(interaction):
			return

		_, game_id, _ = await self._get_draft_game_in_thread(interaction)
		if game_id is None:
			return

		state = await db.get_draft_state(game_id)
		if state is None or str(state["phase"]) != "assigning":
			await interaction.followup.send("Draft is not in assignment phase.", ephemeral=True)
			return

		allies_captain = int(state["allies_captain_user_id"]) if state["allies_captain_user_id"] else None
		axis_captain = int(state["axis_captain_user_id"]) if state["axis_captain_user_id"] else None
		if allies_captain is None or axis_captain is None:
			await interaction.followup.send("Draft state is incomplete. Restart with `/draft start`.", ephemeral=True)
			return

		captain_id = int(interaction.user.id)
		if captain_id not in {allies_captain, axis_captain}:
			await interaction.followup.send("Only captains can assign nations.", ephemeral=True)
			return

		captain_side = "allies" if captain_id == allies_captain else "axis"
		target_assignment = await db.get_draft_team_assignment(game_id, player.id)
		if target_assignment is None:
			await interaction.followup.send("That player is not on a drafted team.", ephemeral=True)
			return

		if str(target_assignment["team_side"]) != captain_side:
			await interaction.followup.send("You can only assign players on your own side.", ephemeral=True)
			return

		resolved_nation = await db.resolve_nation_name(game_id, nation)
		if resolved_nation is None:
			await interaction.followup.send("Nation not found in the draft nation pool.", ephemeral=True)
			return

		nation_row = await db.get_nation_reservation(game_id, resolved_nation)
		if nation_row is None:
			await interaction.followup.send("Nation not found in sheet.", ephemeral=True)
			return

		reserved_by = int(nation_row["reserved_by"]) if nation_row["reserved_by"] is not None else None
		if reserved_by is not None and reserved_by != int(player.id):
			await interaction.followup.send("That nation is already assigned to another player.", ephemeral=True)
			return

		nation_tag = self._nation_tag(resolved_nation)
		bans = await db.list_draft_player_bans(game_id)
		for row in bans:
			if int(row["target_user_id"]) == int(player.id) and str(row["nation_tag"]).upper() == nation_tag:
				await interaction.followup.send(
					f"{player.mention} is banned from **{nation_tag}** (including co-op slots).",
					ephemeral=True,
				)
				return

		current_reserved = await db.get_user_reserved_nations(game_id, player.id)
		if len(current_reserved) > 1:
			await interaction.followup.send("That player has multiple assigned nations. Ask staff to clean the sheet.", ephemeral=True)
			return

		old_nation = str(current_reserved[0]["nation_name"]) if current_reserved else None
		if old_nation is not None and old_nation.lower() == resolved_nation.lower():
			await interaction.followup.send(f"{player.mention} is already assigned to **{resolved_nation}**.", ephemeral=True)
			return

		if old_nation is not None:
			await db.unreserve_nation(game_id, old_nation)

		assigned = await db.reserve_nation(
			game_id=game_id,
			nation_name=resolved_nation,
			user_id=player.id,
			user_name=player.display_name,
		)
		if not assigned:
			if old_nation is not None:
				await db.reserve_nation(game_id, old_nation, player.id, player.display_name)
			await interaction.followup.send("Could not assign nation. Try again.", ephemeral=True)
			return

		team_rows = await db.list_draft_team_assignments(game_id)
		team_user_ids = {int(row["user_id"]) for row in team_rows}
		sheet_rows = await db.list_sheet(game_id)
		assigned_user_ids = {
			int(row["reserved_by"])
			for row in sheet_rows
			if row["reserved_by"] is not None and int(row["reserved_by"]) in team_user_ids
		}
		is_complete = bool(team_user_ids) and team_user_ids.issubset(assigned_user_ids)

		if is_complete:
			await db.update_draft_state(game_id, phase="complete", next_picker_user_id=0)

		await self._refresh_sheet_message(game_id)
		if is_complete:
			await interaction.followup.send(
				f"Assignment locked: {player.mention} -> **{resolved_nation}**. Draft complete.",
			)
			return

		await interaction.followup.send(
			f"Assignment locked: {player.mention} -> **{resolved_nation}**.",
		)

	@draft_assign_slash.autocomplete("nation")
	async def draft_assign_nation_autocomplete(
		self,
		interaction: discord.Interaction,
		current: str,
	) -> list[app_commands.Choice[str]]:
		return await self._available_nation_autocomplete(interaction, current)



	@app_commands.command(name="draft_preferences", description="Set up to 5 preferred nations for no-sheet draft")
	@app_commands.describe(
		top1="First choice",
		top2="Second choice (optional)",
		top3="Third choice (optional)",
		top4="Fourth choice (optional)",
		top5="Fifth choice (optional)",
	)
	async def draft_preferences_slash(
		self,
		interaction: discord.Interaction,
		top1: str,
		top2: str | None = None,
		top3: str | None = None,
		top4: str | None = None,
		top5: str | None = None,
	) -> None:
		if not await self._safe_defer(interaction):
			return

		_, game_id, _ = await self._get_draft_game_in_thread(interaction)
		if game_id is None:
			return

		reservation = await db.get_draft_reservation(game_id, interaction.user.id)
		if reservation is None:
			await interaction.followup.send("Join the draft first with `/draft reserve`.", ephemeral=True)
			return

		raw_choices = [top1, top2, top3, top4, top5]
		choices = [choice.strip() for choice in raw_choices if choice and choice.strip()]
		if not choices:
			await interaction.followup.send("Provide at least one nation preference.", ephemeral=True)
			return

		normalized_seen: set[str] = set()
		for choice in choices:
			normalized = choice.lower()
			if normalized in normalized_seen:
				await interaction.followup.send("Please avoid duplicate nation choices.", ephemeral=True)
				return
			normalized_seen.add(normalized)

		valid_choice_map = {choice.lower(): choice for choice in self._preferences_country_list()}
		canonical_choices: list[str] = []
		for choice in choices:
			canonical = valid_choice_map.get(choice.lower())
			if canonical is None:
				await interaction.followup.send(
					f"`{choice}` is not a valid draft nation preference.",
					ephemeral=True,
				)
				return
			canonical_choices.append(canonical)

		is_late_player = str(reservation["slot_type"]) == "late_hotjoin"
		if is_late_player:
			invalid = [choice for choice in canonical_choices if not self._is_late_allowed_preference(choice)]
			if invalid:
				await interaction.followup.send(
					"Late players can only choose co-op preferences and VICHY.",
					ephemeral=True,
				)
				return

		await db.set_game_preferences(
			game_id=game_id,
			user_id=interaction.user.id,
			user_name=interaction.user.display_name,
			choices=canonical_choices,
		)
		await self._refresh_sheet_message(game_id)

		formatted = [self._format_preference_choice(choice) for choice in canonical_choices]
		await interaction.followup.send(
			"Saved draft preferences: " + ", ".join(formatted),
			ephemeral=True,
		)

	@draft_preferences_slash.autocomplete("top1")
	@draft_preferences_slash.autocomplete("top2")
	@draft_preferences_slash.autocomplete("top3")
	@draft_preferences_slash.autocomplete("top4")
	@draft_preferences_slash.autocomplete("top5")
	async def draft_preferences_country_autocomplete(
		self,
		interaction: discord.Interaction,
		current: str,
	) -> list[app_commands.Choice[str]]:
		return await self._preferences_autocomplete(interaction, current)


	@app_commands.command(name="preferences", description="Deprecated alias for /draft_preferences")
	@app_commands.describe(
		top1="First choice",
		top2="Second choice (optional)",
		top3="Third choice (optional)",
		top4="Fourth choice (optional)",
		top5="Fifth choice (optional)",
	)
	async def preferences_slash(
		self,
		interaction: discord.Interaction,
		top1: str,
		top2: str | None = None,
		top3: str | None = None,
		top4: str | None = None,
		top5: str | None = None,
	) -> None:
		await self.draft_preferences_slash(
			interaction,
			top1,
			top2,
			top3,
			top4,
			top5,
		)

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
