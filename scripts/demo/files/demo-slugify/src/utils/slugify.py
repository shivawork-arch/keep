import re
import unicodedata


def slugify(text: str) -> str:
    """'Red Shoes (Size 9)' -> 'red-shoes-size-9'"""
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
