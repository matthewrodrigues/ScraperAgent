"""Boot the dashboard.

    python main.py             # serve at http://127.0.0.1:8000
    python main.py --host 0.0.0.0 --port 8080

The agent itself is driven from the dashboard — no CLI run loop in v1.
"""

import argparse

import uvicorn


def main() -> None:
    parser = argparse.ArgumentParser(description="ScraperAgent dashboard")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--reload", action="store_true", help="Auto-reload on code changes (dev only)")
    args = parser.parse_args()

    uvicorn.run(
        "api.main:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        log_level="info",
    )


if __name__ == "__main__":
    main()
