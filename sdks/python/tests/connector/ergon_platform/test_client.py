"""Tests for the shared Ergon Platform client wrapper."""

from unittest.mock import patch

from ergon.connector.ergon_platform._client import create_ergon_client
from ergon.connector.ergon_platform.models import ErgonPlatformClient


class _FakeClient:
    pass


def _config(**overrides):
    defaults = dict(client_id="ek_x", client_secret="eks_y", base_url="https://api.test")
    defaults.update(overrides)
    return ErgonPlatformClient(**defaults)


class TestErgonPlatformClientDefaults:
    def test_base_url_defaults_to_production(self):
        client = ErgonPlatformClient(client_id="ek_x", client_secret="eks_y")
        assert client.base_url == "https://platform.ergondata.ai"

    def test_explicit_base_url(self):
        client = ErgonPlatformClient(
            client_id="ek_x",
            client_secret="eks_y",
            base_url="https://api.test",
        )
        assert client.base_url == "https://api.test"


class TestCreateErgonClient:
    def test_wraps_ergon_client_with_config(self):
        fake = _FakeClient()
        captured: dict = {}

        def fake_factory(**kwargs):
            captured.update(kwargs)
            return fake

        with patch(
            "ergon.connector.ergon_platform._client._get_ergon_client",
            return_value=fake_factory,
        ):
            result = create_ergon_client(_config(company_id="co-1"))

        assert result is fake
        assert captured == {
            "client_id": "ek_x",
            "client_secret": "eks_y",
            "base_url": "https://api.test",
            "company_id": "co-1",
            "timeout": 30.0,
            "max_retries": 2,
        }

    def test_uses_production_default_when_base_url_omitted(self):
        captured: dict = {}

        def fake_factory(**kwargs):
            captured.update(kwargs)
            return _FakeClient()

        with patch(
            "ergon.connector.ergon_platform._client._get_ergon_client",
            return_value=fake_factory,
        ):
            create_ergon_client(ErgonPlatformClient(client_id="ek_x", client_secret="eks_y"))

        assert captured["base_url"] == "https://platform.ergondata.ai"
