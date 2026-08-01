# syntax=docker/dockerfile:1.7

ARG POSTGRES_IMAGE=postgres:16.14-bookworm
FROM ${POSTGRES_IMAGE}

RUN apt-get update \
    && apt-get install --yes --no-install-recommends python3 \
    && rm -rf /var/lib/apt/lists/*

COPY benchmarks/compare.py /usr/local/lib/pg_local_cache/compare.py
COPY benchmarks/scenarios.py /usr/local/lib/pg_local_cache/scenarios.py
COPY benchmarks/whole_row.py /usr/local/lib/pg_local_cache/whole_row.py
COPY benchmarks/sql_only.py /usr/local/lib/pg_local_cache/sql_only.py

ENTRYPOINT ["python3", "/usr/local/lib/pg_local_cache/compare.py"]
CMD []
