"""
VOIDAURA — XAUUSD Strict Confluence Signal Bot
Checks EMA9/21 + VWAP + Stoch RSI + MACD on 5m AND 15m.
Only alerts when ALL 4 indicators agree on BOTH timeframes.
Sends signal via Telegram. Polls every 60 seconds.

Active sessions (UTC):
  Asian session:  01:00 - 04:00 UTC  (06:30 - 09:30 IST)
  London session: 08:00 - 12:00 UTC  (13:30 - 17:30 IST)
  New York session: 13:00 - 17:00 UTC (18:30 - 22:30 IST)
"""

import requests
import time
import logging
from datetime import datetime

# ── CONFIG ─────────────────────────────────────────────────────────────────────
TWELVEDATA_API_KEY = "89f768d64ccf41e59baa1080e2ae8d8d"
TELEGRAM_BOT_TOKEN = "8900873009:AAExKQJp4PgpHVJHpPizSiH0JNhUQoQ9zIk"
TELEGRAM_CHAT_ID   = "8493770268"

SYMBOL       = "XAU/USD"
POLL_SECS    = 60
CANDLE_COUNT = 200

# Trading sessions in UTC hours (start, end)
SESSIONS = [
    (1,  4),   # Asian      — 06:30–09:30 IST
    (8,  12),  # London     — 13:30–17:30 IST
    (13, 17),  # New York   — 18:30–22:30 IST
]
# ───────────────────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("VOIDAURA")


# ── INDICATOR MATH ─────────────────────────────────────────────────────────────

def ema(values, period):
    k, prev = 2 / (period + 1), values[0]
    out = [prev]
    for v in values[1:]:
        prev = v * k + prev * (1 - k)
        out.append(prev)
    return out


def vwap_series(candles):
    cum_pv = cum_v = 0
    out = []
    for c in candles:
        typical = (c["h"] + c["l"] + c["c"]) / 3
        cum_pv += typical * c["v"]
        cum_v  += c["v"]
        out.append(cum_pv / cum_v)
    return out


def rsi_series(closes, period=14):
    out = [50.0] * len(closes)
    gains = losses = 0.0
    for i in range(1, len(closes)):
        diff = closes[i] - closes[i - 1]
        g, l = max(diff, 0), max(-diff, 0)
        if i <= period:
            gains += g; losses += l
            if i == period:
                rs = (gains / period) / ((losses / period) or 1e-9)
                out[i] = 100 - 100 / (1 + rs)
        else:
            gains  = (gains  * (period - 1) + g) / period
            losses = (losses * (period - 1) + l) / period
            rs = gains / (losses or 1e-9)
            out[i] = 100 - 100 / (1 + rs)
    return out


def stoch_rsi_series(closes, period=14):
    rsi = rsi_series(closes, period)
    out = [50.0] * len(closes)
    for i in range(period, len(closes)):
        window = rsi[i - period + 1 : i + 1]
        lo, hi = min(window), max(window)
        out[i] = 50 if (hi - lo) < 1e-9 else ((rsi[i] - lo) / (hi - lo)) * 100
    return out


def macd_series(closes):
    e12  = ema(closes, 12)
    e26  = ema(closes, 26)
    line = [e12[i] - e26[i] for i in range(len(closes))]
    sig  = ema(line, 9)
    hist = [line[i] - sig[i] for i in range(len(closes))]
    return hist


def evaluate(candles):
    closes = [c["c"] for c in candles]
    e9    = ema(closes, 9)
    e21   = ema(closes, 21)
    vwap  = vwap_series(candles)
    stoch = stoch_rsi_series(closes)
    hist  = macd_series(closes)

    i, p = len(closes) - 1, len(closes) - 2

    bull = {
        "ema"  : e9[i] > e21[i],
        "vwap" : closes[i] > vwap[i],
        "stoch": stoch[i] < 80 and stoch[i] > stoch[p],
        "macd" : hist[i] > 0 and hist[i] > hist[p],
    }
    bear = {
        "ema"  : e9[i] < e21[i],
        "vwap" : closes[i] < vwap[i],
        "stoch": stoch[i] > 20 and stoch[i] < stoch[p],
        "macd" : hist[i] < 0 and hist[i] < hist[p],
    }

    return {
        "price"     : closes[i],
        "bull_full" : sum(bull.values()) == 4,
        "bear_full" : sum(bear.values()) == 4,
        "bull_score": sum(bull.values()),
        "bear_score": sum(bear.values()),
        "bull_flags": bull,
        "bear_flags": bear,
    }


