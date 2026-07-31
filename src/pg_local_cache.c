#include "postgres.h"

#include <limits.h>

#include "access/htup_details.h"
#include "access/xact.h"
#include "catalog/pg_type_d.h"
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

#include "pg_local_cache.h"

PG_MODULE_MAGIC;

int			pglc_port = 6380;
int			pglc_worker_count = 4;
int			pglc_cache_entries = 16384;
int			pglc_idle_timeout_ms = 300000;
int			pglc_statement_timeout_ms = 2000;
int			pglc_lock_timeout_ms = 250;
int			pglc_max_pipeline_commands = 256;
int			pglc_max_dirty_keys = 4096;
char	   *pglc_bind_address = NULL;
char	   *pglc_database = NULL;
char	   *pglc_role = NULL;
char	   *pglc_auth_token = NULL;
char	   *pglc_auth_token_file = NULL;
bool		pglc_allow_superuser = false;

PgLocalCacheSharedState *pglc_shared = NULL;
HTAB	   *pglc_cache_hash = NULL;
HTAB	   *pglc_relation_hash = NULL;

static shmem_request_hook_type previous_shmem_request_hook = NULL;
static shmem_startup_hook_type previous_shmem_startup_hook = NULL;
static bool pglc_was_preloaded = false;

typedef enum PgLocalCacheDirtyKind
{
	PGLC_DIRTY_KEY = 1,
	PGLC_DIRTY_RELATION = 2,
	PGLC_DIRTY_GLOBAL = 3,
	PGLC_DIRTY_FORGET_RELATION = 4
} PgLocalCacheDirtyKind;

typedef struct PgLocalCacheLocalDirtyKey
{
	uint8		kind;
	Oid			database_oid;
	char		nspace[PGLC_NAMESPACE_MAX];
	char		key[PGLC_KEY_MAX];
} PgLocalCacheLocalDirtyKey;

typedef struct PgLocalCacheLocalDirtyEntry
{
	PgLocalCacheLocalDirtyKey key;
	Oid			relation_oid;
	bool		shared_marker_reserved;
} PgLocalCacheLocalDirtyEntry;

static HTAB *local_dirty_hash = NULL;
static bool local_dirty_published = false;
static bool local_global_fallback = false;
static bool local_bump_config = false;

void		_PG_init(void);

PG_FUNCTION_INFO_V1(pg_local_cache_row_invalidate);
PG_FUNCTION_INFO_V1(pg_local_cache_truncate_invalidate);
PG_FUNCTION_INFO_V1(pg_local_cache_reload);
PG_FUNCTION_INFO_V1(pg_local_cache_invalidate);
PG_FUNCTION_INFO_V1(pg_local_cache_stats);
PG_FUNCTION_INFO_V1(pg_local_cache_forget);

static void pglc_shmem_request(void);
static void pglc_shmem_startup(void);
static Size pglc_shmem_size(void);
static void pglc_xact_callback(XactEvent event, void *arg);
static void pglc_backend_exit(int code, Datum arg);
static void pglc_publish_dirty(void);
static void pglc_finish_dirty(bool committed);
static void pglc_collect_key(Oid database_oid, Oid relation_oid,
							const char *nspace, const char *key);
static void pglc_collect_relation(Oid database_oid, Oid relation_oid,
								 const char *nspace);
static void pglc_collect_forget_relation(Oid database_oid, Oid relation_oid,
										const char *nspace);
static void pglc_collect_global(bool bump_config);
static bool pglc_mapping_exists(const char *nspace);

static void
pglc_define_gucs(void)
{
	DefineCustomIntVariable("pg_local_cache.port",
							"TCP port for the RESP2 listener; 0 disables it.",
							NULL,
							&pglc_port,
							6380,
							0,
							65535,
							PGC_POSTMASTER,
							0,
							NULL,
							NULL,
							NULL);

	DefineCustomIntVariable("pg_local_cache.workers",
							"Number of RESP background workers.",
							NULL,
							&pglc_worker_count,
							4,
							1,
							32,
							PGC_POSTMASTER,
							0,
							NULL,
							NULL,
							NULL);

	DefineCustomIntVariable("pg_local_cache.cache_entries",
							"Maximum number of shared row-cache entries.",
							NULL,
							&pglc_cache_entries,
							16384,
							128,
							65536,
							PGC_POSTMASTER,
							0,
							NULL,
							NULL,
							NULL);

	DefineCustomIntVariable("pg_local_cache.idle_timeout_ms",
							"Close idle RESP clients after this interval.",
							NULL,
							&pglc_idle_timeout_ms,
							300000,
							1000,
							86400000,
							PGC_POSTMASTER,
							GUC_UNIT_MS,
							NULL,
							NULL,
							NULL);

	DefineCustomIntVariable("pg_local_cache.statement_timeout_ms",
							"Maximum duration of a database operation issued by a RESP worker.",
							NULL,
							&pglc_statement_timeout_ms,
							2000,
							100,
							60000,
							PGC_POSTMASTER,
							GUC_UNIT_MS,
							NULL,
							NULL,
							NULL);

	DefineCustomIntVariable("pg_local_cache.lock_timeout_ms",
							"Maximum lock wait for a database operation issued by a RESP worker.",
							NULL,
							&pglc_lock_timeout_ms,
							250,
							10,
							60000,
							PGC_POSTMASTER,
							GUC_UNIT_MS,
							NULL,
							NULL,
							NULL);

	DefineCustomIntVariable("pg_local_cache.max_pipeline_commands",
							"Maximum RESP commands processed for one client per event-loop turn.",
							NULL,
							&pglc_max_pipeline_commands,
							256,
							1,
							4096,
							PGC_POSTMASTER,
							0,
							NULL,
							NULL,
							NULL);

	DefineCustomIntVariable("pg_local_cache.max_dirty_keys",
							"Maximum per-key invalidations collected by one transaction before falling back to relation invalidation.",
							NULL,
							&pglc_max_dirty_keys,
							4096,
							128,
							1048576,
							PGC_POSTMASTER,
							0,
							NULL,
							NULL,
							NULL);

	DefineCustomStringVariable("pg_local_cache.bind_address",
							   "IPv4 address for the RESP2 listener.",
							   NULL,
							   &pglc_bind_address,
							   "127.0.0.1",
							   PGC_POSTMASTER,
							   0,
							   NULL,
							   NULL,
							   NULL);

	DefineCustomStringVariable("pg_local_cache.database",
							   "Database served by this pg_local_cache instance.",
							   NULL,
							   &pglc_database,
							   "postgres",
							   PGC_POSTMASTER,
							   0,
							   NULL,
							   NULL,
							   NULL);

	DefineCustomStringVariable("pg_local_cache.role",
							   "Dedicated LOGIN role used by RESP workers.",
							   NULL,
							   &pglc_role,
							   "local_cache_worker",
							   PGC_POSTMASTER,
							   GUC_SUPERUSER_ONLY,
							   NULL,
							   NULL,
							   NULL);

	DefineCustomStringVariable("pg_local_cache.auth_token",
							   "Inline RESP AUTH token for development; prefer auth_token_file.",
							   NULL,
							   &pglc_auth_token,
							   "",
							   PGC_POSTMASTER,
							   GUC_SUPERUSER_ONLY | GUC_NO_SHOW_ALL,
							   NULL,
							   NULL,
							   NULL);

	DefineCustomStringVariable("pg_local_cache.auth_token_file",
							   "Root-owned or worker-owned mode-0600 file containing the RESP AUTH token.",
							   NULL,
							   &pglc_auth_token_file,
							   "",
							   PGC_POSTMASTER,
							   GUC_SUPERUSER_ONLY | GUC_NO_SHOW_ALL,
							   NULL,
							   NULL,
							   NULL);

	DefineCustomBoolVariable("pg_local_cache.allow_superuser",
							 "Allow RESP workers to run as a superuser (development only).",
							 NULL,
							 &pglc_allow_superuser,
							 false,
							 PGC_POSTMASTER,
							 GUC_SUPERUSER_ONLY,
							 NULL,
							 NULL,
							 NULL);

	MarkGUCPrefixReserved("pg_local_cache");
}

