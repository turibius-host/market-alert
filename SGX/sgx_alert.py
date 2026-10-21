"""
SGX Market-Moving Announcement Alerter
========================================
What this does
---------------
1. Pulls the latest rows from SGX's Company Announcements page.
2. Runs each announcement's title/category text through a keyword-rule
   engine that flags the categories most likely to move a share price
   the next trading day (takeovers, trading halts, profit warnings,
   rights issues, SGX queries, etc).
3. Keeps a local "seen" store so it only processes NEW announcements
   each run.
4. Writes/updates a local webpage (sgx_dashboard.html) listing every
   market-moving announcement flagged so far, with the newest-run hits
   highlighted. Just open that file in your browser to check it.

Why it's built this way
------------------------
SGX's site sits behind bot-protection that specifically appears to
detect and block fully-invisible ("headless") browsers -- a visible
browser window gets the real content, a headless one gets an empty
page. So this script drives a real (visible) Chromium window via
Playwright. A brief Chrome window popping up each run is expected
behavior, not a bug.

Setup
-----
    pip install playwright
    python -m playwright install chromium

Fill in MAX_HISTORY below if you want, then either:
  - run it manually: python sgx_alert.py
  - or schedule it (Windows Task Scheduler) every 15-30 min
    during SGX trading hours (9:00-17:00 SGT, Mon-Fri), with
    "Run only when user is logged on" (a visible browser needs
    an active desktop session).

Each run only adds NEW announcements to the dashboard, so running it
every 15 min will not create duplicate entries.
"""
import json
import os
import re
import smtplib
import ssl
import sys
from dataclasses import dataclass, field
from email.mime.text import MIMEText
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo

# ============================== CONFIG ==================================
SGX_URL = "https://www.sgx.com/stock-exchange/company-announcements"
SEEN_STORE = Path(__file__).parent / "sgx_seen.json"
HITS_STORE = Path(__file__).parent / "sgx_hits.json"
# The workflow sets SGX_DASHBOARD_FILE so the page is written outside the repo
# folder, then published to the "state" branch next to the market alert.
DASHBOARD_FILE = Path(os.environ.get("SGX_DASHBOARD_FILE",
                                     Path(__file__).parent / "index.html"))

# How many flagged announcements to keep in the dashboard's history.
MAX_HISTORY = 500


def now_sgt() -> datetime:
    return datetime.now(ZoneInfo("Asia/Singapore"))

# --- Email (push-to-phone via Gmail) --------------------------------------
# Gmail app password: https://myaccount.google.com/apppasswords
# (Outlook.com no longer supports app-password SMTP for sending, so we
# use Gmail here even if you also read the alerts in an Outlook inbox.)
SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 587
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "")
ALERT_FROM = SMTP_USER
ALERT_TO = os.environ.get("ALERT_TO", SMTP_USER)

# ========================== KEYWORD RULE ENGINE ==========================
#
# Each rule = a category of announcement that historically tends to move
# a share price the next day. `weight` is a rough "how market-moving"
# score (3 = usually very significant, 1 = situational/context-dependent)
# so you can tune MIN_WEIGHT_TO_ALERT below without editing regexes.

@dataclass
class Rule:
    name: str
    patterns: list
    weight: int
    note: str = ""


