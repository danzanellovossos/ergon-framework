# Exemplo — Outlook / Microsoft Graph

```bash
cd sdks/python
uv sync --extra outlook
cp examples/outlook/.env.example examples/outlook/.env
uv run python examples/outlook/config.py
```

Configure no Entra ID uma aplicação com client credentials, admin consent,
`Mail.Read`, `Mail.ReadWrite` e `Mail.Send`. Preencha a mailbox em
`OUTLOOK_USER_EMAIL`.

`task.py` processa cada mensagem. `config.py` monta o client, o connector e a
consumer policy, no mesmo padrão do exemplo de Channels.

O exemplo busca **não lidas com anexo** na Inbox, baixa os anexos e grava
em `examples/outlook/downloads` (ou no caminho de `OUTLOOK_ATTACHMENT_DIR`).
Após sucesso, o ack marca a mensagem como lida. Em falha, o nack mantém a
mensagem não lida, adiciona a categoria `Ergon processing failed` e reinicia a
paginação para permitir nova tentativa.

Regras específicas da aplicação — por exemplo, aceitar somente XML/PDF —
devem permanecer na task, não no connector.
