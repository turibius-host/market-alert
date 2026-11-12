"""
SGX Market-Moving Announcement Alerter
========================================
What this does
---------------
1. Pulls the latest rows from SGX's Company Announcements page.
2. Runs each announcement's title/category text through a keyword-rule
   engine that flags the categories most likely to move a share price
   the next trading day (takeovers, trading halts, profit warnings,
   rights issues, SGX queries, quarterly updates, MOUs, etc).
3. Keeps a "seen" store so it only processes NEW announcements each run.
4. Writes/updates a webpage (index.html on GitHub) listing every
   market-moving announcement flagged so far, newest-run hits highlighted.
5. Emails new hits. If an email fails, it is retried on the next run.

Why it's built this way
------------------------
SGX's site sits behind bot-protection that appears to block fully-invisible
("headless") browsers, so this script drives a real (visible) Chromium window
via Playwright (on GitHub it runs under a virtual display).

CHANGES in this version
-----------------------
- New rules: strategic/business-development news (state-owned partners,
  showcases, expos, joint ventures, cooperation agreements, contract wins),
  letters of intent / term sheets, and "third quarter"-style results wording.
- Tries to load more rows (scroll / "load more") so busy periods don't push
  announcements off the page between runs.
- Prints a [coverage] line every run, with a WARNING when every row on the
  page was new (a sign that announcements may have been missed).
- If the email fails, the hits are retried next run (they used to be lost).
- The "seen" list now keeps the NEWEST 5000 ids (before, trimming was random).
- Dashboard text is HTML-escaped.
"""
import json
import os
import re
import smtplib
import ssl
import sys
from dataclasses import dataclass, field
from email.mime.text import MIMEText
from html import escape as esc
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
# How many rows to try to read from the page each run.
MAX_ROWS = 150
# Most hits sent in one email (the rest go in the next one).
MAX_EMAIL_ITEMS = 30


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
            # quarter / period wording used in titles, e.g. "Third Quarter Results", "3Q2026 Results"
            r"\b(first|second|third|fourth|1st|2nd|3rd|4th)\s+quarter\b",
            r"\b[1-4]q\s?(fy)?\d{0,4}\b",
            r"\b[12]h\s?(fy)?\d{2,4}\b",
            r"\b9m\s?(fy)?\d{2,4}\b",
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
            r"quarter(ly)?\s+update",
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
            r"letter of intent",
            r"\bloi\b",
            r"term sheet",
            r"heads of agreement",
        ],
        weight=2,
        note="Often early-stage/non-binding -- a leading indicator rather than a done deal.",
    ),
    Rule(
        name="Strategic / Business Development News",
        patterns=[
            r"state[- ]owned",
            r"\bshowcas(e|es|ed|ing)\b",
            r"\b(ciie|expo|exhibition)\b",
            r"joint venture|\bjv\b",
            r"(collaboration|cooperation|framework|partnership|supply|offtake|licensing) agreement",
            r"strategic cooperation",
            r"\b(wins?|won|secures?|secured|clinch(es|ed)?|bags?|lands?)\b.{0,40}\b(contracts?|orders?|projects?|tenders?|deals?)\b",
        ],
        weight=2,
        note="Company-promoted business news (new partners, contract wins, expos) -- often moves small caps.",
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

def fetch_announcements(max_rows: int = MAX_ROWS) -> list:
    """
    Loads the SGX announcements page in a real browser and extracts the
    visible rows.

    NOTE: SGX's page is a JS single-page app, so the exact CSS
    selectors below may need adjusting if SGX changes their frontend.
    """
    from playwright.sync_api import sync_playwright

    rows = []
    with sync_playwright() as p:
        # NOTE: headless=False on purpose. SGX's bot-protection appears to
        # detect a fully-invisible (headless) browser and hides the real
        # table content from it -- a visible browser window gets the real
        # data. (On GitHub this runs under a virtual display.)
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

        # --- Try to load more rows ------------------------------------------
        # The page may only show the newest ~20 rows. Scroll and click any
        # "load more" style button so busy periods don't push announcements
        # off the page between runs. Harmless if the page has neither.
        first_count = len(page.query_selector_all("table tbody tr"))
        last_count = -1
        for _ in range(10):
            n = len(page.query_selector_all("table tbody tr"))
            if n >= max_rows or n == last_count:
                break
            last_count = n
            try:
                btn = page.query_selector(
                    "button:has-text('Load more'), button:has-text('Show more'), "
                    "a:has-text('Load more'), a:has-text('Show more')"
                )
                if btn:
                    btn.click()
                else:
                    page.mouse.wheel(0, 30000)
                page.wait_for_timeout(2000)
            except Exception:  # noqa: BLE001 - never let this step break the run
                break
        print(f"[fetch] rows visible: {first_count} at load -> "
              f"{len(page.query_selector_all('table tbody tr'))} after trying to load more")

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
# The seen ids are kept as an ORDERED list (oldest first) so that when it is
# trimmed, the oldest ids are dropped - never recent ones.

def load_seen() -> list:
    if SEEN_STORE.exists():
        try:
            return list(json.loads(SEEN_STORE.read_text()))
        except (json.JSONDecodeError, OSError):
            print("[state] sgx_seen.json unreadable - starting fresh.", file=sys.stderr)
    return []


def save_seen(seen_list: list) -> None:
    # Keep the file from growing forever -- the newest 5000 IDs is plenty.
    SEEN_STORE.write_text(json.dumps(seen_list[-5000:]))


# ============================ HIT HISTORY ===================================
# Every market-moving announcement we've ever flagged, newest first.
# This is what the dashboard is built from. Each entry also records whether
# its email has been sent ("emailed"), so failed emails are retried.

def load_hit_history() -> list:
    if HITS_STORE.exists():
        try:
            return json.loads(HITS_STORE.read_text())
        except (json.JSONDecodeError, OSError):
            print("[state] sgx_hits.json unreadable - starting fresh.", file=sys.stderr)
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
        "emailed": False,
    }


