import uuid
import time

from typing import Optional, Dict


def headers_generator(
        id: uuid.UUID,
        content_type: Optional[str] = None,
        source: Optional[str] = None) -> Dict[str, str]:
    return {

        "content-type": content_type or "application/json",
        "correlation_id": str(id),
        "source": source,
        "timestamp": int(time.time()),
    }