void
_PG_init(void)
{
	BackgroundWorker worker;
	int			i;

	pglc_define_gucs();
	RegisterXactCallback(pglc_xact_callback, NULL);
	before_shmem_exit(pglc_backend_exit, (Datum) 0);

	if (!process_shared_preload_libraries_in_progress)
		return;

	pglc_was_preloaded = true;

	previous_shmem_request_hook = shmem_request_hook;
	shmem_request_hook = pglc_shmem_request;
	previous_shmem_startup_hook = shmem_startup_hook;
	shmem_startup_hook = pglc_shmem_startup;

	if (pglc_port == 0)
		return;

	for (i = 0; i < pglc_worker_count; i++)
	{
		memset(&worker, 0, sizeof(worker));
		snprintf(worker.bgw_name, BGW_MAXLEN, "pg_local_cache RESP worker %d", i);
		strlcpy(worker.bgw_type, "pg_local_cache RESP worker", BGW_MAXLEN);
		worker.bgw_flags = BGWORKER_SHMEM_ACCESS |
			BGWORKER_BACKEND_DATABASE_CONNECTION;
		worker.bgw_start_time = BgWorkerStart_RecoveryFinished;
		worker.bgw_restart_time = 1;
		strlcpy(worker.bgw_library_name, "pg_local_cache", BGW_MAXLEN);
		strlcpy(worker.bgw_function_name, "pg_local_cache_worker_main", BGW_MAXLEN);
		worker.bgw_main_arg = Int32GetDatum(i);
		worker.bgw_notify_pid = 0;
		RegisterBackgroundWorker(&worker);
	}
}

static Size
pglc_shmem_size(void)
{
	Size		size = MAXALIGN(sizeof(PgLocalCacheSharedState));

	size = add_size(size,
					hash_estimate_size(pglc_cache_entries,
									   sizeof(PgLocalCacheCacheEntry)));
	size = add_size(size,
					hash_estimate_size(PGLC_RELATION_STATES_MAX,
									   sizeof(PgLocalCacheRelationState)));
	return size;
}

static void
pglc_shmem_request(void)
{
	if (previous_shmem_request_hook)
		previous_shmem_request_hook();

	RequestAddinShmemSpace(pglc_shmem_size());
	RequestNamedLWLockTranche("pg_local_cache", 1);
}

static void
pglc_shmem_startup(void)
{
	bool		found;
	HASHCTL		control;

	if (previous_shmem_startup_hook)
		previous_shmem_startup_hook();

	LWLockAcquire(AddinShmemInitLock, LW_EXCLUSIVE);

	pglc_shared = ShmemInitStruct("pg_local_cache shared state",
								 sizeof(PgLocalCacheSharedState),
								 &found);
	if (!found)
	{
		memset(pglc_shared, 0, sizeof(PgLocalCacheSharedState));
		pglc_shared->lock = &(GetNamedLWLockTranche("pg_local_cache"))->lock;
		pg_atomic_init_u64(&pglc_shared->clock, 0);
		pg_atomic_init_u64(&pglc_shared->config_generation, 1);
		pg_atomic_init_u64(&pglc_shared->cache_hits, 0);
		pg_atomic_init_u64(&pglc_shared->cache_misses, 0);
		pg_atomic_init_u64(&pglc_shared->negative_hits, 0);
		pg_atomic_init_u64(&pglc_shared->database_reads, 0);
		pg_atomic_init_u64(&pglc_shared->database_writes, 0);
		pg_atomic_init_u64(&pglc_shared->invalidations, 0);
		pg_atomic_init_u64(&pglc_shared->evictions, 0);
		pg_atomic_init_u64(&pglc_shared->active_clients, 0);
		pg_atomic_init_u64(&pglc_shared->rejected_connections, 0);
		pg_atomic_init_u64(&pglc_shared->authentication_failures, 0);
		pg_atomic_init_u64(&pglc_shared->protocol_errors, 0);
		pg_atomic_init_u64(&pglc_shared->output_backpressure_events, 0);
		pg_atomic_init_u64(&pglc_shared->slow_client_drops, 0);
		pg_atomic_init_u64(&pglc_shared->worker_starts, 0);
	}

	memset(&control, 0, sizeof(control));
	control.keysize = sizeof(PgLocalCacheCacheKey);
	control.entrysize = sizeof(PgLocalCacheCacheEntry);
	pglc_cache_hash = ShmemInitHash("pg_local_cache cache",
								   pglc_cache_entries,
								   pglc_cache_entries,
								   &control,
								   HASH_ELEM | HASH_BLOBS);

	memset(&control, 0, sizeof(control));
	control.keysize = sizeof(PgLocalCacheRelationKey);
	control.entrysize = sizeof(PgLocalCacheRelationState);
	pglc_relation_hash = ShmemInitHash("pg_local_cache relation state",
									  PGLC_RELATION_STATES_MAX,
									  PGLC_RELATION_STATES_MAX,
									  &control,
									  HASH_ELEM | HASH_BLOBS);

	LWLockRelease(AddinShmemInitLock);
}

