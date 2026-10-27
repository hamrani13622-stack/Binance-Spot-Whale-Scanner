#!/usr/bin/env python3
"""Binance USDT Spot Opportunity Scanner.
Public market data only: no API keys and no trading orders.
"""
import json
import os
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone

SPOT = "https://api.binance.com"
FUTURES = "https://fapi.binance.com"
MIN_QUOTE_VOLUME = 1_000_000
MAX_SYMBOLS = 80
FUTURES_CONFIRMATIONS = 20
ALERT_SCORE = 65
OUT = "reports"

def api(base, path, params=None, timeout=15):
    url = base + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": "BinanceSpotOpportunityScanner/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))

def num(x, default=0.0):
    try:
        return float(x)
    except (TypeError, ValueError):
        return default

def change(new, old):
    return (new / old - 1) * 100 if old else 0.0

def spot_symbols():
    data = api(SPOT, "/api/v3/exchangeInfo")
    return {
        s["symbol"] for s in data["symbols"]
        if s.get("status") == "TRADING"
        and s.get("quoteAsset") == "USDT"
        and s.get("isSpotTradingAllowed", True)
    }

def analyze(ticker, btc_change):
    symbol = ticker["symbol"]
    kl = api(SPOT, "/api/v3/klines", {"symbol": symbol, "interval": "15m", "limit": 22})
    if len(kl) < 21:
        return None
    # Ignore the currently forming candle.
    closed = kl[:-1]
    last, prev = closed[-1], closed[-2]
    close_now, close_prev = num(last[4]), num(prev[4])
    baseline = [num(k[7]) for k in closed[-21:-1]]
    avg_qvol = sum(baseline) / len(baseline) if baseline else 0
    qvol = num(last[7])
    spike = qvol / avg_qvol if avg_qvol else 0
    buy_ratio = num(last[10]) / qvol if qvol else 0.5
    move1h = change(close_now, num(closed[-5][4]))
    move4h = change(close_now, num(closed[-17][4]))
    high4h = max(num(k[2]) for k in closed[-16:])
    ticker_change = num(ticker.get("priceChangePercent"))
    quote24 = num(ticker.get("quoteVolume"))
    rel = ticker_change - btc_change

    score, reasons, risks = 0, [], []
    if spike >= 2.5:
        score += 20; reasons.append(f"حجم ۱۵دقیقه‌ای {spike:.1f} برابر میانگین")
    elif spike >= 1.7:
        score += 14; reasons.append(f"افزایش حجم {spike:.1f} برابری")
    elif spike >= 1.3:
        score += 7; reasons.append(f"حجم بالاتر از میانگین ({spike:.1f}x)")
    else:
        risks.append("جهش حجم تأیید نشده")

    if buy_ratio >= 0.62:
        score += 20; reasons.append(f"غلبه خرید تهاجمی ({buy_ratio:.0%})")
    elif buy_ratio >= 0.55:
        score += 12; reasons.append(f"کفه خرید کمی سنگین‌تر ({buy_ratio:.0%})")
    elif buy_ratio < 0.45:
        risks.append(f"غلبه فروش تهاجمی ({buy_ratio:.0%})")

    if move1h > 0 and move4h > 0:
        score += 10; reasons.append("روند ۱ساعته و ۴ساعته مثبت")
    elif move1h < -1.5 and move4h < 0:
        risks.append("روند کوتاه‌مدت نزولی")

    if rel >= 3:
        score += 10; reasons.append(f"قوی‌تر از BTC به میزان {rel:.1f}%")
    elif rel >= 1:
        score += 6; reasons.append(f"قدرت نسبی مثبت نسبت به BTC ({rel:.1f}%)")
    elif rel < -3:
        risks.append("ضعیف‌تر از BTC")

    if close_now >= high4h * 0.985 and move1h > 0:
        score += 10; reasons.append("نزدیک سقف ۴ساعته")
    elif high4h and change(high4h, close_now) > 5:
        risks.append(f"{change(high4h, close_now):.1f}% پایین‌تر از سقف ۴ساعته")

    if quote24 >= 20_000_000:
        score += 10; reasons.append("نقدشوندگی ۲۴ساعته بالا")
    elif quote24 >= 5_000_000:
        score += 7; reasons.append("نقدشوندگی مناسب")
    else:
        risks.append("نقدشوندگی نسبتاً پایین")

    book_share = None
    try:
        book = api(SPOT, "/api/v3/depth", {"symbol": symbol, "limit": 20})
        bids = sum(num(x[0]) * num(x[1]) for x in book.get("bids", []))
        asks = sum(num(x[0]) * num(x[1]) for x in book.get("asks", []))
        total = bids + asks
        book_share = bids / total if total else 0.5
        if book_share >= 0.60:
            score += 10; reasons.append("دفتر سفارشات موقتاً به نفع خرید")
        elif book_share <= 0.40:
            risks.append("دفتر سفارشات به نفع فروش")
    except Exception:
        risks.append("داده دفتر سفارشات در دسترس نبود")

    return {
        "symbol": symbol, "score": min(score, 100),
        "status": "آماده بررسی" if score >= ALERT_SCORE else "رصد",
        "price": close_now, "change24h_pct": round(ticker_change, 3),
        "volume24h_usdt": round(quote24, 2),
        "volume_spike_15m": round(spike, 2),
        "taker_buy_ratio_15m": round(buy_ratio, 3),
        "change1h_pct": round(move1h, 3), "change4h_pct": round(move4h, 3),
        "relative_strength_vs_btc_pct": round(rel, 2),
        "book_bid_share": round(book_share, 3) if book_share is not None else None,
        "reasons": reasons, "risks": risks, "futures": {}
    }

