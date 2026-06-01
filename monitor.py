#!/usr/bin/env python3
"""Monitor ETF premium rates from Tencent quotes and notify by webhook."""

from __future__ import annotations

import argparse
import json
import logging
import ssl
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, time as day_time, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


TENCENT_QUOTE_URLS = ("https://qt.gtimg.cn/q=", "http://qt.gtimg.cn/q=")
PREMIUM_RATE_INDEX = 77
DEFAULT_CONFIG_PATH = "config.json"


@dataclass(frozen=True)
class EtfTarget:
    code: str
    threshold: float


@dataclass(frozen=True)
class Webhook:
    url: str
    type: str = "generic"


@dataclass(frozen=True)
class Config:
    etfs: list[EtfTarget]
    webhooks: list[Webhook]
    default_threshold: float
    poll_interval_seconds: int
    timezone: ZoneInfo
    state_file: Path
    max_notifications_per_24h: int


def load_config(path: Path) -> Config:
    raw = json.loads(path.read_text(encoding="utf-8"))
    default_threshold = float(raw.get("default_threshold", 0))

    etfs = []
    for item in raw.get("etfs", []):
        if isinstance(item, str):
            etfs.append(EtfTarget(code=item, threshold=default_threshold))
        else:
            etfs.append(
                EtfTarget(
                    code=str(item["code"]),
                    threshold=float(item.get("threshold", default_threshold)),
                )
            )

    webhooks = []
    for item in raw.get("webhooks", []):
        if isinstance(item, str):
            webhooks.append(Webhook(url=item))
        else:
            webhooks.append(Webhook(url=item["url"], type=item.get("type", "generic")))

    if not etfs:
        raise ValueError("config.etfs must contain at least one ETF code")
    if not webhooks:
        raise ValueError("config.webhooks must contain at least one webhook URL")

    config_dir = path.parent
    state_file = Path(raw.get("state_file", "premium_rate_state.json"))
    if not state_file.is_absolute():
        state_file = config_dir / state_file

    return Config(
        etfs=etfs,
        webhooks=webhooks,
        default_threshold=default_threshold,
        poll_interval_seconds=int(raw.get("poll_interval_seconds", 60)),
        timezone=ZoneInfo(raw.get("timezone", "Asia/Shanghai")),
        state_file=state_file,
        max_notifications_per_24h=int(raw.get("max_notifications_per_24h", 3)),
    )


def market_prefix(code: str) -> str:
    if code.startswith(("5", "6", "9")):
        return f"sh{code}"
    if code.startswith("8"):
        return f"bj{code}"
    return f"sz{code}"


def fetch_tencent_quotes(codes: list[str]) -> dict[str, dict[str, Any]]:
    prefixed = [market_prefix(code) for code in codes]
    last_error: Exception | None = None
    for base_url in TENCENT_QUOTE_URLS:
        url = base_url + ",".join(prefixed)
        request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                data = response.read().decode("gbk")
            return parse_tencent_quotes(data)
        except urllib.error.URLError as exc:
            last_error = exc
            if not is_ssl_certificate_error(exc):
                raise
            logging.warning("quote HTTPS certificate check failed, retrying with HTTP")

    raise RuntimeError("failed to fetch Tencent quotes") from last_error


def is_ssl_certificate_error(exc: urllib.error.URLError) -> bool:
    return isinstance(exc.reason, ssl.SSLCertVerificationError)


def parse_tencent_quotes(data: str) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for line in data.strip().split(";"):
        if not line.strip() or "=" not in line or '"' not in line:
            continue

        key = line.split("=", 1)[0].split("_")[-1]
        vals = line.split('"', 2)[1].split("~")
        if len(vals) <= PREMIUM_RATE_INDEX:
            continue

        code = key[2:]
        result[code] = {
            "code": code,
            "name": vals[1],
            "price": to_float(vals[3]),
            "quote_time": vals[30],
            "change_pct": to_float(vals[32]),
            "premium_rate": to_float(vals[PREMIUM_RATE_INDEX]),
        }
    return result


def to_float(value: str) -> float:
    return float(value) if value else 0.0


def is_monitoring_time(now: datetime) -> bool:
    if now.weekday() >= 5:
        return False
    current = now.time()
    return day_time(9, 15) <= current <= day_time(15, 10)


def load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"notifications": {}}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        logging.warning("state file is invalid JSON, starting with empty state: %s", path)
        return {"notifications": {}}


def save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def can_notify(
    state: dict[str, Any],
    code: str,
    now: datetime,
    max_notifications_per_24h: int,
) -> bool:
    cutoff = now - timedelta(hours=24)
    history = recent_history(state, code, cutoff)
    state.setdefault("notifications", {})[code] = [dt.isoformat() for dt in history]
    return len(history) < max_notifications_per_24h


def record_notification(state: dict[str, Any], code: str, now: datetime) -> None:
    state.setdefault("notifications", {}).setdefault(code, []).append(now.isoformat())


def recent_history(state: dict[str, Any], code: str, cutoff: datetime) -> list[datetime]:
    raw_history = state.setdefault("notifications", {}).get(code, [])
    history = []
    for item in raw_history:
        try:
            dt = datetime.fromisoformat(item)
        except ValueError:
            continue
        if dt >= cutoff:
            history.append(dt)
    return history


