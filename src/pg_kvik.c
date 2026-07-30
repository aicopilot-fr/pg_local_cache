#include "postgres.h"

#include <limits.h>

#include "access/htup_details.h"
#include "access/xact.h"
#include "catalog/pg_type_d.h"
#include "commands/event_trigger.h"
#include "commands/trigger.h"
#include "executor/spi.h"
#include "funcapi.h"
#include "miscadmin.h"
#include "postmaster/bgworker.h"
#include "storage/ipc.h"
#include "storage/shmem.h"
#include "utils/acl.h"
#include "utils/builtins.h"
#include "utils/guc.h"
#include "utils/hsearch.h"
#include "utils/jsonb.h"
#include "utils/lsyscache.h"
#include "utils/memutils.h"
#include "utils/rel.h"

#include "pg_kvik.h"

PG_MODULE_MAGIC;

int			pgk_port = 6380;
int			pgk_worker_count = 2;
int			pgk_cache_entries = 4096;
char	   *pgk_bind_address = NULL;
char	   *pgk_database = NULL;
char	   *pgk_role = NULL;
char	   *pgk_auth_token = NULL;

PgKvikSharedState *pgk_shared = NULL;
HTAB	   *pgk_cache_hash = NULL;
HTAB	   *pgk_relation_hash = NULL;

static shmem_request_hook_type previous_shmem_request_hook = NULL;
static shmem_startup_hook_type previous_shmem_startup_hook = NULL;
static bool pgk_was_preloaded = false;

typedef enum PgKvikDirtyKind
{
	PGK_DIRTY_KEY = 1,
	PGK_DIRTY_RELATION = 2,
	PGK_DIRTY_GLOBAL = 3
} PgKvikDirtyKind;

typedef struct PgKvikLocalDirtyKey
{
	uint8		kind;
	Oid			database_oid;
	char		nspace[PGK_NAMESPACE_MAX];
	char		key[PGK_KEY_MAX];
} PgKvikLocalDirtyKey;

typedef struct PgKvikLocalDirtyEntry
{
	PgKvikLocalDirtyKey key;
	Oid			relation_oid;
	bool		shared_marker_reserved;
} PgKvikLocalDirtyEntry;

static HTAB *local_dirty_hash = NULL;
static bool local_dirty_published = false;
static bool local_global_fallback = false;
static bool local_bump_config = false;

void		_PG_init(void);

PG_FUNCTION_INFO_V1(pg_kvik_row_invalidate);
PG_FUNCTION_INFO_V1(pg_kvik_truncate_invalidate);
PG_FUNCTION_INFO_V1(pg_kvik_ddl_invalidate);
PG_FUNCTION_INFO_V1(pg_kvik_reload);
PG_FUNCTION_INFO_V1(pg_kvik_invalidate);
PG_FUNCTION_INFO_V1(pg_kvik_stats);

static void pgk_shmem_request(void);
static void pgk_shmem_startup(void);
static Size pgk_shmem_size(void);
static void pgk_xact_callback(XactEvent event, void *arg);
static void pgk_backend_exit(int code, Datum arg);
static void pgk_publish_dirty(void);
static void pgk_finish_dirty(bool committed);
static void pgk_collect_key(Oid database_oid, Oid relation_oid,
							const char *nspace, const char *key);
static void pgk_collect_relation(Oid database_oid, Oid relation_oid,
								 const char *nspace);
static void pgk_collect_global(bool bump_config);

static void
pgk_define_gucs(void)
{
	DefineCustomIntVariable("pg_kvik.port",
							"TCP port for the RESP2 listener; 0 disables it.",
							NULL,
							&pgk_port,
							6380,
							0,
							65535,
							PGC_POSTMASTER,
							0,
							NULL,
							NULL,
							NULL);

	DefineCustomIntVariable("pg_kvik.workers",
							"Number of RESP background workers.",
							NULL,
							&pgk_worker_count,
							2,
							1,
							32,
							PGC_POSTMASTER,
							0,
							NULL,
							NULL,
							NULL);

	DefineCustomIntVariable("pg_kvik.cache_entries",
							"Maximum number of shared row-cache entries.",
							NULL,
							&pgk_cache_entries,
							4096,
							128,
							65536,
							PGC_POSTMASTER,
							0,
							NULL,
							NULL,
							NULL);

	DefineCustomStringVariable("pg_kvik.bind_address",
							   "IPv4 address for the RESP2 listener.",
							   NULL,
							   &pgk_bind_address,
							   "127.0.0.1",
							   PGC_POSTMASTER,
							   0,
							   NULL,
							   NULL,
							   NULL);

	DefineCustomStringVariable("pg_kvik.database",
							   "Database served by this pg_kvik instance.",
							   NULL,
							   &pgk_database,
							   "postgres",
							   PGC_POSTMASTER,
							   0,
							   NULL,
							   NULL,
							   NULL);

	DefineCustomStringVariable("pg_kvik.role",
							   "Database role used by RESP workers; empty uses the bootstrap superuser.",
							   NULL,
							   &pgk_role,
							   "",
							   PGC_POSTMASTER,
							   GUC_SUPERUSER_ONLY,
							   NULL,
							   NULL,
							   NULL);

	DefineCustomStringVariable("pg_kvik.auth_token",
							   "Token accepted by the RESP AUTH command.",
							   NULL,
							   &pgk_auth_token,
							   "",
							   PGC_POSTMASTER,
							   GUC_SUPERUSER_ONLY | GUC_NO_SHOW_ALL,
							   NULL,
							   NULL,
							   NULL);

	MarkGUCPrefixReserved("pg_kvik");
}

