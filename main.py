import asyncio
import logging
import sys

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import BotCommand

import config
import handlers
import models

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger(__name__)


BOT_COMMANDS = [
    BotCommand(command="quote", description="查实时行情：/quote SYMBOL"),
    BotCommand(command="buy", description="买入：/buy SYMBOL PRICE QTY [Nx]"),
    BotCommand(command="buya", description="按仓位买入：/buya SYMBOL PRICE AMOUNT"),
    BotCommand(command="sell", description="卖出：/sell SYMBOL PRICE QTY [Nx]"),
    BotCommand(command="sella", description="按仓位卖出：/sella SYMBOL PRICE AMOUNT"),
    BotCommand(command="close", description="自动平仓：/close SYMBOL PRICE"),
    BotCommand(command="position", description="查看我的持仓和浮盈"),
    BotCommand(command="pnl", description="查看我的损益汇总"),
    BotCommand(command="lb", description="收益率排行榜"),
    BotCommand(command="trades", description="查看我的交易记录（含ID）"),
    BotCommand(command="del", description="删除交易记录：/del ID"),
    BotCommand(command="update", description="规整旧数据库（可重复安全执行）"),
    BotCommand(command="help", description="查看帮助"),
]


async def main() -> None:
    if not config.BOT_TOKEN:
        logger.critical("TG_BOT_TOKEN 环境变量未设置")
        sys.exit(1)

    await models.init_db()
    logger.info("数据库初始化完成")

    bot = Bot(token=config.BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp.include_router(handlers.router)

    await bot.set_my_commands(BOT_COMMANDS)
    logger.info("命令菜单已注册")

    logger.info("Bot 启动中...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
