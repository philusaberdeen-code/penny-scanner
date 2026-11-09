#!/usr/bin/env python3
"""
Penny Stock Scanner & Alerter — NASDAQ + NYSE, under $1.

Catches:
  🚀 PARABOLIC   sub-$1 stocks going vertical (accelerating price + volume surge)
  ⚡ CATALYST    fresh 8-K / 6-K / offering / merger filings and press releases
  🦢 BLACK SWAN  bankruptcy, SEC suspension, fraud, clinical hold, restatement,
                 or a statistically extreme move (default ≥4σ vs 60-day volatility)
  ⛔ HALT        trading halts (news pending, volatility pause, regulatory)
  📉 CRASH       sudden collapse on volume

Sessions: premarket (4:00–9:30 ET), regular (9:30–16:00 ET), after-hours (16:00–20:00 ET).

Data (all free, no API keys):
  Nasdaq screener (universe), Yahoo Finance (intraday + extended-hours bars),
  SEC EDGAR live filings, Nasdaq Trader halts feed, GlobeNewswire / PR Newswire,
  Yahoo per-ticker headlines.

Alerts: Telegram, Discord and/or ntfy — whichever env vars are set.

Not financial advice. Sub-$1 stocks are thin, volatile and frequently diluted.
"""
from __future__ import annotations

import argparse
import html
import json
import logging
import math
import os
import re
import statistics
import sys
import threading
import time
import xml.etree.ElementTree as ET
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from datetime import time as dtime
from email.utils import parsedate_to_datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

NY = ZoneInfo("America/New_York")
UK = ZoneInfo("Europe/London")
log = logging.getLogger("scanner")

BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")
NASDAQ_HEADERS = {
    "User-Agent": BROWSER_UA,
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Origin": "https://www.nasdaq.com",
    "Referer": "https://www.nasdaq.com/",
}
YAHOO_HEADERS = {"User-Agent": BROWSER_UA, "Accept": "application/json,text/xml,*/*"}

# ─────────────────────────────── config ────────────────────────────────


def _env(name: str, default: str = "") -> str:
    v = os.environ.get(name, "")
    return v.strip() if v and v.strip() else default


def _envf(name: str, default: float) -> float:
    try:
        return float(_env(name, str(default)))
    except ValueError:
        return default


def _envi(name: str, default: int) -> int:
    return int(_envf(name, default))


