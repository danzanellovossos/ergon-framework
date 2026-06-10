"""Tests for NylasAuthService — auth URL generation and code exchange."""

from unittest.mock import MagicMock, patch

from ergon.connector.nylas.auth_service import NylasAuthService
from ergon.connector.nylas.models import AuthUrlConfig, CodeExchangeInput, NylasAuthClient


def _make_mock_nylas():
    mock = MagicMock()
    mock.auth = MagicMock()
    return mock


def _make_auth_service(client_id: str | None = None) -> NylasAuthService:
    client = NylasAuthClient(api_key="test-api-key", client_id=client_id)
    with patch("ergon.connector.nylas.auth_service._get_nylas_client") as mock_client_cls:
        mock_nylas = _make_mock_nylas()
        mock_client_cls.return_value = mock_nylas
        service = NylasAuthService(client)
    return service


class TestGenerateAuthUrl:
    def test_generates_url_with_required_params(self):
        service = _make_auth_service()
        service._nylas.auth.url_for_oauth2.return_value = "https://api.us.nylas.com/v3/connect/auth?..."

        config = AuthUrlConfig(redirect_uri="http://localhost:5000/oauth/callback")
        url = service.generate_auth_url(config)

        assert url.startswith("https://")
        service._nylas.auth.url_for_oauth2.assert_called_once_with(
            {
                "client_id": "test-api-key",
                "redirect_uri": "http://localhost:5000/oauth/callback",
            }
        )

    def test_passes_optional_provider_and_login_hint(self):
        service = _make_auth_service(client_id="app-client-id")
        service._nylas.auth.url_for_oauth2.return_value = "https://auth.example.com"

        config = AuthUrlConfig(
            redirect_uri="http://localhost:5000/callback",
            provider="google",
            login_hint="user@example.com",
            state="csrf-token",
        )
        service.generate_auth_url(config)

        service._nylas.auth.url_for_oauth2.assert_called_once_with(
            {
                "client_id": "app-client-id",
                "redirect_uri": "http://localhost:5000/callback",
                "provider": "google",
                "login_hint": "user@example.com",
                "state": "csrf-token",
            }
        )


class TestExchangeCodeForToken:
    def test_returns_grant_auth_result(self):
        service = _make_auth_service()
        response = MagicMock()
        response.grant_id = "grant-abc-123"
        response.email = "user@example.com"
        response.provider = "google"
        response.scope = "email"
        service._nylas.auth.exchange_code_for_token.return_value = response

        result = service.exchange_code_for_token(
            CodeExchangeInput(
                code="auth-code-xyz",
                redirect_uri="http://localhost:5000/oauth/callback",
            )
        )

        assert result.grant_id == "grant-abc-123"
        assert result.email == "user@example.com"
        assert result.provider == "google"
        assert result.scope == "email"

        service._nylas.auth.exchange_code_for_token.assert_called_once_with(
            {
                "code": "auth-code-xyz",
                "client_id": "test-api-key",
                "redirect_uri": "http://localhost:5000/oauth/callback",
            }
        )

    def test_handles_dict_response(self):
        service = _make_auth_service()
        service._nylas.auth.exchange_code_for_token.return_value = {
            "grant_id": "grant-dict-456",
            "email": "other@example.com",
        }

        result = service.exchange_code_for_token(
            CodeExchangeInput(code="code-1", redirect_uri="http://localhost/cb")
        )

        assert result.grant_id == "grant-dict-456"
        assert result.email == "other@example.com"
