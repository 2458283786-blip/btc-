
import json
import os
from datetime import datetime, timezone

import requests
import numpy as np
import pandas as pd


OKX_CANDLES_URL = "https://www.okx.com/api/v5/market/candles"


def load_config():
    with open("config.json", "r", encoding="utf-8") as f:
        return json.load(f)


def fetch_okx_candles(inst_id, bar, limit=300):
    params = {"instId": inst_id, "bar": bar, "limit": str(limit)}
    r = requests.get(OKX_CANDLES_URL, params=params, timeout=20)
    r.raise_for_status()
    js = r.json()
    if js.get("code") != "0":
        raise RuntimeError(f"OKX API error: {js}")
    data = js.get("data", [])
    if not data:
        raise RuntimeError(f"No candle data for {inst_id} {bar}")

    rows = []
    for x in data:
        rows.append({
            "timestamp": pd.to_datetime(int(x[0]), unit="ms", utc=True),
            "open": float(x[1]),
            "high": float(x[2]),
            "low": float(x[3]),
            "close": float(x[4]),
            "volume": float(x[5]),
        })

    return pd.DataFrame(rows).sort_values("timestamp").reset_index(drop=True)


def add_ema(df, spans):
    out = df.copy()
    for name, span in spans.items():
        out[name] = out["close"].ewm(span=span, adjust=False).mean()
    return out


def add_adx_di(df, period=14):
    out = df.copy()
    high, low, close = out["high"], out["low"], out["close"]

    up_move = high.diff()
    down_move = -low.diff()

    plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
    minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)

    tr = pd.concat([
        high - low,
        (high - close.shift()).abs(),
        (low - close.shift()).abs()
    ], axis=1).max(axis=1)

    atr = tr.ewm(alpha=1 / period, adjust=False).mean()
    plus_di = 100 * pd.Series(plus_dm, index=out.index).ewm(alpha=1 / period, adjust=False).mean() / atr
    minus_di = 100 * pd.Series(minus_dm, index=out.index).ewm(alpha=1 / period, adjust=False).mean() / atr
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di)

    out["plus_di"] = plus_di
    out["minus_di"] = minus_di
    out["adx"] = dx.ewm(alpha=1 / period, adjust=False).mean()
    return out


def add_boll_volume(df, cfg):
    out = df.copy()
    s = cfg["strategy"]

    w = s["boll_window"]
    mid = out["close"].rolling(w).mean()
    std = out["close"].rolling(w).std()
    out["boll_mid"] = mid
    out["boll_upper"] = mid + s["boll_std"] * std
    out["boll_lower"] = mid - s["boll_std"] * std
    out["boll_width"] = (out["boll_upper"] - out["boll_lower"]) / out["boll_mid"]
    out["boll_width_change_24h"] = out["boll_width"] / out["boll_width"].shift(24) - 1

    vw = s["volume_window"]
    out["vol_ma"] = out["volume"].rolling(vw).mean()
    out["vol_ratio"] = out["volume"] / out["vol_ma"]
    return out


def score_direction(direction, d, h4, h1, cfg):
    s = cfg["strategy"]
    score = 0
    details = []

    if direction == "LONG":
        daily_ok = d["ema55"] > d["ema144"]
        ema_ok = h1["ema21"] > h1["ema55"]
        di_ok = h1["plus_di"] > h1["minus_di"]
        side_text = "做多观察"
    else:
        daily_ok = d["ema55"] < d["ema144"]
        ema_ok = h1["ema21"] < h1["ema55"]
        di_ok = h1["minus_di"] > h1["plus_di"]
        side_text = "做空观察"

    if di_ok:
        score += 30
        details.append("DI同向 +30")
    if ema_ok:
        score += 25
        details.append("1H EMA21/55同向 +25")
    if daily_ok:
        score += 20
        details.append("日线 EMA55/144同向 +20")
    if h4["adx"] > s["adx_threshold"]:
        score += 15
        details.append(f"4H ADX>{s['adx_threshold']} +15")
    if not pd.isna(h1["boll_width_change_24h"]) and h1["boll_width_change_24h"] > 0:
        score += 5
        details.append("BOLL扩张 +5")
    if not pd.isna(h1["vol_ratio"]) and h1["vol_ratio"] > s["volume_ratio_threshold"]:
        score += 5
        details.append(f"成交量>{s['volume_ratio_threshold']}倍 +5")

    return score, details, side_text


