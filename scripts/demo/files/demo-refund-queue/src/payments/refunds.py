from src.queue import enqueue


def request_refund(order_id: str, cents: int, idempotency_key: str) -> str:
    """Queue a refund; the worker calls the provider. Returns the job id."""
    return enqueue("refunds.process", order_id=order_id, cents=cents, idempotency_key=idempotency_key)
