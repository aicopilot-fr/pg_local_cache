#include "postgres.h"

#include "access/genam.h"
#include "access/stratnum.h"
#include "access/sysattr.h"
#include "access/table.h"
#include "access/tableam.h"
#include "access/transam.h"
#include "access/xact.h"
#include "access/xlog.h"
#include "catalog/dependency.h"
#include "catalog/namespace.h"
#include "catalog/pg_am_d.h"
#include "catalog/pg_class.h"
#include "catalog/pg_index.h"
#include "catalog/pg_inherits.h"
#include "catalog/pg_trigger.h"
#include "catalog/pg_type_d.h"
#include "commands/explain.h"
#include "commands/extension.h"
#include "commands/trigger.h"
#include "executor/executor.h"
#include "fmgr.h"
#include "miscadmin.h"
#include "nodes/extensible.h"
#include "nodes/makefuncs.h"
#include "nodes/nodeFuncs.h"
#include "optimizer/cost.h"
#include "optimizer/optimizer.h"
#include "optimizer/pathnode.h"
#include "optimizer/paths.h"
#include "optimizer/planner.h"
#include "optimizer/restrictinfo.h"
#include "parser/parse_coerce.h"
#include "utils/array.h"
#include "utils/builtins.h"
#include "utils/guc.h"
#include "utils/inval.h"
#include "utils/lsyscache.h"
#include "utils/memutils.h"
#include "utils/rel.h"
#include "utils/reltrigger.h"
#include "utils/snapmgr.h"
#include "utils/syscache.h"
#include "utils/typcache.h"

#include "pg_local_cache.h"
#include "key_codec.h"
#include "row_payload.h"

/*
 * This is intentionally a narrow transparent fast path.  It recognizes only
 *
 *     SELECT <direct columns> FROM mapped_table WHERE primary_key = Const/$1
 *
 * and wraps the exact primary-key IndexPath PostgreSQL would otherwise use.
 * The original IndexScan remains a child and is used for every miss or safety
 * fallback, so clients keep using ordinary SQL and ordinary PostgreSQL ACLs.
 */

#define PGLC_SQL_CUSTOM_NAME "pg_local_cache_sql"
#define PGLC_SQL_META_CACHE_SLOTS 32

#define PGLC_PRIVATE_NAMESPACE 0
#define PGLC_PRIVATE_RELATION 1
#define PGLC_PRIVATE_GENERATION 2
#define PGLC_PRIVATE_KEY_COUNT 3
#define PGLC_PRIVATE_KEY_ATTNOS 4
#define PGLC_PRIVATE_KEY_TYPES 5
#define PGLC_PRIVATE_KEY_TYPMODS 6
#define PGLC_PRIVATE_ROW_TYPE 7
#define PGLC_PRIVATE_ROW_NATTS 8
#define PGLC_PRIVATE_ROW_FINGERPRINT 9
#define PGLC_PRIVATE_RELATION_VALIDATION 10
#define PGLC_PRIVATE_KEY_EXPRS 11
#define PGLC_PRIVATE_PLAN_ITEMS 11

typedef struct PgLocalCacheSqlMeta
{
	char		nspace[PGLC_NAMESPACE_MAX];
	char		key_columns[PGLC_MAX_KEY_COLUMNS][NAMEDATALEN];
	Oid			relation_oid;
	int			key_count;
	AttrNumber key_attnos[PGLC_MAX_KEY_COLUMNS];
	Oid			key_types[PGLC_MAX_KEY_COLUMNS];
	int32		key_typmods[PGLC_MAX_KEY_COLUMNS];
	Oid			key_collations[PGLC_MAX_KEY_COLUMNS];
	Oid			row_type_oid;
	int32		row_typmod;
	int			row_natts;
	uint64		row_fingerprint;
	uint64		config_generation;
} PgLocalCacheSqlMeta;

typedef struct PgLocalCacheSqlScanState
{
	CustomScanState css;
	Plan	   *child_plan;
	PlanState  *child;
	int			child_eflags;
	List	   *key_exprs;
	TupleTableSlot *latest_slot;
	char	   *cache_buffer;
	PgLocalCacheMapping mapping;
	FmgrInfo	key_outputs[PGLC_MAX_KEY_COLUMNS];
	int			key_count;
	AttrNumber key_attnos[PGLC_MAX_KEY_COLUMNS];
	Oid			key_types[PGLC_MAX_KEY_COLUMNS];
	int32		key_typmods[PGLC_MAX_KEY_COLUMNS];
	Oid			row_type_oid;
	int32		row_typmod;
	int			row_natts;
	uint64		row_fingerprint;
	uint64		relation_validation_token;
	int			child_payload_resno;
	int			child_ctid_resno;
	int			child_xmin_resno;
	bool		runtime_valid;
	bool		done;
	uint64		hits;
	uint64		misses;
	uint64		bypasses;
} PgLocalCacheSqlScanState;

/*
 * Planning the same primary-key lookup repeatedly must not rescan the private
 * mapping table and re-walk every trigger and tuple attribute.  This small
 * backend-local direct-mapped cache is fenced by the shared mapping generation
 * and PostgreSQL relcache invalidations.  Collisions only discard an entry and
 * force full validation; they can never make an invalid relation cacheable.
 */
typedef struct PgLocalCacheSqlMetaCacheEntry
{
	Oid			relation_oid;
	uint64		config_generation;
	bool		mapping_known;
	bool		mapping_found;
	bool		relation_validated;
	uint64		relation_validation_token;
	PgLocalCacheSqlMeta meta;
} PgLocalCacheSqlMetaCacheEntry;

bool		pglc_sql_cache = true;

static set_rel_pathlist_hook_type previous_set_rel_pathlist_hook = NULL;
static planner_hook_type previous_planner_hook = NULL;
static PgLocalCacheSqlMetaCacheEntry
	pglc_sql_meta_cache[PGLC_SQL_META_CACHE_SLOTS];
static uint64 pglc_sql_relation_validation_sequence = 0;

static PlannedStmt *pglc_sql_planner(Query *parse, const char *query_string,
								 int cursor_options,
								 ParamListInfo bound_params);
static void pglc_sql_set_rel_pathlist(PlannerInfo *root, RelOptInfo *rel,
								  Index rti, RangeTblEntry *rte);
static Plan *pglc_sql_plan_custom_path(PlannerInfo *root, RelOptInfo *rel,
								  CustomPath *best_path, List *tlist,
								  List *clauses, List *custom_plans);
static Node *pglc_sql_create_scan_state(CustomScan *cscan);
static void pglc_sql_begin(CustomScanState *node, EState *estate, int eflags);
static TupleTableSlot *pglc_sql_exec(CustomScanState *node);
static void pglc_sql_end(CustomScanState *node);
static void pglc_sql_rescan(CustomScanState *node);
static void pglc_sql_explain(CustomScanState *node, List *ancestors,
								 ExplainState *es);
static void pglc_sql_relcache_invalidation(Datum argument, Oid relation_oid);

static const CustomPathMethods pglc_sql_path_methods = {
	.CustomName = PGLC_SQL_CUSTOM_NAME,
	.PlanCustomPath = pglc_sql_plan_custom_path
};

static const CustomScanMethods pglc_sql_scan_methods = {
	.CustomName = PGLC_SQL_CUSTOM_NAME,
	.CreateCustomScanState = pglc_sql_create_scan_state
};

static const CustomExecMethods pglc_sql_exec_methods = {
	.CustomName = PGLC_SQL_CUSTOM_NAME,
	.BeginCustomScan = pglc_sql_begin,
	.ExecCustomScan = pglc_sql_exec,
	.EndCustomScan = pglc_sql_end,
	.ReScanCustomScan = pglc_sql_rescan,
	.ExplainCustomScan = pglc_sql_explain
};

static Const *
pglc_sql_oid_const(Oid value)
{
	return makeConst(OIDOID, -1, InvalidOid, sizeof(Oid),
					 ObjectIdGetDatum(value), false, true);
}

static Const *
pglc_sql_int8_const(uint64 value)
{
	return makeConst(INT8OID, -1, InvalidOid, sizeof(int64),
					 Int64GetDatum((int64) value), false, FLOAT8PASSBYVAL);
}

static Oid
pglc_sql_private_oid(List *private, int index)
{
	Const	  *value = (Const *) list_nth(private, index);

	Assert(IsA(value, Const));
	Assert(value->consttype == OIDOID && !value->constisnull);
	return DatumGetObjectId(value->constvalue);
}

static uint64
pglc_sql_private_generation(List *private)
{
	Const	  *value = (Const *) list_nth(private, PGLC_PRIVATE_GENERATION);

	Assert(IsA(value, Const));
	Assert(value->consttype == INT8OID && !value->constisnull);
	return (uint64) DatumGetInt64(value->constvalue);
}

static uint64
pglc_sql_private_uint64(List *private, int index)
{
	Const	  *value = (Const *) list_nth(private, index);

	Assert(IsA(value, Const));
	Assert(value->consttype == INT8OID && !value->constisnull);
	return (uint64) DatumGetInt64(value->constvalue);
}

static int
pglc_sql_private_list_int(List *private, int index, int member)
{
	List	   *values = (List *) list_nth(private, index);

	Assert(IsA(values, List));
	return intVal(list_nth(values, member));
}

static Oid
pglc_sql_private_list_oid(List *private, int index, int member)
{
	List	   *values = (List *) list_nth(private, index);
	Const	  *value;

	Assert(IsA(values, List));
	value = (Const *) list_nth(values, member);
	Assert(IsA(value, Const));
	Assert(value->consttype == OIDOID && !value->constisnull);
	return DatumGetObjectId(value->constvalue);
}

