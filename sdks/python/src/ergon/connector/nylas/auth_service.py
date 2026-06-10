import logging
from typing import Any, Dict

from .models import AuthUrlConfig, CodeExchangeInput, GrantAuthResult, NylasAuthClient
from .service import _get_nylas_client
from .utils import serialize_nylas_object

logger = logging.getLogger(__name__)


class NylasAuthService:
    def __init__(self, client: NylasAuthClient) -> None:
        self.client = client
        Client = _get_nylas_client()
        self._nylas = Client(client.api_key, client.api_uri)

    def generate_auth_url(self, config: AuthUrlConfig) -> str:
        auth_config = config.to_auth_config(self.client.get_client_id())
        logger.info("Generating Nylas Hosted OAuth URL for redirect_uri=%s", config.redirect_uri)
        return self._nylas.auth.url_for_oauth2(auth_config)

    def exchange_code_for_token(self, request: CodeExchangeInput) -> GrantAuthResult:
        exchange_request: Dict[str, Any] = {
            "code": request.code,
            "client_id": self.client.get_client_id(),
            "redirect_uri": request.redirect_uri,
        }
        logger.info("Exchanging authorization code for grant_id")
        response = self._nylas.auth.exchange_code_for_token(exchange_request)
        data = serialize_nylas_object(response)
        if isinstance(data, dict):
            return GrantAuthResult(
                grant_id=str(data.get("grant_id", "")),
                email=data.get("email"),
                provider=data.get("provider"),
                scope=data.get("scope"),
            )
        return GrantAuthResult(
            grant_id=str(getattr(response, "grant_id", "")),
            email=getattr(response, "email", None),
            provider=getattr(response, "provider", None),
            scope=getattr(response, "scope", None),
        )
