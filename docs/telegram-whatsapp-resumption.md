# Retomada administrativa Telegram → WhatsApp

## Estado desta implementação

Código e testes locais implementados para t_d45cb831. NÃO ativado, NÃO aplicado ao caso vivo e NÃO autorizado para atendimento com o estado atual. A tarefa t_e857fdfb deve permanecer bloqueada.

## Contrato e fronteira de confiança

- `tasks.session_id` continua sendo a origem de controle Telegram. Nenhuma escrita é feita em kanban.db ou state.db.
- Uma concessão explícita, emitida pelo administrador local através de `python -m brain.resumption_admin`, vincula a mesma tarefa a um parent direto done cuja origem WhatsApp/DM é comprovada no estado canônico.
- O administrador atesta a autorização Telegram já autenticada. O UUID é somente referência auditável; texto do cartão, presença num grupo, subscription e UUID não autenticam remetente. A CLI NÃO é um novo fluxo automático de autenticação Telegram.
- A fronteira é o administrador do sistema operacional. Root já pode controlar código e armazenamento; UID/env não isolam um agente root malicioso. Não registrar essa CLI/API Python como ferramenta de agente nem endpoint. A recusa a ambiente de worker é defesa adicional, não a prova primária de autoridade.
- O emissor deriva principal, sessão de controle e contexto WhatsApp de metadados canônicos. Não recebe telefone, chat, sessão ou principal como argumento. Subscription só restringe o destino; a prova longitudinal/aliases existente continua obrigatória.
- O serviço autentica o token do worker e valida Task/Run como antes. Para origem Telegram exige uma concessão e revalida vínculo, parent, edge, contexto, aliases e principal a cada chamada. O contexto WhatsApp passa pelo gate longitudinal original. Não existe concessão implícita por herança.
- Concessão dura 3600 segundos e vale somente para o PRIMEIRO novo run canônico após emissão. O watermark impede runs antigos; a ordem dos runs resolve a precisão em segundos dos timestamps Hermes. Primeiro run que falhar sem consultar histórico também consome a oportunidade: não pular para outro run.
- Primeira leitura vincula o run atomicamente em transação BEGIN IMMEDIATE. Leituras repetidas/concorrrentes do mesmo run são idempotentes. Outro run, grant expirado/revogado, mudança de vínculo ou identidade falham fechados. Reemitir o mesmo pedido antes do uso não estende TTL; concessão consumida/expirada não é renovada por replay.
- Persistência e trilha: tabela `conversation_resumptions` no runtime DB pertencente ao Brain, criada somente pela interface de emissão. Conserva referência de autorização, binding, watermark, emissão/expiração, run vinculado, instante de claim e revogação. Não apagar registros para reutilizar concessão.
- `status` expõe somente referência técnica, timestamps e run; nunca destino ou sessão. CLI não envia mensagens nem consulta histórico. A leitura de histórico continua exclusivamente nas ferramentas existentes e no principal autorizado.
- Não há transação distribuída entre os bancos Brain/Hermes. Revalidação em cada leitura e antes de persistir reduz corridas, mas não torna uma entrega comercial atomicamente vinculada ao grant. CEO ainda deve revalidar vigência e duplicidade no momento da entrega. Grant de histórico NÃO garante exactly-once de envio.

## Interface administrativa (operador; não executada em produção nesta tarefa)

Usar o ambiente administrativo autorizado do Brain com a mesma configuração e as variáveis de serviço já provisionadas. `BrainSettings.from_env` exige `BRAIN_TRANSPORT_HMAC_SECRET` mesmo que este comando não ingira transporte; também resolve os principals configurados. Não colocar segredos na linha de comando, relatório ou cartão, não inventar valores e não copiar credenciais para outro profile.

Preflight read-only, sem iniciar BrainService e sem criar banco/tabela:

    PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=/root/brain/src /root/brain/.venv/bin/python -B -m brain.resumption_admin inspect t_e857fdfb --parent t_f22fff28 --config /etc/brain/brain.toml

Resultado exigido antes de qualquer aplicação: `eligible_metadata_only`. Esse resultado comprova somente elegibilidade estrutural, não concessão nem reparo vivo.

Após revisão do código, aprovação operacional de ativação e preflight válido, o operador local registra a autorização já concedida (sem nova confirmação comercial):

    PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=/root/brain/src /root/brain/.venv/bin/python -B -m brain.resumption_admin inspect t_e857fdfb --parent t_f22fff28 --config /etc/brain/brain.toml --authorization-id f922bc7e-4d07-4053-937c-4179f2ae649c --apply --attest-authenticated-control

A emissão não desbloqueia a tarefa. Conferir o readback retornado e depois:

    PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=/root/brain/src /root/brain/.venv/bin/python -B -m brain.resumption_admin status t_e857fdfb --config /etc/brain/brain.toml

Revogação por interface, se necessária, sem SQL manual:

    PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=/root/brain/src /root/brain/.venv/bin/python -B -m brain.resumption_admin revoke t_e857fdfb --config /etc/brain/brain.toml --apply --attest-authenticated-control