void
_PG_init(void)
{
	BackgroundWorker worker;
	int			i;

	pgk_define_gucs();
	RegisterXactCallback(pgk_xact_callback, NULL);
	before_shmem_exit(pgk_backend_exit, (Datum) 0);

	if (!process_shared_preload_libraries_in_progress)
		return;

	pgk_was_preloaded = true;

	previous_shmem_request_hook = shmem_request_hook;
	shmem_request_hook = pgk_shmem_request;
	previous_shmem_startup_hook = shmem_startup_hook;
	shmem_startup_hook = pgk_shmem_startup;

	if (pgk_port == 0)
		return;

	for (i = 0; i < pgk_worker_count; i++)
	{
		memset(&worker, 0, sizeof(worker));
		snprintf(worker.bgw_name, BGW_MAXLEN, "pg_kvik RESP worker %d", i);
		strlcpy(worker.bgw_type, "pg_kvik RESP worker", BGW_MAXLEN);
		worker.bgw_flags = BGWORKER_SHMEM_ACCESS |
			BGWORKER_BACKEND_DATABASE_CONNECTION;
		worker.bgw_start_time = BgWorkerStart_RecoveryFinished;
		worker.bgw_restart_time = 1;
		strlcpy(worker.bgw_library_name, "pg_kvik", BGW_MAXLEN);
		strlcpy(worker.bgw_function_name, "pg_kvik_worker_main", BGW_MAXLEN);
		worker.bgw_main_arg = Int32GetDatum(i);
		worker.bgw_notify_pid = 0;
		RegisterBackgroundWorker(&worker);
	}
}

static Size
pgk_shmem_size(void)
{
	Size		size = MAXALIGN(sizeof(PgKvikSharedState));

	size = add_size(size,
					hash_estimate_size(pgk_cache_entries,
									   sizeof(PgKvikCacheEntry)));
	size = add_size(size,
					hash_estimate_size(PGK_MAX_MAPPINGS,
									   sizeof(PgKvikRelationState)));
	return size;
}

static void
pgk_shmem_request(void)
{
	if (previous_shmem_request_hook)
		previous_shmem_request_hook();

	RequestAddinShmemSpace(pgk_shmem_size());
	RequestNamedLWLockTranche("pg_kvik", 1);
}

static void
pgk_shmem_startup(void)
{
	bool		found;
	HASHCTL		control;

	if (previous_shmem_startup_hook)
		previous_shmem_startup_hook();

	LWLockAcquire(AddinShmemInitLock, LW_EXCLUSIVE);

	pgk_shared = ShmemInitStruct("pg_kvik shared state",
								 sizeof(PgKvikSharedState),
								 &found);
	if (!found)
	{
		memset(pgk_shared, 0, sizeof(PgKvikSharedState));
		pgk_shared->lock = &(GetNamedLWLockTranche("pg_kvik"))->lock;
		pg_atomic_init_u64(&pgk_shared->config_generation, 1);
		pg_atomic_init_u64(&pgk_shared->cache_hits, 0);
		pg_atomic_init_u64(&pgk_shared->cache_misses, 0);
		pg_atomic_init_u64(&pgk_shared->negative_hits, 0);
		pg_atomic_init_u64(&pgk_shared->database_reads, 0);
		pg_atomic_init_u64(&pgk_shared->database_writes, 0);
		pg_atomic_init_u64(&pgk_shared->invalidations, 0);
		pg_atomic_init_u64(&pgk_shared->evictions, 0);
	}

	memset(&control, 0, sizeof(control));
	control.keysize = sizeof(PgKvikCacheKey);
	control.entrysize = sizeof(PgKvikCacheEntry);
	pgk_cache_hash = ShmemInitHash("pg_kvik cache",
								   pgk_cache_entries,
								   pgk_cache_entries,
								   &control,
								   HASH_ELEM | HASH_BLOBS);

	memset(&control, 0, sizeof(control));
	control.keysize = sizeof(PgKvikRelationKey);
	control.entrysize = sizeof(PgKvikRelationState);
	pgk_relation_hash = ShmemInitHash("pg_kvik relation state",
									  PGK_MAX_MAPPINGS,
									  PGK_MAX_MAPPINGS,
									  &control,
									  HASH_ELEM | HASH_BLOBS);

	LWLockRelease(AddinShmemInitLock);
}

