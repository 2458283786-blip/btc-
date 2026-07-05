# Trend Hunter V2

大趋势猎手 V2：使用 GitHub Actions 定时拉取 OKX K 线数据，并通过 Server 酱推送微信告警。

## 监控标的

默认监控：

- BTCUSDT
- ETHUSDT
- SOLUSDT
- SUIUSDT
- DOGEUSDT
- LINKUSDT
- AVAXUSDT
- BNBUSDT

标的配置在 `config.json` 的 `symbols` 中。

## 核心逻辑

策略会分别检查多周期趋势、动量和成交量：

- 1H EMA21/55/144 完整排列
- 4H ADX 高于阈值
- 1H DI 差值高于阈值
- 日线 EMA55/144 方向过滤
- BOLL 宽度扩张
- 成交量放大
- 20 日高低点突破加分
- 输出回踩或反抽 EMA21 的交易参考

信号不会只按分数推送。脚本会先给 LONG/SHORT 候选打分，再用趋势过滤把信号分成：

- `small_trend`：偏 3%-5% 波段观察，要求 4H EMA21/55 同向、4H ADX 走强、价格没有明显远离 1H EMA21，并且至少有结构突破或量能确认。
- `major_trend`：偏 5%-10% 甚至更大单边趋势，额外要求日线 EMA55/144 同向、4H EMA21/55/144 完整排列、4H EMA21 斜率同向、4H ADX 走强。

带 `confirm=0` 的 OKX 未收盘 K 线会被跳过，避免小时线未收完时的假突破进入计算。

默认告警分数阈值：

- 80 分：观察预警
- 90 分：强预警
- 100 分：极强预警

阈值配置在 `config.json` 的 `score_thresholds` 中。

趋势过滤配置在 `config.json` 的 `signal_filters` 中：

```json
{
  "small_score": 85,
  "major_score": 90,
  "small_max_extension_pct": 0.03,
  "major_max_extension_pct": 0.04,
  "ema_gap_pct": 0.0015,
  "breakout_buffer_pct": 0.002
}
```

## 告警去重

跨小时去重由 `alert_state.json` 记录最近一次成功推送的 `币种:方向`。

默认行为：

- 同一个币种和方向在 12 小时内只推送一次
- 如果告警等级升级，例如 80 分观察预警升级到 90 分强预警，会立即再次推送
- 如果信号层级从 `small_trend` 升级到 `major_trend`，也会立即再次推送
- 只有 Server 酱返回成功后才会写入状态，发送失败不会进入冷却

配置在 `config.json` 的 `alerting` 中：

```json
{
  "state_file": "alert_state.json",
  "dedupe_cooldown_hours": 12
}
```

GitHub Actions 使用 cache 恢复和保存 `alert_state.json`，不会把状态文件提交回仓库。

## 本地运行

```bash
pip install -r requirements.txt
python alert_bot.py
```

如果没有配置 `SERVERCHAN_SENDKEY`，脚本会把告警内容打印到控制台，不会发送微信推送，也不会记录为已发送。

## 测试

```bash
python -m pytest -q
```

测试覆盖：

- Server 酱临时失败后的重试
- Server 酱 API 错误返回的失败处理
- 单次运行内重复告警过滤和按分数排序
- OKX 未收盘 K 线过滤
- `small_trend` / `major_trend` 趋势分层
- 跨小时冷却期去重
- 告警等级升级时绕过去重
- 状态文件缺失或损坏时安全回退

## GitHub Actions

工作流文件：`.github/workflows/alert.yml`

触发方式：

- `schedule`：默认 `5 * * * *`，即每小时第 5 分钟触发一次
- `workflow_dispatch`：支持手动触发

运行流程：

1. 安装依赖
2. 恢复 `alert_state.json`
3. 运行测试
4. 运行告警机器人
5. 保存更新后的 `alert_state.json`

注意：GitHub Actions 的定时任务不是严格准点任务，实际执行可能延迟或偶尔跳过。

## Server 酱配置

在 GitHub 仓库中添加 Secret：

```text
SERVERCHAN_SENDKEY
```

脚本会调用：

```text
https://sctapi.ftqq.com/{SERVERCHAN_SENDKEY}.send
```

发送失败时会打印错误，并对临时网络错误进行重试。

## 当前限制

GitHub Actions cache 是不可变缓存，所以工作流每次运行会用新的 cache key 保存一份很小的状态文件，并通过前缀恢复最近状态。GitHub 会按平台策略清理旧缓存。

本项目输出的是趋势观察和交易参考，不构成投资建议。
