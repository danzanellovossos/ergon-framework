# Microsoft Outlook / Graph Connector

Connector sync e async para consumir e enviar mensagens do Outlook pela
Microsoft Graph API, com credenciais de aplicação do Microsoft Entra.

O usuário configura pasta e filtros. O connector monta a consulta Graph —
não é necessário conhecer OData nem KQL.

A superfície do SDK cobre leitura, envio e mutações de mensagem com
`Mail.Read`, `Mail.ReadWrite` e `Mail.Send`.

## Instalação

```bash
pip install 'ergon-framework-python[outlook]'
```

Exemplo executável: [`examples/outlook/`](../../../../examples/outlook/)

## Permissões

O aplicativo Entra precisa de permissões **Application** com admin consent:

| Permissão | Uso |
|---|---|
| `Mail.Read` | Listar mensagens, pastas e anexos |
| `Mail.ReadWrite` | Marcar, categorizar, mover e excluir mensagens |
| `Mail.Send` | Enviar, responder e encaminhar |

O connector usa client credentials (`tenant_id`, `client_id`, `client_secret`)
e acessa a mailbox em `user_email`.

## Consumir mensagens

```python
from ergon.connector import ConnectorConfig
from ergon.connector.outlook import (
    AsyncOutlookGraphConnector,
    OutlookAckActionConfig,
    OutlookConsumerConfig,
    OutlookGraphClient,
    OutlookMessageFilter,
    OutlookNackActionConfig,
)

client = OutlookGraphClient(
    tenant_id="...",
    client_id="...",
    client_secret="...",
    user_email="mailbox@example.com",
)

connector_config = ConnectorConfig(
    connector=AsyncOutlookGraphConnector,
    kwargs={
        "client": client,
        "consumer_config": OutlookConsumerConfig(
            folder_id="Inbox",
            filter=OutlookMessageFilter(unread_only=True, has_attachments=True),
            batch_size=20,
            download_attachments=True,
            ack_config=OutlookAckActionConfig(mark_as_read=True),
            nack_config=OutlookNackActionConfig(
                categories=["Ergon processing failed"],
            ),
        ),
    },
)
```

`fetch_transactions_async()` devolve uma `Transaction` por mensagem:

- `payload` é a resposta Graph (incluindo `body` por padrão);
- `metadata` traz campos prontos para a task: `subject`, `from_email`,
  `to_emails`, `received_at`, `body`, `body_type`, `body_preview`, anexos,
  `is_read` e IDs de conversa/pasta.

Quando `download_attachments=True`, anexos `fileAttachment` incluem bytes em
`metadata["attachments"][*]["content"]`.

Filtros comuns não exigem OData:

```python
from datetime import datetime, timezone

from ergon.connector.outlook import OutlookMessageFilter

OutlookMessageFilter(
    unread_only=True,
    has_attachments=True,
    sender="billing@example.com",
    subject_starts_with="Invoice",
    received_after=datetime(2026, 1, 1, tzinfo=timezone.utc),
)
```

`OutlookMessageSearch` pesquisa texto, assunto, remetente e destinatário sem
KQL. Strings em `search` e `filter` continuam como escape hatch.

**Não combine `search` e `filter` na mesma query** — o Graph rejeita. O
connector levanta `ValueError` nesse caso. A ordem padrão é
`receivedDateTime asc` (mais antigo primeiro). Com filtro + ordenação, o
connector adiciona a expressão exigida pelo Graph para evitar
`InefficientFilter`.

Com `unread_only=True` e o `ack_config` acima, mensagens processadas deixam de
ser buscadas porque o connector as marca como lidas.

## Envio

```python
from ergon.connector.outlook import (
    OutlookAttachmentInput,
    OutlookEmailAddress,
    OutlookProducerConfig,
    OutlookSendMessageInput,
)

connector = AsyncOutlookGraphConnector(
    client,
    producer_config=OutlookProducerConfig(save_to_sent_items=True),
)

await connector.send_message(
    OutlookSendMessageInput(
        to=[OutlookEmailAddress(email="recipient@example.com")],
        subject="Report",
        body="<p>Attached.</p>",
        attachments=[OutlookAttachmentInput(file_path="report.pdf")],
    )
)
```

Também é possível fornecer um `dict` no formato `message` do endpoint
`sendMail`, ou despachar via `dispatch_transactions_async()`.

## Ack e nack

`OutlookAckActionConfig` pode marcar a mensagem como lida, movê-la para outra
pasta ou excluí-la. Com `delete=True`, o connector exclui diretamente e ignora
`mark_as_read`; para manter a mensagem recuperável, combine `mark_as_read=True`
com `move_to_folder_id`. Exclusão e movimentação são mutuamente exclusivas.

`nack_transaction(requeue=True)` marca a mensagem como não lida e reinicia a
paginação. `OutlookNackActionConfig` também pode adicionar categorias ou mover
a mensagem para uma pasta de falha. Com `requeue=False`, uma categoria ou pasta
de falha é obrigatória para evitar que a rejeição seja silenciosa.

## Operações extras

Os connectors sync e async expõem:

- `list_attachments` e `save_attachment`;
- `send_message`, `reply`, `reply_all` e `forward`;
- `mark_as_read`, `mark_as_unread`, `set_flag` e `set_categories`;
- `move_message` e `delete_message`;
- `list_mail_folders` e `get_mail_folder`.

Pastas conhecidas não precisam de UUID:

```python
from ergon.connector.outlook import OutlookWellKnownFolder

inbox = await connector.get_mail_folder(OutlookWellKnownFolder.INBOX)
sent = await connector.get_mail_folder(OutlookWellKnownFolder.SENT_ITEMS)
trash = await connector.get_mail_folder(OutlookWellKnownFolder.DELETED_ITEMS)
```

O service renova o token uma vez ao receber `401`. Em respostas `403`, o erro
indica se a operação requer `Mail.Read`, `Mail.ReadWrite` ou `Mail.Send`. Os
demais erros HTTP são propagados para as políticas de retry do Ergon Framework.

## Escopo do connector

O connector mantém somente comportamento genérico de Outlook/Graph. Regras
como aceitar apenas XML/PDF ou rejeitar quando nenhum anexo específico for
encontrado continuam na task da aplicação.
