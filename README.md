# Penny Stock Scanner (under $1, NASDAQ + NYSE)

Runs free on GitHub's servers from premarket to the end of after-hours and sends alerts to your phone. You don't need to install anything on your phone or laptop.

| Alert | What triggers it |
|---|---|
| 🚀 **PARABOLIC** | A stock that closed under $1 is up 15%+ on the day and 8%+ in 15 minutes (12% in pre/after-hours). The climb has to be speeding up, on surging volume, with real dollar volume, and the price has to be above VWAP. The alert includes a 0–100 score. |
| ⚡ **CATALYST** | A new 8-K/6-K, offering (S-1/424B), merger (425), tender offer, 13D stake, or a press release (FDA approval, buyout, contract, big-name partner, dilution, reverse split…). |
| 🦢 **BLACK SWAN** | Bankruptcy (8-K 1.03), SEC suspension, fraud, clinical hold or FDA rejection, restatement (4.02), debt default (2.04), a delisting decision, or a price move of 4σ or more against the stock's 60-day volatility. |
| ⛔ **HALT** | A trading halt: news pending (T1), a volatility pause (LUDP), or the exchange or SEC asking for information (T12/H10). |
| 📉 **CRASH** | Down 35%+ on the day and still dropping fast on volume. |

Each alert shows the price, % moves, volume, bid/ask spread, the most likely catalyst, and links to the chart, news and SEC filings. Sessions run from 09:00 to 01:00 UK time in winter (4am–8pm ET). The scanner handles US holidays, early closes and clock changes itself.

---

## Setup (about 10 minutes, works from a phone browser)

### 1. Get your alert destination (pick one or more)

**Telegram (recommended)**
1. In Telegram, message **@BotFather**, send `/newbot` and follow the prompts. Copy the **token** it gives you.
2. Open your new bot and press **Start** (otherwise it can't message you).
3. Message **@userinfobot**. It replies with your numeric **ID**, which is your chat ID.

**Discord**
Server Settings → Integrations → Webhooks → New Webhook → choose a channel → **Copy Webhook URL**.

### 2. Create the GitHub repo
1. Sign up at github.com (free), then tap **+ → New repository**. Give it a name like `penny-scanner` and set it to **Public**, then create it.
   *It must be public. Public repos get unlimited free run time, while private ones get 2,000 minutes a month, which this would use up in about 3 days. Your keys stay hidden either way (see step 3).*
2. Tap **Add file → Upload files**, upload `scanner.py` and `requirements.txt`, then tap **Commit changes**.
3. Tap **Add file → Create new file**. For the name, type exactly `.github/workflows/scan.yml`, paste in the contents of `scan.yml`, and tap **Commit changes**.

### 3. Add your keys as Secrets (hidden, even in a public repo)
Go to Repo → **Settings → Secrets and variables → Actions → New repository secret**:

| Name | Value |
|---|---|
| `TELEGRAM_BOT_TOKEN` | the BotFather token |
| `TELEGRAM_CHAT_ID` | your numeric ID |
| `DISCORD_WEBHOOK_URL` | the webhook URL (if using Discord) |
| `SEC_USER_AGENT` | your name and email, e.g. `Jane Smith jane@example.com`. The SEC requires this to serve filings. |

### 4. Turn it on and test
1. Open the **Actions** tab and tap **"I understand… enable"** if GitHub asks.
2. Tap **Penny scanner → Run workflow**, tick **"Just send a test alert"**, then **Run**. A sample alert should reach your phone within about a minute.
3. To try a real scan outside market hours, run it again with **force_session = premarket**. It scans the latest data once.

After that it runs on its own every 10 minutes on trading days. Each run scans continuously for about 9 minutes, so coverage is close to continuous. At the start of each session you get a 🟢 "online" message listing which data sources are working. If a source starts failing, you get a ⚠️ message.

---

## Tuning (optional)
Go to Settings → Secrets and variables → Actions → **Variables** tab → New variable:

| Variable | Default | Meaning |
|---|---|---|
| `MAX_PRICE` | `1.00` | Price ceiling |
| `EXCHANGES` | `nasdaq,nyse` | Add `amex` for NYSE American |
| `PARABOLIC_SCORE` | `65` | Raise to get fewer, stronger alerts |
| `MIN_DAY_GAIN` | `0.15` | Minimum gain on the day (0.15 = 15%) |
| `MIN_15M_GAIN_REGULAR` / `_EXTENDED` | `0.08` / `0.12` | Minimum 15-minute gain |
| `MIN_DOLLAR_VOL_15M_REGULAR` / `_EXTENDED` | `25000` / `10000` | Filters out stocks too illiquid to trade |
| `BLACK_SWAN_SIGMA` | `4` | Size of move that counts as a statistical black swan |
| `CRASH_DAY_DROP` | `-0.35` | Day drop needed for a crash alert |
| `CATALYST_MIN_WEIGHT` | `3` | Catalyst strength 1–5. Set to `4` for only major news |
| `ALERT_COOLDOWN_MIN` | `30` | Minutes before the same stock can alert again. A new leg up of 25% or more re-alerts immediately |
| `HEARTBEAT` | `on` | `off` turns off the session "online" messages |

## Good to know
- **Delay:** GitHub can start scheduled runs 5–15 minutes late at busy times, so the very first minutes of a move can be missed. Inside a run, each scan loop takes about 90 seconds.
- **Data:** the scanner uses free public sources (Nasdaq, Yahoo Finance, SEC EDGAR, Nasdaq Trader halts, GlobeNewswire, PR Newswire) with no API keys. Yahoo sometimes rate-limits shared cloud servers. If that happens the scanner switches to checking stocks one by one, and the ⚠️ message tells you.
- **Keep it alive:** GitHub pauses scheduled workflows in a repo with no activity for 60 days. If alerts stop, open Actions and re-enable the workflow, or make any small edit to the repo.
- **Testing locally (optional):** `pip install requests` → `python scanner.py --session premarket --once -v`. Offline tests: `python -m unittest -v`.
- This is a lead generator, not advice. Sub-$1 stocks are often pumped, diluted (watch for 🔴 offering alerts) or halted, and spreads can be very wide outside regular hours.