def build_notification_text(quote: dict[str, Any], threshold: float) -> str:
    return (
        f"ETF溢价率低于阈值: {quote['name']}({quote['code']}) "
        f"当前溢价率={quote['premium_rate']:.2f}% 阈值={threshold:.2f}% "
        f"现价={quote['price']:.3f} 涨跌幅={quote['change_pct']:.2f}% "
        f"行情时间={quote['quote_time']}"
    )


def webhook_payload(webhook: Webhook, text: str, event: dict[str, Any]) -> dict[str, Any]:
    kind = webhook.type.lower()
    if kind == "bark" or "api.day.app" in webhook.url:
        return {"title": "ETF折溢价率监控", "body": text, "event": event}
    if kind == "feishu":
        return {"msg_type": "text", "content": {"text": text}}
    if kind in {"dingtalk", "wecom"}:
        return {"msgtype": "text", "text": {"content": text}}
    return {"title": "ETF premium rate alert", "text": text, "event": event}


def send_webhook(webhook: Webhook, text: str, event: dict[str, Any]) -> None:
    body = json.dumps(webhook_payload(webhook, text, event), ensure_ascii=False).encode(
        "utf-8"
    )
    request = build_webhook_request(webhook.url, body)
    try:
        open_webhook_request(request)
    except urllib.error.URLError as exc:
        if not is_ssl_certificate_error(exc):
            raise
        logging.warning(
            "webhook HTTPS certificate check failed, retrying without certificate verification"
        )
        open_webhook_request(request, context=ssl._create_unverified_context())


def build_webhook_request(url: str, body: bytes) -> urllib.request.Request:
    return urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )


def open_webhook_request(
    request: urllib.request.Request,
    context: ssl.SSLContext | None = None,
) -> None:
    with urllib.request.urlopen(request, timeout=10, context=context) as response:
        if response.status >= 400:
            raise RuntimeError(f"webhook returned HTTP {response.status}")


def send_test_notifications(config: Config) -> int:
    now = datetime.now(config.timezone)
    event = {
        "type": "test",
        "checked_at": now.isoformat(),
    }
    text = f"ETF折溢价率监控测试通知 checked_at={now.isoformat()}"
    sent = 0
    for webhook in config.webhooks:
        try:
            send_webhook(webhook, text, event)
            sent += 1
            logging.info("test notification sent: %s", webhook.url)
        except (urllib.error.URLError, TimeoutError, RuntimeError) as exc:
            logging.exception("failed to send test webhook %s: %s", webhook.url, exc)
    return sent


def run_once(
    config: Config,
    now: datetime | None = None,
    ignore_window: bool = False,
) -> int:
    now = now or datetime.now(config.timezone)
    if not ignore_window and not is_monitoring_time(now):
        logging.info("outside monitoring window: %s", now.isoformat())
        return 0

    targets = {target.code: target for target in config.etfs}
    quotes = fetch_tencent_quotes(list(targets))
    state = load_state(config.state_file)
    sent = 0

    for code, target in targets.items():
        quote = quotes.get(code)
        if quote is None:
            logging.warning("quote not found for ETF code: %s", code)
            continue

        premium_rate = quote["premium_rate"]
        if premium_rate >= target.threshold:
            logging.info(
                "%s premium_rate %.2f%% >= threshold %.2f%%",
                code,
                premium_rate,
                target.threshold,
            )
            continue

        if not can_notify(
            state,
            code,
            now,
            config.max_notifications_per_24h,
        ):
            logging.info("notification limit reached in 24h for ETF code: %s", code)
            continue

        event = {
            "code": code,
            "name": quote["name"],
            "premium_rate": premium_rate,
            "threshold": target.threshold,
            "price": quote["price"],
            "change_pct": quote["change_pct"],
            "quote_time": quote["quote_time"],
            "checked_at": now.isoformat(),
        }
        text = build_notification_text(quote, target.threshold)
        webhook_ok = False
        for webhook in config.webhooks:
            try:
                send_webhook(webhook, text, event)
                webhook_ok = True
            except (urllib.error.URLError, TimeoutError, RuntimeError) as exc:
                logging.exception("failed to send webhook %s: %s", webhook.url, exc)

        if webhook_ok:
            record_notification(state, code, now)
            sent += 1

    save_state(config.state_file, state)
    return sent


def run_forever(config: Config) -> None:
    while True:
        try:
            run_once(config)
        except Exception:
            logging.exception("monitor iteration failed")
        time.sleep(config.poll_interval_seconds)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="ETF premium rate monitor")
    parser.add_argument(
        "-c",
        "--config",
        default=DEFAULT_CONFIG_PATH,
        help="path to config JSON file",
    )
    parser.add_argument("--once", action="store_true", help="run one check and exit")
    parser.add_argument(
        "--test-notify",
        action="store_true",
        help="send a test notification to configured webhooks and exit",
    )
    parser.add_argument(
        "--ignore-window",
        action="store_true",
        help="ignore weekday and trading-time window checks",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(message)s",
    )
    config = load_config(Path(args.config))
    if args.test_notify:
        return 0 if send_test_notifications(config) == len(config.webhooks) else 1
    if args.once:
        return 0 if run_once(config, ignore_window=args.ignore_window) >= 0 else 1
    run_forever(config)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
