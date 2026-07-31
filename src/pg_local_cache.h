#ifndef PG_LOCAL_CACHE_H
#define PG_LOCAL_CACHE_H

#include "postgres.h"

#include "executor/spi.h"
#include "fmgr.h"
#include "port/atomics.h"
#include "storage/lwlock.h"
#include "utils/hsearch.h"

#include "resp_limits.h"

#define PGLC_NAMESPACE_MAX 64
#define PGLC_KEY_MAX 256
#define PGLC_VALUE_MAX 8192
#define PGLC_MAX_MAPPINGS 128
#define PGLC_RELATION_STATES_MAX 1024
#define PGLC_MAX_CLIENTS_PER_WORKER 128
#define PGLC_RESPONSE_MAX (PGLC_VALUE_MAX + 1024)
#define PGLC_AUTH_TOKEN_MAX 1024
#define PGLC_MAX_AUTH_FAILURES 5
#define PGLC_EVICTION_SAMPLE 64

typedef struct PgLocalCacheCacheKey
{
	Oid			database_oid;
	char		nspace[PGLC_NAMESPACE_MAX];
	char		key[PGLC_KEY_MAX];
} PgLocalCacheCacheKey;

typedef struct PgLocalCacheCacheEntry
{
	PgLocalCacheCacheKey key;
	Oid			relation_oid;
	uint64		global_epoch;
	uint64		relation_version;
	uint64		version;
	pg_atomic_uint64 last_access;
	uint32		dirty_writers;
	uint32		value_len;
	bool		valid;
	bool		negative;
	char		value[PGLC_VALUE_MAX];
} PgLocalCacheCacheEntry;

typedef struct PgLocalCacheRelationKey
{
	Oid			database_oid;
	char		nspace[PGLC_NAMESPACE_MAX];
} PgLocalCacheRelationKey;

typedef struct PgLocalCacheRelationState
{
	PgLocalCacheRelationKey key;
	Oid			relation_oid;
	uint64		version;
	uint32		dirty_writers;
	bool		pending_forget;
} PgLocalCacheRelationState;

typedef struct PgLocalCacheSharedState
{
	LWLock	   *lock;
	pg_atomic_uint64 clock;
	uint64		global_version;
	uint64		global_epoch;
	uint32		global_dirty_writers;
	pg_atomic_uint64 config_generation;
	pg_atomic_uint64 cache_hits;
	pg_atomic_uint64 cache_misses;
	pg_atomic_uint64 negative_hits;
	pg_atomic_uint64 database_reads;
	pg_atomic_uint64 database_writes;
	pg_atomic_uint64 invalidations;
	pg_atomic_uint64 evictions;
	pg_atomic_uint64 active_clients;
	pg_atomic_uint64 rejected_connections;
	pg_atomic_uint64 authentication_failures;
	pg_atomic_uint64 protocol_errors;
	pg_atomic_uint64 slow_client_drops;
	pg_atomic_uint64 worker_starts;
} PgLocalCacheSharedState;

typedef struct PgLocalCacheReadToken
{
	uint64		config_generation;
	uint64		global_version;
	uint64		relation_version;
	uint64		key_version;
	bool		cacheable;
	bool		has_entry;
} PgLocalCacheReadToken;

typedef struct PgLocalCacheMapping
{
	char		nspace[PGLC_NAMESPACE_MAX];
	char		schema_name[NAMEDATALEN];
	char		relation_name[NAMEDATALEN];
	char		key_column[NAMEDATALEN];
	char		value_column[NAMEDATALEN];
	Oid			relation_oid;
	Oid			key_type;
	Oid			value_type;
	Oid			key_ioparam;
	Oid			value_ioparam;
	int32		key_typmod;
	int32		value_typmod;
	uint64		config_generation;
	bool		writable;
	FmgrInfo	key_input;
	FmgrInfo	key_output;
	FmgrInfo	value_input;
	SPIPlanPtr	get_plan;
	SPIPlanPtr	set_plan;
	SPIPlanPtr	delete_plan;
} PgLocalCacheMapping;

extern int	pglc_port;
extern int	pglc_worker_count;
extern int	pglc_cache_entries;
extern int	pglc_idle_timeout_ms;
extern int	pglc_statement_timeout_ms;
extern int	pglc_lock_timeout_ms;
extern int	pglc_max_pipeline_commands;
extern int	pglc_max_dirty_keys;
extern char *pglc_bind_address;
extern char *pglc_database;
extern char *pglc_role;
extern char *pglc_auth_token;
extern char *pglc_auth_token_file;
extern bool pglc_allow_superuser;

extern PgLocalCacheSharedState *pglc_shared;
extern HTAB *pglc_cache_hash;
extern HTAB *pglc_relation_hash;

extern void pglc_require_preload(void);
extern uint64 pglc_config_generation(void);
extern bool pglc_cache_lookup(const PgLocalCacheMapping *mapping,
							 const char *canonical_key,
							 char *value,
							 Size value_capacity,
							 Size *value_len,
							 bool *negative,
							 PgLocalCacheReadToken *token);
extern void pglc_cache_store(const PgLocalCacheMapping *mapping,
							const char *canonical_key,
							const PgLocalCacheReadToken *token,
							const char *value,
							Size value_len,
							bool negative);
extern uint64 pglc_cache_invalidate_namespace(Oid database_oid,
											 const char *nspace);
extern char *pglc_stats_json(void);
extern void pglc_note_database_read(void);
extern void pglc_note_database_write(void);
extern void pg_local_cache_worker_main(Datum main_arg);

#endif
