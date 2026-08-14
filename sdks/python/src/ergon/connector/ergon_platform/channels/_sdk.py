from typing import Any, List, Optional


class SdkRecord:
    """Uniform read API over dict-or-object SDK payloads."""

    def __init__(self, raw: Any) -> None:
        self.raw = raw

    def get(self, key: str, default: Any = None) -> Any:
        """Get a value from the record."""
        if isinstance(self.raw, dict):
            return self.raw.get(key, default)
        return getattr(self.raw, key, default)

    @classmethod
    def items(cls, response: Any, *, keys: Optional[List[str]] = None) -> List[Any]:
        """Get items from a response."""
        candidate_keys = keys or ["items", "messages", "data", "results"]
        if isinstance(response, list):
            return response
        if isinstance(response, dict):
            for key in candidate_keys:
                value = response.get(key)
                if isinstance(value, list):
                    return value
            return []
        for key in candidate_keys:
            value = getattr(response, key, None)
            if isinstance(value, (list, tuple)):
                return list(value)
        return []

    @classmethod
    def total(cls, response: Any) -> int:
        """Get the total from a response."""
        record = cls(response)
        for key in ("total", "total_count", "count"):
            value = record.get(key)
            if value is not None:
                return int(value)
        return 0

    @classmethod
    def serialize(cls, obj: Any) -> Any:
        """Serialize an object to a dictionary."""
        if obj is None or isinstance(obj, (str, int, float, bool, bytes)):
            return obj
        if isinstance(obj, dict):
            return {k: cls.serialize(v) for k, v in obj.items()}
        if isinstance(obj, (list, tuple)):
            return [cls.serialize(item) for item in obj]
        if hasattr(obj, "model_dump"):
            return obj.model_dump(mode="json")
        if hasattr(obj, "to_dict"):
            return obj.to_dict()
        if hasattr(obj, "__dict__"):
            return {k: cls.serialize(v) for k, v in obj.__dict__.items() if not k.startswith("_")}
        return obj

    @classmethod
    def first_id(cls, mapping: dict, *keys: str) -> str:
        """Get the first id from a dictionary."""
        for key in keys:
            value = mapping.get(key)
            if value:
                return str(value)
        return ""
