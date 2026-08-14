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
# preencha ERGON_CLIENT_ID, ERGON_CLIENT_SECRET, CHANNELS_CONFIG_ID,
# CHANNELS_INSTRUCTIONS_ADDRESS e CHANNELS_AUTH_CODE_ADDRESS
cd examples/ergon_platform/channels
python config.py
```

## Variáveis de ambiente

| Variável | Obrigatória | Descrição |
|----------|-------------|-----------|
| `ERGON_CLIENT_ID` | sim | API key id (`ek_...`) |
| `ERGON_CLIENT_SECRET` | sim | API key secret (`eks_...`) |
| `ERGON_BASE_URL` | não | Base da API (default produção) |
| `CHANNELS_CONFIG_ID` | sim | UUID do channel (compartilhado pelas duas caixas) |
| `CHANNELS_INSTRUCTIONS_ADDRESS` | sim | Inbox de programações / instruções |
| `CHANNELS_AUTH_CODE_ADDRESS` | sim | Inbox de código de autenticação (mesmo channel) |
| `CHANNELS_BATCH_SIZE` | não | Eventos por fetch (default `20`) |
| `CHANNELS_STREAMING` | não | `true` = polling contínuo; default = one-shot |

```env
CHANNELS_CONFIG_ID=uuid-do-channel
CHANNELS_INSTRUCTIONS_ADDRESS=programacao@inbox.ergondata.ai
CHANNELS_AUTH_CODE_ADDRESS=otp@inbox.ergondata.ai
```

Um channel, duas caixas, **uma** task. O consume loop só olha a caixa de instruções (`consumer`). A caixa de OTP (`auth_code`) fica injetada em `self.auth_code_connector` para ler no meio do fluxo — sem segundo consume.

O channel na plataforma precisa permitir recebimento (`receive` ou `both`). Para envio, use `send_email_async` / `ErgonPlatformChannelsProducerConfig` em outra task — ver README do SDK em `src/ergon/connector/ergon_platform/channels/`.

## Streaming e ack

Com `CHANNELS_STREAMING=true` a task faz polling contínuo (~5 min por batch / fila vazia, ver `policies.py`). O fetch usa `pending_only=true`: eventos já ackados não voltam. Em sucesso a task chama `ack_transaction`; em erro, `nack_transaction(requeue=True)`.

A API key precisa de `channels:activity:view` + `channels:addresses:receive` (leitura e download de anexos) e `channels:activity:consume` (ack/nack) no channel.

## Como a task usa o connector

`task.py` é o exemplo de consumo. Os métodos do connector, nesta ordem:

1. `consume_transactions` → por baixo, `fetch_transactions` (lista pendente, sem corpo)
2. `fetch_transaction_by_id_async(tx.id)` → detalhe: `message_payload` + `attachments`
3. `ack_transaction` / `nack_transaction` → estado na plataforma

Com `download_attachments=True` (`config.py`), o passo 2 já traz `content` em **bytes**. Cada arquivo tem timeout próprio (`attachment_download_timeout`, default 20s) num client HTTP separado — se o CDN da Resend travar, o anexo fica sem `content` e o ack/nack continua.

Para gravar em disco: `await connector.download_attachments(tx, dest="/tmp")`.
