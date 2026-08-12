# Exemplos — Connector Nylas

Exemplos executáveis de uso do connector Nylas no Ergon Framework.

Documentação de referência do módulo: [`src/ergon/connector/nylas/README.md`](../../src/ergon/connector/nylas/README.md)

## Pré-requisitos

1. Conta [Nylas](https://www.nylas.com/) com API key
2. Callback URI registrado no [Nylas Dashboard](https://developer.nylas.com/docs/v3/auth/) (Hosted Authentication > Callback URIs)
3. SDK instalado com dependência Nylas:

```bash
cd sdks/python
pip install -e ".[nylas]"
```

## Configuração

Copie o template de variáveis e preencha os valores:

```bash
cp examples/nylas/.env.example examples/nylas/.env
```

| Variável | Obrigatória | Descrição |
|----------|-------------|-----------|
| `NYLAS_API_KEY` | Sim | API key do dashboard Nylas |
| `NYLAS_GRANT_ID` | Sim (e-mail) | ID do grant da caixa conectada |
| `NYLAS_REDIRECT_URI` | Sim (auth) | Callback OAuth, ex.: `http://localhost:5000/oauth/callback` |
| `NYLAS_CLIENT_ID` | Não | Application ID (se diferente da API key) |
| `NYLAS_PROVIDER` | Não | Provedor fixo: `google`, `microsoft`, `imap`, ... |
| `NYLAS_LOGIN_HINT` | Não | E-mail sugerido no login |
| `NYLAS_API_URI` | Não | Default: `https://api.us.nylas.com` |
| `NYLAS_INBOX_FOLDER_ID` | Não | ID da pasta inbox |
| `NYLAS_PROCESSED_FOLDER_ID` | Não | Pasta destino após ack |
| `NYLAS_SUBJECT_FILTER` | Não | Filtro de assunto na API (case-sensitive) |

## Configuração do connector (resumo)

Filtros mais usados em `NylasConsumerConfig`:

- `unread=True` — apenas mensagens não lidas
- `has_attachment=True` — mensagens com anexos
- `in_="<FOLDER_ID>"` — pasta ou label (use `service.list_folders()` para descobrir IDs)
- `download_attachments=True` — baixa bytes dos anexos no fetch
- `ack_config=AckActionConfig(mark_as_read=True, move_to_folder_id="...")` — ações pós-processamento; use `delete=True` para hard-delete no ack de mensagens (habilite **Enable hard delete** no Nylas Dashboard)

Envio via `NylasProducerConfig(send_mode="send")` ou `"draft"`. O framework **não** faz ack automático para Nylas — chame `ack_transaction` em `handle_process_success`.

Detalhes de filtros, fluxo fetch→ack e limitações: [README do módulo](../../src/ergon/connector/nylas/README.md).

## Scripts

### 0. `auth_flow.py` — obter grant_id (autenticação)

Use este script **antes** dos demais, se ainda não tiver um `NYLAS_GRANT_ID`.

**Etapa 1 — gerar URL:**

```bash
python examples/nylas/auth_flow.py --generate-url
```

Abra a URL no navegador, autentique a caixa de e-mail e copie o parâmetro `code` da URL de redirect.

**Etapa 2 — trocar code por grant_id:**

```bash
python examples/nylas/auth_flow.py --exchange-code "CODE_DO_REDIRECT"
```

Cole o `NYLAS_GRANT_ID` exibido no seu `.env`.

### 1. `uso_direto_async.py` — uso direto do connector

Ideal para validar credenciais e testar filtros rapidamente, sem o runner do framework.

```bash
python examples/nylas/uso_direto_async.py
```

Fluxo:
1. Lista pastas (se `NYLAS_INBOX_FOLDER_ID` não estiver definido)
2. Busca mensagens não lidas com anexos
3. Imprime assunto, remetente e anexos
4. Aplica `ack_transaction`

### 2. `task_consumer.py` — integração com o framework

Demonstra `AsyncConsumerTask` + `TaskConfig` + `run_task`.

```bash
python examples/nylas/task_consumer.py
```

A task implementa:
- `process_transaction` — lógica de negócio
- `handle_process_success` — chama `ack_transaction` (o framework não faz ack automático para Nylas)
- `execute` — delega ao loop `consume_transactions`

## Uso programático da autenticação

```python
from ergon.connector.nylas import (
    AuthUrlConfig,
    CodeExchangeInput,
    NylasAuthClient,
    NylasAuthService,
)

auth = NylasAuthService(NylasAuthClient(api_key="...", client_id="..."))
url = auth.generate_auth_url(AuthUrlConfig(redirect_uri="http://localhost:5000/oauth/callback"))
result = auth.exchange_code_for_token(CodeExchangeInput(code="...", redirect_uri="http://localhost:5000/oauth/callback"))
print(result.grant_id)
```

## Limitações

Ver a seção completa em [README do módulo](../../src/ergon/connector/nylas/README.md). Em resumo:

- O filtro `subject` da API Nylas é **case-sensitive**
- `auth_flow.py` não inclui servidor HTTP — copie o `code` manualmente da URL de redirect
- Após obter o `grant_id`, use `NylasClient` nos connectors de e-mail normalmente
