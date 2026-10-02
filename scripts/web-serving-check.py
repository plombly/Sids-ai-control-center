#!/usr/bin/env python3
"""Deterministic checks for the static web container and Compose wiring."""

from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def main():
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    services = compose["services"]
    web = services["web"]
    api = services["api"]

    require(web["build"]["context"] == "./apps/web", "web must build apps/web")
    require(web.get("network_mode") == "host" and "ports" not in web,
            "web must use host networking (nginx must see real client addresses)")
    require(
        web["depends_on"]["api"]["condition"] == "service_healthy",
        "web must wait for a healthy api",
    )
    require(api["ports"] == ["127.0.0.1:8000:8000"],
            "the api must only listen on 127.0.0.1 (all traffic goes through nginx on 8080)")
    require("healthcheck" in api, "api must have a healthcheck")
    require("ports" not in services["postgres"], "postgres must not be exposed")
    require(
        services["redis"].get("ports") == ["127.0.0.1:6379:6379"],
        "redis must remain available to host-side LAIka tools only",
    )

    dockerfile = (ROOT / "apps/web/Dockerfile").read_text(encoding="utf-8")
    require("FROM nginx:" in dockerfile, "web must use nginx")
    require("COPY nginx.conf /etc/nginx/conf.d/default.conf" in dockerfile,
            "nginx config must be installed as-is (no credentials substituted into it)")
    require("HEALTHCHECK" in dockerfile, "web container must define a healthcheck")

    nginx = (ROOT / "apps/web/nginx.conf").read_text(encoding="utf-8")
    require("location /api/" in nginx, "nginx must define the API route")
    require("listen 8080 default_server;" in nginx, "nginx must listen on host port 8080")
    require("proxy_pass http://127.0.0.1:8000;" in nginx, "API traffic must go to the api's host port")
    require("location = /health" in nginx, "nginx must provide a health endpoint")
    # Sign-in replaced the old LAN token injection: nginx adds no credentials,
    # strips any operator token a browser sends, and passes the real client
    # address (the API trusts it because only nginx can reach port 8000).
    require('proxy_set_header X-Laika-Token "";' in nginx, "nginx must strip any operator token from browsers")
    require("LAIKA_OPERATOR_TOKEN" not in nginx and "LAIKA_OPERATOR_TOKEN" not in dockerfile,
            "the web container must never hold the operator token")
    require("proxy_set_header X-Real-IP $remote_addr;" in nginx, "nginx must pass the real client address")
    require("env_file" not in web, "the web container needs no secrets")

if __name__ == "__main__":
    main()
    print("web serving configuration: PASS")
