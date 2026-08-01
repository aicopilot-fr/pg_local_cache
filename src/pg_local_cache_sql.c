#include "postgres.h"

#include "access/genam.h"
#include "access/stratnum.h"
#include "access/sysattr.h"
#include "access/table.h"
#include "access/tableam.h"
#include "access/transam.h"
#include "access/xact.h"
#include "access/xlog.h"
#include "catalog/namespace.h"
#include "catalog/pg_am_d.h"
#include "catalog/pg_class.h"
#include "catalog/pg_inherits.h"
#include "catalog/pg_trigger.h"
#include "catalog/pg_type_d.h"
#include "commands/explain.h"
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
#include "utils/builtins.h"
#include "utils/guc.h"
#include "utils/lsyscache.h"
#include "utils/memutils.h"
#include "utils/rel.h"
#include "utils/reltrigger.h"
#include "utils/snapmgr.h"
#include "utils/typcache.h"

#include "pg_local_cache.h"

/*
 * This is intentionally a narrow transparent fast path.  It recognizes only
 *
 *     SELECT mapped_value FROM mapped_table WHERE mapped_key = Const/$1
 *
 * and wraps the exact unique IndexPath that PostgreSQL would otherwise use.
 * The original IndexScan remains a child and is used for every miss or safety
 * fallback, so clients keep using ordinary SQL and ordinary PostgreSQL ACLs.
 */

#define PGLC_SQL_CUSTOM_NAME "pg_local_cache_sql"

#define PGLC_PRIVATE_NAMESPACE 0
#define PGLC_PRIVATE_RELATION 1
#define PGLC_PRIVATE_KEY_ATTNO 2
#define PGLC_PRIVATE_VALUE_ATTNO 3
#define PGLC_PRIVATE_KEY_TYPE 4
#define PGLC_PRIVATE_VALUE_TYPE 5
#define PGLC_PRIVATE_KEY_TYPMOD 6
#define PGLC_PRIVATE_VALUE_TYPMOD 7
#define PGLC_PRIVATE_GENERATION 8
#define PGLC_PRIVATE_KEY_EXPR 9
#define PGLC_PRIVATE_PLAN_ITEMS 9

typedef struct PgLocalCacheSqlMeta
{
	char		nspace[PGLC_NAMESPACE_MAX];
	char		key_column[NAMEDATALEN];
	Oid			relation_oid;
	AttrNumber key_attno;
	AttrNumber value_attno;
	Oid			key_type;
	Oid			value_type;
	int32		key_typmod;
	int32		value_typmod;
	uint64		config_generation;
} PgLocalCacheSqlMeta;

typedef struct PgLocalCacheSqlScanState
{
	CustomScanState css;
	PlanState  *child;
	ExprState  *key_expr;
	TupleTableSlot *latest_slot;
	PgLocalCacheMapping mapping;
	FmgrInfo	key_output;
	FmgrInfo	value_input;
	FmgrInfo	value_output;
	Oid			value_ioparam;
	AttrNumber key_attno;
	AttrNumber value_attno;
	int			child_value_resno;
	int			child_ctid_resno;
	int			child_xmin_resno;
	bool		runtime_valid;
	bool		done;
	uint64		hits;
	uint64		misses;
	uint64		bypasses;
} PgLocalCacheSqlScanState;

bool		pglc_sql_cache = true;

