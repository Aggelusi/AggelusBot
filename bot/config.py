from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


@dataclass(frozen=True)
class Settings:
	discord_token: str
	database_url: str
	command_prefix: str = "!"
	dev_guild_id: int | None = None


def _get_required_env(name: str) -> str:
	"""Return a required environment variable or fail early with context.

	Failing at startup is intentional: it prevents confusing runtime errors
	later (for example when the bot first tries to connect to Discord/DB).
	"""
	value = os.getenv(name)
	if not value:
		raise RuntimeError(f"Missing required environment variable: {name}")
	return value


def _get_optional_int_env(name: str) -> int | None:
	value = os.getenv(name)
	if not value:
		return None
	try:
		return int(value)
	except ValueError as exc:
		raise RuntimeError(f"Environment variable {name} must be an integer") from exc


def load_settings() -> Settings:
	"""Load environment variables from .env and return validated settings."""
	# .env is expected at project root, one level above the "bot" package.
	env_path = Path(__file__).resolve().parents[1] / ".env"
	load_dotenv(dotenv_path=env_path)

	return Settings(
		discord_token=_get_required_env("DISCORD_TOKEN"),
		database_url=_get_required_env("DATABASE_URL"),
		command_prefix=os.getenv("COMMAND_PREFIX", "!"),
		dev_guild_id=_get_optional_int_env("DEV_GUILD_ID"),
	)


settings = load_settings()
