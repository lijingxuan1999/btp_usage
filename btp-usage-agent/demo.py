"""
Quick interactive demo — calls the UAS tools directly against the live API.
No LLM required.

Usage:
    python demo.py
"""
import asyncio
import json
import sys
import os
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv
load_dotenv()

sys.path.insert(0, "app")

from uas_tool import (
    get_btp_usage,
    get_btp_services_summary,
    get_aicore_model_cu_usage,
    get_global_account_monthly_usage,
    list_subaccounts,
)


def sep(title: str):
    print(f"\n{'='*66}")
    print(f"  {title}")
    print(f"{'='*66}")


def today() -> str:
    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")


def n_days_ago(n: int) -> str:
    return (datetime.now(tz=timezone.utc) - timedelta(days=n)).strftime("%Y-%m-%d")


def current_month_range() -> tuple[str, str]:
    now = datetime.now(tz=timezone.utc)
    start = now.replace(day=1).strftime("%Y-%m-%d")
    return start, now.strftime("%Y-%m-%d")


async def demo_services_summary():
    sep("Demo 1 — Services Summary (last 7 days)")
    from_d, to_d = n_days_ago(7), today()
    print(f"  Period: {from_d} → {to_d}")
    raw = await get_btp_services_summary.ainvoke({"from_date": from_d, "to_date": to_d})
    data = json.loads(raw)

    if "error" in data:
        print(f"  ✗ Error: {data['error']}")
        return

    print(f"  Total records : {data['total_records']}")
    print(f"  Services found: {data['service_count']}")
    print()
    print(f"  {'Service':<32} {'Metric':<38} {'Total':>12} {'Unit'}")
    print(f"  {'-'*32} {'-'*38} {'-'*12} {'-'*10}")
    for row in data.get("detail", [])[:12]:
        print(f"  {row['service']:<32} {row['metric']:<38} {row['total_usage']:>12.4f} {row['unit']}")


async def demo_key_usage():
    sep("Demo 2 — Key Services Daily Usage (last 7 days)")
    from_d, to_d = n_days_ago(7), today()
    print(f"  Period: {from_d} → {to_d}")
    raw = await get_btp_usage.ainvoke({
        "from_date": from_d,
        "to_date": to_d,
        "service_filter": "key",
    })
    data = json.loads(raw)

    if "error" in data:
        print(f"  ✗ Error: {data['error']}")
        return

    print(f"  Total records: {data['total_records']}")
    print()
    print(f"  {'Date':<12} {'Service':<28} {'Metric':<38} {'Usage':>12} {'Unit'}")
    print(f"  {'-'*12} {'-'*28} {'-'*38} {'-'*12} {'-'*10}")
    for r in data.get("records", [])[:15]:
        print(f"  {r['date']:<12} {r['service']:<28} {r['metric']:<38} {r['usage']:>12.4f} {r['unit']}")


async def demo_aicore_cu():
    sep("Demo 3 — AI Core CU Usage by Model (this month)")
    from_d, to_d = current_month_range()
    print(f"  Period: {from_d} → {to_d}")
    raw = await get_aicore_model_cu_usage.ainvoke({
        "from_date": from_d,
        "to_date": to_d,
        "time_granularity": "none",
    })
    data = json.loads(raw)

    if "error" in data:
        print(f"  ✗ Error: {data['error']}")
        return

    if data.get("message"):
        print(f"  ℹ {data['message']}")
        return

    print(f"  Grand total CU : {data['grand_total_cu']}")
    print(f"  Records matched: {data['record_count']}")
    print()
    print(f"  {'Model':<52} {'Total CU':>14}")
    print(f"  {'-'*52} {'-'*14}")
    for m in data.get("by_model", []):
        print(f"  {m['model']:<52} {m['total_cu']:>14.6f}")


async def demo_monthly_global():
    sep("Demo 4 — Global Account Monthly Usage (last 3 months)")
    now = datetime.now(tz=timezone.utc)
    if now.month == 1:
        to_ym = f"{now.year - 1}-12"
    else:
        to_ym = f"{now.year}-{now.month - 1:02d}"

    # 3 months back
    ym_parts = to_ym.split("-")
    y, m = int(ym_parts[0]), int(ym_parts[1])
    fm = m - 2
    fy = y
    if fm < 1:
        fm += 12
        fy -= 1
    from_ym = f"{fy}-{fm:02d}"

    print(f"  Period: {from_ym} → {to_ym}")
    raw = await get_global_account_monthly_usage.ainvoke({
        "from_month": from_ym,
        "to_month": to_ym,
        "service_filter": "key",
        "group_by": "month",
    })
    data = json.loads(raw)

    if "error" in data:
        print(f"  ✗ Error: {data['error']}")
        return

    if data.get("message"):
        print(f"  ℹ {data['message']}")
        return

    print(f"  Global Account : {data.get('global_account_name', '')} ({data.get('global_account_id', '')})")
    print(f"  Total records  : {data.get('total_records', 0)}")
    print()
    for mt in data.get("monthly_totals", []):
        print(f"  ── {mt['month']} ──")
        for metric in mt.get("metrics", [])[:5]:
            print(f"     {metric['metric_id']:<40} {metric['total_usage']:>14.4f} {metric['unit']}")


async def main():
    print("\n🚀  BTP Usage Agent — Live API Demo")
    print(f"    Subaccount: {os.environ.get('BTP_SUBACCOUNT_ID', '(not set)')}")
    print(f"    UAS URL   : {os.environ.get('BTP_UAS_URL', '(not set)')}")

    await demo_services_summary()
    await demo_key_usage()
    await demo_aicore_cu()
    await demo_monthly_global()

    print("\n\n✅  Demo complete!")


if __name__ == "__main__":
    asyncio.run(main())