void
pglc_require_preload(void)
{
	if (!pglc_was_preloaded || pglc_shared == NULL ||
		pglc_cache_hash == NULL || pglc_relation_hash == NULL)
		ereport(ERROR,
				(errcode(ERRCODE_OBJECT_NOT_IN_PREREQUISITE_STATE),
				 errmsg("pg_local_cache must be loaded through shared_preload_libraries")));
}

uint64
pglc_config_generation(void)
{
	pglc_require_preload();
	return pg_atomic_read_u64(&pglc_shared->config_generation);
}

static void
make_cache_key(PgLocalCacheCacheKey *result, Oid database_oid,
			   const char *nspace, const char *key)
{
	memset(result, 0, sizeof(*result));
	result->database_oid = database_oid;
	strlcpy(result->nspace, nspace, sizeof(result->nspace));
	strlcpy(result->key, key, sizeof(result->key));
}

static void
make_relation_key(PgLocalCacheRelationKey *result, Oid database_oid,
				  const char *nspace)
{
	memset(result, 0, sizeof(*result));
	result->database_oid = database_oid;
	strlcpy(result->nspace, nspace, sizeof(result->nspace));
}

static PgLocalCacheRelationState *
get_relation_state(Oid database_oid, Oid relation_oid,
				   const char *nspace, bool create)
{
	PgLocalCacheRelationKey key;
	PgLocalCacheRelationState *state;
	bool		found;

	make_relation_key(&key, database_oid, nspace);
	state = hash_search(pglc_relation_hash, &key,
						create ? HASH_ENTER_NULL : HASH_FIND,
						&found);
	if (state != NULL && !found)
	{
		PgLocalCacheRelationKey saved_key = state->key;

		memset(state, 0, sizeof(*state));
		state->key = saved_key;
		state->relation_oid = relation_oid;
		/*
		 * Seed recycled namespace state from the monotonically increasing
		 * transaction generation.  Otherwise removing and later recreating a
		 * namespace could make an old cache entry with relation_version == 0
		 * current again.
		 */
		state->version = pglc_shared->global_version;
	}
	else if (state != NULL && create && OidIsValid(relation_oid) &&
			 state->relation_oid != relation_oid)
	{
		/*
		 * A namespace can be remapped while an older worker still holds its
		 * previous mapping.  Never silently retag version state: force every
		 * entry for either relation to miss.
		 */
		state->version++;
		state->relation_oid = relation_oid;
	}
	return state;
}

static bool
cache_entry_is_current_locked(PgLocalCacheCacheEntry *entry,
							  PgLocalCacheRelationState *relation_state)
{
	return entry->valid &&
			relation_state != NULL &&
			entry->relation_oid == relation_state->relation_oid &&
			entry->global_epoch == pglc_shared->global_epoch &&
			entry->relation_version == relation_state->version;
}

static bool
evict_one_cache_entry(void)
{
	HASH_SEQ_STATUS sequence;
	PgLocalCacheCacheEntry *entry;
	PgLocalCacheCacheKey victim;
	uint64		oldest = PG_UINT64_MAX;
	int			sampled = 0;
	bool		have_victim = false;

	hash_seq_init(&sequence, pglc_cache_hash);
	while ((entry = hash_seq_search(&sequence)) != NULL)
	{
		PgLocalCacheRelationState *relation_state;
		uint64		last_access;

		if (++sampled > PGLC_EVICTION_SAMPLE)
		{
			hash_seq_term(&sequence);
			break;
		}
		if (entry->dirty_writers != 0)
			continue;

		relation_state = get_relation_state(entry->key.database_oid,
											entry->relation_oid,
											entry->key.nspace,
											false);
		if (!cache_entry_is_current_locked(entry, relation_state))
		{
			victim = entry->key;
			have_victim = true;
			hash_seq_term(&sequence);
			break;
		}

		last_access = pg_atomic_read_u64(&entry->last_access);
		if (last_access <= oldest)
		{
			oldest = last_access;
			victim = entry->key;
			have_victim = true;
		}
	}

	if (!have_victim)
		return false;

	(void) hash_search(pglc_cache_hash, &victim, HASH_REMOVE, NULL);
	pg_atomic_fetch_add_u64(&pglc_shared->evictions, 1);
	return true;
}

