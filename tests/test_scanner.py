"""Offline tests: simulated market data, no network needed.  Run:  python -m unittest -v"""
import math
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import scanner as S  # noqa: E402

NY = S.NY


def et(y, mo, d, h, mi, s=0):
    return datetime(y, mo, d, h, mi, s, tzinfo=NY).timestamp()


NOW = et(2026, 11, 9, 8, 30)  # Monday premarket


def chart_json(sym, prev, bars):
    ts, o, h, l, c, v = zip(*bars)
    return {"chart": {"result": [{"meta": {"symbol": sym, "chartPreviousClose": prev},
                                  "timestamp": list(ts),
                                  "indicators": {"quote": [{"open": list(o), "high": list(h), "low": list(l),
                                                            "close": list(c), "volume": list(v)}]}}], "error": None}}


def parabolic_bars():
    """Flat around $0.42 from 04:00, then goes vertical 07:50→08:30 to ~$0.95 on huge volume."""
    bars, t = [], et(2026, 11, 9, 4, 0)
    price = 0.42
    while t <= NOW:
        mins_into_run = (t - et(2026, 11, 9, 7, 50)) / 60
        if mins_into_run < 0:
            p, vol = 0.42 + 0.002 * math.sin(t / 600), 3_000
        else:
            p, vol = 0.42 * math.exp(0.0005 * mins_into_run ** 2 + 0.002 * mins_into_run), 60_000 + 6_000 * mins_into_run
        bars.append((int(t), price, max(price, p) * 1.003, min(price, p) * 0.998, p, vol))
        price = p
        t += 60
    return bars


def flat_bars(level=0.55):
    bars, t = [], et(2026, 11, 9, 4, 0)
    while t <= NOW:
        p = level * (1 + 0.003 * math.sin(t / 300))
        bars.append((int(t), p, p * 1.002, p * 0.998, p, 2_000))
        t += 60
    return bars


def crash_bars():
    bars, t = [], et(2026, 11, 9, 4, 0)
    price = 0.80
    while t <= NOW:
        m = (t - et(2026, 11, 9, 8, 5)) / 60
        p = 0.80 if m < 0 else max(0.80 * (1 - 0.025 * m), 0.30)
        bars.append((int(t), price, max(p, price), min(p, price), p, 4_000 if m < 0 else 150_000))
        price = p
        t += 60
    return bars


UNIVERSE = {"data": {"rows": [
    {"symbol": "ROKT", "name": "Rocket Bio Inc. Common Stock", "lastsale": "$0.40", "netchange": "-0.01", "pctchange": "-2%", "volume": "1,200,000"},
    {"symbol": "FLAT", "name": "Flatline Corp Common Stock", "lastsale": "$0.55", "netchange": "UNCH", "pctchange": "0%", "volume": "300,000"},
    {"symbol": "DOWN", "name": "Downhill Holdings Common Stock", "lastsale": "$0.80", "netchange": "0.02", "pctchange": "2%", "volume": "900,000"},
    {"symbol": "BIGC", "name": "Big Cap Co Common Stock", "lastsale": "$45.10", "netchange": "0.5", "pctchange": "1%", "volume": "9,000,000"},
    {"symbol": "ROKTW", "name": "Rocket Bio Inc. Warrants", "lastsale": "$0.02", "netchange": "0", "pctchange": "0%", "volume": "100"},
] + [{"symbol": f"Z{chr(65 + i // 26)}{chr(65 + i % 26)}", "name": f"Filler {i} Inc", "lastsale": "$0.50",
      "netchange": "0", "pctchange": "0%", "volume": "1000"} for i in range(30)]}}

