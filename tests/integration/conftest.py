import pytest
from testcontainers.postgres import PostgresContainer


@pytest.fixture(scope="session")
def pg_container():
    container = (
        PostgresContainer(
            image="brain-postgres:local",
            username="brain",
            password="brain",
            dbname="brain",
        )
        .with_command("postgres -c shared_preload_libraries=age")
    )
    with container as pg:
        yield pg


@pytest.fixture(scope="session")
def sync_dsn(pg_container):
    # DSN psycopg2 para asserts diretos em testes de infra
    return pg_container.get_connection_url()


@pytest.fixture(scope="session")
def async_dsn(pg_container):
    host = pg_container.get_container_host_ip()
    port = pg_container.get_exposed_port(5432)
    return f"postgresql+asyncpg://brain:brain@{host}:{port}/brain"
