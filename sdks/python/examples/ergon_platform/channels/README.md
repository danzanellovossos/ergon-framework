# Exemplos — Connector Ergon Platform (Channels)

Exemplo de **consumo** de inbox com `AsyncErgonPlatformChannelsConnector` + `AsyncConsumerTask`.

| Arquivo | Papel |
|---------|-------|
| `env.py` | Variáveis do `.env` |
| `policies.py` | `ConsumerPolicy` do fetch |
| `task.py` | `ChannelsEventTask` — processa cada evento recebido |
| `config.py` | Wire-up + entry point (`python config.py`) |

## Rodar

```bash
cd sdks/python
pip install -e '.[ergon-platform]'
cp examples/ergon_platform/channels/.env.example examples/ergon_platform/channels/.env
# preencha ERGON_CLIENT_ID, ERGON_CLIENT_SECRET, CHANNELS_ADDRESS, CHANNELS_CONFIG_ID
cd examples/ergon_platform/channels
python config.py
```

## Variáveis de ambiente

| Variável | Obrigatória | Descrição |
|----------|-------------|-----------|
| `ERGON_CLIENT_ID` | sim | API key id (`ek_...`) |
| `ERGON_CLIENT_SECRET` | sim | API key secret (`eks_...`) |
| `ERGON_BASE_URL` | não | Base da API (default produção) |
| `CHANNELS_ADDRESS` | sim | Inbox com permissão de **recebimento** |
| `CHANNELS_CONFIG_ID` | sim | UUID do channel na plataforma |
| `CHANNELS_BATCH_SIZE` | não | Eventos por fetch (default `20`) |
| `CHANNELS_STREAMING` | não | `true` = polling contínuo; default = one-shot |

```env
CHANNELS_ADDRESS=teste@inbox.ergondata.ai
CHANNELS_CONFIG_ID=uuid-do-channel
```

O channel na plataforma precisa permitir recebimento (`receive` ou `both`). Para envio, use `send_email_async` / `ErgonPlatformChannelsProducerConfig` em outra task — ver README do SDK em `src/ergon/connector/ergon_platform/channels/`.

## Streaming e ack

Com `CHANNELS_STREAMING=true` a task faz polling contínuo (~5s quando a fila está vazia, ver `policies.py`). O fetch usa `pending_only=true`: eventos já ackados não voltam. Em sucesso a task chama `ack_transaction`; em erro, `nack_transaction(requeue=True)`.

A API key precisa de `channels:activity:view` + `channels:addresses:receive` (leitura e download de anexos) e `channels:activity:consume` (ack/nack) no channel.

## Anexos

O payload do evento traz só metadados. A task chama `download_attachments(transaction, dest=downloads)` — o connector resolve ids/filenames e grava em `downloads/{event_id}/{filename}`.
