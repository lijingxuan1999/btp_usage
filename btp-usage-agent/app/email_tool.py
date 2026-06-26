"""
BTP Usage Agent — Email Notification Module — v3 (pure-Python SVG charts)

Charts are rendered as inline SVG strings embedded directly in the HTML body.
No matplotlib, no Pillow, no native extensions required.

Required environment variables (.env):
  SMTP_HOST      = auth.mail.net.sap
  SMTP_PORT      = 587
  SMTP_USER      = hackathon-alerts
  SMTP_PASSWORD  = QAZwsx123!@#
  EMAIL_FROM     = noreply+btp_usage_hackathon@sap.corp
  EMAIL_TO       = jaye.li@sap.com

  CONTRACT_CU    = 100000
  CONTRACT_START = 2026-01-01
  CONTRACT_END   = 2026-12-31
"""

import json
import logging
import os
import smtplib
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Optional

from dotenv import load_dotenv
from langchain_core.tools import tool

from uas_tool import _validate_date

load_dotenv(override=False)
logger = logging.getLogger(__name__)

# ── Runtime credential override ───────────────────────────────────────────────
_runtime_email_config: dict = {}


def _get_cfg(key: str, default: str = "") -> str:
    return _runtime_email_config.get(key) or os.environ.get(key, default)


def _get_recipients() -> list[str]:
    raw = _get_cfg("EMAIL_TO", "jaye.li@sap.com")
    return [e.strip() for e in raw.split(",") if e.strip()]


# ── SMTP sender ───────────────────────────────────────────────────────────────

def _send_email(subject: str, html_body: str) -> None:
    """Send a pure-HTML email (no inline image attachments needed)."""
    import ssl as _ssl
    from email.utils import formatdate

    smtp_host = _get_cfg("SMTP_HOST", "")
    smtp_port = int(_get_cfg("SMTP_PORT", "587"))
    smtp_user = _get_cfg("SMTP_USER", "")
    smtp_pass = _get_cfg("SMTP_PASSWORD", "")
    from_addr = _get_cfg("EMAIL_FROM", "noreply+btp_usage_hackathon@sap.corp")
    to_addrs  = _get_recipients()

    if not smtp_host:
        raise ValueError("SMTP_HOST is not configured.")

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = from_addr
    msg["To"]      = ", ".join(to_addrs)
    msg["Date"]    = formatdate(localtime=False)
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    ctx = _ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode    = _ssl.CERT_NONE
    raw_msg = msg.as_bytes()

    if smtp_port == 465:
        logger.info("SMTP_SSL -> %s:%s", smtp_host, smtp_port)
        with smtplib.SMTP_SSL(smtp_host, smtp_port, context=ctx, timeout=30) as s:
            if smtp_user and smtp_pass:
                s.login(smtp_user, smtp_pass)
            s.sendmail(from_addr, to_addrs, raw_msg)
    elif smtp_port == 587:
        logger.info("SMTP STARTTLS -> %s:%s", smtp_host, smtp_port)
        with smtplib.SMTP(smtp_host, smtp_port, timeout=30) as s:
            s.ehlo()
            s.starttls(context=ctx)
            s.ehlo()
            if smtp_user and smtp_pass:
                s.login(smtp_user, smtp_pass)
            s.sendmail(from_addr, to_addrs, raw_msg)
    else:
        with smtplib.SMTP(smtp_host, smtp_port, timeout=30) as s:
            s.ehlo()
            if smtp_user and smtp_pass:
                s.login(smtp_user, smtp_pass)
            s.sendmail(from_addr, to_addrs, raw_msg)

    logger.info("Email sent to %s -- %s", to_addrs, subject)


# ============================================================================
# Pure-Python SVG / HTML chart generators
# No external libraries -- generates SVG markup as Python f-strings.
# ============================================================================

_PALETTE = ["#3498db", "#2ecc71", "#e74c3c", "#f39c12", "#9b59b6",
            "#1abc9c", "#e67e22", "#34495e", "#95a5a6", "#c0392b"]