# ============================ DASHBOARD (HTML) ==============================

def generate_dashboard(history: list, new_ids: set) -> None:
    rows_html = []
    for h in history:
        is_new = h["id"] in new_ids
        row_class = "new" if is_new else ""
        badge = '<span class="badge">NEW</span> ' if is_new else ""
        rules = esc(", ".join(h["rules"]))
        rows_html.append(f"""
        <tr class="{row_class}">
            <td>{esc(h['timestamp'])}</td>
            <td>{esc(h['company'])}</td>
            <td>{esc(h['security'])}</td>
            <td>{esc(h['category'])}</td>
            <td>{badge}<a href="{esc(h['url'], quote=True)}" target="_blank" rel="noopener">{esc(h['title'])}</a></td>
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

def send_email(hits: list) -> None:
    """hits = list of history dicts (see ann_to_dict)."""
    lines = [f"SGX market-moving announcements — {now_sgt():%d %b %Y %H:%M} SGT\n"]
    for h in hits:
        rule_names = ", ".join(h["rules"])
        lines.append(f"[{rule_names}] {h['company']}")
        lines.append(f"    {h['title']}")
        lines.append(f"    {h['timestamp']} - {h['url']}\n")
    body = "\n".join(lines)

    msg = MIMEText(body)
    msg["Subject"] = f"SGX Alert: {len(hits)} market-moving announcement(s)"
    msg["From"] = ALERT_FROM
    msg["To"] = ALERT_TO

    context = ssl.create_default_context()
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as server:
        server.starttls(context=context)
        server.login(SMTP_USER, SMTP_PASSWORD)
        server.sendmail(ALERT_FROM, [ALERT_TO], msg.as_string())


def unsent(history: list) -> list:
    """Hits whose email hasn't gone out yet (older entries without the flag count as sent)."""
    return [h for h in history if not h.get("emailed", True)][:MAX_EMAIL_ITEMS]


# ================================ MAIN ======================================

def report_coverage(raw: list, new_count: int, had_seen: bool) -> None:
    stamps = [a.timestamp for a in raw]
    print(f"[coverage] rows read: {len(raw)} | newest: {stamps[0]} | oldest: {stamps[-1]} "
          f"| new this run: {new_count}")
    if had_seen and new_count == len(raw):
        print("[coverage] WARNING: every row on the page was new. Announcements posted "
              "since the last run may have scrolled off the page and been missed - "
              "run more often or load more rows.")


def main():
    seen_list = load_seen()
    seen = set(seen_list)
    had_seen = bool(seen_list)
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
    new_count = 0
    for ann in raw:
        if ann.id in seen:
            continue
        seen.add(ann.id)
        seen_list.append(ann.id)
        new_count += 1
        classify(ann)
        if is_market_moving(ann):
            new_hits.append(ann)

    report_coverage(raw, new_count, had_seen)
    save_seen(seen_list)

    if new_hits:
        history = [ann_to_dict(a) for a in new_hits] + history
    save_hit_history(history)   # saved BEFORE emailing, so nothing is lost if the email fails

    generate_dashboard(history, new_ids={a.id for a in new_hits})

    to_send = unsent(history)
    if to_send:
        try:
            send_email(to_send)
            for h in to_send:
                h["emailed"] = True
            save_hit_history(history)
            retried = len(to_send) - len(new_hits)
            extra = f" (including {retried} retried from earlier runs)" if retried > 0 else ""
            print(f"Found {len(new_hits)} new market-moving announcement(s). "
                  f"Emailed {len(to_send)} item(s){extra} and updated the dashboard.")
        except Exception as e:
            print(f"[email error] {e}", file=sys.stderr)
            print(f"Found {len(new_hits)} new market-moving announcement(s), "
                  f"but the email failed to send - it will be retried next run. "
                  f"Dashboard was still updated.")
    else:
        print(f"No new market-moving announcements this run "
              f"({len(raw)} total rows checked). Dashboard refreshed.")


if __name__ == "__main__":
    main()