SEC_ATOM = """<?xml version="1.0" encoding="ISO-8859-1" ?>
<feed xmlns="http://www.w3.org/2005/Atom">
<title>Latest Filings</title>
<entry>
<title>8-K - Rocket Bio Inc. (0001111111) (Filer)</title>
<link rel="alternate" type="text/html" href="https://www.sec.gov/Archives/edgar/data/1111111/000111111126000001-index.htm"/>
<summary type="html"> &lt;b&gt;Filed:&lt;/b&gt; 2026-11-09 &lt;b&gt;AccNo:&lt;/b&gt; 0001111111-26-000001 &lt;b&gt;Size:&lt;/b&gt; 300 KB&lt;br&gt;Item 1.01: Entry into a Material Definitive Agreement&lt;br&gt;Item 5.01: Changes in Control of Registrant</summary>
<updated>2026-11-09T08:10:00-05:00</updated>
<id>urn:tag:sec.gov,2008:accession-number=0001111111-26-000001</id>
</entry>
<entry>
<title>8-K - Downhill Holdings (0002222222) (Filer)</title>
<link rel="alternate" type="text/html" href="https://www.sec.gov/Archives/edgar/data/2222222/x-index.htm"/>
<summary type="html"> &lt;b&gt;Filed:&lt;/b&gt; 2026-11-09 &lt;br&gt;Item 1.03: Bankruptcy or Receivership</summary>
<updated>2026-11-09T08:00:00-05:00</updated>
<id>urn:tag:sec.gov,2008:accession-number=0002222222-26-000009</id>
</entry>
<entry>
<title>8-K - Big Cap Co (0003333333) (Filer)</title>
<link rel="alternate" type="text/html" href="https://www.sec.gov/x"/>
<summary type="html">Item 8.01: Other Events</summary>
<updated>2026-11-09T08:00:00-05:00</updated>
<id>urn:tag:sec.gov,2008:accession-number=0003333333-26-000001</id>
</entry>
</feed>"""

HALTS_RSS = """<?xml version="1.0" encoding="utf-8"?>
<rss version="2.0" xmlns:ndaq="http://www.nasdaqtrader.com/"><channel><title>NASDAQTrader.com</title>
<item><title>FLAT</title>
<ndaq:HaltDate>11/09/2026</ndaq:HaltDate><ndaq:HaltTime>08:20:00</ndaq:HaltTime>
<ndaq:IssueSymbol>FLAT</ndaq:IssueSymbol><ndaq:IssueName>Flatline Corp</ndaq:IssueName>
<ndaq:Market>NASDAQ</ndaq:Market><ndaq:ReasonCode>T1</ndaq:ReasonCode>
<ndaq:PauseThresholdPrice></ndaq:PauseThresholdPrice><ndaq:ResumptionDate></ndaq:ResumptionDate>
<ndaq:ResumptionQuoteTime></ndaq:ResumptionQuoteTime><ndaq:ResumptionTradeTime></ndaq:ResumptionTradeTime>
</item>
<item><title>BIGC</title>
<ndaq:HaltDate>11/09/2026</ndaq:HaltDate><ndaq:HaltTime>08:21:00</ndaq:HaltTime>
<ndaq:IssueSymbol>BIGC</ndaq:IssueSymbol><ndaq:Market>NYSE</ndaq:Market><ndaq:ReasonCode>LUDP</ndaq:ReasonCode>
</item>
</channel></rss>"""

NEWS_RSS = """<?xml version="1.0" encoding="utf-8"?>
<rss version="2.0"><channel><title>wire</title>
<item><title>Rocket Bio Announces FDA Approval of RB-101 for Rare Disease</title>
<link>https://example.com/rokt-fda</link><guid>gnw-1</guid>
<description>BOSTON, Nov. 09, 2026 -- Rocket Bio Inc. (NASDAQ: ROKT) today announced the U.S. FDA has approved...</description>
<pubDate>Mon, 09 Nov 2026 13:15:00 GMT</pubDate></item>
<item><title>Flatline Corp Announces Pricing of $3 Million Registered Direct Offering</title>
<link>https://example.com/flat-offering</link><guid>gnw-2</guid>
<description>Flatline Corp (Nasdaq: FLAT) announced pricing of a registered direct offering</description>
<pubDate>Mon, 09 Nov 2026 13:05:00 GMT</pubDate></item>
<item><title>Big Cap Co beats earnings</title><link>https://example.com/b</link><guid>gnw-3</guid>
<description>(NYSE: BIGC) record revenue</description><pubDate>Mon, 09 Nov 2026 13:00:00 GMT</pubDate></item>
</channel></rss>"""

CIK_JSON = {"0": {"cik_str": 1111111, "ticker": "ROKT", "title": "Rocket Bio"},
            "1": {"cik_str": 2222222, "ticker": "DOWN", "title": "Downhill"},
            "2": {"cik_str": 3333333, "ticker": "BIGC", "title": "Big Cap"}}