def _svg_bar_chart(daily_series: list, title: str = "Daily AI Core CU Usage") -> str:
    """SVG bar chart for daily CU. Returns full HTML block with embedded SVG."""
    if not daily_series:
        return ""

    labels = [(d.get("date") or d.get("period") or "")[-5:] for d in daily_series]
    values = [float(d.get("total_cu") or d.get("cu") or 0) for d in daily_series]

    if not any(v > 0 for v in values):
        return ""

    W, H       = 620, 240
    pad_l      = 62
    pad_r      = 20
    pad_top    = 30
    pad_bot    = 50
    chart_w    = W - pad_l - pad_r
    chart_h    = H - pad_top - pad_bot
    n          = len(values)
    max_val    = max(values) or 1
    avg        = sum(values) / n if n else 0
    bar_w      = max(4, chart_w // n - 4)

    def x_pos(i):
        slot = chart_w / n
        return pad_l + i * slot + (slot - bar_w) / 2

    def y_bar(v):
        return pad_top + chart_h * (1 - v / max_val)

    def bar_h(v):
        return chart_h * v / max_val

    # Y-axis grid lines
    grid_lines = ""
    y_labels   = ""
    for i in range(5):
        v  = max_val * i / 4
        y  = pad_top + chart_h * (1 - i / 4)
        grid_lines += f'<line x1="{pad_l}" y1="{y:.1f}" x2="{W - pad_r}" y2="{y:.1f}" stroke="#e8e8e8" stroke-width="1"/>'
        lbl = f"{v/1000:.1f}k" if v >= 1000 else f"{v:.0f}"
        y_labels += f'<text x="{pad_l - 6}" y="{y + 4:.1f}" text-anchor="end" font-size="10" fill="#888">{lbl}</text>'

    # Bars
    bars = ""
    for i, (v, lbl) in enumerate(zip(values, labels)):
        color = "#e74c3c" if v > avg * 1.5 else "#3498db"
        bx    = x_pos(i)
        by_   = y_bar(v)
        bh    = bar_h(v)
        bars += f'<rect x="{bx:.1f}" y="{by_:.1f}" width="{bar_w}" height="{bh:.1f}" fill="{color}" rx="2"/>'
        if n <= 14 or i % 2 == 0:
            cx = bx + bar_w / 2
            bars += (
                f'<text x="{cx:.1f}" y="{H - pad_bot + 14}" text-anchor="middle" '
                f'font-size="9" fill="#666" '
                f'transform="rotate(-35 {cx:.1f} {H - pad_bot + 14})">{lbl}</text>'
            )

    # Average dashed line
    avg_y    = pad_top + chart_h * (1 - avg / max_val)
    avg_line = (
        f'<line x1="{pad_l}" y1="{avg_y:.1f}" x2="{W - pad_r}" y2="{avg_y:.1f}" '
        f'stroke="#e67e22" stroke-width="1.5" stroke-dasharray="5,3"/>'
        f'<text x="{W - pad_r - 4}" y="{avg_y - 4:.1f}" text-anchor="end" '
        f'font-size="9" fill="#e67e22">avg {avg:,.0f}</text>'
    )

    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" '
        f'style="font-family:Arial,sans-serif;background:white;border-radius:6px;">'
        f'<text x="{W//2}" y="18" text-anchor="middle" font-size="13" font-weight="bold" fill="#2c3e50">{title}</text>'
        f'{grid_lines}{avg_line}{bars}{y_labels}'
        f'<line x1="{pad_l}" y1="{pad_top}" x2="{pad_l}" y2="{pad_top + chart_h}" stroke="#ccc" stroke-width="1"/>'
        f'<line x1="{pad_l}" y1="{pad_top + chart_h}" x2="{W - pad_r}" y2="{pad_top + chart_h}" stroke="#ccc" stroke-width="1"/>'
        f'<rect x="{pad_l + 8}" y="{pad_top + 6}" width="10" height="4" fill="#e74c3c" rx="1"/>'
        f'<text x="{pad_l + 22}" y="{pad_top + 12}" font-size="9" fill="#666">Spike (&gt;1.5x avg)</text>'
        f'<rect x="{pad_l + 110}" y="{pad_top + 6}" width="10" height="4" fill="#3498db" rx="1"/>'
        f'<text x="{pad_l + 124}" y="{pad_top + 12}" font-size="9" fill="#666">Normal</text>'
        f'</svg>'
    )
    return f'<div style="margin:12px 0;text-align:center;">{svg}</div>'


