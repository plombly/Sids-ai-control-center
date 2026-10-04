from commands import reply_to


def test_commands():
    assert reply_to("!ping") == "pong"
    assert reply_to("!echo hi there") == "hi there"
    assert reply_to("hello") is None and reply_to("!nope") is None