def daily_json(sym, base):
    t0 = et(2026, 8, 1, 16, 0)
    bars = []
    for i in range(70):
        p = base * (1 + 0.03 * math.sin(i))
        bars.append((int(t0 + i * 86400), p, p, p, p, 500_000))
    return chart_json(sym, base, bars)


class FakeHttp(S.Http):
    """Routes URLs to fixtures and captures outgoing alerts."""

    def __init__(self, spark_works=True):
        super().__init__()
        self.sent = []
        self.spark_works = spark_works
        self.charts = {"ROKT": chart_json("ROKT", 0.40, parabolic_bars()),
                       "FLAT": chart_json("FLAT", 0.55, flat_bars()),
                       "DOWN": chart_json("DOWN", 0.80, crash_bars())}

    def prime_yahoo(self):
        pass

    def get_json(self, url, params=None, headers=None, source="web", timeout=20):
        self._mark(source, True)
        if "screener/stocks" in url:
            return UNIVERSE if params["exchange"] == "nasdaq" else {"data": {"rows": []}}
        if "company_tickers" in url:
            return CIK_JSON
        if "/v7/finance/spark" in url:
            if not self.spark_works:
                self.health[source] = [0, 1]
                return {"finance": {"error": {"code": "Unauthorized"}}}
            res = []
            for sym in params["symbols"].split(","):
                cj = self.charts.get(sym)
                if cj:
                    r = cj["chart"]["result"][0]
                    # spark only has 5-minute closes
                    ts = r["timestamp"][::5]
                    cl = r["indicators"]["quote"][0]["close"][::5]
                    res.append({"symbol": sym, "response": [{"meta": r["meta"], "timestamp": ts,
                                                             "indicators": {"quote": [{"close": cl}]}}]})
            return {"spark": {"result": res, "error": None}}
        if "/v8/finance/chart/" in url:
            sym = url.rsplit("/", 1)[-1]
            if params.get("interval") == "1d":
                return daily_json(sym, {"ROKT": 0.4, "DOWN": 0.8}.get(sym, 0.5))
            return self.charts.get(sym)
        if "/api/quote/" in url:
            return {"data": {"primaryData": {"bidPrice": "$0.9400", "askPrice": "$0.9550"}}}
        return None

    def get_text(self, url, params=None, headers=None, source="web", timeout=20):
        self._mark(source, True)
        if "browse-edgar" in url:
            return SEC_ATOM if params["type"] == "8-K" else '<feed xmlns="http://www.w3.org/2005/Atom"></feed>'
        if "nasdaqtrader" in url:
            return HALTS_RSS
        if "globenewswire" in url:
            return NEWS_RSS
        if "prnewswire" in url or "feeds.finance.yahoo" in url:
            return '<rss><channel></channel></rss>'
        return None

    def post(self, url, json_body=None, data=None, headers=None):
        self.sent.append((url, json_body, data, headers))
        return True


def make_scanner(http=None, tmp=None, clock=NOW):
    cfg = S.Config()
    cfg.telegram_token, cfg.telegram_chat = "T", "C"
    cfg.discord_webhook = "https://discord.example/hook"
    cfg.ntfy_topic = "topic"
    http = http or FakeHttp()
    st = S.State(Path(tmp) / "state.json")
    n = S.Notifier(cfg, http)
    S.time.sleep = lambda *_: None  # speed: no Telegram pacing / SEC pacing in tests
    sc = S.Scanner(cfg, http, st, n, clock=lambda: clock)
    return sc, http, st


class TestCalendar(unittest.TestCase):
    def test_sessions_winter(self):
        self.assertEqual(S.session_at(et(2026, 11, 9, 3, 59)), None)
        self.assertEqual(S.session_at(et(2026, 11, 9, 4, 0)), "premarket")
        self.assertEqual(S.session_at(et(2026, 11, 9, 9, 30)), "regular")
        self.assertEqual(S.session_at(et(2026, 11, 9, 16, 0)), "afterhours")
        self.assertEqual(S.session_at(et(2026, 11, 9, 20, 0)), None)

    def test_weekend_holiday_earlyclose(self):
        self.assertIsNone(S.session_at(et(2026, 11, 14, 10, 0)))       # Saturday
        self.assertIsNone(S.session_at(et(2026, 11, 26, 10, 0)))       # Thanksgiving
        self.assertEqual(S.session_at(et(2026, 11, 27, 13, 30)), "afterhours")  # early close
        self.assertIsNone(S.session_at(et(2026, 11, 27, 17, 30)))

    def test_summer_time(self):
        # 08:00 UTC in July = 04:00 EDT premarket open
        ts = datetime(2027, 7, 13, 8, 0, tzinfo=S.timezone.utc).timestamp()
        self.assertEqual(S.session_at(ts), "premarket")