def _svg_line_chart(daily_series: list, anomaly_dates: set,
                    title: str = "AI Core CU Trend") -> str:
    """SVG area/line chart with anomaly markers. Returns full HTML block."""
    if not daily_series:
        return ""

    labels     = [(d.get("date") or d.get("period") or "")[-5:] for d in daily_series]
    full_dates = [(d.get("date") or d.get("period") or "") for d in daily_series]
    values     = [float(d.get("total_cu") or 0) for d in daily_series]

    if not any(v > 0 for v in values):
        return ""

    W, H    = 620, 220
    pad_l   = 62
    pad_r   = 20
    pad_top = 30
    pad_bot = 50
    chart_w = W - pad_l - pad_r
    chart_h = H - pad_top - pad_bot
    n       = len(values)
    max_val = max(values) or 1

    def px(i):
        return pad_l + (i / (n - 1)) * chart_w if n > 1 else pad_l + chart_w / 2

    def py(v):
        return pad_top + chart_h * (1 - v / max_val)

    # Grid
    grid_lines = ""
    y_labels   = ""
    for i in range(5):
        v  = max_val * i / 4
        y  = pad_top + chart_h * (1 - i / 4)
        grid_lines += f'<line x1="{pad_l}" y1="{y:.1f}" x2="{W - pad_r}" y2="{y:.1f}" stroke="#eee" stroke-width="1"/>'
        lbl = f"{v/1000:.1f}k" if v >= 1000 else f"{v:.0f}"
        y_labels += f'<text x="{pad_l - 6}" y="{y + 4:.1f}" text-anchor="end" font-size="10" fill="#888">{lbl}</text>'

    # Points list
    pts = [(px(i), py(v)) for i, v in enumerate(values)]

    # Area fill
    base_y = pad_top + chart_h
    area_d = f"M {pts[0][0]:.1f},{pts[0][1]:.1f} " + " ".join(f"L {x:.1f},{y:.1f}" for x, y in pts[1:])
    area_d += f" L {pts[-1][0]:.1f},{base_y} L {pts[0][0]:.1f},{base_y} Z"
    area   = f'<path d="{area_d}" fill="#3498db" fill-opacity="0.15"/>'

    line_d = f"M {pts[0][0]:.1f},{pts[0][1]:.1f} " + " ".join(f"L {x:.1f},{y:.1f}" for x, y in pts[1:])
    line   = f'<path d="{line_d}" fill="none" stroke="#3498db" stroke-width="2"/>'

    # X labels
    x_labels = ""
    for i, lbl in enumerate(labels):
        if n <= 14 or i % 2 == 0:
            x = px(i)
            x_labels += (
                f'<text x="{x:.1f}" y="{H - pad_bot + 14}" text-anchor="middle" '
                f'font-size="9" fill="#666" '
                f'transform="rotate(-35 {x:.1f} {H - pad_bot + 14})">{lbl}</text>'
            )

    # Anomaly markers
    markers = ""
    for i, fd in enumerate(full_dates):
        if fd in anomaly_dates:
            x, y = pts[i]
            markers += f'<circle cx="{x:.1f}" cy="{y:.1f}" r="5" fill="#e74c3c" stroke="white" stroke-width="1.5"/>'

    legend = ""
    if anomaly_dates:
        legend = (
            f'<circle cx="{pad_l + 10}" cy="{pad_top + 10}" r="4" fill="#e74c3c"/>'
            f'<text x="{pad_l + 18}" y="{pad_top + 14}" font-size="9" fill="#666">Anomaly</text>'
        )

    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" '
        f'style="font-family:Arial,sans-serif;background:white;border-radius:6px;">'
        f'<text x="{W//2}" y="18" text-anchor="middle" font-size="13" font-weight="bold" fill="#2c3e50">{title}</text>'
        f'{grid_lines}{area}{line}{markers}{x_labels}{y_labels}{legend}'
        f'<line x1="{pad_l}" y1="{pad_top}" x2="{pad_l}" y2="{pad_top + chart_h}" stroke="#ccc" stroke-width="1"/>'
        f'<line x1="{pad_l}" y1="{pad_top + chart_h}" x2="{W - pad_r}" y2="{pad_top + chart_h}" stroke="#ccc" stroke-width="1"/>'
        f'</svg>'
    )
    return f'<div style="margin:12px 0;text-align:center;">{svg}</div>'


def _html_model_bars(by_model: list) -> str:
    """Horizontal bar chart rendered as HTML divs -- 100% email-safe, no images."""
    if not by_model:
        return ""

    top    = by_model[:8]
    max_cu = float(top[0]["total_cu"]) if top else 1
    if max_cu == 0:
        return ""

    rows = ""
    for i, m in enumerate(top):
        cu    = float(m["total_cu"])
        pct   = cu / max_cu * 100
        color = _PALETTE[i % len(_PALETTE)]
        # Show readable name: prefer the part before '--version' suffix
        raw_model = m["model"]
        label = raw_model.split("--")[0][:35] if "--" in raw_model else raw_model[:35]
        rows += (
            f'<tr>'
            f'<td style="padding:5px 8px;font-size:12px;color:#444;width:180px;'
            f'white-space:nowrap;overflow:hidden;text-overflow:ellipsis;">{label}</td>'
            f'<td style="padding:5px 4px;">'
            f'<div style="background:{color};width:{pct:.1f}%;min-width:4px;height:16px;border-radius:3px;"></div>'
            f'</td>'
            f'<td style="padding:5px 8px;font-size:12px;color:#555;text-align:right;width:90px;">{cu:,.1f} CU</td>'
            f'</tr>'
        )

    return (
        f'<div style="margin:12px 0;background:white;border-radius:6px;padding:12px 0;">'
        f'<p style="margin:0 0 10px 0;font-size:13px;font-weight:bold;color:#2c3e50;text-align:center;">'
        f'CU Share by Model</p>'
        f'<table style="width:100%;border-collapse:collapse;">{rows}</table>'
        f'</div>'
    )


