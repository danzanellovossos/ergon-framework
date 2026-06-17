# Connector Ergon Platform

Connector que integra o framework Ergon com a **Ergon Platform** (API de workflows), envolvendo o SDK [`ergon-platform-sdk`](https://pypi.org/project/ergon-platform-sdk/) (`ErgonClient` / import `ergon_platform`).

## Instalação

```bash
pip install ergon-framework-python[ergon_platform]
```

## Arquitetura

Segue o padrão em camadas dos demais connectors:

```
models.py          -> DTOs Pydantic (client, consumer/producer config, payloads)
service.py         -> Integração sync com o ErgonClient
async_service.py   -> Wrapper async (asyncio.to_thread)
connector.py       -> Adapter Ergon sync (ErgonPlatformConnector)
async_connector.py -> Adapter Ergon async (AsyncErgonPlatformConnector)
```

## Configuração

| Modelo | Propósito |
|--------|-----------|
| `ErgonPlatformClient` | Credenciais e conexão (`client_id`, `client_secret`, `base_url`, `company_id?`, `timeout`, `max_retries`) |
| `ErgonPlatformConsumerConfig` | Origem do fetch (`workflow_id`, `phase_id`, `batch_size`, `offset`, `ack_phase_id?`, `list_params`) |
| `ErgonPlatformProducerConfig` | Defaults de criação (`workflow_id?`, `phase_id?`, `attachment_field_id?`, `default_content_type?`, `parent_item_id?`) |
| `CreateItemInput` | Payload de criação de item, com upload opcional de anexo e suporte a `parent_item_id` |

A autenticação usa a API key (M2M): na primeira requisição o SDK troca `client_id`/`client_secret` por um token em `POST /v1/auth/token` e o reusa/renova automaticamente. O `company_id` é inferido do token quando omitido.

## Mapeamento Connector <-> API

| Método do connector | Operação na plataforma |
|---------------------|------------------------|
| `fetch_transactions` / `fetch_transactions_async` | Lista itens de `workflow_id`/`phase_id` -> `Transaction` |
| `fetch_child_transactions` / `fetch_child_transactions_async` | Lista filhos (`/items/{parent}/children`) e resolve cada filho em `Transaction` |
| `dispatch_transactions` / `dispatch_transactions_async` | Cria item (e faz upload do anexo quando presente) |
| `fetch_transaction_by_id` / `fetch_transaction_by_id_async` | Busca um item por ID -> `Transaction` |
| `ack_transaction` | Move o item para `ack_phase_id` (no-op se não configurado) |
| `nack_transaction` | No-op (o item permanece na fase atual) |
| `move_item_to_phase` | Roteia um item para outra fase |
| `get_pipeline_result` | Consulta o status/resultado do pipeline de anexos |
| `list_item_children` / `list_item_child_targets` / `get_item_child_capabilities` / `unlink_item_child` | Superfície de linhagem de child-items |
| `list_workflows` / `list_workflow_phases` / `list_phase_fields` | Métodos de leitura auxiliares |

## Fetch

`fetch_transactions` lista os itens da fase configurada e converte cada um em `Transaction`:

- `id` = ID do item
- `payload` = item serializado (dict)
- `metadata` = `{ workflow_id, phase_id, company_id, title }`

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
    base_url="https://api.seu-dominio.com",
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

Além do fetch por fase (`fetch_transactions`), o connector expõe uma superfície
dedicada de child-items:

- `fetch_child_transactions(parent_item_id, **params)`: retorna os filhos como `Transaction`.
- `list_item_children(item_id, **params)`: retorna os links de filhos.
- `list_item_child_targets(item_id, **params)`: retorna a árvore de destinos elegíveis.
- `get_item_child_capabilities(item_id, **params)`: retorna `{can_create, can_view, can_unlink}`.
- `unlink_item_child(item_id, child_item_id)`: remove o vínculo pai->filho (não deleta o item filho).

Observação: `fetch_child_transactions` resolve os filhos via leitura por item ID
(um lookup por filho), priorizando simplicidade da API do connector.

## Pipeline de anexos

`get_pipeline_result(workflow_id, item_id, field_id, buckets_file_id=None)` resolve o `buckets_file_id` (se não informado), consulta o status e classifica o estado em `success` / `failed` / `processing` / `unknown`. Em `success`, retorna também `results`.

## Ack

Ao contrário dos brokers, o ack tem **semântica de domínio**: move o item para `ack_phase_id`. Sem `ack_phase_id` configurado, o ack é no-op. Em uma `AsyncConsumerTask`, chame `ack_transaction` em `handle_process_success`.

## Recomendações de uso

- Use `fetch_transactions` quando a unidade de consumo for fase (`workflow_id` + `phase_id`).
- Use `fetch_child_transactions` quando a unidade de consumo for linhagem (filhos de um item pai).
- Prefira definir defaults no `producer_config` e sobrescrever por payload apenas quando necessário.
- Para cenários com alto volume de filhos, pagine via `**params` (por exemplo `limit`/`offset`).

Veja exemplos em `examples/ergon_platform/`.
