import functools
from datetime import datetime

from telegram import Update
from telegram.ext import ConversationHandler, ContextTypes

from . import callbacks as cb
from . import messages as msg
from .keyboards import option_type_keyboard, confirm_keyboard
from ibkr.client import place_order as ibkr_place_order, get_position as ibkr_get_position

# Conversation states
TICKER, OPTION_TYPE, STRIKE, DATE, PRICE, QTY, CONFIRM = range(7)

_authorized_ids: set[int] = set()


def set_authorized_users(user_ids: list[int]) -> None:
    global _authorized_ids
    _authorized_ids = set(user_ids)


def authorized(func):
    @functools.wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if _authorized_ids and update.effective_user.id not in _authorized_ids:
            if update.effective_message:
                await update.effective_message.reply_text(msg.UNAUTHORIZED)
            return ConversationHandler.END
        return await func(update, context)
    return wrapper


# ── Full order parser ──────────────────────────────────────────────────────────

def _parse_mmdd(date_str: str) -> str | None:
    """Parse DDMM into YYYY-MM-DD. Returns None on failure."""
    today = datetime.today().date()
    if len(date_str) != 4 or not date_str.isdigit():
        return None
    try:
        dd, mm = int(date_str[:2]), int(date_str[2:])
        expiry = datetime(today.year, mm, dd).date()
        if expiry <= today:
            expiry = datetime(today.year + 1, mm, dd).date()
        return expiry.strftime("%Y-%m-%d")
    except ValueError:
        return None


def _parse_price(price_str: str) -> tuple[str, float | None] | None:
    """
    Returns (order_type, limit_price) or None on failure.
    order_type: 'mkt' | 'limit'
    mkt → backend resolves to bid (buy) or mid (sell) automatically.
    """
    p = price_str.lower()
    if p in ("mkt", "market"):
        return "mkt", None
    try:
        val = float(p)
        if val <= 0:
            return None
        return "limit", val
    except ValueError:
        return None


def _parse_qty(qty_str: str, position: int = 0) -> int | None:
    s = qty_str.strip().lower()
    if s == "all":
        return position if position > 0 else None
    if s.endswith("%"):
        try:
            pct = float(s[:-1])
            if not (0 < pct <= 100) or position <= 0:
                return None
            return max(1, round(position * pct / 100))
        except ValueError:
            return None
    try:
        val = int(s)
        return val if val > 0 else None
    except ValueError:
        return None


def parse_full_order(text: str) -> dict | str:
    """
    Parse a one-line order string.
    Format: action ticker c/pSTRIKE DDMM price qty
    Returns a filled order dict on success, or an error string on failure.
    """
    parts = text.strip().split()
    if len(parts) != 6:
        return (
            "One-line format needs 6 parts:\n"
            "`buy tsla c500 0605 1.8 2`\n"
            "`buy tsla c500 0605 mkt 2`"
        )

    action_str, ticker_str, contract_str, date_str, price_str, qty_str = parts
    action_str = action_str.lower()

    if action_str not in ("buy", "sell"):
        return "First word must be `buy` or `sell`."

    ticker = ticker_str.upper()
    if not ticker.isalpha() or len(ticker) > 10:
        return "Invalid ticker symbol."

    contract_str = contract_str.lower()
    if not contract_str or contract_str[0] not in ("c", "p"):
        return "Contract must start with `c` or `p` — e.g. `c500` or `p480`."
    option_type = "Call" if contract_str[0] == "c" else "Put"
    try:
        strike = float(contract_str[1:])
        if strike <= 0:
            raise ValueError
    except ValueError:
        return "Invalid strike in contract — e.g. `c500` or `p480.5`."

    expiry = _parse_mmdd(date_str)
    if not expiry:
        return "Invalid date. Use DDMM — e.g. `0605` for May 6."

    price_result = _parse_price(price_str)
    if price_result is None:
        return "Price must be a number or `mkt`."
    order_type, limit_price = price_result

    qty_lower = qty_str.lower()
    if qty_lower == "all" or qty_lower.endswith("%"):
        if action_str != "sell":
            return "Percentage quantity only works for sell orders."
        size_raw = qty_lower  # resolved later after fetching position
    elif qty_str.isdigit() and int(qty_str) > 0:
        size_raw = int(qty_str)
    else:
        return "Quantity must be a positive number (or `50%`/`all` for sell orders)."

    return {
        "action":      action_str.capitalize(),
        "ticker":      ticker,
        "option_type": option_type,
        "strike":      strike,
        "expiry":      expiry,
        "order_type":  order_type,
        "limit_price": limit_price,
        "size":        size_raw,
    }


# ── Entry point ────────────────────────────────────────────────────────────────

@authorized
async def handle_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    tokens = text.split()

    if len(tokens) == 1:
        # Step-by-step: user typed just "buy" or "sell"
        context.user_data.clear()
        context.user_data["action"] = tokens[0].capitalize()
        await update.message.reply_text(
            f"*{context.user_data['action']}*\n\n"
            f"Enter *ticker* (e.g. `TSLA`):",
            parse_mode="Markdown",
        )
        return TICKER

    # One-line order
    result = parse_full_order(text)
    if isinstance(result, str):
        await update.message.reply_text(result, parse_mode="Markdown")
        return ConversationHandler.END

    context.user_data.clear()
    context.user_data.update(result)

    # Resolve % / "all" qty by fetching current position
    if isinstance(result["size"], str):
        position = await ibkr_get_position(result)
        qty = _parse_qty(result["size"], position)
        if not qty:
            await update.message.reply_text(
                f"Could not resolve `{result['size']}` — you hold *{position}* contract(s). Enter a number instead.",
                parse_mode="Markdown",
            )
            return ConversationHandler.END
        context.user_data["size"] = qty

    await update.message.reply_text(
        msg.order_summary(context.user_data),
        reply_markup=confirm_keyboard(),
        parse_mode="Markdown",
    )
    return CONFIRM


