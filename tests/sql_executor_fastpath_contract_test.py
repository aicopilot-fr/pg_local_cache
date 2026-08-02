#!/usr/bin/env python3
"""Source contracts for the transparent SQL executor fast path."""

from __future__ import annotations

from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
SOURCE = (ROOT / "src" / "pg_local_cache_sql.c").read_text(encoding="utf-8")
CORE = (ROOT / "src" / "pg_local_cache.c").read_text(encoding="utf-8")
INSTALL_SQL = (ROOT / "sql" / "pg_local_cache--1.0.0.sql").read_text(
    encoding="utf-8"
)


def source_function(source: str, name: str) -> str:
    marker = f"\n{name}("
    start = source.find(marker)
    if start < 0:
        raise AssertionError(f"C function {name}() is missing")
    opening = source.find("{", start)
    if opening < 0:
        raise AssertionError(f"C function {name}() has no body")
    depth = 0
    for position in range(opening, len(source)):
        character = source[position]
        if character == "{":
            depth += 1
        elif character == "}":
            depth -= 1
            if depth == 0:
                return source[start : position + 1]
    raise AssertionError(f"C function {name}() has an unterminated body")


def c_function(name: str) -> str:
    return source_function(SOURCE, name)


def sql_function(name: str) -> str:
    marker = f"CREATE FUNCTION {name}()"
    start = INSTALL_SQL.find(marker)
    if start < 0:
        raise AssertionError(f"SQL function {name}() is missing")
    end = INSTALL_SQL.find("$function$;", start)
    if end < 0:
        raise AssertionError(f"SQL function {name}() has an unterminated body")
    return INSTALL_SQL[start : end + len("$function$;")]


