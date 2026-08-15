"""The Workflows package split must preserve public modular imports."""


def test_legacy_connector_modules_reexport_workflows_api() -> None:
    from ergon.connector.ergon_platform.async_connector import (
        AsyncErgonPlatformConnector,
    )
    from ergon.connector.ergon_platform.connector import ErgonPlatformConnector
    from ergon.connector.ergon_platform.utils import get_value
    from ergon.connector.ergon_platform.workflows import (
        AsyncErgonPlatformConnector as CurrentAsyncConnector,
    )
    from ergon.connector.ergon_platform.workflows import (
        ErgonPlatformConnector as CurrentConnector,
    )

    assert ErgonPlatformConnector is CurrentConnector
    assert AsyncErgonPlatformConnector is CurrentAsyncConnector
    assert get_value({"value": 42}, "value") == 42
