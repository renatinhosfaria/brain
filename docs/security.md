# Segurança

## Modelo De Confiança

O Brain assume que operadores controlam o deployment, a configuração de runtime e todos os segredos usados pela aplicação. Esses operadores têm acesso ao ambiente, ao banco, ao repositório do vault e aos mecanismos de rotação de credenciais.

Hermes, no papel de curador, é confiável para revisar notas brutas recebidas na inbox e criar ou atualizar notas curadas. Esse principal administra clientes de agente, ciclo de vida de notas brutas e operações de manutenção expostas pelo MCP.

Clientes agentes são escritores parcialmente confiáveis. Eles podem enviar conteúdo bruto apenas para o próprio caminho de inbox, normalmente sob `_agents/{client_slug}/`, e não devem conseguir escrever notas curadas nem acessar caminhos brutos de outros clientes.

Clientes MCP nunca devem receber notas brutas de inbox de outros clientes. A superfície pública para clientes é composta por busca e leitura de conteúdo curado, além da submissão da própria nota bruta quando a permissão estiver ativa.

O repositório do vault deve permanecer privado. Conteúdo em `_agents/` pode ser commitado nesse repositório, e a privacidade do repositório é parte do modelo operacional para reduzir exposição acidental de notas brutas.

## Autenticação

O principal `curator` autentica no MCP com o bearer token definido em `BRAIN_CURATOR_TOKEN`. Esse token é resolvido por `brain.auth.resolve_principal` e identifica operações administrativas e de curadoria.

Clientes agentes autenticam com tokens criados e rotacionados por ferramentas de curadoria. O token gerado segue o formato `brain_client_{slug}_{secret}`; no recebimento, o servidor calcula o hash do token apresentado e resolve apenas clientes com `status == "active"`.

O endpoint operacional `GET /status` usa autenticação separada: ele compara `Authorization: Bearer {BRAIN_AUTH_TOKEN}`. Esse token não substitui tokens MCP de curador ou cliente.

O webhook do GitHub em `POST /webhook/github` valida HMAC com `WEBHOOK_SECRET` e o header `X-Hub-Signature-256`.

A outbox para Hermes usa HMAC com `HERMES_WEBHOOK_SECRET` para assinar eventos enviados ao destino configurado.

## Rate Limiting

As ferramentas MCP aplicam um limite de requisições por principal (curador ou cliente) quando `MCP_RATE_LIMIT_PER_MINUTE` é maior que zero. O controle usa um token bucket em memória por principal e protege contra uso abusivo ou laços de cliente. Por ser estado em processo, o limite vale por instância da API; o deploy padrão usa uma única instância do MCP.

## Permissões

| Principal | Pode fazer | Não pode fazer |
| --- | --- | --- |
| `curator` | Usar ferramentas MCP de curadoria, administrar clientes, revelar ou rotacionar tokens de cliente quando configurado, revisar notas brutas e criar ou atualizar notas curadas. | Autenticar `/status` com `BRAIN_AUTH_TOKEN` por identidade MCP; escrever notas curadas em `_agents/`; ignorar validações de path e assinatura. |
| `client` | Buscar e ler notas curadas permitidas, executar `deep_search` e submeter notas brutas no próprio caminho de inbox quando tiver permissão ativa. | Ler notas brutas via MCP, ler inbox de outros clientes, criar ou atualizar notas curadas, administrar clientes ou usar ferramentas administrativas. |
| `operador HTTP` | Acessar `/health` publicamente e `/status` quando possuir `BRAIN_AUTH_TOKEN`; operar deployment, segredos e infraestrutura. | Usar ferramentas MCP sem token de `curator` ou `client`; chamar webhook GitHub sem assinatura válida. |
| `GitHub webhook` | Enviar eventos assinados para `/webhook/github`, disparando pull do repositório e enfileiramento de indexação ou deleção de Markdown curado. | Autenticar no MCP; acessar `/status`; enfileirar paths rejeitados, como `_agents/`, traversal ou paths fora do repositório. |

## Armazenamento De Tokens

O hash do token autentica clientes. `brain.auth.hash_token` usa SHA-256 sobre o token apresentado; a resolução de principal procura esse hash no repositório de clientes e exige cliente ativo.

O prefixo do token identifica tokens sem expor o token completo. Isso permite listagem e auditoria operacional com menor risco de vazamento do segredo bearer.

O token completo de cliente é recuperável apenas porque fica criptografado com Fernet usando `BRAIN_TOKEN_ENCRYPTION_KEY`. Essa chave é obrigatória no startup e precisa ser uma chave Fernet válida; preserve-a e rotacione-a apenas com procedimento controlado, porque ela protege a capacidade de revelar tokens de cliente já armazenados.

