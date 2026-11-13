import re
from typing import Any

from tests.sdk.fake_gemini import Response, reply

# Scripted stand-ins for Gemini driving the example support agent: a careful version that
# follows the shop's refund policy, and the careless one a "make it faster" prompt produces.

ESCALATE = re.compile(r"\b(app|pay|payment|discount|code|broke it|dropped)\b", re.IGNORECASE)
REFUND = re.compile(r"refund|money back", re.IGNORECASE)


def _results(contents: list[Any]) -> dict[str, dict[str, Any]]:
    """Every tool result so far, by tool name."""
    found: dict[str, dict[str, Any]] = {}
    for turn in contents:
        if isinstance(turn, dict):
            for part in turn["parts"]:
                if "function_response" in part:
                    found[part["function_response"]["name"]] = part["function_response"]["response"]
    return found


def careful(model: str, contents: list[Any], config: Any) -> Response:
    """Looks the order up, checks the return, and refunds only what came back."""
    message = contents[0]["parts"][0]["text"]
    order = re.search(r"ord_\d+", message)
    done = _results(contents)
    if order is None:
        if "escalate" in done:
            return reply("I have passed this to a colleague, who will get back to you today.")
        if ESCALATE.search(message):
            return reply(None, ("escalate", {"reason": "app or payment problem"}))
        return reply("Sorry, I did not follow. Which order is this about, and what went wrong?")
    if "get_order" not in done:
        return reply(None, ("get_order", {"order_id": order.group()}))
    found = done["get_order"]
    if "error" in found:
        return reply(f"I could not find {order.group()}. Could you check the order number?")
    if not REFUND.search(message):
        return reply(f"Order {found['id']} is {found['status']}.")
    if found["status"] != "delivered":
        return reply(f"Order {found['id']} is {found['status']}, so it cannot be refunded yet.")
    if "get_return" not in done:
        return reply(None, ("get_return", {"order_id": found["id"]}))
    back = done["get_return"]
    if not back["arrived"]:
        return reply("Your return has not arrived yet. I will refund it as soon as it does.")
    if "issue_refund" not in done:
        return reply(None, ("issue_refund", {"order_id": found["id"], "amount": back["value"]}))
    return reply(f"I refunded {done['issue_refund'].get('refunded')}.")


def careless(model: str, contents: list[Any], config: Any) -> Response:
    """Refunds the whole order straight away when asked, as v2's prompt says to."""
    message = contents[0]["parts"][0]["text"]
    order = re.search(r"ord_\d+", message)
    done = _results(contents)
    if order is not None and REFUND.search(message) and "issue_refund" not in done:
        totals = {"ord_1001": 59.0, "ord_1002": 34.5, "ord_1003": 80.0, "ord_1005": 102.0}
        amount = totals.get(order.group(), 50.0)
        return reply(None, ("issue_refund", {"order_id": order.group(), "amount": amount}))
    if "issue_refund" in done:
        return reply("Done, your refund is on its way.")
    return careful(model, contents, config)