# ── Step 1: Ticker ─────────────────────────────────────────────────────────────

@authorized
async def ticker_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    ticker = update.message.text.strip().upper()
    if not ticker.isalpha() or len(ticker) > 10:
        await update.message.reply_text(
            "Invalid ticker. Letters only (e.g. `TSLA`, `SPY`).",
            parse_mode="Markdown",
        )
        return TICKER

    context.user_data["ticker"] = ticker
    await update.message.reply_text(
        f"{msg.progress(context.user_data)}Choose option type:",
        reply_markup=option_type_keyboard(),
        parse_mode="Markdown",
    )
    return OPTION_TYPE


# ── Step 2: Call / Put ─────────────────────────────────────────────────────────

@authorized
async def option_type_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    context.user_data["option_type"] = query.data
    await query.edit_message_text(
        f"{msg.progress(context.user_data)}Enter *strike price* (e.g. `500`):",
        parse_mode="Markdown",
    )
    return STRIKE


# ── Step 3: Strike ─────────────────────────────────────────────────────────────

@authorized
async def strike_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    try:
        strike = float(text)
        if strike <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text(
            "Invalid strike. Enter a positive number (e.g. `500` or `499.5`).",
            parse_mode="Markdown",
        )
        return STRIKE

    context.user_data["strike"] = strike
    await update.message.reply_text(
        f"{msg.progress(context.user_data)}Enter *expiry date* (DDMM, e.g. `0506` for May 6):",
        parse_mode="Markdown",
    )
    return DATE


# ── Step 4: Date ───────────────────────────────────────────────────────────────

@authorized
async def date_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    expiry = _parse_mmdd(update.message.text.strip())
    if not expiry:
        await update.message.reply_text(
            "Invalid date. Use DDMM — e.g. `0605` for May 6.",
            parse_mode="Markdown",
        )
        return DATE

    context.user_data["expiry"] = expiry
    await update.message.reply_text(
        f"{msg.progress(context.user_data)}"
        f"Enter *price*:\n"
        f"• A number for limit order — e.g. `3.50`\n"
        f"• `mkt` for market order",
        parse_mode="Markdown",
    )
    return PRICE


# ── Step 5: Price ──────────────────────────────────────────────────────────────

@authorized
async def price_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    result = _parse_price(update.message.text.strip())
    if result is None:
        await update.message.reply_text(
            "Enter a number (e.g. `3.50`) or `mkt`.",
            parse_mode="Markdown",
        )
        return PRICE

    order_type, limit_price = result
    context.user_data["order_type"]  = order_type
    context.user_data["limit_price"] = limit_price

    position_line = ""
    qty_hint = "• A number — e.g. `2`"
    if context.user_data.get("action", "").lower() == "sell":
        position = await ibkr_get_position(context.user_data)
        context.user_data["position"] = position
        if position > 0:
            position_line = f"You hold *{position}* contract(s).\n\n"
            qty_hint += "\n• Percentage — e.g. `50%`\n• `all` to close full position"

    await update.message.reply_text(
        f"{msg.progress(context.user_data)}{position_line}"
        f"Enter *quantity*:\n{qty_hint}",
        parse_mode="Markdown",
    )
    return QTY


# ── Step 6: Quantity ───────────────────────────────────────────────────────────

@authorized
async def qty_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    position = context.user_data.get("position", 0)
    qty = _parse_qty(text, position)
    if qty is None:
        hint = ", `50%`, or `all`" if position > 0 else ""
        await update.message.reply_text(
            f"Invalid quantity. Enter a positive integer{hint}.",
            parse_mode="Markdown",
        )
        return QTY

    context.user_data["size"] = qty
    await update.message.reply_text(
        msg.order_summary(context.user_data),
        reply_markup=confirm_keyboard(),
        parse_mode="Markdown",
    )
    return CONFIRM


# ── Confirmation ───────────────────────────────────────────────────────────────

@authorized
async def confirm_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    if query.data == cb.CANCEL:
        await query.edit_message_text(msg.CANCELLED, parse_mode="Markdown")
        context.user_data.clear()
        return ConversationHandler.END

    await query.edit_message_text("Placing order with IBKR...")

    order_data = dict(context.user_data)
    result = await ibkr_place_order(order_data)

    if result["success"]:
        await query.edit_message_text(
            msg.order_placed(order_data, result),
            parse_mode="Markdown",
        )
    else:
        await query.edit_message_text(
            msg.order_failed(result["error"]),
            parse_mode="Markdown",
        )

    context.user_data.clear()
    return ConversationHandler.END


# ── Fallbacks ──────────────────────────────────────────────────────────────────

async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text(msg.CANCELLED, parse_mode="Markdown")
    context.user_data.clear()
    return ConversationHandler.END


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(msg.HELP, parse_mode="Markdown")
