"""python -m dispatch.api [--host 127.0.0.1] [--port 8080]  (DATABASE_URL picks the database; default SQLite)."""

import argparse

import uvicorn

from .app import create_app


def main() -> None:
    ap = argparse.ArgumentParser(description="LookAtDisPatch API")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8080)
    args = ap.parse_args()
    uvicorn.run(create_app(), host=args.host, port=args.port)


if __name__ == "__main__":
    main()
