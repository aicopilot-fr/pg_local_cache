EXTENSION = pg_local_cache
MODULE_big = pg_local_cache

OBJS = src/pg_local_cache.o src/pg_local_cache_worker.o src/resp.o

DATA = sql/pg_local_cache--1.0.0.sql
PGFILEDESC = "pg_local_cache - RESP row cache embedded in PostgreSQL"

PG_CPPFLAGS = -I$(srcdir)/src
SHLIB_LINK =

PG_CONFIG ?= pg_config
PGXS := $(shell $(PG_CONFIG) --pgxs)
include $(PGXS)

.PHONY: verify-static integration load docker-smoke

verify-static: all
	python3 -m py_compile tests/integration.py tests/load.py
	bash -n docker/entrypoint.sh docker/healthcheck.sh \
		docker/initdb/010_pg_local_cache.sh tests/docker_smoke.sh

integration:
	python3 tests/integration.py

load:
	python3 tests/load.py

docker-smoke:
	bash tests/docker_smoke.sh
