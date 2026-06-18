from mcp.server.fastmcp import FastMCP

from brain.mcp import handlers
from brain.mcp.handlers import Deps


def create_mcp_server(deps: Deps) -> FastMCP:
    mcp = FastMCP("brain", stateless_http=True, streamable_http_path="/")

    @mcp.tool()
    async def remember(namespace: str, messages: list[dict], metadata: dict | None = None) -> dict:
        return await handlers.remember(deps, namespace, messages, metadata)

    @mcp.tool()
    async def search(query: str, namespace: str | None = None,
                     limit: int = 10, include_graph: bool = False) -> dict:
        return await handlers.search(deps, query, namespace, limit, include_graph)

    @mcp.tool()
    async def submit_agent_note(
        title: str | None = None,
        content: str | None = None,
        messages: list[dict] | None = None,
        suggested_namespace: str | None = None,
        metadata: dict | None = None,
    ) -> dict:
        return await handlers.submit_agent_note(
            deps,
            title,
            content,
            messages,
            suggested_namespace,
            metadata,
        )

    @mcp.tool()
    async def get_memory(id: str) -> dict | None:
        return await handlers.get_memory(deps, id)

    @mcp.tool()
    async def list_memories(namespace: str | None = None) -> list[dict]:
        return await handlers.list_memories(deps, namespace)

    @mcp.tool()
    async def update_memory(id: str, content: str | None = None) -> dict | None:
        return await handlers.update_memory(deps, id, content)

    @mcp.tool()
    async def move_memory(id: str, namespace: str) -> dict | None:
        return await handlers.move_memory(deps, id, namespace)

    @mcp.tool()
    async def delete_memory(id: str) -> dict:
        return await handlers.delete_memory(deps, id)

    @mcp.tool()
    async def merge_memories(ids: list[str], into: str | None = None) -> dict:
        return await handlers.merge_memories(deps, ids, into)

    @mcp.tool()
    async def get_document(id_or_path: str) -> dict | None:
        return await handlers.get_document(deps, id_or_path)

    @mcp.tool()
    async def list_documents(namespace: str | None = None) -> list[dict]:
        return await handlers.list_documents(deps, namespace)

    @mcp.tool()
    async def reindex(repo_path: str, namespace: str) -> dict:
        return await handlers.reindex(deps, repo_path, namespace)

    @mcp.tool()
    async def create_agent_client(
        name: str,
        slug: str | None = None,
        description: str | None = None,
        capture_policy: str | None = None,
        recommended_instructions: str | None = None,
        metadata: dict | None = None,
    ) -> dict:
        return await handlers.create_agent_client(
            deps,
            name,
            slug,
            description,
            capture_policy,
            recommended_instructions,
            metadata,
        )

    @mcp.tool()
    async def list_agent_clients() -> list[dict]:
        return await handlers.list_agent_clients(deps)

    @mcp.tool()
    async def get_agent_client(slug: str) -> dict | None:
        return await handlers.get_agent_client(deps, slug)

    @mcp.tool()
    async def reveal_agent_client_token(slug: str) -> dict:
        return await handlers.reveal_agent_client_token(deps, slug)

    @mcp.tool()
    async def rotate_agent_client_token(slug: str) -> dict:
        return await handlers.rotate_agent_client_token(deps, slug)

    @mcp.tool()
    async def disable_agent_client(slug: str) -> dict:
        return await handlers.disable_agent_client(deps, slug)

    @mcp.tool()
    async def get_entity(name: str, namespace: str) -> dict | None:
        return await handlers.get_entity(deps, name, namespace)

    @mcp.tool()
    async def search_entities(query: str, namespace: str) -> list[dict]:
        return await handlers.search_entities(deps, query, namespace)

    @mcp.tool()
    async def get_related(entity: str, namespace: str, depth: int = 1) -> list[dict]:
        return await handlers.get_related(deps, entity, namespace, depth)

    @mcp.tool()
    async def update_entity(name: str, namespace: str, props: dict) -> dict:
        return await handlers.update_entity(deps, name, namespace, props)

    @mcp.tool()
    async def merge_entities(sources: list[str], into: str, namespace: str) -> dict:
        return await handlers.merge_entities(deps, sources, into, namespace)

    @mcp.tool()
    async def delete_entity(name: str, namespace: str) -> dict:
        return await handlers.delete_entity(deps, name, namespace)

    @mcp.tool()
    async def create_namespace(name: str, description: str | None = None) -> dict:
        return await handlers.create_namespace(deps, name, description)

    @mcp.tool()
    async def list_namespaces() -> list[dict]:
        return await handlers.list_namespaces(deps)

    return mcp
