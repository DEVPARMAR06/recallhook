"""Request guard for the local API: localhost Host header + custom header on writes."""
from __future__ import annotations

from fastapi import HTTPException, Request

CLIENT_HEADER = "x-hm-client"


def make_guard(cfg):
    ok_hosts = {f"127.0.0.1:{cfg.port}", f"localhost:{cfg.port}"}

    def guard(request: Request, mutating: bool = False) -> None:
        # Host check blocks DNS-rebinding; the custom header on writes forces a CORS
        # preflight, so a random web page cannot POST/DELETE to localhost.
        if request.headers.get("host", "") not in ok_hosts:
            raise HTTPException(403, "forbidden host")
        if mutating and CLIENT_HEADER not in request.headers:
            raise HTTPException(403, "missing client header")

    return guard
