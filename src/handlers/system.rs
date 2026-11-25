use crate::handlers::AppState;
use crate::models::ModelError;
use crate::telegram::Message;

pub const HELP_TEXT: &str = "股票持仓 Bot 命令：\n\n\
/quote SYMBOL — 查实时行情\n\
/find KEYWORD — 按名称搜索股票代码\n\
/buy SYMBOL PRICE QTY [Nx] — 买入；如填写杠杆需以 x 结尾\n\
/sell SYMBOL PRICE QTY [Nx] — 卖出；如填写杠杆需以 x 结尾\n\
/buya SYMBOL PRICE AMOUNT [Nx] [Ns] — 按人民币预算计算最大买入量\n\
/sella SYMBOL PRICE AMOUNT [Nx] [Ns] — 按人民币预算计算最大卖出量\n\
/buyn SYMBOL QTY [Nx] — 使用 /quote 现价买入\n\
/selln SYMBOL QTY [Nx] — 使用 /quote 现价卖出\n\
/buyna SYMBOL AMOUNT [Nx] [Ns] — 使用现价按人民币预算计算最大买入量\n\
/sellna SYMBOL AMOUNT [Nx] [Ns] — 使用现价按人民币预算计算最大卖出量\n\
/buy 或 /sell SYMBOL PRICE ALL [Nx] — 可平全部或只平指定杠杆条目\n\
/close SYMBOL PRICE — 一次平掉该代码的全部多空杠杆条目\n\
/position — 查看我的持仓和浮盈（统一CNY）\n\
/pnl — 查看历史已实现收益、当前浮盈和总收益\n\
/lb [u|r] — 总收益排行（u，默认）或仅已实现收益排行（r）\n\
/today — 今日持仓市值变动比例排行\n\
/trades — 查看我的交易记录（含ID，按北京时间显示）\n\
/del ID [ID ...] — 删除一条或多条交易记录\n\
/del SYMBOL Nx — 删除指定代码、杠杆的全部交易记录\n\
/update — 将旧数据库一次规整为当前 v3（可重复安全执行）\n\n\
SYMBOL 示例：600000(A股) 920118(北交所) 00700(港股) AAPL(美股) 7203.JP(日股)\n\
ETF 也支持：510050 159919 02800\n\n\
同方向使用不同杠杆会建立独立持仓条目。\n\
已有同向持仓时省略杠杆会沿用该标的最近一笔交易的杠杆，并在确认消息中展示；\n\
普通反向交易仍必须显式填写目标杠杆（包括 1x）。\n\
自动算量命令中，AMOUNT 均为人民币预算，并按标的币种汇率换算；\n\
现价命令仍需用户确认，按确认单中的行情价格记录；/buyn、/selln 也支持 ALL [Nx]。\n\
100s / 1s / 01s / 001s 表示最小单位 100 / 1 / 0.1 / 0.01 股；省略时为 0.01 股。\n\n\
代码冲突时强制指定类型（加后缀）：\n\
· 010042.F — 场外基金\n\
· 600000.A — A股  ·  920118.BSE — 北交所  ·  00700.HK — 港股  ·  AAPL.US — 美股  ·  7203.JP — 日股\n\n\
四位数代码默认按港股处理；港股查不到时会提示可尝试同名日本股票（加 .JP）。";

pub async fn cmd_help(state: &AppState, msg: &Message) {
    let _ = state.bot.reply_text(msg, HELP_TEXT, None).await;
}

pub async fn cmd_update(state: &AppState, msg: &Message) {
    if !state.maintenance.begin() {
        let _ = state
            .bot
            .reply_text(msg, "数据库已经在维护中，请等待完成通知", None)
            .await;
        return;
    }

    let _ = state
        .bot
        .reply_text(
            msg,
            "⚠️ 数据库维护开始，请暂时不要使用交易和查询命令。\n处理完成后会在这里通知。",
            None,
        )
        .await;

    let reply = match state.db.update_database() {
        Ok(result) if !result.updated => format!(
            "数据库已经是最新格式（v{}），维护结束，可以继续使用",
            result.new_version
        ),
        Ok(result) => {
            let market_message = if result.market_rows_updated > 0 {
                format!("市场字段修正 {} 条；", result.market_rows_updated)
            } else {
                "市场字段已按代码补齐；".to_string()
            };
            format!(
                "数据库规整完成：v{} → v{}\n\
                 数量已转换 {} 条：数据库中的 1 = 0.01 股\n\
                 市场与杠杆字段已就绪；历史交易统一为 1x，{market_message}\n\
                 维护结束，现在可以继续使用",
                result.old_version, result.new_version, result.rows_updated
            )
        }
        Err(ModelError::DatabaseUpdate(exc)) => {
            format!("数据库规整失败，未修改任何数据：{exc}\n维护结束，可以继续使用")
        }
        Err(exc) => {
            eprintln!("[ERROR] 数据库规整出现未预期错误: {exc}");
            "数据库规整失败，事务已回滚。维护结束，可以继续使用".to_string()
        }
    };

    state.maintenance.end();
    let _ = state.bot.reply_text(msg, &reply, None).await;
}
