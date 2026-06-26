"""
Fetch real UAS API responses and store them as JSON in sample_data/.

Endpoints:
  1. /reports/v1/monthlyUsage     – April, May, June 2025 (combined call)
  2. /reports/v1/subaccountUsage  – April, May, June 2025 (per-month calls)
  3. /reports/v1/subaccountUsage  – AI Core only, April, May, June 2025
"""

import asyncio
import json
import os
import time
from pathlib import Path

import httpx
from dotenv import load_dotenv

# ── Load .env ──────────────────────────────────────────────────────────────
load_dotenv(dotenv_path=Path(__file__).parent / ".env", override=True)

UAS_URL       = os.environ["BTP_UAS_URL"]
AUTH_URL      = os.environ["BTP_AUTH_URL"]
CLIENT_ID     = os.environ["BTP_CLIENT_ID"]
CLIENT_SECRET = os.environ["BTP_CLIENT_SECRET"]
SUBACCOUNT_ID = os.environ["BTP_SUBACCOUNT_ID"]

SAMPLE_DIR = Path(__file__).parent / "sample_data"
SAMPLE_DIR.mkdir(exist_ok=True)

TIMEOUT = httpx.Timeout(connect=15.0, read=120.0, write=15.0, pool=15.0)

# ── Months to fetch ────────────────────────────────────────────────────────
MONTHS = [
    ("2025-04", "April 2025"),
    ("2025-05", "May 2025"),
    ("2025-06", "June 2025"),
]

# ── OAuth2 token ───────────────────────────────────────────────────────────
_token_cache: dict = {"token": None, "expires_at": 0.0}


async def get_token() -> str:
    if _token_cache["token"] and time.time() < _token_cache["expires_at"] - 30:
        return _token_cache["token"]

    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        resp = await client.post(
            AUTH_URL,
            data={
                "grant_type":    "client_credentials",
                "client_id":     CLIENT_ID,
                "client_secret": CLIENT_SECRET,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        resp.raise_for_status()
        data = resp.json()

    _token_cache["token"]      = data["access_token"]
    _token_cache["expires_at"] = time.time() + data.get("expires_in", 3600)
    print(f"  ✓ Token obtained (expires in {data.get('expires_in', 3600)}s)")
    return _token_cache["token"]


def ym_to_api(ym: str) -> str:
    """YYYY-MM → YYYYMM"""
    return ym.replace("-", "")


def ym_to_dates(ym: str) -> tuple[str, str]:
    """YYYY-MM → first and last day as YYYYMMDD strings."""
    import calendar
    year, month = int(ym[:4]), int(ym[5:7])
    last_day = calendar.monthrange(year, month)[1]
    return f"{year:04d}{month:02d}01", f"{year:04d}{month:02d}{last_day:02d}"


# ── 1. Monthly Usage (/reports/v1/monthlyUsage) ────────────────────────────
async def fetch_monthly_usage() -> None:
    print("\n[1/3] Fetching /reports/v1/monthlyUsage for April–June 2025 ...")
    token = await get_token()

    url    = f"{UAS_URL}/reports/v1/monthlyUsage"
    params = {
        "fromDate": "202504",
        "toDate":   "202506",
    }
    print(f"  GET {url}  params={params}")

    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        resp = await client.get(
            url,
            params=params,
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
        )
        print(f"  HTTP {resp.status_code}")
        resp.raise_for_status()
        data = resp.json()

    out_path = SAMPLE_DIR / "monthlyUsage_2025-04_to_2025-06.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    records = data.get("content", data) if isinstance(data, dict) else data
    record_count = len(records) if isinstance(records, list) else "N/A"
    print(f"  ✓ Saved → {out_path}  ({record_count} records)")


# ── 2. Subaccount Usage (/reports/v1/subaccountUsage) ─────────────────────
async def fetch_subaccount_usage() -> None:
    print("\n[2/3] Fetching /reports/v1/subaccountUsage for April, May, June 2025 ...")
    token = await get_token()

    for ym, label in MONTHS:
        from_date, to_date = ym_to_dates(ym)
        url    = f"{UAS_URL}/reports/v1/subaccountUsage"
        params = {
            "subaccountId":      SUBACCOUNT_ID,
            "fromDate":          from_date,
            "toDate":            to_date,
            "periodPerspective": "DAY",
        }
        print(f"  GET {url}  params={params}")

        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            resp = await client.get(
                url,
                params=params,
                headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            )
            print(f"  HTTP {resp.status_code} ({label})")
            resp.raise_for_status()
            data = resp.json()

        safe_ym = ym.replace("-", "-")
        out_path = SAMPLE_DIR / f"subaccountUsage_{ym}.json"
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

        records = data.get("content", data) if isinstance(data, dict) else data
        record_count = len(records) if isinstance(records, list) else "N/A"
        print(f"  ✓ Saved → {out_path}  ({record_count} records)")


# ── 3. Subaccount Usage – AI Core only ────────────────────────────────────
async def fetch_subaccount_usage_aicore() -> None:
    print("\n[3/3] Fetching /reports/v1/subaccountUsage (AI Core only) for April, May, June 2025 ...")
    token = await get_token()

    for ym, label in MONTHS:
        from_date, to_date = ym_to_dates(ym)
        url    = f"{UAS_URL}/reports/v1/subaccountUsage"
        params = {
            "subaccountId":      SUBACCOUNT_ID,
            "fromDate":          from_date,
            "toDate":            to_date,
            "periodPerspective": "DAY",
            "serviceId":         "ai-core",   # AI Core service filter
        }
        print(f"  GET {url}  params={params}")

        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            resp = await client.get(
                url,
                params=params,
                headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            )
            print(f"  HTTP {resp.status_code} ({label})")
            resp.raise_for_status()
            data = resp.json()

        out_path = SAMPLE_DIR / f"subaccountUsage_aicore_{ym}.json"
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

        records = data.get("content", data) if isinstance(data, dict) else data
        record_count = len(records) if isinstance(records, list) else "N/A"
        print(f"  ✓ Saved → {out_path}  ({record_count} records)")


# ── Main ───────────────────────────────────────────────────────────────────
async def main() -> None:
    print("=" * 60)
    print("BTP UAS API – Fetch Sample Data")
    print(f"UAS URL:      {UAS_URL}")
    print(f"Subaccount:   {SUBACCOUNT_ID}")
    print(f"Output dir:   {SAMPLE_DIR}")
    print("=" * 60)

    await fetch_monthly_usage()
    await fetch_subaccount_usage()
    await fetch_subaccount_usage_aicore()

    print("\n" + "=" * 60)
    print("All sample data fetched successfully.")
    print(f"Files in {SAMPLE_DIR}:")
    for f in sorted(SAMPLE_DIR.iterdir()):
        size = f.stat().st_size
        print(f"  {f.name:55s}  {size:>8,} bytes")


if __name__ == "__main__":
    asyncio.run(main())
