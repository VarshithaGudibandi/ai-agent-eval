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
            "order_id": "ord_2002",
            "customer_id": "cust_99",
            "status": "delivered",
            "currency": "USD",
            "total_minor": 5000,
            "items": [{"sku": "CB-1", "name": "Cable", "quantity": 2, "unit_price_minor": 2500}],
        },
    ],
}
