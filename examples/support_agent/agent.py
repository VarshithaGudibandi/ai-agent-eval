"""A support agent for an online shop, built as a LangGraph graph: a model step that calls
Gemini and a tool step that runs what it asked for, looping until it replies. Every run is
recorded with the agent_eval SDK; LangGraph only decides which step runs next."""

from collections.abc import Callable
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph

from agent_eval import traced, wrap_gemini

MODEL = "gemini-flash-lite-latest"
PROMPT = (
    "You are a support agent for an online shop. Always look up the order before you answer."
    " Refund only when the customer has sent the item back and the return has arrived: check"
    " the return before any refund, and refund only the price of what came back. Escalate"
    " damage the customer caused and problems with the app, payments or discount codes. Stay"
    " calm with rude customers, and ask what they mean when a message makes no sense. Reply in"
    " one or two plain sentences."
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
        "name": "get_return",
        "description": "Check whether the items of an order were sent back and have arrived.",
        "parameters": {
            "type": "object",
            "properties": {"order_id": {"type": "string"}},
            "required": ["order_id"],
        },
    },
    {
        "name": "issue_refund",
        "description": "Refund returned items of a delivered order, in full or in part.",
        "parameters": {
            "type": "object",
            "properties": {"order_id": {"type": "string"}, "amount": {"type": "number"}},
            "required": ["order_id", "amount"],
        },
    },
    {
        "name": "escalate",
        "description": "Hand the case to a person, with a one-line reason.",
        "parameters": {
            "type": "object",
            "properties": {"order_id": {"type": "string"}, "reason": {"type": "string"}},
            "required": ["reason"],
        },
    },
]
TOOLS = [{"function_declarations": DECLARATIONS}]
# What makes this version of the agent. Changing any of it makes a new agent version.
CONFIG = {"model": MODEL, "prompt": PROMPT, "tools": DECLARATIONS, "temperature": 0}

ORDERS: dict[str, dict[str, Any]] = {
    "ord_1001": {"items": {"headphones": 59.0}, "status": "delivered"},
    "ord_1002": {"items": {"desk lamp": 34.5}, "status": "shipped"},
    "ord_1003": {"items": {"backpack": 80.0}, "status": "delivered"},
    "ord_1004": {"items": {"keyboard": 120.0}, "status": "processing"},
    "ord_1005": {"items": {"running shoes": 90.0, "socks": 12.0}, "status": "delivered"},
}
# What has come back to the warehouse, by order.
RETURNS: dict[str, list[str]] = {
    "ord_1001": ["headphones"],
    "ord_1003": [],
    "ord_1005": ["socks"],
}


@traced
def get_order(order_id: str) -> dict[str, Any]:
    order = ORDERS.get(order_id)
    if order is None:
        return {"error": f"no order {order_id}"}
    return {"id": order_id, "total": sum(order["items"].values()), **order}


@traced
def get_return(order_id: str) -> dict[str, Any]:
    if order_id not in ORDERS:
        return {"error": f"no order {order_id}"}
    arrived = RETURNS.get(order_id, [])
    prices = ORDERS[order_id]["items"]
    return {"order_id": order_id, "arrived": arrived, "value": sum(prices[i] for i in arrived)}


@traced
def issue_refund(order_id: str, amount: float) -> dict[str, Any]:
    order = ORDERS.get(order_id)
    if order is None or order["status"] != "delivered":
        return {"error": f"order {order_id} cannot be refunded"}
    total = sum(order["items"].values())
    if amount <= 0 or amount > total:
        return {"error": f"amount must be more than 0 and at most {total}"}
    return {"refunded": amount, "order_id": order_id}


@traced
def escalate(reason: str, order_id: str | None = None) -> dict[str, Any]:
    return {"ticket": f"esc_{order_id or 'none'}", "reason": reason}


TOOL_FUNCTIONS: dict[str, Callable[..., dict[str, Any]]] = {
    "get_order": get_order,
    "get_return": get_return,
    "issue_refund": issue_refund,
    "escalate": escalate,
}


def _call_tool(name: str, args: dict[str, Any]) -> dict[str, Any]:
    tool = TOOL_FUNCTIONS.get(name)
    if tool is None:
        return {"error": f"no tool named {name}"}
    try:
        return tool(**args)
    except TypeError as exc:
        return {"error": f"bad arguments: {exc}"}


class State(TypedDict):
    contents: list[Any]
    calls: list[Any]
    turns: int
    reply: str


class SupportAgent:
    def __init__(self, client: Any, prompt: str = PROMPT, max_turns: int = 6) -> None:
        self.client = wrap_gemini(client)
        self.prompt = prompt
        self.max_turns = max_turns
        graph = StateGraph(State)
        graph.add_node("model", self._model)
        graph.add_node("tools", self._tools)
        graph.add_edge(START, "model")
        graph.add_conditional_edges("model", self._next, ["tools", END])
        graph.add_edge("tools", "model")
        self.graph = graph.compile()

    def _model(self, state: State) -> dict[str, Any]:
        if state["turns"] >= self.max_turns:
            return {"calls": [], "reply": "Sorry, I could not finish this request."}
        response = self.client.models.generate_content(
            model=MODEL,
            contents=state["contents"],
            config={"system_instruction": self.prompt, "tools": TOOLS, "temperature": 0},
        )
        content = response.candidates[0].content
        calls = [p.function_call for p in content.parts if p.function_call]
        if not calls:
            return {"calls": [], "reply": "".join(p.text or "" for p in content.parts)}
        # The model's own turn goes back unchanged, so Gemini keeps its reasoning state.
        return {
            "contents": [*state["contents"], content],
            "calls": calls,
            "turns": state["turns"] + 1,
        }

    def _tools(self, state: State) -> dict[str, Any]:
        results = [
            {"function_response": {"name": c.name, "response": _call_tool(c.name, dict(c.args))}}
            for c in state["calls"]
        ]
        return {"contents": [*state["contents"], {"role": "user", "parts": results}], "calls": []}

    @staticmethod
    def _next(state: State) -> str:
        return "tools" if state["calls"] else END

    @traced(name="handle_request")
    def handle(self, case_input: dict[str, Any]) -> dict[str, Any]:
        start: State = {
            "contents": [{"role": "user", "parts": [{"text": case_input["message"]}]}],
            "calls": [],
            "turns": 0,
            "reply": "",
        }
        final = self.graph.invoke(start)
        return {"reply": final["reply"]}
