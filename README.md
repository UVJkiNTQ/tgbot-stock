# tgbot-stock - Telegram 股票持仓 Bot

群友股票买卖记录、持仓查看、损益计算，接入实时行情。

## 编译与运行（Rust 版）

```bash
cargo build --release
export TG_BOT_TOKEN="你的Bot Token"
./target/release/tgbot-stock
```

可选环境变量：
- `BOT_DB_PATH`：SQLite 数据库路径（默认 `data/trades.db`，与 Python 版 v3 数据库完全兼容）
- `BOT_TIMEZONE`：`/trades` 展示时区（默认 `Asia/Shanghai`）
- `BOT_CHART_FONT`：手动指定中文字体路径（`.ttc` / `.ttf` / `.otf`）
- `TG_API_BASE`：自定义 Telegram Bot API 地址（默认 `https://api.telegram.org`）

## 命令

| 命令 | 说明 | 示例 |
|------|------|------|
| `/quote SYMBOL` | 查询实时行情、当日分时图和日K线图 | `/quote 600000` |
| `/find KEYWORD` | 按名称搜索股票代码（英文/拼音走 Yahoo，中文走腾讯） | `/find wacom` 或 `/find 腾讯` |
| `/buy SYMBOL PRICE QTY [Nx]` | 买入或按指定杠杆回补空仓 | `/buy 600000 10.50 100 5x` |
| `/sell SYMBOL PRICE QTY [Nx]` | 卖出或按指定杠杆建立空仓 | `/sell 600000 11.00 100 5x` |
| `/buya SYMBOL PRICE AMOUNT [Nx] [Ns]` | 按人民币预算计算不超预算的最大买入量 | `/buya 600000 10.50 10000 5x 100s` |
| `/sella SYMBOL PRICE AMOUNT [Nx] [Ns]` | 按人民币预算计算不超预算的最大卖出量 | `/sella 600000 11.00 10000 5x 100s` |
| `/buyn SYMBOL QTY [Nx]` | 使用 `/quote` 现价买入，仍需确认 | `/buyn 600000 100 5x` |
| `/selln SYMBOL QTY [Nx]` | 使用 `/quote` 现价卖出，仍需确认 | `/selln 600000 100 5x` |
| `/buyna SYMBOL AMOUNT [Nx] [Ns]` | 使用现价按人民币预算计算最大买入量 | `/buyna 600000 10000 5x 100s` |
| `/sellna SYMBOL AMOUNT [Nx] [Ns]` | 使用现价按人民币预算计算最大卖出量 | `/sellna 600000 10000 5x 100s` |
| `/buy SYMBOL PRICE ALL [Nx]` | 平全部空头，或只平指定杠杆空头 | `/buy 600000 10.50 ALL`；`/buy 600000 10.50 ALL 5x` |
| `/sell SYMBOL PRICE ALL [Nx]` | 平全部多头，或只平指定杠杆多头 | `/sell 600000 11.00 ALL`；`/sell 600000 11.00 ALL 5x` |
| `/close SYMBOL PRICE` | 一次平掉该代码的全部多空杠杆条目 | `/close 600000 11.00` |
| `/position` | 查看持仓和浮盈 | `/position` |
| `/pnl` | 查看已实现收益、浮动盈亏和历史总收益 | `/pnl` |
| `/lb [u\|r]` | 总收益排行（u，默认）或仅已实现收益排行（r） | `/lb` 或 `/lb r` |
| `/trades` | 查看交易记录（含ID） | `/trades` |
| `/del ID [ID ...]` | 按ID删除一条或多条交易记录 | `/del 3` 或 `/del 3 5 8` |
| `/del SYMBOL Nx` | 删除指定代码、杠杆的全部交易记录 | `/del 600000 5x` |
| `/update` | 将旧数据库一次规整为当前 v3 | `/update` |

## SYMBOL 格式（含基金）

| 市场 | 格式 | 示例 |
|------|------|------|
| A股（沪） | 6位数字，600/601/603/605/688/689 等代码段 | 600000 510050 |
| A股（深） | 6位数字，000/001/002/003/300/301 等代码段 | 000001 159919 |
| 北交所 | 430、830-839、870-879、920 代码段 | 920118 |
| 港股 | 5位数字 | 00700 02800 |
| 美股 | 字母 | AAPL TSLA |
| 日股 | 4位代码，必须加 `.JP` 后缀或 `JP` 前缀 | 7203.JP / JP7203 / 9984.JP |
| 场外基金 | 004-019 开头的6位数字自动识别；重叠代码加 `.F` | 010042 / 002714.F |

纯四位数字仍按港股处理（补零为五位）；港股查不到行情时，会提示可改用 `.JP` 查询同名日本股票。

## 行情来源

