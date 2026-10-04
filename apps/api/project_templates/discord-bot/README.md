# {{NAME}}

A Discord bot in Python (discord.py). Commands live in `commands.py` as plain functions with tests.

1. Create a bot at https://discord.com/developers/applications, enable *Message Content Intent*, copy its token.
2. In LAIka: this project -> Settings -> Secrets -> add `DISCORD_TOKEN`.
3. Restart the app (Overview -> App -> Restart). Invite the bot to your server with the OAuth2 URL generator.

- Test: `python3 -m pytest -q`
