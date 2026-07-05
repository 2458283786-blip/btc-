
import json
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
import numpy as np
import pandas as pd


OKX_CANDLES_URL = "https://www.okx.com/api/v5/market/candles"
ALERT_STATE_VERSION = 1
DEFAULT_ALERT_STATE_FILE = "alert_state.json"
DEFAULT_DEDUPE_COOLDOWN_HOURS = 12
ALERT_BAND_RANKS = {"none": 0, "watch": 1, "strong": 2, "extreme": 3}
SIGNAL_TIER_RANKS = {"none": 0, "small_trend": 1, "major_trend": 2}
ALERT_SIGNAL_TIERS = {"small_trend", "major_trend"}
DEFAULT_SIGNAL_FILTERS = {
    "small_score": 85,
    "major_score": 90,
    "small_max_extension_pct": 0.03,
    "major_max_extension_pct": 0.04,
    "ema_gap_pct": 0.0015,
    "breakout_buffer_pct": 0.002,
}


def load_config():
    with open("config.json", "r", encoding="utf-8") as f:
        return json.load(f)


def signal_filters(cfg):
    filters = dict(DEFAULT_SIGNAL_FILTERS)
    filters.update(cfg.get("signal_filters", {}))
    return filters


def has_structure_confirmation(result):
    return bool(
        result.get("break_24h")
        or result.get("breakout_buffer")
        or (result.get("vol_ok") and result.get("boll_expand"))
    )


def classify_signal(result, cfg):
    filters = signal_filters(cfg)
    score = result.get("score", 0)
    confirmation = has_structure_confirmation(result)

    major_ok = (
        score >= filters["major_score"]
        and result.get("daily_ok")
        and result.get("h4_ema_stack")
        and result.get("h4_slope_ok")
        and result.get("h4_adx_rising")
        and result.get("not_major_extended")
        and confirmation
    )
    if major_ok:
        return "major_trend"

    small_ok = (
        score >= filters["small_score"]
        and result.get("h4_ema_dir")
        and result.get("h4_adx_rising")
        and result.get("not_extended")
        and confirmation
    )
    if small_ok:
        return "small_trend"

    return "none"


def signal_tier_label(tier):
    return {
        "major_trend": "强趋势",
        "small_trend": "趋势观察",
        "none": "未通过趋势过滤",
    }.get(tier or "none", "未通过趋势过滤")


