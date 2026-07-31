# syntax=docker/dockerfile:1.7

ARG POSTGRES_IMAGE=postgres:16.14-bookworm
FROM ${POSTGRES_IMAGE}

RUN apt-get update \
    && apt-get install --yes --no-install-recommends python3 \
    && rm -rf /var/lib/apt/lists/*

COPY benchmarks/compare.py /usr/local/lib/pg_local_cache/compare.py

ENTRYPOINT ["python3", "/usr/local/lib/pg_local_cache/compare.py"]
CMD []
