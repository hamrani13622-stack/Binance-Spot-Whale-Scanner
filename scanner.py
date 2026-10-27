#!/usr/bin/env python3
"""OKX public spot market scanner. Read-only; no API keys and no order placement."""
import json
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

BASE = "https://www.okx.com"
UA = "Mozilla/5.0 OKX-Public-Scanner/1.0"
MAX_PAIRS = 35
PAUSE = 0.12


def api(path, params=None, timeout=20):
    query = urllib.parse.urlencode(params or {})
    url = BASE + path + (("?" + query) if query else "")
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except Exception as exc:
        raise RuntimeError(f"Request failed: {url} ({exc})") from exc
    if str(payload.get("code", "")) != "0":
        raise RuntimeError(f"OKX API error for {path}: code={payload.get('code')} msg={payload.get('msg')}")
    return payload.get("data", [])


def number(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def candles(inst_id, bar, limit=40):
    rows = api("/api/v5/market/candles", {"instId": inst_id, "bar": bar, "limit": str(limit)})
    # OKX returns newest first; each row: ts, open, high, low, close, volume, volumeCcy, volumeCcyQuote, confirm
    rows = list(reversed(rows))
    return [
        {
            "ts": int(r[0]), "open": number(r[1]), "high": number(r[2]),
            "low": number(r[3]), "close": number(r[4]), "vol": number(r[5]),
            "quote_vol": number(r[7]) if len(r) > 7 else number(r[6]),
            "confirm": str(r[8]) if len(r) > 8 else "1",
        }
        for r in rows
    ]


def pct_change(old, new):
    return ((new / old) - 1.0) * 100.0 if old > 0 else 0.0


def average(values):
    return sum(values) / len(values) if values else 0.0


def score_pair(inst_id, ticker, btc_15m, btc_1h):
    time.sleep(PAUSE)
    c15 = candles(inst_id, "15m", 35)
    time.sleep(PAUSE)
    c1h = candles(inst_id, "1H", 30)
    time.sleep(PAUSE)
    c4h = candles(inst_id, "4H", 20)
    if len(c15) < 22 or len(c1h) < 3 or len(c4h) < 3:
        return None

    # Ignore the still-forming candle where possible.
    closed = [c for c in c15 if c["confirm"] == "1"]
    if len(closed) < 21:
        closed = c15[:-1]
    if len(closed) < 21:
        return None

    last = closed[-1]
    prev = closed[-2]
    base_vols = [c["quote_vol"] for c in closed[-21:-1]]
    vol_avg = average(base_vols)
    vol_spike = last["quote_vol"] / vol_avg if vol_avg > 0 else 0.0

    change_15m = pct_change(prev["close"], last["close"])
    change_1h = pct_change(c1h[-2]["close"], c1h[-1]["close"])
    change_4h = pct_change(c4h[-2]["close"], c4h[-1]["close"])
    rs_15m = change_15m - btc_15m
    rs_1h = change_1h - btc_1h

    time.sleep(PAUSE)
    book_data = api("/api/v5/market/books", {"instId": inst_id, "sz": "20"})
    bid_value = ask_value = 0.0
    if book_data:
        for row in book_data[0].get("bids", []):
            bid_value += number(row[0]) * number(row[1])
        for row in book_data[0].get("asks", []):
            ask_value += number(row[0]) * number(row[1])
    book_imbalance = (bid_value / ask_value) if ask_value > 0 else 1.0

    # Recent public trades estimate aggressive buy share. It is a snapshot, not a full candle statistic.
    time.sleep(PAUSE)
    trades = api("/api/v5/market/trades", {"instId": inst_id, "limit": "100"})
    buy_value = sell_value = 0.0
    for trade in trades:
        value = number(trade.get("px")) * number(trade.get("sz"))
        if trade.get("side") == "buy":
            buy_value += value
        else:
            sell_value += value
    total_trade_value = buy_value + sell_value
    taker_buy_ratio = buy_value / total_trade_value if total_trade_value > 0 else 0.5

    score = 0
    if vol_spike >= 2.0:
        score += 25
    elif vol_spike >= 1.5:
        score += 17
    elif vol_spike >= 1.2:
        score += 9
    if taker_buy_ratio >= 0.62:
        score += 20
    elif taker_buy_ratio >= 0.55:
        score += 12
    elif taker_buy_ratio >= 0.50:
        score += 5
    if change_1h > 0 and change_4h > 0:
        score += 15
    elif change_1h > 0 or change_4h > 0:
        score += 8
    if rs_15m > 0.5:
        score += 10
    elif rs_15m > 0:
        score += 5
    if rs_1h > 1.0:
        score += 10
    elif rs_1h > 0:
        score += 5
    if book_imbalance >= 1.5:
        score += 15
    elif book_imbalance >= 1.15:
        score += 8
    elif book_imbalance >= 1.0:
        score += 3

    return {
        "symbol": inst_id,
        "score": score,
        "last_price": number(ticker.get("last")),
        "quote_volume_24h": number(ticker.get("_quote_volume_24h_est")) or
                            number(ticker.get("volCcy24h")) or
                            (number(ticker.get("vol24h")) * number(ticker.get("last"))),
        "volume_spike_vs_20_closed_15m": round(vol_spike, 2),
        "recent_trade_taker_buy_ratio": round(taker_buy_ratio, 3),
        "orderbook_bid_ask_value_ratio": round(book_imbalance, 3),
        "change_15m_pct": round(change_15m, 3),
        "change_1h_pct": round(change_1h, 3),
        "change_4h_pct": round(change_4h, 3),
        "relative_strength_vs_btc_15m_pct": round(rs_15m, 3),
        "relative_strength_vs_btc_1h_pct": round(rs_1h, 3),
        "note": "Heuristic candidate only; not proof of whale accumulation or a buy signal."
    }


def main():
    print("OKX Spot Opportunity Scanner — public data only")
    print("No API keys. No orders. No trading actions.")
    print("UTC:", datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"))
    instruments = api("/api/v5/public/instruments", {"instType": "SPOT"})
    active = {
        x["instId"] for x in instruments
        if x.get("state") == "live" and x.get("quoteCcy") == "USDT"
    }
    tickers = api("/api/v5/market/tickers", {"instType": "SPOT"})
    candidates = []
    for t in tickers:
        inst_id = t.get("instId", "")
        if inst_id in active:
            # OKX ticker uses volCcy24h for the 24h currency-denominated volume.
            # For spot pairs, use that first; fall back to base volume * last price.
            last_price = number(t.get("last"))
            qvol = number(t.get("volCcy24h"))
            if qvol <= 0:
                qvol = number(t.get("vol24h")) * last_price
            if qvol > 0:
                t["_quote_volume_24h_est"] = qvol
                candidates.append((qvol, inst_id, t))
    candidates.sort(reverse=True, key=lambda x: x[0])
    selected = candidates[:MAX_PAIRS]
    print(f"Active USDT spot instruments: {len(active)}")
    print(f"Scanning {len(selected)} highest 24h quote-volume pairs...")

    btc_15m = btc_1h = 0.0
    try:
        time.sleep(PAUSE)
        btc15 = candles("BTC-USDT", "15m", 3)
        time.sleep(PAUSE)
        btc1h = candles("BTC-USDT", "1H", 3)
        if len(btc15) >= 2:
            btc_15m = pct_change(btc15[-2]["close"], btc15[-1]["close"])
        if len(btc1h) >= 2:
            btc_1h = pct_change(btc1h[-2]["close"], btc1h[-1]["close"])
    except Exception as exc:
        print("Warning: BTC benchmark unavailable:", exc)

    results = []
    errors = []
    for index, (_, inst_id, ticker) in enumerate(selected, start=1):
        try:
            result = score_pair(inst_id, ticker, btc_15m, btc_1h)
            if result:
                results.append(result)
            print(f"[{index}/{len(selected)}] {inst_id}: " +
                  (f"score={result['score']}" if result else "insufficient candles"))
        except Exception as exc:
            errors.append({"symbol": inst_id, "error": str(exc)})
            print(f"[{index}/{len(selected)}] {inst_id}: ERROR {exc}")

    results.sort(key=lambda x: (x["score"], x["quote_volume_24h"]), reverse=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    outdir = Path("reports")
    outdir.mkdir(parents=True, exist_ok=True)
    report = {
        "exchange": "OKX",
        "market": "spot USDT",
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "scanned_pairs": len(selected),
        "successful_pairs": len(results),
        "failed_pairs": errors,
        "btc_change_15m_pct": round(btc_15m, 3),
        "btc_change_1h_pct": round(btc_1h, 3),
        "results": results,
        "disclaimer": "Scores are heuristic screening metrics, not investment advice and not proof of whale activity."
    }
    json_path = outdir / f"okx_scan_{stamp}.json"
    md_path = outdir / f"okx_scan_{stamp}.md"
    json_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")

    lines = [
        "# OKX Spot Opportunity Scan",
        "",
        f"- Generated (UTC): {report['generated_utc']}",
        f"- Scanned pairs: {len(selected)}",
        f"- Successful pairs: {len(results)}",
        f"- BTC change 15m / 1h: {btc_15m:.3f}% / {btc_1h:.3f}%",
        "",
        "> Screening only. Scores do not prove whale accumulation and are not buy/sell recommendations.",
        "",
        "| Rank | Pair | Score | Vol spike | Recent buy ratio | Bid/ask value | 1h % | 4h % | RS vs BTC 1h |",
        "|---:|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for rank, item in enumerate(results, start=1):
        lines.append(
            f"| {rank} | {item['symbol']} | {item['score']} | "
            f"{item['volume_spike_vs_20_closed_15m']}x | "
            f"{item['recent_trade_taker_buy_ratio']:.3f} | "
            f"{item['orderbook_bid_ask_value_ratio']:.3f} | "
            f"{item['change_1h_pct']:.3f} | {item['change_4h_pct']:.3f} | "
            f"{item['relative_strength_vs_btc_1h_pct']:.3f} |"
        )
    if errors:
        lines += ["", "## Pair errors", ""]
        lines += [f"- {e['symbol']}: {e['error']}" for e in errors]
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"Reports written: {json_path} and {md_path}")
    if not results:
        print("WARNING: No pairs scanned successfully. See per-pair errors above; report files were still saved.")


if __name__ == "__main__":
    main()
