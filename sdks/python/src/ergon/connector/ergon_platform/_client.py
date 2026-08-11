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


def create_ergon_client(config: ErgonPlatformClient):
    ErgonClient = _get_ergon_client()
    return ErgonClient(
        client_id=config.client_id,
        client_secret=config.client_secret,
        base_url=config.base_url,
        company_id=config.company_id,
        timeout=config.timeout,
        max_retries=config.max_retries,
    )
