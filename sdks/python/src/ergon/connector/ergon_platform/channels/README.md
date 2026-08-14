# Connector Ergon Platform — Channels

Connector que integra o framework Ergon com o módulo **Channels** da Ergon Platform (envio de mensagens, thread messages e feed de atividade), envolvendo o SDK [`ergon-platform-sdk`](https://pypi.org/project/ergon-platform-sdk/) (`ErgonClient.channels`).

## Instalação

```bash
pip install 'ergon-framework-python[ergon-platform]'
```

O extra `ergon-platform` cobre workflows **e** channels — não precisa instalar nada adicional.

## Arquitetura

O connector é a fachada pública do framework (`fetch_transactions`, `send_email`, `ack_transaction`). Ele não fala com a API sozinho: delega para `_ErgonPlatformChannelsOperations`, que por sua vez orquestra objetos com uma responsabilidade cada. Sync e async compartilham esses objetos — o async só envolve as chamadas em `asyncio.to_thread`.

```
task / app
    │
    ▼
ErgonPlatformChannelsConnector  (sync)     AsyncErgonPlatformChannelsConnector
    │                                         │
    └──────────────┬──────────────────────────┘
                   ▼
        _ErgonPlatformChannelsOperations     ← Facade
                   │
     ┌─────────────┼──────────────┬────────────────┐
     ▼             ▼              ▼                ▼
 InboxAddressBook  ActivityAdapter  InboxAttachments  OutboundMessage
     │             │              │                │
     └─────────────┴──────────────┴────────────────┘
                   ▼
              SdkRecord  →  ErgonClient.channels.*  (SDK HTTP)
```

Arquivos com `_` no nome são internos. Apps e tasks importam só `connector`, `async_connector` e `models`.

```
ergon_platform/
├── models.py               # ErgonPlatformClient (credenciais compartilhadas)
├── _client.py              # Factory compartilhada do ErgonClient
└── channels/
    ├── models.py           # configs, SendMessageInput, ChannelsActivityFilter
    ├── connector.py        # API pública sync
    ├── async_connector.py  # API pública async (mesmo contrato)
    ├── _operations.py      # Facade HTTP
    ├── _sdk.py             # Adapter de resposta do SDK
    ├── _activity.py        # Adapter evento → Transaction
    ├── _addresses.py       # Resolução + cache de inbox
    ├── _attachments.py     # Download / hydrate de anexos
    └── _outbound.py        # Builder do POST /send
```

O connector reusa a mesma factory de `ErgonClient` do workflows. Toda ida à rede passa por `connector.client.channels.*`.

### Padrões


| Padrão | Onde | O que resolve |
|--------|------|----------------|
| **Facade** | `_ErgonPlatformChannelsOperations` | Um ponto só para o connector chamar. Fetch, ack, send e resolve inbox não espalham HTTP pelo sync/async. |
| **Adapter** | `SdkRecord` | O SDK devolve dict, objeto Pydantic ou Page. `get` / `items` / `total` / `serialize` escondem isso. |
| **Adapter** | `ActivityAdapter` | Evento da activity da plataforma → `Transaction` do framework (`id`, `payload`, `metadata`). |
| **Strategy** | `ChannelsActivityFilter.matches` / `select` | O filtro client-side (`from_address`, `subject_contains`) vive no próprio filtro. O fetch só pergunta “esta transação entra?”. |
| **Builder** | `OutboundMessage` | `SendMessageInput` ou `dict` vira o body de `POST /send` (roteamento `top` + bloco `config`). |

`InboxAddressBook` e `InboxAttachments` são cache/resolução de endereço e bytes de anexo saíram da facade para não voltar a ser uma classe só.

Não há Factory/Singleton/Visitor aqui — o `ErgonClient` já é criado em `_client.py`, compartilhado com workflows.

### Responsabilidades

| Peça | Arquivo | Faz | Não faz |
|------|---------|-----|---------|
| `ErgonPlatformChannelsConnector` | `connector.py` | Contrato do framework: fetch, send, ack/nack, hydrate se `download_attachments=True`, estado `_seen_event_ids`. | Montar JSON da API, parsear Page, achar `address_id`. |
| `AsyncErgonPlatformChannelsConnector` | `async_connector.py` | O mesmo contrato, I/O em thread. | Lógica de domínio diferente da sync. |
| `_ErgonPlatformChannelsOperations` | `_operations.py` | Encaminha cada método do connector ao colaborador certo e dispara `client.channels.*`. | Guardar regra de filtro, de anexo ou de endereço. |
| `SdkRecord` | `_sdk.py` | Ler um payload heterogêneo (`obj.get` vs `getattr`, lista em `items`/`data`/`messages`). | Conhecer activity, inbox ou send. |
| `ActivityAdapter` | `_activity.py` | `to_transaction`, listar anexos no metadata, `finalize_fetch` (filtro + dedup). | Chamar HTTP. |
| `InboxAddressBook` | `_addresses.py` | Email/`config_id` → `ResolvedInboxAddress` (cache, `configs.addresses`, fallback na activity). | Fetch de email nem send. |
| `InboxAttachments` | `_attachments.py` | `GET .../attachments/{id}/file`; hidrata `content` na transação ou grava em `dest`. | Decidir *se* baixa (isso é flag do consumer no connector). |
| `OutboundMessage` | `_outbound.py` | Normalizar destinatários e payload de envio; extrair `log_id` da resposta. | Resolver inbox de origem. |
| `ChannelsActivityFilter` | `models.py` | Query server-side (`event_type`, `correlation_id`) **e** match client-side. | HTTP. |
| `ResolvedInboxAddress` | `models.py` | `can_send` / `can_receive` e erros claros (`ensure_can_send`, `ensure_can_receive`). | Buscar o endereço na API. |

### Fluxo de fetch

1. Connector exige `consumer_config`, pede o inbox a `InboxAddressBook` e recusa send-only (`ensure_can_receive`).
2. Facade chama `GET /configs/{id}/activity` e `SdkRecord.items` puxa a página.
3. `ActivityAdapter.to_transaction` vira cada evento em `Transaction` (`metadata.attachments` ainda só metadados).
4. `ActivityAdapter.finalize_fetch` aplica `ChannelsActivityFilter.select` e, se `deduplicate_fetched_events`, o set `_seen_event_ids` do connector.
5. Se `download_attachments=True`, `InboxAttachments.hydrate` busca os bytes e coloca `content` em cada anexo. Arquivo lento/falho é **pulado** (sem `content`) para não travar ack/nack. Timeout por arquivo: `attachment_download_timeout` (default 20s, client dedicado, sem retry).

`fetch_transaction_by_id` é o mesmo caminho para um único `GET .../activity/{event_id}`.

### Fluxo de send

1. Connector monta `SendMessageInput` (ou recebe `dict`).
2. `OutboundMessage.normalize` separa roteamento (`address_id`, `channel`, …) do corpo (`to`, `subject`, `html`/`text`, anexos).
3. `InboxAddressBook` resolve o remetente; `ensure_can_send` recusa receive-only.
4. Facade faz `POST /send`. `OutboundMessage.response_id` lê `log_id` (ou fallback) da resposta.

## Configuração

| Modelo | O que você precisa | Para quê |
|--------|-------------------|----------|
| `ErgonPlatformChannelsConfig` | **`address`** + **`config_id`** | Caminho feliz — lê e envia (use `send_address` se a caixa de saída for diferente) |
| `ErgonPlatformChannelsConsumerConfig` | `address` + `config_id` | Só consumir |
| `ErgonPlatformChannelsProducerConfig` | omitir ou só `address` | Só enviar — normalmente vem do unified config |
| `SendMessageInput` | `to`, `subject`, `html`/`text` | Corpo da mensagem |
| `SendMessageAttachment` | `filename`, `content_type`, `content` (base64) | Anexo |

Não informe `address_id` — o connector resolve o UUID a partir do email + `config_id`.

| `ErgonPlatformClient` | Credenciais (`client_id`, `client_secret`, `base_url?`) | Cliente HTTP compartilhado |

## Mapeamento Connector <-> API

| Método do connector | Operação na plataforma |
|---------------------|------------------------|
| `fetch_transactions` / `fetch_transactions_async` | `GET /configs/{config_id}/activity?address_id=...` → `Transaction` por evento |
| `fetch_transaction_by_id` / `fetch_transaction_by_id_async` | `GET /configs/{config_id}/activity/{event_id}` → `Transaction` |
| `get_transactions_count` / `get_transactions_count_async` | Total do feed de activity da inbox |
| `send_email` / `send_email_async` | Atalho: `to`, `subject`, `text`/`html` → `POST /send` |
| `dispatch_transactions` / `dispatch_transactions_async` | `POST /send` para cada `Transaction` (framework Ergon) |
| `send_message` / `send_message_async` | `POST /send` com `SendMessageInput` ou `dict` |
| `ack_transaction` / `nack_transaction` | `POST .../activity/{id}/ack` e `.../nack` (estado em `channel_activity_consumptions`) |
| `download_attachments` | On-demand: lê anexos do `Transaction` e chama `GET .../attachments/{id}/file` |
| `close` | `ErgonClient.close()` |

### API direta do SDK

Para operações fora do contrato do framework, use `connector.client.channels.*`:

| Necessidade | Chamada recomendada |
|-------------|---------------------|
| Listar tipos de canal | `connector.client.channels.channel_types()` |
| Listar endereços | `connector.client.channels.addresses()` / `granted_addresses()` |
| Listar/gerenciar configs | `connector.client.channels.configs.list()` / `get(config_id)` / `create(...)` / `update(...)` |
| Endereços de uma config | `connector.client.channels.configs.addresses(config_id).list()` |
| Verificar config | `connector.client.channels.configs.verify(config_id)` |
| Activity de uma config | `connector.client.channels.configs.activity(config_id)` |
| Grants de endereço | `connector.client.channels.configs.addresses(config_id).grants(address_id)` |
| Webhook inbound de e-mail | `connector.client.channels.webhook_email(data)` |

## Fetch — inbox activity (default)

Consome eventos recebidos na inbox configurada. Com `received_only=True` (default), o connector filtra por `event_type=channels.email.received` (emails inbound). A plataforma expõe `direction=inbound` nos eventos — filtre no app se precisar de lógica extra.

```python
from ergon.connector.ergon_platform import ErgonPlatformClient
from ergon.connector.ergon_platform.channels import (
    ErgonPlatformChannelsConnector,
    ErgonPlatformChannelsConfig,
)

client = ErgonPlatformClient(client_id="ek_...", client_secret="eks_...")

connector = ErgonPlatformChannelsConnector(
    client=client,
    channels_config=ErgonPlatformChannelsConfig(
        address="minha-inbox@inbox.ergondata.ai",
        config_id="uuid-do-console",
    ),
)
try:
    for tx in connector.fetch_transactions():
        print(tx.id, tx.metadata.get("subject"), tx.metadata.get("from_address"))
finally:
    connector.close()
```

Cada `Transaction` retornado carrega:

- `id` = `id` / `log_id` / `provider_message_id` do evento
- `payload` = evento serializado (dict)
- `metadata` = `{ source, event_type, channel, direction, status, thread_id, correlation_id, provider_message_id, subject, from_address, to_addresses, attachments, has_attachment }`
- `metadata["attachments"]` = lista de dicts no mesmo formato do Nylas: `{id, filename, content_type, size}`. O `id` é o `resend_attachment_id` da plataforma. Com `download_attachments=True`, cada item ganha `content` (**bytes**). O payload cru da activity continua em `payload` / `metadata["message_payload"]`.

```python
connector = ErgonPlatformChannelsConnector(
    client=client,
    consumer_config=ErgonPlatformChannelsConsumerConfig(
        address="caixa@inbox.ergondata.ai",
        config_id="...",
        download_attachments=True,
    ),
)
for tx in connector.fetch_transactions():
    for att in tx.metadata.get("attachments") or []:
        print(att["id"], att["filename"], len(att.get("content") or b""))
```

`download_attachments(tx, dest=...)` continua disponível se você quiser gravar em disco depois do fetch.

## Profundidade do feed

`get_transactions_count()` usa `limit=1` para minimizar o payload e lê `total` do feed da inbox. Útil para métricas / backpressure.

## Enviar email

Forma mais simples:

```python
sent_id = connector.send_email(
    to="cliente@empresa.com",
    subject="Olá",
    text="Corpo da mensagem",
)
```

Async:

```python
sent_id = await connector.send_email_async(
    to="cliente@empresa.com",
    subject="Olá",
    html="<p>Corpo da mensagem</p>",
)
```

Com o framework Ergon (`dispatch_transactions`), monte um ``Transaction`` com ``SendMessageInput`` no payload.

## Dispatch (framework Ergon)

`dispatch_transactions` recebe uma lista de `Transaction` cujos payloads são `SendMessageInput` (recomendado) ou `dict` com o mesmo formato do request `channels.send`.

Fluxo:

1. O connector normaliza o payload (``SendMessageInput`` ou ``dict``) em roteamento (`address_id`, `channel`, `resource_id`, `service_name`) e bloco `config` (assunto, corpo, `to`, `cc`, `bcc`, ...).
2. Defaults do `producer_config` / `channels_config` preenchem roteamento (`address`, `channel`, `service_name`, `default_reply_to`). Payload `dict` ainda aceita overrides avançados.
3. `client.channels.send({...})` é chamado.
4. O ID retornado (`log_id` / `provider_message_id` / `thread_id` / `id`) é adicionado à lista de retorno.

```python
from ergon.connector import Transaction
from ergon.connector.ergon_platform import ErgonPlatformClient
from ergon.connector.ergon_platform.channels import (
    ErgonPlatformChannelsConnector,
    ErgonPlatformChannelsConfig,
    SendMessageInput,
)

client = ErgonPlatformClient(client_id="ek_...", client_secret="eks_...")

connector = ErgonPlatformChannelsConnector(
    client=client,
    channels_config=ErgonPlatformChannelsConfig(
        address="minha-inbox@inbox.ergondata.ai",
        config_id="uuid-do-console",
        default_reply_to="ops@empresa.com",
    ),
)

tx = Transaction(
    id="notification-42",
    payload=SendMessageInput(
        to=["cliente@empresa.com"],
        subject="Seu pedido foi aprovado",
        html="<p>Obrigado pelo pedido!</p>",
    ),
)

sent_ids = connector.dispatch_transactions([tx])
connector.close()
```

## Anexos

`SendMessageAttachment` transporta o conteúdo já em base64 (o SDK gera JSON, então bytes precisam ser codificados antes):

```python
import base64
from pathlib import Path

from ergon.connector.ergon_platform.channels import (
    SendMessageAttachment,
    SendMessageInput,
)

path = Path("relatorio.pdf")
attachment = SendMessageAttachment(
    filename=path.name,
    content_type="application/pdf",
    content=base64.b64encode(path.read_bytes()).decode("ascii"),
)

payload = SendMessageInput(
    to=["cliente@empresa.com"],
    subject="Relatório mensal",
    html="<p>Segue anexo.</p>",
    attachments=[attachment],
)
```

## Ack / Nack

A activity continua sendo histórico imutável. O estado de consumo fica em `channel_activity_consumptions` (permissão `channels:activity:consume`).

- `fetch_transactions` envia `pending_only=true` por default — só eventos ainda não ackados (e com `available_at` vencido).
- `ack_transaction` → `POST /configs/{config_id}/activity/{event_id}/ack`
- `nack_transaction(requeue=True, delay_seconds=30)` → nack com requeue; `requeue=False` marca `failed`

```python
await connector.ack_transaction(transaction)
await connector.nack_transaction(transaction, requeue=True, delay_seconds=30)
```

`include_acked=true` lista o histórico (UI). `since` filtra por `created_at`. Dedup em memória (`deduplicate_fetched_events`) é fallback opcional — o ack da plataforma é a fonte da verdade.

## Retrocompatibilidade

Como o connector de channels é novo, não há aliases legados. Os imports oficiais:

```python
from ergon.connector.ergon_platform import (
    ErgonPlatformClient,                       # compartilhado
    ErgonPlatformChannelsConnector,            # sync
    AsyncErgonPlatformChannelsConnector,       # async
    ErgonPlatformChannelsConsumerConfig,
    ErgonPlatformChannelsProducerConfig,
    SendMessageInput,
)
```

Ou direto no subpacote:

```python
from ergon.connector.ergon_platform.channels import (
    ErgonPlatformChannelsConnector,
    ErgonPlatformChannelsConsumerConfig,
    ErgonPlatformChannelsProducerConfig,
    SendMessageInput,
    SendMessageAttachment,
)
```

Veja exemplos em `examples/ergon_platform/channels/`.