class SqlExecutorFastPathContracts(unittest.TestCase):
    def test_fallback_executor_is_initialized_only_when_needed(self) -> None:
        begin = c_function("pglc_sql_begin")
        self.assertIn("state->child_plan =", begin)
        self.assertIn("state->child = NULL", begin)
        self.assertIn("state->css.custom_ps = NIL", begin)
        self.assertNotIn("ExecInitNode", begin)

        initialize = c_function("pglc_sql_init_child")
        self.assertIn("if (state->child == NULL)", initialize)
        self.assertIn("state->css.ss.ps.state->es_query_cxt", initialize)
        self.assertIn("ExecInitNode(state->child_plan", initialize)
        self.assertIn("state->css.custom_ps = list_make1(state->child)", initialize)

        fallback = c_function("pglc_sql_run_child")
        self.assertLess(
            fallback.index("pglc_sql_init_child(state)"),
            fallback.index("ExecProcNode(child)"),
        )

    def test_explain_rescan_and_end_handle_a_lazy_child(self) -> None:
        explain = c_function("pglc_sql_explain")
        self.assertIn("if (state->child == NULL)", explain)
        self.assertIn("pglc_sql_init_child(state)", explain)
        self.assertLess(
            explain.index("pglc_sql_init_child(state)"),
            explain.index('ExplainPropertyText("Cache Namespace"'),
        )

        rescan = c_function("pglc_sql_rescan")
        self.assertIn("if (state->child != NULL)", rescan)
        self.assertIn("ExecReScan(state->child)", rescan)
        end = c_function("pglc_sql_end")
        self.assertIn("if (state->child != NULL)", end)
        self.assertIn("ExecEndNode(state->child)", end)

    def test_latest_visibility_slot_is_allocated_only_for_a_fill(self) -> None:
        begin = c_function("pglc_sql_begin")
        self.assertIn("state->latest_slot = NULL", begin)
        self.assertNotIn("table_slot_create", begin)

        initialize = c_function("pglc_sql_init_latest_slot")
        self.assertIn("if (state->latest_slot == NULL)", initialize)
        self.assertIn("state->css.ss.ps.state->es_query_cxt", initialize)
        self.assertIn("table_slot_create", initialize)
        store = c_function("pglc_sql_maybe_store")
        self.assertLess(
            store.index("if (load_id == 0"),
            store.index("pglc_sql_init_latest_slot(state)"),
        )

    def test_hit_payload_is_copied_once_into_an_aligned_query_buffer(self) -> None:
        create = c_function("pglc_sql_create_scan_state")
        self.assertIn(
            "state_size = MAXALIGN(sizeof(PgLocalCacheSqlScanState))", create
        )
        self.assertIn("palloc(add_size(state_size, PGLC_VALUE_MAX))", create)
        self.assertIn(
            "state->cache_buffer = ((char *) state) + state_size", create
        )
        self.assertNotIn("MemSet(state->cache_buffer", create)

        access = c_function("pglc_sql_access")
        self.assertNotIn("cached[PGLC_VALUE_MAX", access)
        self.assertIn(
            "pglc_cache_lookup_quiet(&state->mapping, canonical_key,",
            access,
        )
        self.assertIn("state->cache_buffer, PGLC_VALUE_MAX", access)
        self.assertIn("pglc_row_payload_decode_in_place(", access)
        self.assertNotIn("pglc_row_payload_decode(\n", access)

    def test_runtime_common_path_uses_an_exact_validation_version(self) -> None:
        invalidate = c_function("pglc_sql_relcache_invalidation")
        self.assertGreaterEqual(
            invalidate.count("relation_validation_token = 0"), 2
        )
        remember = c_function("pglc_sql_remember_relation_meta")
        self.assertIn("pglc_sql_next_relation_validation_token()", remember)

        planner = c_function("pglc_sql_set_rel_pathlist")
        self.assertIn("pglc_sql_relation_validation_token(", planner)
        self.assertIn("pglc_sql_int8_const(relation_validation_token)", planner)

        runtime = c_function("pglc_sql_validate_runtime")
        fast_at = runtime.index("state->relation_validation_token != 0")
        descriptor_at = runtime.index("descriptor = RelationGetDescr(relation)")
        cached_at = runtime.index("pglc_sql_cached_relation_meta")
        provenance_at = runtime.index("pglc_sql_relation_meta")
        self.assertLess(fast_at, descriptor_at)
        self.assertLess(descriptor_at, cached_at)
        self.assertLess(cached_at, provenance_at)
        for guard in (
            "entry->relation_oid == state->mapping.relation_oid",
            "entry->config_generation == current_generation",
            "entry->mapping_known",
            "entry->mapping_found",
            "entry->relation_validated",
            "entry->relation_validation_token ==",
        ):
            self.assertIn(guard, runtime[fast_at:descriptor_at])
        self.assertGreaterEqual(
            runtime.count("state->relation_validation_token = validation_token"),
            3,
        )

    def test_catalog_provenance_changes_bump_the_shared_generation(self) -> None:
        ddl = sql_function("_ddl_invalidate")
        for catalog in (
            "pg_catalog.pg_proc",
            "pg_catalog.pg_extension",
            "pg_catalog.pg_trigger",
        ):
            self.assertIn(catalog, ddl)
        self.assertIn("JOIN local_cache.mapping AS m", ddl)
        self.assertIn("t.tgrelid = m.relation", ddl)
        self.assertIn("PERFORM local_cache._reload()", ddl)

        reload_function = source_function(CORE, "pg_local_cache_reload")
        self.assertIn("pglc_collect_global(true)", reload_function)
        collect_global = source_function(CORE, "pglc_collect_global")
        self.assertIn("collect_dirty(PGLC_DIRTY_GLOBAL", collect_global)
        self.assertIn("local_bump_config = true", collect_global)
        dirty = source_function(CORE, "pglc_current_transaction_is_dirty")
        self.assertIn("local_dirty_hash != NULL", dirty)
        finish = source_function(CORE, "pglc_finish_dirty")
        self.assertIn("&pglc_shared->config_generation", finish)

        runtime = c_function("pglc_sql_validate_runtime")
        self.assertIn(
            "state->mapping.config_generation == current_generation", runtime
        )
        can_use = c_function("pglc_sql_can_use_cache")
        self.assertIn(
            "state->mapping.config_generation == pglc_config_generation()",
            can_use,
        )


if __name__ == "__main__":
    unittest.main()
