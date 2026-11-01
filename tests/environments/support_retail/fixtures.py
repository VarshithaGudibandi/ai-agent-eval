from typing import Any

FIXTURE: dict[str, Any] = {
    "customers": [
        {"customer_id": "cust_12", "name": "Dana Ruiz", "email": "dana@example.com"},
        {"customer_id": "cust_99", "name": "Sam Lee", "email": "sam@example.com"},
    ],
    "orders": [
        {
            "order_id": "ord_1001",
            "customer_id": "cust_12",
            "status": "delivered",
            "currency": "USD",
            "total_minor": 24999,
            "items": [
                {"sku": "HP-200", "name": "Headphones", "quantity": 1, "unit_price_minor": 24999}
            ],
        },
        {
            "order_id": "ord_1002",
            "customer_id": "cust_12",
            "status": "delivered",
            "currency": "USD",
            "total_minor": 9000,
            "items": [
                {"sku": "KB-1", "name": "Keyboard", "quantity": 1, "unit_price_minor": 6000},
                {"sku": "MS-1", "name": "Mouse", "quantity": 1, "unit_price_minor": 3000},
            ],
        },
        {
            "order_id": "ord_2002",
            "customer_id": "cust_99",
            "status": "delivered",
            "currency": "USD",
            "total_minor": 5000,
            "items": [{"sku": "CB-1", "name": "Cable", "quantity": 2, "unit_price_minor": 2500}],
        },
    ],
    "returns": [
        {
            "order_id": "ord_1001",
            "status": "received",
            "inspection": "passed",
            "received_on": "2026-10-28",
        },
        {"order_id": "ord_1002", "status": "in_transit"},
    ],
    "refunds": [
        {
            "refund_id": "ref_0001",
            "order_id": "ord_1002",
            "amount_minor": 3000,
            "currency": "USD",
            "issued_on": "2026-10-20",
        }
    ],
    "tickets": [
        {"ticket_id": "tkt_1", "customer_id": "cust_12", "subject": "Refund", "status": "open"},
        {"ticket_id": "tkt_9", "customer_id": "cust_99", "subject": "Cable", "status": "open"},
    ],
    "reviewer": {
        "decision": "approve",
        "permitted": [
            {"order_id": "ord_1001", "max_amount_minor": 24999},
            # More than is left on the order, to show the balance check stands on its own.
            {"order_id": "ord_1002", "max_amount_minor": 9000},
        ],
    },
}