# ── DATA FETCH ─────────────────────────────────────────────────────────────────

def fetch_candles(interval):
    url = (
        f"https://api.twelvedata.com/time_series"
        f"?symbol={SYMBOL}&interval={interval}"
        f"&outputsize={CANDLE_COUNT}&apikey={TWELVEDATA_API_KEY}"
    )
    r = requests.get(url, timeout=15)
    r.raise_for_status()
    data = r.json()
    if data.get("status") == "error" or "values" not in data:
        raise ValueError(f"TwelveData error: {data.get('message', 'unknown')}")
    return [
        {
            "t": v["datetime"],
            "o": float(v["open"]),
            "h": float(v["high"]),
            "l": float(v["low"]),
            "c": float(v["close"]),
            "v": float(v.get("volume") or 1),
        }
        for v in reversed(data["values"])
    ]


# ── TELEGRAM ───────────────────────────────────────────────────────────────────

def send_telegram(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "Markdown"}
    requests.post(url, json=payload, timeout=10).raise_for_status()


def build_message(direction, ev5, ev15):
    arrow  = "🟢 LONG" if direction == "LONG" else "🔴 SHORT"
    flags5 = ev5["bull_flags"]  if direction == "LONG" else ev5["bear_flags"]
    flags15= ev15["bull_flags"] if direction == "LONG" else ev15["bear_flags"]
    t5  = lambda k: "✅" if flags5[k]  else "❌"
    t15 = lambda k: "✅" if flags15[k] else "❌"
    now = datetime.utcnow().strftime("%H:%M UTC")

    return (
        f"*VOIDAURA ⚡ {arrow} SIGNAL*\n"
        f"`XAUUSD  {ev5['price']:.2f}`\n"
        f"`Time    {now}`\n\n"
        f"*5-MIN  (4/4)*\n"
        f"  EMA 9/21  {t5('ema')}\n"
        f"  VWAP      {t5('vwap')}\n"
        f"  Stoch RSI {t5('stoch')}\n"
        f"  MACD      {t5('macd')}\n\n"
        f"*15-MIN (4/4)*\n"
        f"  EMA 9/21  {t15('ema')}\n"
        f"  VWAP      {t15('vwap')}\n"
        f"  Stoch RSI {t15('stoch')}\n"
        f"  MACD      {t15('macd')}\n\n"
        f"_Strict confluence — all 8 checks passed._"
    )


# ── SESSION CHECK ──────────────────────────────────────────────────────────────

def in_trading_session():
    hour = datetime.utcnow().hour
    return any(start <= hour < end for start, end in SESSIONS)


# ── MAIN LOOP ──────────────────────────────────────────────────────────────────

def main():
    log.info("VOIDAURA bot started")
    log.info("Sessions (UTC): Asian 01-04 | London 08-12 | New York 13-17")
    last_signal = None

    while True:
        if not in_trading_session():
            hour = datetime.utcnow().hour
            log.info("Outside session (UTC hour=%d) — sleeping 10 mins", hour)
            time.sleep(600)
            continue

        try:
            log.info("Fetching candles...")
            c5  = fetch_candles("5min")
            c15 = fetch_candles("15min")

            ev5  = evaluate(c5)
            ev15 = evaluate(c15)

            log.info(
                "5m bull=%d/4 bear=%d/4 | 15m bull=%d/4 bear=%d/4 | price=%.2f",
                ev5["bull_score"], ev5["bear_score"],
                ev15["bull_score"], ev15["bear_score"],
                ev5["price"],
            )

            direction = None
            if ev5["bull_full"] and ev15["bull_full"]:
                direction = "LONG"
            elif ev5["bear_full"] and ev15["bear_full"]:
                direction = "SHORT"

            if direction and direction != last_signal:
                log.info("SIGNAL: %s — sending Telegram", direction)
                send_telegram(build_message(direction, ev5, ev15))
                last_signal = direction
            elif not direction:
                last_signal = None
            else:
                log.info("Signal %s already active — no duplicate", direction)

        except Exception as e:
            log.error("Error: %s", e)

        time.sleep(POLL_SECS)


if __name__ == "__main__":
    main()