static PgLocalCacheSqlMetaCacheEntry *
pglc_sql_meta_cache_entry(Oid relation_oid)
{
	return &pglc_sql_meta_cache[((uint32) relation_oid) &
		(PGLC_SQL_META_CACHE_SLOTS - 1)];
}

/*
 * Backend-local version of a fully validated direct-mapped entry.  Relcache
 * invalidation clears it; the next full validation gets a new non-zero value.
 */
static uint64
pglc_sql_next_relation_validation_token(void)
{
	pglc_sql_relation_validation_sequence++;
	if (pglc_sql_relation_validation_sequence == 0)
		pglc_sql_relation_validation_sequence++;
	return pglc_sql_relation_validation_sequence;
}

static uint64
pglc_sql_relation_validation_token(Oid relation_oid, uint64 generation)
{
	PgLocalCacheSqlMetaCacheEntry *entry =
		pglc_sql_meta_cache_entry(relation_oid);

	if (entry->relation_oid != relation_oid ||
		entry->config_generation != generation ||
		!entry->mapping_known || !entry->mapping_found ||
		!entry->relation_validated)
		return 0;
	return entry->relation_validation_token;
}

static void
pglc_sql_relcache_invalidation(Datum argument, Oid relation_oid)
{
	if (!OidIsValid(relation_oid))
	{
		int			index;

		for (index = 0; index < PGLC_SQL_META_CACHE_SLOTS; index++)
		{
			pglc_sql_meta_cache[index].relation_validated = false;
			pglc_sql_meta_cache[index].relation_validation_token = 0;
		}
	}
	else
	{
		PgLocalCacheSqlMetaCacheEntry *entry =
			pglc_sql_meta_cache_entry(relation_oid);

		if (entry->relation_oid == relation_oid)
		{
			entry->relation_validated = false;
			entry->relation_validation_token = 0;
		}
	}
	(void) argument;
}

static bool
pglc_sql_cached_mapping(Oid relation_oid, uint64 generation,
						PgLocalCacheSqlMeta *meta, bool *found)
{
	PgLocalCacheSqlMetaCacheEntry *entry =
		pglc_sql_meta_cache_entry(relation_oid);

	if (entry->relation_oid != relation_oid ||
		entry->config_generation != generation || !entry->mapping_known)
		return false;
	*found = entry->mapping_found;
	if (*found)
		*meta = entry->meta;
	return true;
}

static void
pglc_sql_remember_mapping(Oid relation_oid, uint64 generation,
						  const PgLocalCacheSqlMeta *meta, bool found)
{
	PgLocalCacheSqlMetaCacheEntry *entry =
		pglc_sql_meta_cache_entry(relation_oid);

	MemSet(entry, 0, sizeof(*entry));
	entry->relation_oid = relation_oid;
	entry->config_generation = generation;
	entry->mapping_known = true;
	entry->mapping_found = found;
	if (found)
		entry->meta = *meta;
}

/*
 * Read the extension's private mapping table below the SQL permission layer.
 * Application roles deliberately have no SELECT privilege on this table; the
 * query's own table ACL is still checked by standard ExecutorStart processing.
 */
static bool
pglc_sql_read_mapping_once(Oid relation_oid, uint64 generation,
						   PgLocalCacheSqlMeta *meta)
{
	Oid			namespace_oid;
	Oid			mapping_oid;
	Relation	mapping_relation;
	TableScanDesc scan;
	TupleTableSlot *slot;
	Snapshot	snapshot;
	AttrNumber namespace_attno;
	AttrNumber relation_attno;
	AttrNumber key_columns_attno;
	bool		found = false;

	namespace_oid = get_namespace_oid("local_cache", true);
	if (!OidIsValid(namespace_oid))
		return false;
	mapping_oid = get_relname_relid("mapping", namespace_oid);
	if (!OidIsValid(mapping_oid))
		return false;

	mapping_relation = try_table_open(mapping_oid, AccessShareLock);
	if (mapping_relation == NULL)
		return false;
	namespace_attno = get_attnum(mapping_oid, "namespace");
	relation_attno = get_attnum(mapping_oid, "relation");
	key_columns_attno = get_attnum(mapping_oid, "key_columns");
	if (namespace_attno == InvalidAttrNumber ||
		relation_attno == InvalidAttrNumber ||
		key_columns_attno == InvalidAttrNumber)
	{
		table_close(mapping_relation, AccessShareLock);
		return false;
	}

	snapshot = RegisterSnapshot(GetLatestSnapshot());
	scan = table_beginscan(mapping_relation, snapshot, 0, NULL);
	slot = table_slot_create(mapping_relation, NULL);
	while (table_scan_getnextslot(scan, ForwardScanDirection, slot))
	{
		Datum		datum;
		bool		isnull;

		datum = slot_getattr(slot, relation_attno, &isnull);
		if (isnull || DatumGetObjectId(datum) != relation_oid)
		{
			ExecClearTuple(slot);
			continue;
		}

		datum = slot_getattr(slot, namespace_attno, &isnull);
		if (!isnull)
		{
			char	   *nspace = TextDatumGetCString(datum);
			int			key_index;

			if (strlen(nspace) >= sizeof(meta->nspace))
				break;

			MemSet(meta, 0, sizeof(*meta));
			strlcpy(meta->nspace, nspace, sizeof(meta->nspace));
			meta->relation_oid = relation_oid;
			meta->config_generation = generation;

			{
				ArrayType  *key_array;
				Datum	   *key_datums;
				bool	   *key_nulls;
				int			key_count;
				int16		type_length;
				bool		type_by_value;
				char		type_alignment;

				datum = slot_getattr(slot, key_columns_attno, &isnull);
				if (isnull)
					break;
				key_array = DatumGetArrayTypeP(datum);
				get_typlenbyvalalign(NAMEOID, &type_length, &type_by_value,
								 &type_alignment);
				deconstruct_array(key_array, NAMEOID, type_length,
							  type_by_value, type_alignment,
							  &key_datums, &key_nulls, &key_count);
				if (ARR_NDIM(key_array) != 1 || ARR_LBOUND(key_array)[0] != 1 ||
					key_count < 1 || key_count > PGLC_MAX_KEY_COLUMNS)
					break;
				meta->key_count = key_count;
				for (key_index = 0; key_index < key_count; key_index++)
				{
					Name		key_name;

					if (key_nulls[key_index])
						break;
					key_name = DatumGetName(key_datums[key_index]);
					strlcpy(meta->key_columns[key_index],
							NameStr(*key_name), NAMEDATALEN);
				}
				if (key_index != key_count)
					break;
			}

			found = true;
			for (key_index = 0; key_index < meta->key_count; key_index++)
			{
				meta->key_attnos[key_index] = get_attnum(
					relation_oid, meta->key_columns[key_index]);
				if (meta->key_attnos[key_index] == InvalidAttrNumber)
					found = false;
			}
		}
		break;
	}

	ExecDropSingleTupleTableSlot(slot);
	table_endscan(scan);
	UnregisterSnapshot(snapshot);
	table_close(mapping_relation, AccessShareLock);
	return found;
}

static bool
pglc_sql_read_mapping(Oid relation_oid, PgLocalCacheSqlMeta *meta)
{
	int			attempt;

	for (attempt = 0; attempt < 2; attempt++)
	{
		uint64		before;
		uint64		after;
		bool		found;

		before = pglc_config_generation();
		if (pglc_sql_cached_mapping(relation_oid, before, meta, &found))
			return found;
		found = pglc_sql_read_mapping_once(relation_oid, before, meta);
		after = pglc_config_generation();
		if (before == after)
		{
			pglc_sql_remember_mapping(relation_oid, before, meta, found);
			return found;
		}
	}
	return false;
}

static bool
pglc_sql_trigger_function(Oid function_oid, Oid namespace_oid,
						  const char *expected_name)
{
	char	   *actual_name;
	Oid		   *argument_types = NULL;
	Oid			return_type;
	int			argument_count = 0;
	bool		matches;

	if (get_func_namespace(function_oid) != namespace_oid)
		return false;
	return_type = get_func_signature(function_oid, &argument_types,
									  &argument_count);
	if (argument_types != NULL)
		pfree(argument_types);
	if (return_type != TRIGGEROID || argument_count != 0)
		return false;
	actual_name = get_func_name(function_oid);
	if (actual_name == NULL)
		return false;
	matches = strcmp(actual_name, expected_name) == 0;
	pfree(actual_name);
	return matches;
}

static bool
pglc_sql_trigger_owned_by_extension(Oid trigger_oid, Oid extension_oid)
{
	List	   *extension_oids;
	bool		owned;

	if (!OidIsValid(trigger_oid) || !OidIsValid(extension_oid))
		return false;
	extension_oids = getAutoExtensionsOfObject(TriggerRelationId, trigger_oid);
	owned = list_member_oid(extension_oids, extension_oid);
	list_free(extension_oids);
	return owned;
}

