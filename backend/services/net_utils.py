from __future__ import annotations

import os
from urllib.parse import urlparse

import requests


PROXY_ENV_KEYS = (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
)

LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


def _is_broken_loopback_proxy(value: str) -> bool:
    if not value:
        return False
    try:
        parsed = urlparse(value)
        host = (parsed.hostname or "").lower()
        port = parsed.port
    except Exception:
        return False
    return host in LOOPBACK_HOSTS and port == 9


def disable_dead_proxy_env() -> None:
    changed = False
    for key in PROXY_ENV_KEYS:
        value = os.environ.get(key)
        if value and _is_broken_loopback_proxy(value):
            os.environ.pop(key, None)
            changed = True
    if changed:
        os.environ["NO_PROXY"] = "*"
        os.environ["no_proxy"] = "*"


def configure_session(session: requests.Session, headers: dict | None = None) -> requests.Session:
    disable_dead_proxy_env()
    session.trust_env = False
    session.proxies.clear()
    if headers:
        session.headers.update(headers)
    return session


def build_session(headers: dict | None = None) -> requests.Session:
    disable_dead_proxy_env()
    try:
        import cloudscraper

        session = cloudscraper.create_scraper(
            browser={"browser": "chrome", "platform": "windows", "mobile": False}
        )
    except Exception:
        session = requests.Session()
    return configure_session(session, headers=headers)
