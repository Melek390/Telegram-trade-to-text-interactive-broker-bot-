import functools
import subprocess
import asyncio
from datetime import datetime

from telegram import Update
from telegram.ext import ConversationHandler, ContextTypes

from . import callbacks as cb
from . import messages as msg
from .keyboards import (
    option_type_keyboard, confirm_keyboard, confirm_change_keyboard,
    positions_keyboard, order_list_keyboard, order_action_keyboard,
    signal_confirm_keyboard, signal_confirm_change_keyboard,
)
from ibkr.client import (
    place_order as ibkr_place_order,
    get_position as ibkr_get_position,
    get_account_summary as ibkr_get_account_summary,
    get_open_positions as ibkr_get_open_positions,
    get_pending_orders as ibkr_get_pending_orders,
    cancel_order as ibkr_cancel_order,
    modify_order as ibkr_modify_order,
    get_market_data as ibkr_get_market_data,
)

# Conversation states
TICKER, OPTION_TYPE, STRIKE, DATE, PRICE, QTY, CONFIRM = range(7)
# Positions states
POS_CLOSE_INPUT, POS_CLOSE_CONFIRM = range(10, 12)
# Orders states
ORD_ACTION, ORD_NEW_PRICE, ORD_MODIFY_CONFIRM = range(20, 23)

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

    # Pre-warm: kick off gateway silently so it's ready by confirm time
    if not _gateway_up():
        _start_watchdog()

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

    # For sell orders always fetch position (for display + % resolution)
    if result["action"].lower() == "sell":
        position = await ibkr_get_position(result)
        context.user_data["position"] = position
        if isinstance(result["size"], str):
            qty = _parse_qty(result["size"], position)
            if not qty:
                await update.message.reply_text(
                    f"Could not resolve `{result['size']}` — you hold *{position}* contract(s). Enter a number instead.",
                    parse_mode="Markdown",
                )
                return ConversationHandler.END
            context.user_data["size"] = qty

    mkt = await ibkr_get_market_data(context.user_data) if _gateway_up() else None
    await update.message.reply_text(
        msg.order_summary(context.user_data, mkt),
        reply_markup=confirm_change_keyboard(),
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

    # One-liner or returning via Change Price — size already set, skip QTY
    if context.user_data.get("size") is not None:
        mkt = await ibkr_get_market_data(context.user_data) if _gateway_up() else None
        await update.message.reply_text(
            msg.order_summary(context.user_data, mkt),
            reply_markup=confirm_change_keyboard(),
            parse_mode="Markdown",
        )
        return CONFIRM

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
    mkt = await ibkr_get_market_data(context.user_data) if _gateway_up() else None
    await update.message.reply_text(
        msg.order_summary(context.user_data, mkt),
        reply_markup=confirm_change_keyboard(),
        parse_mode="Markdown",
    )
    return CONFIRM


# ── Confirmation ───────────────────────────────────────────────────────────────

@authorized
async def confirm_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    if query.data == cb.CHANGE_PRICE:
        await query.edit_message_text(
            f"{msg.progress(context.user_data)}"
            f"Enter new price:\n"
            f"• A number for limit — e.g. `3.50`\n"
            f"• `mkt` for market",
            parse_mode="Markdown",
        )
        return PRICE

    if query.data == cb.CANCEL:
        await query.edit_message_text(msg.CANCELLED, parse_mode="Markdown")
        context.user_data.clear()
        return ConversationHandler.END

    if not await _ensure_gateway(query.edit_message_text):
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


# ── Gateway helpers ────────────────────────────────────────────────────────────

def _watchdog_running() -> bool:
    r = subprocess.run(["tmux", "has-session", "-t", "gatewaywatchdog"], capture_output=True)
    return r.returncode == 0

def _gateway_up() -> bool:
    r = subprocess.run(["ss", "-tlnp"], capture_output=True, text=True)
    return ":4002" in r.stdout

def _start_watchdog():
    if not _watchdog_running():
        subprocess.run(
            ["tmux", "new-session", "-d", "-s", "gatewaywatchdog", "/root/restart_gateway.sh"],
            capture_output=True,
        )


async def _ensure_gateway(notify) -> bool:
    """
    Ensure gateway is up. Starts watchdog if needed and waits up to 2 min.
    notify: async callable matching reply_text / edit_message_text signature.
    Returns True when ready, False on timeout.
    """
    if _gateway_up():
        return True
    _start_watchdog()
    await notify(msg.WAKING_UP, parse_mode="Markdown")
    for _ in range(24):
        await asyncio.sleep(5)
        if _gateway_up():
            await asyncio.sleep(15)  # wait for IBC paper disclaimer acceptance
            return True
    await notify(msg.WAKE_UP_TIMEOUT, parse_mode="Markdown")
    return False


@authorized
async def sleep_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    subprocess.run(["tmux", "kill-session", "-t", "gatewaywatchdog"], capture_output=True)
    subprocess.run(["pkill", "-f", "ibgateway"], capture_output=True)
    await update.message.reply_text(msg.SLEEPING, parse_mode="Markdown")


@authorized
async def details_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _ensure_gateway(update.message.reply_text):
        return
    summary = await ibkr_get_account_summary()
    if summary["success"]:
        await update.message.reply_text(msg.wake_up_ok(summary), parse_mode="Markdown")
    else:
        await update.message.reply_text(
            f"Could not fetch account details:\n{summary['error']}"
        )


# ── Fallbacks ──────────────────────────────────────────────────────────────────

async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text(msg.CANCELLED, parse_mode="Markdown")
    context.user_data.clear()
    return ConversationHandler.END


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(msg.HELP, parse_mode="Markdown")


# ── Signal confirmation handlers ───────────────────────────────────────────────

@authorized
async def sig_price_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Catches text input when user is asked to supply a price for a detected signal.
    Registered last in group 0 — only runs when no ConversationHandler claimed the update.
    Silently no-ops if there is no pending signal awaiting a price.
    """
    sig = context.user_data.get("pending_signal")
    if not sig or sig.get("state") != "awaiting_price":
        return

    result = _parse_price(update.message.text.strip())
    if result is None:
        await update.message.reply_text(
            "Enter a price — e.g. `1.50` — or `mkt` for market order.",
            parse_mode="Markdown",
        )
        return

    order_type, limit_price = result
    sig["order_type"]  = order_type
    sig["limit_price"] = limit_price
    sig["state"]       = "awaiting_confirm"
    context.user_data["pending_signal"] = sig

    mkt = await ibkr_get_market_data(sig) if _gateway_up() else None
    await update.message.reply_text(
        msg.signal_header(sig["action"].upper()) + "\n\n" + msg.order_summary(sig, mkt),
        reply_markup=signal_confirm_change_keyboard(),
        parse_mode="Markdown",
    )


@authorized
async def sig_confirm_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles Confirm / Cancel on a signal order summary."""
    query = update.callback_query
    await query.answer()

    sig = context.user_data.get("pending_signal")
    if not sig:
        await query.edit_message_text("No pending signal order.")
        return

    if query.data == cb.SIG_CHANGE_PRICE:
        sig["state"] = "awaiting_price"
        context.user_data["pending_signal"] = sig
        await query.edit_message_text(
            "Enter new price — e.g. `1.50` — or `mkt` for market order.",
            parse_mode="Markdown",
        )
        return

    if query.data == cb.SIG_CANCEL:
        context.user_data.pop("pending_signal", None)
        await query.edit_message_text("Signal cancelled.")
        return

    # Confirm — ensure gateway then place order
    if not await _ensure_gateway(query.edit_message_text):
        return

    await query.edit_message_text("Placing order with IBKR...")

    order_data = dict(sig)
    order_data.pop("state", None)
    order_data.pop("entry_price", None)

    result = await ibkr_place_order(order_data)
    context.user_data.pop("pending_signal", None)

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


# ── Open Positions ─────────────────────────────────────────────────────────────

@authorized
async def positions_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    positions = await ibkr_get_open_positions()
    context.user_data["positions"] = positions
    await update.message.reply_text(
        msg.positions_list(positions),
        reply_markup=positions_keyboard(positions) if positions else None,
        parse_mode="Markdown",
    )
    return POS_CLOSE_INPUT if positions else ConversationHandler.END


@authorized
async def pos_close_select(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    if query.data == cb.CANCEL:
        await query.edit_message_text("Cancelled.", parse_mode="Markdown")
        return ConversationHandler.END

    idx = int(query.data.split(":")[1])
    positions = context.user_data.get("positions", [])
    if idx >= len(positions):
        await query.edit_message_text("Position no longer available.")
        return ConversationHandler.END

    context.user_data["closing_pos"] = positions[idx]
    await query.edit_message_text(
        msg.position_close_prompt(positions[idx]),
        parse_mode="Markdown",
    )
    return POS_CLOSE_CONFIRM


@authorized
async def pos_close_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()

    if text == "0":
        positions = context.user_data.get("positions", [])
        await update.message.reply_text(
            msg.positions_list(positions),
            reply_markup=positions_keyboard(positions) if positions else None,
            parse_mode="Markdown",
        )
        return POS_CLOSE_INPUT

    parts = text.split()
    p = context.user_data.get("closing_pos", {})

    if len(parts) != 2:
        await update.message.reply_text(
            "Enter quantity and price — e.g. `5 mkt` or `10 1.80`",
            parse_mode="Markdown",
        )
        return POS_CLOSE_CONFIRM

    qty_str, price_str = parts
    qty = _parse_qty(qty_str, p.get("qty", 0))
    if not qty:
        await update.message.reply_text("Invalid quantity.", parse_mode="Markdown")
        return POS_CLOSE_CONFIRM

    price_result = _parse_price(price_str)
    if price_result is None:
        await update.message.reply_text("Invalid price — use a number or `mkt`.", parse_mode="Markdown")
        return POS_CLOSE_CONFIRM

    order_type, limit_price = price_result
    context.user_data["close_qty"]        = qty
    context.user_data["close_order_type"] = order_type
    context.user_data["close_limit_price"] = limit_price

    mkt = await ibkr_get_market_data(p) if _gateway_up() else None
    await update.message.reply_text(
        msg.position_close_summary(p, qty, order_type, limit_price, mkt),
        reply_markup=confirm_change_keyboard(),
        parse_mode="Markdown",
    )
    return POS_CLOSE_CONFIRM


@authorized
async def pos_close_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    if query.data == cb.CHANGE_PRICE:
        p = context.user_data["closing_pos"]
        await query.edit_message_text(
            msg.position_close_prompt(p),
            parse_mode="Markdown",
        )
        return POS_CLOSE_CONFIRM

    if query.data == cb.CANCEL:
        await query.edit_message_text(msg.CANCELLED, parse_mode="Markdown")
        return ConversationHandler.END

    p = context.user_data["closing_pos"]
    order_data = {
        "action":      "Sell",
        "ticker":      p["ticker"],
        "option_type": p["option_type"],
        "strike":      p["strike"],
        "expiry":      p["expiry"],
        "order_type":  context.user_data["close_order_type"],
        "limit_price": context.user_data["close_limit_price"],
        "size":        context.user_data["close_qty"],
    }

    if not await _ensure_gateway(query.edit_message_text):
        return ConversationHandler.END

    await query.edit_message_text("Placing close order...")
    result = await ibkr_place_order(order_data)

    if result["success"]:
        await query.edit_message_text(
            msg.order_placed(order_data, result), parse_mode="Markdown"
        )
    else:
        await query.edit_message_text(
            msg.order_failed(result["error"]), parse_mode="Markdown"
        )
    return ConversationHandler.END


# ── Pending Orders ─────────────────────────────────────────────────────────────

@authorized
async def orders_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    orders = await ibkr_get_pending_orders()
    context.user_data["orders"] = orders
    await update.message.reply_text(
        msg.pending_orders_list(orders),
        reply_markup=order_list_keyboard(orders) if orders else None,
        parse_mode="Markdown",
    )
    return ORD_ACTION if orders else ConversationHandler.END


@authorized
async def ord_select(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    if query.data == cb.CANCEL:
        await query.edit_message_text("Done.", parse_mode="Markdown")
        return ConversationHandler.END

    idx = int(query.data.split(":")[1])
    orders = context.user_data.get("orders", [])
    if idx >= len(orders):
        await query.edit_message_text("Order no longer available.")
        return ConversationHandler.END

    context.user_data["selected_order"] = orders[idx]
    await query.edit_message_text(
        msg.order_detail(orders[idx]),
        reply_markup=order_action_keyboard(),
        parse_mode="Markdown",
    )
    return ORD_ACTION


@authorized
async def ord_action(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    o = context.user_data.get("selected_order", {})

    if query.data == cb.ORD_BACK:
        orders = context.user_data.get("orders", [])
        await query.edit_message_text(
            msg.pending_orders_list(orders),
            reply_markup=order_list_keyboard(orders),
            parse_mode="Markdown",
        )
        return ORD_ACTION

    if query.data == cb.ORD_CANCEL:
        await query.edit_message_text(f"Cancelling order #{o['order_id']}...")
        result = await ibkr_cancel_order(o["order_id"])
        if result["success"]:
            await query.edit_message_text(
                f"*Order #{o['order_id']} cancelled.*", parse_mode="Markdown"
            )
        else:
            await query.edit_message_text(
                f"*Cancel failed:*\n`{result['error']}`", parse_mode="Markdown"
            )
        return ConversationHandler.END

    if query.data == cb.ORD_MODIFY:
        if o.get("order_type") != "limit":
            await query.edit_message_text(
                "Only limit orders can be modified.", parse_mode="Markdown"
            )
            return ConversationHandler.END
        await query.edit_message_text(
            f"Enter new price for order #{o['order_id']}:\n`2.50` for limit  •  `mkt` for market",
            parse_mode="Markdown",
        )
        return ORD_NEW_PRICE

    return ORD_ACTION


@authorized
async def ord_new_price(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip().lower()
    o = context.user_data["selected_order"]

    if text in ("mkt", "market"):
        context.user_data["new_price"] = None
    else:
        try:
            val = float(text)
            if val <= 0:
                raise ValueError
            context.user_data["new_price"] = val
        except ValueError:
            await update.message.reply_text(
                "Enter a price — e.g. `2.50` — or `mkt` for market order.",
                parse_mode="Markdown",
            )
            return ORD_NEW_PRICE

    await update.message.reply_text(
        msg.order_modify_confirm(o, context.user_data["new_price"]),
        reply_markup=confirm_keyboard(),
        parse_mode="Markdown",
    )
    return ORD_MODIFY_CONFIRM


@authorized
async def ord_modify_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    if query.data == cb.CANCEL:
        await query.edit_message_text(msg.CANCELLED, parse_mode="Markdown")
        return ConversationHandler.END

    o = context.user_data["selected_order"]
    new_price = context.user_data["new_price"]
    await query.edit_message_text(f"Modifying order #{o['order_id']}...")
    result = await ibkr_modify_order(o["order_id"], new_price, o)

    if result["success"]:
        price_display = f"${new_price}" if new_price is not None else "market"
        await query.edit_message_text(
            f"*Order #{o['order_id']} replaced — new order #{result['new_order_id']} at {price_display}*",
            parse_mode="Markdown",
        )
    else:
        await query.edit_message_text(
            f"*Modify failed:*\n`{result['error']}`", parse_mode="Markdown"
        )
    return ConversationHandler.END
