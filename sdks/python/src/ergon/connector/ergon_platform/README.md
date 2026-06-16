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
| `ErgonPlatformProducerConfig` | Defaults de criação (`workflow_id?`, `phase_id?`, `attachment_field_id?`, `default_content_type?`) |
| `CreateItemInput` | Payload de criação de item, com upload opcional de anexo |

A autenticação usa a API key (M2M): na primeira requisição o SDK troca `client_id`/`client_secret` por um token em `POST /v1/auth/token` e o reusa/renova automaticamente. O `company_id` é inferido do token quando omitido.

## Mapeamento Connector <-> API

| Método do connector | Operação na plataforma |
|---------------------|------------------------|
| `fetch_transactions` / `fetch_transactions_async` | Lista itens de `workflow_id`/`phase_id` -> `Transaction` |
| `dispatch_transactions` / `dispatch_transactions_async` | Cria item (e faz upload do anexo quando presente) |
| `fetch_transaction_by_id` | Busca um item por ID -> `Transaction` |
| `ack_transaction` | Move o item para `ack_phase_id` (no-op se não configurado) |
| `nack_transaction` | No-op (o item permanece na fase atual) |
| `move_item_to_phase` | Roteia um item para outra fase |
| `get_pipeline_result` | Consulta o status/resultado do pipeline de anexos |
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

## Pipeline de anexos

`get_pipeline_result(workflow_id, item_id, field_id, buckets_file_id=None)` resolve o `buckets_file_id` (se não informado), consulta o status e classifica o estado em `success` / `failed` / `processing` / `unknown`. Em `success`, retorna também `results`.

## Ack

Ao contrário dos brokers, o ack tem **semântica de domínio**: move o item para `ack_phase_id`. Sem `ack_phase_id` configurado, o ack é no-op. Em uma `AsyncConsumerTask`, chame `ack_transaction` em `handle_process_success`.

Veja exemplos em `examples/ergon_platform/`.
