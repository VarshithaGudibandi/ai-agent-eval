"""A minimal external agent for the AI Agent Eval platform.

It speaks protocol version 1 and uses only the Python standard library:

1. The platform POSTs a signed JSON task to this server.
2. The agent checks the signature with the workspace's signing secret.
3. It calls tools through the platform's tool gateway, using the run token it was given.
4. It replies with {"final_response": "..."}.

The agent itself is a few hand-written rules for the support-retail store. Swap the
`decide` function for your own agent; the protocol code around it stays the same.

Run it with:  AGENT_SIGNING_SECRET=whsec_... python agent.py
"""

import hashlib
import hmac
import json
import os
import re
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

PROTOCOL_VERSION = "1"
SIGNATURE_HEADER = "x-agent-eval-signature"
TOLERANCE_SECONDS = 300


def signature_is_valid(secret: str, header: str, body: bytes) -> bool:
    """Accept only requests the platform signed recently with the workspace's secret."""
    try:
        parts = dict(item.split("=", 1) for item in header.split(","))
        stamp = int(parts["t"])
    except (KeyError, ValueError):
        return False
    if abs(time.time() - stamp) > TOLERANCE_SECONDS:
        return False
    expected = hmac.new(secret.encode(), f"{stamp}.".encode() + body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, parts.get("v1", ""))


class Gateway:
    """The only way the agent acts. Every call goes to the platform with the run token."""

    def __init__(self, url: str, token: str) -> None:
        if not url.startswith(("http://", "https://")):
            raise ValueError("the gateway URL must be http or https")
        self.url = url.rstrip("/")
        self.token = token

    def call(self, tool: str, **arguments: Any) -> dict[str, Any]:
        # The scheme was checked above, so urlopen only ever sees http or https.
        request = urllib.request.Request(  # noqa: S310
            f"{self.url}/{tool}",
            data=json.dumps(arguments).encode(),
            headers={"authorization": f"Bearer {self.token}", "content-type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310
                result: dict[str, Any] = json.loads(response.read())
                return result
        except urllib.error.HTTPError as error:
            # 409 means the run was stopped or taken over: stop acting.
            return {"status": "stopped", "error_code": f"http_{error.code}", "output": {}}


def money(minor: int, currency: str) -> str:
    symbol = {"USD": "$", "INR": "₹"}.get(currency, f"{currency} ")
    return f"{symbol}{minor // 100:,}.{minor % 100:02d}"


def decide(task: dict[str, Any], gateway: Gateway) -> str:
    """A tiny support agent: look things up, refund only what the rules allow, say what happened."""
    message = task["user_message"]
    orders = re.findall(r"\bord_\w+", message)
    if not orders:
        return "Happy to help. Which order is this about? Please share the order number."
    order_id = orders[0]
    order = gateway.call("get_order", order_id=order_id)
    if order["status"] != "ok":
        return f"I can't find order {order_id} on your account."
    details = order["output"]
    if not re.search(r"refund|money back", message, re.IGNORECASE):
        return f"Order {order_id} is {details['status']}."
    returned = gateway.call("get_return_status", order_id=order_id)["output"]
    if returned.get("status") != "received" or returned.get("inspection") != "passed":
        state = returned.get("status", "not_started").replace("_", " ")
        return (
            f"Your return for {order_id} is {state}, so I can't issue a refund yet. "
            "A refund can be issued once the item arrives and passes inspection."
        )
    refunds = gateway.call("get_refund_status", order_id=order_id)["output"]
    amount = refunds.get("refundable_minor", 0)
    if amount <= 0:
        return f"Order {order_id} was already refunded in full."
    approval = gateway.call(
        "request_refund_approval",
        order_id=order_id,
        amount_minor=amount,
        reason="Return received and passed inspection.",
    )
    if approval["output"].get("status") != "approved":
        return "I asked for approval, but the refund was not approved, so I can't refund it."
    refund = gateway.call(
        "issue_refund",
        order_id=order_id,
        amount_minor=amount,
        approval_id=approval["output"]["approval_id"],
    )
    if refund["status"] != "ok":
        return "I couldn't complete the refund. Our team will follow up."
    return (
        f"Your return passed inspection, so I have refunded {money(amount, details['currency'])}."
    )


class Handler(BaseHTTPRequestHandler):
    secret = os.environ.get("AGENT_SIGNING_SECRET", "")

    def do_POST(self) -> None:
        body = self.rfile.read(int(self.headers.get("content-length", "0")))
        if not signature_is_valid(self.secret, self.headers.get(SIGNATURE_HEADER, ""), body):
            self._reply(401, {"error": "bad signature"})
            return
        request = json.loads(body)
        if request.get("kind") == "handshake":
            self._reply(200, {"protocol_version": PROTOCOL_VERSION})
            return
        gateway = Gateway(request["tool_gateway"]["url"], request["tool_gateway"]["token"])
        self._reply(200, {"final_response": decide(request["task"], gateway)})

    def _reply(self, status: int, payload: dict[str, Any]) -> None:
        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, format: str, *args: Any) -> None:
        return


def serve(port: int, host: str = "0.0.0.0") -> ThreadingHTTPServer:  # noqa: S104
    # All interfaces by default, because in a container that is how the platform reaches it.
    return ThreadingHTTPServer((host, port), Handler)


if __name__ == "__main__":
    serve(int(os.environ.get("PORT", "8080"))).serve_forever()
