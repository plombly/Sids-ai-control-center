"""What the bot says: plain functions, tested without Discord."""

PREFIX = "!"


def reply_to(text):
    """The bot's answer to a message, or None to stay quiet."""
    text = (text or "").strip()
    if not text.startswith(PREFIX):
        return None
    command, _, rest = text[len(PREFIX):].partition(" ")
    if command == "ping":
        return "pong"
    if command == "echo" and rest.strip():
        return rest.strip()[:1500]
    if command == "help":
        return "Commands: !ping, !echo <text>, !help"
    return None
