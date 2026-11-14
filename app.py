"""Minimal NASA POWER client with Earthdata login from .env."""
from __future__ import annotations

import argparse
import getpass
import json
import os
import stat
import sys
from pathlib import Path

import requests
from dotenv import load_dotenv

POWER_URL = "https://power.larc.nasa.gov/api/temporal/daily/point"
EARTHDATA_PROFILE_URL = "https://urs.earthdata.nasa.gov/profile"
ENV_PATH = Path(__file__).with_name(".env")


class EarthdataError(RuntimeError):
    pass


def load_credentials(env_path: Path = ENV_PATH) -> tuple[str, str]:
    load_dotenv(env_path)
    username = os.getenv("EARTHDATA_USERNAME", "").strip()
    password = os.getenv("EARTHDATA_PASSWORD", "").strip()
    if not username or not password:
        raise EarthdataError(
            f"Missing EARTHDATA_USERNAME / EARTHDATA_PASSWORD in {env_path}. "
            "Run: python app.py init-env"
        )
    return username, password


def write_env_file(username: str, password: str, env_path: Path = ENV_PATH) -> Path:
    env_path.write_text(
        f"EARTHDATA_USERNAME={username}\nEARTHDATA_PASSWORD={password}\n",
        encoding="utf-8",
    )
    env_path.chmod(stat.S_IRUSR | stat.S_IWUSR)  # credentials stay private
    return env_path


class EarthdataSession(requests.Session):
    def __init__(self, username: str, password: str) -> None:
        super().__init__()
        self.auth = (username, password)
        self.headers["User-Agent"] = "spaceapps-2026-nasa-power/0.1"

    def login(self) -> None:
        """Raise EarthdataError unless the credentials are accepted."""
        resp = self.get(EARTHDATA_PROFILE_URL, timeout=30)
        if resp.status_code != 200:
            raise EarthdataError(f"Earthdata login failed (HTTP {resp.status_code})")


def fetch_daily_point(
    session: requests.Session,
    latitude: float,
    longitude: float,
    start: str,
    end: str,
    parameters: tuple[str, ...] = ("T2M", "PRECTOTCORR"),
    community: str = "RE",
) -> dict:
    params = {
        "parameters": ",".join(parameters),
        "community": community,
        "latitude": latitude,
        "longitude": longitude,
        "start": start,
        "end": end,
        "format": "JSON",
    }
    resp = session.get(POWER_URL, params=params, timeout=60)
    resp.raise_for_status()
    return resp.json()


def cmd_init_env(force: bool) -> None:
    if ENV_PATH.exists() and not force:
        raise EarthdataError(f"{ENV_PATH} already exists (use --force to overwrite)")
    username = input("Earthdata username: ").strip()
    password = getpass.getpass("Earthdata password: ")
    if not username or not password:
        raise EarthdataError("Username and password are required")
    print(f"Wrote {write_env_file(username, password)}")


def cmd_check_auth() -> None:
    session = EarthdataSession(*load_credentials())
    session.login()
    print("Earthdata login OK")


def cmd_fetch(args: argparse.Namespace) -> None:
    session = EarthdataSession(*load_credentials())
    data = fetch_daily_point(
        session,
        args.lat,
        args.lon,
        args.start,
        args.end,
        tuple(args.parameters.split(",")),
    )
    print(json.dumps(data, indent=2))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="NASA POWER point data via Earthdata login")
    sub = parser.add_subparsers(dest="command")

    init = sub.add_parser("init-env", help="prompt for credentials and write .env")
    init.add_argument("--force", action="store_true", help="overwrite an existing .env")

    sub.add_parser("check-auth", help="verify the credentials in .env")

    fetch = sub.add_parser("fetch", help="download daily POWER data for one point")
    fetch.add_argument("--lat", type=float, required=True)
    fetch.add_argument("--lon", type=float, required=True)
    fetch.add_argument("--start", required=True, help="YYYYMMDD")
    fetch.add_argument("--end", required=True, help="YYYYMMDD")
    fetch.add_argument("--parameters", default="T2M,PRECTOTCORR")

    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return 2

    handlers = {
        "init-env": lambda: cmd_init_env(args.force),
        "check-auth": cmd_check_auth,
        "fetch": lambda: cmd_fetch(args),
    }
    try:
        handlers[args.command]()
    except (EarthdataError, requests.RequestException) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
