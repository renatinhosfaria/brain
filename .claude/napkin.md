# Napkin

## Corrections
| Date | Source | What Went Wrong | What To Do Instead |
|------|--------|----------------|-------------------|
| 2026-06-04 | user | Escrevi em inglês | O usuário fala português — responder SEMPRE em português (texto, perguntas, mensagens de commit já são pt por convenção do plano) |

## User Preferences
- Idioma: **português** em toda comunicação.

## Patterns That Work
- (acumular aqui)

## Patterns That Don't Work
- (acumular aqui)

## Domain Notes
- Projeto `brain`: provedor de memória pessoal como servidor MCP. Plano em `docs/superpowers/plans/2026-06-04-brain-memory-provider.md`, spec em `docs/superpowers/specs/2026-06-03-brain-memory-provider-design.md`.
- Stack: Python 3.12 + `uv`, SQLAlchemy async, Postgres custom (pgvector + Apache AGE), MCP/FastAPI, OpenAI.
- Ambiente (2026-06-04): `/root/brain` NÃO era repo git; `uv` NÃO instalado; Docker OK (29.1.4); Python do sistema 3.10.
- Execução do plano: TDD obrigatório, commits em pt-BR com Conventional Commits, todo o projeto vive em `brain/`.
