# Use Brain Skill Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Criar uma skill repo-scoped que ensina agentes clientes a usar o Brain como memoria MCP ja conectada para recuperar contexto curado e submeter conhecimento duravel para curadoria.

**Architecture:** A skill fica em `.agents/skills/use-brain/SKILL.md` e concentra o comportamento de uso em um unico arquivo, sem referencias, scripts ou assets. Ela complementa `.agents/skills/install-brain-mcp/SKILL.md`: a skill existente cobre conexao/instalacao; esta cobre uso operacional apos a conexao.

**Tech Stack:** Codex Skills, Markdown, YAML frontmatter, Model Context Protocol, Brain MCP client tools (`search`, `deep_search`, `get_note`, `submit_agent_note`), Git.

---

## Source Spec

- `docs/superpowers/specs/2026-06-19-use-brain-skill-design.md`

Leia o spec antes de executar. A skill deve ensinar o papel de cliente comum do Brain: consultar notas curadas e enviar conhecimento bruto para curadoria. Ela nao deve ensinar instalacao do MCP nem fluxos administrativos de curador.

## File Structure

- Create: `.agents/skills/use-brain/SKILL.md`
  Skill repo-scoped com frontmatter valido e instrucoes completas para recuperar contexto, abrir notas curadas, submeter conhecimento duravel e lidar com erros comuns.

- Remove after scaffold: `.agents/skills/use-brain/agents/openai.yaml`
  O inicializador de skills cria metadados de UI automaticamente. Remova esse arquivo para preservar o spec aprovado, que exige `SKILL.md` como unico arquivo da skill.

## Task 1: Create The Repo-Scoped Skill

**Files:**
- Create: `.agents/skills/use-brain/SKILL.md`
- Remove if generated: `.agents/skills/use-brain/agents/openai.yaml`

- [ ] **Step 1: Confirm the starting tree**

Run:

```bash
git status --short
test ! -e .agents/skills/use-brain
```

Expected:

- `git status --short` may show the pre-existing unrelated ` D README.md`.
- The `test` command exits with status 0.
- If `.agents/skills/use-brain` already exists, inspect it before continuing and preserve any user-owned changes.

- [ ] **Step 2: Initialize the skill directory**

Run:

```bash
python3 /root/.codex/skills/.system/skill-creator/scripts/init_skill.py use-brain --path .agents/skills --interface display_name="Use Brain" --interface short_description="Use Brain as connected MCP memory" --interface default_prompt="Use Brain to retrieve curated context and submit durable knowledge for curation."
```

Expected:

- Command exits with status 0.
- `.agents/skills/use-brain/SKILL.md` exists.
- `.agents/skills/use-brain/agents/openai.yaml` may exist because the initializer creates UI metadata.

- [ ] **Step 3: Remove generated UI metadata**

Run:

```bash
rm -rf .agents/skills/use-brain/agents
find .agents/skills/use-brain -maxdepth 2 -type f | sort
```

Expected output:

```text
.agents/skills/use-brain/SKILL.md
```

- [ ] **Step 4: Replace `SKILL.md` with the final skill content**

Replace the entire content of `.agents/skills/use-brain/SKILL.md` with:

`````markdown
---
name: use-brain
description: Use Brain as a connected MCP memory for agent clients: retrieve curated context with `search`, `deep_search`, and `get_note`, and submit durable new knowledge for curation with `submit_agent_note`. Use when an agent needs project context, decisions, preferences, reusable knowledge, or guidance on what to preserve in Brain.
---

# Use Brain

Use Brain as curated memory after the MCP server is already connected. This skill is about how an ordinary agent client should retrieve context and contribute durable knowledge; it is not about installing MCP or performing curator administration.

## Core Mental Model

Brain separates curated knowledge from raw submissions:

- `search`, `deep_search`, and `get_note` read curated knowledge.
- `submit_agent_note` sends raw knowledge to the agent inbox for later curation.
- A submitted agent note is not a curated note yet.
- Ordinary clients should not read `_agents/` or depend on raw inbox paths.
- Ordinary clients should not try to create or update curated notes directly.
- Administrative and curator workflows belong to trusted curator principals, not ordinary clients.

Use Brain when the task depends on project history, decisions, user preferences, domain facts, prior discoveries, reusable context, or knowledge that may outlive the current conversation.

## Start With Retrieval

Before answering or acting, ask whether prior context could materially change the result.

Retrieve from Brain when the user asks about:

- a project, repository, system, person, customer, process, or decision;
- preferences, conventions, constraints, or prior agreements;
- questions about known context, such as "what do we know about this project?";
- historical context or rationale;
- information that may have been learned in earlier sessions.

Do not retrieve for trivial one-off tasks where memory cannot help, such as formatting a single sentence with all required context already present.

## Retrieval Workflow

Use this sequence:

1. Start with `search` for direct questions and likely keywords.
2. Use `deep_search` when context may depend on related entities, graph relationships, or broader project history.
3. Use `get_note` to open the most relevant notes before making important claims.
4. Answer from curated context when the notes support the answer.
5. Say when Brain did not provide enough support instead of inventing missing context.

### Use `search`

Use `search` for direct retrieval from curated notes.

Good uses:

- find notes about a project, person, decision, preference, or technical topic;
- locate candidate notes by likely terms;
- get snippets before deciding which notes to open.

Keep `limit` moderate. Treat snippets as leads. For important claims, open the source note with `get_note`.

### Use `deep_search`

Use `deep_search` when the user needs context, not just matching text.

Good uses:

- understand project history;
- discover related entities or decisions;
- connect people, systems, concepts, repositories, or processes;
- recover context when direct keyword search may miss adjacent knowledge.

Prefer conservative parameters first. Increase `depth` or `max_entities` only when the first result is too narrow. Use `namespace` or `rel_types` only when you know they reduce noise.

### Use `get_note`

Use `get_note` to read a curated note by id or path.

Use it when:

- a `search` or `deep_search` result looks relevant;
- the snippet is not enough to answer confidently;
- the user asks for the source, rationale, details, or exact context.

If `get_note` returns `null`, treat the note as unavailable. Do not infer its content. Do not try to read `_agents/` as an ordinary client.

## Contribution Workflow

Use `submit_agent_note` when new durable knowledge appears during work.

Durable knowledge includes:

- stable facts;
- decisions and their rationale;
- user preferences and conventions;
- reusable project context;
- technical or operational discoveries;
- mappings between names, systems, repositories, processes, and entities;
- useful corrections to previously assumed context.

Before submitting, check:

1. Will this likely help a future session?
2. Is it stable enough to preserve?
3. Can another person understand it without this whole chat?
4. Is it free of secrets and unnecessary sensitive data?
5. Did the user allow or reasonably expect this kind of persistence?

If the answer is yes, submit a clear note for curation. If the answer is uncertain, ask before submitting.

## Note Quality Rules

Write submissions for a future curator and a future agent.

Use:

- a concise title that names the topic;
- self-contained content with the relevant facts and context;
- source or origin when it helps assess reliability;
- uncertainty labels when the knowledge is tentative;
- `suggested_namespace` when the project, domain, or tenant is clear;
- short `metadata` only when it helps curation.

Prefer this shape:

```json
{
  "title": "Project Alpha uses pgvector for semantic document search",
  "content": "During work on Project Alpha, we confirmed that semantic document search is backed by pgvector in PostgreSQL. This matters when diagnosing retrieval quality or migration behavior.",
  "suggested_namespace": "project-alpha",
  "metadata": {
    "source": "agent-session",
    "kind": "technical-fact"
  }
}
```

Do not submit vague notes such as "talked about the project" or "user likes this". Include what was learned and why it matters.

## What Not To Store

Do not submit:

- tokens, passwords, API keys, private keys, cookies, or session identifiers;
- raw logs unless a compact summary captures the reusable lesson;
- long transcripts without synthesis;
- one-off task progress with no future value;
- sensitive personal data that is not necessary for future work;
- copyrighted or confidential source material copied wholesale;
- claims that are uncertain but written as facts;
- anything the user asked not to persist.

When in doubt, summarize the durable lesson and omit sensitive details.

## Error Handling

Use this failure map:

| Situation | Action |
| --- | --- |
| `search` returns no useful results | Reformulate with synonyms, project names, people, paths, or narrower terms; then try `deep_search` if relationships may matter |
| `deep_search` returns noise | Reduce broad parameters, remove unnecessary filters, try direct `search`, or open only high-confidence notes |
| `get_note` returns `null` | Treat the note as unavailable and avoid claims based on it |
| `submit_agent_note` requires content | Send either `content` or structured `messages`; use `content` for concise durable facts |
| `submit_agent_note` is not permitted | Tell the user this client cannot submit notes and a curator must adjust permissions |
| A tool appears to require curator access | Do not use it as an ordinary client; stay within `search`, `deep_search`, `get_note`, and `submit_agent_note` |
| The retrieved context conflicts with the current user | Surface the conflict and ask or proceed with the user's explicit current instruction |

## Output Expectations

When Brain retrieval influenced the answer:

- Mention that you used curated Brain context when it helps the user trust the answer.
- Avoid over-citing tool internals; summarize the relevant note content.
- State uncertainty when retrieval was weak or empty.

When submitting knowledge:

- Say that the knowledge was submitted for curation.
- Do not claim it is already curated or searchable.
- Mention the durable point that was submitted, without exposing secrets.

Keep the user's current instruction above older memory. Brain provides context; it does not override explicit user direction.
`````