void
pgk_require_preload(void)
{
	if (!pgk_was_preloaded || pgk_shared == NULL ||
		pgk_cache_hash == NULL || pgk_relation_hash == NULL)
		ereport(ERROR,
				(errcode(ERRCODE_OBJECT_NOT_IN_PREREQUISITE_STATE),
				 errmsg("pg_kvik must be loaded through shared_preload_libraries")));
}

uint64
pgk_config_generation(void)
{
	pgk_require_preload();
	return pg_atomic_read_u64(&pgk_shared->config_generation);
}

static void
make_cache_key(PgKvikCacheKey *result, Oid database_oid,
			   const char *nspace, const char *key)
{
	memset(result, 0, sizeof(*result));
	result->database_oid = database_oid;
	strlcpy(result->nspace, nspace, sizeof(result->nspace));
	strlcpy(result->key, key, sizeof(result->key));
}

static void
make_relation_key(PgKvikRelationKey *result, Oid database_oid,
				  const char *nspace)
{
	memset(result, 0, sizeof(*result));
	result->database_oid = database_oid;
	strlcpy(result->nspace, nspace, sizeof(result->nspace));
}

static PgKvikRelationState *
get_relation_state(Oid database_oid, Oid relation_oid,
				   const char *nspace, bool create)
{
	PgKvikRelationKey key;
	PgKvikRelationState *state;
	bool		found;

	make_relation_key(&key, database_oid, nspace);
	state = hash_search(pgk_relation_hash, &key,
						create ? HASH_ENTER_NULL : HASH_FIND,
						&found);
	if (state != NULL && !found)
	{
		PgKvikRelationKey saved_key = state->key;

		memset(state, 0, sizeof(*state));
		state->key = saved_key;
		state->relation_oid = relation_oid;
	}
	else if (state != NULL && OidIsValid(relation_oid))
		state->relation_oid = relation_oid;
	return state;
}

static bool
evict_one_cache_entry(void)
{
	HASH_SEQ_STATUS sequence;
	PgKvikCacheEntry *entry;
	PgKvikCacheKey victim;
	uint64		oldest = PG_UINT64_MAX;
	bool		have_victim = false;

	hash_seq_init(&sequence, pgk_cache_hash);
	while ((entry = hash_seq_search(&sequence)) != NULL)
	{
		if (entry->dirty_writers == 0 && entry->last_access <= oldest)
		{
			oldest = entry->last_access;
			victim = entry->key;
			have_victim = true;
		}
	}

	if (!have_victim)
		return false;

	(void) hash_search(pgk_cache_hash, &victim, HASH_REMOVE, NULL);
	pg_atomic_fetch_add_u64(&pgk_shared->evictions, 1);
	return true;
}

static PgKvikCacheEntry *
get_cache_entry(Oid database_oid, Oid relation_oid,
				const char *nspace, const char *key, bool create)
{
	PgKvikCacheKey cache_key;
	PgKvikCacheEntry *entry;
	bool		found;

	make_cache_key(&cache_key, database_oid, nspace, key);
	entry = hash_search(pgk_cache_hash, &cache_key,
						create ? HASH_ENTER_NULL : HASH_FIND,
						&found);
	if (entry == NULL && create && evict_one_cache_entry())
		entry = hash_search(pgk_cache_hash, &cache_key,
							HASH_ENTER_NULL, &found);

	if (entry != NULL && !found)
	{
		PgKvikCacheKey saved_key = entry->key;

		memset(entry, 0, sizeof(*entry));
		entry->key = saved_key;
		entry->relation_oid = relation_oid;
	}
	else if (entry != NULL && OidIsValid(relation_oid))
		entry->relation_oid = relation_oid;
	return entry;
}

static uint64
invalidate_namespace_locked(Oid database_oid, const char *nspace)
{
	HASH_SEQ_STATUS sequence;
	PgKvikCacheEntry *entry;
	uint64		count = 0;

	hash_seq_init(&sequence, pgk_cache_hash);
	while ((entry = hash_seq_search(&sequence)) != NULL)
	{
		if (entry->key.database_oid == database_oid &&
			strncmp(entry->key.nspace, nspace, PGK_NAMESPACE_MAX) == 0)
		{
			if (entry->valid)
				count++;
			entry->valid = false;
		}
	}
	return count;
}

static uint64
invalidate_all_locked(void)
{
	HASH_SEQ_STATUS sequence;
	PgKvikCacheEntry *entry;
	uint64		count = 0;

	hash_seq_init(&sequence, pgk_cache_hash);
	while ((entry = hash_seq_search(&sequence)) != NULL)
	{
		if (entry->valid)
			count++;
		entry->valid = false;
	}
	return count;
}

