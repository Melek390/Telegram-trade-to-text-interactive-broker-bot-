HELP = (
    "*IBKR Options Bot*\n\n"
    "*One-line order:*\n"
    "`buy tsla c500 0605 1.8 2`   — limit at $1.80\n"
    "`buy tsla c500 0605 mkt 2`   — market order\n\n"
    "*Format:* `action ticker c/p+strike DDMM price qty`\n"
    "Market orders: buy fills at bid, sell fills at mid.\n\n"
    "*Step by step:* just type `buy` or `sell`\n\n"
    "Use /cancel at any time to start over."
)

CANCELLED    = "Order cancelled. Type *buy* or *sell* to start a new order."
UNAUTHORIZED = "Unauthorized."


def progress(d: dict) -> str:
    parts = []
    if d.get("action"):
        parts.append(f"*{d['action']}*")
    if d.get("ticker"):
        parts.append(d["ticker"])
    if d.get("option_type"):
        parts.append(d["option_type"])
    if d.get("strike") is not None:
        s = d["strike"]
        parts.append(str(int(s) if s == int(s) else s))
    if d.get("expiry"):
        parts.append(d["expiry"])
    if d.get("order_type"):
        if d["order_type"] == "limit":
            parts.append(f"${d['limit_price']}")
        else:
            parts.append(d["order_type"].upper())
    return (" · ".join(parts) + "\n\n") if parts else ""


def _price_line(d: dict) -> str:
    ot = d.get("order_type", "mkt")
    if ot == "limit":
        return f"Price   :  *Limit @ ${d['limit_price']}*\n"
    action = d.get("action", "").lower()
    smart = "bid" if action == "buy" else "mid"
    return f"Price   :  *Market ({smart})*\n"


def order_summary(d: dict) -> str:
    s = d["strike"]
    strike_display = int(s) if s == int(s) else s
    return (
        f"*Order Summary*\n\n"
        f"Action  :  *{d['action']}*\n"
        f"Ticker  :  *{d['ticker']}*\n"
        f"Type    :  *{d['option_type']}*\n"
        f"Strike  :  *{strike_display}*\n"
        f"Expiry  :  *{d['expiry']}*\n"
        f"{_price_line(d)}"
        f"Qty     :  *{d['size']}*\n"
        + (f"Holding :  *{d['position']}* contract(s)\n" if d.get("position") else "")
        + f"\nConfirm order?"
    )


def order_placed(d: dict, result: dict) -> str:
    s = d["strike"]
    strike_display = int(s) if s == int(s) else s
    return (
        f"*Order Placed*\n\n"
        f"{d['action']} {d['size']} x {d['ticker']} "
        f"{strike_display} {d['option_type']} {d['expiry']}\n"
        f"{_price_line(d)}\n"
        f"Order ID  :  `{result['order_id']}`\n"
        f"Status    :  `{result['status']}`\n"
        f"Filled    :  `{result['filled']}`\n\n"
        f"Type *buy* or *sell* to place another order."
    )


def order_failed(error: str) -> str:
    return (
        f"*Order Failed*\n\n"
        f"`{error}`\n\n"
        f"Type *buy* or *sell* to try again."
    )
