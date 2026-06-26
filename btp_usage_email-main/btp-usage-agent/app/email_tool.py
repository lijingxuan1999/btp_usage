"""
BTP Usage Agent — Email Notification Module

Provides three email capabilities:
  1. send_summary_email(from_date, to_date)  — on-demand, LLM-callable @tool
  2. send_daily_report_email()               — called by scheduler every morning
  3. send_anomaly_alert_email(anomaly_result) — called by scheduler on spike detection

HTML reports include matplotlib charts embedded as inline CID PNG attachments.

Required environment variables (.env):
  SMTP_HOST      = smtp.gmail.com
  SMTP_PORT      = 587
  SMTP_USER      = your@email.com
  SMTP_PASSWORD  = your-app-password
  EMAIL_FROM     = btp-agent@yourcompany.com
  EMAIL_TO       = admin@yourcompany.com   (comma-separated for multiple)

  CONTRACT_CU    = 100000
  CONTRACT_START = 2026-01-01
  CONTRACT_END   = 2026-12-31
"""

import io
import json
import logging
import os
import smtplib
from datetime import datetime, timedelta, timezone
from email.mime.image import MIMEImage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Optional

import matplotlib
matplotlib.use("Agg")  # non-interactive backend — no display needed
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
import numpy as np

from dotenv import load_dotenv
from langchain_core.tools import tool

from uas_tool import (
    _fetch_usage,
    _validate_date,
    _last_n_days,
)

load_dotenv(override=False)
logger = logging.getLogger(__name__)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _get_recipients() -> list[str]:
    raw = os.environ.get("EMAIL_TO", "")
    return [e.strip() for e in raw.split(",") if e.strip()]


def _send_email(subject: str, html_body: str, inline_images: list[tuple[str, bytes]]) -> None:
    """
    Send an HTML email with inline PNG images via SMTP STARTTLS.

    inline_images: list of (cid, png_bytes) — referenced as <img src="cid:..."> in HTML
    """
    smtp_host = os.environ.get("SMTP_HOST", "")
    smtp_port = int(os.environ.get("SMTP_PORT", "587"))
    smtp_user = os.environ.get("SMTP_USER", "")
    smtp_pass = os.environ.get("SMTP_PASSWORD", "")
    from_addr = os.environ.get("EMAIL_FROM", smtp_user)
    to_addrs  = _get_recipients()

    if not smtp_host or not smtp_user or not to_addrs:
        raise ValueError(
            "Email not configured. Set SMTP_HOST, SMTP_USER, SMTP_PASSWORD, EMAIL_TO in .env"
        )

    msg = MIMEMultipart("related")
    msg["Subject"] = subject
    msg["From"]    = from_addr
    msg["To"]      = ", ".join(to_addrs)

    # Attach HTML body
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    # Attach inline images
    for cid, png_bytes in inline_images:
        img = MIMEImage(png_bytes, "png")
        img.add_header("Content-ID", f"<{cid}>")
        img.add_header("Content-Disposition", "inline", filename=f"{cid}.png")
        msg.attach(img)

    with smtplib.SMTP(smtp_host, smtp_port, timeout=30) as server:
        server.ehlo()
        server.starttls()
        server.login(smtp_user, smtp_pass)
        server.sendmail(from_addr, to_addrs, msg.as_string())

    logger.info("Email sent to %s: %s", to_addrs, subject)


# ── Chart generators ──────────────────────────────────────────────────────────

def _chart_daily_cu(daily_series: list[dict]) -> bytes:
    """Bar chart: daily AI Core CU for last N days."""
    if not daily_series:
        return b""

    dates  = [d["date"][-5:] for d in daily_series]   # MM-DD
    values = [d["total_cu"] for d in daily_series]
    avg    = sum(values) / len(values) if values else 0

    fig, ax = plt.subplots(figsize=(8, 3.5))
    colors = ["#e74c3c" if v > avg * 1.5 else "#3498db" for v in values]
    ax.bar(dates, values, color=colors, edgecolor="white", linewidth=0.5)
    ax.axhline(avg, color="#e67e22", linewidth=1.5, linestyle="--", label=f"Avg {avg:.0f} CU")
    ax.set_title("AI Core Daily CU Usage", fontsize=13, fontweight="bold", pad=10)
    ax.set_xlabel("Date")
    ax.set_ylabel("CU")
    ax.legend(fontsize=9)
    ax.yaxis.grid(True, alpha=0.3)
    ax.set_axisbelow(True)
    plt.xticks(rotation=45, ha="right", fontsize=8)
    plt.tight_layout()

    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=120, bbox_inches="tight")
    plt.close(fig)
    return buf.getvalue()