bool
pgk_cache_lookup(const PgKvikMapping *mapping, const char *canonical_key,
				 char *value, Size value_capacity, Size *value_len,
				 bool *negative, PgKvikReadToken *token)
{
	PgKvikRelationState *relation_state;
	PgKvikCacheEntry *entry;
	bool		hit = false;

	pgk_require_preload();
	memset(token, 0, sizeof(*token));
	*negative = false;
	*value_len = 0;

	LWLockAcquire(pgk_shared->lock, LW_EXCLUSIVE);
	relation_state = get_relation_state(MyDatabaseId, mapping->relation_oid,
										mapping->nspace, true);
	entry = get_cache_entry(MyDatabaseId, mapping->relation_oid,
							mapping->nspace, canonical_key, true);

	token->global_version = pgk_shared->global_version;
	token->relation_version = relation_state ? relation_state->version : 0;
	token->key_version = entry ? entry->version : 0;
	token->has_entry = entry != NULL;
	token->cacheable = relation_state != NULL && entry != NULL &&
		pgk_shared->global_dirty_writers == 0 &&
		relation_state->dirty_writers == 0 &&
		entry->dirty_writers == 0;

	if (token->cacheable && entry->valid)
	{
		if (entry->negative)
		{
			*negative = true;
			hit = true;
		}
		else if (entry->value_len <= value_capacity)
		{
			memcpy(value, entry->value, entry->value_len);
			*value_len = entry->value_len;
			hit = true;
		}
		entry->last_access = ++pgk_shared->clock;
	}
	LWLockRelease(pgk_shared->lock);

	if (hit)
	{
		pg_atomic_fetch_add_u64(&pgk_shared->cache_hits, 1);
		if (*negative)
			pg_atomic_fetch_add_u64(&pgk_shared->negative_hits, 1);
	}
	else
		pg_atomic_fetch_add_u64(&pgk_shared->cache_misses, 1);
	return hit;
}

void
pgk_cache_store(const PgKvikMapping *mapping, const char *canonical_key,
				const PgKvikReadToken *token, const char *value,
				Size value_len, bool negative)
{
	PgKvikRelationState *relation_state;
	PgKvikCacheEntry *entry;

	if (!token->cacheable || !token->has_entry || value_len > PGK_VALUE_MAX)
		return;

	LWLockAcquire(pgk_shared->lock, LW_EXCLUSIVE);
	relation_state = get_relation_state(MyDatabaseId, mapping->relation_oid,
										mapping->nspace, false);
	entry = get_cache_entry(MyDatabaseId, mapping->relation_oid,
							mapping->nspace, canonical_key, false);

	if (relation_state != NULL && entry != NULL &&
		pgk_shared->global_dirty_writers == 0 &&
		relation_state->dirty_writers == 0 &&
		entry->dirty_writers == 0 &&
		pgk_shared->global_version == token->global_version &&
		relation_state->version == token->relation_version &&
		entry->version == token->key_version)
	{
		entry->negative = negative;
		entry->value_len = negative ? 0 : value_len;
		if (!negative && value_len > 0)
			memcpy(entry->value, value, value_len);
		entry->valid = true;
		entry->last_access = ++pgk_shared->clock;
	}
	LWLockRelease(pgk_shared->lock);
}

uint64
pgk_cache_invalidate_namespace(Oid database_oid, const char *nspace)
{
	uint64		count;

	pgk_require_preload();
	LWLockAcquire(pgk_shared->lock, LW_EXCLUSIVE);
	pgk_shared->global_version++;
	count = invalidate_namespace_locked(database_oid, nspace);
	LWLockRelease(pgk_shared->lock);
	pg_atomic_fetch_add_u64(&pgk_shared->invalidations, count);
	return count;
}

void
pgk_note_database_read(void)
{
	pg_atomic_fetch_add_u64(&pgk_shared->database_reads, 1);
}

void
pgk_note_database_write(void)
{
	pg_atomic_fetch_add_u64(&pgk_shared->database_writes, 1);
}

static HTAB *
get_local_dirty_hash(void)
{
	HASHCTL		control;

	if (local_dirty_hash != NULL)
		return local_dirty_hash;

	memset(&control, 0, sizeof(control));
	control.keysize = sizeof(PgKvikLocalDirtyKey);
	control.entrysize = sizeof(PgKvikLocalDirtyEntry);
	control.hcxt = TopTransactionContext;
	local_dirty_hash = hash_create("pg_kvik transaction dirty keys",
								   64,
								   &control,
								   HASH_ELEM | HASH_BLOBS | HASH_CONTEXT);
	return local_dirty_hash;
}

