from __future__ import annotations

from datetime import datetime
import re
from typing import Any

import asyncpg

_pool: asyncpg.Pool | None = None

# Co-op slots are configurable per game for major nations.
DEFAULT_MAJOR_COOPS = {
	"usa": 1,
	"uk": 1,
	"ger": 2,
	"ita": 1,
	"sov": 3,
	"japan": 2,
}

GAME_PRESETS = {"normal", "small", "noob", "no_sheet"}

_MAJOR_NATION_LABELS = {
	"usa": "USA 🇺🇸",
	"uk": "UK 🇬🇧",
	"ger": "GER 🇩🇪",
	"ita": "ITA 🇮🇹",
	"sov": "SOV 🇷🇺",
	"japan": "JAPAN 🇯🇵",
}

_NATION_ALIASES = {
	"usa": "usa",
	"unitedstates": "usa",
	"uk": "uk",
	"england": "uk",
	"britain": "uk",
	"ger": "ger",
	"germany": "ger",
	"ita": "ita",
	"italy": "ita",
	"sov": "sov",
	"ussr": "sov",
	"soviet": "sov",
	"jap": "japan",
	"japan": "japan",
}

_FACTION_ORDER = ["Allies", "Axis", "Comintern", "GEACPS", "Other"]

_FACTION_BY_TAG = {
	"USA": "Allies",
	"UK": "Allies",
	"FRA": "Allies",
	"RAJ": "Allies",
	"CAN": "Allies",
	"SAF": "Allies",
	"AST": "Allies",
	"BRA": "Allies",
	"MEX": "Allies",
	"POL": "Allies",
	"NET": "Allies",
	"GER": "Axis",
	"ITA": "Axis",
	"HUN": "Axis",
	"ROM": "Axis",
	"BUL": "Axis",
	"SPN": "Axis",
	"FIN": "Axis",
	"YUG": "Axis",
	"DEN": "Axis",
	"VICHY": "Axis",
	"SOV": "Comintern",
	"MON": "Comintern",
	"JAPAN": "GEACPS",
	"MAN": "GEACPS",
	"SIA": "GEACPS",
}

_DRAFT_SIDES = {"allies", "axis"}


def build_nation_pool(coop_overrides: dict[str, int] | None = None) -> list[str]:
	"""Build the reservation nation list with configurable co-op slots."""
	coop_values = dict(DEFAULT_MAJOR_COOPS)
	if coop_overrides:
		for key, value in coop_overrides.items():
			if key in coop_values:
				coop_values[key] = max(0, int(value))

	nations = [
		# Allies
		_MAJOR_NATION_LABELS["usa"],
		_MAJOR_NATION_LABELS["uk"],
		"FRA 🇫🇷",
		"POL 🇵🇱",
		"RAJ 🇮🇳",
		"CAN 🇨🇦",
		"SAF 🇿🇦",
		"AST 🇦🇺",
		"BRA 🇧🇷",
		"MEX 🇲🇽",
		"NET 🇳🇱",

		# Axis
		_MAJOR_NATION_LABELS["ger"],
		_MAJOR_NATION_LABELS["ita"],
		"ROM 🇷🇴",
		"HUN 🇭🇺",
		"BUL 🇧🇬",
		"FIN 🇫🇮",
		"SPN 🇪🇸",
		"YUG 🇷🇸",
		"DEN 🇩🇰",
		"VICHY 🇫🇷",

		# Comintern
		_MAJOR_NATION_LABELS["sov"],
		"MON 🇲🇳",

		# GEACPS
		_MAJOR_NATION_LABELS["japan"],
		"MAN 🇨🇳",
		"SIA 🇹🇭",
	]

	for key, nation_label in _MAJOR_NATION_LABELS.items():
		coop_count = coop_values[key]
		insert_index = nations.index(nation_label) + 1
		for i in range(coop_count):
			nations.insert(insert_index + i, f"{nation_label} (Co-op {i + 1})")

	return nations


def build_nation_pool_for_preset(
	preset: str,
	coop_overrides: dict[str, int] | None = None,
) -> list[str]:
	"""Build nation pool according to selected preset."""
	nations = build_nation_pool(coop_overrides)
	if preset == "small":
		# Small sheet trims lower-impact picks requested by users.
		remove_tags = {"POL", "NET", "DEN"}
		filtered: list[str] = []
		for nation in nations:
			tag = nation.split()[0].upper()
			if tag in remove_tags:
				continue
			filtered.append(nation)
		return filtered
	return nations


def is_major_non_coop_nation(nation_name: str) -> bool:
	"""Return True only for the main major slot, not its co-op slots."""
	if "(Co-op" in nation_name:
		return False
	return nation_name in _MAJOR_NATION_LABELS.values()


def _status_affected_rows(status: str) -> int:
	"""Parse asyncpg status text like 'UPDATE 1' or 'INSERT 0 1'."""
	try:
		return int(status.split()[-1])
	except (ValueError, IndexError):
		return 0


def _normalize_nation_text(value: str) -> str:
	"""Normalize text for forgiving nation matching (e.g. GER, Germany, ger)."""
	cleaned = re.sub(r"[^a-z0-9]+", "", value.lower())
	return _NATION_ALIASES.get(cleaned, cleaned)


def _nation_faction(nation_name: str) -> str:
	base = nation_name.split(" (Co-op", 1)[0]
	tag = base.split()[0].upper()
	return _FACTION_BY_TAG.get(tag, "Other")


def build_sheet_display_lines(title: str, rows: list[asyncpg.Record]) -> list[str]:
	"""Build reservation sheet text grouped by faction for Discord messages."""
	buckets: dict[str, list[str]] = {faction: [] for faction in _FACTION_ORDER}

	for row in rows:
		nation = str(row["nation_name"])
		if row["reserved_by"] is None:
			line = f"- {nation}:"
		else:
			line = f"- {nation}: <@{int(row['reserved_by'])}>"

		faction = _nation_faction(nation)
		buckets[faction].append(line)

	lines = [f"## Reservation Sheet — {title}", ""]
	for faction in _FACTION_ORDER:
		entries = buckets[faction]
		if not entries:
			continue
		lines.append(f"### {faction}")
		lines.extend(entries)
		lines.append("")

	if lines and lines[-1] == "":
		lines.pop()

	return lines


