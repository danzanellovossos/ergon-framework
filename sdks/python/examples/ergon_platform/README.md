# Exemplos — Connector Ergon Platform

Exemplos de uso dos connectors da **Ergon Platform**. Cada sub-connector tem seu próprio conjunto de scripts e `.env.example` em uma subpasta.

## Instalação

```bash
cd sdks/python
pip install -e '.[ergon-platform]'
```

O extra `ergon-platform` cobre workflows **e** channels — não precisa instalar nada adicional.

## Sub-connectors

| Sub-connector | Pasta | Descrição |
|---------------|-------|-----------|
| Workflows | [`workflows/`](workflows/) | Consumo de itens de fase, dispatch de itens (com anexo), ack/nack, filhos, pipeline de anexo |
| Channels | [`channels/`](channels/) | Consumo do feed de atividade / mensagens de thread; envio de mensagens (`channels.send`) |

Cada pasta traz:

- `.env.example` — variáveis específicas do sub-connector (copie para `.env` e preencha)
- `uso_direto_async.py` — uso direto do connector (fetch → processa → dispatch/ack), sem runner
- `task_consumer.py` — integração com `TaskConfig` + `AsyncConsumerTask` + `run_task` em loop contínuo
- `README.md` — descrição das variáveis de ambiente e do que cada script faz

## Credenciais compartilhadas

Todos os sub-connectors usam o mesmo `ErgonPlatformClient` (API key da Ergon Platform). Você pode reaproveitar as credenciais entre os `.env` de cada subpasta.
