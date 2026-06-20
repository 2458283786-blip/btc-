# Trend Hunter V2

大趋势猎手 V2：GitHub Actions + Server酱微信推送。

监控：BTC、ETH、SOL、SUI、DOGE、LINK、AVAX、BNB

核心逻辑：
- 1H EMA21/55/144 完整排列
- 4H ADX > 25
- 1H DI 差值 > 10
- 日线 EMA55/144 方向过滤
- BOLL 宽度扩张
- 成交量放大
- 20日高低点突破加分
- 给出回踩 EMA21 的交易建议

GitHub Secret：SERVERCHAN_SENDKEY
