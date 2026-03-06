from __future__ import annotations

from datetime import UTC, datetime
import logging
import re

import discord
from discord import app_commands
from discord.ext import commands

from bot.database import db
from bot.permissions import interaction_user_has_bot_access, member_has_bot_access


class GamesCog(commands.Cog):
	game = app_commands.Group(name="game", description="Create and announce HOI4 games")

	def __init__(self, bot: commands.Bot) -> None:
		self.bot = bot

	async def cog_check(self, ctx: commands.Context) -> bool:
		if ctx.guild is None:
			raise commands.CheckFailure("Use this command in a server.")
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

		if await interaction_user_has_bot_access(interaction):
			return True

		if interaction.response.is_done():
			await interaction.followup.send("Only server administrators or members with the bot access role can use this bot.", ephemeral=True)
		else:
			await interaction.response.send_message("Only server administrators or members with the bot access role can use this bot.", ephemeral=True)
		return False

	def _parse_datetime(self, value: str) -> datetime | None:
		"""Parse user input into a timezone-aware datetime.

		We default to UTC when timezone is omitted so scheduling is predictable
		across different user machines/regions.
		"""
		try:
			dt = datetime.fromisoformat(value.replace(" ", "T"))
		except ValueError:
			return None

		if dt.tzinfo is None:
			dt = dt.replace(tzinfo=UTC)

		return dt

	def _parse_announce_datetime(self, date_value: str, time_value: str) -> datetime | None:
		"""Parse announce date/time using strict DD-MM-YYYY + HH:MM."""
		try:
			dt = datetime.strptime(f"{date_value} {time_value}", "%d-%m-%Y %H:%M")
			dt = dt.replace(tzinfo=UTC)
			return dt
		except ValueError:
			return None

	def _discord_timestamp(self, value: datetime) -> str:
		unix_ts = int(value.timestamp())
		return f"<t:{unix_ts}:F> (<t:{unix_ts}:R>)"

	def _display_date(self, value: datetime) -> str:
		return value.strftime("%d-%m-%Y")

	def _format_user_mention(self, user_id: int | None, fallback_name: str) -> str:
		return f"<@{user_id}>" if user_id else fallback_name

	def _build_thread_name(self, title: str) -> str:
		# Discord thread names max at 100 chars.
		base = re.sub(r"\s+", " ", title).strip() or "Game"
		name = f"{base} - reservations"
		return name[:100]

	def _suppress_link_embeds(self, text: str) -> str:
		"""Wrap raw URLs in angle brackets so Discord does not unfurl embeds."""
		return re.sub(r"(?<!<)(https?://[^\s>]+)", r"<\1>", text)

	def _expand_escaped_newlines(self, text: str) -> str:
		"""Convert literal \\n sequences from slash input into actual newlines."""
		return text.replace("\\n", "\n").strip()

	def _build_thread_announcement_text(
		self,
		*,
		title: str,
		host_id: int,
		manager_id: int | None,
		manager_name: str,
		scheduled_at: datetime,
		mods: str,
		description: str,
		preset: str,
	) -> str:
		mods_display = self._suppress_link_embeds(mods)
		description_display = self._expand_escaped_newlines(description)
		manager_value = f"<@{manager_id}>" if manager_id else manager_name
		lines = [
			f"__**{title}**__",
			self._discord_timestamp(scheduled_at),
			f"**Host:** <@{host_id}>",
			f"**Manager:** {manager_value}",
			f"**Preset:** {preset.replace('_', ' ').title()}",
			f"**Mods:** {mods_display}",
			"**Info:**",
			description_display or "-",
		]
		if preset != "no_sheet":
			lines.append("**Reserve:** Use `/reserve` and `/unreserve` in this thread.")
		else:
			lines.append("**Draft:** Use `/draft_join`, `/draft_vote`, `/draft_start`, `/draft_decide`, `/draft_pick`, and `/draft_assign` in this thread.")
		return "\n".join(lines)

	def _build_announcement_message_content(
		self,
		*,
		title: str,
		host_id: int,
		manager_id: int | None,
		manager_name: str,
		scheduled_at: datetime,
		mods: str,
		description: str,
		preset: str,
		thread_mention: str | None,
	) -> str:
		announcement_text = self._build_thread_announcement_text(
			title=title,
			host_id=host_id,
			manager_id=manager_id,
			manager_name=manager_name,
			scheduled_at=scheduled_at,
			mods=mods,
			description=description,
			preset=preset,
		)
		if thread_mention:
			return f"@everyone\n{announcement_text}\n\n**Info & Reserve:** {thread_mention}"
		return f"@everyone\n{announcement_text}"

	async def _refresh_announcement_message_if_exists(
		self,
		guild: discord.Guild,
		game_id: int,
	) -> None:
		game = await db.get_game(game_id)
		if game is None:
			return

		announce_channel_id = game["announce_channel_id"]
		announce_message_id = game["announce_message_id"]
		if not announce_channel_id or not announce_message_id:
			return

		channel_obj = guild.get_channel(int(announce_channel_id))
		if not isinstance(channel_obj, discord.TextChannel):
			return

		try:
			message = await channel_obj.fetch_message(int(announce_message_id))
		except (discord.NotFound, discord.Forbidden, discord.HTTPException):
			return

		thread_id = game["reservation_thread_id"]
		thread_mention = f"<#{int(thread_id)}>" if thread_id else None
		content = self._build_announcement_message_content(
			title=str(game["title"]),
			host_id=int(game["host_discord_id"]),
			manager_id=int(game["manager_discord_id"]) if game["manager_discord_id"] else None,
			manager_name=str(game["manager_name"] or game["host_name"]),
			scheduled_at=game["scheduled_at"],
			mods=str(game["mods"]),
			description=str(game["description"]),
			preset=str(game["preset"]),
			thread_mention=thread_mention,
		)

		await message.edit(
			content=content,
			allowed_mentions=discord.AllowedMentions.none(),
		)

	async def _safe_defer(self, interaction: discord.Interaction) -> bool:
		if interaction.response.is_done():
			return True
		try:
			await interaction.response.defer(ephemeral=True, thinking=True)
			return True
		except (discord.NotFound, discord.HTTPException):
			return False

	async def _delete_thread_if_exists(
		self,
		guild: discord.Guild,
		thread_id: int | None,
		announce_channel_id: int | None,
		title: str,
	) -> None:
		expected_name = self._build_thread_name(title)

		async def _try_delete_thread_obj(thread_obj: discord.Thread) -> bool:
			try:
				await thread_obj.delete()
				return True
			except discord.Forbidden:
				try:
					await thread_obj.edit(archived=True, locked=True)
				except (discord.Forbidden, discord.HTTPException):
					pass
				return False

		# 1) Preferred path: direct known thread id.
		if thread_id:
			thread_obj = guild.get_thread(int(thread_id)) or guild.get_channel(int(thread_id))
			if thread_obj is None:
				try:
					thread_obj = await guild.fetch_channel(int(thread_id))
				except (discord.NotFound, discord.Forbidden, discord.HTTPException):
					thread_obj = None

			if isinstance(thread_obj, discord.Thread):
				if await _try_delete_thread_obj(thread_obj):
					return

		# 2) Fallback path: locate thread by expected name under announcement channel.
		if not announce_channel_id:
			return
		parent = guild.get_channel(int(announce_channel_id))
		if not isinstance(parent, discord.TextChannel):
			return

		# Check globally active threads first (covers cache misses on parent.threads).
		try:
			for active_thread in await guild.active_threads():
				if active_thread.name == expected_name and active_thread.parent_id == parent.id:
					if await _try_delete_thread_obj(active_thread):
						return
		except (discord.Forbidden, discord.HTTPException):
			pass

		for active_thread in parent.threads:
			if active_thread.name == expected_name:
				if await _try_delete_thread_obj(active_thread):
					return

		try:
			async for archived_thread in parent.archived_threads(limit=100):
				if archived_thread.name == expected_name:
					await _try_delete_thread_obj(archived_thread)
					return
			async for archived_private in parent.archived_threads(limit=100, private=True):
				if archived_private.name == expected_name:
					await _try_delete_thread_obj(archived_private)
					return
		except (discord.Forbidden, discord.HTTPException):
			pass

		# Final cleanup: remove the parent-channel system message that announces thread creation.
		# (This needs Manage Messages permission in the parent channel.)
		try:
			async for msg in parent.history(limit=150):
				thread_ref = getattr(msg, "thread", None)
				if thread_ref and thread_id and int(thread_ref.id) == int(thread_id):
					await msg.delete()
					break
				if msg.type == discord.MessageType.thread_created and expected_name.lower() in (msg.content or "").lower():
					await msg.delete()
					break
		except (discord.Forbidden, discord.HTTPException):
			pass

	async def _refresh_sheet_message_if_exists(self, game_id: int) -> None:
		game = await db.get_game(game_id)
		if game is None:
			return

		thread_id = game["reservation_thread_id"]
		sheet_message_id = game["reservation_sheet_message_id"]
		if not thread_id or not sheet_message_id:
			return

		thread = self.bot.get_channel(int(thread_id))
		if not isinstance(thread, discord.Thread):
			return

		try:
			message = await thread.fetch_message(int(sheet_message_id))
		except (discord.NotFound, discord.Forbidden):
			return

		rows = await db.list_sheet(game_id)
		lines = db.build_sheet_display_lines(str(game["title"]), rows)
		await message.edit(content="\n".join(lines))

	async def game_autocomplete(
		self,
		interaction: discord.Interaction,
		current: str,
	) -> list[app_commands.Choice[str]]:
		if interaction.guild is None:
			return []

		try:
			rows = await db.list_guild_games(interaction.guild.id, limit=25)
		except Exception:
			logging.exception("game_autocomplete failed for guild_id=%s", interaction.guild.id)
			return []

		choices: list[app_commands.Choice[str]] = []
		query = current.lower().strip()
		for row in rows:
			label = f"{row['title']} (#{row['id']})"
			if not query or query in label.lower():
				choices.append(app_commands.Choice(name=label[:100], value=str(row["id"])))
		return choices[:25]

	@commands.command(name="game_create")
	async def game_create(self, ctx: commands.Context, *, payload: str) -> None:
		"""Create a game.

		Format:
		!game_create 2026-03-10T18:00 | Aggelus | Vanilla + Local Mods | Sunday Lobby
		
		Only first three parts are required. The title is optional.
		"""
		parts = [part.strip() for part in payload.split("|")]
		if len(parts) < 3:
			await ctx.send(
				"Usage: !game_create <DD-MM-YYYY HH:MM> | <host name> | <mods> | <optional title>"
			)
			return

		raw_date_time = parts[0].replace("T", " ")
		date_time_parts = raw_date_time.split()
		if len(date_time_parts) != 2:
			await ctx.send("Invalid date/time. Use DD-MM-YYYY HH:MM")
			return

		scheduled_at = self._parse_announce_datetime(date_time_parts[0], date_time_parts[1])
		if scheduled_at is None:
			await ctx.send("Invalid date/time. Use DD-MM-YYYY HH:MM")
			return

		host_name = parts[1]
		mods = parts[2]
		title = parts[3] if len(parts) >= 4 and parts[3] else "HOI4 Multiplayer Lobby"

		game_id = await db.create_game(
			guild_id=ctx.guild.id if ctx.guild else 0,
			title=title,
			host_discord_id=ctx.author.id,
			host_name=host_name,
			manager_discord_id=ctx.author.id,
			manager_name=host_name,
			scheduled_at=scheduled_at,
			mods=mods,
		)
		await ctx.send(
			f"Game created with ID **{game_id}**. Next: run `!sheet_create {game_id}` to create an empty reservation sheet."
		)

	@commands.command(name="game_list")
	async def game_list(self, ctx: commands.Context) -> None:
		"""List upcoming games."""
		if ctx.guild is None:
			await ctx.send("This command can only be used in a server.")
			return

		rows = await db.list_guild_games(guild_id=ctx.guild.id, limit=10)
		if not rows:
			await ctx.send("No games found yet.")
			return

		lines = ["Upcoming games:"]
		for row in rows:
			when = self._discord_timestamp(row["scheduled_at"])
			lines.append(
				f"- ID {row['id']}: {row['title']} | Host: {row['host_name']} | Manager: {row['manager_name']} | {when}"
			)

		await ctx.send("\n".join(lines))

	@commands.command(name="game_announce")
	async def game_announce(self, ctx: commands.Context, game_id: int) -> None:
		"""Post a clean game announcement embed for one game ID."""
		game = await db.get_game(game_id)
		if game is None:
			await ctx.send(f"Game with ID {game_id} was not found.")
			return

		embed = discord.Embed(
			title=game["title"],
			description="New HOI4 game announced!",
			color=discord.Color.blurple(),
		)
		embed.add_field(name="Game ID", value=str(game["id"]), inline=True)
		embed.add_field(
			name="Host",
			value=self._format_user_mention(game["host_discord_id"], game["host_name"]),
			inline=True,
		)
		embed.add_field(
			name="Manager",
			value=self._format_user_mention(game["manager_discord_id"], game["manager_name"] or game["host_name"]),
			inline=True,
		)
		embed.add_field(
			name="Date",
			value=self._discord_timestamp(game["scheduled_at"]),
			inline=False,
		)
		embed.add_field(name="Mods", value=game["mods"] or "Vanilla", inline=False)
		if game["description"]:
			embed.add_field(name="Description", value=game["description"], inline=False)
		if game["notes"]:
			embed.add_field(name="Notes", value=game["notes"], inline=False)

		# Mentioning @everyone is optional; this keeps announcement clean by default.
		await ctx.send(embed=embed)

	@game.command(name="list", description="List upcoming games for this server")
	async def game_list_slash(self, interaction: discord.Interaction) -> None:
		if interaction.guild is None:
			await interaction.response.send_message("Use this in a server.", ephemeral=True)
			return

		rows = await db.list_guild_games(guild_id=interaction.guild.id, limit=10)
		if not rows:
			await interaction.response.send_message("No games found yet.", ephemeral=True)
			return

		lines = ["Upcoming games:"]
		for row in rows:
			lines.append(
				f"- ID {row['id']}: {row['title']} | Host: {row['host_name']} | Manager: {row['manager_name']} | {self._discord_timestamp(row['scheduled_at'])}"
			)

		await interaction.response.send_message("\n".join(lines))

	@game.command(name="announce", description="Create and post a game announcement")
	@app_commands.describe(
		title="Game title, e.g. Hoi4 Noobs on Majors",
		game_date="Date in format DD-MM-YYYY",
		game_time="Time in format HH:MM (24h)",
		mods="Steam workshop links or mod names",
		description="Short info for players, e.g. 'Go to thread and use /reserve'",
		preset="Sheet preset",
		host="Optional host; defaults to the person running command",
		manager="Optional game manager; defaults to the person running command",
		usa_coops="USA co-op slots (default 1)",
		uk_coops="UK co-op slots (default 1)",
		ger_coops="GER co-op slots (default 2)",
		ita_coops="ITA co-op slots (default 1)",
		sov_coops="SOV co-op slots (default 3)",
		japan_coops="JAPAN co-op slots (default 2)",
	)
	@app_commands.choices(
		preset=[
			app_commands.Choice(name="normal sheet", value="normal"),
			app_commands.Choice(name="small sheet", value="small"),
			app_commands.Choice(name="noob game sheet", value="noob"),
			app_commands.Choice(name="no sheet (draft preferences)", value="no_sheet"),
		]
	)
	async def game_announce_slash(
		self,
		interaction: discord.Interaction,
		title: str,
		game_date: str,
		game_time: str,
		mods: str,
		description: str,
		preset: str = "normal",
		host: discord.Member | None = None,
		manager: discord.Member | None = None,
		usa_coops: app_commands.Range[int, 0, 4] = db.DEFAULT_MAJOR_COOPS["usa"],
		uk_coops: app_commands.Range[int, 0, 4] = db.DEFAULT_MAJOR_COOPS["uk"],
		ger_coops: app_commands.Range[int, 0, 4] = db.DEFAULT_MAJOR_COOPS["ger"],
		ita_coops: app_commands.Range[int, 0, 4] = db.DEFAULT_MAJOR_COOPS["ita"],
		sov_coops: app_commands.Range[int, 0, 4] = db.DEFAULT_MAJOR_COOPS["sov"],
		japan_coops: app_commands.Range[int, 0, 4] = db.DEFAULT_MAJOR_COOPS["japan"],
	) -> None:
		# This command can do DB writes + message/thread creation and may exceed
		# Discord's ~3 second initial response window, so we ACK immediately.
		if not await self._safe_defer(interaction):
			return

		try:
			if interaction.guild is None:
				await interaction.followup.send("Use this command in a server.", ephemeral=True)
				return

			scheduled_at = self._parse_announce_datetime(game_date, game_time)
			if scheduled_at is None:
				await interaction.followup.send(
					"Invalid date/time. Use date `DD-MM-YYYY` and time `HH:MM`.",
					ephemeral=True,
				)
				return

			host_member = host or (interaction.user if isinstance(interaction.user, discord.Member) else None)
			host_id = host_member.id if host_member else interaction.user.id
			host_name = host_member.display_name if host_member else interaction.user.display_name
			manager_member = manager or host_member
			manager_id = manager_member.id if manager_member else None
			manager_name = manager_member.display_name if manager_member else host_name
			game_id = await db.create_game(
				guild_id=interaction.guild.id,
				title=title,
				host_discord_id=host_id,
				host_name=host_name,
				manager_discord_id=manager_id,
				manager_name=manager_name,
				scheduled_at=scheduled_at,
				preset=preset,
				majors_locked=(preset != "noob"),
				mods=mods,
				description=description,
			)
			await db.create_reservation_sheet(
				game_id,
				coop_overrides={
					"usa": int(usa_coops),
					"uk": int(uk_coops),
					"ger": int(ger_coops),
					"ita": int(ita_coops),
					"sov": int(sov_coops),
					"japan": int(japan_coops),
				},
			)

			announce_channel_id = await db.get_announce_channel(interaction.guild.id)
			announce_channel = None
			if announce_channel_id is not None:
				announce_channel = interaction.guild.get_channel(announce_channel_id)

			if not isinstance(announce_channel, discord.TextChannel):
				announce_channel = interaction.channel if isinstance(interaction.channel, discord.TextChannel) else None

			if announce_channel is None:
				await interaction.followup.send(
					"Could not find an announcement text channel. Ask admin to configure one.",
					ephemeral=True,
				)
				return

			announcement_message: discord.Message | None = None

			try:
				announcement_message = await announce_channel.send(
					content=self._build_announcement_message_content(
						title=title,
						host_id=host_id,
						manager_id=manager_id,
						manager_name=manager_name,
						scheduled_at=scheduled_at,
						mods=mods,
						description=description,
						preset=preset,
						thread_mention=None,
					),
					allowed_mentions=discord.AllowedMentions(
						everyone=True,
						users=True,
						roles=False,
						replied_user=False,
					),
				)

				thread = await announcement_message.create_thread(
					name=self._build_thread_name(title),
				)
				try:
					await thread.edit(slowmode_delay=10)
				except (discord.Forbidden, discord.HTTPException):
					pass

				try:
					await announcement_message.edit(
						content=self._build_announcement_message_content(
							title=title,
							host_id=host_id,
							manager_id=manager_id,
							manager_name=manager_name,
							scheduled_at=scheduled_at,
							mods=mods,
							description=description,
							preset=preset,
							thread_mention=thread.mention,
						),
						allowed_mentions=discord.AllowedMentions.none(),
					)
				except (discord.Forbidden, discord.HTTPException):
					pass

				if preset != "no_sheet":
					rows = await db.list_sheet(game_id)
					sheet_lines = db.build_sheet_display_lines(title, rows)
					sheet_message = await thread.send("\n".join(sheet_lines))
					await db.set_game_reservation_sheet_message(game_id, sheet_message.id)
				else:
					rows = await db.list_sheet(game_id)
					sheet_lines = db.build_sheet_display_lines(title, rows)
					pref_lines = [
						f"## Draft Board — {title}",
						"",
						"Draft status: Setup",
						"Next pick: not started",
						"",
						"### Not Picked",
						"- none",
						"",
						"### Allies",
						"- none",
						"",
						"### Axis",
						"- none",
						"",
						"---",
						"",
						*sheet_lines,
						"",
						"Use `/draft_join` to enter pool with role/captain options, `/draft_vote` to vote captains, `/draft_start` to begin, `/draft_decide` for captain decision, `/draft_pick` for picks, and `/draft_assign` for nation assignment.",
					]
					pref_message = await thread.send("\n".join(pref_lines))
					try:
						await pref_message.pin(reason="Keep draft board visible")
					except (discord.Forbidden, discord.HTTPException):
						pass
					await db.set_game_reservation_sheet_message(game_id, pref_message.id)
			except discord.Forbidden:
				if announcement_message is not None:
					try:
						await announcement_message.delete()
					except (discord.Forbidden, discord.HTTPException):
						pass
				await db.delete_game(game_id)
				await interaction.followup.send(
					"I couldn't create the reservation thread in the announcement channel. Game creation was rolled back.",
					ephemeral=True,
				)
				return

			await db.set_game_announcement_references(
				game_id=game_id,
				announce_channel_id=announce_channel.id,
				announce_message_id=announcement_message.id,
				reservation_thread_id=thread.id,
			)

			if interaction.channel and interaction.channel.id == announce_channel.id:
				await interaction.followup.send(
					f"Game **{game_id}** created. Reservation thread is ready.",
					ephemeral=True,
				)
			else:
				await interaction.followup.send(
					f"Game **{game_id}** created in {announce_channel.mention}. Reservation thread is ready.",
					ephemeral=True,
				)
		except Exception as exc:
			logging.exception("game_announce_slash failed")
			await interaction.followup.send(
				f"Game announce failed: {type(exc).__name__}: {exc}",
				ephemeral=True,
			)
			return

	@game.command(name="close", description="Close a game, log result, and remove active reservation post")
	@app_commands.describe(
		game="Game to close",
		winner="Winning side",
		end_year="In-game year when match ended",
		notable_events="Brief bullet-style summary of what happened",
		save_game="Optional save file",
		map_screenshot="Optional map screenshot",
	)
	@app_commands.choices(
		winner=[
			app_commands.Choice(name="Allies", value="Allies"),
			app_commands.Choice(name="Axis", value="Axis"),
		]
	)
	async def game_close_slash(
		self,
		interaction: discord.Interaction,
		game: str,
		winner: app_commands.Choice[str],
		end_year: int,
		notable_events: str,
		save_game: discord.Attachment | None = None,
		map_screenshot: discord.Attachment | None = None,
	) -> None:
		if not await self._safe_defer(interaction):
			return
		game_id = int(game)

		if interaction.guild is None:
			await interaction.followup.send("Use this command in a server.", ephemeral=True)
			return

		game_row = await db.get_game(game_id)
		if game_row is None:
			await interaction.followup.send(f"Game with ID {game_id} was not found.", ephemeral=True)
			return

		if int(game_row["guild_id"]) != int(interaction.guild.id):
			await interaction.followup.send("This game belongs to a different server.", ephemeral=True)
			return

		is_host = int(game_row["host_discord_id"]) == int(interaction.user.id)
		can_manage_guild = False
		if isinstance(interaction.user, discord.Member):
			can_manage_guild = interaction.user.guild_permissions.manage_guild

		if not is_host and not can_manage_guild:
			await interaction.followup.send(
				"Only the game host or a server manager can close this game.",
				ephemeral=True,
			)
			return

		log_channel_id = await db.get_log_channel(interaction.guild.id)
		log_channel = interaction.guild.get_channel(log_channel_id) if log_channel_id else None
		if not isinstance(log_channel, discord.TextChannel):
			log_channel = interaction.channel if isinstance(interaction.channel, discord.TextChannel) else None

		if log_channel is None:
			await interaction.followup.send(
				"Could not find a log channel. Set one with `/settings log_channel`.",
				ephemeral=True,
			)
			return

		notable_events_display = self._expand_escaped_newlines(notable_events)

		log_lines = [
			f"__**Game Closed: {game_row['title']}**__",
			f"**Winner:** {winner.value}",
			f"**Game ID:** {game_id}",
			f"**Host:** {self._format_user_mention(game_row['host_discord_id'], game_row['host_name'])}",
			(
				"**Manager:** "
				f"{self._format_user_mention(game_row['manager_discord_id'], game_row['manager_name'] or game_row['host_name'])}"
			),
			f"**Game Date:** {self._display_date(game_row['scheduled_at'])}",
			f"**End Year:** {end_year}",
			f"**Closed By:** {interaction.user.mention}",
			"**Things that happened:**",
			notable_events_display or "-",
		]
		base_log_content = "\n".join(log_lines)

		log_with_links_lines = list(log_lines)
		if save_game is not None:
			log_with_links_lines.append(f"**Save Game:** {save_game.url}")
		if map_screenshot is not None:
			log_with_links_lines.append(f"**Map Screenshot:** {map_screenshot.url}")
		log_content_with_links = "\n".join(log_with_links_lines)

		files: list[discord.File] = []
		if save_game is not None:
			files.append(await save_game.to_file())
		if map_screenshot is not None:
			files.append(await map_screenshot.to_file())

		attachments_warning: str | None = None
		if files:
			try:
				await log_channel.send(content=base_log_content, files=files)
			except (discord.Forbidden, discord.HTTPException):
				# Fallback: still log the result with attachment URLs in plain text.
				await log_channel.send(content=log_content_with_links)
				attachments_warning = (
					"Files could not be re-uploaded to the log channel (size/permission limit). "
					"I logged links to the original attachments instead."
				)
		else:
			await log_channel.send(content=log_content_with_links)

		rows = await db.list_sheet(game_id)
		sheet_lines = db.build_sheet_display_lines(str(game_row["title"]), rows)

		reservation_sheet_snapshot = "\n".join(sheet_lines)

		announce_channel_id = game_row["announce_channel_id"]
		announce_message_id = game_row["announce_message_id"]

		# Delete thread first so cleanup doesn't depend on announcement message state.
		await self._delete_thread_if_exists(
			interaction.guild,
			game_row["reservation_thread_id"],
			announce_channel_id,
			str(game_row["title"]),
		)

		if announce_channel_id and announce_message_id:
			channel_obj = interaction.guild.get_channel(int(announce_channel_id))
			if isinstance(channel_obj, discord.TextChannel):
				try:
					message = await channel_obj.fetch_message(int(announce_message_id))
					await message.delete()
				except (discord.NotFound, discord.Forbidden):
					pass

		await db.create_game_result(
			guild_id=int(game_row["guild_id"]),
			game_id=game_id,
			game_date=game_row["scheduled_at"],
			winning_side=winner.value,
			reservation_sheet=reservation_sheet_snapshot,
		)

		await db.delete_game(game_id)
		status_line = f"Game **{game_id}** closed. Winner logged as **{winner.value}** and active post cleaned up."
		if attachments_warning:
			status_line = f"{status_line}\n{attachments_warning}"
		await interaction.followup.send(status_line, ephemeral=True)

	@game.command(name="cancel", description="Cancel a game and remove active reservation post")
	@app_commands.describe(
		game="Game to cancel",
		reason="Optional reason for cancellation",
	)
	async def game_cancel_slash(
		self,
		interaction: discord.Interaction,
		game: str,
		reason: str | None = None,
	) -> None:
		if not await self._safe_defer(interaction):
			return
		game_id = int(game)

		if interaction.guild is None:
			await interaction.followup.send("Use this command in a server.", ephemeral=True)
			return

		game_row = await db.get_game(game_id)
		if game_row is None:
			await interaction.followup.send(f"Game with ID {game_id} was not found.", ephemeral=True)
			return

		if int(game_row["guild_id"]) != int(interaction.guild.id):
			await interaction.followup.send("This game belongs to a different server.", ephemeral=True)
			return

		is_host = int(game_row["host_discord_id"]) == int(interaction.user.id)
		can_manage_guild = False
		if isinstance(interaction.user, discord.Member):
			can_manage_guild = interaction.user.guild_permissions.manage_guild

		if not is_host and not can_manage_guild:
			await interaction.followup.send(
				"Only the game host or a server manager can cancel this game.",
				ephemeral=True,
			)
			return

		log_channel_id = await db.get_log_channel(interaction.guild.id)
		log_channel = interaction.guild.get_channel(log_channel_id) if log_channel_id else None
		if isinstance(log_channel, discord.TextChannel):
			embed = discord.Embed(
				title=f"Game Cancelled: {game_row['title']}",
				description=reason or "No reason provided.",
				color=discord.Color.orange(),
			)
			embed.add_field(name="Game ID", value=str(game_id), inline=True)
			embed.add_field(
				name="Host",
				value=self._format_user_mention(game_row["host_discord_id"], game_row["host_name"]),
				inline=True,
			)
			embed.add_field(
				name="Manager",
				value=self._format_user_mention(game_row["manager_discord_id"], game_row["manager_name"] or game_row["host_name"]),
				inline=True,
			)
			embed.add_field(name="Planned Date", value=self._display_date(game_row["scheduled_at"]), inline=True)
			embed.add_field(name="Cancelled By", value=interaction.user.mention, inline=True)
			await log_channel.send(embed=embed)

		announce_channel_id = game_row["announce_channel_id"]
		announce_message_id = game_row["announce_message_id"]

		# Delete thread first so cleanup doesn't depend on announcement message state.
		await self._delete_thread_if_exists(
			interaction.guild,
			game_row["reservation_thread_id"],
			announce_channel_id,
			str(game_row["title"]),
		)

		if announce_channel_id and announce_message_id:
			channel_obj = interaction.guild.get_channel(int(announce_channel_id))
			if isinstance(channel_obj, discord.TextChannel):
				try:
					message = await channel_obj.fetch_message(int(announce_message_id))
					await message.delete()
				except (discord.NotFound, discord.Forbidden):
					pass

		await db.delete_game(game_id)
		await interaction.followup.send(
			f"Game **{game_id}** cancelled and removed from active reservations.",
			ephemeral=True,
		)

	@game.command(name="edit", description="Admin edit game details, reservations, and sheet countries")
	@app_commands.describe(
		game="Game to edit",
		date="New date DD-MM-YYYY (optional)",
		time="New time HH:MM (optional)",
		reserve_nation="Nation to force-reserve for a player",
		reserve_player="Player to assign to nation",
		unreserve_tag="Player mention or player ID to clear reservation(s)",
		add_country="Add country/nation to sheet",
		remove_country="Remove country/nation from sheet",
	)
	async def game_edit_slash(
		self,
		interaction: discord.Interaction,
		game: str,
		date: str | None = None,
		time: str | None = None,
		reserve_nation: str | None = None,
		reserve_player: discord.Member | None = None,
		unreserve_tag: str | None = None,
		add_country: str | None = None,
		remove_country: str | None = None,
	) -> None:
		if not await self._safe_defer(interaction):
			return
		game_id = int(game)

		if interaction.guild is None:
			await interaction.followup.send("Use this command in a server.", ephemeral=True)
			return

		game_row = await db.get_game(game_id)
		if game_row is None:
			await interaction.followup.send(f"Game with ID {game_id} was not found.", ephemeral=True)
			return

		is_host = int(game_row["host_discord_id"]) == int(interaction.user.id)
		can_manage_guild = False
		if isinstance(interaction.user, discord.Member):
			can_manage_guild = interaction.user.guild_permissions.manage_guild
		if not is_host and not can_manage_guild:
			await interaction.followup.send(
				"Only the game host or a server manager can edit this game.",
				ephemeral=True,
			)
			return

		changes: list[str] = []

		if date or time:
			current_dt = game_row["scheduled_at"]
			date_value = date or current_dt.strftime("%d-%m-%Y")
			time_value = time or current_dt.strftime("%H:%M")
			new_dt = self._parse_announce_datetime(date_value, time_value)
			if new_dt is None:
				await interaction.followup.send(
					"Invalid date/time. Use DD-MM-YYYY and HH:MM.",
					ephemeral=True,
				)
				return
			if await db.update_game_schedule(game_id, new_dt):
				changes.append(f"schedule -> {self._discord_timestamp(new_dt)}")

		if reserve_nation and reserve_player:
			resolved = await db.resolve_nation_name(game_id, reserve_nation)
			if resolved is None:
				await interaction.followup.send("Reserve nation not found in sheet.", ephemeral=True)
				return
			if await db.admin_set_reservation(game_id, resolved, reserve_player.id, reserve_player.display_name):
				changes.append(f"reserved {resolved} for {reserve_player.mention}")

		if unreserve_tag:
			member_id: int | None = None
			mention_match = re.fullmatch(r"<@!?(\d+)>", unreserve_tag.strip())
			if mention_match:
				member_id = int(mention_match.group(1))
			else:
				try:
					member_id = int(unreserve_tag.strip())
				except ValueError:
					member_id = None

			if member_id is None and interaction.guild is not None:
				query_name = unreserve_tag.strip().lstrip("@").lower()
				matched_member = discord.utils.find(
					lambda m: (
						m.display_name.lower() == query_name
						or m.name.lower() == query_name
						or (m.global_name is not None and m.global_name.lower() == query_name)
					),
					interaction.guild.members,
				)
				if matched_member is not None:
					member_id = int(matched_member.id)

			if member_id is None:
				await interaction.followup.send(
					"Unreserve target must be a player mention, player ID, or exact username/display name.",
					ephemeral=True,
				)
				return

			rows = await db.get_user_reserved_nations(game_id, member_id)
			cleared: list[str] = []
			for row in rows:
				nation_name = str(row["nation_name"])
				if await db.admin_clear_reservation(game_id, nation_name):
					cleared.append(nation_name)

			moved_to_unpicked = False
			removed_from_pool = False
			if str(game_row["preset"]) == "no_sheet":
				draft_player = await db.get_draft_player(game_id, member_id)
				if draft_player is not None and not bool(draft_player["is_captain"]):
					if str(draft_player["side"]) != "unpicked":
						moved_to_unpicked = await db.admin_move_draft_player_to_unpicked(game_id, member_id)
					else:
						removed_from_pool = await db.draft_leave_player(game_id, member_id)

			if not cleared and not moved_to_unpicked and not removed_from_pool:
				await interaction.followup.send(
					"No reservations or draft picks found for that player.",
					ephemeral=True,
				)
				return

			if cleared:
				changes.append(f"unreserved {', '.join(cleared)} for <@{member_id}>")
			if moved_to_unpicked:
				changes.append(f"moved <@{member_id}> back to Not Picked")
			if removed_from_pool:
				changes.append(f"removed <@{member_id}> from draft pool")

		if add_country:
			if await db.add_nation_to_sheet(game_id, add_country.strip()):
				changes.append(f"added nation {add_country.strip()}")

		if remove_country:
			resolved_remove = await db.resolve_nation_name(game_id, remove_country)
			if resolved_remove and await db.remove_nation_from_sheet(game_id, resolved_remove):
				changes.append(f"removed nation {resolved_remove}")

		await self._refresh_sheet_message_if_exists(game_id)
		await self._refresh_announcement_message_if_exists(interaction.guild, game_id)

		if not changes:
			await interaction.followup.send("No changes were applied.", ephemeral=True)
			return

		await interaction.followup.send("Updated game:\n- " + "\n- ".join(changes), ephemeral=True)

	@game_close_slash.autocomplete("game")
	@game_cancel_slash.autocomplete("game")
	@game_edit_slash.autocomplete("game")
	async def game_selector_autocomplete(
		self,
		interaction: discord.Interaction,
		current: str,
	) -> list[app_commands.Choice[str]]:
		return await self.game_autocomplete(interaction, current)


async def setup(bot: commands.Bot) -> None:
	await bot.add_cog(GamesCog(bot))