O curador pode revelar tokens de cliente criptografados ou rotacionar tokens por ferramentas MCP de curadoria. Rotação substitui o segredo efetivo e deve ser tratada como mudança sensível para integrações dependentes.

Valores secretos não devem ser logados nem commitados. Isso inclui tokens bearer, chaves Fernet, secrets de webhook, credenciais de banco, tokens GitHub e chaves de API.

## Webhooks E Assinaturas

O webhook do GitHub usa `X-Hub-Signature-256`. A assinatura é calculada com `WEBHOOK_SECRET` e deve cobrir o corpo recebido pelo endpoint.

A outbox para Hermes envia `X-Brain-Signature`, `X-Brain-Event-Type`, `X-Brain-Event-Id` e `X-Brain-Timestamp`. Esses headers identificam o evento, seu tipo lógico, o timestamp usado na validação e a assinatura própria do Brain.

Por compatibilidade, a entrega ao Hermes também envia `X-Hub-Signature-256`. Esse header permite consumidores que já validam o formato de assinatura do GitHub.

No código atual, `X-Brain-Signature` assina `timestamp + "." + body`. O header compatível `X-Hub-Signature-256` assina apenas o body.

## Segurança De Paths

`normalize_repo_path` protege a indexação de notas curadas e os paths recebidos via webhook. Ele rejeita path absoluto, drive Windows, `..`, path vazio, saída do repositório e extensão não Markdown quando Markdown é obrigatório.

Escritas curadas devem rejeitar paths `_agents/`. A validação de escrita em `brain.ingestion.git_writer.validate_curated_note_path` impede que operações de curadoria criem ou atualizem notas curadas dentro da inbox bruta.

Casos de symlink e path traversal são cobertos por testes de worker e handlers. Mudanças nessa área precisam preservar a garantia de que caminhos normalizados não escapam do repositório nem atravessam a fronteira da inbox.

## Inbox `_agents/`

`_agents/` é uma fronteira de workflow, não uma fronteira de confidencialidade. Ela separa conteúdo bruto pendente de curadoria do vault curado, mas não deve ser tratada como mecanismo isolado de segredo.

Notas brutas não são indexadas para busca pública. O fluxo de indexação curada e os paths de webhook devem ignorar ou rejeitar `_agents/`, mantendo a busca MCP de cliente restrita a conteúdo curado.

Clientes não conseguem ler notas brutas via MCP. As ferramentas de leitura e listagem de notas brutas são de curadoria, enquanto clientes recebem apenas respostas de busca/leitura curada e o resultado da própria submissão.

Notas brutas ainda podem existir no repositório do vault e devem ser tratadas como sensíveis. Isso afeta privacidade do repositório, permissões de Git, backups, logs e revisão manual de commits.

## Segredos E Configuração

`.env.example` contém apenas valores de exemplo. Ele deve servir como template de nomes e formato, não como fonte de valores reutilizáveis em produção.

A aplicação rejeita valores inseguros de exemplo no startup para settings críticos. `brain.config` também exige que `BRAIN_TOKEN_ENCRYPTION_KEY` seja uma chave Fernet válida.

Produção deve usar valores fortes para credenciais do banco, `OPENAI_API_KEY`, `GITHUB_TOKEN`, `BRAIN_CURATOR_TOKEN`, `BRAIN_TOKEN_ENCRYPTION_KEY`, `WEBHOOK_SECRET` e `REPO_URL`. Quando a outbox Hermes estiver habilitada, `HERMES_WEBHOOK_SECRET` e `HERMES_WEBHOOK_URL` também precisam ser configurados com valores de produção.

Gere uma chave Fernet para `BRAIN_TOKEN_ENCRYPTION_KEY` com:

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

## Checklist De Mudanças Sensíveis

- Alterar permissões de principal requer testes de MCP handlers.
- Alterar armazenamento de tokens requer testes de auth e repository.
- Alterar tratamento de paths requer testes de traversal e symlink.
- Alterar visibilidade da inbox requer testes MCP e retriever.
- Alterar assinaturas de webhook requer testes de assinatura.
- Alterar segredos de deployment requer atualização de README, operations e security docs.

## Arquivos De Referência

- [src/brain/auth.py](../src/brain/auth.py)
- [src/brain/config.py](../src/brain/config.py)
- [src/brain/main.py](../src/brain/main.py)
- [src/brain/mcp/handlers.py](../src/brain/mcp/handlers.py)
- [src/brain/repo_paths.py](../src/brain/repo_paths.py)
- [tests/test_auth.py](../tests/test_auth.py)
- [tests/test_config.py](../tests/test_config.py)
- [tests/test_signature.py](../tests/test_signature.py)
- [tests/integration/test_mcp_handlers.py](../tests/integration/test_mcp_handlers.py)
- [tests/integration/test_worker.py](../tests/integration/test_worker.py)
