"""
market_alert.py - Global market-moving news + price-move alerter

Run by Windows Task Scheduler every 10 minutes. Each run it:
  1. reads ~22 free news RSS feeds (in parallel),
  2. scores every headline by keywords and removes duplicates,
  3. checks Treasury yields, Brent oil and the VIX for sudden moves,
  4. rewrites market_dashboard.html (open it once in your browser - it refreshes itself).

Everything it creates lives in the same folder as this script.
"""

import difflib
import hashlib
import html
import json
import os
import re
import smtplib
import ssl
import sys
import traceback
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path
from urllib.parse import urlsplit

import feedparser
import requests

# ============================================================
# SETTINGS - safe to edit
# ============================================================
DASHBOARD_HOURS = 24       # show items from the last N hours
KEEP_HOURS = 168           # remember items this long (used to spot repeats of old stories)
MIN_SCORE = 4              # headlines scoring below this are hidden
URGENT_SCORE = 10          # score needed for the red "URGENT" section
IMPORTANT_SCORE = 5        # score needed for the orange "IMPORTANT" section
NEW_BADGE_MINUTES = 30     # items first seen within this time get a NEW badge
REFRESH_SECONDS = 60       # how often the open dashboard page reloads itself
REQUEST_TIMEOUT = 15       # seconds to wait for each feed

# Email alerts (the login details live in email_config.json - see setup steps)
EMAIL_MIN_LEVEL = "URGENT"   # "URGENT" = only red items are emailed; "IMPORTANT" = orange too
EMAIL_MAX_AGE_HOURS = 3      # don't email items older than this
EMAIL_MAX_ITEMS = 15         # max headlines in one email (extras go in the next one)
EMAIL_MAX_PER_DAY = 30       # safety cap so a busy news day can't flood your inbox
STRONG_WEIGHT = 8            # a single-source headline is only emailed if it has a keyword this strong
                             # (or is confirmed by a second, different publisher)
EMAIL_TOPIC_COOLDOWN_HOURS = 6   # after emailing a topic, hold back follow-ups on it this long
                                 # (unless the new item is clearly bigger)

# Price-move alerts.
#   kind "yield" -> thresholds in basis points (1 bp = 0.01%)
#   kind "pct"   -> thresholds in percent
#   threshold     = move within about PRICE_WINDOW_MIN minutes that triggers an alert
#   day_threshold = move versus the previous close that triggers an alert (catches
#                   gaps at the open, e.g. a sell-off overnight). None = don't check.
MARKETS = [
    # symbol,      label,                   kind,    threshold, day_threshold
    ("^TNX",       "US 10-year yield",      "yield", 8,         None),
    ("^TYX",       "US 30-year yield",      "yield", 8,         None),
    ("BZ=F",       "Brent crude oil",       "pct",   3.0,       None),
    ("^VIX",       "VIX (fear index)",      "pct",   15.0,      None),
    # --- AI trade ---
    ("^NDX",       "Nasdaq-100",            "pct",   1.0,       2.0),
    ("SMH",        "Semiconductor ETF",     "pct",   2.0,       3.5),
    ("NVDA",       "Nvidia",                "pct",   3.0,       5.0),
    ("AVGO",       "Broadcom",              "pct",   3.0,       5.0),
    ("TSM",        "TSMC (US shares)",      "pct",   3.0,       5.0),
    ("AMD",        "AMD",                   "pct",   3.5,       6.0),
    ("MU",         "Micron",                "pct",   3.0,       6.0),
    ("000660.KS",  "SK Hynix",              "pct",   3.0,       6.0),
    ("005930.KS",  "Samsung Electronics",   "pct",   3.0,       5.0),
]
PRICE_WINDOW_MIN = 60      # compare the latest price with the price ~this many minutes ago
ALERT_COOLDOWN_MIN = 60    # don't repeat an alert for the same market within this time

# ============================================================
# NEWS SOURCES: (name, url, bonus)
# bonus is added to a headline's score: + for fast/trusted, - for noisy sources
# ============================================================
GOOGLE_NEWS = (
    "https://news.google.com/rss/search?q="
    "Hormuz+OR+Iran+oil+OR+%22Treasury+yields%22+OR+%22stock+market%22+when:1d"
    "&hl=en-US&gl=US&ceid=US:en"
)

