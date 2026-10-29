def list_orders(db, page: int = 1, size: int = 50):
    """Newest orders first, one page at a time."""
    skip = (page - 1) * size
    return db.query(
        "SELECT * FROM orders ORDER BY created_at DESC LIMIT %s OFFSET %s",
        (size, skip),
    )
