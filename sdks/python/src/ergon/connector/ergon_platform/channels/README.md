# Connector Ergon Platform — Channels

Connector que integra o framework Ergon com o módulo **Channels** da Ergon Platform (envio de mensagens, thread messages e feed de atividade), envolvendo o SDK [`ergon-platform-sdk`](https://pypi.org/project/ergon-platform-sdk/) (`ErgonClient.channels`).

## Instalação

```bash
pip install 'ergon-framework-python[ergon-platform]'
```

O extra `ergon-platform` cobre workflows **e** channels e exige
`ergon-platform-sdk>=0.2.0`, primeira versão com claim/lease e attachment file.

## Arquitetura

O connector é o adapter público do framework (`fetch_transactions`, `send_email`, `ack_transaction`). Ele não fala com a API sozinho: compõe `ErgonPlatformChannelsService`, que agrupa services menores por domínio. Sync e async compartilham esses Services — o async só envolve as chamadas em `asyncio.to_thread`.

```
task / app
    │
    ▼
ErgonPlatformChannelsConnector  (sync)     AsyncErgonPlatformChannelsConnector
    │                                         │
    └──────────────┬──────────────────────────┘
                   ▼
         ErgonPlatformChannelsService        ← composição
                   │
     ┌─────────────┼──────────────┬────────────────┐
     ▼             ▼              ▼                ▼
 ActivityService  AddressService  AttachmentService  MessageService
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
    ├── adapters.py         # Evento da plataforma → Transaction
    ├── services/           # Operações HTTP separadas por domínio
    │   ├── activity.py     # History, claim, ack/nack e lease
    │   ├── addresses.py    # Resolução de inbox
    │   ├── attachments.py  # Hydrate e download
    │   ├── messages.py     # Threads e envio
    │   ├── records.py      # Adapter das respostas do SDK
    │   └── service.py      # Composição dos services
    └── utils.py            # Helpers gerais compartilhados
```

O connector reusa a mesma factory de `ErgonClient` do workflows. Toda ida à rede passa por `connector.client.channels.*`.

### Padrões


| Padrão | Onde | O que resolve |
|--------|------|----------------|
| **Services por domínio** | `services/` | Activity, addresses, attachments e messages não ficam concentrados em uma classe única nem espalhados pelo sync/async. |
| **Adapter** | `SdkRecord` | O SDK devolve dict, objeto Pydantic ou Page. `get` / `items` / `total` / `serialize` escondem isso. |
| **Adapter** | `ActivityAdapter` | Evento da activity da plataforma → `Transaction` do framework (`id`, `payload`, `metadata`). |
| **Strategy** | `ChannelsActivityFilter.matches` / `select` | O filtro client-side (`from_address`, `subject_contains`) vive no próprio filtro. O fetch só pergunta “esta transação entra?”. |
| **Builder** | `ChannelsMessageService.normalize_send_payload` | `SendMessageInput` ou `dict` vira o body de `POST /send` (roteamento `top` + bloco `config`). |

Não há Factory/Singleton/Visitor aqui — o `ErgonClient` já é criado em `_client.py`, compartilhado com workflows.

### Responsabilidades

| Peça | Arquivo | Faz | Não faz |
|------|---------|-----|---------|
| `ErgonPlatformChannelsConnector` | `connector.py` | Contrato do framework: fetch, send, ack/nack, hydrate se `download_attachments=True`, estado `_seen_event_ids`. | Montar JSON da API, parsear Page, achar `address_id`. |
| `AsyncErgonPlatformChannelsConnector` | `async_connector.py` | O mesmo contrato, I/O em thread. | Lógica de domínio diferente da sync. |
| `ErgonPlatformChannelsService` | `services/service.py` | Compartilha o client e compõe `activity`, `addresses`, `attachments` e `messages`. | Concentrar as operações de todos os domínios. |
| `ChannelsActivityService` | `services/activity.py` | Histórico, paginação da claim e settlement de leases. | Resolver endereços ou enviar mensagens. |
| `ChannelsAddressService` | `services/addresses.py` | Resolução e cache de inbox. | Consumir activity. |
| `ChannelsAttachmentService` | `services/attachments.py` | Hydrate e download seguro de anexos. | Decidir a política do consumer. |
| `ChannelsMessageService` | `services/messages.py` | Ler threads, normalizar payload e enviar mensagens. | Gerenciar leases. |
| `SdkRecord` | `services/records.py` | Ler um payload heterogêneo (`obj.get` vs `getattr`, lista em `items`/`data`/`messages`). | Conhecer activity, inbox ou send. |
| `ActivityAdapter` | `adapters.py` | `to_transaction`, anexos no metadata e preservação do lease. | Chamar HTTP. |
| `ChannelsActivityFilter` | `models.py` | Define a identidade/filtro estável da subscription; não-matches são ackados só nessa subscription. | HTTP. |
| `ResolvedInboxAddress` | `models.py` | `can_send` / `can_receive` e erros claros (`ensure_can_send`, `ensure_can_receive`). | Buscar o endereço na API. |

### Fluxo de fetch

1. Connector exige `consumer_config`, pede o inbox a `ChannelsAddressService` e recusa send-only (`ensure_can_receive`).
2. O Service chama `POST /configs/{id}/activity/claims` com `subscription_id`, `consumer_id`, limite, cursor e visibility timeout.
3. Cada item já traz o `ActivityLogDetail` completo e o lease; `ActivityAdapter.claimed_transaction` preserva ambos na mesma `Transaction`.
4. O Service percorre cursores até completar o batch. Eventos fora do filtro/endereço são ackados apenas nessa subscription, então não bloqueiam eventos válidos posteriores.
5. Se `download_attachments=True`, `ChannelsAttachmentService` busca os bytes com até quatro downloads concorrentes por batch, preservando a ordem de eventos e anexos. Falha faz o fetch requeue de todas as claims e propaga a exceção por padrão. `attachment_failure_policy="best_effort"` é explícito, expõe `metadata["attachment_failures"]` e bloqueia ACK sem `allow_attachment_failures=True`.

`fetch_transaction_by_id` continua como leitura avulsa de detalhe, mas não cria lease e portanto não pode ser usado para ACK/NACK.

### Fluxo de send

1. Connector monta `SendMessageInput` (ou recebe `dict`).
2. `ChannelsMessageService.normalize_send_payload` separa roteamento (`address_id`, `channel`, …) do corpo (`to`, `subject`, `html`/`text`, anexos).
3. `ChannelsAddressService` resolve o remetente; `ensure_can_send` recusa receive-only.
4. `ChannelsMessageService` faz `POST /send` e extrai o `log_id` (ou fallback) da resposta.

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
| `fetch_transactions` / `fetch_transactions_async` | `POST /configs/{config_id}/activity/claims` → evento completo + lease por `Transaction` |
| `fetch_transaction_by_id` / `fetch_transaction_by_id_async` | `GET /configs/{config_id}/activity/{event_id}` → `Transaction` |
| `get_transactions_count` / `get_transactions_count_async` | Total do feed de activity da inbox |
| `send_email` / `send_email_async` | Atalho: `to`, `subject`, `text`/`html` → `POST /send` |
| `dispatch_transactions` / `dispatch_transactions_async` | `POST /send` para cada `Transaction` (framework Ergon) |
| `send_message` / `send_message_async` | `POST /send` com `SendMessageInput` ou `dict` |
| `ack_transaction` / `nack_transaction` | `POST .../activity/{id}/ack` e `.../nack` com `subscription_id` + `lease_token` |
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
- `metadata` = `{ source, event_type, channel, direction, status, thread_id, correlation_id, provider_message_id, subject, from_address, to_addresses, attachments, has_attachment, delivery }`
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

`download_attachments(tx, dest=...)` continua disponível para gravação. Nomes
absolutos/drive/UNC são rejeitados, controles Unicode são neutralizados, a
contenção em `{dest}/{event_id}` é validada e o `attachment_id` desambigua nomes.

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

A activity continua sendo histórico imutável. O estado de consumo é independente por `subscription_id` e fica em `channel_activity_consumptions` (permissão `channels:activity:consume`).

- `fetch_transactions` cria leases atômicos via `/activity/claims`; réplicas da mesma subscription não recebem o mesmo evento.
- `ack_transaction` envia o `subscription_id` + `lease_token` guardados em `metadata["delivery"]`.
- `nack_transaction(requeue=True, delay_seconds=30)` libera para retry; `requeue=False` marca a delivery como `failed`.

```python
await connector.ack_transaction(transaction)
await connector.nack_transaction(transaction, requeue=True, delay_seconds=30)
```

O feed histórico (`GET /activity`) não é usado como fila. Dedup em memória (`deduplicate_fetched_events`) é fallback opcional; a claim da plataforma é a fonte da verdade.

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