static PgKvikLocalDirtyEntry *
collect_dirty(PgKvikDirtyKind kind, Oid database_oid, Oid relation_oid,
			  const char *nspace, const char *key)
{
	PgKvikLocalDirtyKey dirty_key;
	PgKvikLocalDirtyEntry *entry;
	bool		found;

	pgk_require_preload();
	memset(&dirty_key, 0, sizeof(dirty_key));
	dirty_key.kind = (uint8) kind;
	dirty_key.database_oid = database_oid;
	if (nspace)
		strlcpy(dirty_key.nspace, nspace, sizeof(dirty_key.nspace));
	if (key)
		strlcpy(dirty_key.key, key, sizeof(dirty_key.key));

	entry = hash_search(get_local_dirty_hash(), &dirty_key, HASH_ENTER, &found);
	if (!found)
	{
		entry->relation_oid = relation_oid;
		entry->shared_marker_reserved = false;
	}
	return entry;
}

static void
pgk_collect_key(Oid database_oid, Oid relation_oid,
				const char *nspace, const char *key)
{
	(void) collect_dirty(PGK_DIRTY_KEY, database_oid, relation_oid,
						 nspace, key);
}

static void
pgk_collect_relation(Oid database_oid, Oid relation_oid,
					 const char *nspace)
{
	(void) collect_dirty(PGK_DIRTY_RELATION, database_oid, relation_oid,
						 nspace, NULL);
}

static void
pgk_collect_global(bool bump_config)
{
	(void) collect_dirty(PGK_DIRTY_GLOBAL, MyDatabaseId, InvalidOid,
						 NULL, NULL);
	if (bump_config)
		local_bump_config = true;
}

static bool
local_has_global_dirty(void)
{
	HASH_SEQ_STATUS sequence;
	PgKvikLocalDirtyEntry *entry;

	hash_seq_init(&sequence, local_dirty_hash);
	while ((entry = hash_seq_search(&sequence)) != NULL)
	{
		if (entry->key.kind == PGK_DIRTY_GLOBAL)
		{
			hash_seq_term(&sequence);
			return true;
		}
	}
	return false;
}

static bool
precreate_shared_entries_locked(void)
{
	HASH_SEQ_STATUS sequence;
	PgKvikLocalDirtyEntry *local;
	bool		success = true;

	hash_seq_init(&sequence, local_dirty_hash);
	while ((local = hash_seq_search(&sequence)) != NULL)
	{
		if (local->key.kind == PGK_DIRTY_KEY)
		{
			PgKvikCacheEntry *entry;

			entry = get_cache_entry(local->key.database_oid,
									local->relation_oid,
									local->key.nspace,
									local->key.key,
									true);
			if (entry == NULL)
			{
				hash_seq_term(&sequence);
				success = false;
				break;
			}
			entry->dirty_writers++;
			local->shared_marker_reserved = true;
		}
		else if (local->key.kind == PGK_DIRTY_RELATION)
		{
			PgKvikRelationState *state;

			state = get_relation_state(local->key.database_oid,
									   local->relation_oid,
									   local->key.nspace,
									   true);
			if (state == NULL)
			{
				hash_seq_term(&sequence);
				success = false;
				break;
			}
			state->dirty_writers++;
			local->shared_marker_reserved = true;
		}
	}

	if (!success)
	{
		hash_seq_init(&sequence, local_dirty_hash);
		while ((local = hash_seq_search(&sequence)) != NULL)
		{
			if (!local->shared_marker_reserved)
				continue;
			if (local->key.kind == PGK_DIRTY_KEY)
			{
				PgKvikCacheEntry *entry;

				entry = get_cache_entry(local->key.database_oid,
										local->relation_oid,
										local->key.nspace,
										local->key.key,
										false);
				Assert(entry != NULL && entry->dirty_writers > 0);
				if (entry != NULL && entry->dirty_writers > 0)
					entry->dirty_writers--;
			}
			else if (local->key.kind == PGK_DIRTY_RELATION)
			{
				PgKvikRelationState *state;

				state = get_relation_state(local->key.database_oid,
										   local->relation_oid,
										   local->key.nspace,
										   false);
				Assert(state != NULL && state->dirty_writers > 0);
				if (state != NULL && state->dirty_writers > 0)
					state->dirty_writers--;
			}
			local->shared_marker_reserved = false;
		}
	}
	return success;
}

static void
pgk_publish_dirty(void)
{
	HASH_SEQ_STATUS sequence;
	PgKvikLocalDirtyEntry *local;
	uint64		invalidated = 0;

	if (local_dirty_hash == NULL || local_dirty_published)
		return;

	LWLockAcquire(pgk_shared->lock, LW_EXCLUSIVE);
	pgk_shared->global_version++;

	if (local_has_global_dirty() || !precreate_shared_entries_locked())
	{
		pgk_shared->global_dirty_writers++;
		invalidated += invalidate_all_locked();
		local_global_fallback = true;
	}
	else
	{
		hash_seq_init(&sequence, local_dirty_hash);
		while ((local = hash_seq_search(&sequence)) != NULL)
		{
			if (local->key.kind == PGK_DIRTY_KEY)
			{
				PgKvikCacheEntry *entry;

				entry = get_cache_entry(local->key.database_oid,
										local->relation_oid,
										local->key.nspace,
										local->key.key,
										false);
				Assert(entry != NULL);
				if (entry->valid)
					invalidated++;
				entry->valid = false;
				entry->version++;
			}
			else if (local->key.kind == PGK_DIRTY_RELATION)
			{
				PgKvikRelationState *state;

				state = get_relation_state(local->key.database_oid,
										   local->relation_oid,
										   local->key.nspace,
										   false);
				Assert(state != NULL);
				state->version++;
				invalidated += invalidate_namespace_locked(
					local->key.database_oid, local->key.nspace);
			}
		}
	}

	local_dirty_published = true;
	LWLockRelease(pgk_shared->lock);
	pg_atomic_fetch_add_u64(&pgk_shared->invalidations, invalidated);
}