static set_rel_pathlist_hook_type previous_set_rel_pathlist_hook = NULL;
static planner_hook_type previous_planner_hook = NULL;

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
	AttrNumber key_attno;
	AttrNumber value_attno;
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
	key_attno = get_attnum(mapping_oid, "key_column");
	value_attno = get_attnum(mapping_oid, "value_column");
	if (namespace_attno == InvalidAttrNumber ||
		relation_attno == InvalidAttrNumber ||
		key_attno == InvalidAttrNumber || value_attno == InvalidAttrNumber)
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
			Datum		key_datum;
			Datum		value_datum;
			Name		key_name;
			Name		value_name;

			key_datum = slot_getattr(slot, key_attno, &isnull);
			if (isnull)
				break;
			key_name = DatumGetName(key_datum);
			value_datum = slot_getattr(slot, value_attno, &isnull);
			if (isnull || strlen(nspace) >= sizeof(meta->nspace))
				break;
			value_name = DatumGetName(value_datum);

			MemSet(meta, 0, sizeof(*meta));
			strlcpy(meta->nspace, nspace, sizeof(meta->nspace));
			strlcpy(meta->key_column, NameStr(*key_name),
					sizeof(meta->key_column));
			meta->relation_oid = relation_oid;
			meta->key_attno = get_attnum(relation_oid, NameStr(*key_name));
			meta->value_attno = get_attnum(relation_oid,
										 NameStr(*value_name));
			meta->config_generation = generation;
			found = meta->key_attno != InvalidAttrNumber &&
				meta->value_attno != InvalidAttrNumber &&
				meta->key_attno != meta->value_attno;
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
		found = pglc_sql_read_mapping_once(relation_oid, before, meta);
		after = pglc_config_generation();
		if (before == after)
			return found;
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
pglc_sql_triggers_valid(Relation relation, const PgLocalCacheSqlMeta *meta)
{
	TriggerDesc *trigger_desc = relation->trigdesc;
	Oid			namespace_oid;
	bool		guard_found = false;
	bool		row_found = false;
	bool		truncate_found = false;
	int			index;

	if (trigger_desc == NULL)
		return false;
	namespace_oid = get_namespace_oid("local_cache", true);
	if (!OidIsValid(namespace_oid))
		return false;

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
				trigger->tgtype == (TRIGGER_TYPE_BEFORE | TRIGGER_TYPE_INSERT |
					TRIGGER_TYPE_UPDATE | TRIGGER_TYPE_DELETE |
					TRIGGER_TYPE_TRUNCATE) &&
				trigger->tgnargs == 0 &&
				pglc_sql_trigger_function(trigger->tgfoid, namespace_oid,
									  "_statement_guard");
		}
		else if (strcmp(trigger->tgname, "pg_local_cache_row_invalidate") == 0)
		{
			row_found = trigger->tgenabled == TRIGGER_FIRES_ALWAYS &&
				plain_trigger &&
				trigger->tgtype == (TRIGGER_TYPE_ROW | TRIGGER_TYPE_INSERT |
					TRIGGER_TYPE_UPDATE | TRIGGER_TYPE_DELETE) &&
				trigger->tgnargs == 2 &&
				pglc_sql_trigger_function(trigger->tgfoid, namespace_oid,
									  "_row_invalidate") &&
				strcmp(trigger->tgargs[0], meta->nspace) == 0 &&
				strcmp(trigger->tgargs[1], meta->key_column) == 0;
		}
		else if (strcmp(trigger->tgname,
						"pg_local_cache_truncate_invalidate") == 0)
		{
			truncate_found = trigger->tgenabled == TRIGGER_FIRES_ALWAYS &&
				plain_trigger &&
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
		type_oid == VARCHAROID || type_oid == UUIDOID;
}

