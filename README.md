# BTC/ETH 趋势预警机器人 - GitHub Actions版

这个版本可以放到 GitHub 仓库里，每小时自动运行一次，并推送 Telegram 到手机。

## 你要做的步骤

### 1. 上传文件到你的仓库

仓库：

https://github.com/2458283786-blip/btc-

把压缩包里的所有文件上传到仓库根目录。

最终结构应该是：

```text
btc-
├─ alert_bot.py
├─ config.json
├─ requirements.txt
└─ .github
   └─ workflows
      └─ alert.yml
```

### 2. 创建 Telegram Bot

1. 打开 Telegram
2. 搜索 BotFather
3. 发送 `/newbot`
4. 创建机器人
5. 复制 Bot Token

### 3. 获取 chat_id

1. 打开你创建的机器人
2. 点击 Start
3. 给它发一句 hello
4. 浏览器打开：

```text
https://api.telegram.org/bot你的TOKEN/getUpdates
```

找到：

```json
"chat":{"id":123456789
```

这个数字就是 chat_id。

### 4. 在 GitHub 设置 Secrets

进入你的仓库：

Settings → Secrets and variables → Actions → New repository secret

添加两个：

```text
TELEGRAM_BOT_TOKEN
TELEGRAM_CHAT_ID
```

注意名字必须完全一样。

### 5. 手动测试

进入仓库：

Actions → Trend Alert Bot → Run workflow

如果成功，Telegram 会收到提醒。

### 6. 自动运行

之后 GitHub 会每小时自动运行一次。

## 信号说明

分数：
- 80+：观察
- 90+：强预警
- 100：极强预警

策略：
- 只做 BTC/ETH
- 目标是捕捉未来 24~72 小时可能出现的 5%~10% 单边波段
- 机器人只推送，不自动下单

## 重要提醒

这不是稳赚系统。
建议先观察 1~2 周，记录每次预警后 24h/72h 的涨跌幅，再决定是否接自动交易。