static void
pgk_finish_dirty(bool committed)
{
	HASH_SEQ_STATUS sequence;
	PgKvikLocalDirtyEntry *local;
	uint64		invalidated = 0;
	bool		bump_config_after_unlock = committed && local_bump_config;

	if (local_dirty_hash == NULL)
		return;

	if (local_dirty_published)
	{
		LWLockAcquire(pgk_shared->lock, LW_EXCLUSIVE);
		if (local_global_fallback)
		{
			invalidated += invalidate_all_locked();
			Assert(pgk_shared->global_dirty_writers > 0);
			pgk_shared->global_dirty_writers--;
		}
		else
		{
			hash_seq_init(&sequence, local_dirty_hash);
			while ((local = hash_seq_search(&sequence)) != NULL)
			{
				if (local->key.kind == PGK_DIRTY_KEY)
				{
					PgKvikCacheEntry *entry;

					entry = get_cache_entry(local->key.database_oid,
											local->relation_oid,
											local->key.nspace,
											local->key.key,
											false);
					if (entry != NULL)
					{
						if (entry->valid)
							invalidated++;
						entry->valid = false;
						Assert(entry->dirty_writers > 0);
						entry->dirty_writers--;
					}
				}
				else if (local->key.kind == PGK_DIRTY_RELATION)
				{
					PgKvikRelationState *state;

					state = get_relation_state(local->key.database_oid,
											   local->relation_oid,
											   local->key.nspace,
											   false);
					if (state != NULL)
					{
						invalidated += invalidate_namespace_locked(
							local->key.database_oid, local->key.nspace);
						Assert(state->dirty_writers > 0);
						state->dirty_writers--;
					}
				}
			}
		}
		if (bump_config_after_unlock)
		{
			pg_atomic_fetch_add_u64(&pgk_shared->config_generation, 1);
			bump_config_after_unlock = false;
		}
		LWLockRelease(pgk_shared->lock);
		pg_atomic_fetch_add_u64(&pgk_shared->invalidations, invalidated);
	}

	if (bump_config_after_unlock)
		pg_atomic_fetch_add_u64(&pgk_shared->config_generation, 1);

	local_dirty_hash = NULL;
	local_dirty_published = false;
	local_global_fallback = false;
	local_bump_config = false;
}

static void
pgk_xact_callback(XactEvent event, void *arg)
{
	switch (event)
	{
		case XACT_EVENT_PRE_COMMIT:
		case XACT_EVENT_PARALLEL_PRE_COMMIT:
			pgk_publish_dirty();
			break;
		case XACT_EVENT_COMMIT:
		case XACT_EVENT_PARALLEL_COMMIT:
			pgk_finish_dirty(true);
			break;
		case XACT_EVENT_ABORT:
		case XACT_EVENT_PARALLEL_ABORT:
			pgk_finish_dirty(false);
			break;
		case XACT_EVENT_PRE_PREPARE:
			if (local_dirty_hash != NULL)
				ereport(ERROR,
						(errcode(ERRCODE_FEATURE_NOT_SUPPORTED),
						 errmsg("PREPARE TRANSACTION is not supported after modifying a pg_kvik mapping")));
			break;
		default:
			break;
	}
}

static void
pgk_backend_exit(int code, Datum arg)
{
	if (local_dirty_hash != NULL && pgk_shared != NULL)
		pgk_finish_dirty(false);
}

static char *
tuple_key_as_cstring(TriggerData *trigger_data, HeapTuple tuple,
					 const char *column_name, bool *is_null)
{
	TupleDesc	descriptor = RelationGetDescr(trigger_data->tg_relation);
	AttrNumber	attribute_number;
	Form_pg_attribute attribute;
	Datum		value;
	Oid			output_function;
	bool		type_is_varlena;

	attribute_number = get_attnum(RelationGetRelid(trigger_data->tg_relation),
								  column_name);
	if (attribute_number == InvalidAttrNumber)
		ereport(ERROR,
				(errcode(ERRCODE_UNDEFINED_COLUMN),
				 errmsg("pg_kvik key column \"%s\" no longer exists",
						column_name)));

	attribute = TupleDescAttr(descriptor, attribute_number - 1);
	value = heap_getattr(tuple, attribute_number, descriptor, is_null);
	if (*is_null)
		return NULL;

	getTypeOutputInfo(attribute->atttypid, &output_function, &type_is_varlena);
	return OidOutputFunctionCall(output_function, value);
}