static PgLocalCacheCacheEntry *
get_cache_entry(Oid database_oid, Oid relation_oid,
				const char *nspace, const char *key, bool create)
{
	PgLocalCacheCacheKey cache_key;
	PgLocalCacheCacheEntry *entry;
	bool		found;

	make_cache_key(&cache_key, database_oid, nspace, key);
	entry = hash_search(pglc_cache_hash, &cache_key,
						create ? HASH_ENTER_NULL : HASH_FIND,
						&found);
	if (entry == NULL && create && evict_one_cache_entry())
		entry = hash_search(pglc_cache_hash, &cache_key,
							HASH_ENTER_NULL, &found);

	if (entry != NULL && !found)
	{
		PgLocalCacheCacheKey saved_key = entry->key;

		memset(entry, 0, sizeof(*entry));
		entry->key = saved_key;
		entry->relation_oid = relation_oid;
		pg_atomic_init_u64(&entry->last_access, 0);
	}
	else if (entry != NULL && create && OidIsValid(relation_oid) &&
			 entry->relation_oid != relation_oid)
	{
		/*
		 * Retagging a valid entry would let a value read from the old
		 * relation become a hit for the new relation.
		 */
		entry->valid = false;
		entry->version++;
		entry->relation_oid = relation_oid;
	}
	return entry;
}

static uint64
invalidate_namespace_locked(Oid database_oid, const char *nspace)
{
	HASH_SEQ_STATUS sequence;
	PgLocalCacheCacheEntry *entry;
	PgLocalCacheRelationState *relation_state;
	uint64		count = 0;

	relation_state = get_relation_state(database_oid, InvalidOid,
									   nspace, false);
	if (relation_state == NULL)
		return 0;

	hash_seq_init(&sequence, pglc_cache_hash);
	while ((entry = hash_seq_search(&sequence)) != NULL)
	{
		if (entry->key.database_oid == database_oid &&
			strncmp(entry->key.nspace, nspace, PGLC_NAMESPACE_MAX) == 0 &&
			cache_entry_is_current_locked(entry, relation_state))
			count++;
	}
	relation_state->version++;
	return count;
}

static uint64
invalidate_all_locked(void)
{
	pglc_shared->global_epoch++;
	return 1;
}

static bool
cache_lookup_locked(const PgLocalCacheMapping *mapping,
					const char *canonical_key,
					char *value, Size value_capacity, Size *value_len,
					bool *negative, PgLocalCacheReadToken *token,
					bool create, bool *complete)
{
	PgLocalCacheRelationState *relation_state;
	PgLocalCacheCacheEntry *entry;
	bool		mapping_matches;
	bool		mapping_current;
	bool		hit = false;

	relation_state = get_relation_state(MyDatabaseId, mapping->relation_oid,
										mapping->nspace, create);
	entry = get_cache_entry(MyDatabaseId, mapping->relation_oid,
							mapping->nspace, canonical_key, create);
	mapping_matches = relation_state != NULL && entry != NULL &&
		relation_state->relation_oid == mapping->relation_oid &&
		entry->relation_oid == mapping->relation_oid;
	mapping_current =
		pg_atomic_read_u64(&pglc_shared->config_generation) ==
		mapping->config_generation;
	*complete = mapping_matches;

	token->config_generation = mapping->config_generation;
	token->global_version = pglc_shared->global_version;
	token->relation_version = relation_state ? relation_state->version : 0;
	token->key_version = entry ? entry->version : 0;
	token->has_entry = entry != NULL;
	token->cacheable = mapping_matches && mapping_current &&
		pglc_shared->global_dirty_writers == 0 &&
		relation_state->dirty_writers == 0 &&
		entry->dirty_writers == 0;

	if (token->cacheable &&
		cache_entry_is_current_locked(entry, relation_state))
	{
		uint64		access_clock;

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
		access_clock =
			pg_atomic_fetch_add_u64(&pglc_shared->clock, 1) + 1;
		pg_atomic_write_u64(&entry->last_access, access_clock);
	}
	return hit;
}

bool
pglc_cache_lookup(const PgLocalCacheMapping *mapping, const char *canonical_key,
				 char *value, Size value_capacity, Size *value_len,
				 bool *negative, PgLocalCacheReadToken *token)
{
	bool		complete = false;
	bool		hit;

	pglc_require_preload();
	memset(token, 0, sizeof(*token));
	*negative = false;
	*value_len = 0;

	LWLockAcquire(pglc_shared->lock, LW_SHARED);
	hit = cache_lookup_locked(mapping, canonical_key,
							  value, value_capacity, value_len,
							  negative, token, false, &complete);
	LWLockRelease(pglc_shared->lock);

	if (!complete)
	{
		*negative = false;
		*value_len = 0;
		LWLockAcquire(pglc_shared->lock, LW_EXCLUSIVE);
		hit = cache_lookup_locked(mapping, canonical_key,
								  value, value_capacity, value_len,
								  negative, token, true, &complete);
		LWLockRelease(pglc_shared->lock);
	}

	if (hit)
	{
		pg_atomic_fetch_add_u64(&pglc_shared->cache_hits, 1);
		if (*negative)
			pg_atomic_fetch_add_u64(&pglc_shared->negative_hits, 1);
	}
	else
		pg_atomic_fetch_add_u64(&pglc_shared->cache_misses, 1);
	return hit;
}

void
pglc_cache_store(const PgLocalCacheMapping *mapping, const char *canonical_key,
				const PgLocalCacheReadToken *token, const char *value,
				Size value_len, bool negative)
{
	PgLocalCacheRelationState *relation_state;
	PgLocalCacheCacheEntry *entry;

	if (!token->cacheable || !token->has_entry || value_len > PGLC_VALUE_MAX ||
		mapping->config_generation != token->config_generation ||
		pg_atomic_read_u64(&pglc_shared->config_generation) !=
		token->config_generation)
		return;

	LWLockAcquire(pglc_shared->lock, LW_EXCLUSIVE);
	relation_state = get_relation_state(MyDatabaseId, mapping->relation_oid,
										mapping->nspace, false);
	entry = get_cache_entry(MyDatabaseId, mapping->relation_oid,
							mapping->nspace, canonical_key, false);

	if (relation_state != NULL && entry != NULL &&
		pg_atomic_read_u64(&pglc_shared->config_generation) ==
		token->config_generation &&
		relation_state->relation_oid == mapping->relation_oid &&
		entry->relation_oid == mapping->relation_oid &&
		pglc_shared->global_dirty_writers == 0 &&
		relation_state->dirty_writers == 0 &&
		entry->dirty_writers == 0 &&
		pglc_shared->global_version == token->global_version &&
		relation_state->version == token->relation_version &&
		entry->version == token->key_version)
	{
		entry->negative = negative;
		entry->value_len = negative ? 0 : value_len;
		if (!negative && value_len > 0)
			memcpy(entry->value, value, value_len);
		entry->global_epoch = pglc_shared->global_epoch;
		entry->relation_version = relation_state->version;
		entry->valid = true;
		pg_atomic_write_u64(
			&entry->last_access,
			pg_atomic_fetch_add_u64(&pglc_shared->clock, 1) + 1);
	}
	LWLockRelease(pglc_shared->lock);
}

