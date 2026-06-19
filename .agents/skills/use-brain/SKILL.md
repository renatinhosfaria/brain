---
name: use-brain
description: Use Brain como memória MCP conectada para clientes de agente ao recuperar contexto curado com `search`, `deep_search` e `get_note`, e enviar novo conhecimento durável para curadoria com `submit_agent_note`. Use quando um agente precisar de contexto de projeto, decisões, preferências, conhecimento reutilizável ou orientação sobre o que preservar no Brain.
---

# Usar Brain

Use o Brain como memória curada depois que o servidor MCP já estiver conectado. Esta skill explica como um cliente de agente comum deve recuperar contexto e contribuir conhecimento durável; ela não trata da instalação do MCP nem da administração por curadores.

## Modelo Mental Principal

O Brain separa conhecimento curado de envios brutos:

- `search`, `deep_search` e `get_note` leem conhecimento curado.
- `submit_agent_note` envia conhecimento bruto para a caixa de entrada de agentes para curadoria posterior.
- Uma nota de agente enviada ainda não é uma nota curada.
- Clientes comuns não devem ler `_agents/` nem depender de caminhos brutos da caixa de entrada.
- Clientes comuns não devem tentar criar ou atualizar notas curadas diretamente.
- Fluxos administrativos e de curadoria pertencem a principals de curador confiáveis, não a clientes comuns.

Use o Brain quando a tarefa depender de histórico de projeto, decisões, preferências do usuário, fatos de domínio, descobertas anteriores, contexto reutilizável ou conhecimento que possa sobreviver à conversa atual.

## Comece Pela Recuperação

Antes de responder ou agir, pergunte se contexto anterior poderia mudar materialmente o resultado.

Recupere informações do Brain quando o usuário perguntar sobre:

- um projeto, repositório, sistema, pessoa, cliente, processo ou decisão;
- preferências, convenções, restrições ou acordos anteriores;
- perguntas sobre contexto conhecido, como "o que sabemos sobre este projeto?";
- contexto histórico ou justificativa;
- informações que podem ter sido aprendidas em sessões anteriores.

Não recupere informações para tarefas triviais e pontuais em que a memória não possa ajudar, como formatar uma única frase quando todo o contexto necessário já está presente.

## Fluxo de Recuperação

Use esta sequência:

1. Comece com `search` para perguntas diretas e palavras-chave prováveis.
2. Use `deep_search` quando o contexto puder depender de entidades relacionadas, relações de grafo ou histórico mais amplo do projeto.
3. Use `get_note` para abrir as notas mais relevantes antes de fazer afirmações importantes.
4. Responda com base no contexto curado quando as notas sustentarem a resposta.
5. Diga quando o Brain não forneceu suporte suficiente em vez de inventar contexto ausente.

### Use `search`

Use `search` para recuperação direta em notas curadas.

Bons usos:

- encontrar notas sobre um projeto, pessoa, decisão, preferência ou tópico técnico;
- localizar notas candidatas por termos prováveis;
- obter trechos antes de decidir quais notas abrir.

Mantenha `limit` moderado. Trate trechos como pistas. Para afirmações importantes, abra a nota de origem com `get_note`.

### Use `deep_search`

Use `deep_search` quando o usuário precisar de contexto, não apenas de correspondência textual.

Bons usos:

- entender o histórico do projeto;
- descobrir entidades ou decisões relacionadas;
- conectar pessoas, sistemas, conceitos, repositórios ou processos;
- recuperar contexto quando a busca direta por palavras-chave puder perder conhecimento adjacente.

Prefira parâmetros conservadores primeiro. Aumente `depth` ou `max_entities` apenas quando o primeiro resultado for estreito demais. Use `namespace` ou `rel_types` somente quando souber que eles reduzem ruído.

### Use `get_note`

Use `get_note` para ler uma nota curada por id ou caminho.

Use quando:

- um resultado de `search` ou `deep_search` parecer relevante;
- o trecho não for suficiente para responder com confiança;
- o usuário pedir a fonte, a justificativa, detalhes ou o contexto exato.

Se `get_note` retornar `null`, trate a nota como indisponível. Não infira seu conteúdo. Não tente ler `_agents/` como cliente comum.

## Fluxo de Contribuição

Use `submit_agent_note` quando novo conhecimento durável surgir durante o trabalho.

Conhecimento durável inclui:

