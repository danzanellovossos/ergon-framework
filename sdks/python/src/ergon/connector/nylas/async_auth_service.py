import asyncio

from .auth_service import NylasAuthService
from .models import AuthUrlConfig, CodeExchangeInput, GrantAuthResult, NylasAuthClient


class AsyncNylasAuthService:
    def __init__(self, client: NylasAuthClient) -> None:
        self._sync = NylasAuthService(client)

    async def generate_auth_url(self, config: AuthUrlConfig) -> str:
        return await asyncio.to_thread(self._sync.generate_auth_url, config)

    async def exchange_code_for_token(self, request: CodeExchangeInput) -> GrantAuthResult:
        return await asyncio.to_thread(self._sync.exchange_code_for_token, request)