uint64
pglc_cache_invalidate_namespace(Oid database_oid, const char *nspace)
{
	uint64		count;

	pglc_require_preload();
	LWLockAcquire(pglc_shared->lock, LW_EXCLUSIVE);
	pglc_shared->global_version++;
	count = invalidate_namespace_locked(database_oid, nspace);
	LWLockRelease(pglc_shared->lock);
	pg_atomic_fetch_add_u64(&pglc_shared->invalidations, 1);
	return count;
}

void
pglc_note_database_read(void)
{
	pg_atomic_fetch_add_u64(&pglc_shared->database_reads, 1);
}

void
pglc_note_database_write(void)
{
	pg_atomic_fetch_add_u64(&pglc_shared->database_writes, 1);
}

static HTAB *
get_local_dirty_hash(void)
{
	HASHCTL		control;

	if (local_dirty_hash != NULL)
		return local_dirty_hash;

	memset(&control, 0, sizeof(control));
	control.keysize = sizeof(PgLocalCacheLocalDirtyKey);
	control.entrysize = sizeof(PgLocalCacheLocalDirtyEntry);
	control.hcxt = TopTransactionContext;
	local_dirty_hash = hash_create("pg_local_cache transaction dirty keys",
								   64,
								   &control,
								   HASH_ELEM | HASH_BLOBS | HASH_CONTEXT);
	return local_dirty_hash;
}

