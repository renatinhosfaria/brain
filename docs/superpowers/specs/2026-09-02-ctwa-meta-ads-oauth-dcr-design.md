# OAuth dinâmico do Brain para Meta Ads MCP

## Objetivo

Fazer o Brain autenticar-se no Meta Ads MCP pelo mesmo modelo protocolar de
clientes gerenciados como ChatGPT: descoberta dos metadados OAuth, registro
dinâmico do cliente (DCR), autorização Code + PKCE e renovação automática. O
operador não deverá criar manualmente uma app Meta nem fornecer App ID ou App
Secret para o fluxo normal.

## Contexto e limites

O Brain já possui um cliente OAuth com callback loopback, armazenamento
AES-256-GCM, refresh lazy, CLI, modo `token` explícito e validação da conta
`act_1598606388477916`. O servidor Meta atualmente anuncia um
`registration_endpoint`, portanto DCR é o método recomendado. CIMD será
reconhecido apenas como alternativa futura caso o servidor anuncie
`client_id_metadata_document_supported`; esta mudança não hospedará um
documento público de cliente.

O registro DCR é uma operação externa que só ocorrerá quando o operador
executar `login`. Testes não chamarão endpoints reais da Meta. O callback
continuará local em `http://127.0.0.1:8766/oauth/callback`; acesso remoto ao
navegador será feito exclusivamente por túnel SSH.

## Fluxo de autenticação

1. `login` lê o envelope local, se existir, e faz discovery do servidor OAuth.
2. O Brain valida issuer, resource, endpoints HTTPS, hosts allowlisted, grants,
   PKCE S256, escopo e `registration_endpoint`.
3. Se não houver uma registration válida para o mesmo issuer/resource/callback,
   o Brain envia um pedido DCR com `redirect_uris`, `grant_types`,
   `response_types`, `token_endpoint_auth_method=none`, nome técnico do cliente
   e somente `ads_read`.
4. A resposta DCR deve conter um `client_id`; qualquer segredo, quando
   publicado pelo servidor, será tratado como segredo e armazenado apenas no
   envelope criptografado. Campos desconhecidos não serão propagados ao CEO ou
   a logs.
5. O Brain gera `state` e `code_verifier`, exibe a URL de autorização e recebe
   um callback single-use. A autorização inclui o `client_id` dinâmico, callback,
   resource, `state`, challenge S256 e `ads_read`.
6. O código é trocado por access/refresh tokens. Scopes concedidos devem ser
   exatamente os solicitados; permissões de escrita ou escopos não solicitados
   invalidam a sessão.
7. O envelope é substituído atomicamente. O serviço usa o `client_id` salvo,
   renova tokens perto da expiração e recarrega uma sessão autorizada por outro
   processo quando o envelope muda.

Não haverá fallback silencioso para token legado, nova tentativa automática com
escopos mais amplos, nem envio de tokens do ChatGPT para o Brain. Se DCR for
anunciado mas rejeitar o registro, `login` falhará com erro técnico bounded e o
atendimento do CEO continuará sem atribuição confirmada.

## Armazenamento

O envelope AES-GCM atual será estendido para distinguir:

- `dynamic_client`: issuer, resource, callback, client_id, eventual
  client_secret, registration timestamp, client expiration e eventual
  registration access token;
- `credentials`: access/refresh token, expirações, scopes e timestamps.

O mesmo AAD, chave root-only, modo `0600`, diretórios privados, leitura
limitada, escrita atômica com `fsync`, `O_NOFOLLOW` e validações de owner/mode
continuarão obrigatórios. O registro salvo será vinculado ao resource, issuer,
callback e escopos que o Brain validou; uma alteração desses parâmetros exige
novo registro. `clear` removerá o envelope sem precisar decifrá-lo.

## CLI e configuração

`configure` deixará de ser necessário no caminho normal. `login` fará
discovery/registro/autorização; `status`, `clear` e `probe` permanecerão.
Opções de teste poderão injetar metadata/token requesters, mas a CLI de
produção não aceitará client secret em argv, stdin não interativo ou arquivo de
configuração do Brain.

As configurações de runtime continuam:

```text
BRAIN_META_ADS_MCP_AUTH_MODE=oauth|token
BRAIN_META_ADS_OAUTH_STORE=/var/lib/brain/credentials/meta-ads-oauth.json.enc
BRAIN_META_ADS_OAUTH_KEY_FILE=/etc/brain/meta-ads-oauth.key
BRAIN_META_ADS_OAUTH_REDIRECT_URI=http://127.0.0.1:8766/oauth/callback
```

`oauth` usará somente o provedor DCR; `token` continuará sendo rollback
explícito. Falhas de registro, login, refresh ou `probe` deixam a atribuição em
`pending`/`unavailable`, sem interromper a conversa ou expor credenciais no
`conversation_context({})`.

## Segurança e conta

- Somente HTTPS e hosts Meta allowlisted serão aceitos para discovery, registro,
  autorização e token.
- O callback validará path, state, uso único e, quando anunciado, issuer da
  resposta.
- O escopo inicial será estritamente `ads_read`; `ads_management`,
  `business_management` e qualquer escrita serão rejeitados.
- O `probe` continuará exigindo presença única da conta
  `1598606388477916` e não aceitará uma conta escolhida por resposta externa.
- Tokens, registration access tokens, client secrets, códigos e respostas brutas
  não aparecerão em logs, métricas, SQLite, contexto do CEO, nomes de arquivo ou
  mensagens de erro.

## Compatibilidade e rollout

O modo legado por token não será removido. A implementação será compatível com
envelopes OAuth existentes apenas quando eles contiverem client registration
válida; envelopes antigos com client pré-registrado poderão ser migrados uma
vez durante o primeiro `login`, sem fallback silencioso. O rollout é:

1. atualizar o Brain com DCR, mantendo atribuição desabilitada;
2. executar `login` por túnel SSH;
3. confirmar `status` e `probe` para a conta fixada;
4. habilitar `BRAIN_META_ADS_MCP_AUTH_MODE=oauth`;
5. fazer sincronização inicial e um CTWA controlado;
6. monitorar `ready`, `refreshing`, `degraded`, `expired` e `missing`.

Rollback é mudar explicitamente para `token` ou desabilitar a atribuição. Em
caso de comprometimento, executar `clear` e revogar o consentimento na Meta.

## Aceite e testes

Adicionar testes para discovery/allowlist do registro, payload DCR mínimo,
rejeição de endpoint ou resposta inválida, persistência e reuso do client_id,
expiração/rotação do registro, callback e PKCE com client dinâmico, scopes
exatos, refresh e `invalid_grant`, corrupção/atomicidade/permissões do envelope,
reinício do Brain, `status`/`probe`, conta divergente, fallback token explícito,
ausência de segredos em logs/contexto e entrega da atribuição confirmada ao CEO.

Manter toda a suíte Python e Observer existente, acrescentando testes de
integração somente com servidores HTTP locais falsos.
