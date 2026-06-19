# Skill `install-brain-mcp` (Design)

- **Data:** 2026-06-19
- **Status:** Design aprovado em brainstorming; aguardando revisao do spec
- **Escopo:** criar uma skill repo-scoped que ensina agentes de IA a instalar ou conectar o MCP do projeto `brain` em clientes LLM/IA.

## Objetivo

Criar uma skill versionada neste repositorio para orientar LLMs e agentes de IA
na instalacao do MCP do `brain`. A skill deve cobrir o fluxo comum de conexao,
os fatos especificos do servidor MCP do projeto e as diferencas entre clientes
potenciais:

- Codex CLI.
- Codex Desktop/App.
- Claude Desktop.
- Claude Web.
- Claude Code.
- Hermes.

A skill sera escrita para outro agente executar a configuracao com seguranca,
fazendo as perguntas certas, escolhendo o token correto, evitando vazamento de
segredos e diagnosticando falhas por camada.

## Fora Do Escopo

- Criar plugin Codex.
- Instalar a skill em `$CODEX_HOME/skills`.
- Alterar codigo Python do `brain`.
- Configurar clientes MCP reais nesta maquina.
- Criar fluxo OAuth, Directory submission ou conector publico para Claude.
- Gerar scripts auxiliares, assets ou referencias externas.

## Local E Formato

A skill sera criada como skill de repositorio em:

```text
.agents/skills/install-brain-mcp/SKILL.md
```

O `SKILL.md` sera o unico arquivo da skill. Ele tera frontmatter YAML contendo
apenas `name` e `description`, seguindo o formato esperado por skills Codex:

```yaml
---
name: install-brain-mcp
description: Install, connect, validate, or troubleshoot the Brain MCP server for AI clients and agents, including Codex CLI, Codex Desktop/App, Claude Desktop, Claude Web, Claude Code, and Hermes. Use when configuring Brain's `/mcp` endpoint, bearer-token authentication, client-vs-curator principals, or client-specific MCP setup.
---
```

A descricao deve acionar a skill quando o usuario pedir para instalar,
configurar, conectar, registrar, validar ou diagnosticar o MCP do `brain` em
clientes como Codex, Claude ou Hermes.

## Arquitetura Da Skill

A skill sera uma matriz unica por cliente, sem arquivos de referencia separados.
Essa escolha prioriza simplicidade de manutencao e deixa toda a orientacao em
um unico ponto.

O corpo do `SKILL.md` tera estas secoes:

- **Core Facts:** endpoint `/mcp`, transporte HTTP streamable, bearer token,
  `/health` publico e separacao entre MCP e `/status`.
- **Required Inputs:** cliente alvo, URL base do `brain`, URL final do MCP,
  principal esperado, token bearer, escopo de configuracao e exposicao de rede.
- **Common Setup Flow:** verificar servidor, montar URL, selecionar token,
  configurar cliente, recarregar o cliente quando necessario e validar tools.
- **Client Matrix:** instrucoes para Codex CLI, Codex Desktop/App, Claude
  Desktop, Claude Web, Claude Code e Hermes.
- **Security Rules:** regras para segredos, tokens, principals e commits.
- **Verification:** comandos ou acoes de validacao por cliente.
- **Troubleshooting:** diagnostico para 401, URL errada, rede inacessivel,
  tools ausentes e configuracao ainda nao carregada.

## Fatos Do `brain` Que A Skill Deve Ensinar

O MCP publico do projeto esta documentado em `docs/mcp-api.md` e
`docs/operations.md`.

Fatos obrigatorios:

- O endpoint MCP e `/mcp`.
- O transporte e HTTP streamable via FastMCP.
- Chamadas MCP usam `Authorization: Bearer <token>`.
- `/health` e publico e serve para verificar disponibilidade basica.
- `/status` usa `BRAIN_AUTH_TOKEN`, mas esse token nao autentica MCP.
- `BRAIN_CURATOR_TOKEN` autentica o principal `curator`.
- Tokens `brain_client_*` autenticam clientes agentes criados pelo sistema.
- Clientes comuns devem usar token de cliente e permissoes limitadas.
- Hermes deve usar credenciais de curador quando atuar como integracao interna
  administrativa.

## Fluxo Comum

O agente que usar a skill deve seguir este fluxo:

1. Identificar o cliente alvo.
2. Confirmar a URL base do deployment do `brain`.
3. Montar `BRAIN_MCP_URL` como `<base>/mcp`.
4. Testar `<base>/health` quando rede e ferramentas permitirem.
5. Confirmar se o principal esperado e `curator` ou `client`.
6. Escolher o token correto sem imprimir o valor completo em logs ou commits.
7. Aplicar a configuracao no formato do cliente alvo.
8. Recarregar ou reiniciar o cliente se a configuracao exigir.
9. Validar que o servidor aparece no cliente e que as tools esperadas estao
   disponiveis.