def _html_quota_bar(cu_used: float, contract_cu: float, projected: float) -> str:
    """Horizontal stacked progress bar -- pure HTML/CSS."""
    if not contract_cu:
        return ""

    used_pct = min(cu_used / contract_cu * 100, 100)
    proj_pct = min(projected / contract_cu * 100, 100)
    extra    = max(0.0, proj_pct - used_pct)
    remain   = max(0.0, 100.0 - proj_pct)

    proj_color = "#27ae60" if proj_pct < 80 else ("#e67e22" if proj_pct < 100 else "#e74c3c")

    return (
        f'<div style="margin:12px 0;background:white;border-radius:6px;padding:16px;">'
        f'<p style="margin:0 0 8px 0;font-size:13px;font-weight:bold;color:#2c3e50;">Annual CU Quota Progress</p>'
        f'<div style="background:#ecf0f1;border-radius:8px;height:28px;overflow:hidden;">'
        f'<div style="float:left;background:#3498db;width:{used_pct:.1f}%;height:28px;" title="Used: {cu_used:,.0f} CU"></div>'
        f'<div style="float:left;background:#f39c12;opacity:0.8;width:{extra:.1f}%;height:28px;" title="Projected extra"></div>'
        f'<div style="float:left;background:#ecf0f1;width:{remain:.1f}%;height:28px;"></div>'
        f'</div>'
        f'<div style="margin-top:8px;font-size:11px;color:#666;">'
        f'<span style="margin-right:16px;">'
        f'<span style="display:inline-block;width:10px;height:10px;background:#3498db;border-radius:2px;margin-right:4px;vertical-align:middle;"></span>'
        f'Used: {cu_used:,.0f} CU ({used_pct:.1f}%)</span>'
        f'<span style="margin-right:16px;">'
        f'<span style="display:inline-block;width:10px;height:10px;background:#f39c12;border-radius:2px;margin-right:4px;vertical-align:middle;"></span>'
        f'Projected: {projected:,.0f} CU ({proj_pct:.1f}%)</span>'
        f'<span style="color:{proj_color};font-weight:bold;">Contract: {contract_cu:,.0f} CU</span>'
        f'</div>'
        f'</div>'
    )


# ============================================================================
# CSS stylesheet
# ============================================================================

_CSS = (
    "body{font-family:Arial,sans-serif;background:#f4f6f9;margin:0;padding:20px;}"
    ".wrap{max-width:680px;margin:auto;background:white;border-radius:8px;overflow:hidden;"
    "box-shadow:0 2px 8px rgba(0,0,0,.1);}"
    ".hdr{background:#2c3e50;color:white;padding:22px 28px;}"
    ".hdr h1{margin:0;font-size:19px;}"
    ".hdr p{margin:4px 0 0;opacity:.8;font-size:12px;}"
    ".badge-safe{background:#27ae60;color:white;padding:3px 10px;border-radius:10px;font-size:11px;font-weight:bold;}"
    ".badge-risk{background:#e67e22;color:white;padding:3px 10px;border-radius:10px;font-size:11px;font-weight:bold;}"
    ".badge-exceed{background:#e74c3c;color:white;padding:3px 10px;border-radius:10px;font-size:11px;font-weight:bold;}"
    ".sec{padding:18px 28px;border-bottom:1px solid #ecf0f1;}"
    ".sec h2{font-size:14px;color:#2c3e50;margin:0 0 12px;border-left:4px solid #3498db;padding-left:9px;}"
    "table{width:100%;border-collapse:collapse;font-size:13px;}"
    "th{background:#f8f9fa;color:#555;text-align:left;padding:7px 9px;font-weight:600;}"
    "td{padding:6px 9px;border-bottom:1px solid #f0f0f0;}"
    "tr:last-child td{border-bottom:none;}"
    ".num{text-align:right;font-family:monospace;}"
    ".ok{color:#27ae60;font-weight:bold;}.warn{color:#e67e22;font-weight:bold;}.err{color:#e74c3c;font-weight:bold;}"
    ".ftr{padding:14px 28px;background:#f8f9fa;font-size:11px;color:#999;text-align:center;}"
)


# ============================================================================
# HTML builders
# ============================================================================

def _badge(verdict: str) -> str:
    cls = {"SAFE": "badge-safe", "AT_RISK": "badge-risk", "WILL_EXCEED": "badge-exceed"}.get(verdict, "badge-risk")
    return f'<span class="{cls}">{verdict.replace("_", " ")}</span>'


def _sc(status: str) -> str:
    return {"ON_TRACK": "ok", "SAFE": "ok", "AHEAD": "ok",
            "AT_RISK": "warn", "BEHIND": "warn",
            "OVER": "err", "WILL_EXCEED": "err"}.get(status, "")


