from typing import Any

FIXTURE: dict[str, Any] = {
    "employees": [
        {
            "user_id": "emp_1",
            "name": "Rhea Kapoor",
            "email": "rhea@corp.example",
            "department": "engineering",
            "status": "active",
            "manager_id": "emp_9",
            "access": ["email"],
        },
        {
            "user_id": "emp_2",
            "name": "Tom Hale",
            "email": "tom@corp.example",
            "department": "engineering",
            "status": "active",
            "manager_id": "emp_1",
            "access": ["email"],
        },
        {
            "user_id": "emp_3",
            "name": "Ana Ruiz",
            "email": "ana@corp.example",
            "department": "sales",
            "status": "suspended",
            "manager_id": "emp_1",
            "access": ["email", "crm"],
        },
        {
            "user_id": "emp_9",
            "name": "Lee Park",
            "email": "lee@corp.example",
            "department": "engineering",
            "status": "active",
            "access": ["email", "jira"],
        },
    ],
    "departments": [
        {"name": "engineering", "self_service": ["jira", "wiki", "github"]},
        {"name": "sales", "self_service": ["crm", "wiki"]},
    ],
    "tickets": [
        {"ticket_id": "it_1", "requester_id": "emp_1", "subject": "Laptop"},
        {"ticket_id": "it_9", "requester_id": "emp_9", "subject": "Monitor"},
    ],
}