static PgLocalCacheLocalDirtyEntry *
collect_dirty(PgLocalCacheDirtyKind kind, Oid database_oid, Oid relation_oid,
			  const char *nspace, const char *key)
{
	PgLocalCacheLocalDirtyKey dirty_key;
	PgLocalCacheLocalDirtyEntry *entry;
	bool		found;

	pglc_require_preload();
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
pglc_collect_key(Oid database_oid, Oid relation_oid,
				const char *nspace, const char *key)
{
	PgLocalCacheLocalDirtyKey relation_key;
	HTAB	   *dirty = get_local_dirty_hash();

	memset(&relation_key, 0, sizeof(relation_key));
	relation_key.kind = (uint8) PGLC_DIRTY_RELATION;
	relation_key.database_oid = database_oid;
	strlcpy(relation_key.nspace, nspace, sizeof(relation_key.nspace));
	if (hash_search(dirty, &relation_key, HASH_FIND, NULL) != NULL)
		return;

	relation_key.kind = (uint8) PGLC_DIRTY_FORGET_RELATION;
	if (hash_search(dirty, &relation_key, HASH_FIND, NULL) != NULL)
		return;

	if (hash_get_num_entries(dirty) >= pglc_max_dirty_keys)
	{
		pglc_collect_relation(database_oid, relation_oid, nspace);
		return;
	}
	(void) collect_dirty(PGLC_DIRTY_KEY, database_oid, relation_oid,
						 nspace, key);
}

static void
pglc_collect_relation(Oid database_oid, Oid relation_oid,
					 const char *nspace)
{
	(void) collect_dirty(PGLC_DIRTY_RELATION, database_oid, relation_oid,
						 nspace, NULL);
}

static void
pglc_collect_forget_relation(Oid database_oid, Oid relation_oid,
							 const char *nspace)
{
	(void) collect_dirty(PGLC_DIRTY_FORGET_RELATION,
						 database_oid, relation_oid, nspace, NULL);
}

static void
pglc_collect_global(bool bump_config)
{
	(void) collect_dirty(PGLC_DIRTY_GLOBAL, MyDatabaseId, InvalidOid,
						 NULL, NULL);
	if (bump_config)
		local_bump_config = true;
}

static bool
local_has_global_dirty(void)
{
	HASH_SEQ_STATUS sequence;
	PgLocalCacheLocalDirtyEntry *entry;

	hash_seq_init(&sequence, local_dirty_hash);
	while ((entry = hash_seq_search(&sequence)) != NULL)
	{
		if (entry->key.kind == PGLC_DIRTY_GLOBAL)
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
	PgLocalCacheLocalDirtyEntry *local;
	bool		success = true;

	hash_seq_init(&sequence, local_dirty_hash);
	while ((local = hash_seq_search(&sequence)) != NULL)
	{
		if (local->key.kind == PGLC_DIRTY_KEY)
		{
			PgLocalCacheCacheEntry *entry;

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
		else if (local->key.kind == PGLC_DIRTY_RELATION ||
				 local->key.kind == PGLC_DIRTY_FORGET_RELATION)
		{
			PgLocalCacheRelationState *state;

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
			if (local->key.kind == PGLC_DIRTY_KEY)
			{
				PgLocalCacheCacheEntry *entry;

				entry = get_cache_entry(local->key.database_oid,
										local->relation_oid,
										local->key.nspace,
										local->key.key,
										false);
				Assert(entry != NULL && entry->dirty_writers > 0);
				if (entry != NULL && entry->dirty_writers > 0)
					entry->dirty_writers--;
			}
			else if (local->key.kind == PGLC_DIRTY_RELATION ||
					 local->key.kind == PGLC_DIRTY_FORGET_RELATION)
			{
				PgLocalCacheRelationState *state;

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
pglc_publish_dirty(void)
{
	HASH_SEQ_STATUS sequence;
	PgLocalCacheLocalDirtyEntry *local;
	uint64		invalidated = 0;

	if (local_dirty_hash == NULL || local_dirty_published)
		return;

	LWLockAcquire(pglc_shared->lock, LW_EXCLUSIVE);
	pglc_shared->global_version++;

	if (local_has_global_dirty() || !precreate_shared_entries_locked())
	{
		pglc_shared->global_dirty_writers++;
		invalidated += invalidate_all_locked();
		local_global_fallback = true;
	}
	else
	{
		hash_seq_init(&sequence, local_dirty_hash);
		while ((local = hash_seq_search(&sequence)) != NULL)
		{
			if (local->key.kind == PGLC_DIRTY_KEY)
			{
				PgLocalCacheCacheEntry *entry;

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
			else if (local->key.kind == PGLC_DIRTY_RELATION ||
					 local->key.kind == PGLC_DIRTY_FORGET_RELATION)
			{
				PgLocalCacheRelationState *state;

				state = get_relation_state(local->key.database_oid,
										   local->relation_oid,
										   local->key.nspace,
										   false);
				Assert(state != NULL);
				state->version++;
				invalidated++;
			}
		}
	}

	local_dirty_published = true;
	LWLockRelease(pglc_shared->lock);
	pg_atomic_fetch_add_u64(&pglc_shared->invalidations, invalidated);
}

static void
forget_relation_states_locked(bool committed)
{
	HASH_SEQ_STATUS sequence;
	PgLocalCacheLocalDirtyEntry *local;

	if (!committed)
		return;

	hash_seq_init(&sequence, local_dirty_hash);
	while ((local = hash_seq_search(&sequence)) != NULL)
	{
		PgLocalCacheRelationKey relation_key;
		PgLocalCacheRelationState *state;

		if (local->key.kind != PGLC_DIRTY_FORGET_RELATION)
			continue;
		make_relation_key(&relation_key, local->key.database_oid,
						  local->key.nspace);
		state = hash_search(pglc_relation_hash, &relation_key,
							HASH_FIND, NULL);
		if (state != NULL)
		{
			state->pending_forget = true;
			if (state->dirty_writers == 0)
				(void) hash_search(pglc_relation_hash, &relation_key,
								   HASH_REMOVE, NULL);
		}
	}
}

static void
pglc_finish_dirty(bool committed)
{
	HASH_SEQ_STATUS sequence;
	PgLocalCacheLocalDirtyEntry *local;
	bool		bump_config_after_unlock = committed && local_bump_config;

	if (local_dirty_hash == NULL)
		return;

	if (local_dirty_published)
	{
		LWLockAcquire(pglc_shared->lock, LW_EXCLUSIVE);
		if (local_global_fallback)
		{
			Assert(pglc_shared->global_dirty_writers > 0);
			pglc_shared->global_dirty_writers--;
		}
		else
		{
			hash_seq_init(&sequence, local_dirty_hash);
			while ((local = hash_seq_search(&sequence)) != NULL)
			{
				if (local->key.kind == PGLC_DIRTY_KEY)
				{
					PgLocalCacheCacheEntry *entry;

					entry = get_cache_entry(local->key.database_oid,
											local->relation_oid,
											local->key.nspace,
											local->key.key,
											false);
					if (entry != NULL)
					{
						entry->valid = false;
						Assert(entry->dirty_writers > 0);
						entry->dirty_writers--;
					}
				}
				else if (local->key.kind == PGLC_DIRTY_RELATION ||
						 local->key.kind == PGLC_DIRTY_FORGET_RELATION)
				{
					PgLocalCacheRelationState *state;

					state = get_relation_state(local->key.database_oid,
											   local->relation_oid,
											   local->key.nspace,
											   false);
						if (state != NULL)
						{
							PgLocalCacheRelationKey relation_key;

							Assert(state->dirty_writers > 0);
							state->dirty_writers--;
							if (state->dirty_writers == 0 &&
								state->pending_forget)
							{
								make_relation_key(
									&relation_key,
									local->key.database_oid,
									local->key.nspace);
								(void) hash_search(
									pglc_relation_hash,
									&relation_key,
									HASH_REMOVE, NULL);
							}
						}
				}
			}
		}
		forget_relation_states_locked(committed);
		if (bump_config_after_unlock)
		{
			pg_atomic_fetch_add_u64(&pglc_shared->config_generation, 1);
			bump_config_after_unlock = false;
		}
		LWLockRelease(pglc_shared->lock);
	}

	if (bump_config_after_unlock)
		pg_atomic_fetch_add_u64(&pglc_shared->config_generation, 1);

	local_dirty_hash = NULL;
	local_dirty_published = false;
	local_global_fallback = false;
	local_bump_config = false;
}

static void
pglc_xact_callback(XactEvent event, void *arg)
{
	switch (event)
	{
		case XACT_EVENT_PRE_COMMIT:
		case XACT_EVENT_PARALLEL_PRE_COMMIT:
			pglc_publish_dirty();
			break;
		case XACT_EVENT_COMMIT:
		case XACT_EVENT_PARALLEL_COMMIT:
			pglc_finish_dirty(true);
			break;
		case XACT_EVENT_ABORT:
		case XACT_EVENT_PARALLEL_ABORT:
			pglc_finish_dirty(false);
			break;
		case XACT_EVENT_PRE_PREPARE:
			if (local_dirty_hash != NULL)
				ereport(ERROR,
						(errcode(ERRCODE_FEATURE_NOT_SUPPORTED),
						 errmsg("PREPARE TRANSACTION is not supported after modifying a pg_local_cache mapping")));
			break;
		default:
			break;
	}
}

static void
pglc_backend_exit(int code, Datum arg)
{
	if (local_dirty_hash != NULL && pglc_shared != NULL)
		pglc_finish_dirty(false);
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
				 errmsg("pg_local_cache key column \"%s\" no longer exists",
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
		pglc_collect_relation(MyDatabaseId,
							 RelationGetRelid(trigger_data->tg_relation),
							 nspace);
		return;
	}

	if (strlen(key) >= PGLC_KEY_MAX)
		pglc_collect_relation(MyDatabaseId,
							 RelationGetRelid(trigger_data->tg_relation),
							 nspace);
	else
		pglc_collect_key(MyDatabaseId,
						RelationGetRelid(trigger_data->tg_relation),
						nspace, key);
}

Datum
pg_local_cache_row_invalidate(PG_FUNCTION_ARGS)
{
	TriggerData *trigger_data;
	const char *nspace;
	const char *column_name;

	if (!CALLED_AS_TRIGGER(fcinfo))
		ereport(ERROR,
				(errcode(ERRCODE_E_R_I_E_TRIGGER_PROTOCOL_VIOLATED),
				 errmsg("pg_local_cache row invalidator must be called as a trigger")));

	trigger_data = (TriggerData *) fcinfo->context;
	if (!TRIGGER_FIRED_AFTER(trigger_data->tg_event) ||
		!TRIGGER_FIRED_FOR_ROW(trigger_data->tg_event) ||
		trigger_data->tg_trigger->tgnargs != 2)
		ereport(ERROR,
				(errcode(ERRCODE_E_R_I_E_TRIGGER_PROTOCOL_VIOLATED),
				 errmsg("invalid pg_local_cache row trigger definition")));

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
pg_local_cache_truncate_invalidate(PG_FUNCTION_ARGS)
{
	TriggerData *trigger_data;
	const char *nspace;

	if (!CALLED_AS_TRIGGER(fcinfo))
		ereport(ERROR,
				(errcode(ERRCODE_E_R_I_E_TRIGGER_PROTOCOL_VIOLATED),
				 errmsg("pg_local_cache truncate invalidator must be called as a trigger")));

	trigger_data = (TriggerData *) fcinfo->context;
	if (!TRIGGER_FIRED_AFTER(trigger_data->tg_event) ||
		!TRIGGER_FIRED_FOR_STATEMENT(trigger_data->tg_event) ||
		!TRIGGER_FIRED_BY_TRUNCATE(trigger_data->tg_event) ||
		trigger_data->tg_trigger->tgnargs != 1)
		ereport(ERROR,
				(errcode(ERRCODE_E_R_I_E_TRIGGER_PROTOCOL_VIOLATED),
				 errmsg("invalid pg_local_cache truncate trigger definition")));

	nspace = trigger_data->tg_trigger->tgargs[0];
	pglc_collect_relation(MyDatabaseId,
						 RelationGetRelid(trigger_data->tg_relation),
						 nspace);
	PG_RETURN_POINTER(NULL);
}

Datum
pg_local_cache_reload(PG_FUNCTION_ARGS)
{
	if (!superuser())
		ereport(ERROR,
				(errcode(ERRCODE_INSUFFICIENT_PRIVILEGE),
				 errmsg("must be superuser to reload pg_local_cache mappings")));
	pglc_collect_global(true);
	PG_RETURN_VOID();
}

Datum
pg_local_cache_forget(PG_FUNCTION_ARGS)
{
	text	   *namespace_text = PG_GETARG_TEXT_PP(0);
	char	   *nspace = text_to_cstring(namespace_text);
	Oid			relation_oid = PG_GETARG_OID(1);

	if (!superuser())
		ereport(ERROR,
				(errcode(ERRCODE_INSUFFICIENT_PRIVILEGE),
				 errmsg("must be superuser to unregister pg_local_cache mappings")));
	if (strlen(nspace) >= PGLC_NAMESPACE_MAX)
		ereport(ERROR,
				(errcode(ERRCODE_NAME_TOO_LONG),
				 errmsg("pg_local_cache namespace is too long")));

	pglc_collect_forget_relation(MyDatabaseId, relation_oid, nspace);
	PG_RETURN_VOID();
}

static uint64
count_namespace_entries(Oid database_oid, const char *nspace)
{
	HASH_SEQ_STATUS sequence;
	PgLocalCacheCacheEntry *entry;
	PgLocalCacheRelationState *relation_state;
	uint64		count = 0;

	LWLockAcquire(pglc_shared->lock, LW_SHARED);
	relation_state = get_relation_state(database_oid, InvalidOid,
									   nspace, false);
	hash_seq_init(&sequence, pglc_cache_hash);
	while ((entry = hash_seq_search(&sequence)) != NULL)
	{
		if (relation_state != NULL &&
			entry->key.database_oid == database_oid &&
			strncmp(entry->key.nspace, nspace, PGLC_NAMESPACE_MAX) == 0 &&
			cache_entry_is_current_locked(entry, relation_state))
			count++;
	}
	LWLockRelease(pglc_shared->lock);
	return count;
}

static bool
pglc_mapping_exists(const char *nspace)
{
	Oid			argument_types[1] = {TEXTOID};
	Datum		arguments[1];
	int			result;
	bool		exists;

	arguments[0] = CStringGetTextDatum(nspace);
	if (SPI_connect() != SPI_OK_CONNECT)
		elog(ERROR, "pg_local_cache could not connect to SPI");
	result = SPI_execute_with_args(
		"SELECT 1 FROM local_cache.mapping WHERE namespace = $1",
		1, argument_types, arguments, NULL, true, 1);
	if (result != SPI_OK_SELECT)
		elog(ERROR, "pg_local_cache could not validate a namespace");
	exists = SPI_processed == 1;
	if (SPI_finish() != SPI_OK_FINISH)
		elog(ERROR, "pg_local_cache could not finish SPI");
	return exists;
}

Datum
pg_local_cache_invalidate(PG_FUNCTION_ARGS)
{
	text	   *namespace_text = PG_GETARG_TEXT_PP(0);
	char	   *nspace = text_to_cstring(namespace_text);
	uint64		count;

	if (!superuser())
		ereport(ERROR,
				(errcode(ERRCODE_INSUFFICIENT_PRIVILEGE),
				 errmsg("must be superuser to invalidate pg_local_cache")));
	if (strlen(nspace) >= PGLC_NAMESPACE_MAX)
		ereport(ERROR,
				(errcode(ERRCODE_NAME_TOO_LONG),
				 errmsg("pg_local_cache namespace is too long")));

	pglc_require_preload();
	if (!pglc_mapping_exists(nspace))
		ereport(ERROR,
				(errcode(ERRCODE_UNDEFINED_OBJECT),
				 errmsg("unknown pg_local_cache namespace \"%s\"", nspace)));
	count = count_namespace_entries(MyDatabaseId, nspace);
	pglc_collect_relation(MyDatabaseId, InvalidOid, nspace);
	PG_RETURN_INT64((int64) count);
}

char *
pglc_stats_json(void)
{
	HASH_SEQ_STATUS sequence;
	PgLocalCacheCacheEntry *entry;
	uint64		positive = 0;
	uint64		negative = 0;
	uint64		dirty = 0;
	uint64		dirty_relations = 0;
	uint64		relation_states = 0;
	uint64		pending_forget = 0;
	uint64		total;
	uint64		cache_hits;
	uint64		cache_misses;
	uint64		negative_hits;
	uint64		database_reads;
	uint64		database_writes;
	uint64		invalidations;
	uint64		evictions;
	uint64		active_clients;
	uint64		rejected_connections;
	uint64		authentication_failures;
	uint64		protocol_errors;
	uint64		output_backpressure_events;
	uint64		slow_client_drops;
	uint64		worker_starts;
	HASH_SEQ_STATUS relation_sequence;
	PgLocalCacheRelationState *relation_state;
	uint32		global_dirty_writers;

	pglc_require_preload();
	LWLockAcquire(pglc_shared->lock, LW_SHARED);
	hash_seq_init(&sequence, pglc_cache_hash);
	while ((entry = hash_seq_search(&sequence)) != NULL)
	{
		relation_state = get_relation_state(entry->key.database_oid,
										   entry->relation_oid,
										   entry->key.nspace,
										   false);
		if (cache_entry_is_current_locked(entry, relation_state) &&
			entry->negative)
			negative++;
		else if (cache_entry_is_current_locked(entry, relation_state))
			positive++;
		if (entry->dirty_writers > 0)
			dirty++;
	}
	hash_seq_init(&relation_sequence, pglc_relation_hash);
	while ((relation_state = hash_seq_search(&relation_sequence)) != NULL)
	{
		relation_states++;
		if (relation_state->dirty_writers > 0)
			dirty_relations++;
		if (relation_state->pending_forget)
			pending_forget++;
	}
	global_dirty_writers = pglc_shared->global_dirty_writers;
	total = hash_get_num_entries(pglc_cache_hash);
	LWLockRelease(pglc_shared->lock);
	cache_hits = pg_atomic_read_u64(&pglc_shared->cache_hits);
	cache_misses = pg_atomic_read_u64(&pglc_shared->cache_misses);
	negative_hits = pg_atomic_read_u64(&pglc_shared->negative_hits);
	database_reads = pg_atomic_read_u64(&pglc_shared->database_reads);
	database_writes = pg_atomic_read_u64(&pglc_shared->database_writes);
	invalidations = pg_atomic_read_u64(&pglc_shared->invalidations);
	evictions = pg_atomic_read_u64(&pglc_shared->evictions);
	active_clients = pg_atomic_read_u64(&pglc_shared->active_clients);
	rejected_connections =
		pg_atomic_read_u64(&pglc_shared->rejected_connections);
	authentication_failures =
		pg_atomic_read_u64(&pglc_shared->authentication_failures);
	protocol_errors = pg_atomic_read_u64(&pglc_shared->protocol_errors);
	output_backpressure_events =
		pg_atomic_read_u64(&pglc_shared->output_backpressure_events);
	slow_client_drops =
		pg_atomic_read_u64(&pglc_shared->slow_client_drops);
	worker_starts = pg_atomic_read_u64(&pglc_shared->worker_starts);

	return psprintf(
		"{\"entries\":" UINT64_FORMAT
		",\"positive_entries\":" UINT64_FORMAT
		",\"negative_entries\":" UINT64_FORMAT
		",\"dirty_entries\":" UINT64_FORMAT
		",\"dirty_relations\":" UINT64_FORMAT
		",\"relation_states\":" UINT64_FORMAT
		",\"pending_forget\":" UINT64_FORMAT
		",\"global_dirty_writers\":%u"
		",\"store_size\":" UINT64_FORMAT
		",\"cache_hits\":" UINT64_FORMAT
		",\"cache_misses\":" UINT64_FORMAT
		",\"negative_hits\":" UINT64_FORMAT
		",\"database_reads\":" UINT64_FORMAT
		",\"database_writes\":" UINT64_FORMAT
		",\"invalidations\":" UINT64_FORMAT
		",\"evictions\":" UINT64_FORMAT
		",\"active_clients\":" UINT64_FORMAT
		",\"rejected_connections\":" UINT64_FORMAT
		",\"authentication_failures\":" UINT64_FORMAT
		",\"protocol_errors\":" UINT64_FORMAT
		",\"output_backpressure_events\":" UINT64_FORMAT
		",\"slow_client_drops\":" UINT64_FORMAT
		",\"worker_starts\":" UINT64_FORMAT
		",\"cache_hit\":" UINT64_FORMAT
		",\"cache_miss\":" UINT64_FORMAT
		",\"cache_evict\":" UINT64_FORMAT
		",\"sql_gets\":" UINT64_FORMAT "}",
		total, positive, negative, dirty, dirty_relations,
		relation_states, pending_forget,
		global_dirty_writers, positive + negative,
		cache_hits, cache_misses, negative_hits,
		database_reads, database_writes, invalidations, evictions,
		active_clients, rejected_connections, authentication_failures,
		protocol_errors, output_backpressure_events, slow_client_drops,
		worker_starts,
		cache_hits, cache_misses, evictions, database_reads);
}

Datum
pg_local_cache_stats(PG_FUNCTION_ARGS)
{
	char	   *json = pglc_stats_json();

	PG_RETURN_DATUM(DirectFunctionCall1(jsonb_in, CStringGetDatum(json)));
}
