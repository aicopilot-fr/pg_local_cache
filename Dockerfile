# syntax=docker/dockerfile:1.7

ARG POSTGRES_IMAGE=postgres:16.14-bookworm

FROM ${POSTGRES_IMAGE} AS builder

RUN apt-get update \
    && apt-get install --yes --no-install-recommends \
        build-essential \
        postgresql-server-dev-16 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build

COPY Makefile pg_local_cache.control ./
COPY sql/ ./sql/
COPY src/ ./src/

RUN make -j"$(nproc)" PG_CONFIG="/usr/lib/postgresql/16/bin/pg_config" \
    && make PG_CONFIG="/usr/lib/postgresql/16/bin/pg_config" \
        DESTDIR=/stage install

FROM ${POSTGRES_IMAGE} AS runtime

COPY --from=builder \
    /stage/usr/lib/postgresql/16/lib/pg_local_cache.so \
    /usr/lib/postgresql/16/lib/pg_local_cache.so
COPY --from=builder \
    /stage/usr/share/postgresql/16/extension/pg_local_cache.control \
    /usr/share/postgresql/16/extension/pg_local_cache.control
COPY --from=builder \
    /stage/usr/share/postgresql/16/extension/pg_local_cache--1.0.0.sql \
    /usr/share/postgresql/16/extension/pg_local_cache--1.0.0.sql

COPY --chmod=0755 docker/entrypoint.sh \
    /usr/local/bin/pg_local_cache_entrypoint
COPY --chmod=0755 docker/healthcheck.sh \
    /usr/local/bin/pg_local_cache_healthcheck
COPY --chmod=0755 docker/attach-table.sh \
    /usr/local/bin/pg_local_cache_attach
COPY --chmod=0755 docker/initdb/010_pg_local_cache.sh \
    /docker-entrypoint-initdb.d/010_pg_local_cache.sh

RUN install -d -o postgres -g postgres -m 0700 /run/pg_local_cache

ENV PG_LOCAL_CACHE_ROLE=local_cache_worker \
    PG_LOCAL_CACHE_BIND_ADDRESS=0.0.0.0 \
    PG_LOCAL_CACHE_PORT=6380 \
    PG_LOCAL_CACHE_WORKERS=8 \
    PG_LOCAL_CACHE_CACHE_ENTRIES=65536 \
    PG_LOCAL_CACHE_MAX_WORKER_PROCESSES=16 \
    PG_LOCAL_CACHE_IDLE_TIMEOUT_MS=300000 \
    PG_LOCAL_CACHE_STATEMENT_TIMEOUT_MS=2000 \
    PG_LOCAL_CACHE_LOCK_TIMEOUT_MS=250 \
    PG_LOCAL_CACHE_SINGLEFLIGHT_WAIT_MS=25 \
    PG_LOCAL_CACHE_MAX_PIPELINE_COMMANDS=256 \
    PG_LOCAL_CACHE_MAX_DIRTY_KEYS=4096 \
    PG_LOCAL_CACHE_AUTH_TOKEN_FILE=/run/secrets/pg_local_cache_auth_token

EXPOSE 5432 6380

HEALTHCHECK --interval=10s --timeout=5s --start-period=30s --retries=6 \
    CMD ["/usr/local/bin/pg_local_cache_healthcheck"]

ENTRYPOINT ["/usr/local/bin/pg_local_cache_entrypoint"]
CMD ["postgres"]