static bool
pglc_sql_triggers_valid(Relation relation, const PgLocalCacheSqlMeta *meta,
						bool check_extension_ownership)
{
	TriggerDesc *trigger_desc = relation->trigdesc;
	Oid			namespace_oid;
	Oid			extension_oid;
	bool		guard_found = false;
	bool		row_found = false;
	bool		truncate_found = false;
	int			index;

	if (trigger_desc == NULL)
		return false;
	namespace_oid = get_namespace_oid("local_cache", true);
	if (!OidIsValid(namespace_oid))
		return false;
	extension_oid = InvalidOid;
	if (check_extension_ownership)
	{
		extension_oid = get_extension_oid("pg_local_cache", true);
		if (!OidIsValid(extension_oid))
			return false;
	}

	for (index = 0; index < trigger_desc->numtriggers; index++)
	{
		Trigger    *trigger = &trigger_desc->triggers[index];
		bool		plain_trigger;

		plain_trigger = !trigger->tgisinternal && !trigger->tgisclone &&
			!trigger->tgdeferrable && !trigger->tginitdeferred &&
			!OidIsValid(trigger->tgconstraint) &&
			!OidIsValid(trigger->tgconstrrelid) &&
			!OidIsValid(trigger->tgconstrindid) && trigger->tgnattr == 0 &&
			trigger->tgqual == NULL && trigger->tgoldtable == NULL &&
			trigger->tgnewtable == NULL;

		if (strcmp(trigger->tgname, "pg_local_cache_statement_guard") == 0)
		{
			guard_found = trigger->tgenabled == TRIGGER_FIRES_ALWAYS &&
				plain_trigger &&
				(!check_extension_ownership ||
				 pglc_sql_trigger_owned_by_extension(trigger->tgoid,
											extension_oid)) &&
				trigger->tgtype == (TRIGGER_TYPE_BEFORE | TRIGGER_TYPE_INSERT |
					TRIGGER_TYPE_UPDATE | TRIGGER_TYPE_DELETE |
					TRIGGER_TYPE_TRUNCATE) &&
				trigger->tgnargs == 0 &&
				pglc_sql_trigger_function(trigger->tgfoid, namespace_oid,
									  "_statement_guard");
		}
		else if (strcmp(trigger->tgname, "pg_local_cache_row_invalidate") == 0)
		{
			bool		key_arguments_match =
				trigger->tgnargs == meta->key_count + 1;
			int			key_index;

			if (key_arguments_match &&
				strcmp(trigger->tgargs[0], meta->nspace) != 0)
				key_arguments_match = false;
			for (key_index = 0;
				 key_arguments_match && key_index < meta->key_count;
				 key_index++)
			{
				if (strcmp(trigger->tgargs[key_index + 1],
						   meta->key_columns[key_index]) != 0)
					key_arguments_match = false;
			}

			row_found = trigger->tgenabled == TRIGGER_FIRES_ALWAYS &&
				plain_trigger &&
				(!check_extension_ownership ||
				 pglc_sql_trigger_owned_by_extension(trigger->tgoid,
											extension_oid)) &&
				trigger->tgtype == (TRIGGER_TYPE_ROW | TRIGGER_TYPE_INSERT |
					TRIGGER_TYPE_UPDATE | TRIGGER_TYPE_DELETE) &&
				key_arguments_match &&
				pglc_sql_trigger_function(trigger->tgfoid, namespace_oid,
									  "_row_invalidate");
		}
		else if (strcmp(trigger->tgname,
						"pg_local_cache_truncate_invalidate") == 0)
		{
			truncate_found = trigger->tgenabled == TRIGGER_FIRES_ALWAYS &&
				plain_trigger &&
				(!check_extension_ownership ||
				 pglc_sql_trigger_owned_by_extension(trigger->tgoid,
											extension_oid)) &&
				trigger->tgtype == TRIGGER_TYPE_TRUNCATE &&
				trigger->tgnargs == 1 &&
				pglc_sql_trigger_function(trigger->tgfoid, namespace_oid,
									  "_truncate_invalidate") &&
				strcmp(trigger->tgargs[0], meta->nspace) == 0;
		}
	}
	return guard_found && row_found && truncate_found;
}

static bool
pglc_sql_key_type_supported(Oid type_oid)
{
	return type_oid == INT2OID || type_oid == INT4OID ||
		type_oid == INT8OID || type_oid == TEXTOID ||
		type_oid == VARCHAROID || type_oid == BPCHAROID ||
		type_oid == UUIDOID;
}

/*
 * pg_class.relhassubclass is only a one-way hint: PostgreSQL may leave it set
 * after the last child is dropped.  Use the catalog scan performed by
 * find_inheritance_children() so a formerly inherited table can safely regain
 * the transparent fast path without requiring ANALYZE.
 */
static bool
pglc_sql_relation_has_children(Relation relation)
{
	List	   *children;
	bool		has_children;
	Oid			relation_oid = RelationGetRelid(relation);

	if (!relation->rd_rel->relhassubclass)
		return false;
	children = find_inheritance_children(relation_oid, NoLock);
	has_children = children != NIL;
	list_free(children);
	return has_children;
}

/*
 * relispartition is an inexpensive relcache check for declarative
 * partitions.  Traditional inheritance children do not set it, so consult
 * pg_inherits as well.  has_superclass() is exact while the caller holds a
 * lock on the relation.
 */
static bool
pglc_sql_relation_has_parent(Relation relation)
{
	if (relation->rd_rel->relispartition)
		return true;
	return has_superclass(RelationGetRelid(relation));
}

static bool
pglc_sql_source_relation_allowed(Relation relation)
{
	Oid			namespace_oid = relation->rd_rel->relnamespace;
	Oid			extension_oid;
	char	   *namespace_name;
	bool		disallowed_namespace;

	namespace_name = get_namespace_name(namespace_oid);
	if (namespace_name == NULL)
		return false;
	disallowed_namespace = strncmp(namespace_name, "pg_", 3) == 0 ||
		strcmp(namespace_name, "information_schema") == 0 ||
		strcmp(namespace_name, "local_cache") == 0;
	pfree(namespace_name);
	if (disallowed_namespace)
		return false;

	extension_oid = get_extension_oid("pg_local_cache", true);
	if (OidIsValid(extension_oid) &&
		get_extension_schema(extension_oid) == namespace_oid)
		return false;

	return !OidIsValid(getExtensionOfObject(RelationRelationId,
										 RelationGetRelid(relation)));
}

/*
 * standard_planner() consults the sticky relhassubclass hint before the
 * set_rel_pathlist hook runs.  If the last child was dropped, that would turn
 * an otherwise ordinary mapped query into a one-member inheritance query and
 * permanently hide our fast path until ANALYZE.  Under an AccessShareLock,
 * replace only that provably empty inheritance expansion in the query tree;
 * no catalog write or surprise table analysis is required.
 */
static void
pglc_sql_normalize_query_inheritance(Query *parse)
{
	ListCell   *cell;

	if (!pglc_sql_cache || pglc_shared == NULL || parse == NULL ||
		parse->commandType != CMD_SELECT)
		return;

	foreach(cell, parse->rtable)
	{
		RangeTblEntry *rte = (RangeTblEntry *) lfirst(cell);
		PgLocalCacheSqlMeta meta;
		Relation	relation;

		if (rte->rtekind != RTE_RELATION || !rte->inh)
			continue;

		relation = try_table_open(rte->relid, AccessShareLock);
		if (relation == NULL)
			continue;
		if (relation->rd_rel->relkind == RELKIND_RELATION &&
			relation->rd_rel->relpersistence == RELPERSISTENCE_PERMANENT &&
			relation->rd_rel->relhassubclass &&
			!pglc_sql_relation_has_children(relation) &&
			pglc_sql_source_relation_allowed(relation) &&
			pglc_sql_read_mapping(rte->relid, &meta))
			rte->inh = false;

		/* Keep the hierarchy stable through planning and execution. */
		table_close(relation, NoLock);
	}
}

static PlannedStmt *
pglc_sql_planner(Query *parse, const char *query_string, int cursor_options,
				 ParamListInfo bound_params)
{
	pglc_sql_normalize_query_inheritance(parse);
	if (previous_planner_hook != NULL)
		return previous_planner_hook(parse, query_string, cursor_options,
								 bound_params);
	return standard_planner(parse, query_string, cursor_options, bound_params);
}

static bool
pglc_sql_relation_base_meta(Relation relation, PgLocalCacheSqlMeta *meta,
							bool check_catalog_provenance)
{
	TupleDesc	descriptor;
	int			key_index;

	if (relation->rd_rel->relkind != RELKIND_RELATION ||
		relation->rd_rel->relpersistence != RELPERSISTENCE_PERMANENT ||
		relation->rd_rel->relam != HEAP_TABLE_AM_OID ||
		relation->rd_rel->relispartition ||
		relation->rd_rel->relrowsecurity || relation->rd_rel->relforcerowsecurity ||
		(check_catalog_provenance &&
		 !pglc_sql_source_relation_allowed(relation)) ||
		!pglc_sql_triggers_valid(relation, meta, check_catalog_provenance))
		return false;

	descriptor = RelationGetDescr(relation);
	if (meta->key_count < 1 || meta->key_count > PGLC_MAX_KEY_COLUMNS)
		return false;
	for (key_index = 0; key_index < meta->key_count; key_index++)
	{
		AttrNumber	key_attno = meta->key_attnos[key_index];
		Form_pg_attribute key_attribute;
		int			previous;

		if (key_attno <= 0 || key_attno > descriptor->natts)
			return false;
		key_attribute = TupleDescAttr(descriptor, key_attno - 1);
		if (key_attribute->attisdropped || !key_attribute->attnotnull ||
			!pglc_sql_key_type_supported(key_attribute->atttypid) ||
			(OidIsValid(key_attribute->attcollation) &&
			 !get_collation_isdeterministic(key_attribute->attcollation)))
			return false;
		for (previous = 0; previous < key_index; previous++)
		{
			if (meta->key_attnos[previous] == key_attno)
				return false;
		}
		meta->key_types[key_index] = key_attribute->atttypid;
		meta->key_typmods[key_index] = key_attribute->atttypmod;
		meta->key_collations[key_index] = key_attribute->attcollation;
	}

	meta->row_type_oid = relation->rd_rel->reltype;
	meta->row_typmod = -1;
	meta->row_natts = descriptor->natts;
	meta->row_fingerprint =
		pglc_row_payload_tupledesc_fingerprint(descriptor);
	if (!OidIsValid(meta->row_type_oid) || meta->row_natts <= 0)
		return false;
	return true;
}

