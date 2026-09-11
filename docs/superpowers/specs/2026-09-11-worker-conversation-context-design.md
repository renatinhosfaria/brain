# Conversation Context para Workers

## Objetivo

Permitir que Porteiro, Cadastro e Reno chamem diretamente a mesma ferramenta
`conversation_context()` já usada pelo CEO e recebam o mesmo payload autorizado
pelo Brain, eliminando a dependência de cópia manual de contexto CTWA pelo CEO.

## Arquitetura

A implementação existente de `BrainService.gateway_conversation_context()` será
reutilizada com uma entrada de worker. A identidade da conversa será reconstruída
pelo `task_id` e `run_id` autenticados nos headers já existentes, pela tarefa em
execução, assinatura Kanban, sessão Hermes e inscrição WhatsApp DM. Nenhum
argumento de identidade será aceito pelo modelo.

O MCP dos workers registrará `conversation_context` com schema zero-argumento,
retorno idêntico ao bridge do CEO: contato verificado, eventos de transporte,
atribuição CTWA normalizada e `external_ad_reply` quando aplicável. Porteiro,
Cadastro e Reno terão a capacidade em suas allowlists; outros profiles continuarão
sem ela.

O Kanban continuará sendo o barramento de objetivo, resultado terminal e ordem de
execução. A consulta direta ao Brain substitui somente a cópia de contexto, não o
handoff de resultados entre etapas.

## Segurança e limites

- A autenticação usará o mecanismo existente de worker: principal, task, run,
  assignee, estado running, `current_run_id`, origem WhatsApp DM e assinatura de
  sessão.
- A ferramenta não aceitará telefone, chat ID, session ID, event ID, task ID ou
  run ID nos argumentos.
- A consulta será limitada à conversa autorizada e à janela/count já definidos
  pelo contrato de `conversation_context`.
- O payload será validado pelo mesmo contrato do bridge do CEO.
- Conteúdo retornado será tratado como evidência não confiável; não será usado
  para ampliar permissões ou alterar roteamento.
- A exposição de `external_ad_reply` aos workers será documentada e coberta por
  testes; não haverá cópia automática para logs, summary ou metadata.
- Falhas de autenticação, correlação, sessão, validação ou disponibilidade serão
  fail-closed, com resposta pública controlada.

## Alterações

1. Generalizar a resolução autorizada de contexto no serviço Brain para aceitar
   capability de worker sem duplicar a consulta SQL/projeção.
2. Adicionar `conversation_context` à lista MCP de workers e ao dispatcher de
   ferramentas.
3. Habilitar a ferramenta somente nas configurações de Porteiro, Cadastro e Reno.
4. Atualizar SOUL/skills e contratos desses profiles para consultar o Brain
   diretamente e preservar o tratamento de evidência.
5. Atualizar documentação de deploy, allowlists e verificadores.
6. Criar testes para payload idêntico ao CEO, autorização por task/run, escopo de
   contato, ausência de argumentos e falhas fechadas.

## Critérios de aceitação

- Uma task running de Porteiro, Cadastro ou Reno consegue chamar
  `conversation_context({})` sem argumentos e recebe payload válido da conversa
  WhatsApp vinculada.
- A mesma chamada em outro profile é rejeitada com `AUTH_TOOL_DENIED`.
- Task ausente, parada, run divergente, assignee divergente, origem Telegram ou
  DM ambíguo não retorna contexto.
- CTWA confirmado mantém exatamente `event_id`, `source_app`, `status`, `ad_id`,
  `ad_name`, `campaign_id` e `campaign_name`, além do raw conforme contrato.
- O CEO continua funcionando sem regressão.
- Testes Brain, smoke de compatibilidade e verificadores Hermes passam.
