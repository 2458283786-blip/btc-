import json
from datetime import datetime, timedelta, timezone

import requests

import alert_bot


class FakeResponse:
    def __init__(self, payload, text="response", status_error=None):
        self.payload = payload
        self.text = text
        self.status_error = status_error

    def raise_for_status(self):
        if self.status_error:
            raise self.status_error

    def json(self):
        return self.payload


class FakeSession:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = []

    def post(self, url, data, timeout):
        self.calls.append({"url": url, "data": data, "timeout": timeout})
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def test_send_serverchan_retries_transient_failure(monkeypatch):
    monkeypatch.setenv("SERVERCHAN_SENDKEY", "test-sendkey")
    session = FakeSession([
        requests.Timeout("temporary timeout"),
        FakeResponse({"code": 0, "message": "success"}),
    ])

    sent = alert_bot.send_serverchan(
        "body",
        "title",
        session=session,
        retries=2,
        sleep_func=lambda _seconds: None,
    )

    assert sent is True
    assert len(session.calls) == 2
    assert session.calls[0]["data"] == {"title": "title", "desp": "body"}


def test_send_serverchan_returns_false_for_api_error(monkeypatch):
    monkeypatch.setenv("SERVERCHAN_SENDKEY", "test-sendkey")
    session = FakeSession([
        FakeResponse({"code": 40001, "message": "bad sendkey"}, text="bad sendkey"),
    ])

    sent = alert_bot.send_serverchan(
        "body",
        "title",
        session=session,
        retries=1,
        sleep_func=lambda _seconds: None,
    )

    assert sent is False
    assert len(session.calls) == 1


def test_build_alerts_filters_duplicate_alerts_and_sorts_by_score():
    cfg = {"score_thresholds": {"watch": 80}}
    btc = {
        "name": "BTCUSDT",
        "direction": "LONG",
        "score": 95,
        "timestamp": "2026-07-05 00:00:00+00:00",
    }
    eth = {
        "name": "ETHUSDT",
        "direction": "LONG",
        "score": 85,
        "timestamp": "2026-07-05 00:00:00+00:00",
    }
    low_score = {
        "name": "SOLUSDT",
        "direction": "LONG",
        "score": 75,
        "timestamp": "2026-07-05 00:00:00+00:00",
    }

    alerts = alert_bot.build_alerts([eth, btc, dict(btc), low_score], cfg)

    assert [alert["name"] for alert in alerts] == ["BTCUSDT", "ETHUSDT"]


def test_fetch_okx_candles_ignores_unconfirmed_rows(monkeypatch):
    def fake_get(url, params, timeout):
        return FakeResponse({
            "code": "0",
            "data": [
                ["1000", "1", "2", "0.5", "1.5", "10", "10", "15", "0"],
                ["2000", "1", "3", "0.8", "2.5", "11", "11", "25", "1"],
                ["3000", "2", "4", "1.0", "3.5", "12"],
            ],
        })

    monkeypatch.setattr(alert_bot.requests, "get", fake_get)

    df = alert_bot.fetch_okx_candles("BTC-USDT-SWAP", "1H", 3)

    assert list(df["close"]) == [2.5, 3.5]


def signal_filter_cfg():
    return {
        "score_thresholds": {"watch": 80, "strong": 90, "extreme": 100},
        "signal_filters": {
            "small_score": 85,
            "major_score": 90,
            "small_max_extension_pct": 0.03,
            "major_max_extension_pct": 0.04,
            "ema_gap_pct": 0.0015,
            "breakout_buffer_pct": 0.002,
        },
    }


def trend_candidate(direction="LONG", **overrides):
    candidate = {
        "direction": direction,
        "score": 92,
        "daily_ok": True,
        "h4_ema_dir": True,
        "h4_ema_stack": True,
        "h4_slope_ok": True,
        "h4_adx_rising": True,
        "not_extended": True,
        "not_major_extended": True,
        "vol_ok": True,
        "boll_expand": True,
        "break_24h": False,
        "breakout_buffer": False,
    }
    candidate.update(overrides)
    return candidate


def test_classify_major_trend_long_requires_daily_and_h4_alignment():
    cfg = signal_filter_cfg()

    assert alert_bot.classify_signal(trend_candidate("LONG"), cfg) == "major_trend"
    assert alert_bot.classify_signal(
        trend_candidate("LONG", score=84, daily_ok=False),
        cfg,
    ) == "none"
    assert alert_bot.classify_signal(
        trend_candidate("LONG", h4_ema_stack=False),
        cfg,
    ) == "small_trend"


def test_classify_major_trend_short_mirrors_long():
    cfg = signal_filter_cfg()

    assert alert_bot.classify_signal(trend_candidate("SHORT"), cfg) == "major_trend"
    assert alert_bot.classify_signal(
        trend_candidate("SHORT", h4_slope_ok=False),
        cfg,
    ) == "small_trend"


def test_classify_small_trend_allows_lighter_confirmation():
    cfg = signal_filter_cfg()
    candidate = trend_candidate(
        "LONG",
        score=85,
        daily_ok=False,
        h4_ema_stack=False,
        h4_slope_ok=False,
        breakout_buffer=True,
        vol_ok=False,
        boll_expand=False,
    )

    assert alert_bot.classify_signal(candidate, cfg) == "small_trend"