static bool
pglc_sql_relation_meta(Relation relation, PgLocalCacheSqlMeta *meta,
					   bool check_catalog_provenance)
{
	return pglc_sql_relation_base_meta(relation, meta,
									   check_catalog_provenance) &&
		!pglc_sql_relation_has_children(relation) &&
		!pglc_sql_relation_has_parent(relation);
}

static bool
pglc_sql_same_mapping(const PgLocalCacheSqlMeta *left,
					  const PgLocalCacheSqlMeta *right)
{
	int			key_index;

	if (left->relation_oid != right->relation_oid ||
		left->config_generation != right->config_generation ||
		strcmp(left->nspace, right->nspace) != 0 ||
		left->key_count != right->key_count)
		return false;
	for (key_index = 0; key_index < left->key_count; key_index++)
	{
		if (left->key_attnos[key_index] != right->key_attnos[key_index] ||
			strcmp(left->key_columns[key_index],
				   right->key_columns[key_index]) != 0)
			return false;
	}
	return true;
}

static bool
pglc_sql_cached_relation_meta(PgLocalCacheSqlMeta *meta)
{
	PgLocalCacheSqlMetaCacheEntry *entry =
		pglc_sql_meta_cache_entry(meta->relation_oid);

	if (entry->relation_oid != meta->relation_oid ||
		entry->config_generation != meta->config_generation ||
		!entry->mapping_known || !entry->mapping_found ||
		!entry->relation_validated ||
		!pglc_sql_same_mapping(meta, &entry->meta))
		return false;
	*meta = entry->meta;
	return true;
}

static void
pglc_sql_remember_relation_meta(const PgLocalCacheSqlMeta *meta)
{
	PgLocalCacheSqlMetaCacheEntry *entry =
		pglc_sql_meta_cache_entry(meta->relation_oid);

	if (entry->relation_oid != meta->relation_oid ||
		entry->config_generation != meta->config_generation)
	{
		MemSet(entry, 0, sizeof(*entry));
		entry->relation_oid = meta->relation_oid;
		entry->config_generation = meta->config_generation;
		entry->mapping_known = true;
		entry->mapping_found = true;
	}
	entry->meta = *meta;
	entry->relation_validated = true;
	entry->relation_validation_token =
		pglc_sql_next_relation_validation_token();
}

static bool
pglc_sql_meta_matches_state(const PgLocalCacheSqlMeta *meta,
							const PgLocalCacheSqlScanState *state)
{
	int			key_index;

	if (meta->relation_oid != state->mapping.relation_oid ||
		strcmp(meta->nspace, state->mapping.nspace) != 0 ||
		meta->key_count != state->key_count ||
		meta->row_type_oid != state->row_type_oid ||
		meta->row_natts != state->row_natts ||
		meta->row_fingerprint != state->row_fingerprint)
		return false;
	for (key_index = 0; key_index < state->key_count; key_index++)
	{
		if (meta->key_attnos[key_index] != state->key_attnos[key_index] ||
			meta->key_types[key_index] != state->key_types[key_index] ||
			meta->key_typmods[key_index] != state->key_typmods[key_index] ||
			strcmp(meta->key_columns[key_index],
				   state->mapping.key_columns[key_index]) != 0)
			return false;
	}
	return true;
}

static bool
pglc_sql_limit_supported(Node *limit_count)
{
	Const	  *limit;

	if (limit_count == NULL)
		return true;
	if (!IsA(limit_count, Const))
		return false;
	limit = (Const *) limit_count;
	return !limit->constisnull && limit->consttype == INT8OID &&
		DatumGetInt64(limit->constvalue) == 1;
}

static bool
pglc_sql_simple_query(PlannerInfo *root, RelOptInfo *rel, Index rti,
					  RangeTblEntry *rte)
{
	Query	  *query = root->parse;
	Node	  *from_item;
	ListCell   *cell;

	if (query->commandType != CMD_SELECT || query->resultRelation != 0 ||
		query->hasAggs || query->hasWindowFuncs || query->hasTargetSRFs ||
		query->hasSubLinks || query->hasModifyingCTE || query->cteList != NIL ||
		query->setOperations != NULL || query->groupClause != NIL ||
		query->groupingSets != NIL || query->havingQual != NULL ||
		query->windowClause != NIL || query->distinctClause != NIL ||
		query->sortClause != NIL || query->limitOffset != NULL ||
		!pglc_sql_limit_supported(query->limitCount) || query->rowMarks != NIL ||
		list_length(query->rtable) != 1 || query->jointree == NULL ||
		list_length(query->jointree->fromlist) != 1 ||
		query->targetList == NIL)
		return false;

	from_item = (Node *) linitial(query->jointree->fromlist);
	if (!IsA(from_item, RangeTblRef) ||
		(Index) ((RangeTblRef *) from_item)->rtindex != rti)
		return false;
	foreach(cell, query->targetList)
	{
		TargetEntry *target = (TargetEntry *) lfirst(cell);
		Var		   *var;

		if (target->resjunk || !IsA(target->expr, Var))
			return false;
		var = (Var *) target->expr;
		if ((Index) var->varno != rti || var->varattno <= 0 ||
			var->varlevelsup != 0)
			return false;
	}

	if (rte->rtekind != RTE_RELATION || rel->relid != rti ||
		rte->tablesample != NULL || rte->securityQuals != NIL || rte->inh ||
		rel->reloptkind != RELOPT_BASEREL || rel->lateral_relids != NULL ||
		rel->direct_lateral_relids != NULL)
		return false;
	return true;
}

static bool
pglc_sql_key_datum_compatible(Oid key_type, Oid expression_type)
{
	if (key_type == expression_type)
		return true;
	return (key_type == TEXTOID && expression_type == VARCHAROID) ||
		(key_type == VARCHAROID && expression_type == TEXTOID);
}

/*
 * PostgreSQL intentionally keeps cross-type integer equality operators in
 * the integer btree opfamily.  Accept only lossless widening conversions for
 * the cache key Datum; narrowing a bigint expression could raise or change
 * the semantics of an otherwise valid comparison, so that shape falls back.
 */
static bool
pglc_sql_key_input_supported(Oid key_type, Oid expression_type)
{
	if (pglc_sql_key_datum_compatible(key_type, expression_type))
		return true;
	return (key_type == INT4OID && expression_type == INT2OID) ||
		(key_type == INT8OID &&
		 (expression_type == INT2OID || expression_type == INT4OID));
}

static Expr *
pglc_sql_coerce_key_expr(Expr *expression, Oid key_type, int32 key_typmod)
{
	Oid			expression_type = exprType((Node *) expression);
	Node	   *coerced;

	if (pglc_sql_key_datum_compatible(key_type, expression_type))
		return expression;
	coerced = coerce_to_target_type(NULL, (Node *) expression,
								 expression_type, key_type,
								 key_typmod, COERCION_IMPLICIT,
								 COERCE_IMPLICIT_CAST, -1);
	return (Expr *) coerced;
}

static Node *
pglc_sql_strip_relabels(Node *node)
{
	while (node != NULL && IsA(node, RelabelType))
		node = (Node *) ((RelabelType *) node)->arg;
	return node;
}

static int
pglc_sql_key_var(Node *operand, Index rti, const PgLocalCacheSqlMeta *meta)
{
	Node	   *base;
	Var		   *var;
	int			key_index;

	base = pglc_sql_strip_relabels(operand);
	if (!IsA(base, Var))
		return -1;
	var = (Var *) base;
	if ((Index) var->varno != rti || var->varlevelsup != 0)
		return -1;
	for (key_index = 0; key_index < meta->key_count; key_index++)
	{
		if (var->varattno == meta->key_attnos[key_index] &&
			var->vartype == meta->key_types[key_index] &&
			pglc_sql_key_datum_compatible(meta->key_types[key_index],
										 exprType(operand)))
			return key_index;
	}
	return -1;
}

static bool
pglc_sql_match_clauses(RelOptInfo *rel, Index rti,
					   const PgLocalCacheSqlMeta *meta,
					   RestrictInfo **restrict_infos, List **key_exprs)
{
	ListCell   *cell;
	Expr	   *ordered_exprs[PGLC_MAX_KEY_COLUMNS];
	int			key_index;

	if (list_length(rel->baserestrictinfo) != meta->key_count)
		return false;
	MemSet(restrict_infos, 0,
			sizeof(RestrictInfo *) * PGLC_MAX_KEY_COLUMNS);
	MemSet(ordered_exprs, 0, sizeof(ordered_exprs));

	foreach(cell, rel->baserestrictinfo)
	{
		RestrictInfo *rinfo = (RestrictInfo *) lfirst(cell);
		OpExpr	  *operator;
		Node	  *left;
		Node	  *right;
		Expr	  *other;
		Expr	  *coerced_other;
		Node	  *other_base;

		if (!IsA(rinfo, RestrictInfo) || rinfo->pseudoconstant ||
			!IsA(rinfo->clause, OpExpr))
			return false;
		operator = (OpExpr *) rinfo->clause;
		if (operator->opresulttype != BOOLOID ||
			list_length(operator->args) != 2)
			return false;

		left = (Node *) linitial(operator->args);
		right = (Node *) lsecond(operator->args);
		key_index = pglc_sql_key_var(left, rti, meta);
		if (key_index >= 0)
			other = (Expr *) right;
		else
		{
			key_index = pglc_sql_key_var(right, rti, meta);
			if (key_index < 0)
				return false;
			other = (Expr *) left;
		}
		if (restrict_infos[key_index] != NULL)
			return false;

		other_base = pglc_sql_strip_relabels((Node *) other);
		if ((!IsA(other_base, Const) && !IsA(other_base, Param)) ||
			(IsA(other_base, Param) &&
			 ((Param *) other_base)->paramkind != PARAM_EXTERN) ||
			!pglc_sql_key_input_supported(meta->key_types[key_index],
									 exprType((Node *) other)))
			return false;

		/* The exact btree IndexPath validates equality strategy/opfamily. */
		coerced_other = pglc_sql_coerce_key_expr(
			other, meta->key_types[key_index], meta->key_typmods[key_index]);
		if (coerced_other == NULL)
			return false;
		restrict_infos[key_index] = rinfo;
		ordered_exprs[key_index] = coerced_other;
	}

	*key_exprs = NIL;
	for (key_index = 0; key_index < meta->key_count; key_index++)
	{
		if (restrict_infos[key_index] == NULL ||
			ordered_exprs[key_index] == NULL)
			return false;
		*key_exprs = lappend(*key_exprs, ordered_exprs[key_index]);
	}
	return true;
}