async def connect(database_url: str) -> asyncpg.Pool:
	"""Create the shared connection pool once and reuse it.

	A pool is better than one long-lived connection because Discord bots can
	receive multiple commands concurrently.
	"""
	global _pool

	if _pool is None:
		_pool = await asyncpg.create_pool(database_url, min_size=1, max_size=10)
		await init_schema()

	return _pool


async def close() -> None:
	"""Close the shared pool during bot shutdown to free DB resources."""
	global _pool

	if _pool is not None:
		await _pool.close()
		_pool = None


def get_pool() -> asyncpg.Pool:
	"""Return the active pool or raise a clear error if not initialized yet."""
	if _pool is None:
		raise RuntimeError("Database pool is not initialized. Call connect() first.")
	return _pool


async def execute(query: str, *args: Any) -> str:
	"""Run INSERT/UPDATE/DELETE statements and return asyncpg status text."""
	pool = get_pool()
	async with pool.acquire() as conn:
		return await conn.execute(query, *args)


async def fetch(query: str, *args: Any) -> list[asyncpg.Record]:
	"""Fetch multiple rows for SELECT queries."""
	pool = get_pool()
	async with pool.acquire() as conn:
		return await conn.fetch(query, *args)


async def fetchrow(query: str, *args: Any) -> asyncpg.Record | None:
	"""Fetch a single row or None."""
	pool = get_pool()
	async with pool.acquire() as conn:
		return await conn.fetchrow(query, *args)


async def fetchval(query: str, *args: Any) -> Any:
	"""Fetch a single scalar value (first column of first row)."""
	pool = get_pool()
	async with pool.acquire() as conn:
		return await conn.fetchval(query, *args)


async def get_database_time() -> Any:
	"""Example helper used by cogs so SQL stays centralized in this module."""
	return await fetchval("SELECT NOW()")


