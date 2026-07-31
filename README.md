# tgbot-stock - Telegram 股票持仓 Bot

群友股票买卖记录、持仓查看、损益计算，接入实时行情。

## 安装

```bash
pip install -r requirements.txt
```

## 配置

设置环境变量：

```powershell
$env:TG_BOT_TOKEN = "你的Bot Token"
python main.py
```

或 Linux/Mac：

```bash
export TG_BOT_TOKEN="你的Bot Token"
python main.py
```

## 命令

| 命令 | 说明 | 示例 |
|------|------|------|
| `/quote SYMBOL` | 查询实时行情 | `/quote 600000` |
| `/buy SYMBOL PRICE QTY` | 记录买入 | `/buy 600000 10.50 100` 或 `/buy 01810 28.9 500` |
| `/sell SYMBOL PRICE QTY` | 记录卖出 | `/sell 600000 11.00 100` |
| `/position` | 查看持仓和浮盈 | `/position` |
| `/pnl` | 查看损益汇总 | `/pnl` |
| `/lb` | 收益率排行榜 | `/lb` |
| `/trades` | 查看交易记录（含ID） | `/trades` |
| `/del [ID]` | 删除交易记录 | `/del` 或 `/del 3` |

## SYMBOL 格式（含基金）

| 市场 | 格式 | 示例 |
|------|------|------|
| A股（沪） | 6位数字，5/6/9开头 | 600000 510050 |
| A股（深） | 6位数字，0/1/2/3开头 | 000001 159919 |
| 港股 | 5位数字 | 00700 02800 |
| 美股 | 字母 | AAPL TSLA |
| 场外基金 | 6位数字（自动识别） | 010042 |

## 行情来源

- A股/港股：新浪财经（免费，延迟~3秒）
- 美股：Yahoo Finance
- 场外基金：天天基金（估算净值）
- 行情缓存 3 秒，避免频繁请求被封
- 买入/卖出价格偏离现价 >5% 时需二次确认，按钮仅有发起人本人可操作
- 港股价格及名称按新浪港股字段解析，交易金额以 HKD 记录并按实时汇率折算 CNY

## 数据结构

所有交易记录存储在 `data/trades.db`，持仓由买卖记录聚合计算（加权平均成本）。