class Config:
    def __init__(self) -> None:
        self.max_price = _envf("MAX_PRICE", 1.00)
        self.exchanges = [e.strip().lower() for e in _env("EXCHANGES", "nasdaq,nyse").split(",") if e.strip()]
        self.cycle_seconds = _envi("CYCLE_SECONDS", 90)
        # parabolic gates
        self.parabolic_score = _envf("PARABOLIC_SCORE", 65)
        self.min_day_gain = _envf("MIN_DAY_GAIN", 0.15)
        self.min_15m_regular = _envf("MIN_15M_GAIN_REGULAR", 0.08)
        self.min_15m_extended = _envf("MIN_15M_GAIN_EXTENDED", 0.12)
        self.min_dv_regular = _envf("MIN_DOLLAR_VOL_15M_REGULAR", 25_000)
        self.min_dv_extended = _envf("MIN_DOLLAR_VOL_15M_EXTENDED", 10_000)
        # black swan / crash
        self.sigma = _envf("BLACK_SWAN_SIGMA", 4.0)
        self.crash_day = _envf("CRASH_DAY_DROP", -0.35)
        # catalysts
        self.catalyst_min_weight = _envi("CATALYST_MIN_WEIGHT", 3)
        self.catalyst_max_age_min = _envi("CATALYST_MAX_AGE_MIN", 60)
        # alert behaviour
        self.cooldown_min = _envi("ALERT_COOLDOWN_MIN", 30)
        self.realert_gain = _envf("REALERT_EXTRA_GAIN", 0.25)
        self.max_alerts_per_cycle = _envi("MAX_ALERTS_PER_CYCLE", 8)
        self.max_deep_scans = _envi("MAX_DEEP_SCANS", 40)
        self.fallback_per_cycle = _envi("FALLBACK_SCANS_PER_CYCLE", 80)
        self.heartbeat = _env("HEARTBEAT", "on").lower() != "off"
        # credentials
        self.sec_ua = _env("SEC_USER_AGENT", "")
        self.telegram_token = _env("TELEGRAM_BOT_TOKEN")
        self.telegram_chat = _env("TELEGRAM_CHAT_ID")
        self.discord_webhook = _env("DISCORD_WEBHOOK_URL")
        self.ntfy_topic = _env("NTFY_TOPIC")
        self.ntfy_server = _env("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
        # set by CLI
        self.forced_session: str | None = None


# ─────────────────────────────── market calendar ───────────────────────

HOLIDAYS = {
    date(2026, 1, 1), date(2026, 1, 19), date(2026, 2, 16), date(2026, 4, 3), date(2026, 5, 25),
    date(2026, 6, 19), date(2026, 7, 3), date(2026, 9, 7), date(2026, 11, 26), date(2026, 12, 25),
    date(2027, 1, 1), date(2027, 1, 18), date(2027, 2, 15), date(2027, 3, 26), date(2027, 5, 31),
    date(2027, 6, 18), date(2027, 7, 5), date(2027, 9, 6), date(2027, 11, 25), date(2027, 12, 24),
}
EARLY_CLOSE = {date(2026, 11, 27), date(2026, 12, 24), date(2027, 11, 26)}
SESSION_LABEL = {"premarket": "Premarket", "regular": "Regular hours", "afterhours": "After-hours"}


def session_at(ts: float) -> str | None:
    t = datetime.fromtimestamp(ts, NY)
    d = t.date()
    if t.weekday() >= 5 or d in HOLIDAYS:
        return None
    close = dtime(13, 0) if d in EARLY_CLOSE else dtime(16, 0)
    post_end = dtime(17, 0) if d in EARLY_CLOSE else dtime(20, 0)
    now = t.time()
    if dtime(4, 0) <= now < dtime(9, 30):
        return "premarket"
    if dtime(9, 30) <= now < close:
        return "regular"
    if close <= now < post_end:
        return "afterhours"
    return None


# ─────────────────────────────── helpers ───────────────────────────────


def to_float(x) -> float | None:
    if x is None:
        return None
    if isinstance(x, (int, float)):
        return float(x) if math.isfinite(x) else None
    s = str(x).strip().replace("$", "").replace(",", "").replace("%", "")
    if s.upper() == "UNCH":
        return 0.0
    try:
        return float(s)
    except ValueError:
        return None


def fp(p: float | None) -> str:
    if p is None:
        return "?"
    return f"${p:.4f}" if p < 1 else f"${p:.3f}" if p < 10 else f"${p:.2f}"


def pct(x: float | None) -> str:
    return "?" if x is None else f"{x * 100:+.0f}%"


def human(n: float | None) -> str:
    if n is None:
        return "?"
    for div, suf in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if abs(n) >= div:
            return f"{n / div:.1f}{suf}"
    return f"{n:.0f}"


def clamp01(x: float) -> float:
    return max(0.0, min(1.0, x))


def clock_line(ts: float) -> str:
    return (f"{datetime.fromtimestamp(ts, UK):%H:%M} UK / "
            f"{datetime.fromtimestamp(ts, NY):%H:%M} ET")


def age_str(seconds: float) -> str:
    m = max(0, int(seconds // 60))
    return f"{m}m ago" if m < 90 else f"{m // 60}h ago"


def localname(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


# ─────────────────────────────── HTTP ──────────────────────────────────


class Http:
    """requests wrapper with retries, back-off and per-source health counters."""

    def __init__(self) -> None:
        self.s = requests.Session()
        adapter = requests.adapters.HTTPAdapter(pool_connections=20, pool_maxsize=20)
        self.s.mount("https://", adapter)
        self.health: dict[str, list[int]] = defaultdict(lambda: [0, 0])
        self._lock = threading.Lock()
        self._yahoo_primed = False

    def _mark(self, source: str, ok: bool) -> None:
        with self._lock:
            self.health[source][0 if ok else 1] += 1

    def prime_yahoo(self) -> None:
        if self._yahoo_primed:
            return
        self._yahoo_primed = True
        try:  # sets the consent cookie Yahoo expects; a 404 response is normal
            self.s.get("https://fc.yahoo.com", headers=YAHOO_HEADERS, timeout=10)
        except requests.RequestException:
            pass

    def _get(self, url, params, headers, source, timeout):
        last = ""
        for attempt in range(3):
            try:
                r = self.s.get(url, params=params, headers=headers, timeout=timeout)
            except requests.RequestException as e:
                last = type(e).__name__
                time.sleep(1 + attempt * 2)
                continue
            if r.status_code == 404:          # unknown/delisted symbol: not a source failure
                self._mark(source, True)
                return None
            if r.status_code == 429 or r.status_code >= 500:
                last = f"HTTP {r.status_code}"
                time.sleep(2 + attempt * 3)
                continue
            if r.status_code != 200:
                last = f"HTTP {r.status_code}"
                break
            self._mark(source, True)
            return r
        self._mark(source, False)
        log.debug("GET failed %s (%s): %s", source, last, url)
        return None

    def get_json(self, url, params=None, headers=None, source="web", timeout=20):
        r = self._get(url, params, headers, source, timeout)
        if r is None:
            return None
        try:
            return r.json()
        except ValueError:
            self._mark(source, False)
            return None

    def get_text(self, url, params=None, headers=None, source="web", timeout=20):
        r = self._get(url, params, headers, source, timeout)
        return None if r is None else r.text

    def post(self, url, json_body=None, data=None, headers=None) -> bool:
        for attempt in range(3):
            try:
                r = self.s.post(url, json=json_body, data=data, headers=headers, timeout=15)
                if r.status_code == 429:
                    time.sleep(float(r.headers.get("Retry-After", 2)) + 0.5)
                    continue
                if r.status_code < 300:
                    return True
                log.warning("Alert POST failed (HTTP %s): %s", r.status_code, r.text[:200])
                return False
            except requests.RequestException as e:
                log.warning("Alert POST error: %s", e)
                time.sleep(1 + attempt)
        return False


# ─────────────────────────────── data: universe ────────────────────────

SYMBOL_RE = re.compile(r"^[A-Z]{1,5}$")
SKIP_NAME_RE = re.compile(r"\b(warrants?|units?|rights?|preferred|depositary shares? representing .*preferred|notes due)\b", re.I)


def fetch_universe(http: Http, cfg: Config) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for ex in cfg.exchanges:
        data = http.get_json(
            "https://api.nasdaq.com/api/screener/stocks",
            params={"tableonly": "true", "limit": "25", "offset": "0", "exchange": ex, "download": "true"},
            headers=NASDAQ_HEADERS, source="nasdaq", timeout=40)
        rows = (((data or {}).get("data") or {}).get("rows")) or []
        if not rows:
            log.warning("Universe: no rows for %s", ex)
        for row in rows:
            sym = (row.get("symbol") or "").strip().upper()
            name = (row.get("name") or "").strip()
            if not SYMBOL_RE.match(sym) or SKIP_NAME_RE.search(name):
                continue
            last = to_float(row.get("lastsale"))
            chg = to_float(row.get("netchange")) or 0.0
            if not last or last <= 0:
                continue
            prev = last - chg if last - chg > 0 else last
            if min(last, prev) < cfg.max_price:
                out[sym] = {"name": name, "exchange": ex.upper(), "last": last, "prev": prev,
                            "volume": to_float(row.get("volume")) or 0}
    return out


# ─────────────────────────────── data: prices ──────────────────────────


@dataclass
class Series:
    symbol: str
    ts: list
    o: list
    h: list
    l: list
    c: list
    v: list
    prev_close: float | None


def parse_chart_result(res: dict | None, symbol: str | None = None) -> Series | None:
    if not res:
        return None
    meta = res.get("meta") or {}
    ts = res.get("timestamp") or []
    q = (((res.get("indicators") or {}).get("quote")) or [{}])[0] or {}
    c = q.get("close") or []
    n = min(len(ts), len(c))
    if n == 0:
        return None

    def col(key):
        arr = q.get(key) or []
        return arr if len(arr) >= n else [None] * n

    o, h, l, v = col("open"), col("high"), col("low"), col("volume")
    S = Series(symbol or meta.get("symbol") or "?", [], [], [], [], [], [],
               to_float(meta.get("chartPreviousClose")) or to_float(meta.get("previousClose")))
    for i in range(n):
        ci = to_float(c[i])
        if ci is None or ci <= 0:
            continue
        S.ts.append(int(ts[i]))
        S.c.append(ci)
        S.o.append(to_float(o[i]) or ci)
        S.h.append(max(to_float(h[i]) or ci, ci))
        S.l.append(min(to_float(l[i]) or ci, ci))
        S.v.append(to_float(v[i]))
    return S if S.c else None


def parse_spark(data) -> dict[str, Series]:
    """Handles both Yahoo spark response shapes (v7 nested and v8 flat)."""
    out: dict[str, Series] = {}
    if not isinstance(data, dict):
        return out
    if "spark" in data:
        for res in (data.get("spark") or {}).get("result") or []:
            sym = res.get("symbol")
            resp = (res.get("response") or [None])[0]
            s = parse_chart_result(resp, sym)
            if s and sym:
                out[sym] = s
    else:
        for sym, v in data.items():
            if isinstance(v, dict) and "close" in v and "timestamp" in v:
                res = {"meta": {"chartPreviousClose": v.get("chartPreviousClose") or v.get("previousClose")},
                       "timestamp": v.get("timestamp"), "indicators": {"quote": [{"close": v.get("close")}]}}
                s = parse_chart_result(res, sym)
                if s:
                    out[sym] = s
    return out


def fetch_spark(http: Http, symbols: list[str]) -> dict[str, Series]:
    http.prime_yahoo()
    out: dict[str, Series] = {}
    chunks = [symbols[i:i + 20] for i in range(0, len(symbols), 20)]

    def one(chunk):
        data = http.get_json("https://query1.finance.yahoo.com/v7/finance/spark",
                             params={"symbols": ",".join(chunk), "range": "1d", "interval": "5m",
                                     "includePrePost": "true"},
                             headers=YAHOO_HEADERS, source="yahoo")
        return parse_spark(data)

    with ThreadPoolExecutor(max_workers=4) as pool:
        for res in pool.map(one, chunks):
            out.update(res)
    return out


def fetch_chart(http: Http, sym: str, interval="1m", rng="1d", prepost=True) -> Series | None:
    http.prime_yahoo()
    data = http.get_json(f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}",
                         params={"interval": interval, "range": rng,
                                 "includePrePost": "true" if prepost else "false"},
                         headers=YAHOO_HEADERS, source="yahoo")
    res = (((data or {}).get("chart") or {}).get("result") or [None])[0]
    return parse_chart_result(res, sym)


def fetch_charts(http: Http, symbols: list[str]) -> dict[str, Series]:
    out = {}
    with ThreadPoolExecutor(max_workers=6) as pool:
        for sym, s in zip(symbols, pool.map(lambda s: fetch_chart(http, s), symbols)):
            if s:
                out[sym] = s
    return out


def fetch_daily_stats(http: Http, sym: str) -> dict | None:
    s = fetch_chart(http, sym, interval="1d", rng="3mo", prepost=False)
    if not s or len(s.c) < 15:
        return None
    closes = s.c[:-1]  # exclude today's (possibly partial) bar
    rets = [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes)) if closes[i - 1] > 0]
    rets = rets[-60:]
    vols = [v for v in s.v[:-1] if v][-60:]
    if len(rets) < 10:
        return None
    return {"sigma": statistics.pstdev(rets) or 0.05, "avgvol": statistics.mean(vols) if vols else 0}


def fetch_bid_ask(http: Http, sym: str) -> tuple[float, float] | None:
    data = http.get_json(f"https://api.nasdaq.com/api/quote/{sym}/info",
                         params={"assetclass": "stocks"}, headers=NASDAQ_HEADERS, source="nasdaq", timeout=10)
    pd = ((data or {}).get("data") or {}).get("primaryData") or {}
    bid, ask = to_float(pd.get("bidPrice")), to_float(pd.get("askPrice"))
    if bid and ask and ask >= bid > 0:
        return bid, ask
    return None


# ─────────────────────────────── analysis ──────────────────────────────


@dataclass
class Analysis:
    symbol: str
    last: float
    prev_close: float
    day_gain: float
    g5: float
    g15: float
    g30: float
    accel: float
    rel_vol: float | None
    dv15: float
    vwap: float | None
    hl_frac: float
    score: float
    last_ts: int
    n_bars: int


def _price_at(ts, c, target, fallback):
    p = None
    for t, ci in zip(ts, c):
        if t <= target:
            p = ci
        else:
            break
    return p if p is not None else fallback


def _slope(ts, c, t0, t1):
    pts = [((t - t0) / 60.0, math.log(ci)) for t, ci in zip(ts, c) if t0 <= t <= t1]
    if len(pts) < 3:
        return None
    mx = sum(p[0] for p in pts) / len(pts)
    my = sum(p[1] for p in pts) / len(pts)
    den = sum((p[0] - mx) ** 2 for p in pts)
    if den == 0:
        return None
    return sum((p[0] - mx) * (p[1] - my) for p in pts) / den


def today_slice(s: Series) -> Series:
    day = datetime.fromtimestamp(s.ts[-1], NY).date()
    idx = [i for i, t in enumerate(s.ts) if datetime.fromtimestamp(t, NY).date() == day]
    pick = lambda arr: [arr[i] for i in idx]
    return Series(s.symbol, pick(s.ts), pick(s.o), pick(s.h), pick(s.l), pick(s.c), pick(s.v), s.prev_close)


def quick_metrics(s: Series, fallback_prev: float | None) -> tuple[float, float, float] | None:
    """(last, day_gain, 15m_gain) from any bar series (spark has 5-minute bars)."""
    t = today_slice(s)
    if not t.c:
        return None
    prev = t.prev_close or fallback_prev or t.o[0]
    last, anchor = t.c[-1], t.ts[-1]
    p15 = _price_at(t.ts, t.c, anchor - 15 * 60, t.o[0])
    return last, last / prev - 1, last / p15 - 1 if p15 else 0.0


def analyze(s: Series, fallback_prev: float | None) -> Analysis | None:
    t = today_slice(s)
    if len(t.c) < 3:
        return None
    ts, c, v = t.ts, t.c, t.v
    anchor = ts[-1]
    last = c[-1]
    prev = t.prev_close or fallback_prev or t.o[0]
    first_open = t.o[0]

    def gain(mins):
        p = _price_at(ts, c, anchor - mins * 60, first_open)
        return last / p - 1 if p else 0.0

    g5, g15, g30 = gain(5), gain(15), gain(30)

    recent = _slope(ts, c, anchor - 10 * 60, anchor)
    prior = _slope(ts, c, anchor - 40 * 60, anchor - 10 * 60)
    if recent is None or recent < 0.002:   # under ~0.2%/min isn't a move worth calling acceleration
        accel = 1.0 if recent is None or recent >= 0 else 0.0
    elif prior is None:
        accel = 2.0 if recent > 0 else 0.0
    elif prior <= 0:
        accel = 4.0 if recent > 0 else 0.0
    else:
        accel = recent / prior

    cut = anchor - 15 * 60
    rec = [(ci, vi) for ti, ci, vi in zip(ts, c, v) if ti > cut]
    base = [vi for ti, vi in zip(ts, v) if ti <= cut and vi]
    rec_vols = [vi or 0 for _, vi in rec]
    dv15 = sum(ci * (vi or 0) for ci, vi in rec)
    rel_vol = None
    if len(base) >= 10 and rec_vols:
        rel_vol = (sum(rec_vols) / len(rec_vols)) / max(statistics.median(base), 1.0)

    num = sum(((hi + lo + ci) / 3) * (vi or 0) for hi, lo, ci, vi in zip(t.h, t.l, c, v))
    den = sum(vi or 0 for vi in v)
    vwap = num / den if den > 0 else None

    lows = t.l[-9:]
    hl_frac = (sum(1 for i in range(1, len(lows)) if lows[i] >= lows[i - 1]) / (len(lows) - 1)) if len(lows) > 1 else 0.0

    score = (
        clamp01((last / prev - 1) / 1.0) * 20
        + clamp01(g15 / 0.30) * 25
        + clamp01(g5 / 0.10) * 5
        + (clamp01((accel - 1) / 2.0) * 20 if accel > 1 else 0)
        + (clamp01(math.log(rel_vol) / math.log(10)) * 20 if rel_vol and rel_vol > 1 else (10 if rel_vol is None else 0))
        + 10 * hl_frac * (1.0 if vwap is None or last >= vwap else 0.5)
    )
    return Analysis(s.symbol, last, prev, last / prev - 1, g5, g15, g30, accel, rel_vol, dv15, vwap,
                    hl_frac, score, anchor, len(c))


# ─────────────────────────────── catalysts ─────────────────────────────


@dataclass
class Catalyst:
    symbol: str
    source: str
    kind: str        # bullish | bearish | blackswan | halt | neutral
    weight: int      # 0-5
    title: str
    detail: str
    url: str
    ts: float
    key: str


HEADLINE_RULES = [
    ("blackswan", 5, "Bankruptcy", r"\bchapter (11|7)\b|\bbankruptcy\b|\breceivership\b|\binsolven"),
    ("blackswan", 5, "Regulator / fraud action", r"\bSEC (charges|suspends|halts)|trading suspension|\bfraud\b|\bindict|\bsubpoena"),
    ("blackswan", 4, "Clinical hold / FDA rejection", r"clinical hold|complete response letter|\bCRL\b|refus(e|al) to file"),
    ("blackswan", 4, "Going concern / restatement", r"going concern|\brestate(ment|s|d)?\b|non-reliance"),
    ("blackswan", 4, "Delisting decision", r"delisting determination|suspended from trading|(will be|to be) delisted|delisted from"),
    ("bullish", 5, "Buyout / merger", r"to be acquired|definitive (merger )?agreement|merger agreement|\bbuyout\b|tender offer|take[- ]private|go[- ]private"),
    ("bullish", 5, "FDA approval", r"\bFDA\b.{0,40}\b(approv|clear|grant)|\b(approv|clear)\w*\b.{0,30}\bFDA\b"),
    ("bullish", 4, "Positive trial data", r"met (its |the )?primary endpoint|positive (topline|top-line|phase|interim|pivotal)|statistically significant"),
    ("bullish", 3, "FDA designation", r"breakthrough (therapy|device)|fast track|orphan drug|priority review"),
    ("bullish", 3, "Big-name partner", r"\b(nvidia|tesla|amazon|microsoft|google|alphabet|apple|walmart|openai|spacex|pfizer|lockheed|boeing|department of (defense|energy)|pentagon)\b"),
    ("bullish", 3, "Contract / order", r"\bcontract\b|\bawarded\b|purchase order|\borders? (worth|valued|totaling)"),
    ("bullish", 2, "Partnership / licence", r"partnership|collaboration|licens(e|ing) agreement|strategic (alliance|investment)|joint venture|\bMOU\b|letter of intent"),
    ("bullish", 2, "Crypto / AI pivot", r"bitcoin|crypto|ethereum|solana|digital asset treasury|\bAI\b|artificial intelligence"),
    ("bullish", 2, "Buyback / insider buying", r"(share|stock) (buyback|repurchase)|insider (buy|purchase)"),
    ("bullish", 2, "Strong results", r"record revenue|revenue (up|increase|grew)|turns? profitable|first profitable"),
    ("bearish", 3, "Share offering (dilution)", r"public offering|registered direct|private placement|at[- ]the[- ]market|\bATM (program|offering|facility)|pricing of|warrant (inducement|exercise)|equity line"),
    ("bearish", 3, "Trial failure", r"did not meet|failed to (meet|achieve)|discontinu(e|es|ed|ing) (the )?(trial|study|development|program)"),
    ("bearish", 2, "Reverse split", r"reverse (stock |share )?split"),
    ("bearish", 2, "Listing deficiency", r"minimum bid|non-?compliance|deficiency (notice|letter)|delisting notice|nasdaq notification"),
    ("bearish", 1, "Executive exit", r"\b(CEO|CFO|chief executive|chief financial).{0,30}(resign|depart|step(s|ping)? down)"),
]
_RULES = [(k, w, lbl, re.compile(p, re.I)) for k, w, lbl, p in HEADLINE_RULES]
_KIND_RANK = {"blackswan": 3, "bearish": 2, "bullish": 1, "neutral": 0}


def classify_headline(text: str) -> tuple[str, int, str]:
    best = ("neutral", 0, "News")
    for kind, w, lbl, rx in _RULES:
        if rx.search(text) and (w, _KIND_RANK[kind]) > (best[1], _KIND_RANK[best[0]]):
            best = (kind, w, lbl)
    return best


SEC_ITEMS = {
    "1.01": ("bullish", 2, "Material agreement"), "1.02": ("bearish", 1, "Agreement terminated"),
    "1.03": ("blackswan", 5, "Bankruptcy / receivership"), "2.01": ("bullish", 2, "Acquisition / disposal completed"),
    "2.02": ("neutral", 1, "Results"), "2.04": ("blackswan", 4, "Debt default / acceleration"),
    "2.06": ("bearish", 2, "Material impairment"), "3.01": ("bearish", 2, "Delisting notice / transfer"),
    "3.02": ("bearish", 2, "Unregistered share sale (dilution)"), "3.03": ("bearish", 1, "Shareholder rights changed"),
    "4.01": ("bearish", 2, "Auditor change"), "4.02": ("blackswan", 4, "Past financials unreliable"),
    "5.01": ("bullish", 3, "Change in control"), "5.02": ("neutral", 1, "Officer / director change"),
    "5.03": ("neutral", 1, "Charter change (often reverse split)"), "7.01": ("neutral", 1, "Reg FD disclosure"),
    "8.01": ("neutral", 1, "Other events"),
}
SEC_FORMS = {
    "6-K": ("neutral", 1, "Foreign issuer report"), "S-1": ("bearish", 3, "Share registration (dilution)"),
    "424B": ("bearish", 3, "Offering prospectus (dilution)"), "SC 13D": ("bullish", 2, "5%+ activist stake"),
    "SCHEDULE 13D": ("bullish", 2, "5%+ activist stake"), "425": ("bullish", 3, "Merger communication"),
    "SC TO-T": ("bullish", 4, "Tender offer"),
}
SEC_FEEDS = ["8-K", "6-K", "S-1", "424B", "SC 13D", "SCHEDULE 13D", "425", "SC TO-T"]
SEC_TITLE_RE = re.compile(r"^(?P<form>.+?) - (?P<name>.+) \((?P<cik>\d{10})\) \((?P<role>[^)]+)\)\s*$")


def fetch_cik_map(http: Http, cfg: Config, universe: dict) -> dict[str, str]:
    data = http.get_json("https://www.sec.gov/files/company_tickers.json",
                         headers={"User-Agent": cfg.sec_ua or "PennyScanner/1.0"}, source="sec", timeout=30)
    out = {}
    for row in (data or {}).values() if isinstance(data, dict) else []:
        t = str(row.get("ticker", "")).upper()
        if t in universe:
            out[str(row.get("cik_str")).zfill(10)] = t
    return out


def parse_sec_atom(xml_text: str, cik_map: dict[str, str]) -> list[Catalyst]:
    out = []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return out
    for entry in root:
        if localname(entry.tag) != "entry":
            continue
        f = {localname(ch.tag): ch for ch in entry}
        title = (f["title"].text or "") if "title" in f else ""
        m = SEC_TITLE_RE.match(title.strip())
        if not m:
            continue
        sym = cik_map.get(m["cik"])
        if not sym:
            continue
        form = m["form"].strip()
        summary = html.unescape((f["summary"].text or "") if "summary" in f else "")
        url = f["link"].get("href", "") if "link" in f else ""
        key = (f["id"].text or url) if "id" in f else url
        try:
            ts = datetime.fromisoformat((f["updated"].text or "").strip()).timestamp()
        except (KeyError, ValueError):
            ts = time.time()
        parts = []
        for prefix, spec in SEC_FORMS.items():
            if form.upper().startswith(prefix):
                parts.append(spec)
        for item in re.findall(r"Item (\d\.\d\d)", summary):
            if item in SEC_ITEMS:
                parts.append(SEC_ITEMS[item])
        if not parts:
            parts = [("neutral", 1, "Filing")]
        kind, weight, _ = max(parts, key=lambda p: (p[1], _KIND_RANK[p[0]]))
        labels = list(dict.fromkeys(p[2] for p in parts))
        out.append(Catalyst(sym, "SEC", kind, weight, f"{form}: {', '.join(labels[:3])}",
                            m["name"], url, ts, f"sec:{key}"))
    return out


HALT_CODES = {
    "T1": ("halt", "News pending"), "T2": ("halt", "News released"), "T3": ("halt", "News & resumption times"),
    "T5": ("halt", "Single-stock pause"), "T6": ("halt", "Extraordinary market activity"),
    "T12": ("blackswan", "Exchange requested more information"), "H4": ("blackswan", "Listing non-compliance"),
    "H9": ("blackswan", "Not current in filings"), "H10": ("blackswan", "SEC trading suspension"),
    "H11": ("blackswan", "Regulatory concern"), "LUDP": ("halt", "Volatility pause (LULD)"),
    "LUDS": ("halt", "Volatility pause (LULD straddle)"), "M": ("halt", "Volatility pause"),
    "MWC1": ("blackswan", "Market-wide circuit breaker"), "D": ("blackswan", "Delisted"),
}


def parse_halts(xml_text: str, universe: dict) -> list[Catalyst]:
    out = []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return out
    for item in root.iter():
        if localname(item.tag) != "item":
            continue
        f = {localname(ch.tag): (ch.text or "").strip() for ch in item}
        sym = f.get("IssueSymbol", "").upper()
        if sym not in universe and not f.get("ReasonCode", "").startswith(("MWC",)):
            continue
        code = f.get("ReasonCode", "")
        kind, reason = HALT_CODES.get(code, ("halt", f"Halt code {code or '?'}"))
        try:
            ts = datetime.strptime(f"{f.get('HaltDate')} {f.get('HaltTime')}", "%m/%d/%Y %H:%M:%S").replace(tzinfo=NY).timestamp()
        except ValueError:
            ts = time.time()
        resume = f.get("ResumptionTradeTime") or ""
        detail = f"Halted {f.get('HaltTime', '?')} ET on {f.get('Market', '?')}"
        if f.get("PauseThresholdPrice"):
            detail += f" · pause price {f['PauseThresholdPrice']}"
        detail += f" · resumes {resume} ET" if resume else " · no resumption time yet"
        out.append(Catalyst(sym, "Halt", kind, 5 if kind == "blackswan" else 4, f"{code}: {reason}", detail,
                            "https://www.nasdaqtrader.com/trader.aspx?id=TradeHalts", ts,
                            f"halt:{sym}:{f.get('HaltDate')}:{f.get('HaltTime')}:{code}:{bool(resume)}"))
    return out


NEWS_FEEDS = [
    ("GlobeNewswire", "https://www.globenewswire.com/RssFeed/orgclass/1/feedTitle/GlobeNewswire%20-%20News%20about%20Public%20Companies"),
    ("PR Newswire", "https://www.prnewswire.com/rss/news-releases-list.rss"),
]
TICKER_IN_TEXT = re.compile(r"\b(?:NASDAQ|Nasdaq|NYSE(?:\s?American|\s?Arca|\s?MKT)?)\s*(?:CM|GM|GS)?\s*:\s*\"?([A-Z]{1,5})\b")


def parse_rss(xml_text: str) -> list[dict]:
    items = []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return items
    for it in root.iter():
        if localname(it.tag) != "item":
            continue
        f = {localname(ch.tag): (ch.text or "").strip() for ch in it}
        try:
            ts = parsedate_to_datetime(f.get("pubDate", "")).timestamp()
        except (TypeError, ValueError):
            ts = time.time()
        desc = re.sub(r"<[^>]+>", " ", html.unescape(f.get("description", "")))
        items.append({"title": html.unescape(f.get("title", "")), "desc": desc, "url": f.get("link", ""),
                      "id": f.get("guid") or f.get("link") or f.get("title"), "ts": ts})
    return items


def news_catalysts(items: list[dict], source: str, universe: dict, symbol: str | None = None) -> list[Catalyst]:
    out = []
    for it in items:
        syms = {symbol} if symbol else set(TICKER_IN_TEXT.findall(it["title"] + " " + it["desc"][:600]))
        for sym in syms:
            if sym not in universe:
                continue
            kind, w, label = classify_headline(it["title"] + " " + it["desc"][:300])
            out.append(Catalyst(sym, source, kind, w, it["title"][:200], label, it["url"], it["ts"],
                                f"news:{sym}:{it['id']}"))
    return out


# ─────────────────────────────── state ─────────────────────────────────


class State:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.d = {"alerts": {}, "seen": {}, "catalysts": {}, "daily": {}, "universe": {}, "cik": {},
                  "rotation": 0, "notices": {}}
        if path.exists():
            try:
                self.d.update(json.loads(path.read_text()))
            except (ValueError, OSError):
                log.warning("State file unreadable; starting fresh")

    def save(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.d, separators=(",", ":")))
        tmp.replace(self.path)

    def prune(self, now: float) -> None:
        self.d["seen"] = {k: t for k, t in self.d["seen"].items() if now - t < 3 * 86400}
        self.d["alerts"] = {k: a for k, a in self.d["alerts"].items() if now - a["t"] < 86400}
        cats = {}
        for sym, lst in self.d["catalysts"].items():
            keep = [c for c in lst if now - c["ts"] < 86400]
            if keep:
                cats[sym] = keep[-5:]
        self.d["catalysts"] = cats
        today = datetime.fromtimestamp(now, NY).date().isoformat()
        self.d["daily"] = {s: v for s, v in self.d["daily"].items() if v.get("d") == today}

    def seen(self, key: str) -> bool:
        return key in self.d["seen"]

    def mark_seen(self, key: str, now: float) -> None:
        self.d["seen"][key] = now

    def add_catalyst(self, c: Catalyst) -> None:
        self.d["catalysts"].setdefault(c.symbol, []).append(
            {"kind": c.kind, "w": c.weight, "title": c.title, "src": c.source, "url": c.url, "ts": c.ts})

    def best_catalyst(self, sym: str) -> dict | None:
        lst = self.d["catalysts"].get(sym) or []
        return max(lst, key=lambda c: (c["w"], c["ts"])) if lst else None

    def should_alert(self, kind: str, sym: str, price: float | None, now: float, cooldown_min: int,
                     realert_gain: float) -> bool:
        a = self.d["alerts"].get(f"{kind}:{sym}")
        if not a:
            return True
        if now - a["t"] >= cooldown_min * 60:
            return True
        return bool(kind == "PARABOLIC" and price and a.get("price") and price >= a["price"] * (1 + realert_gain))

    def record_alert(self, kind: str, sym: str, price: float | None, now: float) -> None:
        self.d["alerts"][f"{kind}:{sym}"] = {"t": now, "price": price}

    def recently_alerted(self, now: float, within: float = 7200) -> set[str]:
        return {k.split(":", 1)[1] for k, a in self.d["alerts"].items() if now - a["t"] < within}


