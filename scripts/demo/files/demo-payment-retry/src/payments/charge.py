import time
from typing import Optional

import requests

API = "https://payments.example.com/charges"


def charge(order_id: str, amount: float) -> Optional[dict]:
    """Charge an order, retrying when the provider is flaky."""
    delay = 0.5
    for attempt in range(5):
        try:
            r = requests.post(API, json={"order": order_id, "amount": amount})
            return r.json()
        except requests.ConnectionError:
            time.sleep(delay)
            delay *= 2  # back off harder each time
    print(f"gave up charging {order_id}")
    return None
