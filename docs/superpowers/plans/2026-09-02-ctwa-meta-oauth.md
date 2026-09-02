# OAuth direto do Brain para Meta Ads MCP — Plano de execução

## Objetivo

Adicionar OAuth Authorization Code + PKCE S256 ao Brain para a Meta Ads MCP,
com callback local via SSH, armazenamento criptografado root-only, refresh
automático e fallback explícito para o token atual.

## Tarefas

- [ ] Task 1: implementar cliente OAuth, descoberta, PKCE e store criptografado.
- [ ] Task 2: integrar provider ao MCP/serviço, configuração, CLI e systemd.
- [ ] Task 3: completar testes de integração, documentação operacional e revisão.

## Restrições

- Conta única: `act_1598606388477916`.
- Endpoint fixo: `https://mcp.facebook.com/ads`.
- Redirect URI: `http://127.0.0.1:8766/oauth/callback`.
- Scopes: somente `ads_read` e o mínimo MCP publicado/exigido; nunca pedir
  `ads_management` automaticamente.
- Nenhum segredo em logs, argv, SQLite ou contexto do CEO.
- O atendimento deve continuar quando OAuth/Meta estiver indisponível.