Concessão expirada, revogada ou já consumida permanece negada. Não existe opção de força/renovação; se uma nova tentativa administrativa for necessária, manter bloqueio e revisar o contrato em vez de apagar estado ou fabricar outro run.

## Ativação e validação viva pendentes

1. Operador revisa o commit local e executa a suíte canônica em contexto administrativo normal, fora de contexto delegado. Não remover marcadores nativos de delegação no worker para forçar os testes.
2. Confirmar qual pacote/fonte a unit brain.service usa. A fonte alterada é /root/brain/src; o processo já carregado não foi reiniciado. Se houver cópia empacotada, sua publicação/instalação pertence ao operador; não editar /usr/local/lib/hermes-agent. Nenhum plugin Hermes ou config/allowlist foi alterado.
3. Ativação requer autorização específica para `systemctl restart brain.service`; o Dev não a recebeu nem a executou. Operador verifica fonte/cópia efetiva, health e journal após ativação. A mera indicação active/running não prova nova versão.
4. No ambiente correto, executar preflight, emissão e readback do grant. Antes de marcar t_d45cb831 tecnicamente concluída, documentar provas de ativação e vínculo operacional; por enquanto essas provas estão pendentes.
5. CEO mantém o mesmo cartão t_e857fdfb. Somente depois do handoff técnico terminal aceito libera a etapa pelo fluxo nativo do quadro, respeitando o grafo. Não criar tarefa substituta nem editar Task/Run/sessão.
6. O primeiro run autorizado do Reno faz a consulta normal sob seu próprio token/Task/Run. Conferir status do grant e audit Brain para esse run, sem substituir headers. Se negado, parar; não forçar replay.
7. Reno prepara texto NOVO, verificando vigência/intervenção posterior. CEO entrega uma única vez ao destino técnico comprovado e verifica envio/duplicidade pelo mecanismo normal. Não há mensagem de teste para cliente, payload antigo ou resposta comercial preparada pelo Dev.

A autorização comercial já existe: operador autenticado confirmou Recanto Verde II pronto para morar e ausência de novas mensagens/intervenção humana na ocasião. Não pedir esses fatos novamente; não inferir estoque, preço ou disponibilidade e não ignorar intervenções posteriores.

## Evidência executada nesta tentativa

- Probe read-only `/root/.hermes/ops/reports/inspect_resumption_t_d45cb831.py`: exit 0. Parent WhatsApp/DM com origem longitudinal válida; child Telegram/group com mesmo destino herdado e origem fora do conjunto WhatsApp. Journal confirma AUTH_SESSION_MISMATCH de t_e857fdfb/run 412.
- TDD: teste positivo primeiro falhou pela ausência do módulo; após implementação passou. Negativa de salto do primeiro run encontrou falha real e passou após correção. Teste de timestamp inteiro no mesmo segundo reproduziu falsa negativa e passou após ajuste à precisão nativa. CLI testada com UID/env sintéticos e concessão em banco temporário.
- Comando focado: `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src:tests .venv/bin/python -B -m unittest test_resumption test_brain`, cwd /root/brain: 69 testes, OK, exit 0. Inclui 12 testes de retomada e 57 preexistentes de Brain.
- `ruff check src tests scripts integrations`, `ruff format --check src tests scripts integrations` e `git diff --check`: exit 0.
- Suíte canônica: `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=/root/brain:/root/brain/src .venv/bin/python -B -m unittest discover -s tests`, cwd /root/brain: 645 testes executados, uma falha, exit 1. A falha isolada `test_current_hermes_supports_brain_runtime_contracts` ocorre em `hermes_cli/kanban_db_connect.py:679`, abrindo uma fixture inexistente em mode=ro sob guarda nativa de contexto delegado. Arquivos desse caminho não foram modificados. Não declarar suíte completa verde; repetir no contexto apropriado pelo operador, sem contornar a guarda.
- Primeira execução ampla, a partir do home Dev sem a raiz Brain no PYTHONPATH, também teve nove erros de import de scripts. Corrigida a invocação/cwd; esses erros desapareceram.
- CLI preflight vivo, sem env administrativo completo: exit 1, `RESUMPTION_UNAVAILABLE`. Não comprovou elegibilidade do novo caminho. Sondagem adicional por python -c recusada pelo runtime headless; ação encerrada sem alternativa para contornar. O loader requer o HMAC de transporte; a causa exata desse retorno vivo não foi determinada nessa sondagem.
- Revisão independente de código: passed=true, sem erros lógicos/concerns de segurança. Sugestão de teste para runtime ausente/indisponível foi incorporada e passou. O revisor não executou testes (tentou pytest ausente); a evidência de execução acima pertence ao Dev via unittest.
- Consulta ao índice oficial https://hermes-agent.nousresearch.com/docs/llms.txt: exit 0. O contrato específico Brain não é fornecido pelo Hermes upstream; código instalado foi a referência para criação/herança e precisão dos runs.

Nenhuma concessão viva, alteração manual de banco/sessão, troca de credencial, deploy/restart ou mensagem externa foi executada.