def _chart_model_pie(by_model: list[dict]) -> bytes:
    """Pie chart: CU share by AI model."""
    if not by_model:
        return b""

    # Cap at top 6 models, group the rest as "Other"
    top    = by_model[:6]
    other  = sum(m["total_cu"] for m in by_model[6:])
    labels = [m["model"].split("--")[-1][:20] for m in top]
    sizes  = [m["total_cu"] for m in top]
    if other > 0:
        labels.append("Other")
        sizes.append(other)

    colors = ["#3498db", "#2ecc71", "#e74c3c", "#f39c12", "#9b59b6", "#1abc9c", "#95a5a6"]

    fig, ax = plt.subplots(figsize=(6, 4))
    wedges, texts, autotexts = ax.pie(
        sizes, labels=None, autopct="%1.1f%%",
        colors=colors[:len(sizes)], startangle=140,
        wedgeprops={"edgecolor": "white", "linewidth": 1},
    )
    for at in autotexts:
        at.set_fontsize(8)
    ax.legend(
        wedges, labels,
        loc="center left", bbox_to_anchor=(1, 0.5),
        fontsize=8, frameon=False,
    )
    ax.set_title("CU Share by Model", fontsize=13, fontweight="bold")
    plt.tight_layout()

    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=120, bbox_inches="tight")
    plt.close(fig)
    return buf.getvalue()


def _chart_quota_progress(cu_used: float, contract_cu: float, projected: float) -> bytes:
    """Horizontal stacked bar: used / remaining / at-risk zone."""
    fig, ax = plt.subplots(figsize=(8, 1.8))

    used_pct      = min(cu_used / contract_cu, 1.0)
    projected_pct = min(projected / contract_cu, 1.0)

    ax.barh(["Quota"], [used_pct], color="#3498db", label=f"Used ({cu_used:,.0f} CU)")
    if projected_pct > used_pct:
        ax.barh(["Quota"], [projected_pct - used_pct], left=[used_pct],
                color="#f39c12", alpha=0.6, label=f"Projected ({projected:,.0f} CU)")
    ax.barh(["Quota"], [max(0, 1.0 - projected_pct)], left=[min(projected_pct, 1.0)],
            color="#ecf0f1", label=f"Contract ({contract_cu:,.0f} CU)")
    ax.axvline(1.0, color="#e74c3c", linewidth=2, linestyle="--")

    ax.set_xlim(0, max(1.1, projected_pct + 0.05))
    ax.xaxis.set_major_formatter(plt.FuncFormatter(lambda x, _: f"{x*100:.0f}%"))
    ax.set_title("Annual CU Quota Progress", fontsize=11, fontweight="bold")
    ax.legend(fontsize=8, loc="lower right")
    ax.yaxis.set_visible(False)
    plt.tight_layout()

    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=120, bbox_inches="tight")
    plt.close(fig)
    return buf.getvalue()


def _chart_anomaly_trend(daily_series: list[dict], anomaly_dates: set) -> bytes:
    """Line chart: daily CU trend with anomaly days highlighted in red."""
    if not daily_series:
        return b""

    dates  = [d["date"][-5:] for d in daily_series]
    values = [d["total_cu"] for d in daily_series]
    colors = ["#e74c3c" if d["date"] in anomaly_dates else "#3498db" for d in daily_series]

    fig, ax = plt.subplots(figsize=(8, 3.5))
    ax.plot(dates, values, color="#3498db", linewidth=2, zorder=2)
    ax.fill_between(range(len(dates)), values, alpha=0.15, color="#3498db")

    # Highlight anomaly dots
    for i, (d, v) in enumerate(zip(daily_series, values)):
        if d["date"] in anomaly_dates:
            ax.scatter(i, v, color="#e74c3c", s=80, zorder=3)

    ax.set_xticks(range(len(dates)))
    ax.set_xticklabels(dates, rotation=45, ha="right", fontsize=8)
    ax.set_title("AI Core CU Daily Trend (anomalies in red)", fontsize=12, fontweight="bold")
    ax.set_ylabel("CU")
    ax.yaxis.grid(True, alpha=0.3)
    ax.set_axisbelow(True)

    normal_patch  = mpatches.Patch(color="#3498db", label="Normal")
    anomaly_patch = mpatches.Patch(color="#e74c3c", label="Anomaly")
    ax.legend(handles=[normal_patch, anomaly_patch], fontsize=9)
    plt.tight_layout()

    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=120, bbox_inches="tight")
    plt.close(fig)
    return buf.getvalue()