def score_symbol(symbol_cfg, cfg):
    s = cfg["strategy"]
    inst = symbol_cfg["okx_inst_id"]
    name = symbol_cfg["name"]

    d1 = fetch_okx_candles(inst, "1D", 300)
    h4 = fetch_okx_candles(inst, "4H", 300)
    h1 = fetch_okx_candles(inst, "1H", 300)

    d1 = add_ema(d1, {"ema55": s["daily_ema_fast"], "ema144": s["daily_ema_slow"]})
    h4 = add_adx_di(h4, s["adx_period"])
    h1 = add_ema(h1, {"ema21": s["h1_ema_fast"], "ema55": s["h1_ema_slow"]})
    h1 = add_adx_di(h1, s["adx_period"])
    h1 = add_boll_volume(h1, cfg)

    d = d1.iloc[-1]
    f = h4.iloc[-1]
    r = h1.iloc[-1]

    candidates = []
    for direction in ["LONG", "SHORT"]:
        score, details, side_text = score_direction(direction, d, f, r, cfg)
        candidates.append({
            "name": name,
            "direction": direction,
            "side_text": side_text,
            "score": score,
            "details": details,
            "close": float(r["close"]),
            "h4_adx": float(f["adx"]),
            "plus_di": float(r["plus_di"]),
            "minus_di": float(r["minus_di"]),
            "ema21": float(r["ema21"]),
            "ema55": float(r["ema55"]),
            "vol_ratio": None if pd.isna(r["vol_ratio"]) else float(r["vol_ratio"]),
            "boll_width_change_24h": None if pd.isna(r["boll_width_change_24h"]) else float(r["boll_width_change_24h"]),
            "timestamp": str(r["timestamp"]),
        })

    return max(candidates, key=lambda x: x["score"])


def level(score, cfg):
    th = cfg["score_thresholds"]
    if score >= th["extreme"]:
        return "🔥 极强预警"
    if score >= th["strong"]:
        return "🚨 强预警"
    if score >= th["watch"]:
        return "👀 观察预警"
    return "无信号"


def format_message(result, cfg):
    vol_text = "NA" if result["vol_ratio"] is None else f"{result['vol_ratio']:.2f}x"
    bw_text = "NA" if result["boll_width_change_24h"] is None else f"{result['boll_width_change_24h']*100:.2f}%"

    return f"""
{level(result['score'], cfg)} | {result['name']} | {result['side_text']}

分数：{result['score']}/100
价格：{result['close']:.4f}

核心指标：
- 4H ADX：{result['h4_adx']:.2f}
- 1H +DI：{result['plus_di']:.2f}
- 1H -DI：{result['minus_di']:.2f}
- 1H EMA21：{result['ema21']:.4f}
- 1H EMA55：{result['ema55']:.4f}
- 成交量倍率：{vol_text}
- BOLL宽度24H变化：{bw_text}

得分明细：
{chr(10).join('- ' + d for d in result['details'])}

交易提醒：
- 80分：观察
- 90分：等回踩/反抽 EMA21
- 100分：重点盯盘，必须止损
- 参考止损：0.8%
- 目标：5%-10%单边波段

时间：{result['timestamp']}
""".strip()


def send_telegram(text):
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()

    if not token or not chat_id:
        print("[Telegram Secrets 未配置]")
        print(text)
        return

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    r = requests.post(url, data={"chat_id": chat_id, "text": text}, timeout=20)
    if r.status_code != 200:
        raise RuntimeError(f"Telegram send failed: {r.text}")


def main():
    cfg = load_config()
    print(f"Checking trend alerts at {datetime.now(timezone.utc).isoformat()}")

    results = []
    for sym in cfg["symbols"]:
        try:
            res = score_symbol(sym, cfg)
            results.append(res)
            print(f"{res['name']} {res['direction']} score={res['score']}")
        except Exception as e:
            print(f"Failed {sym['name']}: {e}")

    alerts = [r for r in results if r["score"] >= cfg["score_thresholds"]["watch"]]

    if not alerts:
        print("No alerts above threshold.")
        print("\n".join([f"{r['name']} {r['direction']} {r['score']}/100" for r in results]))
        return

    for r in sorted(alerts, key=lambda x: x["score"], reverse=True):
        msg = format_message(r, cfg)
        print(msg)
        send_telegram(msg)


if __name__ == "__main__":
    main()
