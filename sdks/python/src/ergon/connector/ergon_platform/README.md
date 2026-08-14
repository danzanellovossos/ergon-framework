# Connector Ergon Platform

Grupo de connectors que integram o framework Ergon com a **Ergon Platform**, através do SDK [`ergon-platform-sdk`](https://pypi.org/project/ergon-platform-sdk/) (`ErgonClient` / import `ergon_platform`). Cada serviço da plataforma tem seu próprio connector, mas todos compartilham as credenciais e a factory do `ErgonClient`.

## Instalação

```bash
pip install 'ergon-framework-python[ergon-platform]'
```

O extra `ergon-platform` traz `ergon-platform-sdk` + `httpx` e cobre **todos** os sub-connectors.

## Organização

```
ergon_platform/
├── models.py     # ErgonPlatformClient (credenciais compartilhadas)
├── _client.py    # Factory compartilhada do ErgonClient
├── workflows/    # Connector do módulo Workflows (itens, fases, anexos, ...)
└── channels/     # Connector do módulo Channels (send, thread messages, activity, ...)
```

Um único `ErgonPlatformClient` alimenta os dois connectors — basta reaproveitar a mesma API key para instanciar `workflows.ErgonPlatformConnector` e `channels.ErgonPlatformChannelsConnector`.

## Sub-connectors

| Sub-connector | Documentação | Principais classes |
|---------------|--------------|--------------------|
| Workflows | [`workflows/README.md`](workflows/README.md) | `ErgonPlatformConnector`, `AsyncErgonPlatformConnector`, `ErgonPlatformConsumerConfig`, `ErgonPlatformProducerConfig`, `CreateItemInput` |
| Channels | [`channels/README.md`](channels/README.md) | `ErgonPlatformChannelsConnector`, `AsyncErgonPlatformChannelsConnector`, `ErgonPlatformChannelsConfig`, `SendMessageInput` |

## Credenciais compartilhadas

`ErgonPlatformClient` continua exposto no pacote raiz e serve para **todos** os sub-connectors:

```python
from ergon.connector.ergon_platform import ErgonPlatformClient
from ergon.connector.ergon_platform.workflows import ErgonPlatformConnector
from ergon.connector.ergon_platform.channels import ErgonPlatformChannelsConnector

client_config = ErgonPlatformClient(
    client_id="ek_...",
    client_secret="eks_...",
    # base_url default = https://platform.ergondata.ai
)

workflows_conn = ErgonPlatformConnector(client=client_config, ...)
channels_conn = ErgonPlatformChannelsConnector(client=client_config, ...)
```

Cada connector instancia sua própria conexão `ErgonClient` (nada é compartilhado além da configuração). Isso mantém o modelo de ciclo de vida por worker do framework.

## Retrocompatibilidade

Todos os símbolos de workflows continuam expostos direto no pacote raiz — os imports antigos não quebram:

```python
# Continua funcionando exatamente como antes
from ergon.connector.ergon_platform import (
    ErgonPlatformClient,
    ErgonPlatformConnector,
    AsyncErgonPlatformConnector,
    ErgonPlatformConsumerConfig,
    ErgonPlatformProducerConfig,
    CreateItemInput,
)
```

Para código novo, prefira importar do sub-pacote correspondente. Isso deixa explícito qual serviço da plataforma está em uso e evita coincidência de nomes entre sub-connectors:

```python
from ergon.connector.ergon_platform import ErgonPlatformClient
from ergon.connector.ergon_platform.workflows import (
    ErgonPlatformConnector,
    ErgonPlatformConsumerConfig,
    CreateItemInput,
)
from ergon.connector.ergon_platform.channels import (
    ErgonPlatformChannelsConnector,
    ErgonPlatformChannelsConfig,
    SendMessageInput,
)
```

## Exemplos

- Workflows: [`examples/ergon_platform/workflows/`](../../../../examples/ergon_platform/workflows/)
- Channels: [`examples/ergon_platform/channels/`](../../../../examples/ergon_platform/channels/)
