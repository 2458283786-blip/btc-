
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


def add_ema(df, mapping):
    out = df.copy()
    for name, span in mapping.items():
        out[name] = out["close"].ewm(span=span, adjust=False).mean()
    return out


def add_adx_di(df, period=14):
    out = df.copy()
    high, low, close = out["high"], out["low"], out["close"]
    up_move = high.diff()
    down_move = -low.diff()
    plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
    minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)
    tr = pd.concat([high - low, (high - close.shift()).abs(), (low - close.shift()).abs()], axis=1).max(axis=1)
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
    mid = out["close"].rolling(s["boll_window"]).mean()
    std = out["close"].rolling(s["boll_window"]).std()
    out["boll_upper"] = mid + s["boll_std"] * std
    out["boll_lower"] = mid - s["boll_std"] * std
    out["boll_width"] = (out["boll_upper"] - out["boll_lower"]) / mid
    out["boll_width_change_24h"] = out["boll_width"] / out["boll_width"].shift(24) - 1
    out["vol_ma"] = out["volume"].rolling(s["volume_window"]).mean()
    out["vol_ratio"] = out["volume"] / out["vol_ma"]
    return out


def score_symbol(symbol_cfg, cfg):
    s = cfg["strategy"]
    inst = symbol_cfg["okx_inst_id"]
    name = symbol_cfg["name"]

    d1 = fetch_okx_candles(inst, "1D", 300)
    h4 = fetch_okx_candles(inst, "4H", 300)
    h1 = fetch_okx_candles(inst, "1H", 300)

    d1 = add_ema(d1, {"d_ema55": s["daily_ema_fast"], "d_ema144": s["daily_ema_slow"]})
    h4 = add_adx_di(h4, s["adx_period"])
    h1 = add_ema(h1, {"ema21": s["h1_ema_fast"], "ema55": s["h1_ema_mid"], "ema144": s["h1_ema_slow"]})
    h1 = add_adx_di(h1, s["adx_period"])
    h1 = add_boll_volume(h1, cfg)

    d = d1.iloc[-1]
    f = h4.iloc[-1]
    r = h1.iloc[-1]

    high_20d = float(d1["high"].shift(1).tail(s["breakout_days"]).max())
    low_20d = float(d1["low"].shift(1).tail(s["breakout_days"]).min())

    candidates = []
    for direction in ["LONG", "SHORT"]:
        score = 0
        details = []

        if direction == "LONG":
            side_text = "做多观察"
            daily_ok = d["d_ema55"] > d["d_ema144"]
            ema_full = r["ema21"] > r["ema55"] > r["ema144"]
            di_gap = r["plus_di"] - r["minus_di"]
            di_ok = di_gap > s["di_gap_threshold"]
            breakout = r["close"] > high_20d
            pullback_zone = r["ema21"] * (1 - s["pullback_distance_pct"])
            invalid_price = r["ema55"]
            target1 = r["close"] * 1.05
            target2 = r["close"] * 1.10
        else:
            side_text = "做空观察"
            daily_ok = d["d_ema55"] < d["d_ema144"]
            ema_full = r["ema21"] < r["ema55"] < r["ema144"]
            di_gap = r["minus_di"] - r["plus_di"]
            di_ok = di_gap > s["di_gap_threshold"]
            breakout = r["close"] < low_20d
            pullback_zone = r["ema21"] * (1 + s["pullback_distance_pct"])
            invalid_price = r["ema55"]
            target1 = r["close"] * 0.95
            target2 = r["close"] * 0.90

        adx_ok = f["adx"] > s["adx_threshold"]
        boll_expand = (not pd.isna(r["boll_width_change_24h"])) and r["boll_width_change_24h"] > 0
        vol_ok = (not pd.isna(r["vol_ratio"])) and r["vol_ratio"] > s["volume_ratio_threshold"]

        if ema_full:
            score += 30; details.append("1H EMA21/55/144完整排列 +30")
        if adx_ok:
            score += 25; details.append(f"4H ADX>{s['adx_threshold']} +25")
        if di_ok:
            score += 20; details.append(f"DI差值>{s['di_gap_threshold']} +20")
        if daily_ok:
            score += 10; details.append("日线EMA55/144同向 +10")
        if boll_expand:
            score += 5; details.append("BOLL扩张 +5")
        if vol_ok:
            score += 5; details.append(f"成交量>{s['volume_ratio_threshold']}倍 +5")
        if breakout:
            score += 5; details.append("突破20日高/低点 +5")

        candidates.append({
            "name": name, "direction": direction, "side_text": side_text,
            "score": score, "details": details, "close": float(r["close"]),
            "h4_adx": float(f["adx"]), "plus_di": float(r["plus_di"]),
            "minus_di": float(r["minus_di"]), "di_gap": float(di_gap),
            "ema21": float(r["ema21"]), "ema55": float(r["ema55"]),
            "ema144": float(r["ema144"]),
            "vol_ratio": None if pd.isna(r["vol_ratio"]) else float(r["vol_ratio"]),
            "boll_width_change_24h": None if pd.isna(r["boll_width_change_24h"]) else float(r["boll_width_change_24h"]),
            "breakout": bool(breakout), "pullback_zone": float(pullback_zone),
            "invalid_price": float(invalid_price), "target1": float(target1),
            "target2": float(target2), "timestamp": str(r["timestamp"]),
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
    breakout_text = "是" if result["breakout"] else "否"
    detail_text = "\n".join("- " + d for d in result["details"]) if result["details"] else "- 无"

    return f"""
{level(result['score'], cfg)} | {result['name']} | {result['side_text']}

分数：{result['score']}/100
价格：{result['close']:.4f}

核心指标：
- 4H ADX：{result['h4_adx']:.2f}
- 1H +DI：{result['plus_di']:.2f}
- 1H -DI：{result['minus_di']:.2f}
- DI差值：{result['di_gap']:.2f}
- 1H EMA21：{result['ema21']:.4f}
- 1H EMA55：{result['ema55']:.4f}
- 1H EMA144：{result['ema144']:.4f}
- 成交量倍率：{vol_text}
- BOLL宽度24H变化：{bw_text}
- 20日突破：{breakout_text}

得分明细：
{detail_text}

交易计划：
- 方向：{result['side_text']}
- 建议：等回踩/反抽 EMA21 后再考虑
- 参考回踩区：{result['pullback_zone']:.4f}
- 失效参考：{result['invalid_price']:.4f}
- 第一目标：{result['target1']:.4f}
- 第二目标：{result['target2']:.4f}
- 建议止损：0.8%-1.2%
- 目标行情：5%-10%单边波段

时间：{result['timestamp']}
""".strip()


def send_serverchan(text, title="趋势预警"):
    sendkey = os.getenv("SERVERCHAN_SENDKEY", "").strip()
    if not sendkey:
        print("[SERVERCHAN_SENDKEY 未配置]")
        print(text)
        return
    url = f"https://sctapi.ftqq.com/{sendkey}.send"
    r = requests.post(url, data={"title": title, "desp": text}, timeout=20)
    print("Server酱发送成功")
    print(r.text)


def main():
    cfg = load_config()
    print(f"Checking trend hunter V2 at {datetime.now(timezone.utc).isoformat()}")

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
        send_serverchan(msg, f"{r['name']} {level(r['score'], cfg)}")


if __name__ == "__main__":
    main()