static bool
pglc_sql_index_is_primary(Oid index_oid)
{
	HeapTuple	index_tuple;
	bool		is_primary = false;

	index_tuple = SearchSysCache1(INDEXRELID, ObjectIdGetDatum(index_oid));
	if (HeapTupleIsValid(index_tuple))
	{
		Form_pg_index index_form = (Form_pg_index) GETSTRUCT(index_tuple);

		is_primary = index_form->indisprimary;
		ReleaseSysCache(index_tuple);
	}
	return is_primary;
}

static IndexPath *
pglc_sql_primary_index_path(PlannerInfo *root, RelOptInfo *rel,
							   const PgLocalCacheSqlMeta *meta,
							   RestrictInfo **restrict_infos)
{
	ListCell   *cell;
	IndexPath  *best = NULL;

	if (!enable_indexscan)
		return NULL;

	/*
	 * Build a private ordinary IndexPath from rel->indexlist.  A tiny table's
	 * IndexPath can already have been pruned as dominated by a SeqScan before
	 * this hook runs; keeping the private child makes the SQL cache usable for
	 * that common development and test shape too.
	 */
	foreach(cell, rel->indexlist)
	{
		IndexOptInfo *index_info = (IndexOptInfo *) lfirst(cell);
		List	   *index_clauses = NIL;
		IndexPath  *index_path;
		bool		matches = true;
		int			key_index;

		if (index_info == NULL || index_info->relam != BTREE_AM_OID ||
			!pglc_sql_index_is_primary(index_info->indexoid) ||
			!index_info->unique || !index_info->immediate ||
			index_info->hypothetical || !index_info->amhasgettuple ||
			index_info->nkeycolumns != meta->key_count ||
			index_info->indpred != NIL)
			continue;

		for (key_index = 0; key_index < meta->key_count; key_index++)
		{
			RestrictInfo *rinfo = restrict_infos[key_index];
			RestrictInfo *indexqual_rinfo;
			TypeCacheEntry *type_cache;
			OpExpr	  *operator = (OpExpr *) rinfo->clause;
			Node	  *left = (Node *) linitial(operator->args);
			Node	  *right = (Node *) lsecond(operator->args);
			Oid			index_operator;
			IndexClause *index_clause;

			type_cache = lookup_type_cache(meta->key_types[key_index],
									   TYPECACHE_BTREE_OPFAMILY);
			if (!OidIsValid(type_cache->btree_opf) ||
				index_info->indexkeys[key_index] !=
				meta->key_attnos[key_index] ||
				index_info->opfamily[key_index] != type_cache->btree_opf ||
				(index_info->indexcollations[key_index] != InvalidOid &&
				 index_info->indexcollations[key_index] !=
				 operator->inputcollid))
			{
				matches = false;
				break;
			}

			if (match_index_to_operand(left, key_index, index_info))
			{
				index_operator = operator->opno;
				indexqual_rinfo = rinfo;
			}
			else if (match_index_to_operand(right, key_index, index_info))
			{
				index_operator = get_commutator(operator->opno);
				if (!OidIsValid(index_operator))
				{
					matches = false;
					break;
				}
				indexqual_rinfo = commute_restrictinfo(rinfo,
												 index_operator);
			}
			else
			{
				matches = false;
				break;
			}
			if (get_op_opfamily_strategy(index_operator,
								 type_cache->btree_opf) !=
				BTEqualStrategyNumber)
			{
				matches = false;
				break;
			}

			index_clause = makeNode(IndexClause);
			index_clause->rinfo = rinfo;
			index_clause->indexquals = list_make1(indexqual_rinfo);
			index_clause->lossy = false;
			index_clause->indexcol = key_index;
			index_clause->indexcols = NIL;
			index_clauses = lappend(index_clauses, index_clause);
		}
		if (!matches || list_length(index_clauses) != meta->key_count)
			continue;
		index_path = create_index_path(root, index_info,
								   index_clauses, NIL, NIL, NIL,
								   ForwardScanDirection, false, NULL,
								   1.0, false);
		if (best == NULL || index_path->path.total_cost < best->path.total_cost)
			best = index_path;
	}
	return best;
}

static bool
pglc_sql_targets_supported(Query *query, Index rti, Relation relation)
{
	TupleDesc	descriptor = RelationGetDescr(relation);
	ListCell   *cell;

	foreach(cell, query->targetList)
	{
		TargetEntry *target = (TargetEntry *) lfirst(cell);
		Var		   *var = (Var *) target->expr;
		Form_pg_attribute attribute;

		/* pglc_sql_simple_query already established a direct, local Var. */
		if ((Index) var->varno != rti || var->varattno <= 0 ||
			var->varattno > descriptor->natts || var->varlevelsup != 0)
			return false;
		attribute = TupleDescAttr(descriptor, var->varattno - 1);
		if (attribute->attisdropped || var->vartype != attribute->atttypid ||
			var->vartypmod != attribute->atttypmod ||
			var->varcollid != attribute->attcollation)
			return false;
	}
	return true;
}

static void
pglc_sql_set_rel_pathlist(PlannerInfo *root, RelOptInfo *rel, Index rti,
						  RangeTblEntry *rte)
{
	PgLocalCacheSqlMeta meta;
	Relation	relation;
	RestrictInfo *restrict_infos[PGLC_MAX_KEY_COLUMNS];
	List	   *key_exprs;
	IndexPath  *index_path;
	CustomPath *custom_path;
	List	   *private = NIL;
	List	   *key_attnos = NIL;
	List	   *key_types = NIL;
	List	   *key_typmods = NIL;
	uint64		relation_validation_token;
	int			key_index;

	if (previous_set_rel_pathlist_hook != NULL)
		previous_set_rel_pathlist_hook(root, rel, rti, rte);

	if (!pglc_sql_cache || pglc_shared == NULL ||
		XactIsoLevel != XACT_READ_COMMITTED || RecoveryInProgress() ||
		pglc_current_transaction_is_dirty() ||
		!pglc_sql_simple_query(root, rel, rti, rte) ||
		!pglc_sql_read_mapping(rte->relid, &meta))
		return;

	relation = table_open(rte->relid, NoLock);
	if (!pglc_sql_cached_relation_meta(&meta))
	{
		if (!pglc_sql_relation_meta(relation, &meta, true))
		{
			table_close(relation, NoLock);
			return;
		}
		pglc_sql_remember_relation_meta(&meta);
	}
	if (!pglc_sql_targets_supported(root->parse, rti, relation) ||
		!pglc_sql_match_clauses(rel, rti, &meta, restrict_infos,
							   &key_exprs))
	{
		table_close(relation, NoLock);
		return;
	}
	table_close(relation, NoLock);
	relation_validation_token = pglc_sql_relation_validation_token(
		meta.relation_oid, meta.config_generation);
	if (relation_validation_token == 0)
		return;

	index_path = pglc_sql_primary_index_path(root, rel, &meta, restrict_infos);
	if (index_path == NULL)
		return;
	for (key_index = 0; key_index < meta.key_count; key_index++)
	{
		key_attnos = lappend(key_attnos,
							 makeInteger(meta.key_attnos[key_index]));
		key_types = lappend(key_types,
						   pglc_sql_oid_const(meta.key_types[key_index]));
		key_typmods = lappend(key_typmods,
							  makeInteger(meta.key_typmods[key_index]));
	}

	private = lappend(private, makeString(pstrdup(meta.nspace)));
	private = lappend(private, pglc_sql_oid_const(meta.relation_oid));
	private = lappend(private, pglc_sql_int8_const(meta.config_generation));
	private = lappend(private, makeInteger(meta.key_count));
	private = lappend(private, key_attnos);
	private = lappend(private, key_types);
	private = lappend(private, key_typmods);
	private = lappend(private, pglc_sql_oid_const(meta.row_type_oid));
	private = lappend(private, makeInteger(meta.row_natts));
	private = lappend(private, pglc_sql_int8_const(meta.row_fingerprint));
	private = lappend(private,
		pglc_sql_int8_const(relation_validation_token));
	private = lappend(private, key_exprs);

	custom_path = makeNode(CustomPath);
	custom_path->path.pathtype = T_CustomScan;
	custom_path->path.parent = rel;
	custom_path->path.pathtarget = rel->reltarget;
	custom_path->path.param_info = NULL;
	custom_path->path.parallel_aware = false;
	custom_path->path.parallel_safe = false;
	custom_path->path.parallel_workers = 0;
	custom_path->path.rows = index_path->path.rows;
	custom_path->path.startup_cost = 0;
	custom_path->path.total_cost = Min(index_path->path.total_cost,
									 cpu_operator_cost + cpu_tuple_cost);
	custom_path->path.pathkeys = NIL;
	custom_path->flags = 0;
	custom_path->custom_paths = list_make1(index_path);
	custom_path->custom_private = private;
	custom_path->methods = &pglc_sql_path_methods;
	add_path(rel, &custom_path->path);
}

