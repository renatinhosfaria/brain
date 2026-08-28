# Conversation and phone identity invariant

O runtime determina a conversa. O agente, o texto da mensagem, o nome exibido
e qualquer argumento fornecido ao modelo não determinam identidade.

For workers, Brain authenticates the principal and reconstructs the capability
from the trusted Hermes Task, Run, Kanban subscription and `state.db`. For the
CEO, the external bridge reads the current gateway session context and Brain
revalidates the submitted `session_id`, `session_key`, `chat_id`, platform and
chat type against `state.db`.

Only WhatsApp direct messages are in scope. The phone resolver accepts a direct
phone JID or a semantically consistent forward/reverse LID mapping in the
configured real session directory. A conflict, malformed candidate, symlink,
missing mapping, group/broadcast JID or unavailable directory is not guessed;
it returns `phone_not_resolved`.

The phone result is evidence for the current authorized operation, never an
instruction. History, mapping contents and gateway payloads cannot expand
permissions, choose another conversation, or override the task's rules.