# ── HTML builders ─────────────────────────────────────────────────────────────

_CSS = """
body { font-family: Arial, sans-serif; background: #f4f6f9; margin: 0; padding: 20px; }
.container { max-width: 700px; margin: auto; background: white;
             border-radius: 8px; overflow: hidden;
             box-shadow: 0 2px 8px rgba(0,0,0,0.1); }
.header { background: #2c3e50; color: white; padding: 24px 30px; }
.header h1 { margin: 0; font-size: 20px; }
.header p  { margin: 4px 0 0; opacity: 0.8; font-size: 13px; }
.badge-safe    { background: #27ae60; color: white; padding: 4px 12px;
                 border-radius: 12px; font-size: 12px; font-weight: bold; }
.badge-risk    { background: #e67e22; color: white; padding: 4px 12px;
                 border-radius: 12px; font-size: 12px; font-weight: bold; }
.badge-exceed  { background: #e74c3c; color: white; padding: 4px 12px;
                 border-radius: 12px; font-size: 12px; font-weight: bold; }
.section { padding: 20px 30px; border-bottom: 1px solid #ecf0f1; }
.section h2 { font-size: 15px; color: #2c3e50; margin: 0 0 14px;
              border-left: 4px solid #3498db; padding-left: 10px; }
table { width: 100%; border-collapse: collapse; font-size: 13px; }
th { background: #f8f9fa; color: #555; text-align: left;
     padding: 8px 10px; font-weight: 600; }
td { padding: 7px 10px; border-bottom: 1px solid #f0f0f0; }
tr:last-child td { border-bottom: none; }
.num { text-align: right; font-family: monospace; }
.ok   { color: #27ae60; font-weight: bold; }
.warn { color: #e67e22; font-weight: bold; }
.err  { color: #e74c3c; font-weight: bold; }
.chart { text-align: center; padding: 10px 0; }
.footer { padding: 16px 30px; background: #f8f9fa;
          font-size: 11px; color: #999; text-align: center; }
"""


def _status_badge(verdict: str) -> str:
    cls = {"SAFE": "badge-safe", "AT_RISK": "badge-risk", "WILL_EXCEED": "badge-exceed"}.get(verdict, "badge-risk")
    return f'<span class="{cls}">{verdict.replace("_", " ")}</span>'


def _status_class(status: str) -> str:
    return {"ON_TRACK": "ok", "SAFE": "ok", "AHEAD": "ok",
            "AT_RISK": "warn", "BEHIND": "warn",
            "OVER": "err", "WILL_EXCEED": "err"}.get(status, "")


def _build_report_html(
    from_date: str,
    to_date: str,
    services_rows: list[dict],
    aicore_by_model: list[dict],
    aicore_daily: list[dict],
    quota: Optional[dict],
    hana_summary: Optional[dict],
    chart_cids: dict,
) -> str:
    today_str = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    verdict   = quota.get("verdict", "SAFE") if quota else "SAFE"

    # ── Services table rows ───────────────────────────────────────────────────
    svc_rows_html = ""
    for row in services_rows[:20]:  # cap at 20 rows
        svc_rows_html += (
            f"<tr><td>{row['service']}</td><td>{row['metric']}</td>"
            f"<td class='num'>{row['total_usage']:,.2f}</td>"
            f"<td>{row['unit']}</td></tr>"
        )

    # ── AI Core model table rows ──────────────────────────────────────────────
    model_rows_html = ""
    for m in aicore_by_model[:8]:
        model_rows_html += (
            f"<tr><td>{m['model']}</td>"
            f"<td class='num'>{m['total_cu']:,.2f} CU</td></tr>"
        )

    # ── Quota section ─────────────────────────────────────────────────────────
    quota_html = ""
    if quota:
        tm   = quota.get("this_month", {})
        cum  = quota.get("cumulative", {})
        proj = quota.get("projection", {})
        quota_html = f"""
        <div class="section">
          <h2>Annual Quota Status</h2>
          {'<div class="chart"><img src="cid:chart_quota" width="640"></div>' if chart_cids.get("chart_quota") else ""}
          <table>
            <tr><th>Metric</th><th>Value</th><th>Status</th></tr>
            <tr>
              <td>This month ({tm.get('month','')})</td>
              <td class="num">{tm.get('used',0):,.1f} / {tm.get('target',0):,.1f} CU
                  ({tm.get('pct_used',0):.1f}%)</td>
              <td class="{_status_class(tm.get('status',''))}">{tm.get('status','')}</td>
            </tr>
            <tr>
              <td>Cumulative YTD</td>
              <td class="num">{cum.get('used',0):,.1f} / {cum.get('allowed',0):,.1f} CU</td>
              <td class="{_status_class(cum.get('status',''))}">{cum.get('status','')}</td>
            </tr>
            <tr>
              <td>Year-end projection</td>
              <td class="num">{proj.get('projected_annual',0):,.1f} CU</td>
              <td class="{_status_class(proj.get('status',''))}">{proj.get('status','')}</td>
            </tr>
          </table>
        </div>"""

    # ── HANA section ──────────────────────────────────────────────────────────
    hana_html = ""
    if hana_summary:
        hana_rows = ""
        for name, info in hana_summary.items():
            hana_rows += (
                f"<tr><td>{name}</td>"
                f"<td class='num'>{info.get('max_value', 'N/A')}</td>"
                f"<td>{info.get('unit','')}</td></tr>"
            )
        hana_html = f"""
        <div class="section">
          <h2>HANA Cloud Metrics (last 24h)</h2>
          <table>
            <tr><th>Metric</th><th class="num">Max Value</th><th>Unit</th></tr>
            {hana_rows}
          </table>
        </div>"""

    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8">
