# HOI4 Discord Bot (Local Test Guide)

## 1) Create environment file

Copy [.env.example](.env.example) to `.env` and fill real values.

Required keys:
- `DISCORD_TOKEN`
- `DATABASE_URL`

Optional keys:
- `DEV_GUILD_ID` (recommended while developing slash commands)

## 2) Install dependencies

Install packages from [requirements.txt](requirements.txt).

## 3) Prepare PostgreSQL

Create a database (for example `hoi4_bot`) and make sure `DATABASE_URL` points to it.

The bot creates tables automatically on startup.

## 4) Enable Discord setting

In Discord Developer Portal for your bot:
- Enable **Message Content Intent**

## 5) Run the bot

Start from project root with Python module mode so imports work:
- `python -m bot.main`

## 6) Test commands in Discord

- `!ping`
- `!game_create 2026-03-10T18:00 | Aggelus | Vanilla + Local Mods | Sunday Lobby`
- `!game_list`
- `!sheet_create 1`
- `!sheet 1`
- `!reserve 1 Germany`
- `!unreserve 1 Germany`
- `!game_announce 1`

## Slash command workflow (recommended)

1. Set announcement channel (admin):
	- `/settings announce_channel`
2. (Optional) Allow a non-admin role to run restricted bot commands:
	- `/settings bot_access_role`
3. Announce and create game in one step:
	- `/game announce`
4. Reserve nations:
	- `/reserve`
	- `/unreserve`
