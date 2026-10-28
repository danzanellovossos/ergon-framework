import os
import socket
from typing import Optional

from .models import ErgonPlatformClient

_ERGON_PLATFORM_IMPORT_ERROR = (
    "The 'ergon-platform' connector dependencies are not installed. "
    "Install them with: pip install 'ergon-framework-python[ergon-platform]'"
)


def _get_ergon_client():
    try:
        from ergon_platform import ErgonClient  # type: ignore[reportMissingImports]
    except ImportError as exc:
        raise ImportError(_ERGON_PLATFORM_IMPORT_ERROR) from exc
    return ErgonClient


def default_consumer_id() -> str:
    """One identity per worker process: ``<hostname>:<pid>``.

    Pull only phases fence item claims by this value; two processes must never
    share it, two batches of the same process should.
    """
    host = socket.gethostname().split(".")[0] or "worker"
    safe_host = "".join(ch if (ch.isalnum() or ch in "._:@-") else "-" for ch in host)
    return f"{safe_host}:{os.getpid()}"[:128]


def create_ergon_client(config: ErgonPlatformClient, *, consumer_id: Optional[str] = None):
    """Build the SDK client. ``consumer_id`` adds ``X-Ergon-Consumer-Id`` to every request."""
    ErgonClient = _get_ergon_client()
    if consumer_id is None:
        return ErgonClient(
            client_id=config.client_id,
            client_secret=config.client_secret,
            base_url=config.base_url,
            company_id=config.company_id,
            timeout=config.timeout,
            max_retries=config.max_retries,
        )
    return ErgonClient(
        client_id=config.client_id,
        client_secret=config.client_secret,
        base_url=config.base_url,
        company_id=config.company_id,
        consumer_id=consumer_id,
        timeout=config.timeout,
        max_retries=config.max_retries,
    )