def _build_report_html(
    from_date: str,
    to_date: str,
    services_rows: list,
    aicore_by_model: list,
    aicore_daily: list,
    quota,
    hana_summary,
) -> str:
    today_str = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    verdict   = quota.get("verdict", "SAFE") if quota else "SAFE"

    bar_chart_html  = _svg_bar_chart(aicore_daily)
    model_bars_html = _html_model_bars(aicore_by_model)

    quota_bar_html   = ""
    quota_table_html = ""
    if quota:
        tm   = quota.get("this_month", {})
        cum  = quota.get("cumulative", {})
        proj = quota.get("projection", {})
        quota_bar_html = _html_quota_bar(
            cum.get("used", 0),
            quota.get("contract", {}).get("contract_cu", 0),
            proj.get("projected_annual", 0),
        )
        quota_table_html = (
            f'<table><tr><th>Metric</th><th>Value</th><th>Status</th></tr>'
            f'<tr><td>This month ({tm.get("month","")})</td>'
            f'<td class="num">{tm.get("used",0):,.1f} / {tm.get("target",0):,.1f} CU ({tm.get("pct_used",0):.1f}%)</td>'
            f'<td class="{_sc(tm.get("status",""))}">{tm.get("status","")}</td></tr>'
            f'<tr><td>Cumulative YTD</td>'
            f'<td class="num">{cum.get("used",0):,.1f} / {cum.get("allowed",0):,.1f} CU</td>'
            f'<td class="{_sc(cum.get("status",""))}">{cum.get("status","")}</td></tr>'
            f'<tr><td>Year-end projection</td>'
            f'<td class="num">{proj.get("projected_annual",0):,.1f} CU</td>'
            f'<td class="{_sc(proj.get("status",""))}">{proj.get("status","")}</td></tr>'
            f'</table>'
        )

    model_table_rows = ""
    for m in aicore_by_model[:8]:
        model_table_rows += (
            f'<tr><td>{m["model"]}</td>'
            f'<td class="num">{m["total_cu"]:,.2f} CU</td></tr>'
        )

    svc_rows_html = ""
    for row in services_rows[:20]:
        svc_rows_html += (
            f'<tr><td>{row["service"]}</td><td>{row["metric"]}</td>'
            f'<td class="num">{row["total_usage"]:,.2f}</td>'
            f'<td>{row["unit"]}</td></tr>'
        )

    hana_section = ""
    if hana_summary:
        hana_rows = ""
        for name, info in hana_summary.items():
            hana_rows += (
                f'<tr><td>{name}</td>'
                f'<td class="num">{info.get("max_value","N/A")}</td>'
                f'<td>{info.get("unit","")}</td></tr>'
            )
        hana_section = (
            f'<div class="sec"><h2>HANA Cloud Metrics (last 24h)</h2>'
            f'<table><tr><th>Metric</th><th class="num">Max Value</th><th>Unit</th></tr>'
            f'{hana_rows}</table></div>'
        )

    quota_section = ""
    if quota:
        quota_section = (
            f'<div class="sec"><h2>Annual Quota Status</h2>'
            f'{quota_bar_html}{quota_table_html}</div>'
        )

    return (
        f'<!DOCTYPE html><html><head><meta charset="utf-8"><style>{_CSS}</style></head>'
        f'<body><div class="wrap">'
        f'<div class="hdr">'
        f'<h1>BTP Daily Usage Report &nbsp; {_badge(verdict)}</h1>'
        f'<p>Period: {from_date} to {to_date} &nbsp;|&nbsp; Generated: {today_str}</p>'
        f'</div>'
        f'<div class="sec"><h2>AI Core -- Daily CU Usage</h2>'
        f'{bar_chart_html}'
        f'<table><tr><th>Model</th><th class="num">Total CU</th></tr>{model_table_rows}</table>'
        f'{model_bars_html}</div>'
        f'{quota_section}'
        f'{hana_section}'
        f'<div class="sec"><h2>All BTP Services</h2>'
        f'<table><tr><th>Service</th><th>Metric</th><th class="num">Usage</th><th>Unit</th></tr>'
        f'{svc_rows_html}</table></div>'
        f'<div class="ftr">BTP Usage Agent &nbsp;|&nbsp; Auto-generated &nbsp;|&nbsp; {today_str}</div>'
        f'</div></body></html>'
    )