10. Diagnosticar falhas por camada antes de sugerir mudancas no servidor.

## Matriz De Clientes

### Codex CLI

A skill deve orientar o uso de configuracao MCP no `config.toml` do Codex ou
comandos `codex mcp`, preferindo variavel de ambiente para o bearer token quando
possivel. O formato principal para HTTP deve usar a URL `<base>/mcp` e um token
bearer por variavel de ambiente.

### Codex Desktop/App

A skill deve tratar Codex Desktop/App com cautela. A documentacao oficial atual
confirma skills no Codex app e MCP no Codex CLI/IDE; portanto a skill deve
instruir o agente a verificar se a superficie atual do app suporta MCP direto no
ambiente do usuario. Quando nao houver suporte direto, o caminho recomendado e
configurar via Codex CLI/IDE ou distribuir futuramente como plugin.

### Claude Code

A skill deve ensinar configuracao de servidor HTTP remoto/local via
`claude mcp add --transport http`, incluindo header
`Authorization: Bearer <BRAIN_MCP_TOKEN>`.
Tambem deve orientar verificacao com `/mcp` dentro do Claude Code.

### Claude Desktop

A skill deve distinguir dois caminhos:

- conectores remotos configurados pela conta Claude, que precisam de servidor
  acessivel pela infraestrutura da Anthropic;
- configuracoes locais do Desktop, quando aplicaveis ao ambiente do usuario.

Para o `brain`, o agente deve preferir o caminho HTTP remoto quando a URL for
publica, e deve explicar que servidores locais ou privados podem nao funcionar
como conectores remotos.

### Claude Web

A skill deve marcar a restricao central: Claude Web/custom connectors acessam o
MCP a partir da infraestrutura da Anthropic, nao da maquina local do usuario.
Assim, `localhost`, VPN privada ou firewall fechado nao bastam. A URL do
`brain` precisa estar publicamente acessivel ou allowlisted conforme a politica
de rede do plano.

### Hermes

A skill deve tratar Hermes como integracao interna do projeto. O agente deve
configurar Hermes para chamar `BRAIN_MCP_URL` com credencial de curador quando
precisar operar ferramentas administrativas. A skill tambem deve evitar confundir
`HERMES_WEBHOOK_SECRET`, usado na outbox do `brain`, com o bearer token usado
para autenticar chamadas MCP.

## Seguranca

A skill deve impor estas regras:

- Nunca commitar tokens, `.env`, arquivos locais de secrets ou configuracoes com
  bearer token literal.
- Preferir variaveis de ambiente ou stores seguros quando o cliente suportar.
- Usar placeholders como `<BRAIN_MCP_TOKEN>` em exemplos.
- Nao usar `BRAIN_AUTH_TOKEN` para MCP.
- Escolher token de cliente para agentes comuns e token de curador somente para
  integracoes administrativas confiaveis.
- Avisar quando Claude Web/Desktop remoto exigir exposicao publica do MCP.
- Validar que a URL pertence ao deployment esperado antes de inserir um token.

## Tratamento De Erros

A skill deve diagnosticar:

- `401`: token ausente, token errado, principal errado, cliente desabilitado ou
  uso indevido de `BRAIN_AUTH_TOKEN`.
- `404`: URL final incorreta; deve apontar para `/mcp`.
- Falha de rede: servidor parado, proxy, firewall, DNS, TLS ou cliente tentando
  acessar `localhost` de outro ambiente.
- Tools ausentes: servidor errado, principal sem permissao, configuracao nao
  recarregada ou cliente filtrando tools.
- Claude Web sem conexao: MCP nao esta publico ou nao atende requisitos de rede
  do conector remoto.
- Hermes sem permissao administrativa: token nao e de curador ou aponta para
  ambiente errado.

## Validacao

Depois da implementacao, validar:

- `SKILL.md` existe em `.agents/skills/install-brain-mcp/`.
- Frontmatter contem `name` e `description`.
- O nome da skill usa apenas letras minusculas e hifens.
- A descricao menciona instalacao/conexao do MCP do `brain` e clientes alvo.
- Todos os clientes listados no objetivo aparecem na matriz.
- O texto cobre `/mcp`, `/health`, bearer token, `curator`, `client` e
  `BRAIN_AUTH_TOKEN`.
- Nao ha tokens reais nem exemplos que incentivem commit de segredo.
- O conteudo esta consistente com `docs/mcp-api.md`, `docs/operations.md` e
  `docs/security.md`.

## Referencias

- `docs/mcp-api.md`
- `docs/operations.md`
- `docs/security.md`
- Documentacao oficial Codex: skills repo-scoped em `.agents/skills` e MCP via
  configuracao Codex.
- Documentacao oficial Claude Code: MCP HTTP com `claude mcp add --transport http`.
- Documentacao oficial Claude custom connectors: conectores remotos precisam de
  MCP acessivel pela infraestrutura da Anthropic.