FEEDS = [
    ("MarketWatch Bulletins",     "https://feeds.marketwatch.com/marketwatch/bulletins", 3),
    ("MarketWatch Top Stories",   "https://feeds.marketwatch.com/marketwatch/topstories", 0),
    ("Bloomberg Markets",         "https://www.bloomberg.com/feeds/markets/news.rss", 1),
    ("Bloomberg Economics",       "https://www.bloomberg.com/feeds/economics/news.rss", 1),
    ("Bloomberg Politics",        "https://www.bloomberg.com/feeds/politics/news.rss", 0),
    ("WSJ Markets",               "https://feeds.a.dj.com/rss/RSSMarketsMain.xml", 1),
    ("WSJ World News",            "https://feeds.a.dj.com/rss/RSSWorldNews.xml", 0),
    ("FT Markets",                "https://www.ft.com/markets?format=rss", 1),
    ("CNBC Top News",             "https://www.cnbc.com/id/100003114/device/rss/rss.html", 0),
    ("Yahoo Finance (S&P 500)",   "https://feeds.finance.yahoo.com/rss/2.0/headline?s=%5EGSPC&region=US&lang=en-US", 0),
    ("Yahoo Finance via Google",  "https://news.google.com/rss/search?q=site:finance.yahoo.com+when:1d&hl=en-US&gl=US&ceid=US:en", 0),
    ("Investing.com Indicators",  "https://www.investing.com/rss/news_95.rss", 1),
    ("Investing.com Economy",     "https://www.investing.com/rss/news_14.rss", 0),
    ("Investing.com Forex",       "https://www.investing.com/rss/news_1.rss", 0),
    ("US Federal Reserve",        "https://www.federalreserve.gov/feeds/press_all.xml", 2),
    ("European Central Bank",     "https://www.ecb.europa.eu/rss/pub.html", 1),
    ("Al Jazeera",                "https://www.aljazeera.com/xml/rss/all.xml", 0),
    ("BBC Middle East",           "https://feeds.bbci.co.uk/news/world/middle_east/rss.xml", 0),
    ("OilPrice.com",              "https://oilprice.com/rss/main", 0),
    ("Google News: key topics",   GOOGLE_NEWS, 0),
    ("Seeking Alpha",             "https://seekingalpha.com/feed.xml", -1),
    ("The Economist Finance",     "https://www.economist.com/finance-and-economics/rss.xml", -1),
    ("Zero Hedge",                "https://feeds.feedburner.com/zerohedge/feed", -3),
]

# ---- AI trade: tech/semiconductor feeds ----
FEEDS += [
    ("Bloomberg Technology",      "https://www.bloomberg.com/feeds/technology/news.rss", 1),
    ("WSJ Tech",                  "https://feeds.a.dj.com/rss/RSSWSJD.xml", 1),
    ("FT Technology",             "https://www.ft.com/technology?format=rss", 1),
    ("CNBC Tech",                 "https://www.cnbc.com/id/19854910/device/rss/rss.html", 0),
    ("Tom's Hardware",            "https://www.tomshardware.com/feeds/all", -1),
    ("SemiAnalysis",              "https://newsletter.semianalysis.com/feed", 0),
]


def google_news(query):
    """Build a Google News RSS search URL (last 24 hours). Spaces become +."""
    q = query.replace(" ", "+").replace('"', "%22")
    return f"https://news.google.com/rss/search?q={q}+when:1d&hl=en-US&gl=US&ceid=US:en"


# ---- AI trade: Google News topic searches (these pull FT, Bloomberg, Reuters etc.
#      headlines from many outlets at once, so they catch stories the feeds above miss) ----
FEEDS += [
    ("GNews: AI labs & funding",
     google_news('OpenAI OR Anthropic OR xAI OR Stargate OR SoftBank funding OR valuation OR raise OR revenue OR compute'), 1),
    ("GNews: AI chips & earnings",
     google_news('Nvidia OR AMD OR Broadcom OR TSMC OR Micron OR "SK Hynix" OR Samsung OR ASML guidance OR shares OR forecast'), 1),
    ("GNews: China chips & export controls",
     google_news('CXMT OR YMTC OR SMIC OR "Huawei Ascend" OR DeepSeek OR "export controls" chips'), 1),
    ("GNews: Big Tech AI capex",
     google_news('Microsoft OR Meta OR Alphabet OR Amazon OR Oracle OR CoreWeave capex OR "data center" OR "AI spending"'), 1),
    ("GNews: Memory market (DRAM/HBM)",
     google_news('DRAM OR HBM OR NAND memory prices OR shortage OR oversupply'), 1),
    ("TrendForce via Google",
     "https://news.google.com/rss/search?q=site:trendforce.com+when:1d&hl=en-US&gl=US&ceid=US:en", 0),
]

# ============================================================
# KEYWORDS: weight -> terms. A trailing * means "any ending" (missile* = missile, missiles)
# A headline's score = its highest matching weight + 1 per extra matching keyword
# (max +4) + the source bonus + 2 for every other source reporting the same story.
# ============================================================
KEYWORDS = {
    10: [
        "hormuz", "missile*", "airstrike*", "air strike*", "ceasefire", "escalat*",
        "oil tanker*", "emergency rate", "emergency meeting", "circuit breaker",
        "trading halt*", "default*", "bank run", "flash crash", "blockade",
        "invasion", "nuclear", "market crash", "bond rout", "yields spike",
        "yields surge", "yields jump", "sovereign downgrade",
    ],
    5: [
        "treasury yield*", "10-year yield", "30-year yield", "bond yield*",
        "sell-off", "selloff", "plunge*", "tumble*", "soar*", "surge*",
        "rate hike*", "rate cut*", "fed", "powell", "fomc", "inflation", "cpi",
        "payrolls", "jobs report", "opec", "crude", "brent", "iran*", "israel*",
        "gaza", "houthi*", "red sea", "tariff*", "recession", "downgrade*",
        "sanction*", "war", "shutdown",
    ],
    2: [
        "stocks", "wall street", "dollar", "gold", "yen", "yuan", "china",
        "oil", "bond*", "gdp", "earnings", "ecb", "boj", "bank of japan",
        "rally", "volatility", "unemployment",
    ],
}