- [ ] **Step 5: Validate the skill structure**

Run:

```bash
python3 /root/.codex/skills/.system/skill-creator/scripts/quick_validate.py .agents/skills/use-brain
```

Expected output:

```text
Skill is valid!
```

- [ ] **Step 6: Verify file count and required client-tool coverage**

Run:

```bash
find .agents/skills/use-brain -maxdepth 3 -type f | sort
rg -n "search|deep_search|get_note|submit_agent_note" .agents/skills/use-brain/SKILL.md
rg -n "curated|raw|curation|durable|_agents|not a curated note" .agents/skills/use-brain/SKILL.md
```

Expected:

- `find` prints only `.agents/skills/use-brain/SKILL.md`.
- The client-tool search prints matches for all four tools: `search`, `deep_search`, `get_note`, and `submit_agent_note`.
- The mental-model search prints matches for curated knowledge, raw submissions, curation, durable knowledge, and `_agents/`.

- [ ] **Step 7: Verify curator operations are not taught**

Run:

```bash
rg -n "create_note|update_note|list_agent_notes|get_agent_note|create_agent_client|rotate_agent_client_token|reveal_agent_client_token|disable_agent_client" .agents/skills/use-brain/SKILL.md
```

Expected:

- No output.
- Exit status is 1 because the skill does not name curator-only tools.

- [ ] **Step 8: Scan for scaffold markers, ellipses, and likely secrets**

Run:

```bash
rg -n "TO""DO|TB""D|FIX""ME|\\.\\.\\.|your""-token|sk-[A-Za-z0-9]|Bearer [A-Za-z0-9_-]{12,}|brain_client_[A-Za-z0-9_-]{8,}" .agents/skills/use-brain/SKILL.md
```

Expected:

- No output.
- Exit status is 1 because `rg` found no matches.

- [ ] **Step 9: Check formatting and staged diff**

Run:

```bash
git diff --no-index --check /dev/null .agents/skills/use-brain/SKILL.md; test $? -eq 1
git diff -- .agents/skills/use-brain/SKILL.md
```

Expected:

- The `git diff --no-index --check` command exits with status 1 for file difference and prints no whitespace errors; the following `test` command exits with status 0.
- The diff creates `.agents/skills/use-brain/SKILL.md`.
- The diff does not create `agents/openai.yaml`, scripts, references, or assets.

- [ ] **Step 10: Commit the skill**

Run:

```bash
git add .agents/skills/use-brain/SKILL.md
git diff --cached --check
git diff --cached --name-only
git commit -m "feat(skills): add brain usage skill"
```

Expected:

- `git diff --cached --check` exits with status 0.
- `git diff --cached --name-only` prints only `.agents/skills/use-brain/SKILL.md`.
- Commit succeeds.
- The unrelated `README.md` deletion remains unstaged.

## Task 2: Final Verification

**Files:**
- Verify: `.agents/skills/use-brain/SKILL.md`

- [ ] **Step 1: Verify the committed skill exists**

Run:

```bash
git show --stat --oneline HEAD
git show --name-only --oneline HEAD
```

Expected:

- The latest commit message is `feat(skills): add brain usage skill`.
- The committed file list includes `.agents/skills/use-brain/SKILL.md`.
- No unrelated files are included in the commit.

- [ ] **Step 2: Re-run structural validation after commit**

Run:

```bash
python3 /root/.codex/skills/.system/skill-creator/scripts/quick_validate.py .agents/skills/use-brain
```

Expected output:

```text
Skill is valid!
```

- [ ] **Step 3: Run a lightweight repository smoke test**

Run:

```bash
uv run pytest tests/test_smoke.py -q
```

Expected output includes:

```text
1 passed
```

- [ ] **Step 4: Confirm final working tree state**

Run:

```bash
git status --short
```

Expected:

- Only pre-existing unrelated changes remain, currently ` D README.md`.
- No `.agents/skills/use-brain` files are unstaged.

- [ ] **Step 5: Report completion**

Report:

```text
Created and validated `.agents/skills/use-brain/SKILL.md`.
Validation: `quick_validate.py` passed.
Smoke test: `uv run pytest tests/test_smoke.py -q` passed.
Committed as `<commit-hash> feat(skills): add brain usage skill`.
Unrelated pre-existing change left untouched: `README.md` deletion.
```