<style>{_CSS}</style>
</head><body>
<div class="container">
  <div class="header">
    <h1>BTP Daily Usage Report &nbsp; {_status_badge(verdict)}</h1>
    <p>Period: {from_date} to {to_date} &nbsp;|&nbsp; Generated: {today_str}</p>
  </div>

  <div class="section">
    <h2>AI Core CU — Daily Trend</h2>
    {'<div class="chart"><img src="cid:chart_daily" width="640"></div>' if chart_cids.get("chart_daily") else ""}
    <table>
      <tr><th>Model</th><th class="num">Total CU</th></tr>
      {model_rows_html}
    </table>
    {'<div class="chart"><img src="cid:chart_pie" width="480"></div>' if chart_cids.get("chart_pie") else ""}
  </div>

  {quota_html}

  {hana_html}

  <div class="section">
    <h2>All BTP Services</h2>
    <table>
      <tr><th>Service</th><th>Metric</th><th class="num">Usage</th><th>Unit</th></tr>
      {svc_rows_html}
    </table>
  </div>

  <div class="footer">
    BTP Usage Agent &nbsp;|&nbsp; Auto-generated report &nbsp;|&nbsp; {today_str}
  </div>
</div>
</body></html>"""


def _build_anomaly_html(anomaly_result: dict, chart_bytes: bytes) -> str:
    from_date = anomaly_result.get("from_date", "")
    to_date   = anomaly_result.get("to_date", "")
    algorithm = anomaly_result.get("algorithm_used", "")
    sensitivity = anomaly_result.get("sensitivity", "")
    total_anomalies   = anomaly_result.get("total_daily_anomalies", [])
    per_model         = anomaly_result.get("per_model_anomalies", {})
    today_str = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    anomaly_rows = ""
    for a in total_anomalies:
        anomaly_rows += (
            f"<tr><td>{a['date']}</td>"
            f"<td class='num err'>{a['value']:,.2f} CU</td>"
            f"<td>{a['direction'].upper()}</td>"
            f"<td class='num'>{a['score']:.2f} ({a['score_type']})</td>"
            f"<td>{a['pct_vs_mean']:+.1f}%</td></tr>"
        )

    model_rows = ""
    for model, anomalies in per_model.items():
        for a in anomalies:
            model_rows += (
                f"<tr><td>{model.split('--')[-1][:30]}</td>"
                f"<td>{a['date']}</td>"
                f"<td class='num err'>{a['value']:,.2f} CU</td>"
                f"<td class='num'>{a['score']:.2f}</td>"
                f"<td>{a.get('pct_vs_mean', a.get('pct_vs_median', 0)):+.1f}%</td></tr>"
            )

    chart_img = '<div class="chart"><img src="cid:chart_anomaly" width="640"></div>' if chart_bytes else ""

    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8">
<style>{_CSS}</style>
</head><body>
<div class="container">
  <div class="header" style="background:#c0392b;">
    <h1>&#9888; AI Core CU Anomaly Alert</h1>
    <p>Period: {from_date} to {to_date} &nbsp;|&nbsp;
       Algorithm: {algorithm} &nbsp;|&nbsp; Sensitivity: {sensitivity} &nbsp;|&nbsp;
       Generated: {today_str}</p>
  </div>

  <div class="section">
    <h2>Daily Trend with Anomaly Days Highlighted</h2>
    {chart_img}
  </div>

  {'<div class="section"><h2>Total Daily CU Anomalies</h2><table><tr><th>Date</th><th class="num">CU Value</th><th>Direction</th><th class="num">Score</th><th>vs Mean</th></tr>' + anomaly_rows + '</table></div>' if anomaly_rows else ''}

  {'<div class="section"><h2>Per-Model Anomalies</h2><table><tr><th>Model</th><th>Date</th><th class="num">CU Value</th><th class="num">Score</th><th>vs Mean</th></tr>' + model_rows + '</table></div>' if model_rows else ''}

  <div class="footer">
    BTP Usage Agent &nbsp;|&nbsp; Anomaly Alert &nbsp;|&nbsp; {today_str}
  </div>
</div>
</body></html>"""