# ============================================================
# AI-TRADE KEYWORDS (same idea; headlines matching these also get an "AI" badge)
# Company names alone score low (2) so a passing mention doesn't clutter the
# dashboard - they matter when combined with an event word (see COMBOS below).
# ============================================================
AI_KEYWORDS = {
    8: [
        "cxmt", "ymtc", "export control*", "export ban*", "chip ban*", "entity list",
        "capex cut*", "order cut*", "cancel* order*", "cancels order*",
        "chip tariff*", "semiconductor tariff*", "circular financing",
        "hbm shortage", "memory shortage", "dram price*", "memory price*",
    ],
    7: ["ai bubble"],
    5: [
        "funding round*", "valuation", "fundrais*", "ipo", "compute deal*",
        "chip deal*", "supply deal*", "guidance", "hbm", "dram", "nand",
        "capex", "capital expenditure*", "hyperscaler*", "ai spending", "ai demand",
        "ai trade", "ai stocks", "ai rally", "ai selloff", "magnificent seven",
        "mag 7", "deepseek", "smic", "huawei", "misreport*", "section 232",
    ],
    3: ["data center*", "datacenter*"],
    2: [
        "openai", "anthropic", "xai", "stargate", "softbank", "nvidia", "amd",
        "broadcom", "tsmc", "micron", "sk hynix", "samsung", "asml", "marvell",
        "intel", "coreweave", "cerebras", "gpu*", "semiconductor*", "chip", "chips",
        "chipmaker*", "ai", "artificial intelligence", "llm*", "memory",
    ],
}

# COMBOS: if a headline contains ANY term from the first list AND ANY term from the
# second, its base score becomes at least the number shown. (floor, names, events)
COMBOS = [
    # AI-lab financing news, e.g. "OpenAI funding cut to $50B, FT reports"
    (8, ["openai", "anthropic", "xai", "stargate", "softbank", "mistral"],
        ["funding", "valuation", "raise*", "round", "stake", "investment*", "revenue",
         "compute", "commit*", "misreport*", "disput*", "denies", "denied", "ipo",
         "loss*", "burn"]),
    # Big moves in the chip names themselves
    (7, ["nvidia", "amd", "broadcom", "tsmc", "micron", "sk hynix", "samsung", "asml",
         "marvell", "intel", "cerebras", "coreweave", "super micro"],
        ["plunge*", "tumble*", "slump*", "sell-off", "selloff", "crash*", "plummet*",
         "slide*", "sink*", "sank", "tank*", "soar*", "surge*", "jump*", "rally",
         "guidance", "downgrade*", "warns", "warning", "forecast", "outlook"]),
    # Memory-cycle / China-memory news, e.g. "CXMT flags denser chip design"
    (7, ["cxmt", "ymtc", "memory", "dram", "hbm", "nand"],
        ["price*", "oversupply", "glut", "shortage", "denser", "breakthrough",
         "ramp*", "expan*", "capacity", "plunge*", "slump*", "sell-off", "selloff",
         "tumble*", "surge*", "rally", "china*"]),
    # Big Tech spending plans
    (6, ["microsoft", "meta", "alphabet", "google", "amazon", "oracle", "coreweave", "openai"],
        ["capex", "capital expenditure*", "data center*", "datacenter*", "ai spending",
         "ai investment*", "ai infrastructure"]),
]

# ============================================================
# Files
# ============================================================
BASE = Path(__file__).resolve().parent
# On your PC everything lives next to this script. On GitHub Actions the workflow sets
# MARKET_ALERT_DIR (a folder that is saved between runs) and MARKET_ALERT_DASHBOARD.
DATA_DIR = Path(os.environ.get("MARKET_ALERT_DIR") or BASE)
STATE_FILE = DATA_DIR / "market_state.json"
DASHBOARD = DATA_DIR / (os.environ.get("MARKET_ALERT_DASHBOARD") or "market_dashboard.html")
LOG_FILE = BASE / "market_alert.log"
EMAIL_CONFIG = BASE / "email_config.json"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )
}
UTC = timezone.utc

try:
    from zoneinfo import ZoneInfo
    LOCAL_TZ = ZoneInfo(os.environ["MARKET_ALERT_TZ"]) if os.environ.get("MARKET_ALERT_TZ") else None
except Exception:  # noqa: BLE001 - unknown timezone name or no tz database: fall back to PC time
    LOCAL_TZ = None


def local(dt):
    """Convert a datetime to your timezone (MARKET_ALERT_TZ, else the PC's own)."""
    return dt.astimezone(LOCAL_TZ) if LOCAL_TZ else dt.astimezone()


# ============================================================
# Helpers
# ============================================================
def log(msg):
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"{stamp}  {msg}"
    print(line)
    try:
        if LOG_FILE.exists() and LOG_FILE.stat().st_size > 1_000_000:
            LOG_FILE.replace(BASE / "market_alert.old.log")
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def make_pattern(term):
    wildcard = term.endswith("*")
    core = re.escape(term.rstrip("*"))
    pat = r"\b" + core
    if wildcard:
        pat += r"\w*"
    elif core[-1].isalnum():
        pat += r"\b"
    return re.compile(pat, re.IGNORECASE)


def _merge_keywords():
    merged = {}
    for source in (KEYWORDS, AI_KEYWORDS):
        for weight, terms in source.items():
            merged.setdefault(weight, []).extend(terms)
    return merged


