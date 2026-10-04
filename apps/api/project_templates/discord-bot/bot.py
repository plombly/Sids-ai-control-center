"""{{NAME}}: a Discord bot. Set DISCORD_TOKEN in the project's Settings -> Secrets."""

import os
import sys

from commands import reply_to


def main():
    token = os.environ.get("DISCORD_TOKEN", "").strip()
    if not token:
        print("No DISCORD_TOKEN: add it in LAIka -> this project -> Settings -> Secrets, then restart the app.",
              flush=True)
        return 1
    import discord

    intents = discord.Intents.default()
    intents.message_content = True
    client = discord.Client(intents=intents)

    @client.event
    async def on_ready():
        print(f"Logged in as {client.user}", flush=True)

    @client.event
    async def on_message(message):
        if message.author.bot:
            return
        answer = reply_to(message.content)
        if answer:
            await message.channel.send(answer)

    client.run(token)
    return 0


if __name__ == "__main__":
    sys.exit(main())
