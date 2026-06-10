"""
Fluxo de autenticação Nylas — gerar URL e obter grant_id.

Uso:
    cd sdks/python
    pip install -e ".[nylas]"
    cp examples/nylas/.env.example examples/nylas/.env
    # preencher NYLAS_API_KEY e NYLAS_REDIRECT_URI no .env

    # Etapa 1: gerar URL de autenticação
    python examples/nylas/auth_flow.py --generate-url

    # Etapa 2: trocar o code (da URL de redirect) pelo grant_id
    python examples/nylas/auth_flow.py --exchange-code "CODE_DO_REDIRECT"
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

from ergon.connector.nylas import (
    AuthUrlConfig,
    CodeExchangeInput,
    NylasAuthClient,
    NylasAuthService,
)

load_dotenv(Path(__file__).parent / ".env")

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger(__name__)


def _require_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        logger.error("Variável de ambiente obrigatória não definida: %s", name)
        sys.exit(1)
    return value


def _build_auth_client() -> NylasAuthClient:
    api_key = _require_env("NYLAS_API_KEY")
    api_uri = os.getenv("NYLAS_API_URI", "https://api.us.nylas.com").strip()
    client_id = os.getenv("NYLAS_CLIENT_ID", "").strip() or None
    return NylasAuthClient(api_key=api_key, client_id=client_id, api_uri=api_uri)


def generate_url() -> None:
    redirect_uri = _require_env("NYLAS_REDIRECT_URI")
    provider = os.getenv("NYLAS_PROVIDER", "").strip() or None
    login_hint = os.getenv("NYLAS_LOGIN_HINT", "").strip() or None

    service = NylasAuthService(_build_auth_client())
    config = AuthUrlConfig(
        redirect_uri=redirect_uri,
        provider=provider,
        login_hint=login_hint,
    )
    auth_url = service.generate_auth_url(config)

    print("\n=== URL de autenticação ===\n")
    print(auth_url)
    print("\n1. Abra a URL acima no navegador")
    print("2. Autentique a caixa de e-mail")
    print("3. Copie o parâmetro 'code' da URL de redirect")
    print("4. Execute: python examples/nylas/auth_flow.py --exchange-code \"SEU_CODE\"\n")


def exchange_code(code: str) -> None:
    redirect_uri = _require_env("NYLAS_REDIRECT_URI")

    service = NylasAuthService(_build_auth_client())
    result = service.exchange_code_for_token(
        CodeExchangeInput(code=code, redirect_uri=redirect_uri)
    )

    print("\n=== Grant obtido com sucesso ===\n")
    print(f"NYLAS_GRANT_ID={result.grant_id}")
    if result.email:
        print(f"Email: {result.email}")
    if result.provider:
        print(f"Provider: {result.provider}")
    print("\nAdicione NYLAS_GRANT_ID ao seu arquivo .env para usar os outros exemplos.\n")


def main() -> None:
    parser = argparse.ArgumentParser(description="Fluxo de autenticação Nylas (Hosted OAuth)")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--generate-url",
        action="store_true",
        help="Gera a URL de autenticação para conectar a caixa de e-mail",
    )
    group.add_argument(
        "--exchange-code",
        metavar="CODE",
        help="Troca o authorization code pelo grant_id",
    )
    args = parser.parse_args()

    if args.generate_url:
        generate_url()
    else:
        exchange_code(args.exchange_code)


if __name__ == "__main__":
    main()