COMPILED = [
    (weight, [(t.rstrip("*"), make_pattern(t)) for t in terms])
    for weight, terms in sorted(_merge_keywords().items(), reverse=True)
]

# Terms that earn a headline the "AI" badge
AI_TERMS = {t.rstrip("*") for terms in AI_KEYWORDS.values() for t in terms}
AI_TERMS |= {t.rstrip("*") for _, names, events in COMBOS for t in names}

COMBOS_COMPILED = [
    (floor,
     [(t.rstrip("*"), make_pattern(t)) for t in names],
     [(t.rstrip("*"), make_pattern(t)) for t in events])
    for floor, names, events in COMBOS
]


def score_text(text):
    """Return (score_from_keywords, matched_terms, strongest_weight)."""
    top = 0
    hits = []
    for weight, patterns in COMPILED:
        for term, pat in patterns:
            if pat.search(text):
                if term not in hits:
                    hits.append(term)
                top = max(top, weight)
    for floor, names, events in COMBOS_COMPILED:
        found_names = [t for t, p in names if p.search(text)]
        found_events = [t for t, p in events if p.search(text)]
        if found_names and found_events:
            top = max(top, floor)
            for t in found_names + found_events:
                if t not in hits:
                    hits.append(t)
    if not hits:
        return 0, [], 0
    return top + min(len(hits) - 1, 4), hits, top


def is_ai(hits):
    return any(h in AI_TERMS for h in hits)


def level_of(score):
    if score >= URGENT_SCORE:
        return "URGENT"
    if score >= IMPORTANT_SCORE:
        return "IMPORTANT"
    return "WATCH"


def clean_html(s):
    s = re.sub(r"<[^>]+>", " ", s or "")
    s = html.unescape(s)
    return re.sub(r"\s+", " ", s).strip()


def norm_title(t):
    return re.sub(r"[^a-z0-9 ]", "", t.lower()).strip()


STOPWORDS = {
    "the", "and", "for", "with", "from", "that", "this", "its", "are", "was", "has",
    "have", "will", "after", "over", "into", "amid", "says", "say", "new", "more",
    "than", "about", "report", "reports", "reportedly", "could", "may", "his", "her",
}


def tokens(nt):
    return {w for w in nt.split() if len(w) > 2 and w not in STOPWORDS}


def is_same_story(nt_a, tok_a, nt_b, tok_b):
    """Fuzzy check: do two normalised headlines describe the same story?"""
    if not tok_a or not tok_b:
        return False
    inter = len(tok_a & tok_b)
    if inter < 3:
        return nt_a == nt_b
    jac = inter / len(tok_a | tok_b)
    if jac >= 0.55:
        return True
    if jac >= 0.3:
        return difflib.SequenceMatcher(None, nt_a, nt_b).ratio() >= 0.8
    return False


def publisher(source_name):
    """'Bloomberg Markets' and 'Bloomberg Technology' are one publisher, so they
    can't 'confirm' each other. All Google News searches count as one publisher too."""
    return source_name.split()[0].split(":")[0]


def canonical(link):
    if not link:
        return ""
    p = urlsplit(link.strip())
    return f"{p.scheme}://{p.netloc}{p.path}"


def entry_time(entry):
    t = entry.get("published_parsed") or entry.get("updated_parsed")
    if not t:
        return None
    try:
        return datetime(*t[:6], tzinfo=UTC)
    except (TypeError, ValueError):
        return None


def parse_iso(s):
    return datetime.fromisoformat(s)


def ago(dt, now):
    secs = max(0, int((now - dt).total_seconds()))
    if secs < 90:
        return "just now"
    if secs < 3600:
        return f"{secs // 60} min ago"
    if secs < 86400:
        return f"{secs // 3600} h ago"
    return f"{secs // 86400} d ago"


def load_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            log("State file unreadable - starting fresh.")
    return {"items": [], "seen": {}, "prices": {}, "last_alert": {}, "status": {}, "snapshots": []}


def save_state(state):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state), encoding="utf-8")
    tmp.replace(STATE_FILE)


# ============================================================
# 1) News feeds
# ============================================================
def fetch_feed(feed):
    name, url, bonus = feed
    try:
        r = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
        entries = feedparser.parse(r.content).entries
        return name, bonus, entries, None
    except Exception as e:  # noqa: BLE001 - we want to survive any feed failure
        return name, bonus, [], str(e)[:120]


