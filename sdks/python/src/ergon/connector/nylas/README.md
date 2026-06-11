# Nylas Email Connector

Connector sync e async para leitura e envio de e-mail via [Nylas](https://www.nylas.com/) (Gmail, Microsoft, IMAP, Yahoo, iCloud).

## Instalação

```bash
pip install ergon-framework-python[nylas]
```

## Pré-requisitos

Autenticação e conexão da caixa usam [Nylas Hosted Auth](https://developer.nylas.com/docs/v3/auth/). Registre o `redirect_uri` no Nylas Dashboard antes de iniciar o OAuth.

Exemplos executáveis: [`examples/nylas/`](../../../../examples/nylas/)

## Autenticação (obter grant_id)

Use `NylasAuthService` para gerar a URL OAuth e trocar o authorization code por um `grant_id`:

```python
from ergon.connector.nylas import (
    AuthUrlConfig,
    CodeExchangeInput,
    NylasAuthClient,
    NylasAuthService,
)

auth = NylasAuthService(
    NylasAuthClient(
        api_key="<NYLAS_API_KEY>",
        client_id="<NYLAS_CLIENT_ID>",  # opcional; default é api_key
    )
)

# Etapa 1: redirecionar o usuário para esta URL
auth_url = auth.generate_auth_url(
    AuthUrlConfig(
        redirect_uri="http://localhost:5000/oauth/callback",
        provider="google",  # opcional
        login_hint="user@example.com",  # opcional
    )
)

# Etapa 2: após o redirect, trocar o code por grant_id
result = auth.exchange_code_for_token(
    CodeExchangeInput(
        code="<CODE_FROM_REDIRECT>",
        redirect_uri="http://localhost:5000/oauth/callback",
    )
)
grant_id = result.grant_id
```

Variante async: `AsyncNylasAuthService` com a mesma API.

CLI: `python examples/nylas/auth_flow.py --generate-url` e depois `--exchange-code "..."`.

Após obter o `grant_id`, passe-o para `NylasClient` nas operações do connector.

## Configuração

| Classe | Papel |
|--------|-------|
| `NylasClient` | Credenciais (`api_key`, `grant_id`, `api_uri`) |
| `NylasConsumerConfig` | Filtros de fetch, batch, download de anexos, ack |
| `NylasProducerConfig` | Modo de envio (`send` ou `draft`), remetente padrão |
| `AckActionConfig` | Ações pós-processamento (marcar lido, mover pasta, star) |

## Async Nylas Connector (recomendado)

```python
from ergon.connector import ConnectorConfig
from ergon.connector.nylas import (
    AckActionConfig,
    AsyncNylasConnector,
    NylasClient,
    NylasConsumerConfig,
    NylasProducerConfig,
)
from ergon.task.base import TaskConfig

config = TaskConfig(
    name="email-processor",
    task=MyEmailTask,
    connectors={
        "inbox": ConnectorConfig(
            connector=AsyncNylasConnector,
            kwargs={
                "client": NylasClient(
                    api_key="...",
                    grant_id="...",
                ),
                "consumer_config": NylasConsumerConfig(
                    subject="Invoice",
                    has_attachment=True,
                    unread=True,
                    in_="<INBOX_FOLDER_ID>",
                    batch_size=10,
                    download_attachments=True,
                    ack_config=AckActionConfig(
                        mark_as_read=True,
                        move_to_folder_id="<PROCESSED_FOLDER_ID>",
                    ),
                ),
                "producer_config": NylasProducerConfig(send_mode="send"),
            },
        ),
    },
    policies=[consumer_policy],
)
```

## Sync Nylas Connector

```python
from ergon.connector.nylas import NylasConnector, NylasClient, NylasConsumerConfig

connector = NylasConnector(
    client=NylasClient(api_key="...", grant_id="..."),
    consumer_config=NylasConsumerConfig(unread=True, batch_size=5),
)
```

## Filtros de fetch

| Filter | Description |
|--------|-------------|
| `subject` | Case-sensitive partial subject match (API-level) |
| `has_attachment` | Messages with attachments |
| `from_`, `to`, `cc`, `bcc`, `any_email` | Participant email filters |
| `unread`, `starred` | Message state |
| `received_after`, `received_before` | Unix timestamp range |
| `in_` | Folder or label ID |
| `thread_id` | Specific thread |
| `search_query_native` | Provider-specific search string |
| `client_side_filter` | Post-API filters (case-insensitive subject, attachment name/type) |

Use `service.list_folders()` para descobrir IDs de pasta para o filtro `in_`.

## Fluxo fetch → process → ack

1. **Fetch**: `fetch_transactions_async` retorna `Transaction` com payload da mensagem e metadata (`thread_id`, `attachments`, `unread`).
2. **Process**: A task executa a lógica de negócio.
3. **Ack**: `ack_transaction` aplica `AckActionConfig` (marcar lido, mover pasta, star).

`nack_transaction` é no-op para Nylas — mensagens permanecem disponíveis para refetch.

## Dispatch (envio de e-mail)

Defina `producer_config.send_mode` como `"send"` (default) para envio direto ou `"draft"` para criar e enviar um rascunho. O `payload` da transaction deve ser um `SendMessageInput` ou dict compatível com a API de envio Nylas.

## Limitações conhecidas

- O filtro `subject` na API é case-sensitive; use `client_side_filter.subject_contains` para matching flexível.
- `search_query_native` não pode ser combinado com `unread`, `has_attachment`, `subject`, etc. (apenas `in`, `limit` e `page_token` são permitidos junto). Para filtro por nome de anexo que compõe com outros filtros, use `client_side_filter.attachment_filename_contains`.
- A sintaxe de `search_query_native` varia por provedor (Google, Microsoft, IMAP).
- Anexos grandes do Microsoft (>25MB) exigem o fluxo de large-attachment da Nylas (não incluído neste connector).
