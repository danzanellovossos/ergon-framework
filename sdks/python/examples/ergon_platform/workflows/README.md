# Exemplos — Connector Ergon Platform (Workflows)

Exemplos de uso do `ErgonPlatformConnector` / `AsyncErgonPlatformConnector`, que envolvem o SDK `ergon-platform-sdk` (`ErgonClient.workflows`).

## Instalação

```bash
cd sdks/python
pip install -e '.[ergon-platform]'
cp examples/ergon_platform/workflows/.env.example examples/ergon_platform/workflows/.env
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
| `ERGON_NACK_PHASE_ID` | não | Fase de destino aplicada no nack `requeue=False` |
| `ERGON_UNASSIGNED` | não | `true`/`false` — busca somente unassigned + claim no fetch |
| `ERGON_ASSIGNED_TO` | não | UUID explícito para filtrar itens atribuídos |
| `ERGON_CREATE_PHASE_ID` | não | Fase usada ao criar itens (dispatch) |
| `ERGON_ATTACHMENT_FIELD_ID` | não | Field ID que recebe o anexo |
| `ERGON_PARENT_ITEM_ID` | não | Pai default no exemplo de card filho |

## Scripts

- `uso_direto_async.py` — uso direto do connector (fetch → processa → ack), sem runner. Também percorre os cenários de fetch `unassigned` / principal M2M / `assigned_to` explícito.
- `task_consumer.py` — integração completa com `TaskConfig` + `AsyncConsumerTask` + runner em loop contínuo.
