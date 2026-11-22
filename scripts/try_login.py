"""Try the PitPat login flow outside Home Assistant.

Usage:
    PITPAT_EMAIL=you@example.com python scripts/try_login.py

Prompts for the password. Prints token metadata, never the tokens themselves.
"""

import asyncio
import getpass
import os
import sys
import time
from datetime import date, timedelta
import types
from pathlib import Path

# Register the package without running its __init__ (which needs Home Assistant).
_pkg = types.ModuleType("pitpat")
_pkg.__path__ = [str(Path(__file__).resolve().parents[1] / "custom_components" / "ha_pitpat")]
sys.modules["pitpat"] = _pkg

import aiohttp  # noqa: E402

from pitpat.api import PitPatApiClient  # noqa: E402
from pitpat.auth import async_login, async_refresh  # noqa: E402


async def main() -> None:
    email = os.environ.get("PITPAT_EMAIL") or input("Email: ")
    password = getpass.getpass("Password: ")

    tokens = await async_login(email, password)
    print(f"Login OK: sub={tokens.subject}, expires in {tokens.expires_at - time.time():.0f}s")

    async with aiohttp.ClientSession() as session:
        refreshed = await async_refresh(session, tokens.refresh_token)
        rotated = refreshed.refresh_token != tokens.refresh_token
        print(f"Refresh OK: refresh token rotated={rotated}")

        client = PitPatApiClient(session, refreshed)
        settings = await client.async_get_settings()
        print(f"Settings: UserId={settings.get('UserId')}, dogs={settings.get('UserDogTotal')}")

        for dog in await client.async_get_dogs(settings["UserId"]):
            monitor = dog.get("Monitor") or {}
            print(f"Dog: {dog['Name']} ({dog['Id']}), firmware={monitor.get('FirmwareVersion')}")
            status = await client.async_get_monitor(settings["UserId"], dog["Id"])
            battery = ((status or {}).get("BatteryInfo") or {}).get("Value") or {}
            print(f"  Monitor: battery={battery.get('BatteryLevelFraction')}")
            today = date.today()
            months = await client.async_get_activity(
                settings["UserId"], dog["Id"], today - timedelta(days=1), today
            )
            for day in (months[0].get("ActivityDays") or [])[:2] if months else []:
                print(f"  {day['Date'][:10]}: exercise={day['ExerciseMinutes']} min")


asyncio.run(main())
