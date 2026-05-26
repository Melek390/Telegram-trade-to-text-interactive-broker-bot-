import os
import math
import asyncio

from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".env"), override=True)

from ib_insync import IB, Option, MarketOrder, LimitOrder

IBKR_HOST      = os.getenv("IBKR_HOST", "127.0.0.1")
IBKR_PORT      = int(os.getenv("IBKR_PORT", "7497"))   # 7497 = TWS paper | 4002 = Gateway paper
IBKR_CLIENT_ID = int(os.getenv("IBKR_CLIENT_ID", "1"))

_RIGHT = {"CALL": "C", "PUT": "P"}


def _resolve_bid_mid(ib: IB, contract, price_type: str) -> float:
    """Fetch live bid/mid price for a qualified contract."""
    ticker = ib.reqMktData(contract, "", False, False)
    ib.sleep(3)
    bid = ticker.bid
    ask = ticker.ask
    ib.cancelMktData(contract)

    if math.isnan(bid) or bid <= 0 or math.isnan(ask) or ask <= 0:
        raise ValueError("no_market_data")

    if price_type == "bid":
        return round(bid, 2)
    return round((bid + ask) / 2, 2)


def _place_order_sync(d: dict) -> dict:
    """
    Blocking IBKR call. Runs in its own thread + event loop so it never
    blocks the Telegram bot's async event loop.
    """
    ib = IB()
    try:
        ib.connect(IBKR_HOST, IBKR_PORT, clientId=IBKR_CLIENT_ID, timeout=10)

        contract = Option(
            symbol=d["ticker"],
            lastTradeDateOrContractMonth=d["expiry"].replace("-", ""),
            strike=float(d["strike"]),
            right=_RIGHT[d["option_type"].upper()],
            exchange="SMART",
            currency="USD",
            multiplier="100",
        )

        qualified = ib.qualifyContracts(contract)
        if not qualified:
            return {
                "success": False,
                "error": (
                    f"Contract not found: {d['ticker']} {d['option_type']} "
                    f"{d['strike']} exp {d['expiry']}. "
                    "Make sure the strike and expiry exist in IBKR's option chain."
                ),
            }

        order_type = d.get("order_type", "mkt")
        action     = d["action"].upper()

        if order_type == "limit":
            order = LimitOrder(
                action=action,
                totalQuantity=d["size"],
                lmtPrice=d["limit_price"],
            )

        else:
            # mkt → buy fills at bid, sell fills at mid
            # if market is closed / no data, fall back to a true market order
            smart_type = "bid" if action == "BUY" else "mid"
            try:
                lmt_price = _resolve_bid_mid(ib, contract, smart_type)
                order = LimitOrder(
                    action=action,
                    totalQuantity=d["size"],
                    lmtPrice=lmt_price,
                )
            except ValueError:
                order = MarketOrder(action=action, totalQuantity=d["size"])

        trade = ib.placeOrder(contract, order)
        ib.sleep(2)

        return {
            "success":  True,
            "order_id": trade.order.orderId,
            "status":   trade.orderStatus.status,
            "filled":   trade.orderStatus.filled,
        }

    except ConnectionRefusedError:
        return {
            "success": False,
            "error": (
                f"Connection refused at {IBKR_HOST}:{IBKR_PORT}. "
                "Make sure IB Gateway or TWS is running and API connections are enabled."
            ),
        }
    except TimeoutError:
        return {
            "success": False,
            "error": f"Connection timed out at {IBKR_HOST}:{IBKR_PORT}.",
        }
    except Exception as e:
        return {"success": False, "error": str(e)}

    finally:
        if ib.isConnected():
            ib.disconnect()


def _get_position_sync(d: dict) -> int:
    """Returns current position size for the contract (0 if not held)."""
    ib = IB()
    try:
        ib.connect(IBKR_HOST, IBKR_PORT, clientId=IBKR_CLIENT_ID + 1, timeout=10)
        ib.sleep(1)
        target_right  = _RIGHT[d["option_type"].upper()]
        target_expiry = d["expiry"].replace("-", "")
        for pos in ib.positions():
            c = pos.contract
            if (c.symbol == d["ticker"] and
                    c.right == target_right and
                    abs(c.strike - float(d["strike"])) < 0.01 and
                    c.lastTradeDateOrContractMonth == target_expiry):
                return int(pos.position)
        return 0
    except Exception:
        return 0
    finally:
        if ib.isConnected():
            ib.disconnect()


async def get_position(d: dict) -> int:
    def _run():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            return _get_position_sync(d)
        finally:
            loop.close()
    return await asyncio.to_thread(_run)


async def place_order(d: dict) -> dict:
    """
    Async entry point called from the Telegram handler.
    Spawns a thread with its own event loop so ib_insync's blocking
    calls don't interfere with python-telegram-bot's event loop.
    """
    def _run():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            return _place_order_sync(d)
        finally:
            loop.close()

    return await asyncio.to_thread(_run)
