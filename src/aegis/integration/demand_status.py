"""Accepted demand is deliberately narrower than 'not cancelled'. Unknown states fail closed."""
from .streams import FieldError

STATUS_RULES = {
    "amazon_sp": {
        "unshipped": True, "partially_shipped": True, "shipped": True,
        "pending": False, "pending_availability": False, "cancelled": False, "canceled": False,
        "unfulfillable": False, "invoice_unconfirmed": False,
    },
    "tiktok_shop": {
        "awaiting_shipment": True, "partially_shipping": True, "awaiting_collection": True,
        "in_transit": True, "delivered": True, "completed": True,
        "unpaid": False, "on_hold": False, "cancelled": False, "canceled": False,
    },
    "generic": {
        **dict.fromkeys(("paid", "confirmed", "accepted", "processing", "unshipped", "partially_shipped",
                         "shipped", "fulfilled", "delivered", "completed"), True),
        **dict.fromkeys(("pending", "unpaid", "cancelled", "canceled", "void", "rejected", "unfulfillable"), False),
    },
}


def eligible(platform: str, status: str) -> bool:
    rules = STATUS_RULES.get(platform, STATUS_RULES["generic"])
    value = status.strip().lower()
    if value not in rules:
        raise FieldError("status", f"unrecognized {platform} order status; map it explicitly before replay")
    return rules[value]