static void
collect_tuple_key(TriggerData *trigger_data, HeapTuple tuple,
				  const char *nspace, const char *column_name)
{
	char	   *key;
	bool		is_null;

	key = tuple_key_as_cstring(trigger_data, tuple, column_name, &is_null);
	if (is_null || key == NULL)
	{
		pgk_collect_relation(MyDatabaseId,
							 RelationGetRelid(trigger_data->tg_relation),
							 nspace);
		return;
	}

	if (strlen(key) >= PGK_KEY_MAX)
		pgk_collect_relation(MyDatabaseId,
							 RelationGetRelid(trigger_data->tg_relation),
							 nspace);
	else
		pgk_collect_key(MyDatabaseId,
						RelationGetRelid(trigger_data->tg_relation),
						nspace, key);
}

Datum
pg_kvik_row_invalidate(PG_FUNCTION_ARGS)
{
	TriggerData *trigger_data;
	const char *nspace;
	const char *column_name;

	if (!CALLED_AS_TRIGGER(fcinfo))
		ereport(ERROR,
				(errcode(ERRCODE_E_R_I_E_TRIGGER_PROTOCOL_VIOLATED),
				 errmsg("pg_kvik row invalidator must be called as a trigger")));

	trigger_data = (TriggerData *) fcinfo->context;
	if (!TRIGGER_FIRED_AFTER(trigger_data->tg_event) ||
		!TRIGGER_FIRED_FOR_ROW(trigger_data->tg_event) ||
		trigger_data->tg_trigger->tgnargs != 2)
		ereport(ERROR,
				(errcode(ERRCODE_E_R_I_E_TRIGGER_PROTOCOL_VIOLATED),
				 errmsg("invalid pg_kvik row trigger definition")));

	nspace = trigger_data->tg_trigger->tgargs[0];
	column_name = trigger_data->tg_trigger->tgargs[1];

	if (TRIGGER_FIRED_BY_INSERT(trigger_data->tg_event))
		collect_tuple_key(trigger_data, trigger_data->tg_trigtuple,
						  nspace, column_name);
	else if (TRIGGER_FIRED_BY_DELETE(trigger_data->tg_event))
		collect_tuple_key(trigger_data, trigger_data->tg_trigtuple,
						  nspace, column_name);
	else if (TRIGGER_FIRED_BY_UPDATE(trigger_data->tg_event))
	{
		collect_tuple_key(trigger_data, trigger_data->tg_trigtuple,
						  nspace, column_name);
		collect_tuple_key(trigger_data, trigger_data->tg_newtuple,
						  nspace, column_name);
	}

	if (TRIGGER_FIRED_BY_INSERT(trigger_data->tg_event) ||
		TRIGGER_FIRED_BY_DELETE(trigger_data->tg_event))
		PG_RETURN_POINTER(trigger_data->tg_trigtuple);
	PG_RETURN_POINTER(trigger_data->tg_newtuple);
}

Datum
pg_kvik_truncate_invalidate(PG_FUNCTION_ARGS)
{
	TriggerData *trigger_data;
	const char *nspace;

	if (!CALLED_AS_TRIGGER(fcinfo))
		ereport(ERROR,
				(errcode(ERRCODE_E_R_I_E_TRIGGER_PROTOCOL_VIOLATED),
				 errmsg("pg_kvik truncate invalidator must be called as a trigger")));

	trigger_data = (TriggerData *) fcinfo->context;
	if (!TRIGGER_FIRED_AFTER(trigger_data->tg_event) ||
		!TRIGGER_FIRED_FOR_STATEMENT(trigger_data->tg_event) ||
		!TRIGGER_FIRED_BY_TRUNCATE(trigger_data->tg_event) ||
		trigger_data->tg_trigger->tgnargs != 1)
		ereport(ERROR,
				(errcode(ERRCODE_E_R_I_E_TRIGGER_PROTOCOL_VIOLATED),
				 errmsg("invalid pg_kvik truncate trigger definition")));

	nspace = trigger_data->tg_trigger->tgargs[0];
	pgk_collect_relation(MyDatabaseId,
						 RelationGetRelid(trigger_data->tg_relation),
						 nspace);
	PG_RETURN_POINTER(NULL);
}

Datum
pg_kvik_ddl_invalidate(PG_FUNCTION_ARGS)
{
	if (!CALLED_AS_EVENT_TRIGGER(fcinfo))
		ereport(ERROR,
				(errcode(ERRCODE_E_R_I_E_EVENT_TRIGGER_PROTOCOL_VIOLATED),
				 errmsg("pg_kvik DDL invalidator must be called as an event trigger")));

	pgk_collect_global(true);
	PG_RETURN_VOID();
}