class TestParsing(unittest.TestCase):
    def test_universe_filters(self):
        http = FakeHttp()
        uni = S.fetch_universe(http, S.Config())
        self.assertIn("ROKT", uni)
        self.assertNotIn("BIGC", uni)       # over $1
        self.assertNotIn("ROKTW", uni)      # warrant
        self.assertAlmostEqual(uni["ROKT"]["prev"], 0.41)
        self.assertAlmostEqual(uni["FLAT"]["prev"], 0.55)

    def test_spark_shapes(self):
        v7 = {"spark": {"result": [{"symbol": "AAA", "response": [{"meta": {"chartPreviousClose": 0.5},
              "timestamp": [1, 2], "indicators": {"quote": [{"close": [0.5, None]}]}}]}]}}
        v8 = {"BBB": {"symbol": "BBB", "timestamp": [1, 2], "close": [0.3, 0.33], "chartPreviousClose": 0.3}}
        self.assertEqual(S.parse_spark(v7)["AAA"].c, [0.5])
        self.assertEqual(S.parse_spark(v8)["BBB"].c, [0.3, 0.33])
        self.assertEqual(S.parse_spark({"finance": {"error": {}}}), {})

    def test_sec(self):
        cats = S.parse_sec_atom(SEC_ATOM, {"0001111111": "ROKT", "0002222222": "DOWN"})
        by = {c.symbol: c for c in cats}
        self.assertEqual(by["ROKT"].kind, "bullish")
        self.assertEqual(by["ROKT"].weight, 3)
        self.assertIn("Change in control", by["ROKT"].title)
        self.assertEqual(by["DOWN"].kind, "blackswan")
        self.assertNotIn("BIGC", by)

    def test_halts(self):
        cats = S.parse_halts(HALTS_RSS, {"FLAT": {}})
        self.assertEqual(len(cats), 1)
        self.assertEqual(cats[0].title, "T1: News pending")
        self.assertIn("no resumption", cats[0].detail)

    def test_headlines(self):
        self.assertEqual(S.classify_headline("XYZ files voluntary Chapter 11 petitions")[0], "blackswan")
        self.assertEqual(S.classify_headline("Company receives FDA approval for drug")[:2], ("bullish", 5))
        self.assertEqual(S.classify_headline("Announces pricing of $5M registered direct offering")[0], "bearish")
        self.assertEqual(S.classify_headline("FDA issues Complete Response Letter")[0], "blackswan")
        self.assertEqual(S.classify_headline("Company to host webcast")[0], "neutral")
        items = S.parse_rss(NEWS_RSS)
        cats = S.news_catalysts(items, "GNW", {"ROKT": {}, "FLAT": {}})
        self.assertEqual({c.symbol for c in cats}, {"ROKT", "FLAT"})


class TestAnalysis(unittest.TestCase):
    def test_parabolic_scores_high(self):
        s = S.parse_chart_result(chart_json("ROKT", 0.40, parabolic_bars())["chart"]["result"][0])
        a = S.analyze(s, 0.40)
        self.assertGreater(a.day_gain, 0.9)
        self.assertGreater(a.g15, 0.15)
        self.assertGreater(a.accel, 1.2)
        self.assertGreater(a.rel_vol, 5)
        self.assertGreaterEqual(a.score, 75)

    def test_flat_scores_low(self):
        s = S.parse_chart_result(chart_json("FLAT", 0.55, flat_bars())["chart"]["result"][0])
        a = S.analyze(s, 0.55)
        self.assertLess(a.score, 30)

    def test_cooldown(self):
        with tempfile.TemporaryDirectory() as d:
            st = S.State(Path(d) / "s.json")
            self.assertTrue(st.should_alert("PARABOLIC", "X", 0.5, 1000, 30, 0.25))
            st.record_alert("PARABOLIC", "X", 0.5, 1000)
            self.assertFalse(st.should_alert("PARABOLIC", "X", 0.55, 1300, 30, 0.25))
            self.assertTrue(st.should_alert("PARABOLIC", "X", 0.65, 1300, 30, 0.25))   # new leg up
            self.assertTrue(st.should_alert("PARABOLIC", "X", 0.5, 1000 + 1801, 30, 0.25))


