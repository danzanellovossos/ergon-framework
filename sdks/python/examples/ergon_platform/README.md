# Exemplos — Connector Ergon Platform

Exemplos de uso do `ErgonPlatformConnector` / `AsyncErgonPlatformConnector`, que envolvem o SDK `ergon-platform-sdk` (`ErgonClient`).

## Instalação

```bash
cd sdks/python
pip install -e .
# instale/disponibilize o pacote ergon-platform-sdk no mesmo ambiente
cp examples/ergon_platform/.env.example examples/ergon_platform/.env
# preencha as credenciais e IDs de workflow/fase no .env
```

## Variáveis de ambiente

| Variável | Obrigatória | Descrição |
|----------|-------------|-----------|
| `ERGON_CLIENT_ID` | sim | API key id (`ek_...`) |
| `ERGON_CLIENT_SECRET` | sim | API key secret (`eks_...`) |
| `ERGON_COMPANY_ID` | não | Inferido do token quando omitido |
| `ERGON_WORKFLOW_ID` | sim | Workflow consumido |
| `ERGON_PHASE_ID` | sim | Fase de onde os itens são lidos |
| `ERGON_ACK_PHASE_ID` | não | Fase de destino aplicada no ack |
| `ERGON_CREATE_PHASE_ID` | não | Fase usada ao criar itens (dispatch) |
| `ERGON_ATTACHMENT_FIELD_ID` | não | Field ID que recebe o anexo |

## Scripts

- `uso_direto_async.py` — uso direto do connector (fetch → processa → ack), sem runner.
- `task_consumer.py` — integração completa com `TaskConfig` + `AsyncConsumerTask` + runner em loop contínuo.