static Plan *
pglc_sql_plan_custom_path(PlannerInfo *root, RelOptInfo *rel,
						  CustomPath *best_path, List *tlist,
						  List *clauses, List *custom_plans)
{
	CustomScan *scan;
	Plan	   *child;
	List	   *private;
	List	   *key_exprs;
	Oid			row_type_oid;
	int			child_payload_resno;
	int			child_ctid_resno;
	int			child_xmin_resno;

	Assert(list_length(custom_plans) == 1);
	Assert(list_length(best_path->custom_private) ==
		   PGLC_PRIVATE_PLAN_ITEMS + 1);
	child = (Plan *) linitial(custom_plans);
	private = list_copy_head(best_path->custom_private,
							 PGLC_PRIVATE_PLAN_ITEMS);
	key_exprs = (List *) list_nth(best_path->custom_private,
								 PGLC_PRIVATE_KEY_EXPRS);
	row_type_oid = pglc_sql_private_oid(private, PGLC_PRIVATE_ROW_TYPE);

	child_payload_resno = list_length(child->targetlist) + 1;
	child->targetlist = lappend(child->targetlist,
		makeTargetEntry((Expr *) makeVar(rel->relid, InvalidAttrNumber,
									 row_type_oid, -1, InvalidOid, 0),
						child_payload_resno, NULL, true));
	child_ctid_resno = list_length(child->targetlist) + 1;
	child->targetlist = lappend(child->targetlist,
		makeTargetEntry((Expr *) makeVar(rel->relid,
									 SelfItemPointerAttributeNumber,
									 TIDOID, -1, InvalidOid, 0),
					child_ctid_resno, NULL, true));
	child_xmin_resno = list_length(child->targetlist) + 1;
	child->targetlist = lappend(child->targetlist,
		makeTargetEntry((Expr *) makeVar(rel->relid,
									 MinTransactionIdAttributeNumber,
									 XIDOID, -1, InvalidOid, 0),
					child_xmin_resno, NULL, true));

	private = lappend(private, makeInteger(child_payload_resno));
	private = lappend(private, makeInteger(child_ctid_resno));
	private = lappend(private, makeInteger(child_xmin_resno));

	scan = makeNode(CustomScan);
	scan->scan.plan.targetlist = tlist;
	scan->scan.plan.qual = NIL;
	scan->scan.scanrelid = rel->relid;
	scan->flags = 0;
	scan->custom_plans = custom_plans;
	scan->custom_exprs = (List *) copyObject(key_exprs);
	scan->custom_private = private;
	scan->custom_scan_tlist = NIL;
	scan->methods = &pglc_sql_scan_methods;

	(void) root;
	(void) clauses;
	return &scan->scan.plan;
}

static Node *
pglc_sql_create_scan_state(CustomScan *cscan)
{
	PgLocalCacheSqlScanState *state;
	Size		state_size = MAXALIGN(sizeof(PgLocalCacheSqlScanState));

	state = (PgLocalCacheSqlScanState *)
		palloc(add_size(state_size, PGLC_VALUE_MAX));
	MemSet(state, 0, sizeof(*state));
	state->cache_buffer = ((char *) state) + state_size;
	NodeSetTag(state, T_CustomScanState);
	state->css.methods = &pglc_sql_exec_methods;
	(void) cscan;
	return (Node *) state;
}

static bool
pglc_sql_validate_runtime(PgLocalCacheSqlScanState *state,
						  CustomScan *scan)
{
	Relation	relation = state->css.ss.ss_currentRelation;
	PgLocalCacheSqlMetaCacheEntry *entry;
	PgLocalCacheSqlMeta planned_meta;
	PgLocalCacheSqlMeta current_meta;
	TupleDesc	descriptor;
	uint64		current_generation;
	uint64		validation_token;
	int			key_index;

	if (relation == NULL ||
		RelationGetRelid(relation) != state->mapping.relation_oid ||
		relation->rd_rel->relkind != RELKIND_RELATION ||
		relation->rd_rel->relpersistence != RELPERSISTENCE_PERMANENT ||
		relation->rd_rel->relam != HEAP_TABLE_AM_OID ||
		relation->rd_rel->relispartition ||
		relation->rd_rel->relrowsecurity || relation->rd_rel->relforcerowsecurity)
		return false;
	if (state->key_count < 1 || state->key_count > PGLC_MAX_KEY_COLUMNS)
		return false;

	current_generation = pglc_config_generation();
	entry = pglc_sql_meta_cache_entry(state->mapping.relation_oid);
	/* Exact OID/generation/version checks make slot collisions fail closed. */
	if (state->mapping.config_generation == current_generation &&
		state->relation_validation_token != 0 &&
		entry->relation_oid == state->mapping.relation_oid &&
		entry->config_generation == current_generation &&
		entry->mapping_known && entry->mapping_found &&
		entry->relation_validated &&
		entry->relation_validation_token ==
		state->relation_validation_token)
		return true;

	/*
	 * Token mismatch is uncommon (DDL, cache-slot collision, or a mapping
	 * reload).  Reconstruct the exact planned metadata only on this slow path.
	 */
	descriptor = RelationGetDescr(relation);
	MemSet(&planned_meta, 0, sizeof(planned_meta));
	strlcpy(planned_meta.nspace, state->mapping.nspace,
			sizeof(planned_meta.nspace));
	planned_meta.relation_oid = state->mapping.relation_oid;
	planned_meta.config_generation = state->mapping.config_generation;
	planned_meta.key_count = state->key_count;
	for (key_index = 0; key_index < state->key_count; key_index++)
	{
		Form_pg_attribute key_attribute;

		if (state->key_attnos[key_index] <= 0 ||
			state->key_attnos[key_index] > descriptor->natts)
			return false;
		key_attribute = TupleDescAttr(descriptor,
									 state->key_attnos[key_index] - 1);
		strlcpy(planned_meta.key_columns[key_index],
				NameStr(key_attribute->attname), NAMEDATALEN);
		planned_meta.key_attnos[key_index] = state->key_attnos[key_index];
	}
	if (state->mapping.config_generation == current_generation)
	{
		current_meta = planned_meta;
		if (pglc_sql_cached_relation_meta(&current_meta) &&
			pglc_sql_meta_matches_state(&current_meta, state))
		{
			validation_token = pglc_sql_relation_validation_token(
				state->mapping.relation_oid, current_generation);
			if (validation_token == 0)
				return false;
			state->relation_validation_token = validation_token;
			return true;
		}
	}
	/*
	 * A cache miss here means either the relation was invalidated or another
	 * mapping changed.  Repeat every shape, source, trigger-ownership, and
	 * dependency check before accepting a new local validation entry.  Relcache
	 * invalidation alone is sufficient to force this fail-closed slow path.
	 */
	if (!pglc_sql_relation_meta(relation, &planned_meta, true) ||
		planned_meta.row_type_oid != state->row_type_oid ||
		planned_meta.row_natts != state->row_natts ||
		planned_meta.row_fingerprint != state->row_fingerprint)
		return false;
	for (key_index = 0; key_index < state->key_count; key_index++)
	{
		if (planned_meta.key_types[key_index] != state->key_types[key_index] ||
			planned_meta.key_typmods[key_index] != state->key_typmods[key_index])
			return false;
	}

	if (state->mapping.config_generation == current_generation)
	{
		pglc_sql_remember_relation_meta(&planned_meta);
		validation_token = pglc_sql_relation_validation_token(
			state->mapping.relation_oid, current_generation);
		if (validation_token == 0)
			return false;
		state->relation_validation_token = validation_token;
		(void) scan;
		return true;
	}

	/*
	 * A reload caused by another mapping must not condemn a long-lived generic
	 * plan to permanent bypass.  Re-read this relation's current mapping and
	 * accept a new generation only when every plan-relevant field and all three
	 * exact invalidation triggers are unchanged.
	 */
	MemSet(&current_meta, 0, sizeof(current_meta));
	if (!pglc_sql_read_mapping(RelationGetRelid(relation), &current_meta) ||
		!pglc_sql_relation_meta(relation, &current_meta, true))
		return false;
	if (current_meta.relation_oid != state->mapping.relation_oid ||
		strcmp(current_meta.nspace, state->mapping.nspace) != 0 ||
		current_meta.key_count != state->key_count ||
		current_meta.row_type_oid != state->row_type_oid ||
		current_meta.row_natts != state->row_natts ||
		current_meta.row_fingerprint != state->row_fingerprint)
		return false;
	for (key_index = 0; key_index < state->key_count; key_index++)
	{
		if (current_meta.key_attnos[key_index] != state->key_attnos[key_index] ||
			current_meta.key_types[key_index] != state->key_types[key_index] ||
			current_meta.key_typmods[key_index] !=
			state->key_typmods[key_index] ||
			strcmp(current_meta.key_columns[key_index],
				   planned_meta.key_columns[key_index]) != 0)
			return false;
	}

	pglc_sql_remember_relation_meta(&current_meta);
	state->mapping.config_generation = current_meta.config_generation;
	validation_token = pglc_sql_relation_validation_token(
		state->mapping.relation_oid, current_meta.config_generation);
	if (validation_token == 0)
		return false;
	state->relation_validation_token = validation_token;
	(void) scan;
	return true;
}

