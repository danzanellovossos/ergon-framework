# Connector Ergon Platform

Connector que integra o framework Ergon com a **Ergon Platform** (API de workflows), envolvendo o SDK [`ergon-platform-sdk`](https://pypi.org/project/ergon-platform-sdk/) (`ErgonClient` / import `ergon_platform`).

## Instalação

```bash
pip install ergon-framework-python
# instale/disponibilize o pacote ergon-platform-sdk no mesmo ambiente
```

## Arquitetura

Segue o padrão em camadas dos demais connectors (Pipefy, RabbitMQ):

```
models.py          -> DTOs Pydantic (client, consumer/producer config, payloads)
service.py         -> Integração sync com o ErgonClient (API completa)
async_service.py   -> Wrapper async (asyncio.to_thread)
connector.py       -> Adapter Ergon sync (ErgonPlatformConnector)
async_connector.py -> Adapter Ergon async (AsyncErgonPlatformConnector)
```

O **connector** implementa só o contrato do framework (`fetch`/`dispatch`/`ack`/`close`), helpers que retornam `Transaction` e `get_transactions_count`. Operações auxiliares da plataforma (claim, comments, pipeline, listagens, etc.) ficam em **`connector.service`**.

## Configuração

| Modelo | Propósito |
|--------|-----------|
| `ErgonPlatformClient` | Credenciais e conexão (`client_id`, `client_secret`, `base_url` (default `https://platform.ergondata.ai`), `company_id?`, `timeout`, `max_retries`) |
| `ErgonPlatformConsumerConfig` | Origem do fetch (`workflow_id`, `phase_id`, `batch_size`, `offset`, `ack_phase_id?`, `nack_phase_id?`, `unassigned`, `list_params`) |
| `ErgonPlatformProducerConfig` | Defaults de criação (`workflow_id?`, `phase_id?`, `attachment_field_id?`, `default_content_type?`, `parent_item_id?`) |
| `CreateItemInput` | Payload de criação de item, com upload opcional de anexo e suporte a `parent_item_id` |

A autenticação usa a API key (M2M): na primeira requisição o SDK troca `client_id`/`client_secret` por um token em `POST /v1/auth/token` e o reusa/renova automaticamente. O `company_id` é inferido do token quando omitido.

## Mapeamento Connector <-> API

| Método do connector | Operação na plataforma |
|---------------------|------------------------|
| `fetch_transactions` / `fetch_transactions_async` | Lista itens de `workflow_id`/`phase_id` -> `Transaction` |
| `fetch_child_transactions` / `fetch_child_transactions_async` | Lista filhos e resolve cada filho em `Transaction` |
| `fetch_items_by_query` | Query filtrada -> `Transaction` |
| `dispatch_transactions` / `dispatch_transactions_async` | Cria item (e faz upload do anexo quando presente) |
| `fetch_transaction_by_id` / `fetch_transaction_by_id_async` | Busca um item por ID -> `Transaction` |
| `get_transactions_count` / `get_transactions_count_async` | Total de itens na fase de consumo (profundidade de fila) |
| `ack_transaction` | Move o item para `ack_phase_id` (no-op se não configurado) |
| `nack_transaction` | `requeue=True` faz `release_item` (com delay opcional); `requeue=False` move para `nack_phase_id` |

### API via `connector.service`

Para demais operações, use `connector.service` (ou `AsyncErgonPlatformService`):

| Método do service | Operação na plataforma |
|-------------------|------------------------|
| `list_workflows` / `list_workflow_phases` / `list_phase_fields` | Leitura auxiliar de workflows e fases (`list_phase_fields` agrega campos da fase + workflow por padrão) |
| `move_item_to_phase` | Roteia um item para outra fase |
| `route_item_to_global_target` | Move para fase de global-timeout (bypass do grafo) |
| `bulk_create_items` | Cria itens em lote (`/items/bulk-create`) |
| `query_items` | Listagem filtrada/paginada (`POST /workflows/{id}/items/query`) |
| `fetch_items_by_query` | Igual a `query_items`, mas converte em `Transaction` |
| `claim_item` / `assign_item` / `assign_item_group` / `release_item` | Ciclo de atribuição |
| `list_item_comments` / `add_item_comment` | Comentários |
| `list_item_events` | Histórico de atividade |
| `get_pipeline_result` | Status/resultado do pipeline de anexos |
| `list_item_children` / `list_item_child_targets` / `get_item_child_capabilities` / `unlink_item_child` | Linhagem de child-items |
| `fetch_child_items` | Filhos resolvidos como `Transaction` (equivalente a `fetch_child_transactions`) |

## Fetch

`fetch_transactions` lista os itens da fase configurada e converte cada um em `Transaction`:

- `id` = ID do item
- `payload` = item serializado (dict)
- `metadata` = `{ workflow_id, phase_id, company_id, title }`

Regras de atribuição no consumo:

- `unassigned=True`: busca apenas cards não atribuídos (`assigned=no`) e tenta `claim` em cada item antes de retornar.
- `unassigned=False` sem `assigned_to` explícito: usa o principal ID da API key (M2M), mantendo o comportamento padrão.
- `unassigned=False` com `assigned_to` em `list_params` (ou no `fetch`): respeita o UUID informado.

### Exemplos de configuração de consumo

```python
from ergon.connector.ergon_platform import ErgonPlatformConsumerConfig

# Caso 1: somente unassigned (faz claim no fetch)
cfg_unassigned = ErgonPlatformConsumerConfig(
    workflow_id="wf-1",
    phase_id="ph-1",
    unassigned=True,
)

# Caso 2: assigned_to implícito (principal id M2M)
cfg_principal = ErgonPlatformConsumerConfig(
    workflow_id="wf-1",
    phase_id="ph-1",
    unassigned=False,
)

# Caso 3: assigned_to explícito (UUID específico)
cfg_explicit = ErgonPlatformConsumerConfig(
    workflow_id="wf-1",
    phase_id="ph-1",
    unassigned=False,
    list_params={"assigned_to": "11111111-2222-3333-4444-555555555555"},
)
```

Nos exemplos em `examples/ergon_platform/`:

- `task_consumer.py` lê `ERGON_UNASSIGNED` e `ERGON_ASSIGNED_TO`.
- `uso_direto_async.py` percorre os 3 cenários de fetch (unassigned, principal M2M, assigned_to explícito).
- `.env.example` documenta as variáveis `ERGON_UNASSIGNED`, `ERGON_ASSIGNED_TO` e `ERGON_NACK_PHASE_ID`.

## Profundidade de fila

`get_transactions_count` lê o `total` retornado pelo SDK na listagem da fase de consumo (`consumer_config.workflow_id` + `phase_id`), com `limit=1` para minimizar payload.

```python
pending = connector.get_transactions_count()
```

## Dispatch (criação de item)

O `payload` da `Transaction` deve ser um `CreateItemInput` ou um `dict` compatível. Quando `attachment` é fornecido:

1. `create_item` cria o item.
2. `item_attachment_upload_url` gera a URL pré-assinada.
3. O arquivo é enviado via `PUT` (`httpx`).
4. `confirm_item_attachment` confirma o upload.

`attachment_field_id` é obrigatório quando há anexo (no payload ou no `producer_config`).

Exemplo (criação de card com attachment):

```python
from ergon.connector import Transaction
from ergon.connector.ergon_platform import (
    CreateItemInput,
    ErgonPlatformClient,
    ErgonPlatformConnector,
    ErgonPlatformProducerConfig,
)

client = ErgonPlatformClient(
    client_id="ek_xxx",
    client_secret="eks_xxx",
    # base_url default = https://platform.ergondata.ai (origem do gateway)
)

producer = ErgonPlatformProducerConfig(
    workflow_id="wf-destino",
    phase_id="phase-entrada",
    attachment_field_id="field-attachment-id",
)

connector = ErgonPlatformConnector(client=client, producer_config=producer)

tx = Transaction(
    id="create-with-attachment-1",
    payload=CreateItemInput(
        title="Card com anexo",
        field_values={"field-text-id": "Documento recebido"},
        attachment="C:/arquivos/documento.pdf",
        # opcional quando já definido no producer_config:
        # attachment_field_id="field-attachment-id",
    ),
    metadata={},
)

created_ids = connector.dispatch_transactions([tx])
print(created_ids)  # ["<item_id>"]

connector.close()
```

### Child item no dispatch

Para criar item filho, informe `parent_item_id`:

- no próprio payload (`CreateItemInput.parent_item_id`), ou
- como default no `ErgonPlatformProducerConfig.parent_item_id`.

Quando os dois estiverem presentes, o valor do payload prevalece.

Exemplo:

```python
from ergon.connector import Transaction
from ergon.connector.ergon_platform import CreateItemInput

tx = Transaction(
    id="create-child-1",
    payload=CreateItemInput(
        title="Filho",
        workflow_id="wf-destino",
        phase_id="phase-destino",
        parent_item_id="item-pai-123",
    ),
    metadata={},
)
```

## Child items (fetch e linhagem)

Além do fetch por fase (`fetch_transactions`), o connector expõe `fetch_child_transactions(parent_item_id, **params)` que retorna filhos como `Transaction`.

Para operações de linhagem (links, targets, capabilities, unlink), use `connector.service`:

```python
links = connector.service.list_item_children("parent-1")
targets = connector.service.list_item_child_targets("parent-1")
caps = connector.service.get_item_child_capabilities("parent-1")
connector.service.unlink_item_child("parent-1", "child-1")
```

Observação: `fetch_child_transactions` resolve os filhos via leitura por item ID (um lookup por filho), priorizando simplicidade da API do connector.

## Operações de item via service

Exemplo (consumo filtrado por `query` no connector):

```python
txns = connector.fetch_items_by_query(
    "wf-1",
    {"filters": [{"field_id": "campo-status", "operator": "eq", "value": "novo"}]},
)
```

Outras operações via service:

```python
connector.service.bulk_create_items("wf-1", [{"title": "A"}])
connector.service.claim_item("item-1")
connector.service.add_item_comment("item-1", {"body": "ok"})
result = connector.service.get_pipeline_result("wf-1", "item-1", "field-1")
```

## Pipeline de anexos

`connector.service.get_pipeline_result(workflow_id, item_id, field_id, buckets_file_id=None)` resolve o `buckets_file_id` (se não informado), consulta o status e classifica o estado em `success` / `failed` / `processing` / `unknown`. Em `success`, retorna também `results`.

## Ack

Ao contrário dos brokers, o ack tem **semântica de domínio**: move o item para `ack_phase_id`. Sem `ack_phase_id` configurado, o ack é no-op. Em uma `AsyncConsumerTask`, chame `ack_transaction` em `handle_process_success`.

## Nack

`nack_transaction` também usa semântica de domínio:

- `requeue=True`: faz `release_item` com `delay_seconds` opcional (o controle de delay é aplicado pela plataforma), mantendo o mesmo card.
- `requeue=False`: move o item para `nack_phase_id` (fase de erro), que deve estar configurada no `ErgonPlatformConsumerConfig`.

## Recomendações de uso

- Use `fetch_transactions` quando a unidade de consumo for fase (`workflow_id` + `phase_id`).
- Use `fetch_child_transactions` quando a unidade de consumo for linhagem (filhos de um item pai).
- Prefira definir defaults no `producer_config` e sobrescrever por payload apenas quando necessário.
- Para API da plataforma fora do contrato do framework, acesse `connector.service.*`.
- Para cenários com alto volume de filhos, pagine via `**params` (por exemplo `limit`/`offset`).

Veja exemplos em `examples/ergon_platform/`.