# ─────────────────────────────── alerts ────────────────────────────────

EMOJI = {"PARABOLIC": "🚀", "CATALYST": "⚡", "BLACKSWAN": "🦢", "HALT": "⛔", "CRASH": "📉", "INFO": "🟢", "WARN": "⚠️"}
LABEL = {"PARABOLIC": "PARABOLIC", "CATALYST": "CATALYST", "BLACKSWAN": "BLACK SWAN", "HALT": "HALT",
         "CRASH": "CRASH", "INFO": "Scanner", "WARN": "Scanner warning"}
NTFY_TAGS = {"PARABOLIC": "rocket", "CATALYST": "zap", "BLACKSWAN": "swan", "HALT": "no_entry",
             "CRASH": "chart_with_downwards_trend", "INFO": "green_circle", "WARN": "warning"}


@dataclass
class Alert:
    kind: str
    symbol: str
    title: str
    lines: list = field(default_factory=list)
    links: list = field(default_factory=list)
    priority: int = 3
    price: float | None = None


class Notifier:
    def __init__(self, cfg: Config, http: Http) -> None:
        self.cfg, self.http = cfg, http
        self.channels = [n for n, ok in (("telegram", cfg.telegram_token and cfg.telegram_chat),
                                         ("discord", cfg.discord_webhook), ("ntfy", cfg.ntfy_topic)) if ok]
        if not self.channels:
            log.warning("No alert channel configured — alerts will only be printed to the log.")

    def head(self, a: Alert) -> str:
        sym = f": {a.symbol}" if a.symbol else ""
        return f"{EMOJI[a.kind]} {LABEL[a.kind]}{sym}"

    def send(self, a: Alert) -> None:
        log.info("ALERT %s %s — %s | %s", a.kind, a.symbol, a.title, " | ".join(a.lines))
        if "telegram" in self.channels:
            text = f"<b>{html.escape(self.head(a))}</b> — {html.escape(a.title)}\n" + "\n".join(html.escape(x) for x in a.lines)
            if a.links:
                text += "\n" + " · ".join(f'<a href="{html.escape(u, quote=True)}">{html.escape(lbl)}</a>' for lbl, u in a.links)
            self.http.post(f"https://api.telegram.org/bot{self.cfg.telegram_token}/sendMessage",
                           json_body={"chat_id": self.cfg.telegram_chat, "text": text[:4000], "parse_mode": "HTML",
                                      "disable_web_page_preview": True})
        if "discord" in self.channels:
            text = f"**{self.head(a)}** — {a.title}\n" + "\n".join(a.lines)
            if a.links:
                text += "\n" + " · ".join(f"[{lbl}](<{u}>)" for lbl, u in a.links)
            self.http.post(self.cfg.discord_webhook, json_body={"content": text[:1990]})
        if "ntfy" in self.channels:
            safe = lambda s: s.encode("latin-1", "ignore").decode("latin-1")
            headers = {"Title": safe(f"{LABEL[a.kind]}{': ' + a.symbol if a.symbol else ''} - {a.title}")[:200],
                       "Tags": NTFY_TAGS[a.kind], "Priority": str(max(1, min(5, a.priority)))}
            if a.links:
                headers["Click"] = a.links[0][1]
            self.http.post(f"{self.cfg.ntfy_server}/{self.cfg.ntfy_topic}",
                           data="\n".join(a.lines).encode("utf-8"), headers=headers)
        if "telegram" in self.channels:
            time.sleep(1.0)  # Telegram allows ~1 msg/sec per chat


