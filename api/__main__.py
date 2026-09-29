"""Start Scrivio:  python -m api

Binds to loopback. The old documented command passed --host 0.0.0.0,
which offers the API to every machine on the network the laptop happens
to be joined to: a cafe, a conference, an office.

    PORT=8899            the port to listen on
    SCRIVIO_HOST         the address to bind; loopback unless you change it

Binding anywhere else is allowed, because containers and remote machines
are real. It is never the default, it says what it is doing, and the
server still refuses any request addressed to a host name that is not
listed in SCRIVIO_ALLOWED_HOSTS.
"""
from __future__ import annotations

import ipaddress
import os
import sys

import uvicorn


def _is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def main() -> int:
    host = os.environ.get("SCRIVIO_HOST", "127.0.0.1").strip() or "127.0.0.1"
    try:
        port = int(os.environ.get("PORT", "8899"))
    except ValueError:
        print("PORT must be a number.", file=sys.stderr)
        return 2
    if not _is_loopback(host):
        print(
            f"\n  Scrivio is binding to {host}, which other machines can reach.\n"
            "  It will answer only requests addressed to a name listed in\n"
            "  SCRIVIO_ALLOWED_HOSTS, and only from a paired browser. Traffic is\n"
            "  plain HTTP: put TLS in front of it before using it over a network.\n",
            file=sys.stderr, flush=True,
        )
    shown = "localhost" if _is_loopback(host) else host
    print(f"\n  Scrivio: http://{shown}:{port}", file=sys.stderr, flush=True)
    from api import observability
    observability.configure_logging()
    # log_config=None: keep the format set above, which carries the
    # request and job ids, instead of uvicorn's own.
    uvicorn.run("api.server:app", host=host, port=port, log_config=None)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