def _build_anomaly_html(anomaly_result: dict) -> str:
    from_date       = anomaly_result.get("from_date", "")
    to_date         = anomaly_result.get("to_date", "")
    algorithm       = anomaly_result.get("algorithm_used", "")
    sensitivity     = anomaly_result.get("sensitivity", "")
    total_anomalies = anomaly_result.get("total_daily_anomalies", [])
    per_model       = anomaly_result.get("per_model_anomalies", {})
    today_str       = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    daily_series  = anomaly_result.get("data_summary", {}).get("daily_series", [])
    anomaly_dates = {a["date"] for a in total_anomalies}
    for anoms in per_model.values():
        for a in anoms:
            anomaly_dates.add(a["date"])

    trend_chart = _svg_line_chart(daily_series, anomaly_dates,
                                  title="AI Core CU Trend (anomalies in red)")

    anomaly_rows = ""
    for a in total_anomalies:
        anomaly_rows += (
            f'<tr><td>{a["date"]}</td>'
            f'<td class="num err">{a["value"]:,.2f} CU</td>'
            f'<td>{a["direction"].upper()}</td>'
            f'<td class="num">{a["score"]:.2f} ({a["score_type"]})</td>'
            f'<td>{a["pct_vs_mean"]:+.1f}%</td></tr>'
        )

    model_rows = ""
    for model, anomalies in per_model.items():
        for a in anomalies:
            model_rows += (
                f'<tr><td>{(model.split("--")[0] if "--" in model else model)[:30]}</td>'
                f'<td>{a["date"]}</td>'
                f'<td class="num err">{a["value"]:,.2f} CU</td>'
                f'<td class="num">{a["score"]:.2f}</td>'
                f'<td>{a.get("pct_vs_mean", a.get("pct_vs_median", 0)):+.1f}%</td></tr>'
            )

    trend_sec = f'<div class="sec"><h2>Daily Trend with Anomalies</h2>{trend_chart}</div>' if trend_chart else ""
    anom_sec  = (f'<div class="sec"><h2>Daily CU Anomalies</h2>'
                 f'<table><tr><th>Date</th><th class="num">CU</th><th>Direction</th>'
                 f'<th class="num">Score</th><th>vs Mean</th></tr>{anomaly_rows}</table></div>') if anomaly_rows else ""
    model_sec = (f'<div class="sec"><h2>Per-Model Anomalies</h2>'
                 f'<table><tr><th>Model</th><th>Date</th><th class="num">CU</th>'
                 f'<th class="num">Score</th><th>vs Mean</th></tr>{model_rows}</table></div>') if model_rows else ""

    return (
        f'<!DOCTYPE html><html><head><meta charset="utf-8"><style>{_CSS}</style></head>'
        f'<body><div class="wrap">'
        f'<div class="hdr" style="background:#c0392b;">'
        f'<h1>&#9888; AI Core CU Anomaly Alert</h1>'
        f'<p>Period: {from_date} to {to_date} &nbsp;|&nbsp; '
        f'Algorithm: {algorithm} &nbsp;|&nbsp; Sensitivity: {sensitivity} &nbsp;|&nbsp; {today_str}</p>'
        f'</div>'
        f'{trend_sec}{anom_sec}{model_sec}'
        f'<div class="ftr">BTP Usage Agent &nbsp;|&nbsp; Anomaly Alert &nbsp;|&nbsp; {today_str}</div>'
        f'</div></body></html>'
    )


# ============================================================================
# Report data fetcher
# ============================================================================

async def _fetch_and_build_report(from_date: str, to_date: str) -> str:
    """Fetch all BTP data, generate charts, return complete HTML string."""
    from uas_tool import get_btp_services_summary, get_aicore_model_cu_usage, check_quota_status

    # 1. BTP services
    try:
        svc_raw  = json.loads(await get_btp_services_summary.ainvoke({"from_date": from_date, "to_date": to_date}))
        svc_rows = svc_raw.get("detail", [])
    except Exception as exc:
        logger.warning("services summary failed: %s", exc)
        svc_rows = []

    # 2. AI Core usage
    try:
        aicore_raw    = json.loads(await get_aicore_model_cu_usage.ainvoke(
            {"from_date": from_date, "to_date": to_date, "time_granularity": "day"}
        ))
        aicore_models  = aicore_raw.get("by_model", [])
        aicore_periods = aicore_raw.get("by_period", [])
    except Exception as exc:
        logger.warning("aicore usage failed: %s", exc)
        aicore_models  = []
        aicore_periods = []

    daily_series = [
        {"date": d.get("date") or d.get("period") or "", "total_cu": d.get("total_cu", 0)}
        for d in aicore_periods
    ]

    # 3. Quota
    quota          = None
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
            logger.warning("quota failed: %s", exc)

    # 4. HANA (optional)
    hana_summary = None
    try:
        from hana_tool import _hana_get, _default_time_range_24h, _resolve_service_instance_id
        sid = _resolve_service_instance_id(None)
        s_ts, e_ts = _default_time_range_24h()
        hana_raw = await _hana_get(
            f"/metrics/v1/serviceInstances/{sid}/values",
            {"startTimestamp": s_ts, "endTimestamp": e_ts,
             "names": "HDBMemoryUsed,HDBCPU,HDBDiskUsed",
             "aggregates": "max", "interval": 3600},
        )
        hana_summary = {}
        for m in hana_raw.get("data", []):
            name   = m.get("name", "")
            vals   = m.get("values", [])
            if vals:
                max_val = max((v.get("max", 0) for v in vals), default=0)
                hana_summary[name] = {
                    "max_value": round(max_val / (1024 ** 3), 2) if "Used" in name else round(max_val, 2),
                    "unit":      "GB" if "Used" in name else "%",
                }
    except Exception as exc:
        logger.warning("HANA metrics failed (non-fatal): %s", exc)

    model_totals = [{"model": m["model"], "total_cu": m["total_cu"]} for m in aicore_models]

    return _build_report_html(
        from_date, to_date,
        svc_rows, model_totals, daily_series,
        quota, hana_summary,
    )


# ============================================================================
# LangChain Tools
# ============================================================================