def ingest_feeds(state, now, first_run):
    items = state["items"]
    seen = state["seen"]
    status = {}
    cutoff = now - timedelta(hours=DASHBOARD_HOURS)
    new_count = 0
    merged_count = 0
    index = [(it, tokens(it["nt"])) for it in items]   # stories remembered for KEEP_HOURS

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(fetch_feed, FEEDS))

    for name, bonus, entries, error in results:
        if error:
            status[name] = {"ok": False, "error": error}
            log(f"FEED FAILED: {name}: {error}")
            continue
        status[name] = {"ok": True, "n": len(entries)}
        pub_name = publisher(name)

        for e in entries:
            title = clean_html(e.get("title", ""))
            if not title:
                continue
            link = canonical(e.get("link", ""))
            uid = link or hashlib.md5(title.encode("utf-8")).hexdigest()
            if uid in seen:
                continue
            seen[uid] = now.isoformat()

            published = entry_time(e) or now
            published = min(published, now)
            if published < cutoff:
                continue

            summary = clean_html(e.get("summary", ""))[:300]
            kw_score, hits, top = score_text(title + " " + summary)
            score = kw_score + bonus
            # Google News adds " - Outlet" to titles; ignore that when matching duplicates
            nt = norm_title(re.sub(r"\s+[-\u2013|]\s+[^-\u2013|]{2,40}$", "", title))
            tok = tokens(nt)

            # Same story as one we already hold (any age up to KEEP_HOURS)? Merge, don't re-alert.
            match = None
            for it, itok in index:
                if is_same_story(nt, tok, it["nt"], itok):
                    match = it
                    break
            if match:
                pubs = match.setdefault("pubs", [publisher(x) for x in match["sources"]])
                if name not in match["sources"]:
                    match["sources"].append(name)
                if pub_name not in pubs:
                    pubs.append(pub_name)
                    if len(pubs) <= 3:          # confirmation by other publishers: at most +4 in total
                        match["score"] += 2
                    merged_count += 1
                continue

            if score < MIN_SCORE:
                continue

            item = {
                "id": uid,
                "title": title,
                "nt": nt,
                "link": link or e.get("link", ""),
                "sources": [name],
                "pubs": [pub_name],
                "published": published.isoformat(),
                "first_seen": (published if first_run else now).isoformat(),
                "score": score,
                "top": top,
                "hits": hits[:6],
                "ai": is_ai(hits),
            }
            items.append(item)
            index.append((item, tok))
            new_count += 1

    state["status"] = status
    ok = sum(1 for s_ in status.values() if s_["ok"])
    log(f"Feeds OK: {ok}/{len(FEEDS)} | new headlines: {new_count} | merged duplicates: {merged_count}")


# ============================================================
# 2) Price moves (yields, oil, VIX)
# ============================================================
def get_price(symbol):
    import yfinance as yf

    t = yf.Ticker(symbol)
    try:
        fi = t.fast_info
        return float(fi["last_price"]), float(fi["previous_close"])
    except Exception:  # noqa: BLE001
        closes = t.history(period="5d")["Close"].dropna()
        return float(closes.iloc[-1]), float(closes.iloc[-2])


AI_SYMBOLS = {"^NDX", "SMH", "NVDA", "AVGO", "TSM", "AMD", "MU", "000660.KS", "005930.KS"}


def recently(last_alert, key, now, minutes):
    t = last_alert.get(key)
    return bool(t) and parse_iso(t) > now - timedelta(minutes=minutes)


def add_price_alert(state, now, symbol, title, key):
    state["items"].append({
        "id": f"mkt-{key}-{now:%Y%m%d%H%M}",
        "title": title,
        "nt": norm_title(title),
        "link": "",
        "sources": ["Market data"],
        "published": now.isoformat(),
        "first_seen": now.isoformat(),
        "score": URGENT_SCORE,
        "hits": ["price move"],
        "ai": symbol in AI_SYMBOLS,
    })
    state["last_alert"][key] = now.isoformat()
    log(f"PRICE ALERT: {title}")


def check_markets(state, now):
    prices = state.setdefault("prices", {})
    last_alert = state.setdefault("last_alert", {})
    snapshots = []

    # forget old alert records after a week
    week_ago = now - timedelta(days=7)
    for k in [k for k, v in last_alert.items() if parse_iso(v) < week_ago]:
        del last_alert[k]

    for symbol, label, kind, threshold, day_threshold in MARKETS:
        try:
            last, prev = get_price(symbol)
        except Exception as e:  # noqa: BLE001
            log(f"PRICE FAILED: {symbol}: {str(e)[:100]}")
            snapshots.append({"label": label, "error": True, "ai": symbol in AI_SYMBOLS})
            continue

        if kind == "yield" and last > 20:   # some Yahoo feeds quote yields x10
            last, prev = last / 10, prev / 10

        hist = prices.setdefault(symbol, [])
        hist.append([now.isoformat(), last])
        keep_after = now - timedelta(hours=26)
        hist[:] = [h for h in hist if parse_iso(h[0]) >= keep_after]

        # latest stored price at least PRICE_WINDOW_MIN ago (but not stale by > 3 h)
        ref_time = now - timedelta(minutes=PRICE_WINDOW_MIN)
        older = [h for h in hist if parse_iso(h[0]) <= ref_time]
        ref = None
        if older and parse_iso(older[-1][0]) >= now - timedelta(hours=3):
            ref = older[-1][1]

        if kind == "yield":
            day_change = (last - prev) * 100
            day_text = f"{day_change:+.1f} bp today"
            value_text = f"{last:.3f}%"
            move = (last - ref) * 100 if ref is not None else None
            fmt = lambda m: f"{abs(m):.1f} bp"   # noqa: E731
        else:
            day_change = (last - prev) / prev * 100
            day_text = f"{day_change:+.2f}% today"
            value_text = f"{last:,.2f}"
            move = (last - ref) / ref * 100 if ref is not None else None
            fmt = lambda m: f"{abs(m):.1f}%"     # noqa: E731

        snapshots.append({"label": label, "value": value_text, "day": day_text,
                          "up": day_change >= 0, "ai": symbol in AI_SYMBOLS})

        # 1) fast move within about the last hour
        if (move is not None and abs(move) >= threshold
                and not recently(last_alert, symbol, now, ALERT_COOLDOWN_MIN)):
            direction = "jumped" if move > 0 else "dropped"
            title = (f"{label} {direction} {fmt(move)} in about "
                     f"{PRICE_WINDOW_MIN} min (now {value_text})")
            add_price_alert(state, now, symbol, title, symbol)

        # 2) big move versus the previous close (catches gaps at the open).
        #    One alert per symbol per direction per trading day.
        if day_threshold is not None and abs(day_change) >= day_threshold:
            direction = "up" if day_change > 0 else "down"
            key = f"{symbol}:day:{direction}:{prev:.4f}"
            if key not in last_alert:
                title = (f"{label} {direction} {abs(day_change):.1f}% versus the "
                         f"previous close (now {value_text})")
                add_price_alert(state, now, symbol, title, key)

    state["snapshots"] = snapshots