async def init_schema() -> None:
	"""Create required tables if they do not exist.

	Keeping this in code makes first-time setup friendlier for beginners and
	keeps the bot runnable without a separate migration tool.
	"""
	await execute(
		"""
		CREATE TABLE IF NOT EXISTS games (
			id SERIAL PRIMARY KEY,
			guild_id BIGINT NOT NULL DEFAULT 0,
			title TEXT NOT NULL,
			host_discord_id BIGINT NOT NULL,
			host_name TEXT NOT NULL,
			manager_discord_id BIGINT,
			manager_name TEXT NOT NULL DEFAULT '',
			scheduled_at TIMESTAMPTZ NOT NULL,
			mods TEXT NOT NULL DEFAULT '',
			description TEXT NOT NULL DEFAULT '',
			notes TEXT NOT NULL DEFAULT '',
			announce_channel_id BIGINT,
			announce_message_id BIGINT,
			reservation_thread_id BIGINT,
			reservation_sheet_message_id BIGINT,
			preset TEXT NOT NULL DEFAULT 'normal',
			majors_locked BOOLEAN NOT NULL DEFAULT TRUE,
			created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
		)
		"""
	)

	# Lightweight migrations so older local databases keep working.
	await execute("ALTER TABLE games ADD COLUMN IF NOT EXISTS guild_id BIGINT NOT NULL DEFAULT 0")
	await execute(
		"ALTER TABLE games ADD COLUMN IF NOT EXISTS manager_name TEXT NOT NULL DEFAULT ''"
	)
	await execute("ALTER TABLE games ADD COLUMN IF NOT EXISTS manager_discord_id BIGINT")
	await execute(
		"ALTER TABLE games ADD COLUMN IF NOT EXISTS description TEXT NOT NULL DEFAULT ''"
	)
	await execute("ALTER TABLE games ADD COLUMN IF NOT EXISTS announce_channel_id BIGINT")
	await execute("ALTER TABLE games ADD COLUMN IF NOT EXISTS announce_message_id BIGINT")
	await execute("ALTER TABLE games ADD COLUMN IF NOT EXISTS reservation_thread_id BIGINT")
	await execute("ALTER TABLE games ADD COLUMN IF NOT EXISTS reservation_sheet_message_id BIGINT")
	await execute("ALTER TABLE games ADD COLUMN IF NOT EXISTS preset TEXT NOT NULL DEFAULT 'normal'")
	await execute(
		"ALTER TABLE games ADD COLUMN IF NOT EXISTS majors_locked BOOLEAN NOT NULL DEFAULT TRUE"
	)

	await execute(
		"""
		CREATE TABLE IF NOT EXISTS game_nations (
			id SERIAL PRIMARY KEY,
			game_id INTEGER NOT NULL REFERENCES games(id) ON DELETE CASCADE,
			nation_name TEXT NOT NULL,
			reserved_by BIGINT,
			reserved_by_name TEXT,
			reserved_at TIMESTAMPTZ,
			UNIQUE (game_id, nation_name)
		)
		"""
	)

	await execute(
		"""
		CREATE TABLE IF NOT EXISTS bot_settings (
			guild_id BIGINT NOT NULL,
			setting_key TEXT NOT NULL,
			setting_value TEXT NOT NULL,
			PRIMARY KEY (guild_id, setting_key)
		)
		"""
	)

	await execute(
		"""
		CREATE TABLE IF NOT EXISTS game_preferences (
			id SERIAL PRIMARY KEY,
			game_id INTEGER NOT NULL REFERENCES games(id) ON DELETE CASCADE,
			user_id BIGINT NOT NULL,
			user_name TEXT NOT NULL,
			choices TEXT[] NOT NULL,
			updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
			UNIQUE (game_id, user_id)
		)
		"""
	)

	await execute(
		"""
		CREATE TABLE IF NOT EXISTS game_draft_state (
			game_id INTEGER PRIMARY KEY REFERENCES games(id) ON DELETE CASCADE,
			allies_captain_id BIGINT,
			allies_captain_name TEXT NOT NULL DEFAULT '',
			axis_captain_id BIGINT,
			axis_captain_name TEXT NOT NULL DEFAULT '',
			team_decider_id BIGINT,
			team_decider_name TEXT NOT NULL DEFAULT '',
			pending_side_choice_captain_id BIGINT,
			first_pick_captain_id BIGINT,
			next_turn TEXT NOT NULL DEFAULT 'allies',
			status TEXT NOT NULL DEFAULT 'setup',
			board_message_id BIGINT,
			updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
		)
		"""
	)

	await execute(
		"""
		CREATE TABLE IF NOT EXISTS game_draft_players (
			id SERIAL PRIMARY KEY,
			game_id INTEGER NOT NULL REFERENCES games(id) ON DELETE CASCADE,
			user_id BIGINT NOT NULL,
			user_name TEXT NOT NULL,
			role_preference TEXT NOT NULL DEFAULT 'fill',
			up_for_captain BOOLEAN NOT NULL DEFAULT FALSE,
			side TEXT NOT NULL DEFAULT 'unpicked',
			is_captain BOOLEAN NOT NULL DEFAULT FALSE,
			picked_at TIMESTAMPTZ,
			updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
			UNIQUE (game_id, user_id)
		)
		"""
	)

	await execute(
		"""
		CREATE TABLE IF NOT EXISTS game_draft_votes (
			id SERIAL PRIMARY KEY,
			game_id INTEGER NOT NULL REFERENCES games(id) ON DELETE CASCADE,
			voter_id BIGINT NOT NULL,
			voter_name TEXT NOT NULL,
			candidate_user_id BIGINT NOT NULL,
			created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
			updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
			UNIQUE (game_id, voter_id),
			UNIQUE (game_id, voter_id, candidate_user_id),
			FOREIGN KEY (game_id, candidate_user_id)
				REFERENCES game_draft_players (game_id, user_id)
				ON DELETE CASCADE
		)
		"""
	)

	await execute(
		"ALTER TABLE game_draft_players ADD COLUMN IF NOT EXISTS role_preference TEXT NOT NULL DEFAULT 'fill'"
	)
	await execute(
		"ALTER TABLE game_draft_players ADD COLUMN IF NOT EXISTS up_for_captain BOOLEAN NOT NULL DEFAULT FALSE"
	)
	await execute("ALTER TABLE game_draft_state ADD COLUMN IF NOT EXISTS team_decider_id BIGINT")
	await execute(
		"ALTER TABLE game_draft_state ADD COLUMN IF NOT EXISTS team_decider_name TEXT NOT NULL DEFAULT ''"
	)
	await execute(
		"ALTER TABLE game_draft_state ADD COLUMN IF NOT EXISTS pending_side_choice_captain_id BIGINT"
	)
	await execute(
		"ALTER TABLE game_draft_state ADD COLUMN IF NOT EXISTS first_pick_captain_id BIGINT"
	)

	# v2 votes table: allows up to 2 captain votes per voter.
	await execute(
		"""
		CREATE TABLE IF NOT EXISTS game_draft_votes_v2 (
			id SERIAL PRIMARY KEY,
			game_id INTEGER NOT NULL REFERENCES games(id) ON DELETE CASCADE,
			voter_id BIGINT NOT NULL,
			voter_name TEXT NOT NULL,
			candidate_user_id BIGINT NOT NULL,
			vote_slot SMALLINT NOT NULL CHECK (vote_slot IN (1, 2)),
			created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
			updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
			UNIQUE (game_id, voter_id, vote_slot),
			UNIQUE (game_id, voter_id, candidate_user_id),
			FOREIGN KEY (game_id, candidate_user_id)
				REFERENCES game_draft_players (game_id, user_id)
				ON DELETE CASCADE
		)
		"""
	)
	await execute(
		"""
		INSERT INTO game_draft_votes_v2 (game_id, voter_id, voter_name, candidate_user_id, vote_slot)
		SELECT v.game_id, v.voter_id, v.voter_name, v.candidate_user_id, 1
		FROM game_draft_votes v
		ON CONFLICT (game_id, voter_id, vote_slot) DO NOTHING
		"""
	)

	await execute(
		"""
		CREATE TABLE IF NOT EXISTS game_draft_bans (
			id SERIAL PRIMARY KEY,
			game_id INTEGER NOT NULL REFERENCES games(id) ON DELETE CASCADE,
			side TEXT NOT NULL,
			banned_player_id BIGINT NOT NULL,
			banned_player_name TEXT NOT NULL,
			banned_nation_tag TEXT NOT NULL,
			created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
			updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
			UNIQUE (game_id, side)
		)
		"""
	)

	await execute(
		"""
		CREATE TABLE IF NOT EXISTS game_results (
			id SERIAL PRIMARY KEY,
			guild_id BIGINT NOT NULL,
			game_id INTEGER NOT NULL,
			game_date TIMESTAMPTZ NOT NULL,
			winning_side TEXT NOT NULL,
			reservation_sheet TEXT NOT NULL DEFAULT '',
			closed_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
		)
		"""
	)
	await execute("ALTER TABLE game_results ADD COLUMN IF NOT EXISTS reservation_sheet TEXT NOT NULL DEFAULT ''")
	await execute("ALTER TABLE game_results ADD COLUMN IF NOT EXISTS winning_side TEXT")
	await execute(
		"""
		DO $$
		BEGIN
			IF EXISTS (
				SELECT 1
				FROM information_schema.columns
				WHERE table_name = 'game_results' AND column_name = 'winner'
			) THEN
				UPDATE game_results
				SET winning_side = winner
				WHERE winning_side IS NULL AND winner IS NOT NULL;
			END IF;
		END $$;
		"""
	)
	await execute(
		"""
		DO $$
		BEGIN
			IF EXISTS (
				SELECT 1
				FROM information_schema.columns
				WHERE table_name = 'game_results' AND column_name = 'title'
			) THEN
				ALTER TABLE game_results ALTER COLUMN title DROP NOT NULL;
			END IF;
			IF EXISTS (
				SELECT 1
				FROM information_schema.columns
				WHERE table_name = 'game_results' AND column_name = 'end_year'
			) THEN
				ALTER TABLE game_results ALTER COLUMN end_year DROP NOT NULL;
			END IF;
			IF EXISTS (
				SELECT 1
				FROM information_schema.columns
				WHERE table_name = 'game_results' AND column_name = 'closed_by'
			) THEN
				ALTER TABLE game_results ALTER COLUMN closed_by DROP NOT NULL;
			END IF;
		END $$;
		"""
	)
	await execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_game_results_guild_game ON game_results(guild_id, game_id)")
	await execute("CREATE INDEX IF NOT EXISTS ix_game_results_guild_id ON game_results(guild_id)")


