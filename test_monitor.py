from __future__ import annotations

import unittest
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import monitor


SAMPLE = 'v_sz159659="51~纳斯达克100ETF招商~159659~2.458~2.382~2.409~1982462~1058519~919988~2.458~1414~2.457~9074~2.456~2965~2.455~960~2.454~766~2.459~639~2.460~3199~2.461~700~2.462~728~2.463~205~~20260601142003~0.076~3.19~2.459~2.400~2.458/1982462/482332933~1982462~48233~4.59~~~2.459~2.400~2.48~106.15~106.15~0.00~2.620~2.144~1.11~9708~2.433~~~~~~48233.2933~0.0000~0~~ETF~23.89~5.90~~~~2.459~1.657~13.38~19.79~30.19~4318744064~4318744064~47.01~35.13~4318744064~10.60~2.2224~48.88~0.37~2.2164~CNY~0~~2.449~6513~";'


class TencentQuoteTest(unittest.TestCase):
    def test_parse_premium_rate(self) -> None:
        quotes = monitor.parse_tencent_quotes(SAMPLE)

        self.assertEqual(quotes["159659"]["name"], "纳斯达克100ETF招商")
        self.assertEqual(quotes["159659"]["premium_rate"], 10.60)
        self.assertEqual(quotes["159659"]["quote_time"], "20260601142003")


class NotificationLimitTest(unittest.TestCase):
    def test_can_notify_allows_only_configured_count_per_24h(self) -> None:
        tz = ZoneInfo("Asia/Shanghai")
        now = datetime(2026, 6, 1, 14, 20, tzinfo=tz)
        state = {
            "notifications": {
                "159659": [
                    (now - timedelta(hours=23)).isoformat(),
                    (now - timedelta(hours=1)).isoformat(),
                    (now - timedelta(minutes=10)).isoformat(),
                    (now - timedelta(hours=25)).isoformat(),
                ]
            }
        }

        self.assertFalse(monitor.can_notify(state, "159659", now, 3))
        self.assertEqual(len(state["notifications"]["159659"]), 3)

    def test_market_window(self) -> None:
        tz = ZoneInfo("Asia/Shanghai")

        self.assertTrue(
            monitor.is_monitoring_time(datetime(2026, 6, 1, 9, 15, tzinfo=tz))
        )
        self.assertTrue(
            monitor.is_monitoring_time(datetime(2026, 6, 1, 15, 10, tzinfo=tz))
        )
        self.assertFalse(
            monitor.is_monitoring_time(datetime(2026, 6, 1, 15, 11, tzinfo=tz))
        )
        self.assertFalse(
            monitor.is_monitoring_time(datetime(2026, 6, 6, 10, 0, tzinfo=tz))
        )

    def test_etf_market_prefix(self) -> None:
        self.assertEqual(monitor.market_prefix("159659"), "sz159659")
        self.assertEqual(monitor.market_prefix("510300"), "sh510300")

    def test_ignore_window_fetches_outside_market_time(self) -> None:
        tz = ZoneInfo("Asia/Shanghai")
        config = monitor.Config(
            etfs=[monitor.EtfTarget(code="159659", threshold=11.0)],
            webhooks=[monitor.Webhook(url="https://example.com/webhook")],
            default_threshold=0.0,
            poll_interval_seconds=60,
            timezone=tz,
            state_file=Path("/tmp/premium-rate-monitor-test-state.json"),
            max_notifications_per_24h=3,
        )
        called = {"fetch": False}
        original_fetch = monitor.fetch_tencent_quotes
        original_load_state = monitor.load_state
        original_save_state = monitor.save_state
        original_send_webhook = monitor.send_webhook

        def fake_fetch(codes: list[str]) -> dict[str, dict]:
            called["fetch"] = True
            return {
                "159659": {
                    "code": "159659",
                    "name": "ETF",
                    "price": 1.0,
                    "quote_time": "20260601151151",
                    "change_pct": 0.0,
                    "premium_rate": 10.0,
                }
            }

        try:
            monitor.fetch_tencent_quotes = fake_fetch
            monitor.load_state = lambda path: {"notifications": {}}
            monitor.save_state = lambda path, state: None
            monitor.send_webhook = lambda webhook, text, event: None

            sent = monitor.run_once(
                config,
                now=datetime(2026, 6, 1, 15, 11, tzinfo=tz),
                ignore_window=True,
            )
        finally:
            monitor.fetch_tencent_quotes = original_fetch
            monitor.load_state = original_load_state
            monitor.save_state = original_save_state
            monitor.send_webhook = original_send_webhook

        self.assertTrue(called["fetch"])
        self.assertEqual(sent, 1)


if __name__ == "__main__":
    unittest.main()
