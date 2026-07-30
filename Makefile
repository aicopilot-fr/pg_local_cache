EXTENSION = pg_kvik
MODULE_big = pg_kvik

OBJS = src/pg_kvik.o src/pg_kvik_worker.o src/resp.o

DATA = sql/pg_kvik--0.1.0.sql
PGFILEDESC = "pg_kvik - RESP row cache embedded in PostgreSQL"

PG_CPPFLAGS = -I$(srcdir)/src
SHLIB_LINK =

PG_CONFIG ?= pg_config
PGXS := $(shell $(PG_CONFIG) --pgxs)
include $(PGXS)

.PHONY: integration
integration:
	python3 tests/integration.py
