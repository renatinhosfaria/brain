# brain — Documentação Técnica

## Visão Geral

`brain` é um serviço FastAPI e MCP para um vault Markdown curado. Clientes
pesquisam e leem notas curadas; submissões brutas de agentes entram em
`_agents/` para curadoria pelo Hermes antes de comporem a base consultável.

## Para Quem Mantém O Projeto

Quem mantém o projeto deve começar pelos documentos que explicam estrutura,
execução local, operação, persistência, segurança e decisões arquiteturais:

- [architecture.md](architecture.md)
- [development.md](development.md)
- [operations.md](operations.md)
- [data-model.md](data-model.md)
- [security.md](security.md)
- [decisions/README.md](decisions/README.md)

## Para Quem Integra Via MCP

Quem integra clientes via MCP deve consultar o contrato público, os requisitos
de segurança e as orientações operacionais:

- [mcp-api.md](mcp-api.md)
- [security.md](security.md)
- [operations.md](operations.md)

## Mapa Da Documentação

| Documento | Leitor principal | Quando usar |
| --- | --- | --- |
| [architecture.md](architecture.md) | Mantenedores | Entender componentes, limites e fluxos técnicos do serviço. |
| [development.md](development.md) | Mantenedores | Preparar ambiente local, rodar testes e alterar o código. |
| [operations.md](operations.md) | Operadores e mantenedores | Subir, monitorar, diagnosticar e recuperar o serviço. |
| [data-model.md](data-model.md) | Mantenedores | Entender entidades, índices, persistência e regras de dados. |
| [security.md](security.md) | Mantenedores e integradores MCP | Avaliar autenticação, autorização, segredos e limites de exposição. |
| [mcp-api.md](mcp-api.md) | Integradores MCP | Implementar clientes que pesquisam, leem e submetem notas. |
| [decisions/README.md](decisions/README.md) | Mantenedores | Consultar e registrar decisões arquiteturais relevantes. |

## Convenções

- Escrever documentação técnica em português, com termos de domínio mantidos em
  inglês quando forem nomes de protocolo, ferramenta, endpoint, variável ou
  conceito consolidado.
- Usar diagramas Mermaid para fluxos, relações e visões estruturais que fiquem
  mais claras visualmente do que em texto corrido.
- Preferir links relativos entre documentos do repositório para manter a
  navegação válida em branches, forks e visualizadores locais.
- Atualizar a documentação na mesma mudança que alterar arquitetura, contratos
  públicos MCP, deployment, modelo de dados ou comportamento de segurança.

## Referências De Prática

- Diátaxis: https://diataxis.fr/
- C4 Model: https://c4model.com/
- Cognitect ADR article: https://www.cognitect.com/blog/2011/11/15/documenting-architecture-decisions
- Martin Fowler ADR page: https://martinfowler.com/articles/2019-04-adrs.html