RULES = [
    Rule(
        name="Takeover / Privatisation Offer",
        patterns=[
            r"\b(mandatory|voluntary)\s+(tender|acquisition|takeover|purchase)\s+offer",
            r"scheme of arrangement",
            r"\bprivatis(e|ation)\b|\bprivatiz(e|ation)\b",
            r"exit offer",
            r"delisting",
        ],
        weight=3,
        note="Offer price is usually at a premium to last close.",
    ),
    Rule(
        name="SGX Query on Unusual Price/Volume",
        patterns=[
            r"query from sgx",
            r"unusual (price|trading|volume) (movement|activity)",
            r"trading query",
            r"response to (sgx )?query",
        ],
        weight=3,
        note="Leading indicator -- often precedes the actual disclosure.",
    ),
    Rule(
        name="Trading Halt / Suspension",
        patterns=[
            r"trading halt",
            r"suspension of trading",
            r"halt (lifted|withdrawn)",
            r"resumption of trading",
        ],
        weight=3,
        note="Pent-up reaction tends to gap on reopening.",
    ),
    Rule(
        name="Profit Guidance / Profit Warning",
        patterns=[
            r"profit (guidance|warning|alert)",
            r"expects? (a )?(net )?loss",
            r"negative (results|earnings)",
        ],
        weight=3,
    ),
    Rule(
        name="Rights Issue / Placement / Fundraising",
        patterns=[
            r"rights issue",
            r"private placement",
            r"placement of (up to )?[\d,]*\s*new (ordinary )?shares",
            r"\bplacement\b.{0,60}\bshares\b",
            r"proposed (equity fund[- ]raising|share issue)",
        ],
        weight=2,
        note="Often a discount to last close -> usually pressures price down.",
    ),
    Rule(
        name="Credit Rating / Going Concern / Audit Qualification",
        patterns=[
            r"credit rating",
            r"downgrade(d)?",
            r"going concern",
            r"qualified opinion",
            r"disclaimer of opinion",
        ],
        weight=3,
    ),
    Rule(
        name="Major Contract / Litigation",
        patterns=[
            r"contract (awarded|secured|terminated|termination)",
            r"awarded.{0,40}(contract|tender)",
            r"award(ed)? of tender",
            r"letter of award",
            r"writ of summons",
            r"legal proceedings",
            r"arbitration",
        ],
        weight=2,
    ),
    Rule(
        name="Key Executive Resignation",
        patterns=[
            r"resignation.{0,40}with immediate effect",
            r"cessation of (chairman|chief executive|ceo|cfo|managing director|director)",
        ],
        weight=2,
        note="Weight depends heavily on whether a reason is given.",
    ),
    Rule(
        name="Financial Results Release",
        patterns=[
            r"full year(ly)? results",
            r"half year(ly)? results",
            r"quarterly results",
            r"interim results",
            r"financial statements and related announcement.*results",
        ],
        weight=2,
        note="Actual numbers can surprise vs expectations even with no explicit warning.",
    ),
    Rule(
        name="Trading / Business / Delivery Update",
        patterns=[
            r"delivery update",
            r"trading update",
            r"business update",
            r"production update",
            r"sales update",
            r"operational update",
            r"\b[1-4]q\d{0,4}\s*(business )?update",
        ],
        weight=2,
        note="Sounds routine, but numbers inside (e.g. delivery counts) can move price, especially for closely-watched names.",
    ),
    Rule(
        name="Major Asset Acquisition / Disposal",
        patterns=[
            r"disposal of",
            r"divest(s|ment|ure)?",
            r"acquisition of",
            r"asset acquisitions and disposals",
            r"sale of.{0,40}(propert|asset|building|land)",
        ],
        weight=2,
        note="Materiality varies a lot by deal size -- use judgement before treating as a strong signal.",
    ),
    Rule(
        name="MOU / Strategic Investment / Partnership",
        patterns=[
            r"memorandum of understanding",
            r"\bmou(s)?\b",
            r"substantial investment",
            r"strategic (partnership|investment|collaboration)",
        ],
        weight=2,
        note="Often early-stage/non-binding -- a leading indicator rather than a done deal.",
    ),
    Rule(
        name="Share Buyback",
        patterns=[
            r"share buyback",
            r"buy-?back program",
            r"commences.{0,30}buyback",
        ],
        weight=2,
    ),
    Rule(
        name="Bonus Issue / Capitalisation Issue",
        patterns=[
            r"bonus issue",
            r"capitalisation issue",
            r"capitalization issue",
        ],
        weight=2,
    ),
]

MIN_WEIGHT_TO_ALERT = 2  # ignore weight-1 rules if you add any later

# =========================== DATA STRUCTURES =============================

@dataclass
class Announcement:
    id: str  # stable identifier (SGX reference / URL)
    company: str
    title: str
    category: str
    timestamp: str
    url: str
    security: str = ""
    matches: list = field(default_factory=list)  # filled in by classify()


# ============================ FETCH (Playwright) ==========================