def test_build_alerts_uses_signal_tier_not_raw_score():
    cfg = {"score_thresholds": {"watch": 80}}
    rejected = {
        "name": "BTCUSDT",
        "direction": "LONG",
        "score": 100,
        "timestamp": "2026-07-05 00:00:00+00:00",
        "signal_tier": "none",
    }
    small = {
        "name": "ETHUSDT",
        "direction": "LONG",
        "score": 96,
        "timestamp": "2026-07-05 00:00:00+00:00",
        "signal_tier": "small_trend",
    }
    major = {
        "name": "SOLUSDT",
        "direction": "SHORT",
        "score": 90,
        "timestamp": "2026-07-05 00:00:00+00:00",
        "signal_tier": "major_trend",
    }

    alerts = alert_bot.build_alerts([rejected, small, major], cfg)

    assert [alert["name"] for alert in alerts] == ["SOLUSDT", "ETHUSDT"]


def test_filter_persistent_alerts_suppresses_same_signal_inside_cooldown():
    cfg = {
        "score_thresholds": {"watch": 80, "strong": 90, "extreme": 100},
        "alerting": {"dedupe_cooldown_hours": 12},
    }
    now = datetime(2026, 7, 5, 8, 0, tzinfo=timezone.utc)
    alert = {"name": "BTCUSDT", "direction": "LONG", "score": 85}
    state = {
        "version": 1,
        "alerts": {
            "BTCUSDT:LONG": {
                "last_sent_at": (now - timedelta(hours=2)).isoformat(),
                "score": 85,
                "band": "watch",
            }
        },
    }

    sendable, suppressed = alert_bot.filter_persistent_alerts([alert], state, cfg, now)

    assert sendable == []
    assert suppressed == [alert]


def test_filter_persistent_alerts_allows_alert_band_upgrade_inside_cooldown():
    cfg = {
        "score_thresholds": {"watch": 80, "strong": 90, "extreme": 100},
        "alerting": {"dedupe_cooldown_hours": 12},
    }
    now = datetime(2026, 7, 5, 8, 0, tzinfo=timezone.utc)
    alert = {"name": "BTCUSDT", "direction": "LONG", "score": 90}
    state = {
        "version": 1,
        "alerts": {
            "BTCUSDT:LONG": {
                "last_sent_at": (now - timedelta(hours=2)).isoformat(),
                "score": 85,
                "band": "watch",
            }
        },
    }

    sendable, suppressed = alert_bot.filter_persistent_alerts([alert], state, cfg, now)

    assert sendable == [alert]
    assert suppressed == []


def test_filter_persistent_alerts_allows_signal_tier_upgrade_inside_cooldown():
    cfg = {
        "score_thresholds": {"watch": 80, "strong": 90, "extreme": 100},
        "alerting": {"dedupe_cooldown_hours": 12},
    }
    now = datetime(2026, 7, 5, 8, 0, tzinfo=timezone.utc)
    alert = {
        "name": "BTCUSDT",
        "direction": "LONG",
        "score": 90,
        "signal_tier": "major_trend",
    }
    state = {
        "version": 1,
        "alerts": {
            "BTCUSDT:LONG": {
                "last_sent_at": (now - timedelta(hours=2)).isoformat(),
                "score": 96,
                "band": "strong",
                "tier": "small_trend",
            }
        },
    }

    sendable, suppressed = alert_bot.filter_persistent_alerts([alert], state, cfg, now)

    assert sendable == [alert]
    assert suppressed == []


def test_record_sent_alert_updates_persistent_state():
    cfg = {"score_thresholds": {"watch": 80, "strong": 90, "extreme": 100}}
    now = datetime(2026, 7, 5, 8, 0, tzinfo=timezone.utc)
    alert = {"name": "ETHUSDT", "direction": "SHORT", "score": 95}
    state = alert_bot.new_alert_state()

    alert_bot.record_sent_alert(alert, state, cfg, now)

    assert state["alerts"]["ETHUSDT:SHORT"] == {
        "last_sent_at": "2026-07-05T08:00:00+00:00",
        "score": 95,
        "band": "strong",
    }


def test_record_sent_alert_stores_signal_tier_when_present():
    cfg = {"score_thresholds": {"watch": 80, "strong": 90, "extreme": 100}}
    now = datetime(2026, 7, 5, 8, 0, tzinfo=timezone.utc)
    alert = {
        "name": "ETHUSDT",
        "direction": "SHORT",
        "score": 90,
        "signal_tier": "major_trend",
    }
    state = alert_bot.new_alert_state()

    alert_bot.record_sent_alert(alert, state, cfg, now)

    assert state["alerts"]["ETHUSDT:SHORT"]["tier"] == "major_trend"


def test_load_alert_state_falls_back_when_file_missing_or_invalid(tmp_path):
    missing_path = tmp_path / "missing.json"
    invalid_path = tmp_path / "invalid.json"
    invalid_path.write_text("{not json", encoding="utf-8")

    missing_state = alert_bot.load_alert_state(missing_path)
    invalid_state = alert_bot.load_alert_state(invalid_path)

    assert missing_state == {"version": 1, "alerts": {}}
    assert invalid_state == {"version": 1, "alerts": {}}


def test_save_alert_state_writes_json(tmp_path):
    path = tmp_path / "state.json"
    state = {
        "version": 1,
        "alerts": {
            "BTCUSDT:LONG": {
                "last_sent_at": "2026-07-05T08:00:00+00:00",
                "score": 90,
                "band": "strong",
            }
        },
    }

    alert_bot.save_alert_state(state, path)

    assert json.loads(path.read_text(encoding="utf-8")) == state