static void
pglc_sql_begin(CustomScanState *node, EState *estate, int eflags)
{
	PgLocalCacheSqlScanState *state = (PgLocalCacheSqlScanState *) node;
	CustomScan *scan = (CustomScan *) node->ss.ps.plan;
	bool		is_varlena;
	ListCell   *cell;
	int			key_index;

	Assert(list_length(scan->custom_plans) == 1);
	Assert(list_length(scan->custom_private) ==
		   PGLC_PRIVATE_PLAN_ITEMS + 3);

	state->key_count = intVal(list_nth(scan->custom_private,
									   PGLC_PRIVATE_KEY_COUNT));
	Assert(state->key_count >= 1 &&
		   state->key_count <= PGLC_MAX_KEY_COLUMNS);
	Assert(list_length(scan->custom_exprs) == state->key_count);
	state->row_type_oid = pglc_sql_private_oid(scan->custom_private,
										 PGLC_PRIVATE_ROW_TYPE);
	state->row_typmod = -1;
	state->row_natts = intVal(list_nth(scan->custom_private,
									PGLC_PRIVATE_ROW_NATTS));
	state->row_fingerprint = pglc_sql_private_uint64(
		scan->custom_private, PGLC_PRIVATE_ROW_FINGERPRINT);
	state->relation_validation_token = pglc_sql_private_uint64(
		scan->custom_private, PGLC_PRIVATE_RELATION_VALIDATION);
	state->child_payload_resno = intVal(list_nth(scan->custom_private,
										 PGLC_PRIVATE_PLAN_ITEMS));
	state->child_ctid_resno = intVal(list_nth(scan->custom_private,
										PGLC_PRIVATE_PLAN_ITEMS + 1));
	state->child_xmin_resno = intVal(list_nth(scan->custom_private,
										PGLC_PRIVATE_PLAN_ITEMS + 2));

	MemSet(&state->mapping, 0, sizeof(state->mapping));
	strlcpy(state->mapping.nspace,
			strVal(list_nth(scan->custom_private, PGLC_PRIVATE_NAMESPACE)),
			sizeof(state->mapping.nspace));
	state->mapping.relation_oid = pglc_sql_private_oid(scan->custom_private,
												 PGLC_PRIVATE_RELATION);
	state->mapping.config_generation =
		pglc_sql_private_generation(scan->custom_private);
	state->mapping.key_count = state->key_count;
	state->mapping.row_type_oid = state->row_type_oid;
	state->mapping.row_typmod = state->row_typmod;
	state->mapping.row_natts = state->row_natts;
	state->mapping.row_descriptor_fingerprint = state->row_fingerprint;
	state->mapping.row_desc =
		RelationGetDescr(state->css.ss.ss_currentRelation);

	for (key_index = 0; key_index < state->key_count; key_index++)
	{
		Oid			key_output_oid;
		Form_pg_attribute key_attribute;

		state->key_attnos[key_index] = (AttrNumber)
			pglc_sql_private_list_int(scan->custom_private,
									   PGLC_PRIVATE_KEY_ATTNOS, key_index);
		state->key_types[key_index] =
			pglc_sql_private_list_oid(scan->custom_private,
									  PGLC_PRIVATE_KEY_TYPES, key_index);
		state->key_typmods[key_index] =
			pglc_sql_private_list_int(scan->custom_private,
									   PGLC_PRIVATE_KEY_TYPMODS, key_index);
		if (state->key_types[key_index] != INT2OID &&
			state->key_types[key_index] != INT4OID &&
			state->key_types[key_index] != INT8OID)
		{
			getTypeOutputInfo(state->key_types[key_index], &key_output_oid,
							  &is_varlena);
			fmgr_info(key_output_oid, &state->key_outputs[key_index]);
		}

		state->mapping.key_attnos[key_index] = state->key_attnos[key_index];
		state->mapping.key_types[key_index] = state->key_types[key_index];
		state->mapping.key_typmods[key_index] = state->key_typmods[key_index];
		state->mapping.key_outputs[key_index] = state->key_outputs[key_index];
		if (state->key_attnos[key_index] > 0 &&
			state->key_attnos[key_index] <= state->mapping.row_desc->natts)
		{
			key_attribute = TupleDescAttr(state->mapping.row_desc,
										 state->key_attnos[key_index] - 1);
			strlcpy(state->mapping.key_columns[key_index],
					NameStr(key_attribute->attname), NAMEDATALEN);
		}
	}
	state->child_plan = (Plan *) linitial(scan->custom_plans);
	state->child_eflags = eflags;
	state->child = NULL;
	state->css.custom_ps = NIL;
	state->key_exprs = NIL;
	foreach(cell, scan->custom_exprs)
		state->key_exprs = lappend(state->key_exprs,
			ExecInitExpr((Expr *) lfirst(cell), &state->css.ss.ps));
	state->latest_slot = NULL;
	state->runtime_valid = pglc_sql_validate_runtime(state, scan);
	state->done = false;
	(void) estate;
}

static bool
pglc_sql_can_use_cache(PgLocalCacheSqlScanState *state)
{
	Snapshot	snapshot = state->css.ss.ps.state->es_snapshot;

	return state->runtime_valid && pglc_sql_cache &&
		XactIsoLevel == XACT_READ_COMMITTED && !RecoveryInProgress() &&
		!IsParallelWorker() && !IsInParallelMode() &&
		!pglc_current_transaction_is_dirty() && snapshot != NULL &&
		snapshot->snapshot_type == SNAPSHOT_MVCC &&
		state->mapping.config_generation == pglc_config_generation();
}

typedef enum PgLocalCacheSourceVisibility
{
	PGLC_SOURCE_VISIBLE = 0,
	PGLC_SOURCE_SNAPSHOT_REJECTED,
	PGLC_SOURCE_AGE_EXPIRED
} PgLocalCacheSourceVisibility;

/*
 * source_xmin is the raw heap xmin.  Do not consult pg_xact here: the status
 * of an old, frozen tuple may already have been truncated.  A cache entry is
 * admitted only after a latest-snapshot visibility proof.  The FullXID
 * observation horizon bounds the lifetime of the raw 32-bit value to less
 * than half its ID space; after that we conservatively use the child scan.
 * For a very old/frozen raw xmin, snapshot membership can only cause a false
 * miss, never expose a version that was invisible when it was admitted.
 */
static PgLocalCacheSourceVisibility
pglc_sql_source_visibility(TransactionId source_xmin,
							   uint64 source_observed_full_xid,
							   Snapshot snapshot)
{
	uint64		current_full_xid;

	current_full_xid =
		U64FromFullTransactionId(ReadNextFullTransactionId());
	if (source_observed_full_xid == 0 ||
		current_full_xid < source_observed_full_xid ||
		current_full_xid - source_observed_full_xid >=
		UINT64CONST(0x80000000))
		return PGLC_SOURCE_AGE_EXPIRED;
	if (TransactionIdEquals(source_xmin, FrozenTransactionId) ||
		TransactionIdEquals(source_xmin, BootstrapTransactionId))
		return PGLC_SOURCE_VISIBLE;
	if (!TransactionIdIsNormal(source_xmin) ||
		TransactionIdIsCurrentTransactionId(source_xmin) ||
		XidInMVCCSnapshot(source_xmin, snapshot))
		return PGLC_SOURCE_SNAPSHOT_REJECTED;
	return PGLC_SOURCE_VISIBLE;
}

static TupleTableSlot *
pglc_sql_form_row_tuple(PgLocalCacheSqlScanState *state, Datum row)
{
	TupleTableSlot *slot = state->css.ss.ss_ScanTupleSlot;

	ExecClearTuple(slot);
	ExecStoreHeapTupleDatum(row, slot);
	slot->tts_tableOid = state->mapping.relation_oid;
	return slot;
}

static PlanState *
pglc_sql_init_child(PgLocalCacheSqlScanState *state)
{
	if (state->child == NULL)
	{
		MemoryContext old_context;

		Assert(state->child_plan != NULL);
		old_context = MemoryContextSwitchTo(
			state->css.ss.ps.state->es_query_cxt);
		state->child = ExecInitNode(state->child_plan,
			state->css.ss.ps.state, state->child_eflags);
		state->css.custom_ps = list_make1(state->child);
		MemoryContextSwitchTo(old_context);
	}
	return state->child;
}

static TupleTableSlot *
pglc_sql_init_latest_slot(PgLocalCacheSqlScanState *state)
{
	if (state->latest_slot == NULL)
	{
		MemoryContext old_context;

		old_context = MemoryContextSwitchTo(
			state->css.ss.ps.state->es_query_cxt);
		state->latest_slot = table_slot_create(
			state->css.ss.ss_currentRelation,
			&state->css.ss.ps.state->es_tupleTable);
		MemoryContextSwitchTo(old_context);
	}
	return state->latest_slot;
}