async def create_game(
	guild_id: int,
	title: str,
	host_discord_id: int,
	host_name: str,
	manager_discord_id: int | None,
	manager_name: str,
	scheduled_at: datetime,
	preset: str = "normal",
	majors_locked: bool = True,
	mods: str = "",
	description: str = "",
	notes: str = "",
) -> int:
	"""Create a game lobby and return its numeric ID."""
	game_id = await fetchval(
		"""
		INSERT INTO games (
			guild_id, title, host_discord_id, host_name, manager_discord_id, manager_name, preset, majors_locked, scheduled_at, mods, description, notes
		)
		VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)
		RETURNING id
		""",
		guild_id,
		title,
		host_discord_id,
		host_name,
		manager_discord_id,
		manager_name,
		preset if preset in GAME_PRESETS else "normal",
		majors_locked,
		scheduled_at,
		mods,
		description,
		notes,
	)
	return int(game_id)


async def list_games(limit: int = 10) -> list[asyncpg.Record]:
	"""Return recent games for quick browsing in Discord."""
	return await fetch(
		"""
		SELECT id, title, host_name, manager_name, preset, scheduled_at, mods
		FROM games
		ORDER BY scheduled_at ASC
		LIMIT $1
		""",
		limit,
	)


async def list_guild_games(guild_id: int, limit: int = 10) -> list[asyncpg.Record]:
	"""Return recent games for a single Discord server."""
	return await fetch(
		"""
		SELECT id, title, host_name, manager_name, preset, scheduled_at, mods
		FROM games
		WHERE guild_id = $1
		ORDER BY scheduled_at ASC
		LIMIT $2
		""",
		guild_id,
		limit,
	)


async def get_game(game_id: int) -> asyncpg.Record | None:
	"""Return one game by ID."""
	return await fetchrow(
		"""
		SELECT
			id,
			guild_id,
			title,
			host_discord_id,
			host_name,
			manager_discord_id,
			manager_name,
			scheduled_at,
			mods,
			description,
			notes,
			announce_channel_id,
			announce_message_id,
			reservation_thread_id,
			reservation_sheet_message_id,
			preset,
			majors_locked
		FROM games
		WHERE id = $1
		""",
		game_id,
	)


async def get_game_by_thread_id(thread_id: int) -> asyncpg.Record | None:
	"""Return active game linked to a reservation thread."""
	return await fetchrow(
		"""
		SELECT
			id,
			guild_id,
			title,
			host_discord_id,
			host_name,
			manager_discord_id,
			manager_name,
			scheduled_at,
			mods,
			description,
			notes,
			announce_channel_id,
			announce_message_id,
			reservation_thread_id,
			reservation_sheet_message_id,
			preset,
			majors_locked
		FROM games
		WHERE reservation_thread_id = $1
		""",
		thread_id,
	)


async def set_game_announcement_references(
	game_id: int,
	announce_channel_id: int,
	announce_message_id: int | None,
	reservation_thread_id: int | None,
) -> None:
	"""Store message/thread IDs so we can clean up when a game is closed."""
	await execute(
		"""
		UPDATE games
		SET
			announce_channel_id = $2,
			announce_message_id = $3,
			reservation_thread_id = $4
		WHERE id = $1
		""",
		game_id,
		announce_channel_id,
		announce_message_id,
		reservation_thread_id,
	)


async def set_game_reservation_sheet_message(game_id: int, message_id: int) -> None:
	"""Store reservation sheet message ID for live edits after reserves/unreserves."""
	await execute(
		"""
		UPDATE games
		SET reservation_sheet_message_id = $2
		WHERE id = $1
		""",
		game_id,
		message_id,
	)


async def update_game_schedule(game_id: int, scheduled_at: datetime) -> bool:
	result = await execute(
		"UPDATE games SET scheduled_at = $2 WHERE id = $1",
		game_id,
		scheduled_at,
	)
	return _status_affected_rows(result) == 1


async def delete_game(game_id: int) -> bool:
	"""Delete an active game and its reservation sheet."""
	result = await execute("DELETE FROM games WHERE id = $1", game_id)
	return _status_affected_rows(result) == 1


async def create_game_result(
	guild_id: int,
	game_id: int,
	game_date: datetime,
	winning_side: str,
	reservation_sheet: str,
) -> int:
	"""Store guild-scoped archived result data for closed games."""
	result_id = await fetchval(
		"""
		INSERT INTO game_results (
			guild_id, game_id, game_date, winning_side, reservation_sheet
		)
		VALUES ($1, $2, $3, $4, $5)
		ON CONFLICT (guild_id, game_id)
		DO UPDATE SET
			game_date = EXCLUDED.game_date,
			winning_side = EXCLUDED.winning_side,
			reservation_sheet = EXCLUDED.reservation_sheet,
			closed_at = NOW()
		RETURNING id
		""",
		guild_id,
		game_id,
		game_date,
		winning_side,
		reservation_sheet,
	)
	return int(result_id)


async def get_game_result_for_guild(guild_id: int, game_id: int) -> asyncpg.Record | None:
	"""Return one archived game result, scoped to one guild only."""
	return await fetchrow(
		"""
		SELECT guild_id, game_id, game_date, winning_side, reservation_sheet, closed_at
		FROM game_results
		WHERE guild_id = $1 AND game_id = $2
		""",
		guild_id,
		game_id,
	)


async def list_game_results_for_guild(guild_id: int, limit: int = 10) -> list[asyncpg.Record]:
	"""List archived game results for one guild only."""
	return await fetch(
		"""
		SELECT guild_id, game_id, game_date, winning_side, reservation_sheet, closed_at
		FROM game_results
		WHERE guild_id = $1
		ORDER BY closed_at DESC
		LIMIT $2
		""",
		guild_id,
		limit,
	)


async def set_guild_setting(guild_id: int, key: str, value: str) -> None:
	"""Store one per-guild bot setting value."""
	await execute(
		"""
		INSERT INTO bot_settings (guild_id, setting_key, setting_value)
		VALUES ($1, $2, $3)
		ON CONFLICT (guild_id, setting_key)
		DO UPDATE SET setting_value = EXCLUDED.setting_value
		""",
		guild_id,
		key,
		value,
	)


async def get_guild_setting(guild_id: int, key: str) -> str | None:
	"""Read one per-guild bot setting value."""
	return await fetchval(
		"""
		SELECT setting_value
		FROM bot_settings
		WHERE guild_id = $1 AND setting_key = $2
		""",
		guild_id,
		key,
	)


async def set_announce_channel(guild_id: int, channel_id: int) -> None:
	await set_guild_setting(guild_id, "announce_channel_id", str(channel_id))


async def get_announce_channel(guild_id: int) -> int | None:
	value = await get_guild_setting(guild_id, "announce_channel_id")
	if value is None:
		return None
	try:
		return int(value)
	except ValueError:
		return None