# ── Core report builder (shared by all 3 email types) ─────────────────────────

async def _fetch_and_build_report(from_date: str, to_date: str) -> tuple[str, list[tuple[str, bytes]]]:
    """
    Fetch all data, generate charts, build HTML.
    Returns (html_string, [(cid, png_bytes), ...])
    """
    from uas_tool import get_btp_services_summary, get_aicore_model_cu_usage, check_quota_status
    from hana_tool import _hana_get, _default_time_range_24h, _resolve_service_instance_id

    inline_images: list[tuple[str, bytes]] = []
    chart_cids: dict[str, bool] = {}

    # ── 1. All BTP services summary ──────────────────────────────────────────
    try:
        svc_raw  = json.loads(await get_btp_services_summary.ainvoke({"from_date": from_date, "to_date": to_date}))
        svc_rows = svc_raw.get("detail", [])
    except Exception as exc:
        logger.warning("services summary failed: %s", exc)
        svc_rows = []

    # ── 2. AI Core model breakdown ───────────────────────────────────────────
    try:
        aicore_raw   = json.loads(await get_aicore_model_cu_usage.ainvoke(
            {"from_date": from_date, "to_date": to_date, "time_granularity": "day"}
        ))
        aicore_models  = aicore_raw.get("by_model", [])
        aicore_by_period = aicore_raw.get("by_period", [])
    except Exception as exc:
        logger.warning("aicore model usage failed: %s", exc)
        aicore_models    = []
        aicore_by_period = []

    # ── 3. Quota status ──────────────────────────────────────────────────────
    quota = None
    contract_cu    = float(os.environ.get("CONTRACT_CU", "0"))
    contract_start = os.environ.get("CONTRACT_START", "")
    contract_end   = os.environ.get("CONTRACT_END", "")
    if contract_cu and contract_start and contract_end:
        try:
            quota = json.loads(await check_quota_status.ainvoke({
                "contract_cu":    contract_cu,
                "contract_start": contract_start,
                "contract_end":   contract_end,
            }))
        except Exception as exc:
            logger.warning("quota status failed: %s", exc)

    # ── 4. HANA metrics (last 24h) ───────────────────────────────────────────
    hana_summary = None
    try:
        sid = _resolve_service_instance_id(None)
        s_ts, e_ts = _default_time_range_24h()
        hana_raw = await _hana_get(
            f"/metrics/v1/serviceInstances/{sid}/values",
            {"startTimestamp": s_ts, "endTimestamp": e_ts,
             "names": "HDBMemoryUsed,HDBCPU,HDBDiskUsed",
             "aggregates": "max", "interval": 3600},
        )
        hana_summary = {}
        units = {"HDBMemoryUsed": "bytes", "HDBCPU": "%", "HDBDiskUsed": "bytes"}
        for m in hana_raw.get("data", []):
            name   = m.get("name", "")
            values = m.get("values", [])
            if values:
                max_val = max((v.get("max", 0) for v in values), default=0)
                hana_summary[name] = {
                    "max_value": round(max_val / (1024**3), 2) if "Used" in name else round(max_val, 2),
                    "unit":      "GB" if "Used" in name else units.get(name, ""),
                }
    except Exception as exc:
        logger.warning("HANA metrics failed (non-fatal): %s", exc)

    # ── 5. Generate charts ───────────────────────────────────────────────────
    daily_series = aicore_by_period if aicore_by_period else []

    chart_daily = _chart_daily_cu(daily_series)
    if chart_daily:
        inline_images.append(("chart_daily", chart_daily))
        chart_cids["chart_daily"] = True

    # Flatten by_model for the no-breakdown totals
    model_totals = [{"model": m["model"], "total_cu": m["total_cu"]} for m in aicore_models]
    chart_pie = _chart_model_pie(model_totals)
    if chart_pie:
        inline_images.append(("chart_pie", chart_pie))
        chart_cids["chart_pie"] = True

    if quota:
        proj = quota.get("projection", {})
        chart_q = _chart_quota_progress(
            quota["cumulative"]["used"],
            quota["contract"]["contract_cu"],
            proj.get("projected_annual", 0),
        )
        if chart_q:
            inline_images.append(("chart_quota", chart_q))
            chart_cids["chart_quota"] = True

    # ── 6. Build HTML ────────────────────────────────────────────────────────
    html = _build_report_html(
        from_date, to_date,
        svc_rows, model_totals, daily_series,
        quota, hana_summary, chart_cids,
    )

    return html, inline_images