@tool
def set_email_config(
    smtp_host: Optional[str] = None,
    smtp_port: Optional[int] = None,
    smtp_user: Optional[str] = None,
    smtp_password: Optional[str] = None,
    email_from: Optional[str] = None,
    email_to: Optional[str] = None,
) -> str:
    """
    Update SMTP email configuration at runtime without restarting the agent.
    All parameters are optional -- only provided values are updated.

    Args:
        smtp_host:     SMTP server hostname
        smtp_port:     SMTP port (25, 465, or 587)
        smtp_user:     SMTP login username
        smtp_password: SMTP password or API key
        email_from:    Sender address
        email_to:      Recipient(s), comma-separated
    Returns:
        Summary of active configuration.
    """
    if smtp_host     is not None: _runtime_email_config["SMTP_HOST"]     = smtp_host
    if smtp_port     is not None: _runtime_email_config["SMTP_PORT"]     = str(smtp_port)
    if smtp_user     is not None: _runtime_email_config["SMTP_USER"]     = smtp_user
    if smtp_password is not None: _runtime_email_config["SMTP_PASSWORD"] = smtp_password
    if email_from    is not None: _runtime_email_config["EMAIL_FROM"]    = email_from
    if email_to      is not None: _runtime_email_config["EMAIL_TO"]      = email_to

    current = {
        "SMTP_HOST":     _get_cfg("SMTP_HOST", "auth.mail.net.sap"),
        "SMTP_PORT":     _get_cfg("SMTP_PORT", "587"),
        "SMTP_USER":     _get_cfg("SMTP_USER", ""),
        "SMTP_PASSWORD": "***" if _get_cfg("SMTP_PASSWORD") else "<not set>",
        "EMAIL_FROM":    _get_cfg("EMAIL_FROM", ""),
        "EMAIL_TO":      _get_cfg("EMAIL_TO", ""),
    }
    return (
        f"Email config updated. Runtime overrides: {list(_runtime_email_config.keys()) or 'none'}\n\n"
        "Active configuration:\n"
        + "\n".join(f"  {k} = {v}" for k, v in current.items())
        + "\n\nCall send_test_email() to verify."
    )


@tool
async def send_test_email() -> str:
    """
    Send a plain-text test email to verify SMTP configuration.
    Use this after set_email_config() to confirm credentials work.
    Returns: confirmation or a detailed error message.
    """
    recipients = _get_recipients()
    if not recipients:
        return "EMAIL_TO is not configured. Call set_email_config(email_to='you@example.com') first."

    smtp_host = _get_cfg("SMTP_HOST", "auth.mail.net.sap")
    smtp_port = int(_get_cfg("SMTP_PORT", "587"))
    smtp_user = _get_cfg("SMTP_USER", "")
    smtp_pass = _get_cfg("SMTP_PASSWORD", "")
    from_addr = _get_cfg("EMAIL_FROM", "noreply+btp_usage_hackathon@sap.corp")

    import ssl as _ssl
    from email.utils import formatdate

    ctx = _ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode    = _ssl.CERT_NONE

    body = (
        f"Hello,\n\nThis is a test email from your BTP Usage Agent.\n"
        f"SMTP={smtp_host}:{smtp_port}  From={from_addr}  To={', '.join(recipients)}\n\n"
        f"-- BTP Usage Agent"
    )
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = "BTP Usage Agent -- Email Test"
    msg["From"]    = from_addr
    msg["To"]      = ", ".join(recipients)
    msg["Date"]    = formatdate(localtime=False)
    raw = msg.as_bytes()

    try:
        if smtp_port == 465:
            with smtplib.SMTP_SSL(smtp_host, smtp_port, context=ctx, timeout=15) as s:
                if smtp_user and smtp_pass:
                    s.login(smtp_user, smtp_pass)
                s.sendmail(from_addr, recipients, raw)
        else:
            with smtplib.SMTP(smtp_host, smtp_port, timeout=15) as s:
                s.ehlo()
                s.starttls(context=ctx)
                s.ehlo()
                if smtp_user and smtp_pass:
                    s.login(smtp_user, smtp_pass)
                s.sendmail(from_addr, recipients, raw)
        return f"Test email sent to: {', '.join(recipients)}"
    except Exception as e:
        return f"FAILED: {type(e).__name__}: {e}"


@tool
def test_smtp_relay() -> str:
    """
    Probe candidate SMTP relay hosts via TCP connect to find which one is
    reachable from this container. Use when email delivery fails with network errors.
    Returns: table of SUCCESS/FAIL results with recommendation.
    """
    import socket
    candidates = [
        ("auth.mail.net.sap", 587), ("auth.mail.net.sap", 465),
        ("smtprelay.sap.corp", 25), ("smtprelay.sap.corp", 587),
        ("mailout.sap.corp",   25), ("relay.sap.com",     587),
        ("smtp.sendgrid.net",  587),
    ]
    results   = ["=== SMTP Relay Probe ===", ""]
    reachable = []
    for host, port in candidates:
        try:
            s = socket.create_connection((host, port), timeout=6)
            banner = b""
            s.settimeout(3)
            try:
                banner = s.recv(256).rstrip()
            except Exception:
                pass
            s.close()
            results.append(f"  REACHABLE  {host}:{port}  -- {banner[:80].decode('utf-8','replace')}")
            reachable.append((host, port))
        except socket.gaierror as e:
            results.append(f"  DNS FAIL   {host}:{port}  -- {e}")
        except Exception as e:
            results.append(f"  FAILED     {host}:{port}  -- {type(e).__name__}: {e}")
    results.append("")
    if reachable:
        results.append(f"RECOMMENDATION: use {reachable[0][0]}:{reachable[0][1]}")
    else:
        results.append("NONE reachable -- contact SAP IT for outbound SMTP relay.")
    return "\n".join(results)