def signal_reason_lists(result, cfg):
    filters = signal_filters(cfg)
    confirmations = []
    if result.get("break_24h"):
        confirmations.append("突破近24H结构")
    if result.get("breakout_buffer"):
        confirmations.append("有效突破20日高低点")
    if result.get("vol_ok") and result.get("boll_expand"):
        confirmations.append("放量且BOLL扩张")

    passed = []
    if result.get("daily_ok"):
        passed.append("日线方向同向")
    if result.get("h4_ema_stack"):
        passed.append("4H EMA21/55/144完整排列")
    elif result.get("h4_ema_dir"):
        passed.append("4H EMA21/55同向")
    if result.get("h4_slope_ok"):
        passed.append("4H EMA21斜率同向")
    if result.get("h4_adx_rising"):
        passed.append("4H ADX走强")
    if result.get("not_major_extended"):
        passed.append(f"未远离1H EMA21超过{filters['major_max_extension_pct'] * 100:.1f}%")
    elif result.get("not_extended"):
        passed.append(f"未远离1H EMA21超过{filters['small_max_extension_pct'] * 100:.1f}%")
    passed.extend(confirmations)

    rejected = []
    if result.get("score", 0) < filters["small_score"]:
        rejected.append(f"分数低于{filters['small_score']}")
    if not result.get("h4_ema_dir"):
        rejected.append("4H EMA21/55未同向")
    if not result.get("h4_adx_rising"):
        rejected.append("4H ADX没有走强")
    if not result.get("not_extended"):
        rejected.append("价格离1H EMA21过远")
    if not confirmations:
        rejected.append("缺少结构/量能确认")
    if result.get("score", 0) >= filters["major_score"] and result.get("signal_tier") != "major_trend":
        if not result.get("daily_ok"):
            rejected.append("日线方向未同向")
        if not result.get("h4_ema_stack"):
            rejected.append("4H EMA完整排列不足")
        if not result.get("h4_slope_ok"):
            rejected.append("4H EMA21斜率不足")

    return passed, rejected


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
        if len(x) > 8 and str(x[8]) != "1":
            continue
        rows.append({
            "timestamp": pd.to_datetime(int(x[0]), unit="ms", utc=True),
            "open": float(x[1]),
            "high": float(x[2]),
            "low": float(x[3]),
            "close": float(x[4]),
            "volume": float(x[5]),
        })
    if not rows:
        raise RuntimeError(f"No confirmed candle data for {inst_id} {bar}")
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
    filters = signal_filters(cfg)
    inst = symbol_cfg["okx_inst_id"]
    name = symbol_cfg["name"]

    d1 = fetch_okx_candles(inst, "1D", 300)
    h4 = fetch_okx_candles(inst, "4H", 300)
    h1 = fetch_okx_candles(inst, "1H", 300)

    d1 = add_ema(d1, {"d_ema55": s["daily_ema_fast"], "d_ema144": s["daily_ema_slow"]})
    h4 = add_adx_di(h4, s["adx_period"])
    h4 = add_ema(h4, {
        "h4_ema21": s["h1_ema_fast"],
        "h4_ema55": s["h1_ema_mid"],
        "h4_ema144": s["h1_ema_slow"],
    })
    h4["h4_ema21_slope3"] = h4["h4_ema21"] / h4["h4_ema21"].shift(3) - 1
    h4["h4_adx_delta3"] = h4["adx"] - h4["adx"].shift(3)
    h1 = add_ema(h1, {"ema21": s["h1_ema_fast"], "ema55": s["h1_ema_mid"], "ema144": s["h1_ema_slow"]})
    h1 = add_adx_di(h1, s["adx_period"])
    h1 = add_boll_volume(h1, cfg)

    d = d1.iloc[-1]
    f = h4.iloc[-1]
    r = h1.iloc[-1]

    high_20d = float(d1["high"].shift(1).tail(s["breakout_days"]).max())
    low_20d = float(d1["low"].shift(1).tail(s["breakout_days"]).min())
    high_24h = float(h1["high"].shift(1).tail(24).max())
    low_24h = float(h1["low"].shift(1).tail(24).min())
    ema_gap = filters["ema_gap_pct"]

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
            h4_ema_dir = f["h4_ema21"] > f["h4_ema55"] * (1 + ema_gap)
            h4_ema_stack = h4_ema_dir and f["h4_ema55"] > f["h4_ema144"] * (1 + ema_gap)
            h4_slope_ok = (not pd.isna(f["h4_ema21_slope3"])) and f["h4_ema21_slope3"] > 0
            break_24h = r["close"] > high_24h
            breakout_buffer = r["close"] > high_20d * (1 + filters["breakout_buffer_pct"])
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
            h4_ema_dir = f["h4_ema21"] < f["h4_ema55"] * (1 - ema_gap)
            h4_ema_stack = h4_ema_dir and f["h4_ema55"] < f["h4_ema144"] * (1 - ema_gap)
            h4_slope_ok = (not pd.isna(f["h4_ema21_slope3"])) and f["h4_ema21_slope3"] < 0
            break_24h = r["close"] < low_24h
            breakout_buffer = r["close"] < low_20d * (1 - filters["breakout_buffer_pct"])

        adx_ok = f["adx"] > s["adx_threshold"]
        h4_adx_rising = (not pd.isna(f["h4_adx_delta3"])) and f["h4_adx_delta3"] > 0
        boll_expand = (not pd.isna(r["boll_width_change_24h"])) and r["boll_width_change_24h"] > 0
        vol_ok = (not pd.isna(r["vol_ratio"])) and r["vol_ratio"] > s["volume_ratio_threshold"]
        extension_pct = abs(float(r["close"]) / float(r["ema21"]) - 1) if r["ema21"] else float("inf")
        not_extended = extension_pct <= filters["small_max_extension_pct"]
        not_major_extended = extension_pct <= filters["major_max_extension_pct"]

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

        candidate = {
            "name": name, "direction": direction, "side_text": side_text,
            "score": score, "details": details, "close": float(r["close"]),
            "h4_adx": float(f["adx"]), "plus_di": float(r["plus_di"]),
            "minus_di": float(r["minus_di"]), "di_gap": float(di_gap),
            "ema21": float(r["ema21"]), "ema55": float(r["ema55"]),
            "ema144": float(r["ema144"]),
            "vol_ratio": None if pd.isna(r["vol_ratio"]) else float(r["vol_ratio"]),
            "boll_width_change_24h": None if pd.isna(r["boll_width_change_24h"]) else float(r["boll_width_change_24h"]),
            "daily_ok": bool(daily_ok), "ema_full": bool(ema_full),
            "h4_ema_dir": bool(h4_ema_dir), "h4_ema_stack": bool(h4_ema_stack),
            "h4_slope_ok": bool(h4_slope_ok), "h4_adx_rising": bool(h4_adx_rising),
            "h4_adx_delta3": None if pd.isna(f["h4_adx_delta3"]) else float(f["h4_adx_delta3"]),
            "h4_ema21": float(f["h4_ema21"]), "h4_ema55": float(f["h4_ema55"]),
            "h4_ema144": float(f["h4_ema144"]), "extension_pct": float(extension_pct),
            "not_extended": bool(not_extended), "not_major_extended": bool(not_major_extended),
            "vol_ok": bool(vol_ok), "boll_expand": bool(boll_expand),
            "break_24h": bool(break_24h), "breakout_buffer": bool(breakout_buffer),
            "breakout": bool(breakout), "pullback_zone": float(pullback_zone),
            "invalid_price": float(invalid_price), "target1": float(target1),
            "target2": float(target2), "timestamp": str(r["timestamp"]),
        }
        candidate["signal_tier"] = classify_signal(candidate, cfg)
        candidate["quality_reasons"], candidate["reject_reasons"] = signal_reason_lists(candidate, cfg)
        candidates.append(candidate)

    return max(
        candidates,
        key=lambda x: (SIGNAL_TIER_RANKS.get(x.get("signal_tier", "none"), 0), x["score"]),
    )


