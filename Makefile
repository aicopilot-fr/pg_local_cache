EXTENSION = pg_local_cache
MODULE_big = pg_local_cache

OBJS = src/pg_local_cache.o src/pg_local_cache_worker.o src/resp.o

DATA = sql/pg_local_cache--1.0.0.sql
PGFILEDESC = "pg_local_cache - RESP row cache embedded in PostgreSQL"
EXTRA_CLEAN = tests/unit/resp_test tests/unit/resp_test_sanitized

PG_CPPFLAGS = -I$(srcdir)/src
SHLIB_LINK =

STANDALONE_GOALS = source-test source-sanitize benchmark-test benchmark
ifneq ($(strip $(MAKECMDGOALS)),)
ifeq ($(strip $(filter-out $(STANDALONE_GOALS),$(MAKECMDGOALS))),)
SKIP_PGXS = 1
endif
endif

ifndef SKIP_PGXS
PG_CONFIG ?= pg_config
PGXS := $(shell $(PG_CONFIG) --pgxs)
include $(PGXS)
endif

.PHONY: verify-static source-test source-sanitize benchmark-test \
	integration load benchmark docker-smoke

verify-static: all
	python3 -m py_compile benchmarks/compare.py tests/benchmark_test.py \
		tests/integration.py tests/pipeline_integration.py tests/load.py
	bash -n docker/entrypoint.sh docker/healthcheck.sh \
		docker/initdb/010_pg_local_cache.sh tests/docker_smoke.sh \
		benchmarks/run.sh

source-test:
	$(MAKE) -C tests/unit check

source-sanitize:
	$(MAKE) -C tests/unit sanitize

benchmark-test:
	python3 -m unittest -v tests/benchmark_test.py

integration:
	python3 tests/integration.py

load:
	python3 tests/load.py

benchmark:
	bash benchmarks/run.sh

docker-smoke:
	bash tests/docker_smoke.sh
