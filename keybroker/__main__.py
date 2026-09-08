"""Run the broker: python -m keybroker

Binds loopback only. Tailscale Funnel runs on this machine and forwards to
127.0.0.1, so there is never a reason to listen on all interfaces.
"""

import uvicorn

import config


if __name__ == "__main__":
    uvicorn.run(
        "keybroker.app:app",
        host="127.0.0.1",
        port=config.BROKER_PORT,
        log_level="info",
    )
