import json
import threading
import urllib.request
from http.server import ThreadingHTTPServer

import app


def test_items_are_added_listed_and_validated(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    server = ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        assert json.load(urllib.request.urlopen(f"{base}/health")) == {"status": "ok"}
        request = urllib.request.Request(f"{base}/items", data=b'{"name": "first"}', method="POST")
        assert json.load(urllib.request.urlopen(request))["name"] == "first"
        assert [item["name"] for item in json.load(urllib.request.urlopen(f"{base}/items"))] == ["first"]
        try:
            urllib.request.urlopen(urllib.request.Request(f"{base}/items", data=b'{"name": ""}', method="POST"))
            raise AssertionError("an empty name was accepted")
        except urllib.error.HTTPError as error:
            assert error.code == 400
    finally:
        server.shutdown()