static bool
pglc_sql_maybe_store(PgLocalCacheSqlScanState *state,
					 const char *canonical_key,
					 const PgLocalCacheReadToken *token,
					 uint64 load_id, TupleTableSlot *child_slot)
{
	Datum		ctid_datum;
	Datum		xmin_datum;
	ItemPointerData ctid;
	TransactionId source_xmin;
	volatile Snapshot latest_snapshot = NULL;
	volatile bool validated = false;
	TupleTableSlot *latest_slot;
	bool		isnull;
	char		row_payload[PGLC_VALUE_MAX];
	Size		serialized_len;

	if (load_id == 0 || !pglc_sql_can_use_cache(state))
		return false;
	ctid_datum = slot_getattr(child_slot, state->child_ctid_resno, &isnull);
	if (isnull)
		return false;
	ctid = *DatumGetItemPointer(ctid_datum);
	xmin_datum = slot_getattr(child_slot, state->child_xmin_resno, &isnull);
	if (isnull)
		return false;
	source_xmin = DatumGetTransactionId(xmin_datum);
	if (!TransactionIdIsValid(source_xmin) ||
		TransactionIdIsCurrentTransactionId(source_xmin))
		return false;
	latest_slot = pglc_sql_init_latest_slot(state);

	PG_TRY();
	{
		Datum		latest_xmin_datum;
		TransactionId latest_xmin;

		latest_snapshot = RegisterSnapshot(GetLatestSnapshot());
		ExecClearTuple(latest_slot);
		if (table_tuple_fetch_row_version(state->css.ss.ss_currentRelation,
										  &ctid, (Snapshot) latest_snapshot,
										  latest_slot))
		{
			latest_xmin_datum = slot_getsysattr(latest_slot,
											 MinTransactionIdAttributeNumber,
											 &isnull);
			if (!isnull)
			{
				latest_xmin = DatumGetTransactionId(latest_xmin_datum);
				/* Fetching with latest_snapshot is the visibility proof. */
				validated = TransactionIdEquals(latest_xmin, source_xmin);
			}
		}
		UnregisterSnapshot((Snapshot) latest_snapshot);
		latest_snapshot = NULL;
	}
	PG_CATCH();
	{
		if (latest_snapshot != NULL)
			UnregisterSnapshot((Snapshot) latest_snapshot);
		PG_RE_THROW();
	}
	PG_END_TRY();
	if (!validated)
		return false;

	if (!pglc_row_payload_encode(
			latest_slot,
			RelationGetDescr(state->css.ss.ss_currentRelation), 0,
			row_payload, sizeof(row_payload), &serialized_len))
		return false;
	return pglc_cache_store(&state->mapping, canonical_key, token,
						row_payload, serialized_len, false, load_id,
						source_xmin);
}

static TupleTableSlot *
pglc_sql_run_child(PgLocalCacheSqlScanState *state, const char *canonical_key,
				   const PgLocalCacheReadToken *token, uint64 load_id)
{
	volatile TupleTableSlot *result = NULL;

	PG_TRY();
	{
		PlanState  *child;
		TupleTableSlot *child_slot;
		Datum		payload;
		bool		payload_isnull;

		child = pglc_sql_init_child(state);
		child_slot = ExecProcNode(child);
		if (!TupIsNull(child_slot))
		{
			payload = slot_getattr(child_slot, state->child_payload_resno,
								   &payload_isnull);
			if (load_id != 0 && !payload_isnull &&
				pglc_sql_maybe_store(state, canonical_key, token, load_id,
								 child_slot))
				pglc_note_sql_cache_fill();
			if (!payload_isnull)
				result = pglc_sql_form_row_tuple(state, payload);
		}
		if (load_id != 0)
			pglc_cache_release_load(&state->mapping, canonical_key, token,
								load_id);
	}
	PG_CATCH();
	{
		if (load_id != 0)
			pglc_cache_release_load(&state->mapping, canonical_key, token,
								load_id);
		PG_RE_THROW();
	}
	PG_END_TRY();
	return (TupleTableSlot *) result;
}

static TupleTableSlot *
pglc_sql_access(ScanState *scan_state)
{
	PgLocalCacheSqlScanState *state = (PgLocalCacheSqlScanState *) scan_state;
	ExprContext *econtext = state->css.ss.ps.ps_ExprContext;
	Datum		keys[PGLC_MAX_KEY_COLUMNS];
	bool		key_nulls[PGLC_MAX_KEY_COLUMNS];
	char		canonical_key[PGLC_KEY_MAX];
	Size		canonical_key_len;
	Size		cached_len;
	bool		negative;
	TransactionId source_xmin;
	PgLocalCacheReadToken token;
	PgLocalCacheSourceVisibility visibility = PGLC_SOURCE_SNAPSHOT_REJECTED;
	bool		hit;
	int			lookup_attempt;
	uint64		load_id = 0;
	MemoryContext old_context;
	ListCell   *cell;
	int			key_index = 0;

	if (state->done)
		return NULL;
	state->done = true;
	foreach(cell, state->key_exprs)
	{
		keys[key_index] = ExecEvalExprSwitchContext(
			(ExprState *) lfirst(cell), econtext, &key_nulls[key_index]);
		if (key_nulls[key_index])
			return NULL;
		key_index++;
	}
	Assert(key_index == state->key_count);

	if (!pglc_sql_can_use_cache(state))
	{
		state->bypasses++;
		pglc_note_sql_cache_bypass();
		return pglc_sql_run_child(state, NULL, NULL, 0);
	}

	old_context = MemoryContextSwitchTo(econtext->ecxt_per_tuple_memory);
	hit = pglc_canonical_key_typed(keys, key_nulls, state->key_count,
								   state->key_types, state->key_outputs,
								   canonical_key, sizeof(canonical_key),
								   &canonical_key_len);
	MemoryContextSwitchTo(old_context);
	if (!hit || canonical_key_len == 0)
	{
		state->bypasses++;
		pglc_note_sql_cache_bypass();
		return pglc_sql_run_child(state, NULL, NULL, 0);
	}

	for (lookup_attempt = 0; lookup_attempt < 2; lookup_attempt++)
	{
		hit = pglc_cache_lookup_quiet(&state->mapping, canonical_key,
									 state->cache_buffer, PGLC_VALUE_MAX,
									 &cached_len,
									 &negative, &source_xmin, &token);
		if (!hit || negative)
			break;

		visibility = pglc_sql_source_visibility(
			source_xmin, token.source_observed_full_xid,
			state->css.ss.ps.state->es_snapshot);
		if (visibility == PGLC_SOURCE_VISIBLE)
		{
			PgLocalCacheRowPayloadView view;

			if (pglc_row_payload_decode_in_place(
					state->cache_buffer, cached_len,
					RelationGetDescr(state->css.ss.ss_currentRelation),
					state->row_fingerprint, &view))
			{
				state->hits++;
				pglc_note_sql_cache_hit();
				return pglc_sql_form_row_tuple(state, view.composite);
			}
			/* Corruption or stale row shape is never exposed to SQL. */
		}
		if (lookup_attempt != 0 ||
			(visibility != PGLC_SOURCE_AGE_EXPIRED &&
			 visibility != PGLC_SOURCE_VISIBLE))
			break;

		/* Retire only the exact over-age or malformed positive just observed. */
		(void) pglc_cache_retire_positive(&state->mapping, canonical_key,
									  &token, source_xmin);
	}

	state->misses++;
	pglc_note_sql_cache_miss();
	/* Negative entries and entries too new for this snapshot always fall back. */
	if (!hit && pglc_cache_claim_load(&state->mapping, canonical_key,
										&token, &load_id) != PGLC_LOAD_OWNER)
		load_id = 0;
	return pglc_sql_run_child(state, canonical_key, &token, load_id);
}

static bool
pglc_sql_recheck(ScanState *scan_state, TupleTableSlot *slot)
{
	(void) scan_state;
	(void) slot;
	return true;
}

static TupleTableSlot *
pglc_sql_exec(CustomScanState *node)
{
	return ExecScan(&node->ss, pglc_sql_access, pglc_sql_recheck);
}

static void
pglc_sql_end(CustomScanState *node)
{
	PgLocalCacheSqlScanState *state = (PgLocalCacheSqlScanState *) node;

	if (state->child != NULL)
		ExecEndNode(state->child);
}

static void
pglc_sql_rescan(CustomScanState *node)
{
	PgLocalCacheSqlScanState *state = (PgLocalCacheSqlScanState *) node;

	state->done = false;
	ExecScanReScan(&state->css.ss);
	if (state->latest_slot != NULL)
		ExecClearTuple(state->latest_slot);
	if (state->child != NULL)
		ExecReScan(state->child);
}

static void
pglc_sql_explain(CustomScanState *node, List *ancestors, ExplainState *es)
{
	PgLocalCacheSqlScanState *state = (PgLocalCacheSqlScanState *) node;

	/* ExplainCustomScan runs before PostgreSQL walks custom_ps children. */
	if (state->child == NULL)
		(void) pglc_sql_init_child(state);
	ExplainPropertyText("Cache Namespace", state->mapping.nspace, es);
	ExplainPropertyText("Cache Policy", "positive MVCC-safe entries", es);
	ExplainPropertyText("On Miss", "unique index scan", es);
	if (es->analyze)
	{
		ExplainPropertyInteger("Cache Hits", NULL, (int64) state->hits, es);
		ExplainPropertyInteger("Cache Misses", NULL, (int64) state->misses, es);
		ExplainPropertyInteger("Cache Bypasses", NULL,
							   (int64) state->bypasses, es);
	}
	(void) ancestors;
}

void
pglc_sql_init(void)
{
	DefineCustomBoolVariable("pg_local_cache.sql_cache",
							 "Enable the transparent SQL primary-key cache fast path.",
							 NULL,
							 &pglc_sql_cache,
							 true,
							 PGC_USERSET,
							 0,
							 NULL,
							 NULL,
							 NULL);

	if (!process_shared_preload_libraries_in_progress)
		return;
	RegisterCustomScanMethods(&pglc_sql_scan_methods);
	CacheRegisterRelcacheCallback(pglc_sql_relcache_invalidation, (Datum) 0);
	previous_planner_hook = planner_hook;
	planner_hook = pglc_sql_planner;
	previous_set_rel_pathlist_hook = set_rel_pathlist_hook;
	set_rel_pathlist_hook = pglc_sql_set_rel_pathlist;
}
