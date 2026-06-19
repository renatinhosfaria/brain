# 0001 — Arquitetura Da Documentação Técnica

- **Status:** Aceita
- **Data:** 2026-06-18

## Contexto

O README da raiz é adequado para quickstart de produção, mas mantenedores e
integradores MCP precisam de documentação técnica mais profunda para entender
arquitetura, contratos, operação, modelo de dados e segurança.

## Decisão

Manter a documentação técnica em Markdown dentro de `docs/`, separada por
necessidade de leitura. Usar Mermaid para diagramas, uma simplificação do C4
Model para documentação de arquitetura e ADRs em `docs/decisions/` para
registrar decisões arquiteturalmente significativas.

## Consequências

A navegação fica mais simples e os documentos ficam menores e revisáveis.
Mudanças em arquitetura, contratos públicos MCP, deployment, modelo de dados e
segurança passam a exigir atualização da documentação na mesma mudança.