async def set_log_channel(guild_id: int, channel_id: int) -> None:
	await set_guild_setting(guild_id, "log_channel_id", str(channel_id))


async def get_log_channel(guild_id: int) -> int | None:
	value = await get_guild_setting(guild_id, "log_channel_id")
	if value is None:
		return None
	try:
		return int(value)
	except ValueError:
		return None


async def set_major_lock_role(guild_id: int, role_id: int) -> None:
	await set_guild_setting(guild_id, "major_lock_role_id", str(role_id))


async def get_major_lock_role(guild_id: int) -> int | None:
	value = await get_guild_setting(guild_id, "major_lock_role_id")
	if value is None:
		return None
	try:
		return int(value)
	except ValueError:
		return None


async def set_bot_access_role(guild_id: int, role_id: int) -> None:
	await set_guild_setting(guild_id, "bot_access_role_id", str(role_id))


async def get_bot_access_role(guild_id: int) -> int | None:
	value = await get_guild_setting(guild_id, "bot_access_role_id")
	if value is None:
		return None
	try:
		return int(value)
	except ValueError:
		return None


async def set_admin_notify_channel(guild_id: int, channel_id: int) -> None:
	await set_guild_setting(guild_id, "admin_notify_channel_id", str(channel_id))


async def get_admin_notify_channel(guild_id: int) -> int | None:
	value = await get_guild_setting(guild_id, "admin_notify_channel_id")
	if value is None:
		return None
	try:
		return int(value)
	except ValueError:
		return None


async def is_game_host(game_id: int, user_id: int) -> bool:
	"""Check host ownership for host-only commands."""
	host_id = await fetchval("SELECT host_discord_id FROM games WHERE id = $1", game_id)
	return host_id is not None and int(host_id) == int(user_id)


async def create_reservation_sheet(game_id: int, coop_overrides: dict[str, int] | None = None) -> int:
	"""Create empty nation rows for a game and return number of inserted rows."""
	pool = get_pool()
	inserted = 0
	preset = await fetchval("SELECT preset FROM games WHERE id = $1", game_id)
	nations = build_nation_pool_for_preset(str(preset or "normal"), coop_overrides)

	async with pool.acquire() as conn:
		async with conn.transaction():
			for nation in nations:
				result = await conn.execute(
					"""
					INSERT INTO game_nations (game_id, nation_name)
					VALUES ($1, $2)
					ON CONFLICT (game_id, nation_name) DO NOTHING
					""",
					game_id,
					nation,
				)
				if _status_affected_rows(result) == 1:
					inserted += 1

	return inserted


async def add_nation_to_sheet(game_id: int, nation_name: str) -> bool:
	result = await execute(
		"""
		INSERT INTO game_nations (game_id, nation_name)
		VALUES ($1, $2)
		ON CONFLICT (game_id, nation_name) DO NOTHING
		""",
		game_id,
		nation_name,
	)
	return _status_affected_rows(result) == 1


async def remove_nation_from_sheet(game_id: int, nation_name: str) -> bool:
	result = await execute(
		"""
		DELETE FROM game_nations
		WHERE game_id = $1 AND LOWER(nation_name) = LOWER($2)
		""",
		game_id,
		nation_name,
	)
	return _status_affected_rows(result) == 1


async def list_sheet(game_id: int) -> list[asyncpg.Record]:
	"""Return all nations for a game with reservation status."""
	return await fetch(
		"""
		SELECT nation_name, reserved_by, reserved_by_name, reserved_at
		FROM game_nations
		WHERE game_id = $1
		ORDER BY id ASC
		""",
		game_id,
	)


async def list_available_nations(game_id: int) -> list[str]:
	"""Return only currently available nation labels for a game."""
	rows = await fetch(
		"""
		SELECT nation_name
		FROM game_nations
		WHERE game_id = $1 AND reserved_by IS NULL
		ORDER BY id ASC
		""",
		game_id,
	)
	return [str(row["nation_name"]) for row in rows]


async def get_first_available_coop_slot(game_id: int, major_tag: str) -> str | None:
	"""Return first free co-op slot for a major tag (e.g. GER -> GER ... (Co-op 1))."""
	return await fetchval(
		"""
		SELECT nation_name
		FROM game_nations
		WHERE game_id = $1
		  AND reserved_by IS NULL
		  AND nation_name ILIKE $2
		ORDER BY id ASC
		LIMIT 1
		""",
		game_id,
		f"{major_tag} % (Co-op %",
	)


def nation_tag_from_name(nation_name: str) -> str:
	"""Extract canonical nation tag (first token) from nation label."""
	base = nation_name.split(" (Co-op", 1)[0]
	return base.split()[0].upper()


async def resolve_nation_name(game_id: int, user_input: str) -> str | None:
	"""Resolve user nation input to canonical nation label in this game sheet."""
	rows = await fetch(
		"""
		SELECT nation_name
		FROM game_nations
		WHERE game_id = $1
		ORDER BY nation_name ASC
		""",
		game_id,
	)
	if not rows:
		return None

	input_norm = _normalize_nation_text(user_input)
	candidates = [str(r["nation_name"]) for r in rows]

	# 1) Exact case-insensitive match against full label.
	for nation in candidates:
		if nation.lower() == user_input.lower():
			return nation

	# 2) Normalized exact match (handles emojis/punctuation/aliases).
	normalized_map = {nation: _normalize_nation_text(nation) for nation in candidates}
	for nation, nation_norm in normalized_map.items():
		if nation_norm == input_norm:
			return nation

	# 3) Prefix/contains fallback to allow inputs like "ger", "germany", "sov coop 2".
	for nation, nation_norm in normalized_map.items():
		if nation_norm.startswith(input_norm) or input_norm in nation_norm:
			return nation

	return None


async def reserve_nation(game_id: int, nation_name: str, user_id: int, user_name: str) -> bool:
	"""Reserve an available nation. Returns True if reservation succeeded."""
	result = await execute(
		"""
		UPDATE game_nations
		SET reserved_by = $3, reserved_by_name = $4, reserved_at = NOW()
		WHERE game_id = $1
		  AND LOWER(nation_name) = LOWER($2)
		  AND reserved_by IS NULL
		""",
		game_id,
		nation_name,
		user_id,
		user_name,
	)
	return _status_affected_rows(result) == 1