- A股/港股：新浪财经（免费，延迟~3秒）
- 美股：Yahoo Finance
- 日股：Yahoo Finance（`7203.JP` 对应 `7203.T`）
- 场外基金：天天基金（估算净值）
- `/quote` 附当日分时图：A股/北交所/港股用腾讯分钟接口，美股/日股用 Yahoo Finance 分钟数据；基金不提供分时图。分时图获取或发送失败时自动退回文字行情
- `/quote` 同时附日K线图（默认展示最近约90个交易日）：A股用腾讯前复权日K，港股用腾讯日K，北交所用新浪日K，美股/日股用 Yahoo Finance 日线；日K获取失败时只发送分时图，全部图表失败时退回文字行情
- 图表标题按“名称(代码)”显示；中文名称需要 CJK 字体（如文泉驿微米黑、Noto Sans CJK），程序会自动搜索系统/用户字体目录，也可用环境变量 `BOT_CHART_FONT=/path/to/font.ttc` 指定；找不到 CJK 字体时标题只显示代码
- 行情按“证券代码 + 市场”分别缓存 3 秒，避免同代码股票与基金串线
- `/find` 按名称搜索股票代码：英文/拼音使用 Yahoo Finance，中文使用腾讯 smartbox；结果优先展示本地上市，可直接复制到 `/quote` 或交易命令
- `/buyn`、`/selln`、`/buyna`、`/sellna` 自动使用与 `/quote` 同源的行情价格（含相同缓存），按确认单价格记录；确认、取消、杠杆与预算规则均与原命令相同，`/buyn`、`/selln` 也支持 `ALL [Nx]`
- 买入/卖出价格偏离现价 >5% 时需二次确认，按钮仅有发起人本人可操作
- 支持负持仓（先卖后买）；空头价格上涨时亏损、下跌时盈利
- QTY 使用“股”为输入单位并支持最多两位小数；例如输入 `1` 股存为整数 `100`，输入 `0.01` 股存为整数 `1`
- `/buya` 和 `/sella` 的 `AMOUNT` 是人民币预算，先按 `rate` 换算为标的币种，再按 `AMOUNT / (PRICE × rate)` 算量并按最小单位向下取整，保证折算成交额不超过预算；最小单位 `100s`、`1s`、`01s`、`001s` 分别代表 100、1、0.1、0.01 股，省略时为 0.01 股，连一份最小单位也买不起时不会进入确认流程
- 杠杆是交易命令的第 4 个参数，如填写只接受以 `x` 结尾的格式（如 `1x`、`2.5x`、`5x`）；首次建仓可省略并按 `1x` 处理，已有同向持仓时省略会沿用该标的最近一笔交易的杠杆，并在确认消息中展示
- 杠杆按保证金口径计算：数量、名义成本、市值和 PnL 金额不乘杠杆；保证金为名义成本除以杠杆，收益率为 PnL 除以保证金
- 同一代码按市场和杠杆分成独立持仓条目；同方向使用新杠杆时会新建条目并在确认消息中提醒
- 普通反向交易仍必须填写目标条目的正确杠杆，包括平 `1x` 仓也必须写 `1x`；缺少或写错时操作无效
- `SELL ... ALL` 一次平掉全部多头条目，`SELL ... ALL 5x` 只平 `5x` 多头；`BUY ... ALL [Nx]` 对空头使用相同规则
- `/close SYMBOL PRICE` 在一个数据库事务中一次关闭该代码的全部多空杠杆条目
- `/pnl` 按加权平均成本统计完整历史：平仓数量计入已实现收益，剩余持仓按实时行情计入浮动盈亏；外币历史收益使用成交时保存的汇率
- `/lb` 等同 `/lb u`，按已实现收益加浮动盈亏排行；`/lb r` 只按已平仓产生的已实现收益排行
- `/del` 可接受一个或多个记录 ID，也可用 `SYMBOL Nx` 一次删除指定市场代码和杠杆桶的全部记录
- 港股价格及名称按新浪港股字段解析，交易金额以 HKD 记录并按实时汇率折算 CNY
- 日股价格及名称按 Yahoo Finance 字段解析，交易金额以 JPY 记录并按实时汇率折算 CNY
- `/trades` 的交易时间在数据库中保留 UTC，展示时转换为 `BOT_TIMEZONE`（默认 `Asia/Shanghai`）
- `/update` 期间机器人进入维护模式：开始时提示暂停操作，交易、查询和确认按钮会被拦截，事务结束后再通知恢复使用

## 数据结构

所有交易记录存储在 `data/trades.db`，市场会在确认交易时与证券代码一起入库，持仓按“证券代码 + 市场 + 杠杆”聚合为独立条目，并分别计算多空方向的加权平均开仓价。数量以 `0.01` 股为最小整数单位存储。旧数据库升级后执行一次 `/update`：v0→v1 会把历史数量乘以 `100`、增加杠杆与市场字段并按代码补齐市场，v1→v2 会扩展市场约束以支持北交所，v2→v3 会扩展市场约束以支持日股；基金专用代码段也会从旧版误标的 A 股桶中修正为 `FUND`。无法仅凭代码区分的冲突代码必须使用 `.F`、`.A` 或 `.JP`。版本号与数据在同一事务提交，重复执行不会再次转换。升级前的短暂窗口仍能兼容读取旧数量格式。

## 代码结构

- `src/main.rs` / `src/config.rs`：程序入口、环境变量配置与长轮询主循环
- `src/telegram.rs`：基于 `reqwest`（`rustls`）的轻量 Telegram Bot API 客户端与有界内存 FSM
- `src/http_client.rs` / `src/cache.rs`：共享 HTTP 连接池、Yahoo Finance v8 封装与有界 TTL 缓存
- `src/handlers/`：命令与回调路由（`system.rs`、`trading.rs`、`callbacks.rs`、`reports.rs`、`common.rs`）
- `src/models/`：SQLite 数据访问层（`types.rs`、`schema.rs`、`repository.rs`）
- `src/pnl.rs`：持仓成本、盈亏与排行榜计算
- `src/quotes.rs` / `src/instrument.rs` / `src/instrument_search.rs`：证券代码识别、实时行情、汇率与 `/find` 搜索
- `src/intraday.rs` / `src/kline.rs` / `src/chartstyle.rs`：分时/日 K 数据获取及 `tiny-skia` + `ab_glyph` + `mmap` 字体图表渲染