def links_for(sym: str, exchange: str) -> list[tuple[str, str]]:
    ex = "NYSE" if exchange.upper().startswith("NYSE") else "NASDAQ"
    return [("Chart", f"https://www.tradingview.com/chart/?symbol={ex}:{sym}"),
            ("News", f"https://finance.yahoo.com/quote/{sym}/news"),
            ("Filings", f"https://www.sec.gov/edgar/search/#/q=&entityName={sym}")]


# ─────────────────────────────── scanner ───────────────────────────────


class Scanner:
    def __init__(self, cfg: Config, http: Http, state: State, notifier: Notifier, clock=time.time) -> None:
        self.cfg, self.http, self.state, self.notifier, self.clock = cfg, http, state, notifier, clock
        self.universe: dict[str, dict] = {}
        self.cik_map: dict[str, str] = {}
        self.last_quick: dict[str, tuple] = {}

    # ---- setup ----
    def load_universe(self) -> int:
        uni = fetch_universe(self.http, self.cfg)
        now = self.clock()
        if len(uni) >= 20:
            self.universe = uni
            self.state.d["universe"] = {"t": now, "rows": uni}
        else:
            cached = self.state.d.get("universe") or {}
            self.universe = cached.get("rows") or {}
            if self.universe:
                log.warning("Using cached universe from %s", age_str(now - cached.get("t", now)))
        today = datetime.fromtimestamp(now, NY).date().isoformat()
        cik = self.state.d.get("cik") or {}
        if cik.get("d") == today and cik.get("map"):
            self.cik_map = cik["map"]
        else:
            fresh = fetch_cik_map(self.http, self.cfg, self.universe) if self.universe else {}
            self.cik_map = fresh or cik.get("map") or {}
            if fresh:
                self.state.d["cik"] = {"d": today, "map": fresh}
        log.info("Universe: %d stocks under %s on %s · %d mapped to SEC CIKs",
                 len(self.universe), fp(self.cfg.max_price), "/".join(e.upper() for e in self.cfg.exchanges), len(self.cik_map))
        return len(self.universe)

    # ---- catalysts ----
    def collect_catalysts(self) -> list[Catalyst]:
        cats: list[Catalyst] = []
        sec_headers = {"User-Agent": self.cfg.sec_ua or "PennyScanner/1.0", "Accept-Encoding": "gzip, deflate"}
        for form in SEC_FEEDS:
            txt = self.http.get_text("https://www.sec.gov/cgi-bin/browse-edgar",
                                     params={"action": "getcurrent", "type": form, "company": "", "dateb": "",
                                             "owner": "include", "start": "0", "count": "100", "output": "atom"},
                                     headers=sec_headers, source="sec")
            if txt:
                cats += parse_sec_atom(txt, self.cik_map)
            time.sleep(0.15)  # SEC fair-access: stay well under 10 req/s
        txt = self.http.get_text("https://www.nasdaqtrader.com/rss.aspx", params={"feed": "tradehalts"},
                                 headers={"User-Agent": BROWSER_UA}, source="halts")
        if txt:
            cats += parse_halts(txt, self.universe)
        for name, url in NEWS_FEEDS:
            txt = self.http.get_text(url, headers={"User-Agent": BROWSER_UA}, source="news")
            if txt:
                cats += news_catalysts(parse_rss(txt), name, self.universe)
        return cats

    def symbol_news(self, sym: str, now: float) -> Catalyst | None:
        txt = self.http.get_text("https://feeds.finance.yahoo.com/rss/2.0/headline",
                                 params={"s": sym, "region": "US", "lang": "en-US"},
                                 headers={"User-Agent": BROWSER_UA}, source="news")
        if not txt:
            return None
        cats = [c for c in news_catalysts(parse_rss(txt), "Yahoo News", self.universe, symbol=sym)
                if now - c.ts < 24 * 3600]
        for c in cats:
            if not self.state.seen(c.key):
                self.state.mark_seen(c.key, now)
                self.state.add_catalyst(c)
        return max(cats, key=lambda c: (c.weight, c.ts)) if cats else None

    def catalyst_alert(self, c: Catalyst, now: float) -> Alert | None:
        if c.kind == "halt":
            kind = "HALT"
        elif c.kind == "blackswan":
            kind = "BLACKSWAN"
        elif c.weight >= self.cfg.catalyst_min_weight:
            kind = "CATALYST"
        else:
            return None
        u = self.universe.get(c.symbol, {})
        q = self.last_quick.get(c.symbol)
        sentiment = {"bullish": "🟢 Bullish", "bearish": "🔴 Bearish (often dilution / pressure)",
                     "blackswan": "🦢 Black-swan event", "halt": "⛔ Trading halted"}.get(c.kind, "Neutral")
        lines = [f"{u.get('exchange', '')} · {u.get('name', '')}".strip(" ·"),
                 f"{sentiment} · {c.source} · {age_str(now - c.ts)}"]
        if c.detail and c.source != "SEC":
            lines.append(c.detail)
        if q:
            lines.append(f"Price {fp(q[0])} ({pct(q[1])} today, {pct(q[2])} in 15m)")
        else:
            lines.append(f"Last {fp(u.get('last'))} · prev close {fp(u.get('prev'))}")
        session = self.cfg.forced_session or session_at(now)
        lines.append(f"{SESSION_LABEL.get(session, 'Closed')} · {clock_line(now)}")
        links = ([("Source", c.url)] if c.url else []) + links_for(c.symbol, u.get("exchange", ""))[:2]
        prio = 5 if kind in ("BLACKSWAN", "HALT") else 4
        return Alert(kind, c.symbol, c.title, lines, links, prio, q[0] if q else None)

    # ---- price scan ----
    def quick_scan(self, syms: list[str]) -> dict[str, Series]:
        series = fetch_spark(self.http, syms) if syms else {}
        if syms and len(series) < max(5, 0.2 * len(syms)):
            n = self.cfg.fallback_per_cycle
            rot = self.state.d.get("rotation", 0) % max(len(syms), 1)
            batch = (syms[rot:] + syms[:rot])[:n]
            self.state.d["rotation"] = rot + n
            log.info("Batch price feed thin (%d/%d) — checking %d symbols individually", len(series), len(syms), len(batch))
            series.update(fetch_charts(self.http, batch))
        return series

    def cycle(self) -> list[Alert]:
        now = self.clock()
        session = self.cfg.forced_session or session_at(now)
        ext = session != "regular"
        alerts: list[Alert] = []
        self.state.prune(now)

        # 1) catalysts first — they explain moves and can arrive before the move
        new_cats: list[Catalyst] = []
        for c in self.collect_catalysts():
            if self.state.seen(c.key):
                continue
            self.state.mark_seen(c.key, now)
            self.state.add_catalyst(c)
            fresh = now - c.ts <= self.cfg.catalyst_max_age_min * 60
            ongoing_halt = c.source == "Halt" and "no resumption" in c.detail and \
                datetime.fromtimestamp(c.ts, NY).date() == datetime.fromtimestamp(now, NY).date()
            if fresh or ongoing_halt:
                new_cats.append(c)

        # 2) cheap scan of every sub-$1 stock
        syms = sorted(self.universe)
        quick = self.quick_scan(syms)
        stale_limit = 30 * 60
        self.last_quick = {}
        for sym, s in quick.items():
            if not self.cfg.forced_session and now - s.ts[-1] > stale_limit:
                continue
            m = quick_metrics(s, self.universe.get(sym, {}).get("prev"))
            if m:
                self.last_quick[sym] = m

        # 3) pick movers for a deep 1-minute look
        min15 = self.cfg.min_15m_extended if ext else self.cfg.min_15m_regular
        hot = {sym for sym, (_, day, g15) in self.last_quick.items()
               if day >= self.cfg.min_day_gain * 0.66 or g15 >= min15 * 0.75 or day <= self.cfg.crash_day}
        hot |= {s for s, lst in self.state.d["catalysts"].items() if any(now - c["ts"] < 7200 for c in lst)}
        hot |= self.state.recently_alerted(now) & set(self.universe)
        ranked = sorted(hot, key=lambda s: -max(abs(self.last_quick.get(s, (0, 0, 0))[1]),
                                                  abs(self.last_quick.get(s, (0, 0, 0))[2]) * 3))
        deep = ranked[: self.cfg.max_deep_scans]
        charts = fetch_charts(self.http, deep)

        for sym in deep:
            s = charts.get(sym)
            if not s or (not self.cfg.forced_session and now - s.ts[-1] > stale_limit):
                continue
            u = self.universe.get(sym, {})
            a = analyze(s, u.get("prev"))
            if not a:
                continue
            self.last_quick[sym] = (a.last, a.day_gain, a.g15)
            mindv = self.cfg.min_dv_extended if ext else self.cfg.min_dv_regular
            parabolic = (a.prev_close < self.cfg.max_price and a.day_gain >= self.cfg.min_day_gain
                         and a.g15 >= min15 and a.dv15 >= mindv and a.score >= self.cfg.parabolic_score
                         and (a.vwap is None or a.last >= a.vwap))
            crash = a.day_gain <= self.cfg.crash_day and a.g15 <= -0.10 and a.dv15 >= mindv / 2
            if not (parabolic or crash):
                continue
            z = self.zscore(sym, a, now)
            if parabolic and self.state.should_alert("PARABOLIC", sym, a.last, now, self.cfg.cooldown_min, self.cfg.realert_gain):
                alerts.append(self.parabolic_alert(sym, a, z, session, now))
            elif crash:
                kind = "BLACKSWAN" if z is not None and z <= -self.cfg.sigma else "CRASH"
                if self.state.should_alert(kind, sym, a.last, now, self.cfg.cooldown_min, self.cfg.realert_gain):
                    alerts.append(self.crash_alert(kind, sym, a, z, session, now))

        # 4) catalyst alerts (built now so they carry the live price). A plain catalyst for a
        #    stock that is already going parabolic this cycle is folded into that alert.
        running = {a.symbol for a in alerts if a.kind == "PARABOLIC"}
        for c in new_cats:
            a = self.catalyst_alert(c, now)
            if a and not (a.kind == "CATALYST" and c.symbol in running):
                alerts.append(a)

        # 5) rank, enrich with bid/ask, send
        alerts.sort(key=lambda x: -x.priority)
        out, overflow = [], []
        for a in alerts:
            (out if len(out) < self.cfg.max_alerts_per_cycle else overflow).append(a)
        for a in out:
            if a.symbol and a.kind in ("PARABOLIC", "CRASH", "BLACKSWAN", "HALT", "CATALYST"):
                ba = fetch_bid_ask(self.http, a.symbol)
                if ba:
                    bid, ask = ba
                    spread = (ask - bid) / ((ask + bid) / 2)
                    a.lines.insert(2, f"Bid/ask {fp(bid)} / {fp(ask)} · spread {spread * 100:.1f}%"
                                      + (" ⚠️ wide" if spread > 0.05 else ""))
            self.notifier.send(a)
            if a.kind in ("PARABOLIC", "CRASH", "BLACKSWAN"):
                self.state.record_alert(a.kind, a.symbol, a.price, now)
        if overflow:
            self.notifier.send(Alert("INFO", "", f"{len(overflow)} more signals this cycle",
                                     [", ".join(f"{EMOJI[a.kind]} {a.symbol}" for a in overflow)], [], 2))
            for a in overflow:
                if a.kind in ("PARABOLIC", "CRASH", "BLACKSWAN"):
                    self.state.record_alert(a.kind, a.symbol, a.price, now)
        return out + overflow

    def zscore(self, sym: str, a: Analysis, now: float) -> float | None:
        today = datetime.fromtimestamp(now, NY).date().isoformat()
        st = self.state.d["daily"].get(sym)
        if not st:
            stats = fetch_daily_stats(self.http, sym)
            st = {"d": today, **(stats or {"sigma": None, "avgvol": None})}
            self.state.d["daily"][sym] = st
        if not st.get("sigma") or a.last <= 0 or a.prev_close <= 0:
            return None
        return math.log(a.last / a.prev_close) / st["sigma"]

    def parabolic_alert(self, sym: str, a: Analysis, z: float | None, session: str, now: float) -> Alert:
        u = self.universe.get(sym, {})
        cat = self.state.best_catalyst(sym)
        if not cat:
            c = self.symbol_news(sym, now)
            cat = self.state.best_catalyst(sym) if c else None
        lines = [f"{SESSION_LABEL.get(session, session)} · {u.get('exchange', '')} · {u.get('name', '')}",
                 f"Price {fp(a.last)}  ({pct(a.day_gain)} today · {pct(a.g15)} in 15m · {pct(a.g5)} in 5m)",
                 f"Prev close {fp(a.prev_close)} · VWAP {fp(a.vwap)} · score {a.score:.0f}/100",
                 f"15m $ volume {human(a.dv15)} · rel vol {('%.1fx' % a.rel_vol) if a.rel_vol else 'n/a'} · accel {a.accel:.1f}x"]
        if z is not None and abs(z) >= self.cfg.sigma:
            lines.append(f"🦢 {z:+.1f}σ move vs its 60-day volatility")
        if cat:
            lines.append(f"Catalyst: {cat['title']} ({cat['src']}, {age_str(now - cat['ts'])})")
        else:
            lines.append("No catalyst found — momentum / possible pump, size carefully")
        lines.append(clock_line(now))
        prio = 5 if (cat and cat["w"] >= 4) or (z and z >= self.cfg.sigma) else 4
        return Alert("PARABOLIC", sym, f"{pct(a.day_gain)} to {fp(a.last)}", lines, links_for(sym, u.get("exchange", "")),
                     prio, a.last)

    def crash_alert(self, kind: str, sym: str, a: Analysis, z: float | None, session: str, now: float) -> Alert:
        u = self.universe.get(sym, {})
        cat = self.state.best_catalyst(sym) or (self.symbol_news(sym, now) and self.state.best_catalyst(sym))
        lines = [f"{SESSION_LABEL.get(session, session)} · {u.get('exchange', '')} · {u.get('name', '')}",
                 f"Price {fp(a.last)}  ({pct(a.day_gain)} today · {pct(a.g15)} in 15m)",
                 f"15m $ volume {human(a.dv15)}" + (f" · {z:+.1f}σ move" if z is not None else "")]
        if cat:
            lines.append(f"Likely cause: {cat['title']} ({cat['src']})")
        lines.append(clock_line(now))
        return Alert(kind, sym, f"{pct(a.day_gain)} to {fp(a.last)}", lines, links_for(sym, u.get("exchange", "")), 5, a.last)

    # ---- housekeeping ----
    def health_line(self) -> str:
        parts = []
        for src in ("nasdaq", "yahoo", "sec", "halts", "news"):
            ok, bad = self.http.health.get(src, [0, 0])
            parts.append(f"{src} {'✓' if ok and ok >= bad else ('✗' if bad else '–')}")
        return " · ".join(parts)

    def notices(self, session: str, now: float) -> None:
        today = datetime.fromtimestamp(now, NY).date().isoformat()
        nt = self.state.d.setdefault("notices", {})
        if self.cfg.heartbeat and nt.get("heartbeat") != f"{today}:{session}":
            nt["heartbeat"] = f"{today}:{session}"
            self.notifier.send(Alert("INFO", "", f"online — {SESSION_LABEL.get(session, session)}",
                                     [f"Watching {len(self.universe)} stocks under {fp(self.cfg.max_price)} on "
                                      f"{'/'.join(e.upper() for e in self.cfg.exchanges)}",
                                      f"Sources: {self.health_line()}", clock_line(now)], [], 2))
        for src, what in (("yahoo", "Price data (Yahoo) — parabolic/crash alerts are paused"),
                          ("sec", "SEC filings — set SEC_USER_AGENT to 'Your Name your@email'"),
                          ("nasdaq", "Nasdaq stock list — using the cached list if there is one")):
            ok, bad = self.http.health.get(src, [0, 0])
            if bad and not ok and nt.get(f"warn:{src}") != today:
                nt[f"warn:{src}"] = today
                self.notifier.send(Alert("WARN", "", f"{src} unreachable", [what, clock_line(now)], [], 3))


