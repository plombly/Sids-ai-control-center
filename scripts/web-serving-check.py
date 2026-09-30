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
    require(web["ports"] == ["8080:80"], "web must publish host port 8080")
    require(
        web["depends_on"]["api"]["condition"] == "service_healthy",
        "web must wait for a healthy api",
    )
    require(api["ports"] == ["8000:8000"], "api port must remain unchanged")
    require("healthcheck" in api, "api must have a healthcheck")
    require("ports" not in services["postgres"], "postgres must not be exposed")
    require(
        services["redis"].get("ports") == ["127.0.0.1:6379:6379"],
        "redis must remain available to host-side SID tools only",
    )

    dockerfile = (ROOT / "apps/web/Dockerfile").read_text(encoding="utf-8")
    require("FROM nginx:" in dockerfile, "web must use nginx")
    require(
        "COPY nginx.conf /etc/nginx/templates/default.conf.template" in dockerfile,
        "nginx config must be installed as a template (operator token substitution)",
    )
    require("HEALTHCHECK" in dockerfile, "web container must define a healthcheck")

    nginx = (ROOT / "apps/web/nginx.conf").read_text(encoding="utf-8")
    require("location /api/" in nginx, "nginx must define the API route")
    require("proxy_pass http://api:8000;" in nginx, "API traffic must use the internal api service")
    require("location = /health" in nginx, "nginx must provide a health endpoint")
    require(
        'proxy_set_header X-SID-Token "${SID_OPERATOR_TOKEN}";' in nginx,
        "API route must inject the operator token from the environment",
    )


if __name__ == "__main__":
    main()
    print("web serving configuration: PASS")
