from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)


def make_engine(database_url: str) -> AsyncEngine:
    # O tipo pgvector.sqlalchemy.Vector já serializa a lista para o literal
    # de vetor que o Postgres entende (e desserializa no retorno). NÃO registrar
    # o codec asyncpg (pgvector.asyncpg.register_vector) aqui: ele entraria em
    # conflito com o tipo do SQLAlchemy, tentando reparsear a string já pronta.
    #
    # Fixa search_path=public por conexão: o AGE cria um schema com o nome do
    # grafo ("brain"), que colide com o nome do usuário ("brain") e faz o
    # "$user" do search_path padrão apontar para esse schema — jogando as
    # tabelas da app nele em vez de public. As operações de grafo (graph/age.py)
    # ajustam o próprio search_path quando precisam de ag_catalog.
    return create_async_engine(
        database_url,
        pool_pre_ping=True,
        connect_args={"server_settings": {"search_path": "public"}},
    )


def make_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
