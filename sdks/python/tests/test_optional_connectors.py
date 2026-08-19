"""Regression tests for connector dependency isolation."""

import subprocess
import sys
import textwrap


def _run_isolated(script: str) -> None:
    result = subprocess.run(
        [sys.executable, "-c", textwrap.dedent(script)],
        text=True,
        capture_output=True,
    )
    assert result.returncode == 0, f"isolated script failed:\n{result.stdout}\n{result.stderr}"


def test_core_import_does_not_load_connector_clients() -> None:
    _run_isolated(
        """
        import sys

        import ergon
        from ergon.connector import Connector, Transaction

        assert Connector is not None
        assert Transaction is not None
        assert "aio_pika" not in sys.modules
        assert "asyncpg" not in sys.modules
        assert "boto3" not in sys.modules
        assert "httpx" not in sys.modules
        assert "msal" not in sys.modules
        assert "nylas" not in sys.modules
        assert "openpyxl" not in sys.modules
        assert "pika" not in sys.modules
        assert "requests" not in sys.modules
        """
    )


def test_connector_models_remain_available_without_loading_client() -> None:
    _run_isolated(
        """
        import sys

        from ergon.connector import (
            AsyncRabbitmqConsumerConfig,
            OutlookAckActionConfig,
            OutlookGraphClient,
            OutlookNackActionConfig,
        )

        assert AsyncRabbitmqConsumerConfig is not None
        assert OutlookAckActionConfig is not None
        assert OutlookGraphClient is not None
        assert OutlookNackActionConfig is not None
        assert "aio_pika" not in sys.modules
        assert "msal" not in sys.modules
        assert "pika" not in sys.modules
        assert "requests" not in sys.modules
        """
    )


def test_subpackages_are_reachable_as_module_attributes() -> None:
    _run_isolated(
        """
        import ergon.connector

        assert ergon.connector.rabbitmq.RabbitMQConnector is not None
        assert ergon.connector.postgres.PostgresClient is not None
        assert ergon.connector.outlook.OutlookGraphClient is not None
        assert "outlook" in dir(ergon.connector)
        assert "rabbitmq" in dir(ergon.connector)
        """
    )


def test_missing_outlook_extra_has_actionable_error() -> None:
    _run_isolated(
        """
        import importlib.abc
        import sys


        class BlockRequests(importlib.abc.MetaPathFinder):
            def find_spec(self, fullname, path, target=None):
                if fullname == "requests" or fullname.startswith("requests."):
                    raise ModuleNotFoundError("blocked for test", name=fullname)
                return None


        sys.meta_path.insert(0, BlockRequests())

        try:
            from ergon.connector import OutlookGraphService
        except ImportError as exc:
            assert "ergon-framework-python[outlook]" in str(exc)
        else:
            raise AssertionError("OutlookGraphService imported without the blocked dependency")
        """
    )


def test_missing_connector_extra_has_actionable_error() -> None:
    _run_isolated(
        """
        import importlib.abc
        import sys


        class BlockPika(importlib.abc.MetaPathFinder):
            def find_spec(self, fullname, path, target=None):
                if fullname == "pika" or fullname.startswith("pika."):
                    raise ModuleNotFoundError("blocked for test", name=fullname)
                return None


        sys.meta_path.insert(0, BlockPika())

        try:
            from ergon.connector import RabbitMQConnector
        except ImportError as exc:
            assert "ergon-framework-python[rabbitmq]" in str(exc)
        else:
            raise AssertionError("RabbitMQConnector imported without the blocked dependency")
        """
    )