def futures_context(item):
    s = item["symbol"]
    try:
        x = api(FUTURES, "/fapi/v1/fundingRate", {"symbol": s, "limit": 1})
        if x: item["futures"]["last_funding_rate"] = num(x[-1].get("fundingRate"))
    except Exception: pass
    try:
        x = api(FUTURES, "/futures/data/topLongShortPositionRatio",
                {"symbol": s, "period": "1h", "limit": 1})
        if x: item["futures"]["top_trader_long_short_ratio"] = num(x[-1].get("longShortRatio"))
    except Exception: pass

def main():
    os.makedirs(OUT, exist_ok=True)
    now = datetime.now(timezone.utc)
    print("Binance Spot Opportunity Scanner — public data only")
    print("UTC:", now.strftime("%Y-%m-%d %H:%M:%S"))
    tickers = api(SPOT, "/api/v3/ticker/24hr")
    btc = next((t for t in tickers if t.get("symbol") == "BTCUSDT"), {})
    btc_change = num(btc.get("priceChangePercent"))
    valid = spot_symbols()
    liquid = [t for t in tickers if t.get("symbol") in valid
              and num(t.get("quoteVolume")) >= MIN_QUOTE_VOLUME
              and not any(x in t.get("symbol", "") for x in ("UPUSDT", "DOWNUSDT", "BULLUSDT", "BEARUSDT"))]
    liquid.sort(key=lambda t: num(t.get("quoteVolume")), reverse=True)
    universe = liquid[:MAX_SYMBOLS]
    print(f"Symbols to scan: {len(universe)}")
    results = []
    for i, t in enumerate(universe, 1):
        try:
            item = analyze(t, btc_change)
            if item: results.append(item)
            print(f"[{i}/{len(universe)}] {t['symbol']}")
        except Exception as e:
            print(f"Skipped {t.get('symbol')}: {e}")
        time.sleep(0.06)
    results.sort(key=lambda x: x["score"], reverse=True)
    for item in results[:FUTURES_CONFIRMATIONS]:
        futures_context(item)
        time.sleep(0.06)

    stamp = now.strftime("%Y%m%d_%H%M%S")
    json_path = os.path.join(OUT, f"scan_{stamp}.json")
    md_path = os.path.join(OUT, f"scan_{stamp}.md")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump({"timestamp_utc": now.isoformat(), "results": results}, f, ensure_ascii=False, indent=2)
    with open(md_path, "w", encoding="utf-8") as f:
        f.write("# Binance Spot Scanner Report\n\n")
        f.write(f"- Time (UTC): {now.isoformat()}\n- Universe: top {len(universe)} liquid USDT spot pairs\n")
        f.write("- Public data only; no orders submitted.\n\n")
        f.write("| Symbol | Score | Status | 24h % | 15m volume x | Taker buy | Reasons | Risks |\n")
        f.write("|---|---:|---|---:|---:|---:|---|---|\n")
        for x in results[:30]:
            f.write(f"| {x['symbol']} | {x['score']}/100 | {x['status']} | {x['change24h_pct']:+.2f}% | "
                    f"{x['volume_spike_15m']:.2f}x | {x['taker_buy_ratio_15m']:.0%} | "
                    f"{'؛ '.join(x['reasons']) or '—'} | {'؛ '.join(x['risks']) or '—'} |\n")
    print("\nTOP RESULTS")
    for x in results[:15]:
        print(f"{x['symbol']:14} score={x['score']:3}/100 | {x['status']} | "
              f"24h={x['change24h_pct']:+.2f}% | volSpike={x['volume_spike_15m']}x | "
              f"takerBuy={x['taker_buy_ratio_15m']:.0%}")
        if x["reasons"]: print("  + " + "؛ ".join(x["reasons"]))
        if x["risks"]: print("  - " + "؛ ".join(x["risks"]))
    print(f"\nReports saved: {json_path} and {md_path}")
    print("Score is experimental, not proof of whale buying or a guarantee of profit.")

if __name__ == "__main__":
    main()
