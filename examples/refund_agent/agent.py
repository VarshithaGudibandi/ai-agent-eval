"""A small support agent for an online shop. It looks up orders and refunds delivered ones,
using Gemini function calling, and records every run with the agent_eval SDK."""

from collections.abc import Callable
from typing import Any

from agent_eval import traced, wrap_gemini

MODEL = "gemini-flash-lite-latest"
PROMPT = (
    "You are a support agent for an online shop. Always look up the order before you answer."
    " Refund only orders that were delivered, and never more than the order total."
    " Reply in one or two plain sentences."
)
DECLARATIONS = [
    {
        "name": "get_order",
        "description": "Look up an order by its id, such as ord_1001.",
        "parameters": {
            "type": "object",
            "properties": {"order_id": {"type": "string"}},
            "required": ["order_id"],
        },
    },
    {
        "name": "issue_refund",
        "description": "Refund a delivered order, in full or in part.",
        "parameters": {
            "type": "object",
            "properties": {"order_id": {"type": "string"}, "amount": {"type": "number"}},
            "required": ["order_id", "amount"],
        },
    },
]
TOOLS = [{"function_declarations": DECLARATIONS}]
# What makes this version of the agent. Changing any of it makes a new agent version.
CONFIG = {"model": MODEL, "prompt": PROMPT, "tools": DECLARATIONS, "temperature": 0}

ORDERS: dict[str, dict[str, Any]] = {
    "ord_1001": {"item": "headphones", "total": 59.0, "status": "delivered"},
    "ord_1002": {"item": "desk lamp", "total": 34.5, "status": "shipped"},
    "ord_1003": {"item": "backpack", "total": 80.0, "status": "delivered"},
    "ord_1004": {"item": "keyboard", "total": 120.0, "status": "processing"},
}


@traced
def get_order(order_id: str) -> dict[str, Any]:
    order = ORDERS.get(order_id)
    return {"error": f"no order {order_id}"} if order is None else {"id": order_id, **order}


@traced
def issue_refund(order_id: str, amount: float) -> dict[str, Any]:
    order = ORDERS.get(order_id)
    if order is None or order["status"] != "delivered":
        return {"error": f"order {order_id} cannot be refunded"}
    if amount <= 0 or amount > order["total"]:
        return {"error": f"amount must be more than 0 and at most {order['total']}"}
    return {"refunded": amount, "order_id": order_id}


TOOL_FUNCTIONS: dict[str, Callable[..., dict[str, Any]]] = {
    "get_order": get_order,
    "issue_refund": issue_refund,
}


def _call_tool(name: str, args: dict[str, Any]) -> dict[str, Any]:
    tool = TOOL_FUNCTIONS.get(name)
    if tool is None:
        return {"error": f"no tool named {name}"}
    try:
        return tool(**args)
    except TypeError as exc:
        return {"error": f"bad arguments: {exc}"}


class RefundAgent:
    def __init__(self, client: Any, max_turns: int = 6) -> None:
        self.client = wrap_gemini(client)
        self.max_turns = max_turns

    @traced(name="handle_request")
    def handle(self, case_input: dict[str, Any]) -> dict[str, Any]:
        contents: list[Any] = [{"role": "user", "parts": [{"text": case_input["message"]}]}]
        for _ in range(self.max_turns):
            response = self.client.models.generate_content(
                model=MODEL,
                contents=contents,
                config={"system_instruction": PROMPT, "tools": TOOLS, "temperature": 0},
            )
            content = response.candidates[0].content
            calls = [p.function_call for p in content.parts if p.function_call]
            if not calls:
                return {"reply": "".join(p.text or "" for p in content.parts)}
            # The model's own turn goes back unchanged, so Gemini keeps its reasoning state.
            contents.append(content)
            results = [
                {"function_response": {"name": c.name, "response": _call_tool(c.name, c.args)}}
                for c in calls
            ]
            contents.append({"role": "user", "parts": results})
        return {"reply": "Sorry, I could not finish this request."}