# ─────────────────────────────── main ──────────────────────────────────


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Sub-$1 parabolic / catalyst / black-swan scanner")
    ap.add_argument("--duration", type=int, default=560, help="seconds to keep scanning (default 560)")
    ap.add_argument("--once", action="store_true", help="run a single cycle")
    ap.add_argument("--session", choices=["premarket", "regular", "afterhours"],
                    help="force a session (testing outside market hours)")
    ap.add_argument("--state", default="state.json")
    ap.add_argument("--test-alert", action="store_true", help="send a sample alert to every channel and exit")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")

    cfg = Config()
    cfg.forced_session = args.session
    http = Http()
    notifier = Notifier(cfg, http)

    if args.test_alert:
        now = time.time()
        notifier.send(Alert("PARABOLIC", "TEST", "+142% to $0.8400 (sample)", [
            "Premarket · NASDAQ · Example Therapeutics Inc. (this is a test)",
            "Price $0.8400  (+142% today · +38% in 15m · +9% in 5m)",
            "Prev close $0.3470 · VWAP $0.6120 · score 91/100",
            "15m $ volume 1.2M · rel vol 14.2x · accel 3.1x",
            "Catalyst: FDA approval headline (GlobeNewswire, 6m ago)", clock_line(now)],
            [("Chart", "https://www.tradingview.com/")], 3))
        print("Test alert sent to:", ", ".join(notifier.channels) or "log only (no channels configured)")
        return 0 if notifier.channels else 1

    if args.session is None and session_at(time.time()) is None:
        log.info("US market closed (%s) — nothing to do.", clock_line(time.time()))
        return 0
    if not cfg.sec_ua:
        log.warning("SEC_USER_AGENT not set — SEC may refuse filing requests.")

    state = State(Path(args.state))
    scanner = Scanner(cfg, http, state, notifier)
    if not scanner.load_universe():
        log.error("No stock universe available (Nasdaq screener unreachable and no cache).")
        state.save()
        scanner.notices(args.session or session_at(time.time()) or "regular", time.time())
        state.save()
        return 2

    end = time.time() + args.duration
    first = True
    while True:
        started = time.time()
        session = args.session or session_at(started)
        if session is None:
            log.info("Session ended.")
            break
        try:
            alerts = scanner.cycle()
            log.info("Cycle done in %.0fs · %d alerts · %s", time.time() - started, len(alerts), scanner.health_line())
        except Exception:  # keep the loop alive; one bad response shouldn't kill the run
            log.exception("Cycle failed")
        if first:
            scanner.notices(session, time.time())
            first = False
        state.save()
        if args.once or time.time() + cfg.cycle_seconds > end:
            break
        time.sleep(max(5, cfg.cycle_seconds - (time.time() - started)))
    state.save()
    return 0


if __name__ == "__main__":
    sys.exit(main())