def level(score, cfg, signal_tier=None):
    if signal_tier == "major_trend":
        return "强趋势预警"
    if signal_tier == "small_trend":
        return "趋势观察"
    th = cfg["score_thresholds"]
    if score >= th["extreme"]:
        return "极强预警"
    if score >= th["strong"]:
        return "强预警"
    if score >= th["watch"]:
        return "观察预警"
    return "无信号"


def format_message(result, cfg):
    vol_text = "NA" if result["vol_ratio"] is None else f"{result['vol_ratio']:.2f}x"
    bw_text = "NA" if result["boll_width_change_24h"] is None else f"{result['boll_width_change_24h']*100:.2f}%"
    breakout_text = "是" if result["breakout"] else "否"
    detail_text = "\n".join("- " + d for d in result["details"]) if result["details"] else "- 无"
    quality_text = "\n".join("- " + d for d in result.get("quality_reasons", [])) or "- 无"
    reject_text = "\n".join("- " + d for d in result.get("reject_reasons", [])) or "- 无"
    tier_text = signal_tier_label(result.get("signal_tier"))

    return f"""
{level(result['score'], cfg, result.get('signal_tier'))} | {result['name']} | {result['side_text']}

信号层级：{tier_text}
分数：{result['score']}/100
价格：{result['close']:.4f}

核心指标：
- 4H ADX：{result['h4_adx']:.2f}
- 4H ADX 3根变化：{result.get('h4_adx_delta3') if result.get('h4_adx_delta3') is not None else 'NA'}
- 1H +DI：{result['plus_di']:.2f}
- 1H -DI：{result['minus_di']:.2f}
- DI差值：{result['di_gap']:.2f}
- 1H EMA21：{result['ema21']:.4f}
- 1H EMA55：{result['ema55']:.4f}
- 1H EMA144：{result['ema144']:.4f}
- 价格偏离1H EMA21：{result.get('extension_pct', 0) * 100:.2f}%
- 成交量倍率：{vol_text}
- BOLL宽度24H变化：{bw_text}
- 20日突破：{breakout_text}

趋势过滤通过：
{quality_text}

仍需注意：
{reject_text}

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


def alert_key(result):
    return (
        result.get("name"),
        result.get("direction"),
        result.get("timestamp"),
        result.get("score"),
    )


def build_alerts(results, cfg):
    threshold = cfg["score_thresholds"]["watch"]
    seen = set()
    alerts = []
    for result in results:
        if "signal_tier" in result:
            if result.get("signal_tier") not in ALERT_SIGNAL_TIERS:
                continue
        else:
            if result["score"] < threshold:
                continue
        key = alert_key(result)
        if key in seen:
            continue
        seen.add(key)
        alerts.append(result)
    return sorted(
        alerts,
        key=lambda x: (SIGNAL_TIER_RANKS.get(x.get("signal_tier", "none"), 0), x["score"]),
        reverse=True,
    )


def new_alert_state():
    return {"version": ALERT_STATE_VERSION, "alerts": {}}


def load_alert_state(path):
    path = Path(path)
    if not path.exists():
        return new_alert_state()

    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        print(f"Alert state load failed, starting fresh: {e}")
        return new_alert_state()

    alerts = state.get("alerts") if isinstance(state, dict) else None
    if not isinstance(alerts, dict):
        return new_alert_state()

    return {"version": ALERT_STATE_VERSION, "alerts": alerts}


def save_alert_state(state, path):
    path = Path(path)
    if path.parent != Path("."):
        path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def alerting_config(cfg):
    return cfg.get("alerting", {})


def alert_state_path(cfg):
    return alerting_config(cfg).get("state_file", DEFAULT_ALERT_STATE_FILE)


def dedupe_cooldown(cfg):
    hours = alerting_config(cfg).get(
        "dedupe_cooldown_hours",
        DEFAULT_DEDUPE_COOLDOWN_HOURS,
    )
    return timedelta(hours=float(hours))


def score_band(score, cfg):
    th = cfg["score_thresholds"]
    if score >= th["extreme"]:
        return "extreme"
    if score >= th["strong"]:
        return "strong"
    if score >= th["watch"]:
        return "watch"
    return "none"


def persistent_alert_key(result):
    return f"{result.get('name')}:{result.get('direction')}"


def parse_state_timestamp(value):
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def should_send_persistent_alert(result, state, cfg, now=None):
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    record = state.get("alerts", {}).get(persistent_alert_key(result))
    if not record:
        return True

    current_tier = result.get("signal_tier", "none")
    previous_tier = record.get("tier", "none")
    if SIGNAL_TIER_RANKS.get(current_tier, 0) > SIGNAL_TIER_RANKS.get(previous_tier, 0):
        return True

    current_band = score_band(result["score"], cfg)
    previous_band = record.get("band") or score_band(record.get("score", 0), cfg)
    if ALERT_BAND_RANKS[current_band] > ALERT_BAND_RANKS.get(previous_band, 0):
        return True

    last_sent_at = parse_state_timestamp(record.get("last_sent_at"))
    if last_sent_at is None:
        return True

    return now - last_sent_at >= dedupe_cooldown(cfg)


def filter_persistent_alerts(alerts, state, cfg, now=None):
    now = now or datetime.now(timezone.utc)
    sendable = []
    suppressed = []
    for alert in alerts:
        if should_send_persistent_alert(alert, state, cfg, now):
            sendable.append(alert)
        else:
            suppressed.append(alert)
    return sendable, suppressed


def record_sent_alert(result, state, cfg, now=None):
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    record = {
        "last_sent_at": now.isoformat(),
        "score": result["score"],
        "band": score_band(result["score"], cfg),
    }
    if "signal_tier" in result:
        record["tier"] = result["signal_tier"]
    state.setdefault("alerts", {})[persistent_alert_key(result)] = record


def send_serverchan(
    text,
    title="趋势预警",
    session=None,
    timeout=20,
    retries=2,
    sleep_func=time.sleep,
):
    sendkey = os.getenv("SERVERCHAN_SENDKEY", "").strip()
    if not sendkey:
        print("[SERVERCHAN_SENDKEY 未配置]")
        print(text)
        return False
    url = f"https://sctapi.ftqq.com/{sendkey}.send"
    client = session or requests
    payload = {"title": title, "desp": text}

    for attempt in range(1, retries + 1):
        try:
            response = client.post(url, data=payload, timeout=timeout)
            response.raise_for_status()
        except requests.RequestException as e:
            print(f"Server酱发送失败({attempt}/{retries}): {e}")
            if attempt < retries:
                sleep_func(1)
            continue

        try:
            data = response.json()
        except ValueError:
            print("Server酱返回非 JSON，发送结果未知")
            print(response.text)
            return False

        code = data.get("code", data.get("errno", 0))
        if code in (0, "0"):
            print("Server酱发送成功")
            print(response.text)
            return True

        print(f"Server酱发送失败: {data}")
        return False

    return False


def main():
    cfg = load_config()
    state_path = alert_state_path(cfg)
    state = load_alert_state(state_path)
    print(f"Checking trend hunter V2 at {datetime.now(timezone.utc).isoformat()}")

    try:
        results = []
        for sym in cfg["symbols"]:
            try:
                res = score_symbol(sym, cfg)
                results.append(res)
                print(f"{res['name']} {res['direction']} score={res['score']} tier={res.get('signal_tier', 'none')}")
            except Exception as e:
                print(f"Failed {sym['name']}: {e}")

        alerts = build_alerts(results, cfg)
        if not alerts:
            print("No alerts above threshold.")
            print("\n".join([
                f"{r['name']} {r['direction']} {r['score']}/100 tier={r.get('signal_tier', 'none')}"
                for r in results
            ]))
            return

        now = datetime.now(timezone.utc)
        sendable_alerts, suppressed_alerts = filter_persistent_alerts(alerts, state, cfg, now)
        for r in suppressed_alerts:
            print(
                f"Suppressed duplicate alert: {r['name']} {r['direction']} "
                f"score={r['score']} tier={r.get('signal_tier', 'none')}"
            )

        if not sendable_alerts:
            print("All alerts suppressed by dedupe state.")
            return

        for r in sendable_alerts:
            msg = format_message(r, cfg)
            print(msg)
            sent = send_serverchan(msg, f"{r['name']} {level(r['score'], cfg, r.get('signal_tier'))}")
            if sent:
                record_sent_alert(r, state, cfg, now)
    finally:
        save_alert_state(state, state_path)


if __name__ == "__main__":
    main()
