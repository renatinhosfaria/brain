---
name: install-brain-mcp
description: Use quando precisar instalar, conectar, validar ou solucionar problemas do servidor Brain MCP para clientes e agentes de IA, incluindo Codex CLI, Codex Desktop/App, Claude Desktop, Claude Web, Claude Code e Hermes. Use ao configurar o endpoint `/mcp` do Brain, autenticação por token bearer, principals de cliente versus curador ou configuração MCP específica de cliente.
---

# Instalar Brain MCP

Use esta skill quando um usuário quiser conectar um cliente ou agente de IA ao servidor MCP `brain`. Trate isso como uma tarefa de integração com credenciais ativas: verifique o cliente-alvo, confirme a URL do servidor, escolha o principal correto e evite expor tokens bearer.

## Entradas Necessárias

Determine estes valores antes de escrever ou alterar a configuração do cliente:

- **Cliente-alvo:** Codex CLI, Codex Desktop/App, Claude Desktop, Claude Web, Claude Code, Hermes ou outro cliente compatível com MCP.
- **URL base do Brain:** a raiz da implantação, como `https://brain.example.com`.
- **URL MCP do Brain:** o endpoint MCP final, sempre `<brain-base-url>/mcp`.
- **Tipo de principal:** `curator` para integrações administrativas confiáveis, ou `client` para clientes de agente comuns.
- **Origem do token bearer:** uma variável de ambiente, armazenamento seguro ou valor de token fornecido uma única vez pelo usuário.
- **Local de rede:** máquina local, rede privada, internet pública ou endpoint público em allowlist.
- **Escopo de configuração:** local do usuário, local do projeto, gerenciado pela organização ou configuração de runtime do Hermes.

Peça valores ausentes quando eles não puderem ser inferidos de arquivos locais ou do ambiente atual. Nunca peça ao usuário para colar um segredo se uma variável de ambiente local ou armazenamento seguro já estiver disponível.

## Fatos do Brain MCP

Use estes fatos específicos do projeto:

- O endpoint MCP é `/mcp`.
- O transporte é HTTP streamable por meio do FastMCP.
- Requisições MCP são autenticadas com `Authorization: Bearer <token>`.
- `GET /health` é público e verifica disponibilidade básica do serviço.
- `GET /status` usa `BRAIN_AUTH_TOKEN`, mas `BRAIN_AUTH_TOKEN` não autentica MCP.
- `BRAIN_CURATOR_TOKEN` autentica o principal `curator`.
- Tokens que começam com `brain_client_` autenticam clientes de agente criados pelas ferramentas de curador do Brain.
- Clientes podem pesquisar e ler notas curadas e podem enviar notas de agente quando suas permissões permitirem.
- Credenciais de curador podem administrar clientes, notas brutas de agentes, notas curadas, manutenção do grafo e outras ferramentas MCP protegidas.
- `_agents/` é o estado bruto do fluxo de caixa de entrada. Clientes comuns não devem tratá-lo como conteúdo público pesquisável.

## Fluxo Comum de Configuração

Siga esta sequência para todos os clientes:

1. Identifique o cliente-alvo e seu mecanismo atual de configuração MCP.
2. Normalize a URL:
   - Se o usuário fornecer `https://brain.example.com`, use `https://brain.example.com/mcp`.
   - Se o usuário fornecer `https://brain.example.com/mcp`, mantenha.
   - Não use `/health` nem `/status` como URL MCP.
3. Verifique a disponibilidade do serviço quando ferramentas de rede estiverem disponíveis:

   ```bash
   curl -fsS "$BRAIN_BASE_URL/health"
   ```

4. Selecione o token:
   - Use um token `brain_client_` para clientes de agente comuns.
   - Use `BRAIN_CURATOR_TOKEN` apenas para Hermes ou outra integração administrativa confiável.
   - Não use `BRAIN_AUTH_TOKEN` para MCP.
5. Prefira configuração de token baseada em variável de ambiente quando o cliente oferecer suporte.
6. Escreva somente a configuração necessária para o cliente selecionado.
7. Recarregue ou reinicie o cliente se sua configuração MCP for carregada na inicialização.
8. Verifique se o servidor MCP aparece no cliente e expõe as ferramentas esperadas.
9. Diagnostique falhas por camada: rede, URL, transporte, autenticação, permissões do principal e então comportamento de carregamento específico do cliente.

## Matriz de Clientes

### Codex CLI

O Codex oferece suporte a servidores MCP HTTP streamable por meio de `config.toml`. Prefira este formato para configuração no nível do usuário:

```toml
[mcp_servers.brain]
url = "https://brain.example.com/mcp"
bearer_token_env_var = "BRAIN_MCP_TOKEN"
```

