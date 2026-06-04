# ---- Estágio builder: compila o Apache AGE numa release fixa ----
FROM pgvector/pgvector:pg16 AS builder

ARG AGE_REF=release/PG16/1.5.0

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        git \
        postgresql-server-dev-16 \
        flex \
        bison \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN git clone https://github.com/apache/age.git /tmp/age \
    && cd /tmp/age \
    && git checkout ${AGE_REF} \
    && make \
    && make install

# ---- Imagem final: pgvector + artefatos do AGE copiados ----
FROM pgvector/pgvector:pg16

# Copia a lib e os arquivos de extensão do AGE compilados no builder
COPY --from=builder /usr/lib/postgresql/16/lib/age.so /usr/lib/postgresql/16/lib/age.so
COPY --from=builder /usr/share/postgresql/16/extension/age.control /usr/share/postgresql/16/extension/age.control
COPY --from=builder /usr/share/postgresql/16/extension/age--*.sql /usr/share/postgresql/16/extension/

# Scripts de init rodam na primeira inicialização do cluster
COPY init/01-extensions.sql /docker-entrypoint-initdb.d/01-extensions.sql
