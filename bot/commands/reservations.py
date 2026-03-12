from __future__ import annotations

import random
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
		return command_name in {
			"reserve",
			"unreserve",
			"draft_join",
			"draft_vote",
			"draft_decide",
			"draft_pick",
			"draft_ban",
			"draft_assign",
		}

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
			draft_state = await db.get_draft_state(game_id)
			players = await db.list_draft_players(game_id)
			draft_bans = await db.list_draft_bans(game_id)
			candidate_rows = await db.list_captain_candidates_with_votes(game_id)
			vote_map = {int(row["user_id"]): int(row["vote_count"]) for row in candidate_rows}
			captain_pool = [row for row in players if str(row["side"]) == "captain"]
			unpicked = [row for row in players if str(row["side"]) == "unpicked"]
			allies = [row for row in players if str(row["side"]) == "allies"]
			axis = [row for row in players if str(row["side"]) == "axis"]

			captain_lines: list[str] = []
			if draft_state is not None and draft_state["allies_captain_id"]:
				captain_lines.append(f"- Allies: <@{int(draft_state['allies_captain_id'])}>")
			if draft_state is not None and draft_state["axis_captain_id"]:
				captain_lines.append(f"- Axis: <@{int(draft_state['axis_captain_id'])}>")
			if not captain_lines and captain_pool:
				for row in captain_pool:
					role_text = str(row["role_preference"]).replace("_", " ").title()
					votes = vote_map.get(int(row["user_id"]), 0)
					captain_lines.append(f"- <@{int(row['user_id'])}> | Role: {role_text} | Captain votes: {votes}")
			if not captain_lines:
				captain_lines = ["- none"]

			def _render_side_rows(rows: list, *, side_list_mode: bool = False) -> list[str]:
				if not rows:
					return ["- none"]
				result: list[str] = []
				for row in rows:
					role_text = str(row["role_preference"]).replace("_", " ").title()
					if side_list_mode:
						result.append(f"- <@{int(row['user_id'])}> | Role: {role_text}")
					else:
						captain_opt_in = bool(row["up_for_captain"])
						votes = vote_map.get(int(row["user_id"]), 0)
						captain_vote_text = f" | Captain votes: {votes}" if captain_opt_in else ""
						captain_opt_text = " | Captain candidate" if captain_opt_in else ""
						captain_suffix = " (Captain)" if bool(row["is_captain"]) else ""
						result.append(
							f"- <@{int(row['user_id'])}>{captain_suffix} | Role: {role_text}{captain_opt_text}{captain_vote_text}"
						)
				return result

			if draft_state is None:
				status_line = "Draft status: setup"
				turn_line = "Next pick: not started"
			else:
				status_text = str(draft_state["status"]).replace("_", " ").title()
				status_line = f"Draft status: {status_text}"
				state_status = str(draft_state["status"])
				next_turn = str(draft_state["next_turn"])
				if state_status == "captain_decision" and draft_state["team_decider_id"]:
					turn_line = (
						"Captain decision: "
						f"<@{int(draft_state['team_decider_id'])}> chooses first pick or team side via `/draft_decide`."
					)
				elif state_status == "pending_side_choice" and draft_state["pending_side_choice_captain_id"]:
					turn_line = (
						"Captain decision: "
						f"<@{int(draft_state['pending_side_choice_captain_id'])}> chooses team side via `/draft_decide`."
					)
				elif state_status == "banning" and next_turn in {"allies", "axis"}:
					captain_id = draft_state["allies_captain_id"] if next_turn == "allies" else draft_state["axis_captain_id"]
					if captain_id:
						turn_line = f"Ban turn: <@{int(captain_id)}> uses `/draft_ban`."
					else:
						turn_line = "Ban turn: captain not set"
				elif next_turn == "allies" and draft_state["allies_captain_id"]:
					turn_line = f"Next pick: <@{int(draft_state['allies_captain_id'])}>"
				elif next_turn == "axis" and draft_state["axis_captain_id"]:
					turn_line = f"Next pick: <@{int(draft_state['axis_captain_id'])}>"
				else:
					turn_line = "Next pick: not set"

			ban_lines: list[str] = []
			ban_by_side = {str(row["side"]): row for row in draft_bans}
			for side_name in ("allies", "axis"):
				row = ban_by_side.get(side_name)
				if row is None:
					ban_lines.append(f"- {side_name.title()}: none")
					continue
				ban_lines.append(
					f"- {side_name.title()}: <@{int(row['banned_player_id'])}> banned from **{str(row['banned_nation_tag']).upper()}**"
				)

			lines = [
				f"## Draft Board — {game['title']}",
				"",
				status_line,
				turn_line,
				"",
				"### Captains",
				*captain_lines,
				"",
				"### Not Picked",
				*_render_side_rows(unpicked),
				"",
				"### Allies",
				*_render_side_rows(allies, side_list_mode=True),
				"",
				"### Axis",
				*_render_side_rows(axis, side_list_mode=True),
				"",
				"### Bans",
				*ban_lines,
			]

			sheet_rows = await db.list_sheet(game_id)
			lines.extend(["", "---", ""])
			lines.extend(db.build_sheet_display_lines(str(game["title"]), sheet_rows))
			lines.extend(
				[
					"",
					"Use `/draft_join` to enter pool with role/captain options, `/draft_vote` to vote captains (up to 2), `/draft_start` to begin, `/draft_decide` for captain decision, `/draft_pick` for picks, `/draft_ban` for bans, and `/draft_assign` for nation assignment.",
				]
			)
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
			await db.set_draft_board_message(game_id, message.id)

		if str(game["preset"]) == "no_sheet":
			try:
				await message.pin(reason="Keep draft board visible")
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

		resolved_nation = await db.resolve_nation_name(game_id, nation_name)
		if resolved_nation is None:
			await ctx.send("Nation not found in sheet. Check spelling or create the sheet first.")
			return

		if bool(game["majors_locked"]) and db.is_major_non_coop_nation(resolved_nation):
			role_id = await db.get_major_lock_role(int(game["guild_id"]))
			if role_id and isinstance(ctx.author, discord.Member):
				has_role = any(role.id == role_id for role in ctx.author.roles)
				if not has_role:
					role_obj = ctx.guild.get_role(role_id) if ctx.guild else None
					role_label = role_obj.mention if role_obj else f"role ID {role_id}"
					await ctx.send(f"You need {role_label} to reserve major main slots.")
					return

		success = await db.reserve_nation(
			game_id=game_id,
			nation_name=resolved_nation,
			user_id=ctx.author.id,
			user_name=ctx.author.display_name,
		)
		if success:
			await ctx.send(f"{ctx.author.mention} reserved **{resolved_nation}** for game **{game_id}**.")
			return

		reservation = await db.get_nation_reservation(game_id, resolved_nation)
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
				"This game uses captain draft mode. Use `/draft_join`, `/draft_vote`, `/draft_start`, `/draft_decide`, `/draft_pick`, `/draft_ban`, and `/draft_assign`.",
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
			removed = await db.draft_leave_player(game_id, interaction.user.id)
			if removed:
				await self._refresh_sheet_message(game_id)
				await self._notify_admin_unreserve(
					interaction.guild,
					interaction.user.display_name,
					"draft pool",
					str(game["title"]),
				)
				await interaction.followup.send(
					"You were removed from the unpicked draft pool.",
					ephemeral=True,
				)
			else:
				await interaction.followup.send(
					"You can only leave while unpicked and not a captain.",
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

	@app_commands.command(name="draft_join", description="Join no-sheet draft with role preference and captain opt-in")
	@app_commands.describe(
		role="Your preferred role",
		up_for_captain="Set true if you want to be captain candidate",
	)
	@app_commands.choices(
		role=[
			app_commands.Choice(name="Major", value="major"),
			app_commands.Choice(name="Minor", value="minor"),
			app_commands.Choice(name="Fill", value="fill"),
			app_commands.Choice(name="Late hotjoin", value="late_hotjoin"),
		]
	)
	async def draft_join_slash(
		self,
		interaction: discord.Interaction,
		role: app_commands.Choice[str],
		up_for_captain: bool = False,
	) -> None:
		if not await self._safe_defer(interaction):
			return

		if not isinstance(interaction.channel, discord.Thread):
			await interaction.followup.send("Use `/draft_join` in the game thread.", ephemeral=True)
			return

		game = await db.get_game_by_thread_id(interaction.channel.id)
		if game is None:
			await interaction.followup.send("This thread is not linked to an active game.", ephemeral=True)
			return

		if str(game["preset"]) != "no_sheet":
			await interaction.followup.send("This game does not use captain draft mode.", ephemeral=True)
			return

		state = await db.get_draft_state(int(game["id"]))
		if state is not None and str(state["status"]) != "setup":
			await interaction.followup.send("Draft has already started. Joining is locked.", ephemeral=True)
			return

		await db.draft_join_player(
			int(game["id"]),
			interaction.user.id,
			interaction.user.display_name,
			role_preference=role.value,
			up_for_captain=up_for_captain,
		)
		await self._refresh_sheet_message(int(game["id"]))
		await interaction.followup.send(
			f"You are in the draft pool. Role: **{role.name}**. Captain candidate: **{'Yes' if up_for_captain else 'No'}**.",
			ephemeral=True,
		)

	@app_commands.command(name="draft_vote", description="Vote for a captain candidate")
	@app_commands.describe(candidate="Player to vote for as captain")
	async def draft_vote_slash(self, interaction: discord.Interaction, candidate: discord.Member) -> None:
		if not await self._safe_defer(interaction):
			return

		if not isinstance(interaction.channel, discord.Thread):
			await interaction.followup.send("Use `/draft_vote` in the game thread.", ephemeral=True)
			return

		game = await db.get_game_by_thread_id(interaction.channel.id)
		if game is None:
			await interaction.followup.send("This thread is not linked to an active game.", ephemeral=True)
			return

		if str(game["preset"]) != "no_sheet":
			await interaction.followup.send("This game does not use captain draft mode.", ephemeral=True)
			return

		game_id = int(game["id"])
		state = await db.get_draft_state(game_id)
		if state is not None and str(state["status"]) != "setup":
			await interaction.followup.send("Captain voting is closed after `/draft_start`.", ephemeral=True)
			return

		voter = await db.get_draft_player(game_id, interaction.user.id)
		if voter is None:
			await interaction.followup.send("Join first with `/draft_join` before voting.", ephemeral=True)
			return

		if int(candidate.id) == int(interaction.user.id):
			await interaction.followup.send("You cannot vote for yourself.", ephemeral=True)
			return

		target = await db.get_draft_player(game_id, candidate.id)
		if target is None:
			await interaction.followup.send("That user is not in the draft pool.", ephemeral=True)
			return

		if not bool(target["up_for_captain"]):
			await interaction.followup.send("That player is not up for captain.", ephemeral=True)
			return

		action, vote_count = await db.toggle_captain_vote(
			game_id,
			interaction.user.id,
			interaction.user.display_name,
			candidate.id,
		)
		if action == "ineligible":
			await interaction.followup.send("Could not save vote. Candidate may no longer be eligible.", ephemeral=True)
			return
		if action == "limit":
			await interaction.followup.send(
				"You already used both captain votes. Remove one by running `/draft_vote` on someone you already voted for.",
				ephemeral=True,
			)
			return

		await self._refresh_sheet_message(game_id)
		if action == "removed":
			await interaction.followup.send(
				f"Vote removed for {candidate.mention}. You now have **{vote_count}/2** votes used.",
				ephemeral=True,
			)
		else:
			await interaction.followup.send(
				f"Vote saved for {candidate.mention}. You now have **{vote_count}/2** votes used.",
				ephemeral=True,
			)

	@app_commands.command(name="draft_start", description="Start captain draft using top 2 voted candidates")
	async def draft_start_slash(
		self,
		interaction: discord.Interaction,
	) -> None:
		if not await self._safe_defer(interaction, ephemeral=False):
			return

		if not isinstance(interaction.channel, discord.Thread):
			await interaction.followup.send("Use `/draft_start` in the game thread.", ephemeral=True)
			return

		game = await db.get_game_by_thread_id(interaction.channel.id)
		if game is None:
			await interaction.followup.send("This thread is not linked to an active game.", ephemeral=True)
			return

		if str(game["preset"]) != "no_sheet":
			await interaction.followup.send("This game does not use captain draft mode.", ephemeral=True)
			return

		game_id = int(game["id"])
		top_candidates = await db.get_top_captain_candidates(game_id, limit=2)
		if len(top_candidates) < 2:
			await interaction.followup.send(
				"Need at least 2 captain candidates with votes. Ask players to `/draft_join` with captain opt-in and use `/draft_vote`.",
				ephemeral=True,
			)
			return

		captain_a = top_candidates[0]
		captain_b = top_candidates[1]
		decider = random.choice([captain_a, captain_b])
		other = captain_b if int(decider["user_id"]) == int(captain_a["user_id"]) else captain_a

		await db.initialize_draft_captain_decision(
			game_id=game_id,
			captain_a_id=int(captain_a["user_id"]),
			captain_a_name=str(captain_a["user_name"]),
			captain_b_id=int(captain_b["user_id"]),
			captain_b_name=str(captain_b["user_name"]),
			team_decider_id=int(decider["user_id"]),
			team_decider_name=str(decider["user_name"]),
		)

		await self._refresh_sheet_message(game_id)
		await interaction.followup.send(
			(
				"Captains selected by votes:\n"
				f"- <@{int(captain_a['user_id'])}> ({int(captain_a['vote_count'])} votes)\n"
				f"- <@{int(captain_b['user_id'])}> ({int(captain_b['vote_count'])} votes)\n\n"
				f"Random decision captain: <@{int(decider['user_id'])}>.\n"
				"Use `/draft_decide` to choose either first pick or team side."
			)
		)

	@app_commands.command(name="draft_decide", description="Captain decision: first pick or team side")
	@app_commands.describe(
		decision="Pick first pick or team side",
		side="Side to lead when choosing team side",
	)
	@app_commands.choices(
		decision=[
			app_commands.Choice(name="First pick", value="first_pick"),
			app_commands.Choice(name="Choose team side", value="team_side"),
		],
		side=[
			app_commands.Choice(name="Allies", value="allies"),
			app_commands.Choice(name="Axis", value="axis"),
		],
	)
	async def draft_decide_slash(
		self,
		interaction: discord.Interaction,
		decision: app_commands.Choice[str],
		side: app_commands.Choice[str] | None = None,
	) -> None:
		if not await self._safe_defer(interaction, ephemeral=False):
			return

		if not isinstance(interaction.channel, discord.Thread):
			await interaction.followup.send("Use `/draft_decide` in the game thread.", ephemeral=True)
			return

		game = await db.get_game_by_thread_id(interaction.channel.id)
		if game is None:
			await interaction.followup.send("This thread is not linked to an active game.", ephemeral=True)
			return

		if str(game["preset"]) != "no_sheet":
			await interaction.followup.send("This game does not use captain draft mode.", ephemeral=True)
			return

		game_id = int(game["id"])
		state = await db.get_draft_state(game_id)
		if state is None:
			await interaction.followup.send("Draft has not started yet. Use `/draft_start`.", ephemeral=True)
			return

		status = str(state["status"])
		if status == "captain_decision":
			if not state["team_decider_id"] or int(interaction.user.id) != int(state["team_decider_id"]):
				await interaction.followup.send("Only the random decision captain can do this step.", ephemeral=True)
				return

			captains = [row for row in await db.list_draft_players_by_side(game_id, "captain") if bool(row["is_captain"])]
			if len(captains) != 2:
				await interaction.followup.send("Expected exactly 2 captains in captain pool.", ephemeral=True)
				return

			other_captain = captains[0] if int(captains[1]["user_id"]) == int(interaction.user.id) else captains[1]

			if decision.value == "first_pick":
				await db.set_draft_first_pick_choice(
					game_id,
					first_pick_captain_id=interaction.user.id,
					pending_side_choice_captain_id=int(other_captain["user_id"]),
				)
				await self._refresh_sheet_message(game_id)
				await interaction.followup.send(
					f"You chose **First Pick**. <@{int(other_captain['user_id'])}> now chooses team side with `/draft_decide decision:Choose team side side:<Allies/Axis>`.",
				)
				return

			if side is None:
				await interaction.followup.send("When choosing team side, include `side` (Allies or Axis).", ephemeral=True)
				return

			chosen_side = side.value
			first_pick_captain_id = int(other_captain["user_id"])
			if chosen_side == "allies":
				allies_id, allies_name = interaction.user.id, interaction.user.display_name
				axis_id, axis_name = int(other_captain["user_id"]), str(other_captain["user_name"])
			else:
				axis_id, axis_name = interaction.user.id, interaction.user.display_name
				allies_id, allies_name = int(other_captain["user_id"]), str(other_captain["user_name"])

			await db.finalize_draft_sides(
				game_id=game_id,
				allies_captain_id=allies_id,
				allies_captain_name=allies_name,
				axis_captain_id=axis_id,
				axis_captain_name=axis_name,
				first_pick_captain_id=first_pick_captain_id,
			)
			await self._refresh_sheet_message(game_id)
			await interaction.followup.send(
				f"Sides set. Draft picks start now. First pick: <@{first_pick_captain_id}>.",
			)
			return

		if status == "pending_side_choice":
			if not state["pending_side_choice_captain_id"] or int(interaction.user.id) != int(state["pending_side_choice_captain_id"]):
				await interaction.followup.send("Only the pending captain can choose team side now.", ephemeral=True)
				return

			if decision.value != "team_side" or side is None:
				await interaction.followup.send("You must choose team side now with `decision:Choose team side` and a side.", ephemeral=True)
				return

			captains = [row for row in await db.list_draft_players_by_side(game_id, "captain") if bool(row["is_captain"])]
			if len(captains) != 2 or not state["first_pick_captain_id"]:
				await interaction.followup.send("Draft state is invalid. Restart with `/draft_start`.", ephemeral=True)
				return

			first_pick_captain_id = int(state["first_pick_captain_id"])
			first_pick_row = captains[0] if int(captains[0]["user_id"]) == first_pick_captain_id else captains[1]

			chosen_side = side.value
			if chosen_side == "allies":
				allies_id, allies_name = interaction.user.id, interaction.user.display_name
				axis_id, axis_name = int(first_pick_row["user_id"]), str(first_pick_row["user_name"])
			else:
				axis_id, axis_name = interaction.user.id, interaction.user.display_name
				allies_id, allies_name = int(first_pick_row["user_id"]), str(first_pick_row["user_name"])

			await db.finalize_draft_sides(
				game_id=game_id,
				allies_captain_id=allies_id,
				allies_captain_name=allies_name,
				axis_captain_id=axis_id,
				axis_captain_name=axis_name,
				first_pick_captain_id=first_pick_captain_id,
			)
			await self._refresh_sheet_message(game_id)
			await interaction.followup.send(
				f"Sides set. Draft picks start now. First pick: <@{first_pick_captain_id}>.",
			)
			return

		await interaction.followup.send("Draft is not in captain decision stage.", ephemeral=True)

	@app_commands.command(name="draft_pick", description="Captain picks one player")
	@app_commands.describe(player="Player to pick")
	async def draft_pick_slash(self, interaction: discord.Interaction, player: discord.Member) -> None:
		if not await self._safe_defer(interaction, ephemeral=False):
			return

		if not isinstance(interaction.channel, discord.Thread):
			await interaction.followup.send("Use `/draft_pick` in the game thread.", ephemeral=True)
			return

		game = await db.get_game_by_thread_id(interaction.channel.id)
		if game is None:
			await interaction.followup.send("This thread is not linked to an active game.", ephemeral=True)
			return

		if str(game["preset"]) != "no_sheet":
			await interaction.followup.send("This game does not use captain draft mode.", ephemeral=True)
			return

		game_id = int(game["id"])
		state = await db.get_draft_state(game_id)
		if state is None or str(state["status"]) != "picking":
			await interaction.followup.send("Draft is not currently in picking stage.", ephemeral=True)
			return

		acting_side: str | None = None
		if int(interaction.user.id) == int(state["allies_captain_id"]):
			acting_side = "allies"
		elif int(interaction.user.id) == int(state["axis_captain_id"]):
			acting_side = "axis"

		if acting_side is None:
			await interaction.followup.send("Only captains can pick players.", ephemeral=True)
			return

		next_turn = str(state["next_turn"])
		if next_turn != acting_side:
			await interaction.followup.send("It is not your turn to pick.", ephemeral=True)
			return

		target = await db.get_draft_player(game_id, player.id)
		if target is None:
			await interaction.followup.send("That user is not in the draft pool. Ask them to use `/draft_join`.", ephemeral=True)
			return

		if bool(target["is_captain"]):
			await interaction.followup.send("Captains cannot be picked.", ephemeral=True)
			return

		if str(target["side"]) != "unpicked":
			await interaction.followup.send("That player has already been picked.", ephemeral=True)
			return

		moved = await db.draft_pick_player(game_id, player.id, acting_side)
		if not moved:
			await interaction.followup.send("Could not apply pick. Try again.", ephemeral=True)
			return

		remaining = await db.count_unpicked_draft_players(game_id)
		if remaining == 0:
			next_ban_side = "allies"
			if state["first_pick_captain_id"]:
				first_pick_captain_id = int(state["first_pick_captain_id"])
				if state["axis_captain_id"] and first_pick_captain_id == int(state["axis_captain_id"]):
					next_ban_side = "axis"
			await db.set_draft_status(game_id, "banning")
			await db.set_draft_next_turn(game_id, next_ban_side)
		else:
			await db.set_draft_next_turn(game_id, "axis" if acting_side == "allies" else "allies")

		await self._refresh_sheet_message(game_id)
		await interaction.followup.send(
			f"{interaction.user.mention} picked {player.mention} for **{acting_side.title()}**.",
		)

	@app_commands.command(name="draft_ban", description="Captain bans one opposing player from one nation tag")
	@app_commands.describe(
		player="Opposing side player to ban from a nation",
		nation="Nation (tag or name), e.g. GER or Germany",
	)
	async def draft_ban_slash(
		self,
		interaction: discord.Interaction,
		player: discord.Member,
		nation: str,
	) -> None:
		if not await self._safe_defer(interaction):
			return

		if not isinstance(interaction.channel, discord.Thread):
			await interaction.followup.send("Use `/draft_ban` in the game thread.", ephemeral=True)
			return

		game = await db.get_game_by_thread_id(interaction.channel.id)
		if game is None:
			await interaction.followup.send("This thread is not linked to an active game.", ephemeral=True)
			return

		if str(game["preset"]) != "no_sheet":
			await interaction.followup.send("This game does not use captain draft mode.", ephemeral=True)
			return

		game_id = int(game["id"])
		state = await db.get_draft_state(game_id)
		if state is None or str(state["status"]) != "banning":
			await interaction.followup.send("Draft is not currently in banning stage.", ephemeral=True)
			return

		captain_side: str | None = None
		if state["allies_captain_id"] and int(interaction.user.id) == int(state["allies_captain_id"]):
			captain_side = "allies"
		elif state["axis_captain_id"] and int(interaction.user.id) == int(state["axis_captain_id"]):
			captain_side = "axis"

		if captain_side is None:
			await interaction.followup.send("Only captains can issue bans.", ephemeral=True)
			return

		next_turn = str(state["next_turn"])
		if next_turn != captain_side:
			await interaction.followup.send("It is not your turn to ban.", ephemeral=True)
			return

		existing_ban = await db.get_draft_ban_for_side(game_id, captain_side)
		if existing_ban is not None:
			await interaction.followup.send("Your side has already used its ban.", ephemeral=True)
			return

		target = await db.get_draft_player(game_id, player.id)
		if target is None:
			await interaction.followup.send("That user is not in the draft pool.", ephemeral=True)
			return

		target_side = str(target["side"])
		if target_side == captain_side:
			await interaction.followup.send("You must ban a player from the opposing side.", ephemeral=True)
			return

		if target_side not in {"allies", "axis"}:
			await interaction.followup.send("You can only ban players that have already been drafted to a side.", ephemeral=True)
			return

		resolved_nation = await db.resolve_nation_name(game_id, nation)
		if resolved_nation is None:
			await interaction.followup.send("Nation not found in this game sheet.", ephemeral=True)
			return

		nation_tag = db.nation_tag_from_name(resolved_nation)
		created = await db.set_draft_ban(
			game_id=game_id,
			side=captain_side,
			banned_player_id=player.id,
			banned_player_name=player.display_name,
			banned_nation_tag=nation_tag,
		)
		if not created:
			await interaction.followup.send("Could not apply ban. Try again.", ephemeral=True)
			return

		other_side = "axis" if captain_side == "allies" else "allies"
		other_done = await db.get_draft_ban_for_side(game_id, other_side)
		if other_done is None:
			await db.set_draft_next_turn(game_id, other_side)
		else:
			await db.set_draft_status(game_id, "assigning")
			await db.set_draft_next_turn(game_id, "none")

		await self._refresh_sheet_message(game_id)
		await interaction.followup.send(
			f"{interaction.user.mention} banned {player.mention} from **{nation_tag}** (main and co-op).",
		)

	@app_commands.command(name="draft_assign", description="Captain assigns one of their players to a nation")
	@app_commands.describe(player="Player on your side", nation="Nation to assign")
	async def draft_assign_slash(
		self,
		interaction: discord.Interaction,
		player: discord.Member,
		nation: str,
	) -> None:
		if not await self._safe_defer(interaction):
			return

		if not isinstance(interaction.channel, discord.Thread):
			await interaction.followup.send("Use `/draft_assign` in the game thread.", ephemeral=True)
			return

		game = await db.get_game_by_thread_id(interaction.channel.id)
		if game is None:
			await interaction.followup.send("This thread is not linked to an active game.", ephemeral=True)
			return

		if str(game["preset"]) != "no_sheet":
			await interaction.followup.send("This game does not use captain draft mode.", ephemeral=True)
			return

		game_id = int(game["id"])
		state = await db.get_draft_state(game_id)
		if state is None:
			await interaction.followup.send("Draft has not been started yet.", ephemeral=True)
			return
		if str(state["status"]) != "assigning":
			await interaction.followup.send("Draft assignments are locked until picks and bans are complete.", ephemeral=True)
			return

		captain_side: str | None = None
		if int(interaction.user.id) == int(state["allies_captain_id"]):
			captain_side = "allies"
		elif int(interaction.user.id) == int(state["axis_captain_id"]):
			captain_side = "axis"

		if captain_side is None:
			await interaction.followup.send("Only captains can assign nations.", ephemeral=True)
			return

		target = await db.get_draft_player(game_id, player.id)
		if target is None or str(target["side"]) != captain_side:
			await interaction.followup.send("You can only assign players that are on your side.", ephemeral=True)
			return

		resolved_nation = await db.resolve_nation_name(game_id, nation)
		coop_pick = re.match(r"^([A-Za-z]+)\s*(?:\(\s*)?co\s*-?\s*op(?:\s*\))?$", nation.strip(), flags=re.IGNORECASE)
		if coop_pick:
			major_tag = coop_pick.group(1).upper()
			resolved_nation = await db.get_first_available_coop_slot(game_id, major_tag)

		if resolved_nation is None:
			available = await db.list_available_nations(game_id)
			preview = "\n".join(f"- {nation_name}" for nation_name in available[:20]) or "- none"
			await interaction.followup.send(
				"Nation not found or not available. Available nations right now:\n" + preview,
				ephemeral=True,
			)
			return

		if await db.is_player_banned_from_nation(game_id, player.id, resolved_nation):
			tag = db.nation_tag_from_name(resolved_nation)
			await interaction.followup.send(
				f"{player.mention} is banned from **{tag}** (main and co-op). Choose a different nation.",
				ephemeral=True,
			)
			return

		if bool(game["majors_locked"]) and db.is_major_non_coop_nation(resolved_nation):
			role_id = await db.get_major_lock_role(int(game["guild_id"]))
			if role_id:
				has_role = any(role.id == role_id for role in player.roles)
				if not has_role:
					role_obj = interaction.guild.get_role(role_id) if interaction.guild else None
					role_label = role_obj.mention if role_obj else f"role ID {role_id}"
					await interaction.followup.send(
						f"{player.mention} needs {role_label} to be assigned major main slots.",
						ephemeral=True,
					)
					return

		reservation = await db.get_nation_reservation(game_id, resolved_nation)
		if reservation is None:
			await interaction.followup.send("Nation not found in reservation sheet.", ephemeral=True)
			return

		if reservation["reserved_by"] is not None and int(reservation["reserved_by"]) != int(player.id):
			await interaction.followup.send(
				f"{resolved_nation} is already assigned to <@{int(reservation['reserved_by'])}>.",
				ephemeral=True,
			)
			return

		current = await db.get_user_reserved_nations(game_id, player.id)
		for row in current:
			await db.admin_clear_reservation(game_id, str(row["nation_name"]))

		updated = await db.admin_set_reservation(game_id, resolved_nation, player.id, player.display_name)
		if not updated:
			await interaction.followup.send("Could not assign nation. Try again.", ephemeral=True)
			return

		await self._refresh_sheet_message(game_id)
		await interaction.followup.send(
			f"Assigned {player.mention} to **{resolved_nation}**.",
			ephemeral=True,
		)

	@draft_assign_slash.autocomplete("nation")
	async def draft_assign_nation_autocomplete(
		self,
		interaction: discord.Interaction,
		current: str,
	) -> list[app_commands.Choice[str]]:
		return await self._available_nation_autocomplete(interaction, current)


async def setup(bot: commands.Bot) -> None:
	await bot.add_cog(ReservationsCog(bot))
