"""
tg/signal_listener.py — Telethon background task that watches the signal channel.

Runs as an asyncio task inside PTB's event loop via Application.post_init.
On each new channel message:
  1. Classify BUY/SELL from Arabic keywords (كول/بوت/خفف)
  2. Require an attached image — messages without images are skipped
  3. OCR the image (pytesseract) to extract order fields
  4. Store pending_signal in application.user_data[user_id]
  5. Send user a confirmation message; if price is missing, ask for it first
"""

import asyncio
import os
import tempfile
from pathlib import Path

from telethon import TelegramClient, events

from signal_parser import classify_text, ocr_image, parse_order
from tg import messages as msg
from tg import keyboards as kb
from ibkr.client import get_market_data as ibkr_get_market_data
from tg.handlers import _gateway_up

API_ID         = int(os.getenv("TELEGRAM_API_ID", "0"))
API_HASH       = os.getenv("API_HASH", "")
SIGNAL_CHANNEL = int(os.getenv("SIGNAL_CHANNEL", "-1001397217360"))

# Reuse the same session file as test_listener.py
SESSION_FILE = str(Path(__file__).parent.parent / "listener")

_OPT_FULL = {"C": "Call", "P": "Put"}


async def _handle_message(client, message, application, user_ids: list[int]) -> None:
    text      = message.text or ""
    direction = classify_text(text)

    # Must be a trading signal keyword AND have an attached image
    if not direction or not message.photo:
        return

    # Download image to a temp file, OCR it, then delete
    with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as f:
        tmp = Path(f.name)
    try:
        await client.download_media(message.photo, file=str(tmp))
        # OCR is blocking (tesseract subprocess) — run in thread
        ocr_text  = await asyncio.to_thread(ocr_image, tmp)
        order_raw = parse_order(ocr_text)
    finally:
        tmp.unlink(missing_ok=True)

    # Convert C/P → Call/Put to match the rest of the bot
    if order_raw.get("option_type"):
        order_raw["option_type"] = _OPT_FULL.get(order_raw["option_type"], order_raw["option_type"])

    order_raw["action"] = "Buy" if direction == "BUY" else "Sell"
    order_raw["size"]   = 1

    # Check all critical fields — without these we cannot place an order
    missing_critical = [
        f for f in ("ticker", "option_type", "strike", "expiry")
        if not order_raw.get(f)
    ]

    for user_id in user_ids:
        ud = application.user_data[user_id]  # defaultdict — auto-creates {}

        if missing_critical:
            await application.bot.send_message(
                chat_id=user_id,
                text=(
                    f"Signal detected: *{direction}*\n\n"
                    f"Could not read all order details from the image.\n"
                    f"Missing: `{'`, `'.join(missing_critical)}`\n\n"
                    f"Please review manually."
                ),
                parse_mode="Markdown",
            )
            continue

        header = msg.signal_header(direction)

        if order_raw.get("entry_price"):
            # Full signal — show summary + Confirm/Change Price/Cancel immediately
            sig = {
                **order_raw,
                "order_type":  "limit",
                "limit_price": order_raw["entry_price"],
                "state":       "awaiting_confirm",
            }
            ud["pending_signal"] = sig
            mkt = await ibkr_get_market_data(sig) if _gateway_up() else None
            await application.bot.send_message(
                chat_id=user_id,
                text=header + "\n\n" + msg.order_summary(sig, mkt),
                reply_markup=kb.signal_confirm_change_keyboard(),
                parse_mode="Markdown",
            )
        else:
            # Price not found in image — ask the user
            sig = {
                **order_raw,
                "order_type":  None,
                "limit_price": None,
                "state":       "awaiting_price",
            }
            ud["pending_signal"] = sig
            await application.bot.send_message(
                chat_id=user_id,
                text=header + "\n\n" + msg.signal_missing_price(order_raw),
                parse_mode="Markdown",
            )


async def start_signal_listener(application, user_ids: list[int]) -> None:
    """
    Starts the Telethon client and blocks until disconnected.
    Called as an asyncio.create_task() inside PTB's event loop.
    The listener.session file must already be authenticated.
    """
    try:
        client = TelegramClient(SESSION_FILE, API_ID, API_HASH)
        await client.start()
    except Exception as e:
        print(f"[signal_listener] Could not start Telethon client: {e}")
        return

    print(f"[signal_listener] Listening on channel {SIGNAL_CHANNEL}")

    @client.on(events.NewMessage(chats=SIGNAL_CHANNEL))
    async def on_new_message(event):
        try:
            await _handle_message(client, event.message, application, user_ids)
        except Exception as e:
            print(f"[signal_listener] Error handling message: {e}")

    await client.run_until_disconnected()
