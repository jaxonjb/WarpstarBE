from bson import ObjectId
from typing import Any


def serialize_doc(doc: dict | None) -> dict | None:
    """Recursively convert ObjectId fields to strings for JSON serialisation."""
    if doc is None:
        return None
    out = {}
    for k, v in doc.items():
        key = "id" if k == "_id" else k
        if isinstance(v, ObjectId):
            out[key] = str(v)
        elif isinstance(v, list):
            out[key] = [serialize_doc(i) if isinstance(i, dict) else (str(i) if isinstance(i, ObjectId) else i) for i in v]
        elif isinstance(v, dict):
            out[key] = serialize_doc(v)
        else:
            out[key] = v
    return out


def serialize_docs(docs: list[dict]) -> list[dict]:
    return [serialize_doc(d) for d in docs]


def paginate_params(skip: int = 0, limit: int = 20) -> dict[str, int]:
    limit = min(limit, 100)  # hard cap
    return {"skip": skip, "limit": limit}
