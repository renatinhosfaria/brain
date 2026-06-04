import sqlalchemy as sa
from sqlalchemy import text


def test_extensoes_vector_e_age_presentes(sync_dsn):
    engine = sa.create_engine(sync_dsn)
    with engine.connect() as conn:
        rows = conn.execute(
            text("SELECT extname FROM pg_extension WHERE extname IN ('vector', 'age')")
        ).scalars().all()
    assert set(rows) == {"vector", "age"}


def test_grafo_brain_existe(sync_dsn):
    engine = sa.create_engine(sync_dsn)
    with engine.connect() as conn:
        conn.execute(text("LOAD 'age'"))
        conn.execute(text('SET search_path = ag_catalog, "$user", public'))
        names = conn.execute(text("SELECT name::text FROM ag_graph")).scalars().all()
    assert "brain" in names


def test_vector_aceita_2000_dims(sync_dsn):
    engine = sa.create_engine(sync_dsn)
    with engine.connect() as conn:
        conn.execute(text("CREATE TEMP TABLE t (v vector(2000))"))
        conn.execute(text("INSERT INTO t (v) VALUES (:v)"), {"v": "[" + ",".join(["0"] * 2000) + "]"})
        n = conn.execute(text("SELECT count(*) FROM t")).scalar()
    assert n == 1