class TestEndToEnd(unittest.TestCase):
    def run_cycle(self, http=None):
        with tempfile.TemporaryDirectory() as d:
            sc, http, st = make_scanner(http, d)
            self.assertGreater(sc.load_universe(), 20)
            alerts = sc.cycle()
            st.save()
            again = sc.cycle()          # same data again: nothing should repeat
            return alerts, again, http, sc

    def test_full_cycle(self):
        alerts, again, http, sc = self.run_cycle()
        got = {(a.kind, a.symbol) for a in alerts}
        self.assertIn(("PARABOLIC", "ROKT"), got)
        self.assertIn(("BLACKSWAN", "DOWN"), got)          # 8-K item 1.03 bankruptcy
        self.assertIn(("HALT", "FLAT"), got)               # T1 news pending
        self.assertIn(("CATALYST", "FLAT"), got)           # registered direct offering
        self.assertNotIn(("CATALYST", "ROKT"), got)        # folded into the parabolic alert
        self.assertNotIn("BIGC", {a.symbol for a in alerts})
        self.assertTrue(any(k in ("CRASH", "BLACKSWAN") and s == "DOWN" for k, s in got))
        rokt = next(a for a in alerts if a.kind == "PARABOLIC")
        text = "\n".join(rokt.lines)
        self.assertIn("Catalyst:", text)
        self.assertIn("Bid/ask", text)
        self.assertEqual(again, [])
        # each alert went to all three channels
        urls = [u for u, *_ in http.sent]
        self.assertTrue(any("api.telegram.org" in u for u in urls))
        self.assertTrue(any("discord" in u for u in urls))
        self.assertTrue(any("ntfy.sh/topic" in u for u in urls))
        for u, body, data, headers in http.sent:
            if headers:  # ntfy headers must be latin-1 encodable
                for v in headers.values():
                    v.encode("latin-1")
        print("\n--- sample alerts ---")
        for a in alerts:
            print(f"{S.EMOJI[a.kind]} {S.LABEL[a.kind]}: {a.symbol} — {a.title}")
            for line in a.lines:
                print("   ", line)

    def test_fallback_when_batch_feed_blocked(self):
        alerts, _, _, _ = self.run_cycle(FakeHttp(spark_works=False))
        self.assertIn(("PARABOLIC", "ROKT"), {(a.kind, a.symbol) for a in alerts})

    def test_heartbeat_and_warning(self):
        with tempfile.TemporaryDirectory() as d:
            sc, http, st = make_scanner(None, d)
            sc.load_universe()
            sc.http.health["yahoo"] = [0, 5]
            sc.notices("premarket", NOW)
            titles = [h.get("Title", "") for _, _, _, h in http.sent if h]
            self.assertTrue(any("online" in t for t in titles))
            self.assertTrue(any("yahoo unreachable" in t for t in titles))
            n = len(http.sent)
            sc.notices("premarket", NOW)       # not repeated the same day/session
            self.assertEqual(len(http.sent), n)


class TestTelegramChatDiscovery(unittest.TestCase):
    def test_finds_and_remembers_chat_id(self):
        class TG(FakeHttp):
            calls = 0

            def get_json(self, url, params=None, headers=None, source="web", timeout=20):
                if "getUpdates" in url:
                    TG.calls += 1
                    return {"ok": True, "result": [{"update_id": 1, "message": {"chat": {"id": 7123456789}, "text": "hi"}}]}
                return super().get_json(url, params, headers, source, timeout)

        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "state.json"
            cfg = S.Config()
            cfg.telegram_token, cfg.telegram_chat = "T", ""
            S.resolve_telegram_chat(cfg, TG(), p)
            self.assertEqual(cfg.telegram_chat, "7123456789")
            st = S.State(p)                      # scanner state keeps it
            st.save()
            cfg2 = S.Config()
            cfg2.telegram_token, cfg2.telegram_chat = "T", ""
            S.resolve_telegram_chat(cfg2, TG(), p)
            self.assertEqual(cfg2.telegram_chat, "7123456789")
            self.assertEqual(TG.calls, 1)        # second time read from saved state


if __name__ == "__main__":
    unittest.main()