# ============================================================
# 3) Dashboard
# ============================================================
CSS = """
body{font-family:Segoe UI,Arial,sans-serif;background:#10141a;color:#e6e9ee;margin:0;padding:20px;max-width:1500px;margin:auto}
.cols{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:16px;align-items:start}
@media (max-width:900px){.cols{grid-template-columns:1fr}}
.col{min-width:0}
.item{overflow-wrap:anywhere}
h1{font-size:22px;margin:0 0 4px}
.sub{color:#8b95a5;font-size:13px;margin-bottom:16px}
.cards{display:flex;flex-wrap:wrap;gap:10px;margin-bottom:22px}
.card{background:#1a2029;border-radius:8px;padding:10px 14px;min-width:150px}
.card .l{font-size:12px;color:#8b95a5}.card .v{font-size:20px;font-weight:600}
.up{color:#4cd38a}.down{color:#ff6b6b}.err{color:#8b95a5}
h2{font-size:16px;margin:24px 0 8px;padding-left:10px;border-left:4px solid}
.URGENT h2{border-color:#ff4d4d;color:#ff7a7a}
.IMPORTANT h2{border-color:#ffa534;color:#ffb85c}
.WATCH h2{border-color:#e3c94a;color:#e8d46f}
.item{background:#1a2029;border-radius:8px;padding:10px 14px;margin-bottom:8px}
.item a{color:#e6e9ee;text-decoration:none;font-weight:500}.item a:hover{text-decoration:underline}
.meta{font-size:12px;color:#8b95a5;margin-top:4px}
.new{background:#2f6fed;color:#fff;font-size:11px;border-radius:4px;padding:1px 6px;margin-right:6px}
.ai{background:#8a5cf6;color:#fff;font-size:11px;border-radius:4px;padding:1px 6px;margin-right:6px}
.grp{font-size:12px;color:#8b95a5;margin:0 0 6px;text-transform:uppercase;letter-spacing:.06em}
.card.small{min-width:120px;padding:8px 12px}.card.small .v{font-size:17px}
.empty{color:#8b95a5;font-size:13px;padding:6px 2px}
.health{font-size:12px;color:#8b95a5;margin-top:30px;line-height:1.7}
.bad{color:#ff7a7a}
"""


def render_section(level, items, now):
    out = [f'<div class="col {level}"><h2>{level} ({len(items)})</h2>']
    if not items:
        out.append('<div class="empty">Nothing right now.</div>')
    for it in items:
        pub = parse_iso(it["published"])
        is_new = parse_iso(it["first_seen"]) > now - timedelta(minutes=NEW_BADGE_MINUTES)
        badge = '<span class="new">NEW</span>' if is_new else ""
        if it.get("ai"):
            badge += '<span class="ai">AI</span>'
        title = html.escape(it["title"])
        if it.get("link"):
            title = f'<a href="{html.escape(it["link"], quote=True)}" target="_blank" rel="noopener">{title}</a>'
        sources = html.escape(", ".join(it["sources"]))
        local_time = local(pub).strftime("%d %b %H:%M")
        kw = html.escape(", ".join(it.get("hits", [])))
        out.append(
            f'<div class="item">{badge}{title}'
            f'<div class="meta">{local_time} ({ago(pub, now)}) &middot; {sources}'
            f'{" &middot; " + kw if kw else ""}</div></div>'
        )
    out.append("</div>")
    return "\n".join(out)


def write_dashboard(state, now):
    window = now - timedelta(hours=DASHBOARD_HOURS)
    visible = [it for it in state["items"] if parse_iso(it["published"]) >= window]
    visible.sort(key=lambda it: it["published"], reverse=True)

    groups = {"URGENT": [], "IMPORTANT": [], "WATCH": []}
    for it in visible:
        groups[level_of(it["score"])].append(it)

    new_urgent = sum(
        1 for it in groups["URGENT"]
        if parse_iso(it["first_seen"]) > now - timedelta(minutes=NEW_BADGE_MINUTES)
    )
    title = f"({new_urgent}) Market Alerts" if new_urgent else "Market Alerts"

    def make_card(snap, small=False):
        size = " small" if small else ""
        label = html.escape(snap["label"])
        if snap.get("error"):
            return (f'<div class="card{size}"><div class="l">{label}</div>'
                    f'<div class="v err">unavailable</div></div>')
        cls = "up" if snap["up"] else "down"
        return (f'<div class="card{size}"><div class="l">{label}</div>'
                f'<div class="v">{html.escape(snap["value"])}</div>'
                f'<div class="l {cls}">{html.escape(snap["day"])}</div></div>')

    snaps = state.get("snapshots", [])
    macro_cards = "".join(make_card(x) for x in snaps if not x.get("ai"))
    ai_cards = "".join(make_card(x, small=True) for x in snaps if x.get("ai"))
    cards_html = f'<div class="grp">Macro</div><div class="cards">{macro_cards}</div>'
    if ai_cards:
        cards_html += f'<div class="grp">AI trade</div><div class="cards">{ai_cards}</div>'

    health = []
    for name, st in state.get("status", {}).items():
        if st["ok"]:
            health.append(f'{html.escape(name)}: OK ({st["n"]})')
        else:
            health.append(f'<span class="bad">{html.escape(name)}: FAILED - {html.escape(st["error"])}</span>')

    page = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8">
