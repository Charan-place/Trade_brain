"""Shared HTTP identity. Upstox's Cloudflare blocks Python's default 'Python-urllib/x.y' agent (error 1010),
so we send the same User-Agent the official upstox-python-sdk sends (api_client.py: 'Swagger-Codegen/1.0.0/python')."""
USER_AGENT = "Swagger-Codegen/1.0.0/python"


def headers(token: str | None = None, accept: str = "application/json", **extra: str) -> dict[str, str]:
    h = {"Accept": accept, "User-Agent": USER_AGENT, **extra}
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h