async def get_nation_reservation(game_id: int, nation_name: str) -> asyncpg.Record | None:
	"""Return reservation details for one nation in a game."""
	return await fetchrow(
		"""
		SELECT nation_name, reserved_by, reserved_by_name
		FROM game_nations
		WHERE game_id = $1 AND LOWER(nation_name) = LOWER($2)
		""",
		game_id,
		nation_name,
	)


async def get_user_reserved_nations(game_id: int, user_id: int) -> list[asyncpg.Record]:
	"""Return nations reserved by a specific user in a game."""
	return await fetch(
		"""
		SELECT nation_name, reserved_by, reserved_by_name
		FROM game_nations
		WHERE game_id = $1 AND reserved_by = $2
		ORDER BY id ASC
		""",
		game_id,
		user_id,
	)


async def unreserve_nation(game_id: int, nation_name: str) -> bool:
	"""Clear reservation for a nation. Returns True if row was updated."""
	result = await execute(
		"""
		UPDATE game_nations
		SET reserved_by = NULL, reserved_by_name = NULL, reserved_at = NULL
		WHERE game_id = $1
		  AND LOWER(nation_name) = LOWER($2)
		  AND reserved_by IS NOT NULL
		""",
		game_id,
		nation_name,
	)
	return _status_affected_rows(result) == 1


async def admin_set_reservation(game_id: int, nation_name: str, user_id: int, user_name: str) -> bool:
	"""Force reservation assignment for admin edit flow."""
	result = await execute(
		"""
		UPDATE game_nations
		SET reserved_by = $3, reserved_by_name = $4, reserved_at = NOW()
		WHERE game_id = $1 AND LOWER(nation_name) = LOWER($2)
		""",
		game_id,
		nation_name,
		user_id,
		user_name,
	)
	return _status_affected_rows(result) == 1


async def admin_clear_reservation(game_id: int, nation_name: str) -> bool:
	"""Force clear reservation for admin edit flow."""
	result = await execute(
		"""
		UPDATE game_nations
		SET reserved_by = NULL, reserved_by_name = NULL, reserved_at = NULL
		WHERE game_id = $1 AND LOWER(nation_name) = LOWER($2)
		""",
		game_id,
		nation_name,
	)
	return _status_affected_rows(result) == 1


async def set_game_preferences(game_id: int, user_id: int, user_name: str, choices: list[str]) -> None:
	"""Save/update top country choices for no-sheet games."""
	await execute(
		"""
		INSERT INTO game_preferences (game_id, user_id, user_name, choices)
		VALUES ($1, $2, $3, $4)
		ON CONFLICT (game_id, user_id)
		DO UPDATE SET choices = EXCLUDED.choices, user_name = EXCLUDED.user_name, updated_at = NOW()
		""",
		game_id,
		user_id,
		user_name,
		choices,
	)


async def clear_game_preferences(game_id: int, user_id: int) -> bool:
	"""Remove one player's draft preferences for a no-sheet game."""
	result = await execute(
		"""
		DELETE FROM game_preferences
		WHERE game_id = $1 AND user_id = $2
		""",
		game_id,
		user_id,
	)
	return _status_affected_rows(result) == 1


async def list_game_preferences(game_id: int) -> list[asyncpg.Record]:
	"""Return submitted no-sheet preferences for a game."""
	return await fetch(
		"""
		SELECT user_id, user_name, choices, updated_at
		FROM game_preferences
		WHERE game_id = $1
		ORDER BY updated_at ASC
		""",
		game_id,
	)


async def draft_join_player(
	game_id: int,
	user_id: int,
	user_name: str,
	role_preference: str = "fill",
	up_for_captain: bool = False,
) -> None:
	"""Add or refresh a player in the draft pool for a game."""
	await execute(
		"""
		INSERT INTO game_draft_players (game_id, user_id, user_name, role_preference, up_for_captain)
		VALUES ($1, $2, $3, $4, $5)
		ON CONFLICT (game_id, user_id)
		DO UPDATE SET
			user_name = EXCLUDED.user_name,
			role_preference = EXCLUDED.role_preference,
			up_for_captain = EXCLUDED.up_for_captain,
			updated_at = NOW()
		""",
		game_id,
		user_id,
		user_name,
		role_preference,
		up_for_captain,
	)


async def draft_leave_player(game_id: int, user_id: int) -> bool:
	"""Remove a player from draft pool only if they have not been picked yet."""
	result = await execute(
		"""
		DELETE FROM game_draft_players
		WHERE game_id = $1 AND user_id = $2 AND side = 'unpicked' AND is_captain = FALSE
		""",
		game_id,
		user_id,
	)
	return _status_affected_rows(result) == 1


async def get_draft_state(game_id: int) -> asyncpg.Record | None:
	return await fetchrow(
		"""
		SELECT
			game_id,
			allies_captain_id,
			allies_captain_name,
			axis_captain_id,
			axis_captain_name,
			team_decider_id,
			team_decider_name,
			pending_side_choice_captain_id,
			first_pick_captain_id,
			next_turn,
			status,
			board_message_id,
			updated_at
		FROM game_draft_state
		WHERE game_id = $1
		""",
		game_id,
	)


async def list_draft_players(game_id: int) -> list[asyncpg.Record]:
	return await fetch(
		"""
		SELECT user_id, user_name, role_preference, up_for_captain, side, is_captain, picked_at, updated_at
		FROM game_draft_players
		WHERE game_id = $1
		ORDER BY
			CASE side
				WHEN 'allies' THEN 1
				WHEN 'axis' THEN 2
				ELSE 3
			END,
			is_captain DESC,
			COALESCE(picked_at, updated_at) ASC
		""",
		game_id,
	)


async def list_draft_players_by_side(game_id: int, side: str) -> list[asyncpg.Record]:
	return await fetch(
		"""
		SELECT user_id, user_name, role_preference, up_for_captain, side, is_captain, picked_at, updated_at
		FROM game_draft_players
		WHERE game_id = $1 AND side = $2
		ORDER BY is_captain DESC, COALESCE(picked_at, updated_at) ASC
		""",
		game_id,
		side,
	)


async def get_draft_player(game_id: int, user_id: int) -> asyncpg.Record | None:
	return await fetchrow(
		"""
		SELECT user_id, user_name, role_preference, up_for_captain, side, is_captain, picked_at, updated_at
		FROM game_draft_players
		WHERE game_id = $1 AND user_id = $2
		""",
		game_id,
		user_id,
	)