- fatos estáveis;
- decisões e suas justificativas;
- preferências e convenções do usuário;
- contexto de projeto reutilizável;
- descobertas técnicas ou operacionais;
- mapeamentos entre nomes, sistemas, repositórios, processos e entidades;
- correções úteis para contexto anteriormente presumido.

Antes de enviar, verifique:

1. Isso provavelmente ajudará uma sessão futura?
2. É estável o suficiente para preservar?
3. Outra pessoa consegue entender sem esta conversa inteira?
4. Está livre de segredos e dados sensíveis desnecessários?
5. O usuário permitiu ou razoavelmente esperava esse tipo de persistência?

Se a resposta for sim, envie uma nota clara para curadoria. Se a resposta for incerta, pergunte antes de enviar.

## Regras de Qualidade das Notas

Escreva envios para um curador futuro e um agente futuro.

Use:

- um título conciso que nomeie o tópico;
- conteúdo autocontido com os fatos e o contexto relevantes;
- fonte ou origem quando isso ajudar a avaliar confiabilidade;
- marcadores de incerteza quando o conhecimento for tentativo;
- `suggested_namespace` quando o projeto, domínio ou tenant estiver claro;
- `metadata` curto somente quando ajudar a curadoria.

Prefira este formato:

```json
{
  "title": "O Projeto Alpha usa pgvector para busca semântica de documentos",
  "content": "Durante o trabalho no Projeto Alpha, confirmamos que a busca semântica de documentos é baseada em pgvector no PostgreSQL. Isso importa ao diagnosticar qualidade de recuperação ou comportamento de migração.",
  "suggested_namespace": "project-alpha",
  "metadata": {
    "source": "agent-session",
    "kind": "technical-fact"
  }
}
```

Não envie notas vagas como "conversamos sobre o projeto" ou "o usuário gosta disso". Inclua o que foi aprendido e por que isso importa.

## O Que Não Armazenar

Não envie:

- tokens, senhas, chaves de API, chaves privadas, cookies ou identificadores de sessão;
- logs brutos, a menos que um resumo compacto capture a lição reutilizável;
- transcrições longas sem síntese;
- progresso de tarefas pontuais sem valor futuro;
- dados pessoais sensíveis que não sejam necessários para trabalhos futuros;
- material-fonte protegido por direitos autorais ou confidencial copiado integralmente;
- afirmações incertas escritas como fatos;
- qualquer coisa que o usuário pediu para não persistir.

Em caso de dúvida, resuma a lição durável e omita detalhes sensíveis.

## Tratamento de Erros

Use este mapa de falhas:

| Situação | Ação |
| --- | --- |
| `search` não retorna resultados úteis | Reformule com sinônimos, nomes de projetos, pessoas, caminhos ou termos mais específicos; depois tente `deep_search` se relacionamentos puderem importar |
| `deep_search` retorna ruído | Reduza parâmetros amplos, remova filtros desnecessários, tente `search` direto ou abra apenas notas de alta confiança |
| `get_note` retorna `null` | Trate a nota como indisponível e evite afirmações baseadas nela |
| `submit_agent_note` exige conteúdo | Envie `content` ou `messages` estruturadas; use `content` para fatos duráveis concisos |
| `submit_agent_note` não é permitido | Diga ao usuário que este cliente não pode enviar notas e que um curador deve ajustar as permissões |
| Uma ferramenta parece exigir acesso de curador | Não a use como cliente comum; mantenha-se em `search`, `deep_search`, `get_note` e `submit_agent_note` |
| O contexto recuperado conflita com o usuário atual | Exponha o conflito e pergunte, ou prossiga com a instrução atual explícita do usuário |

## Expectativas de Saída

Quando a recuperação do Brain influenciar a resposta:

- Mencione que usou contexto curado do Brain quando isso ajudar o usuário a confiar na resposta.
- Evite citar em excesso detalhes internos das ferramentas; resuma o conteúdo relevante das notas.
- Declare incerteza quando a recuperação tiver sido fraca ou vazia.

Ao enviar conhecimento:

- Diga que o conhecimento foi enviado para curadoria.
- Não afirme que ele já está curado ou pesquisável.
- Mencione o ponto durável enviado, sem expor segredos.

Mantenha a instrução atual do usuário acima de memórias mais antigas. O Brain fornece contexto; ele não substitui direcionamento explícito do usuário.