def fetch_announcements(max_rows: int = 60) -> list:
    """
    Loads the SGX announcements page in a real headless browser and
    extracts the visible rows.

    NOTE: SGX's page is a JS single-page app, so the exact CSS
    selectors below may need adjusting if SGX changes their frontend.
    Run once with headless=False the first time to visually confirm
    the selectors still match, then switch back to headless=True.
    """
    from playwright.sync_api import sync_playwright

    rows = []
    with sync_playwright() as p:
        # NOTE: headless=False on purpose. SGX's bot-protection appears to
        # detect a fully-invisible (headless) browser and hides the real
        # table content from it -- a visible browser window gets the real
        # data. This means a Chrome window will briefly pop up each run.
        # If you ever want to try headless again, change this to True,
        # but re-test with test_fetch.py first to confirm rows come back.
        browser = p.chromium.launch(headless=False)
        page = browser.new_page(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            )
        )
        page.goto(SGX_URL, wait_until="networkidle", timeout=60000)
        # Give the SPA time to hydrate the announcements table.
        page.wait_for_timeout(6000)

        # Confirmed live column order (23 Sep 2026 capture):
        # 0 = Date & Time, 1 = Issuer Name, 2 = Security Name,
        # 3 = Title, 4 = Category
        row_elements = page.query_selector_all("table tbody tr")
        for r in row_elements[:max_rows]:
            cells = r.query_selector_all("td")
            if len(cells) < 5:
                continue
            text_cells = [c.inner_text().strip() for c in cells]
            # The Title cell's <a> links to the actual announcement/PDF;
            # it's the first link in the row (the Category cell's <a> is
            # just a filter link, and comes after it).
            link_el = r.query_selector("a")
            href = link_el.get_attribute("href") if link_el else ""

            timestamp = text_cells[0]
            company = text_cells[1]
            security = text_cells[2]
            title = text_cells[3]
            category = text_cells[4]
            ann_id = href or f"{company}|{title}|{timestamp}"

            rows.append(
                Announcement(
                    id=ann_id,
                    company=company,
                    title=title,
                    category=category,
                    timestamp=timestamp,
                    url=(href if href.startswith("http") else f"https://www.sgx.com{href}"),
                    security=security,
                )
            )
        if not rows:
            try:
                print(f"[debug] page title: {page.title()!r}, html length: {len(page.content())}")
                page.screenshot(path=str(Path(__file__).parent / "debug.png"), full_page=True)
            except Exception as e:
                print(f"[debug] could not save screenshot: {e}", file=sys.stderr)
        browser.close()
    return rows


# ---- Alternative fetch backend: Apify actor (paid, more robust) ---------
# Swap fetch_announcements() for this if Akamai blocks your IP.
#
# def fetch_announcements(max_rows: int = 60) -> list:
#     import requests
#     APIFY_TOKEN = "your_apify_token"
#     resp = requests.post(
#         "https://api.apify.com/v2/acts/crawlerbros~sgx-company-announcements-scraper"
#         f"/run-sync-get-dataset-items?token={APIFY_TOKEN}",
#         json={"maxItems": max_rows},
#         timeout=120,
#     )
#     resp.raise_for_status()
#     out = []
#     for item in resp.json():
#         out.append(Announcement(
#             id=item.get("pdfUrl") or item.get("title", ""),
#             company=item.get("issuerName", ""),
#             title=item.get("title", ""),
#             category=item.get("category", ""),
#             timestamp=item.get("publishDate", ""),
#             url=item.get("pdfUrl", ""),
#         ))
#     return out


# ============================ CLASSIFY ====================================

def classify(ann: Announcement) -> Announcement:
    haystack = f"{ann.category} {ann.title}".lower()
    for rule in RULES:
        for pat in rule.patterns:
            if re.search(pat, haystack):
                ann.matches.append(rule)
                break  # one match per rule is enough
    return ann


def is_market_moving(ann: Announcement) -> bool:
    return any(r.weight >= MIN_WEIGHT_TO_ALERT for r in ann.matches)


# ============================ STATE (dedup) ================================

def load_seen() -> set:
    if SEEN_STORE.exists():
        return set(json.loads(SEEN_STORE.read_text()))
    return set()


def save_seen(seen: set) -> None:
    # Keep the file from growing forever -- last 5000 IDs is plenty.
    trimmed = list(seen)[-5000:]
    SEEN_STORE.write_text(json.dumps(trimmed))


# ============================ HIT HISTORY ===================================
# Every market-moving announcement we've ever flagged, newest first.
# This is what the dashboard is built from.

def load_hit_history() -> list:
    if HITS_STORE.exists():
        return json.loads(HITS_STORE.read_text())
    return []


def save_hit_history(history: list) -> None:
    trimmed = history[:MAX_HISTORY]
    HITS_STORE.write_text(json.dumps(trimmed, indent=2))


def ann_to_dict(ann: Announcement) -> dict:
    return {
        "id": ann.id,
        "company": ann.company,
        "security": ann.security,
        "category": ann.category,
        "title": ann.title,
        "timestamp": ann.timestamp,
        "url": ann.url,
        "rules": [r.name for r in ann.matches],
    }


# ============================ DASHBOARD (HTML) ==============================