async def initialize_draft_captain_decision(
	game_id: int,
	captain_a_id: int,
	captain_a_name: str,
	captain_b_id: int,
	captain_b_name: str,
	team_decider_id: int,
	team_decider_name: str,
) -> None:
	"""Initialize captain decision phase before team side assignment."""
	pool = get_pool()
	async with pool.acquire() as conn:
		async with conn.transaction():
			await conn.execute(
				"DELETE FROM game_draft_bans WHERE game_id = $1",
				game_id,
			)
			await conn.execute(
				"DELETE FROM game_draft_votes_v2 WHERE game_id = $1",
				game_id,
			)
			await conn.execute(
				"""
				UPDATE game_draft_players
				SET side = 'unpicked', is_captain = FALSE, picked_at = NULL, updated_at = NOW()
				WHERE game_id = $1
				""",
				game_id,
			)

			await conn.execute(
				"""
				INSERT INTO game_draft_players (game_id, user_id, user_name, side, is_captain, picked_at)
				VALUES ($1, $2, $3, 'captain', TRUE, NOW())
				ON CONFLICT (game_id, user_id)
				DO UPDATE SET
					user_name = EXCLUDED.user_name,
					side = 'captain',
					is_captain = TRUE,
					picked_at = NOW(),
					updated_at = NOW()
				""",
				game_id,
				captain_a_id,
				captain_a_name,
			)

			await conn.execute(
				"""
				INSERT INTO game_draft_players (game_id, user_id, user_name, side, is_captain, picked_at)
				VALUES ($1, $2, $3, 'captain', TRUE, NOW())
				ON CONFLICT (game_id, user_id)
				DO UPDATE SET
					user_name = EXCLUDED.user_name,
					side = 'captain',
					is_captain = TRUE,
					picked_at = NOW(),
					updated_at = NOW()
				""",
				game_id,
				captain_b_id,
				captain_b_name,
			)

			await conn.execute(
				"""
				INSERT INTO game_draft_state (
					game_id,
					allies_captain_id,
					allies_captain_name,
					axis_captain_id,
					axis_captain_name,
					team_decider_id,
					team_decider_name,
					pending_side_choice_captain_id,
					first_pick_captain_id,
					next_turn,
					status,
					updated_at
				)
				VALUES ($1, NULL, '', NULL, '', $2, $3, NULL, NULL, 'none', 'captain_decision', NOW())
				ON CONFLICT (game_id)
				DO UPDATE SET
					allies_captain_id = NULL,
					allies_captain_name = '',
					axis_captain_id = NULL,
					axis_captain_name = '',
					team_decider_id = EXCLUDED.team_decider_id,
					team_decider_name = EXCLUDED.team_decider_name,
					pending_side_choice_captain_id = NULL,
					first_pick_captain_id = NULL,
					next_turn = 'none',
					status = 'captain_decision',
					updated_at = NOW()
				""",
				game_id,
				team_decider_id,
				team_decider_name,
			)


async def set_draft_first_pick_choice(game_id: int, first_pick_captain_id: int, pending_side_choice_captain_id: int) -> None:
	await execute(
		"""
		UPDATE game_draft_state
		SET
			first_pick_captain_id = $2,
			pending_side_choice_captain_id = $3,
			status = 'pending_side_choice',
			updated_at = NOW()
		WHERE game_id = $1
		""",
		game_id,
		first_pick_captain_id,
		pending_side_choice_captain_id,
	)


async def finalize_draft_sides(
	game_id: int,
	allies_captain_id: int,
	allies_captain_name: str,
	axis_captain_id: int,
	axis_captain_name: str,
	first_pick_captain_id: int,
) -> None:
	pool = get_pool()
	async with pool.acquire() as conn:
		async with conn.transaction():
			await conn.execute(
				"""
				UPDATE game_draft_players
				SET side = 'allies', picked_at = NOW(), updated_at = NOW()
				WHERE game_id = $1 AND user_id = $2 AND is_captain = TRUE
				""",
				game_id,
				allies_captain_id,
			)

			await conn.execute(
				"""
				UPDATE game_draft_players
				SET side = 'axis', picked_at = NOW(), updated_at = NOW()
				WHERE game_id = $1 AND user_id = $2 AND is_captain = TRUE
				""",
				game_id,
				axis_captain_id,
			)

			first_pick_side = "allies" if int(first_pick_captain_id) == int(allies_captain_id) else "axis"
			await conn.execute(
				"""
				UPDATE game_draft_state
				SET
					allies_captain_id = $2,
					allies_captain_name = $3,
					axis_captain_id = $4,
					axis_captain_name = $5,
					first_pick_captain_id = $6,
					pending_side_choice_captain_id = NULL,
					next_turn = $7,
					status = 'picking',
					updated_at = NOW()
				WHERE game_id = $1
				""",
				game_id,
				allies_captain_id,
				allies_captain_name,
				axis_captain_id,
				axis_captain_name,
				first_pick_captain_id,
				first_pick_side,
			)


async def set_captain_vote(game_id: int, voter_id: int, voter_name: str, candidate_user_id: int) -> bool:
	"""Legacy wrapper: keep compatibility with older callers."""
	action, _ = await toggle_captain_vote(game_id, voter_id, voter_name, candidate_user_id)
	return action in {"added", "removed"}


