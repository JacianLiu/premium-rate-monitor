# ETF Premium Rate Monitor

监控 ETF 折溢价率，并在周一到周五 `09:15` 到 `15:10` 之间按配置轮询。当前溢价率低于阈值时发送 webhook 通知，同一个 ETF 在 24 小时内最多通知 3 次。

## 配置

复制示例配置：

```bash
cp config.example.json config.json
```

字段说明：

- `etfs`: 需要监控的 ETF 编码。可以写字符串，也可以写对象。对象里的 `threshold` 会覆盖全局阈值。
- `default_threshold`: 默认溢价率阈值，单位是百分比。例如 `0` 表示低于 `0%` 时通知。
- `webhooks`: 通知地址数组。支持字符串，或 `{ "url": "...", "type": "generic" }` 对象。
- `webhooks[].type`: 支持 `generic`、`feishu`、`dingtalk`、`wecom`。
- `poll_interval_seconds`: 轮询间隔。
- `state_file`: 通知限流状态文件。
- `max_notifications_per_24h`: 每个 ETF 24 小时最多通知次数。

## 运行

单次检查：

```bash
python3 monitor.py --config config.json --once
```

长期运行：

```bash
python3 monitor.py --config config.json
```

`monitor.py` 内部会判断时间窗口，不在周一到周五 `09:15-15:10` 时不会请求行情或发送通知。

## 行情字段

行情接口使用：

```text
https://qt.gtimg.cn/q=
```

脚本会自动给 ETF 编码补 `sh`、`sz`、`bj` 前缀，并解析腾讯响应中下标 `77` 的字段作为溢价率。参考响应里的 `10.60` 会被解析为 `10.60%`。

## 测试

```bash
python3 -m unittest
```