def generate_dashboard(history: list, new_ids: set) -> None:
    rows_html = []
    for h in history:
        is_new = h["id"] in new_ids
        row_class = "new" if is_new else ""
        badge = '<span class="badge">NEW</span> ' if is_new else ""
        rules = ", ".join(h["rules"])
        rows_html.append(f"""
        <tr class="{row_class}">
            <td>{h['timestamp']}</td>
            <td>{h['company']}</td>
            <td>{h['security']}</td>
            <td>{h['category']}</td>
            <td>{badge}<a href="{h['url']}" target="_blank">{h['title']}</a></td>
            <td>{rules}</td>
        </tr>""")

    html = f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<meta http-equiv="refresh" content="60">
<title>SGX Market-Moving Alerts</title>
<style>
  body {{ font-family: Arial, sans-serif; margin: 24px; background: #f7f8fa; color: #1a1a1a; }}
  h1 {{ font-size: 20px; }}
  .updated {{ color: #666; font-size: 13px; margin-bottom: 16px; }}
  table {{ border-collapse: collapse; width: 100%; background: #fff; }}
  th, td {{ padding: 8px 10px; border-bottom: 1px solid #e0e0e0; font-size: 13px; text-align: left; vertical-align: top; }}
  th {{ background: #1a2a4a; color: #fff; position: sticky; top: 0; }}
  tr.new {{ background: #fff8d6; }}
  .badge {{ background: #d9534f; color: #fff; font-size: 11px; padding: 2px 6px; border-radius: 3px; }}
  a {{ color: #1a4a8a; text-decoration: none; }}
  a:hover {{ text-decoration: underline; }}
</style>
</head>
<body>
  <h1>SGX Market-Moving Announcements</h1>
  <div class="updated">Last updated: {now_sgt():%d %b %Y %H:%M:%S} SGT (auto-refreshes every 60s while open)</div>
  <table>
    <thead>
      <tr><th>Time</th><th>Company</th><th>Security</th><th>Category</th><th>Title</th><th>Matched Rule(s)</th></tr>
    </thead>
    <tbody>
      {"".join(rows_html) if rows_html else '<tr><td colspan="6">No market-moving announcements flagged yet.</td></tr>'}
    </tbody>
  </table>
</body>
</html>"""
    DASHBOARD_FILE.write_text(html, encoding="utf-8")


# ============================== EMAIL =======================================

def send_email(new_hits: list) -> None:
    lines = [f"SGX market-moving announcements — {now_sgt():%d %b %Y %H:%M}\n"]
    for ann in new_hits:
        rule_names = ", ".join(r.name for r in ann.matches)
        lines.append(f"[{rule_names}] {ann.company}")
        lines.append(f"    {ann.title}")
        lines.append(f"    {ann.timestamp} - {ann.url}\n")
    body = "\n".join(lines)

    msg = MIMEText(body)
    msg["Subject"] = f"SGX Alert: {len(new_hits)} market-moving announcement(s)"
    msg["From"] = ALERT_FROM
    msg["To"] = ALERT_TO

    context = ssl.create_default_context()
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
        server.starttls(context=context)
        server.login(SMTP_USER, SMTP_PASSWORD)
        server.sendmail(ALERT_FROM, [ALERT_TO], msg.as_string())


# ================================ MAIN ======================================

def main():
    seen = load_seen()
    history = load_hit_history()

    try:
        raw = fetch_announcements()
    except Exception as e:
        print(f"[fetch error] {e}", file=sys.stderr)
        sys.exit(1)

    if not raw:
        print("[fetch error] 0 rows returned -- SGX may be blocking this server.",
              file=sys.stderr)
        sys.exit(1)

    new_hits = []
    for ann in raw:
        if ann.id in seen:
            continue
        seen.add(ann.id)
        classify(ann)
        if is_market_moving(ann):
            new_hits.append(ann)

    save_seen(seen)

    if new_hits:
        history = [ann_to_dict(a) for a in new_hits] + history
        save_hit_history(history)

    generate_dashboard(history, new_ids={a.id for a in new_hits})

    if new_hits:
        try:
            send_email(new_hits)
            print(f"Found {len(new_hits)} new market-moving announcement(s). "
                  f"Emailed alert and updated sgx_dashboard.html.")
        except Exception as e:
            print(f"[email error] {e}", file=sys.stderr)
            print(f"Found {len(new_hits)} new market-moving announcement(s), "
                  f"but the email failed to send. Dashboard was still updated.")
    else:
        print(f"No new market-moving announcements this run "
              f"({len(raw)} total rows checked). Dashboard refreshed.")


if __name__ == "__main__":
    main()