async def toggle_captain_vote(
	game_id: int,
	voter_id: int,
	voter_name: str,
	candidate_user_id: int,
) -> tuple[str, int]:
	"""Toggle a captain vote for one candidate.

	Returns:
	- ("added", vote_count): vote added (max 2 total votes per voter)
	- ("removed", vote_count): existing vote removed
	- ("limit", vote_count): voter already has 2 votes and tried adding a third
	- ("ineligible", vote_count): candidate is not eligible captain
	"""
	eligible = await fetchval(
		"""
		SELECT 1
		FROM game_draft_players
		WHERE game_id = $1 AND user_id = $2 AND up_for_captain = TRUE
		""",
		game_id,
		candidate_user_id,
	)
	if not eligible:
		vote_count = await count_voter_captain_votes(game_id, voter_id)
		return "ineligible", vote_count

	existing_vote_id = await fetchval(
		"""
		SELECT id
		FROM game_draft_votes_v2
		WHERE game_id = $1 AND voter_id = $2 AND candidate_user_id = $3
		""",
		game_id,
		voter_id,
		candidate_user_id,
	)
	if existing_vote_id is not None:
		await execute(
			"DELETE FROM game_draft_votes_v2 WHERE id = $1",
			int(existing_vote_id),
		)
		vote_count = await count_voter_captain_votes(game_id, voter_id)
		return "removed", vote_count

	vote_count = await count_voter_captain_votes(game_id, voter_id)
	if vote_count >= 2:
		return "limit", vote_count

	used_slots = await fetch(
		"""
		SELECT vote_slot
		FROM game_draft_votes_v2
		WHERE game_id = $1 AND voter_id = $2
		ORDER BY vote_slot ASC
		""",
		game_id,
		voter_id,
	)
	used = {int(row["vote_slot"]) for row in used_slots}
	next_slot = 1 if 1 not in used else 2

	await execute(
		"""
		INSERT INTO game_draft_votes_v2 (game_id, voter_id, voter_name, candidate_user_id, vote_slot)
		VALUES ($1, $2, $3, $4, $5)
		ON CONFLICT (game_id, voter_id, candidate_user_id)
		DO UPDATE SET
			voter_name = EXCLUDED.voter_name,
			vote_slot = EXCLUDED.vote_slot,
			updated_at = NOW()
		""",
		game_id,
		voter_id,
		voter_name,
		candidate_user_id,
		next_slot,
	)
	vote_count = await count_voter_captain_votes(game_id, voter_id)
	return "added", vote_count


async def count_voter_captain_votes(game_id: int, voter_id: int) -> int:
	value = await fetchval(
		"""
		SELECT COUNT(*)
		FROM game_draft_votes_v2
		WHERE game_id = $1 AND voter_id = $2
		""",
		game_id,
		voter_id,
	)
	return int(value or 0)


async def list_captain_candidates_with_votes(game_id: int) -> list[asyncpg.Record]:
	return await fetch(
		"""
		SELECT
			p.user_id,
			p.user_name,
			p.role_preference,
			p.up_for_captain,
			p.side,
			p.is_captain,
			p.updated_at,
			COUNT(v.id)::INT AS vote_count
		FROM game_draft_players p
		LEFT JOIN game_draft_votes_v2 v
			ON v.game_id = p.game_id AND v.candidate_user_id = p.user_id
		WHERE p.game_id = $1 AND p.up_for_captain = TRUE
		GROUP BY p.user_id, p.user_name, p.role_preference, p.up_for_captain, p.side, p.is_captain, p.updated_at
		ORDER BY vote_count DESC, p.updated_at ASC, p.user_id ASC
		""",
		game_id,
	)


async def list_draft_bans(game_id: int) -> list[asyncpg.Record]:
	return await fetch(
		"""
		SELECT side, banned_player_id, banned_player_name, banned_nation_tag, created_at, updated_at
		FROM game_draft_bans
		WHERE game_id = $1
		ORDER BY side ASC
		""",
		game_id,
	)


async def get_draft_ban_for_side(game_id: int, side: str) -> asyncpg.Record | None:
	if side not in _DRAFT_SIDES:
		return None
	return await fetchrow(
		"""
		SELECT side, banned_player_id, banned_player_name, banned_nation_tag, created_at, updated_at
		FROM game_draft_bans
		WHERE game_id = $1 AND side = $2
		""",
		game_id,
		side,
	)


async def set_draft_ban(
	game_id: int,
	side: str,
	banned_player_id: int,
	banned_player_name: str,
	banned_nation_tag: str,
) -> bool:
	if side not in _DRAFT_SIDES:
		return False

	result = await execute(
		"""
		INSERT INTO game_draft_bans (game_id, side, banned_player_id, banned_player_name, banned_nation_tag)
		VALUES ($1, $2, $3, $4, $5)
		ON CONFLICT (game_id, side)
		DO NOTHING
		""",
		game_id,
		side,
		banned_player_id,
		banned_player_name,
		banned_nation_tag.upper(),
	)
	return _status_affected_rows(result) == 1


async def is_player_banned_from_nation(game_id: int, player_id: int, nation_name: str) -> bool:
	nation_tag = nation_tag_from_name(nation_name)
	ban_exists = await fetchval(
		"""
		SELECT 1
		FROM game_draft_bans
		WHERE game_id = $1 AND banned_player_id = $2 AND banned_nation_tag = $3
		""",
		game_id,
		player_id,
		nation_tag,
	)
	return bool(ban_exists)


async def get_top_captain_candidates(game_id: int, limit: int = 2) -> list[asyncpg.Record]:
	rows = await list_captain_candidates_with_votes(game_id)
	return rows[:limit]


async def set_draft_board_message(game_id: int, message_id: int) -> None:
	await execute(
		"""
		UPDATE game_draft_state
		SET board_message_id = $2, updated_at = NOW()
		WHERE game_id = $1
		""",
		game_id,
		message_id,
	)


async def set_draft_status(game_id: int, status: str) -> None:
	await execute(
		"""
		UPDATE game_draft_state
		SET status = $2, updated_at = NOW()
		WHERE game_id = $1
		""",
		game_id,
		status,
	)


async def draft_pick_player(game_id: int, player_user_id: int, side: str) -> bool:
	"""Move one unpicked player to a side."""
	result = await execute(
		"""
		UPDATE game_draft_players
		SET side = $3, picked_at = NOW(), updated_at = NOW()
		WHERE game_id = $1 AND user_id = $2 AND side = 'unpicked' AND is_captain = FALSE
		""",
		game_id,
		player_user_id,
		side,
	)
	return _status_affected_rows(result) == 1


async def admin_move_draft_player_to_unpicked(game_id: int, user_id: int) -> bool:
	"""Admin helper to undo a draft pick for non-captain players."""
	result = await execute(
		"""
		UPDATE game_draft_players
		SET side = 'unpicked', picked_at = NULL, updated_at = NOW()
		WHERE game_id = $1 AND user_id = $2 AND is_captain = FALSE AND side <> 'unpicked'
		""",
		game_id,
		user_id,
	)
	return _status_affected_rows(result) == 1


async def set_draft_next_turn(game_id: int, side: str) -> None:
	await execute(
		"""
		UPDATE game_draft_state
		SET next_turn = $2, updated_at = NOW()
		WHERE game_id = $1
		""",
		game_id,
		side,
	)


async def count_unpicked_draft_players(game_id: int) -> int:
	value = await fetchval(
		"""
		SELECT COUNT(*)
		FROM game_draft_players
		WHERE game_id = $1 AND side = 'unpicked' AND is_captain = FALSE
		""",
		game_id,
	)
	return int(value or 0)