<meta name="robots" content="noindex">
<meta http-equiv="refresh" content="{REFRESH_SECONDS}">
<title>{title}</title><style>{CSS}</style></head><body>
<h1>Market Alerts</h1>
<div class="sub">Updated {local(now).strftime("%d %b %Y %H:%M")} &middot; showing last {DASHBOARD_HOURS} h &middot; page refreshes every {REFRESH_SECONDS}s</div>
{cards_html}
<div class="cols">
{render_section("URGENT", groups["URGENT"], now)}
{render_section("IMPORTANT", groups["IMPORTANT"], now)}
{render_section("WATCH", groups["WATCH"], now)}
</div>
<div class="health"><b>Feed status</b><br>{" &middot; ".join(health)}</div>
</body></html>"""
    DASHBOARD.write_text(page, encoding="utf-8")


# ============================================================
# 4) Email alerts
# ============================================================
def load_email_config():
    """Return email settings, or None if not set up yet.
    GitHub Actions passes them as environment variables (from encrypted secrets);
    on your PC they come from email_config.json."""
    env_sender = (os.environ.get("GMAIL_ADDRESS") or "").strip()
    env_pw = (os.environ.get("GMAIL_APP_PASSWORD") or "").replace(" ", "").strip()
    if env_sender and env_pw:
        return {"sender": env_sender, "app_password": env_pw,
                "recipient": (os.environ.get("ALERT_RECIPIENT") or "").strip() or env_sender}
    if not EMAIL_CONFIG.exists():
        return None
    try:
        cfg = json.loads(EMAIL_CONFIG.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        log(f"email_config.json is not valid: {e}")
        return None
    if not cfg.get("enabled", True):
        return None
    pw = str(cfg.get("app_password", "")).replace(" ", "")
    if not cfg.get("sender") or not cfg.get("recipient") or not pw or pw.startswith("PASTE"):
        return None
    cfg["app_password"] = pw
    return cfg


def send_email(cfg, subject, text_body, html_body):
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = cfg["sender"]
    msg["To"] = cfg["recipient"]
    msg.set_content(text_body)
    msg.add_alternative(html_body, subtype="html")
    ctx = ssl.create_default_context()
    debug = 1 if "--debug" in sys.argv else 0
    errors = []
    # Try port 465 (SSL) first; if something on the PC blocks it, try 587 (STARTTLS).
    for port in (465, 587):
        try:
            if port == 465:
                with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ctx, timeout=30) as server:
                    server.set_debuglevel(debug)
                    server.ehlo()
                    server.esmtp_features["auth"] = "PLAIN"   # one login method only
                    server.login(cfg["sender"], cfg["app_password"])
                    server.send_message(msg)
            else:
                with smtplib.SMTP("smtp.gmail.com", 587, timeout=30) as server:
                    server.set_debuglevel(debug)
                    server.ehlo()
                    server.starttls(context=ctx)
                    server.ehlo()
                    server.esmtp_features["auth"] = "PLAIN"   # one login method only
                    server.login(cfg["sender"], cfg["app_password"])
                    server.send_message(msg)
            return
        except smtplib.SMTPAuthenticationError:
            raise RuntimeError(
                "Gmail rejected the login. The app_password in email_config.json is wrong, "
                "was revoked, or belongs to a different Gmail account than 'sender'. "
                "App passwords are exactly 16 letters - create a new one and paste it in."
            )
        except (OSError, smtplib.SMTPException) as e:
            errors.append(f"port {port}: {type(e).__name__}: {e}")
    raise RuntimeError(" | ".join(errors))


def is_confirmed(it):
    """Single-source headlines need a strong keyword, unless a 2nd publisher confirms them."""
    if "Market data" in it["sources"]:
        return True
    pubs = it.get("pubs") or [publisher(x) for x in it["sources"]]
    return len(set(pubs)) >= 2 or it.get("top", 0) >= STRONG_WEIGHT


def topic_key(it):
    """The two strongest keywords identify the topic, e.g. 'cxmt|dram'."""
    return "|".join(sorted(it.get("hits", [])[:2]))


def send_alerts(state, now, first_run):
    """Email new items that reach EMAIL_MIN_LEVEL. One email per run, at most."""
    min_score = IMPORTANT_SCORE if EMAIL_MIN_LEVEL == "IMPORTANT" else URGENT_SCORE
    label = "important+" if EMAIL_MIN_LEVEL == "IMPORTANT" else "urgent"
    fresh_after = now - timedelta(hours=EMAIL_MAX_AGE_HOURS)

    candidates = [it for it in state["items"] if it["score"] >= min_score]

    # First run (or first run after an update that adds new sources): don't email the backlog.
    if first_run or not state.get("email_init_v3"):
        for it in candidates:
            it["emailed"] = True
        state["email_init_v3"] = True
        return

    pending = [it for it in candidates
               if not it.get("emailed") and parse_iso(it["published"]) >= fresh_after]
    pending = [it for it in pending if is_confirmed(it)]
    if not pending:
        return

    cfg = load_email_config()
    if cfg is None:
        log(f"{len(pending)} {label} item(s) waiting but email is not set up (email_config.json).")
        return

    times = [t for t in state.setdefault("email_times", [])
             if parse_iso(t) > now - timedelta(hours=24)]
    state["email_times"] = times
    if len(times) >= EMAIL_MAX_PER_DAY:
        log(f"Daily email limit ({EMAIL_MAX_PER_DAY}) reached - skipping.")
        return

    # Topic cooldown: don't email a follow-up on a topic we emailed about recently,
    # unless the new item is clearly bigger. Suppressed items still show on the dashboard.
    topics = state.setdefault("topics", {})
    cooldown = timedelta(hours=EMAIL_TOPIC_COOLDOWN_HOURS)
    for k in [k for k, v in topics.items() if parse_iso(v["t"]) < now - timedelta(days=3)]:
        del topics[k]

    pending.sort(key=lambda it: (it["score"], it["published"]), reverse=True)
    batch, batch_topics = [], set()
    for it in pending:
        if "Market data" not in it["sources"]:
            key = topic_key(it)
            rec = topics.get(key)
            if key in batch_topics or (
                    rec and parse_iso(rec["t"]) > now - cooldown and it["score"] < rec["score"] + 3):
                it["emailed"] = True          # treat as a follow-up: dashboard only
                log(f"Follow-up not emailed (topic '{key}' already sent): {it['title'][:80]}")
                continue
            batch_topics.add(key)
        batch.append(it)
        if len(batch) >= EMAIL_MAX_ITEMS:
            break
    if not batch:
        return

    subject = f"[Market Alert] {len(batch)} {label}: {batch[0]['title'][:90]}"
    text_lines, html_rows = [], []
    for it in batch:
        when = local(parse_iso(it["published"])).strftime("%d %b %H:%M")
        sources = ("[AI trade] " if it.get("ai") else "") + ", ".join(it["sources"])
        text_lines.append(f"- {it['title']}\n  {when} | {sources}"
                          + (f"\n  {it['link']}" if it.get("link") else ""))
        t = html.escape(it["title"])
        if it.get("link"):
            t = f'<a href="{html.escape(it["link"], quote=True)}">{t}</a>'
        html_rows.append(f'<p style="margin:0 0 14px"><b>{t}</b><br>'
                         f'<span style="color:#666;font-size:12px">{when} &middot; '
                         f'{html.escape(sources)}</span></p>')

    try:
        send_email(cfg, subject, "\n\n".join(text_lines),
                   "<div style='font-family:Arial,sans-serif'>" + "".join(html_rows) + "</div>")
    except Exception as e:  # noqa: BLE001
        log(f"EMAIL FAILED: {str(e)[:200]}")
        return

    for it in batch:
        it["emailed"] = True
        if "Market data" not in it["sources"]:
            topics[topic_key(it)] = {"t": now.isoformat(), "score": it["score"]}
        pub_local = local(parse_iso(it["published"])).strftime("%d %b %H:%M")
        log(f"  emailed [{it['score']}] {it['title'][:90]} | {', '.join(it['sources'])} | published {pub_local}")
    times.append(now.isoformat())
    log(f"Email sent to {cfg['recipient']} with {len(batch)} item(s).")


def test_email():
    """python market_alert.py --test-email  ->  sends a test message."""
    cfg = load_email_config()
    if cfg is None:
        print("Email is not set up yet. Create email_config.json first (see setup steps).")
        return
    try:
        send_email(cfg, "[Market Alert] Test email",
                   "If you can read this, your market alert emails are working.",
                   "<p>If you can read this, your market alert emails are working.</p>")
        print(f"Test email sent to {cfg['recipient']}. Check your inbox (and Spam).")
    except Exception as e:  # noqa: BLE001
        print(f"FAILED: {e}")


# ============================================================
# Main
# ============================================================
def main():
    now = datetime.now(UTC)
    state = load_state()
    first_run = not state["items"] and not state["seen"]

    ingest_feeds(state, now, first_run)
    check_markets(state, now)
    send_alerts(state, now, first_run)

    keep_after = now - timedelta(hours=KEEP_HOURS)
    state["items"] = [it for it in state["items"] if parse_iso(it["published"]) >= keep_after]
    seen_cut = now - timedelta(days=7)
    state["seen"] = {k: v for k, v in state["seen"].items() if parse_iso(v) >= seen_cut}

    save_state(state)
    write_dashboard(state, now)
    log("Dashboard updated.")


if __name__ == "__main__":
    if "--test-email" in sys.argv:
        test_email()
        sys.exit(0)
    try:
        main()
    except Exception:  # noqa: BLE001
        log("CRASH:\n" + traceback.format_exc())
        sys.exit(1)