# ── LangChain Tool (LLM-callable) ─────────────────────────────────────────────

@tool
async def send_summary_email(
    from_date: str,
    to_date: str,
) -> str:
    """
    Generate a BTP usage summary report for a given date range and send it by email.

    Fetches AI Core CU usage, quota status, HANA metrics, and all BTP services
    data for the specified period, generates an HTML report with charts, and emails
    it to the configured recipients (EMAIL_TO env var).

    Args:
        from_date: Start date in YYYY-MM-DD format (e.g. "2026-06-01")
        to_date:   End date in YYYY-MM-DD format (e.g. "2026-06-17")

    Returns:
        Confirmation message with recipient list, or error description.
    """
    from_date = _validate_date(from_date, "from_date")
    to_date   = _validate_date(to_date,   "to_date")
    if from_date > to_date:
        from_date, to_date = to_date, from_date

    recipients = _get_recipients()
    if not recipients:
        return "Email not configured — set EMAIL_TO in your .env file."

    try:
        html, inline_images = await _fetch_and_build_report(from_date, to_date)
        subject = f"BTP Usage Report: {from_date} to {to_date}"
        _send_email(subject, html, inline_images)
        return f"Report sent to {', '.join(recipients)} covering {from_date} to {to_date}."
    except Exception as exc:
        logger.exception("send_summary_email failed")
        return f"Failed to send report: {exc}"


# ── Scheduler-facing functions (NOT @tool) ────────────────────────────────────

async def send_daily_report_email() -> None:
    """Called by APScheduler every morning. Sends yesterday's usage report."""
    yesterday = (datetime.now(tz=timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")
    try:
        html, inline_images = await _fetch_and_build_report(yesterday, yesterday)
        subject = f"BTP Daily Report — {yesterday}"
        _send_email(subject, html, inline_images)
        logger.info("Daily report email sent for %s", yesterday)
    except Exception as exc:
        logger.exception("Daily report email failed: %s", exc)


async def send_anomaly_alert_email(anomaly_result: dict) -> None:
    """Called by APScheduler when detect_aicore_cu_anomaly finds anomalies."""
    total_anomalies = anomaly_result.get("total_daily_anomalies", [])
    per_model       = anomaly_result.get("per_model_anomalies", {})

    anomaly_dates = {a["date"] for a in total_anomalies}
    for anoms in per_model.values():
        for a in anoms:
            anomaly_dates.add(a["date"])

    daily_series = anomaly_result.get("data_summary", {}).get("daily_series", [])
    chart_bytes  = _chart_anomaly_trend(daily_series, anomaly_dates)

    html = _build_anomaly_html(anomaly_result, chart_bytes)

    total_count = len(total_anomalies) + sum(len(v) for v in per_model.values())
    subject = f"⚠️ BTP AI Core Anomaly Alert — {total_count} anomaly(-ies) detected"

    inline_images = []
    if chart_bytes:
        inline_images.append(("chart_anomaly", chart_bytes))

    try:
        _send_email(subject, html, inline_images)
        logger.info("Anomaly alert email sent: %d anomalies", total_count)
    except Exception as exc:
        logger.exception("Anomaly alert email failed: %s", exc)