@tool
def debug_email_config() -> str:
    """
    Diagnostic: show the SMTP configuration seen by the running container and
    attempt a TCP connection to the SMTP host. Use this to troubleshoot email
    delivery problems.
    Returns: config summary and TCP connectivity result.
    """
    import socket
    smtp_host = _get_cfg("SMTP_HOST", "<not set>")
    smtp_port_raw = _get_cfg("SMTP_PORT", "587")
    smtp_user = _get_cfg("SMTP_USER", "<not set>")
    smtp_pass = _get_cfg("SMTP_PASSWORD", "")
    from_addr = _get_cfg("EMAIL_FROM", "<not set>")
    to_addr   = _get_cfg("EMAIL_TO",   "<not set>")

    lines = [
        "=== Runtime Email Config ===",
        f"  SMTP_HOST     : {smtp_host}",
        f"  SMTP_PORT     : {smtp_port_raw}",
        f"  SMTP_USER     : {smtp_user}",
        f"  SMTP_PASSWORD : {'***' if smtp_pass else '<not set>'}",
        f"  EMAIL_FROM    : {from_addr}",
        f"  EMAIL_TO      : {to_addr}",
        f"  Chart engine  : pure-Python SVG (no matplotlib/Pillow required)",
        "",
    ]
    port = int(smtp_port_raw) if smtp_port_raw.isdigit() else 587
    try:
        sock = socket.create_connection((smtp_host, port), timeout=8)
        sock.close()
        lines.append(f"TCP {smtp_host}:{port} -> REACHABLE")
    except Exception as exc:
        lines.append(f"TCP {smtp_host}:{port} -> FAILED: {type(exc).__name__}: {exc}")
    return "\n".join(lines)


@tool
async def send_summary_email(from_date: str, to_date: str) -> str:
    """
    Generate a BTP usage report for the given date range and send it by email.

    The HTML report includes inline SVG charts (no images, no external dependencies):
      - Bar chart: AI Core daily CU usage with average line and spike highlights
      - Horizontal bars: per-model CU breakdown
      - Progress bar: annual quota used vs projected vs contract limit
      - Tables: HANA Cloud metrics and all BTP services usage

    Args:
        from_date: Start date in YYYY-MM-DD format (e.g. "2026-06-01")
        to_date:   End date in YYYY-MM-DD format (e.g. "2026-06-17")
    Returns:
        Confirmation with recipient list, or error description.
    """
    from_date = _validate_date(from_date, "from_date")
    to_date   = _validate_date(to_date,   "to_date")
    if from_date > to_date:
        from_date, to_date = to_date, from_date

    recipients = _get_recipients()
    if not recipients:
        return "Email not configured -- set EMAIL_TO in .env or call set_email_config()."

    try:
        html = await _fetch_and_build_report(from_date, to_date)
        _send_email(f"BTP Usage Report: {from_date} to {to_date}", html)
        return f"Report sent to {', '.join(recipients)} covering {from_date} to {to_date}."
    except Exception as exc:
        import traceback
        logger.error("send_summary_email failed:\n%s", traceback.format_exc())
        return f"Failed to send report: {type(exc).__name__}: {exc}"


# ============================================================================
# Scheduler-facing functions (NOT @tool -- called by APScheduler)
# ============================================================================

async def send_daily_report_email() -> None:
    """Called by APScheduler every morning to send yesterday's report."""
    yesterday = (datetime.now(tz=timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")
    try:
        html = await _fetch_and_build_report(yesterday, yesterday)
        _send_email(f"BTP Daily Report -- {yesterday}", html)
        logger.info("Daily report sent for %s", yesterday)
    except Exception as exc:
        logger.exception("Daily report failed: %s", exc)


async def send_anomaly_alert_email(anomaly_result: dict) -> None:
    """Called by APScheduler when detect_aicore_cu_anomaly finds anomalies."""
    total_anomalies = anomaly_result.get("total_daily_anomalies", [])
    per_model       = anomaly_result.get("per_model_anomalies", {})
    total_count     = len(total_anomalies) + sum(len(v) for v in per_model.values())
    html = _build_anomaly_html(anomaly_result)
    try:
        _send_email(f"BTP AI Core Anomaly Alert -- {total_count} anomaly(-ies) detected", html)
        logger.info("Anomaly alert sent: %d anomalies", total_count)
    except Exception as exc:
        logger.exception("Anomaly alert failed: %s", exc)
