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
    return create_async_engine(database_url, pool_pre_ping=True)


def make_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