Datum
pg_kvik_reload(PG_FUNCTION_ARGS)
{
	if (!superuser())
		ereport(ERROR,
				(errcode(ERRCODE_INSUFFICIENT_PRIVILEGE),
				 errmsg("must be superuser to reload pg_kvik mappings")));
	pgk_collect_global(true);
	PG_RETURN_VOID();
}

static uint64
count_namespace_entries(Oid database_oid, const char *nspace)
{
	HASH_SEQ_STATUS sequence;
	PgKvikCacheEntry *entry;
	uint64		count = 0;

	LWLockAcquire(pgk_shared->lock, LW_SHARED);
	hash_seq_init(&sequence, pgk_cache_hash);
	while ((entry = hash_seq_search(&sequence)) != NULL)
	{
		if (entry->valid &&
			entry->key.database_oid == database_oid &&
			strncmp(entry->key.nspace, nspace, PGK_NAMESPACE_MAX) == 0)
			count++;
	}
	LWLockRelease(pgk_shared->lock);
	return count;
}

Datum
pg_kvik_invalidate(PG_FUNCTION_ARGS)
{
	text	   *namespace_text = PG_GETARG_TEXT_PP(0);
	char	   *nspace = text_to_cstring(namespace_text);
	uint64		count;

	if (!superuser())
		ereport(ERROR,
				(errcode(ERRCODE_INSUFFICIENT_PRIVILEGE),
				 errmsg("must be superuser to invalidate pg_kvik")));
	if (strlen(nspace) >= PGK_NAMESPACE_MAX)
		ereport(ERROR,
				(errcode(ERRCODE_NAME_TOO_LONG),
				 errmsg("pg_kvik namespace is too long")));

	pgk_require_preload();
	count = count_namespace_entries(MyDatabaseId, nspace);
	pgk_collect_relation(MyDatabaseId, InvalidOid, nspace);
	PG_RETURN_INT64((int64) count);
}

char *
pgk_stats_json(void)
{
	HASH_SEQ_STATUS sequence;
	PgKvikCacheEntry *entry;
	uint64		positive = 0;
	uint64		negative = 0;
	uint64		dirty = 0;
	uint64		total;
	uint64		cache_hits;
	uint64		cache_misses;
	uint64		negative_hits;
	uint64		database_reads;
	uint64		database_writes;
	uint64		invalidations;
	uint64		evictions;

	pgk_require_preload();
	LWLockAcquire(pgk_shared->lock, LW_SHARED);
	hash_seq_init(&sequence, pgk_cache_hash);
	while ((entry = hash_seq_search(&sequence)) != NULL)
	{
		if (entry->valid && entry->negative)
			negative++;
		else if (entry->valid)
			positive++;
		if (entry->dirty_writers > 0)
			dirty++;
	}
	total = hash_get_num_entries(pgk_cache_hash);
	LWLockRelease(pgk_shared->lock);
	cache_hits = pg_atomic_read_u64(&pgk_shared->cache_hits);
	cache_misses = pg_atomic_read_u64(&pgk_shared->cache_misses);
	negative_hits = pg_atomic_read_u64(&pgk_shared->negative_hits);
	database_reads = pg_atomic_read_u64(&pgk_shared->database_reads);
	database_writes = pg_atomic_read_u64(&pgk_shared->database_writes);
	invalidations = pg_atomic_read_u64(&pgk_shared->invalidations);
	evictions = pg_atomic_read_u64(&pgk_shared->evictions);

	return psprintf(
		"{\"entries\":" UINT64_FORMAT
		",\"positive_entries\":" UINT64_FORMAT
		",\"negative_entries\":" UINT64_FORMAT
		",\"dirty_entries\":" UINT64_FORMAT
		",\"store_size\":" UINT64_FORMAT
		",\"cache_hits\":" UINT64_FORMAT
		",\"cache_misses\":" UINT64_FORMAT
		",\"negative_hits\":" UINT64_FORMAT
		",\"database_reads\":" UINT64_FORMAT
		",\"database_writes\":" UINT64_FORMAT
		",\"invalidations\":" UINT64_FORMAT
		",\"evictions\":" UINT64_FORMAT
		",\"cache_hit\":" UINT64_FORMAT
		",\"cache_miss\":" UINT64_FORMAT
		",\"cache_evict\":" UINT64_FORMAT
		",\"sql_gets\":" UINT64_FORMAT "}",
		total, positive, negative, dirty, positive + negative,
		cache_hits, cache_misses, negative_hits,
		database_reads, database_writes, invalidations, evictions,
		cache_hits, cache_misses, evictions, database_reads);
}

Datum
pg_kvik_stats(PG_FUNCTION_ARGS)
{
	char	   *json = pgk_stats_json();

	PG_RETURN_DATUM(DirectFunctionCall1(jsonb_in, CStringGetDatum(json)));
}