static bool
pglc_sql_value_type_supported(Oid type_oid)
{
	return type_oid == INT2OID || type_oid == INT4OID ||
		type_oid == INT8OID || type_oid == NUMERICOID ||
		type_oid == BOOLOID || type_oid == TEXTOID ||
		type_oid == VARCHAROID || type_oid == BPCHAROID ||
			type_oid == UUIDOID || type_oid == JSONOID || type_oid == JSONBOID;
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
pglc_sql_relation_base_meta(Relation relation, PgLocalCacheSqlMeta *meta)
{
	TupleDesc	descriptor;
	Form_pg_attribute key_attribute;
	Form_pg_attribute value_attribute;

	if (relation->rd_rel->relkind != RELKIND_RELATION ||
		relation->rd_rel->relpersistence != RELPERSISTENCE_PERMANENT ||
		relation->rd_rel->relam != HEAP_TABLE_AM_OID ||
		relation->rd_rel->relispartition ||
		relation->rd_rel->relrowsecurity || relation->rd_rel->relforcerowsecurity ||
		!pglc_sql_triggers_valid(relation, meta))
		return false;

	descriptor = RelationGetDescr(relation);
	if (meta->key_attno <= 0 || meta->key_attno > descriptor->natts ||
		meta->value_attno <= 0 || meta->value_attno > descriptor->natts)
		return false;
	key_attribute = TupleDescAttr(descriptor, meta->key_attno - 1);
	value_attribute = TupleDescAttr(descriptor, meta->value_attno - 1);
	if (key_attribute->attisdropped || value_attribute->attisdropped ||
		!key_attribute->attnotnull || !value_attribute->attnotnull ||
		!pglc_sql_key_type_supported(key_attribute->atttypid) ||
		(OidIsValid(key_attribute->attcollation) &&
		 !get_collation_isdeterministic(key_attribute->attcollation)) ||
		!pglc_sql_value_type_supported(value_attribute->atttypid))
		return false;

	meta->key_type = key_attribute->atttypid;
	meta->value_type = value_attribute->atttypid;
	meta->key_typmod = key_attribute->atttypmod;
	meta->value_typmod = value_attribute->atttypmod;
	return true;
}

static bool
pglc_sql_relation_meta(Relation relation, PgLocalCacheSqlMeta *meta)
{
	return pglc_sql_relation_base_meta(relation, meta) &&
		!pglc_sql_relation_has_children(relation) &&
		!pglc_sql_relation_has_parent(relation);
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
	TargetEntry *target;

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
		list_length(query->targetList) != 1)
		return false;

	from_item = (Node *) linitial(query->jointree->fromlist);
	if (!IsA(from_item, RangeTblRef) ||
		((RangeTblRef *) from_item)->rtindex != rti)
		return false;
	target = (TargetEntry *) linitial(query->targetList);
	if (target->resjunk || !IsA(target->expr, Var))
		return false;

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
pglc_sql_coerce_key_expr(Expr *expression, const PgLocalCacheSqlMeta *meta)
{
	Oid			expression_type = exprType((Node *) expression);
	Node	   *coerced;

	if (pglc_sql_key_datum_compatible(meta->key_type, expression_type))
		return expression;
	coerced = coerce_to_target_type(NULL, (Node *) expression,
								 expression_type, meta->key_type,
								 meta->key_typmod, COERCION_IMPLICIT,
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

static Var *
pglc_sql_key_var(Node *operand, Index rti, const PgLocalCacheSqlMeta *meta)
{
	Node	   *base;
	Var		   *var;

	if (!pglc_sql_key_datum_compatible(meta->key_type, exprType(operand)))
		return NULL;
	base = pglc_sql_strip_relabels(operand);
	if (!IsA(base, Var))
		return NULL;
	var = (Var *) base;
	if (var->varno != rti || var->varattno != meta->key_attno ||
		var->varlevelsup != 0 || var->vartype != meta->key_type)
		return NULL;
	return var;
}

static bool
pglc_sql_match_clause(RelOptInfo *rel, Index rti,
					  const PgLocalCacheSqlMeta *meta,
					  RestrictInfo **restrict_info, Expr **key_expr)
{
	RestrictInfo *rinfo;
	OpExpr	  *operator;
	Node	  *left;
	Node	  *right;
	Var		  *key_var;
	Expr	  *other;
	Expr	  *coerced_other;
	Node	  *other_base;

	if (list_length(rel->baserestrictinfo) != 1)
		return false;
	rinfo = (RestrictInfo *) linitial(rel->baserestrictinfo);
	if (!IsA(rinfo, RestrictInfo) || rinfo->pseudoconstant ||
		!IsA(rinfo->clause, OpExpr))
		return false;
	operator = (OpExpr *) rinfo->clause;
	if (operator->opresulttype != BOOLOID || list_length(operator->args) != 2)
		return false;

	left = (Node *) linitial(operator->args);
	right = (Node *) lsecond(operator->args);
	key_var = pglc_sql_key_var(left, rti, meta);
	if (key_var != NULL)
	{
		other = (Expr *) right;
	}
	else if ((key_var = pglc_sql_key_var(right, rti, meta)) != NULL)
	{
		other = (Expr *) left;
	}
	else
		return false;

	other_base = pglc_sql_strip_relabels((Node *) other);
	if ((!IsA(other_base, Const) && !IsA(other_base, Param)) ||
		(IsA(other_base, Param) &&
		 ((Param *) other_base)->paramkind != PARAM_EXTERN) ||
		!pglc_sql_key_input_supported(meta->key_type,
								 exprType((Node *) other)) ||
		key_var->vartype != meta->key_type)
		return false;

	/* The selected unique btree IndexPath validates equality opfamily/strategy. */
	coerced_other = pglc_sql_coerce_key_expr(other, meta);
	if (coerced_other == NULL)
		return false;

	*restrict_info = rinfo;
	*key_expr = coerced_other;
	return true;
}

static IndexPath *
pglc_sql_unique_index_path(PlannerInfo *root, RelOptInfo *rel,
							   RestrictInfo *rinfo, AttrNumber key_attno,
							   Oid key_type)
{
	ListCell   *cell;
	IndexPath  *best = NULL;
	TypeCacheEntry *type_cache;
	OpExpr	   *operator = (OpExpr *) rinfo->clause;
	Node	   *left = (Node *) linitial(operator->args);
	Node	   *right = (Node *) lsecond(operator->args);

	if (!enable_indexscan)
		return NULL;
	type_cache = lookup_type_cache(key_type, TYPECACHE_BTREE_OPFAMILY);
	if (!OidIsValid(type_cache->btree_opf))
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
		RestrictInfo *indexqual_rinfo;
		Oid			index_operator;
		IndexClause *index_clause;
		IndexPath  *index_path;

		if (index_info == NULL || index_info->relam != BTREE_AM_OID ||
			!index_info->unique || !index_info->immediate ||
			index_info->hypothetical || !index_info->amhasgettuple ||
			index_info->nkeycolumns != 1 ||
			index_info->indexkeys[0] != key_attno ||
			index_info->opfamily[0] != type_cache->btree_opf ||
			index_info->indpred != NIL ||
			(index_info->indexcollations[0] != InvalidOid &&
			 index_info->indexcollations[0] != operator->inputcollid))
			continue;

		if (match_index_to_operand(left, 0, index_info))
		{
			index_operator = operator->opno;
			indexqual_rinfo = rinfo;
		}
		else if (match_index_to_operand(right, 0, index_info))
		{
			index_operator = get_commutator(operator->opno);
			if (!OidIsValid(index_operator))
				continue;
			indexqual_rinfo = commute_restrictinfo(rinfo, index_operator);
		}
		else
			continue;
		if (get_op_opfamily_strategy(index_operator,
								 type_cache->btree_opf) != BTEqualStrategyNumber)
			continue;

		index_clause = makeNode(IndexClause);
		index_clause->rinfo = rinfo;
		index_clause->indexquals = list_make1(indexqual_rinfo);
		index_clause->lossy = false;
		index_clause->indexcol = 0;
		index_clause->indexcols = NIL;
		index_path = create_index_path(root, index_info,
								   list_make1(index_clause), NIL, NIL, NIL,
								   ForwardScanDirection, false, NULL,
								   1.0, false);
		if (best == NULL || index_path->path.total_cost < best->path.total_cost)
			best = index_path;
	}
	return best;
}

static void
pglc_sql_set_rel_pathlist(PlannerInfo *root, RelOptInfo *rel, Index rti,
						  RangeTblEntry *rte)
{
	PgLocalCacheSqlMeta meta;
	Relation	relation;
	TargetEntry *target;
	Var		  *target_var;
	RestrictInfo *rinfo;
	Expr	  *key_expr;
	IndexPath  *index_path;
	CustomPath *custom_path;
	List	   *private = NIL;

	if (previous_set_rel_pathlist_hook != NULL)
		previous_set_rel_pathlist_hook(root, rel, rti, rte);

	if (!pglc_sql_cache || pglc_shared == NULL ||
		XactIsoLevel != XACT_READ_COMMITTED || RecoveryInProgress() ||
		pglc_current_transaction_is_dirty() ||
		!pglc_sql_simple_query(root, rel, rti, rte) ||
		!pglc_sql_read_mapping(rte->relid, &meta))
		return;

	relation = table_open(rte->relid, NoLock);
	if (!pglc_sql_relation_meta(relation, &meta))
	{
		table_close(relation, NoLock);
		return;
	}

	target = (TargetEntry *) linitial(root->parse->targetList);
	target_var = (Var *) target->expr;
	if (target_var->varno != rti || target_var->varattno != meta.value_attno ||
		target_var->varlevelsup != 0 || target_var->vartype != meta.value_type ||
		!pglc_sql_match_clause(rel, rti, &meta, &rinfo, &key_expr))
	{
		table_close(relation, NoLock);
		return;
	}
	table_close(relation, NoLock);

	index_path = pglc_sql_unique_index_path(root, rel, rinfo,
										meta.key_attno, meta.key_type);
	if (index_path == NULL)
		return;

	private = lappend(private, makeString(pstrdup(meta.nspace)));
	private = lappend(private, pglc_sql_oid_const(meta.relation_oid));
	private = lappend(private, makeInteger(meta.key_attno));
	private = lappend(private, makeInteger(meta.value_attno));
	private = lappend(private, pglc_sql_oid_const(meta.key_type));
	private = lappend(private, pglc_sql_oid_const(meta.value_type));
	private = lappend(private, makeInteger(meta.key_typmod));
	private = lappend(private, makeInteger(meta.value_typmod));
	private = lappend(private, pglc_sql_int8_const(meta.config_generation));
	private = lappend(private, key_expr);

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
	Expr	   *key_expr;
	AttrNumber value_attno;
	Oid			value_type;
	int32		value_typmod;
	int			child_value_resno;
	int			child_ctid_resno;
	int			child_xmin_resno;

	Assert(list_length(custom_plans) == 1);
	Assert(list_length(best_path->custom_private) ==
		   PGLC_PRIVATE_PLAN_ITEMS + 1);
	child = (Plan *) linitial(custom_plans);
	private = list_copy_head(best_path->custom_private,
							 PGLC_PRIVATE_PLAN_ITEMS);
	key_expr = (Expr *) list_nth(best_path->custom_private,
								 PGLC_PRIVATE_KEY_EXPR);
	value_attno = intVal(list_nth(private, PGLC_PRIVATE_VALUE_ATTNO));
	value_type = pglc_sql_private_oid(private, PGLC_PRIVATE_VALUE_TYPE);
	value_typmod = intVal(list_nth(private, PGLC_PRIVATE_VALUE_TYPMOD));

	child_value_resno = list_length(child->targetlist) + 1;
	child->targetlist = lappend(child->targetlist,
		makeTargetEntry((Expr *) makeVar(rel->relid, value_attno,
									 value_type, value_typmod,
									 get_typcollation(value_type), 0),
					child_value_resno, NULL, true));
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

	private = lappend(private, makeInteger(child_value_resno));
	private = lappend(private, makeInteger(child_ctid_resno));
	private = lappend(private, makeInteger(child_xmin_resno));

	scan = makeNode(CustomScan);
	scan->scan.plan.targetlist = tlist;
	scan->scan.plan.qual = NIL;
	scan->scan.scanrelid = rel->relid;
	scan->flags = 0;
	scan->custom_plans = custom_plans;
	scan->custom_exprs = list_make1(copyObject(key_expr));
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

	state = (PgLocalCacheSqlScanState *)
		palloc0(sizeof(PgLocalCacheSqlScanState));
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
	PgLocalCacheSqlMeta planned_meta;
	PgLocalCacheSqlMeta current_meta;
	TupleDesc	descriptor;
	Form_pg_attribute key_attribute;
	uint64		current_generation;

	if (relation == NULL ||
		RelationGetRelid(relation) != state->mapping.relation_oid ||
		relation->rd_rel->relkind != RELKIND_RELATION ||
		relation->rd_rel->relpersistence != RELPERSISTENCE_PERMANENT ||
		relation->rd_rel->relam != HEAP_TABLE_AM_OID ||
		relation->rd_rel->relrowsecurity || relation->rd_rel->relforcerowsecurity)
		return false;

	descriptor = RelationGetDescr(relation);
	if (state->key_attno <= 0 || state->key_attno > descriptor->natts ||
		state->value_attno <= 0 || state->value_attno > descriptor->natts)
		return false;
	key_attribute = TupleDescAttr(descriptor, state->key_attno - 1);
	MemSet(&planned_meta, 0, sizeof(planned_meta));
	strlcpy(planned_meta.nspace, state->mapping.nspace,
			sizeof(planned_meta.nspace));
	strlcpy(planned_meta.key_column, NameStr(key_attribute->attname),
			sizeof(planned_meta.key_column));
	planned_meta.relation_oid = state->mapping.relation_oid;
	planned_meta.key_attno = state->key_attno;
	planned_meta.value_attno = state->value_attno;
	/*
	 * Keep the ordinary execution path free of mapping-table and inheritance
	 * catalog scans.  Relcache-backed shape, RLS, partition and exact trigger
	 * checks still run for every execution.  Relevant DDL advances the global
	 * generation; only that slow path performs the exact hierarchy and mapping
	 * revalidation below.
	 */
	if (!pglc_sql_relation_base_meta(relation, &planned_meta) ||
		planned_meta.key_type != state->mapping.key_type ||
		planned_meta.value_type != state->mapping.value_type ||
		planned_meta.key_typmod != state->mapping.key_typmod ||
		planned_meta.value_typmod != state->mapping.value_typmod)
		return false;

	current_generation = pglc_config_generation();
	if (state->mapping.config_generation == current_generation)
	{
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
		!pglc_sql_relation_meta(relation, &current_meta))
		return false;
	if (current_meta.relation_oid != state->mapping.relation_oid ||
		strcmp(current_meta.nspace, state->mapping.nspace) != 0 ||
		current_meta.key_attno != state->key_attno ||
		current_meta.value_attno != state->value_attno ||
		current_meta.key_type != state->mapping.key_type ||
		current_meta.value_type != state->mapping.value_type ||
		current_meta.key_typmod != state->mapping.key_typmod ||
		current_meta.value_typmod != state->mapping.value_typmod)
		return false;

	state->mapping.config_generation = current_meta.config_generation;
	(void) scan;
	return true;
}

static void
pglc_sql_begin(CustomScanState *node, EState *estate, int eflags)
{
	PgLocalCacheSqlScanState *state = (PgLocalCacheSqlScanState *) node;
	CustomScan *scan = (CustomScan *) node->ss.ps.plan;
	Oid			key_output_oid;
	Oid			value_input_oid;
	Oid			value_output_oid;
	bool		is_varlena;

	Assert(list_length(scan->custom_plans) == 1);
	Assert(list_length(scan->custom_exprs) == 1);
	Assert(list_length(scan->custom_private) ==
		   PGLC_PRIVATE_PLAN_ITEMS + 3);

	state->key_attno = intVal(list_nth(scan->custom_private,
									PGLC_PRIVATE_KEY_ATTNO));
	state->value_attno = intVal(list_nth(scan->custom_private,
									  PGLC_PRIVATE_VALUE_ATTNO));
	state->child_value_resno = intVal(list_nth(scan->custom_private,
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
	state->mapping.key_type = pglc_sql_private_oid(scan->custom_private,
											 PGLC_PRIVATE_KEY_TYPE);
	state->mapping.value_type = pglc_sql_private_oid(scan->custom_private,
											   PGLC_PRIVATE_VALUE_TYPE);
	state->mapping.key_typmod = intVal(list_nth(scan->custom_private,
											 PGLC_PRIVATE_KEY_TYPMOD));
	state->mapping.value_typmod = intVal(list_nth(scan->custom_private,
											   PGLC_PRIVATE_VALUE_TYPMOD));
	state->mapping.config_generation =
		pglc_sql_private_generation(scan->custom_private);

	getTypeOutputInfo(state->mapping.key_type, &key_output_oid, &is_varlena);
	fmgr_info(key_output_oid, &state->key_output);
	getTypeInputInfo(state->mapping.value_type, &value_input_oid,
					 &state->value_ioparam);
	fmgr_info(value_input_oid, &state->value_input);
	getTypeOutputInfo(state->mapping.value_type, &value_output_oid,
					  &is_varlena);
	fmgr_info(value_output_oid, &state->value_output);

	state->child = ExecInitNode((Plan *) linitial(scan->custom_plans),
								estate, eflags);
	state->css.custom_ps = list_make1(state->child);
	state->key_expr = ExecInitExpr((Expr *) linitial(scan->custom_exprs),
								 &state->css.ss.ps);
	state->latest_slot = table_slot_create(state->css.ss.ss_currentRelation,
											&estate->es_tupleTable);
	state->runtime_valid = pglc_sql_validate_runtime(state, scan);
	state->done = false;
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
pglc_sql_form_scan_tuple(PgLocalCacheSqlScanState *state,
						 Datum key, Datum value, bool value_isnull)
{
	TupleTableSlot *slot = state->css.ss.ss_ScanTupleSlot;
	int			natts = slot->tts_tupleDescriptor->natts;

	ExecClearTuple(slot);
	MemSet(slot->tts_values, 0, sizeof(Datum) * natts);
	MemSet(slot->tts_isnull, true, sizeof(bool) * natts);
	slot->tts_values[state->key_attno - 1] = key;
	slot->tts_isnull[state->key_attno - 1] = false;
	slot->tts_values[state->value_attno - 1] = value;
	slot->tts_isnull[state->value_attno - 1] = value_isnull;
	slot->tts_tableOid = state->mapping.relation_oid;
	ExecStoreVirtualTuple(slot);
	return slot;
}

static bool
pglc_sql_maybe_store(PgLocalCacheSqlScanState *state,
					 const char *canonical_key,
					 const PgLocalCacheReadToken *token,
					 uint64 load_id, TupleTableSlot *child_slot,
					 Datum value, bool value_isnull)
{
	Datum		ctid_datum;
	Datum		xmin_datum;
	ItemPointerData ctid;
	TransactionId source_xmin;
	volatile Snapshot latest_snapshot = NULL;
	volatile bool validated = false;
	bool		isnull;
	char	   *serialized;
	Size		serialized_len;
	MemoryContext old_context;

	if (load_id == 0 || value_isnull || !pglc_sql_can_use_cache(state))
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

	PG_TRY();
	{
		Datum		latest_xmin_datum;
		TransactionId latest_xmin;

		latest_snapshot = RegisterSnapshot(GetLatestSnapshot());
		ExecClearTuple(state->latest_slot);
		if (table_tuple_fetch_row_version(state->css.ss.ss_currentRelation,
										  &ctid, (Snapshot) latest_snapshot,
										  state->latest_slot))
		{
			latest_xmin_datum = slot_getsysattr(state->latest_slot,
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

	old_context = MemoryContextSwitchTo(
		state->css.ss.ps.ps_ExprContext->ecxt_per_tuple_memory);
	serialized = OutputFunctionCall(&state->value_output, value);
	MemoryContextSwitchTo(old_context);
	serialized_len = strlen(serialized);
	if (serialized_len > PGLC_VALUE_MAX)
		return false;
	return pglc_cache_store(&state->mapping, canonical_key, token,
						serialized, serialized_len, false, load_id,
						source_xmin);
}

static TupleTableSlot *
pglc_sql_run_child(PgLocalCacheSqlScanState *state, Datum key,
				   const char *canonical_key,
				   const PgLocalCacheReadToken *token, uint64 load_id)
{
	volatile TupleTableSlot *result = NULL;

	PG_TRY();
	{
		TupleTableSlot *child_slot;
		Datum		value;
		bool		value_isnull;

		child_slot = ExecProcNode(state->child);
		if (!TupIsNull(child_slot))
		{
			value = slot_getattr(child_slot, state->child_value_resno,
								 &value_isnull);
			if (load_id != 0 &&
				pglc_sql_maybe_store(state, canonical_key, token, load_id,
								 child_slot, value, value_isnull))
				pg_atomic_fetch_add_u64(&pglc_shared->sql_cache_fills, 1);
			result = pglc_sql_form_scan_tuple(state, key, value, value_isnull);
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
	Datum		key;
	bool		key_isnull;
	char	   *canonical_key;
	char		cached[PGLC_VALUE_MAX + 1];
	Size		cached_len;
	bool		negative;
	TransactionId source_xmin;
	PgLocalCacheReadToken token;
	PgLocalCacheSourceVisibility visibility = PGLC_SOURCE_SNAPSHOT_REJECTED;
	bool		hit;
	int			lookup_attempt;
	uint64		load_id = 0;
	MemoryContext old_context;

	if (state->done)
		return NULL;
	state->done = true;
	key = ExecEvalExprSwitchContext(state->key_expr, econtext, &key_isnull);
	if (key_isnull)
		return NULL;

	if (!pglc_sql_can_use_cache(state))
	{
		state->bypasses++;
		pg_atomic_fetch_add_u64(&pglc_shared->sql_cache_bypasses, 1);
		return pglc_sql_run_child(state, key, NULL, NULL, 0);
	}

	old_context = MemoryContextSwitchTo(econtext->ecxt_per_tuple_memory);
	canonical_key = OutputFunctionCall(&state->key_output, key);
	MemoryContextSwitchTo(old_context);
	if (strlen(canonical_key) >= PGLC_KEY_MAX)
	{
		state->bypasses++;
		pg_atomic_fetch_add_u64(&pglc_shared->sql_cache_bypasses, 1);
		return pglc_sql_run_child(state, key, NULL, NULL, 0);
	}

	for (lookup_attempt = 0; lookup_attempt < 2; lookup_attempt++)
	{
		hit = pglc_cache_lookup_quiet(&state->mapping, canonical_key,
									 cached, PGLC_VALUE_MAX, &cached_len,
									 &negative, &source_xmin, &token);
		if (!hit || negative)
			break;

		visibility = pglc_sql_source_visibility(
			source_xmin, token.source_observed_full_xid,
			state->css.ss.ps.state->es_snapshot);
		if (visibility == PGLC_SOURCE_VISIBLE)
		{
			Datum		value;

			cached[cached_len] = '\0';
			old_context = MemoryContextSwitchTo(econtext->ecxt_per_tuple_memory);
			value = InputFunctionCall(&state->value_input, cached,
									  state->value_ioparam,
									  state->mapping.value_typmod);
			MemoryContextSwitchTo(old_context);
			state->hits++;
			pg_atomic_fetch_add_u64(&pglc_shared->sql_cache_hits, 1);
			return pglc_sql_form_scan_tuple(state, key, value, false);
		}
		if (visibility != PGLC_SOURCE_AGE_EXPIRED || lookup_attempt != 0)
			break;

		/* Retire only the exact over-age positive observed by this lookup. */
		(void) pglc_cache_retire_positive(&state->mapping, canonical_key,
									  &token, source_xmin);
	}

	state->misses++;
	pg_atomic_fetch_add_u64(&pglc_shared->sql_cache_misses, 1);
	/* Negative entries and entries too new for this snapshot always fall back. */
	if (!hit && pglc_cache_claim_load(&state->mapping, canonical_key,
										&token, &load_id) != PGLC_LOAD_OWNER)
		load_id = 0;
	return pglc_sql_run_child(state, key, canonical_key, &token, load_id);
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
	previous_planner_hook = planner_hook;
	planner_hook = pglc_sql_planner;
	previous_set_rel_pathlist_hook = set_rel_pathlist_hook;
	set_rel_pathlist_hook = pglc_sql_set_rel_pathlist;
}