Para configuração com escopo de projeto em um repositório confiável, use `.codex/config.toml` com a mesma tabela. Para configuração global do usuário, use o arquivo de configuração do Codex do usuário.

Ao executar a configuração:

```bash
export BRAIN_MCP_TOKEN="<BRAIN_MCP_TOKEN>"
codex mcp --help
```

Use `codex mcp --help` para confirmar os flags exatos de adição/atualização da CLI instalada antes de usar o gerenciamento MCP por linha de comando. Se os flags estiverem pouco claros ou diferirem da documentação atual, edite `config.toml` diretamente em vez de adivinhar.

Verifique dentro da TUI do Codex com:

```text
/mcp
```

Resultado esperado: aparece um servidor chamado `brain`. Se isso não acontecer, verifique se o arquivo de configuração está no escopo ativo do Codex e se o shell que inicia o Codex tem `BRAIN_MCP_TOKEN` definido.

### Codex Desktop/App

As skills do Codex estão disponíveis no app Codex, mas o suporte a configuração direta de MCP pode variar por superfície e versão. Não afirme que Codex Desktop/App pode consumir um servidor MCP remoto até que a UI atual do app ou a documentação oficial confirme o caminho.

Use este caminho de decisão:

- Se o app expuser configurações de MCP, configure a mesma URL HTTP e o mesmo padrão de variável de ambiente para token bearer usados no Codex CLI.
- Se o app compartilhar a configuração ativa do Codex com a CLI ou IDE no ambiente do usuário, configure `config.toml` e verifique pelo app.
- Se o app não expuser configurações diretas de MCP, explique a limitação e recomende Codex CLI/IDE para uso direto de MCP.
- Se o usuário precisar de distribuição pelo Codex App posteriormente, recomende um plugin como trabalho futuro de empacotamento, não como parte desta skill.

### Claude Code

Claude Code oferece suporte a servidores MCP HTTP. Use `claude mcp add` para o escopo do usuário atual ou do projeto.

Exemplo de comando usando uma variável de shell em vez de um token literal no histórico do shell:

```bash
export BRAIN_MCP_URL="https://brain.example.com/mcp"
export BRAIN_MCP_TOKEN="<BRAIN_MCP_TOKEN>"
claude mcp add --transport http brain "$BRAIN_MCP_URL" --header "Authorization: Bearer $BRAIN_MCP_TOKEN"
```

Para configuração compartilhada pelo projeto, use a configuração MCP com escopo de projeto do Claude Code somente quando o token não for incluído em commit. Mantenha segredos em configurações locais do usuário, variáveis de ambiente ou um helper seguro compatível com o cliente.

Verifique dentro do Claude Code com:

```text
/mcp
```

Resultado esperado: `brain` aparece na lista de servidores MCP. Se a autenticação falhar, verifique novamente se o token é um token Brain MCP, não `BRAIN_AUTH_TOKEN`.

### Claude Desktop

Claude Desktop tem dois caminhos MCP relevantes:

- **Conectores remotos pela conta Claude:** trate-os como Claude Web. A URL MCP do Brain deve ser alcançável pela infraestrutura da Anthropic.
- **Configuração MCP local do Desktop:** use somente quando a versão instalada do Desktop oferecer suporte ao transporte e aos headers de autenticação necessários.

Quando a configuração local do Desktop oferecer suporte a servidores MCP HTTP com headers, use uma configuração equivalente a:

```json
{
  "mcpServers": {
    "brain": {
      "type": "http",
      "url": "https://brain.example.com/mcp",
      "headers": {
        "Authorization": "Bearer ${BRAIN_MCP_TOKEN}"
      }
    }
  }
}
```

Não faça commit deste arquivo se ele contiver um token literal. Se o caminho do Desktop usar um conector de conta Claude em vez de configuração local, siga as restrições do Claude Web abaixo.

### Claude Web

Conectores personalizados do Claude Web usam MCP remoto. O servidor é contatado pela infraestrutura da Anthropic, não pela máquina local do usuário.

Antes de configurar Claude Web, confirme todos estes pontos:

- A URL MCP do Brain é publicamente alcançável ou está em allowlist para o plano Claude relevante.
- TLS e DNS funcionam de fora da rede privada do usuário.
- O caminho do conector consegue fornecer autenticação compatível com a autenticação MCP por token bearer do Brain.
- O usuário entende que expor o Brain publicamente exige autenticação, monitoramento e tratamento de segredos em padrão de produção.

Se o fluxo atual de conector do Claude Web não conseguir enviar o token bearer exigido pelo Brain, não enfraqueça a autenticação do Brain. Recomende um destes caminhos:

- Use Claude Code com um servidor MCP HTTP e header bearer.
- Use configuração MCP local do Claude Desktop se a versão instalada oferecer suporte.
- Adicione um pequeno proxy de autenticação confiável ou uma camada de conector compatível com OAuth como trabalho futuro separado.

