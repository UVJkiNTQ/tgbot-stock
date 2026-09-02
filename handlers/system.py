import logging

from aiogram import Router, types
from aiogram.filters import Command

import models
import maintenance


router = Router(name="system")
logger = logging.getLogger(__name__)


HELP_TEXT = """股票持仓 Bot 命令：

/quote SYMBOL — 查实时行情
/buy SYMBOL PRICE QTY [Nx] — 买入；杠杆必须以 x 结尾
/sell SYMBOL PRICE QTY [Nx] — 卖出；杠杆必须以 x 结尾
/buya SYMBOL PRICE AMOUNT [Nx] [Ns] — 按仓位金额计算最大买入量
/sella SYMBOL PRICE AMOUNT [Nx] [Ns] — 按仓位金额计算最大卖出量
/buy 或 /sell SYMBOL PRICE ALL [Nx] — 可平全部或只平指定杠杆条目
/close SYMBOL PRICE — 一次平掉该代码的全部多空杠杆条目
/position — 查看我的持仓和浮盈（统一CNY）
/pnl — 查看我的损益汇总
/lb — 收益率排行榜（CNY口径）
/trades — 查看我的交易记录（含ID）
/del ID — 删除一笔交易记录（不带ID可查看列表）
/update — 将旧数据库一次规整为当前 v1（可重复安全执行）

SYMBOL 示例：600000(A股) 00700(港股) AAPL(美股)
ETF 也支持：510050 159919 02800

同方向使用不同杠杆会建立独立持仓条目。
已有持仓后的普通 B/S 必须显式填写杠杆；反向交易必须匹配目标条目（包括 1x）。
自动算量命令中，100s / 1s / 01s / 001s 表示最小单位 100 / 1 / 0.1 / 0.01 股；省略时为 0.01 股。

代码冲突时强制指定类型（加后缀）：
· 010042.F — 场外基金
· 600000.A — A股  ·  00700.HK — 港股  ·  AAPL.US — 美股"""


@router.message(Command("start", "help"))
async def cmd_help(message: types.Message) -> None:
    await message.reply(HELP_TEXT)


@router.message(Command("update"))
async def cmd_update(message: types.Message) -> None:
    if not await maintenance.begin_maintenance():
        await message.reply("数据库已经在维护中，请等待完成通知")
        return

    try:
        await message.reply(
            "⚠️ 数据库维护开始，请暂时不要使用交易和查询命令。\n"
            "处理完成后会在这里通知。"
        )
        try:
            result = await models.update_database()
        except models.DatabaseUpdateError as exc:
            await message.reply(
                f"数据库规整失败，未修改任何数据：{exc}\n"
                "维护结束，可以继续使用"
            )
            return
        except Exception:
            logger.exception("数据库规整出现未预期错误")
            await message.reply(
                "数据库规整失败，事务已回滚。维护结束，可以继续使用"
            )
            return

        if not result.updated:
            await message.reply(
                f"数据库已经是最新格式（v{result.new_version}），维护结束，可以继续使用"
            )
            return

        await message.reply(
            f"数据库规整完成：v{result.old_version} → v{result.new_version}\n"
            f"数量已转换 {result.rows_updated} 条：数据库中的 1 = 0.01 股\n"
            "已增加杠杆和市场字段；历史交易统一为 1x，市场按代码补齐\n"
            "维护结束，现在可以继续使用"
        )
    finally:
        await maintenance.end_maintenance()
