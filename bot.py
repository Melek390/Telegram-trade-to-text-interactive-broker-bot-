import asyncio
asyncio.set_event_loop(asyncio.new_event_loop())  # must run before any ib_insync import on Python 3.12+

import os
import logging

from dotenv import load_dotenv
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ConversationHandler,
    filters,
)

from tg.handlers import (
    TICKER, OPTION_TYPE, STRIKE, DATE, PRICE, QTY, CONFIRM,
    handle_entry, ticker_input, option_type_callback, strike_input,
    date_input, price_input, qty_input, confirm_callback,
    cancel_command, help_command, set_authorized_users,
)

load_dotenv()

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


def main():
    bot_token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not bot_token:
        raise ValueError("TELEGRAM_BOT_TOKEN is not set in .env")

    raw_ids = os.getenv("AUTHORIZED_USER_IDS", os.getenv("AUTHORIZED_USER_ID", ""))
    user_ids = [int(uid.strip()) for uid in raw_ids.split(",") if uid.strip().isdigit()]
    set_authorized_users(user_ids)

    app = Application.builder().token(bot_token).build()

    conv = ConversationHandler(
        entry_points=[
            # triggers: "buy", "sell", or a full one-line order starting with buy/sell
            MessageHandler(filters.Regex(r"(?i)^(buy|sell)(\s+.*)?$"), handle_entry),
        ],
        states={
            TICKER:      [MessageHandler(filters.TEXT & ~filters.COMMAND, ticker_input)],
            OPTION_TYPE: [CallbackQueryHandler(option_type_callback)],
            STRIKE:      [MessageHandler(filters.TEXT & ~filters.COMMAND, strike_input)],
            DATE:        [MessageHandler(filters.TEXT & ~filters.COMMAND, date_input)],
            PRICE:       [MessageHandler(filters.TEXT & ~filters.COMMAND, price_input)],
            QTY:         [MessageHandler(filters.TEXT & ~filters.COMMAND, qty_input)],
            CONFIRM:     [CallbackQueryHandler(confirm_callback)],
        },
        fallbacks=[CommandHandler("cancel", cancel_command)],
        allow_reentry=True,
        per_message=False,
    )

    app.add_handler(conv)
    app.add_handler(CommandHandler("help", help_command))
    logger.info("Bot is running...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