### Hermes

Trate o Hermes como uma integração interna confiável quando ele executar fluxos de curador do Brain.

Configure o Hermes com:

```bash
BRAIN_MCP_URL="https://brain.example.com/mcp"
BRAIN_MCP_TOKEN="<BRAIN_CURATOR_TOKEN>"
```

Use um token de curador somente quando o Hermes precisar de ferramentas MCP administrativas. Se o Hermes precisar apenas de capacidades de cliente comum, crie um cliente de agente do Brain e use o token `brain_client_` dele.

Não confunda estes valores relacionados ao Hermes:

- `BRAIN_MCP_TOKEN`: token bearer que o Hermes envia ao Brain MCP.
- `HERMES_WEBHOOK_SECRET`: segredo que o Brain usa para assinar entregas de outbox ao Hermes.
- `HERMES_WEBHOOK_URL`: URL que o Brain usa para entregar eventos ao Hermes.

Configurações de webhook não autenticam o Hermes em `/mcp`.

## Regras de Segurança

Aplique estas regras sempre:

- Nunca commite `.env`, arquivos locais de segredo, tokens bearer literais ou tokens de cliente gerados.
- Prefira variáveis de ambiente, armazenamentos seguros ou comandos auxiliares em vez de valores estáticos de token em arquivos de configuração.
- Use exemplos como `<BRAIN_MCP_TOKEN>` e `<BRAIN_CURATOR_TOKEN>` em vez de valores reais.
- Valide a URL de destino antes de anexar um token a ela.
- Use tokens de cliente para agentes normais.
- Use tokens de curador somente para integrações administrativas confiáveis.
- Explique a exposição de rede do Claude Web e de conectores remotos do Claude antes de recomendar esse caminho.
- Não remova a autenticação do Brain para facilitar a conexão de um cliente.

## Verificação

Use a verificação mais forte disponível para o cliente selecionado:

- Serviço Brain:

  ```bash
  curl -fsS "$BRAIN_BASE_URL/health"
  ```

- Codex CLI/TUI:

  ```text
  /mcp
  ```

- Claude Code:

  ```text
  /mcp
  ```

- Claude Web ou conector remoto do Claude:
  - Confirme que o conector foi adicionado e autenticado nas configurações do Claude.
  - Confirme que a URL do Brain não é `localhost` e é alcançável de fora da rede privada.

- Hermes:
  - Confirme que o Hermes usa `BRAIN_MCP_URL` terminando em `/mcp`.
  - Confirme que o Hermes usa o token bearer MCP pretendido, não segredos de webhook.
  - Confirme que logs de qualquer chamada MCP com falha mascaram o token.

Não trate uma resposta bem-sucedida de `/health` como prova de que a autenticação MCP funciona. `/health` prova apenas que o serviço HTTP está alcançável.

## Solução de Problemas

Use este mapa de falhas:

| Sintoma | Causa Provável | Ação |
| --- | --- | --- |
| `401` do MCP | Token ausente, token errado, cliente desativado, principal errado ou `BRAIN_AUTH_TOKEN` usado para MCP | Use `BRAIN_CURATOR_TOKEN` para fluxos de curador ou um token `brain_client_` para clientes comuns |
| `404` ou erro de rota | Cliente aponta para o caminho errado | Use `<brain-base-url>/mcp` |
| `/health` funciona, mas MCP falha | Problema de autenticação ou transporte MCP | Verifique o token bearer, o transporte MCP do cliente e os logs do servidor |
| Claude Web não consegue conectar | Brain está local, privado, atrás de VPN, bloqueado por firewall ou a autenticação do conector não consegue enviar o token bearer | Use um endpoint público ou em allowlist, Claude Code, configuração local do Desktop ou trabalho futuro de proxy de autenticação |
| Ferramentas estão ausentes | Principal não tem permissão, cliente filtrou ferramentas, servidor não recarregou ou ambiente Brain errado está configurado | Verifique o principal, ferramentas habilitadas, recarga do cliente e URL de destino |
| Hermes não consegue administrar o Brain | Hermes está usando um token de cliente ou segredo de webhook | Use credenciais MCP de curador para fluxos administrativos |
| Token aparece no diff de configuração | Segredo foi escrito literalmente | Remova-o do arquivo, rotacione o token se ele foi exposto e mude para configuração por variável de ambiente ou armazenamento seguro |

## Expectativas de Saída

Ao ajudar um usuário a instalar Brain MCP:

- Informe o caminho de cliente selecionado.
- Informe o formato exato da URL MCP.
- Informe qual classe de token é necessária sem imprimir o token.
- Forneça somente a configuração relevante para esse cliente.
- Inclua uma etapa de verificação.
- Inclua o próximo diagnóstico mais provável se a verificação falhar.
