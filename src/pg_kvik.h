#ifndef PG_KVIK_H
#define PG_KVIK_H

#include "postgres.h"

#include "executor/spi.h"
#include "fmgr.h"
#include "port/atomics.h"
#include "storage/lwlock.h"
#include "utils/hsearch.h"

#define PGK_NAMESPACE_MAX 64
#define PGK_KEY_MAX 256
#define PGK_VALUE_MAX 8192
#define PGK_MAX_MAPPINGS 128
#define PGK_MAX_CLIENTS_PER_WORKER 128
#define PGK_REQUEST_MAX 65536
#define PGK_RESP_MAX_ARGS 16

typedef struct PgKvikCacheKey
{
	Oid			database_oid;
	char		nspace[PGK_NAMESPACE_MAX];
	char		key[PGK_KEY_MAX];
} PgKvikCacheKey;

typedef struct PgKvikCacheEntry
{
	PgKvikCacheKey key;
	Oid			relation_oid;
	uint64		version;
	uint64		last_access;
	uint32		dirty_writers;
	uint32		value_len;
	bool		valid;
	bool		negative;
	char		value[PGK_VALUE_MAX];
} PgKvikCacheEntry;

typedef struct PgKvikRelationKey
{
	Oid			database_oid;
	char		nspace[PGK_NAMESPACE_MAX];
} PgKvikRelationKey;

typedef struct PgKvikRelationState
{
	PgKvikRelationKey key;
	Oid			relation_oid;
	uint64		version;
	uint32		dirty_writers;
} PgKvikRelationState;

typedef struct PgKvikSharedState
{
	LWLock	   *lock;
	uint64		clock;
	uint64		global_version;
	uint32		global_dirty_writers;
	pg_atomic_uint64 config_generation;
	pg_atomic_uint64 cache_hits;
	pg_atomic_uint64 cache_misses;
	pg_atomic_uint64 negative_hits;
	pg_atomic_uint64 database_reads;
	pg_atomic_uint64 database_writes;
	pg_atomic_uint64 invalidations;
	pg_atomic_uint64 evictions;
} PgKvikSharedState;

typedef struct PgKvikReadToken
{
	uint64		global_version;
	uint64		relation_version;
	uint64		key_version;
	bool		cacheable;
	bool		has_entry;
} PgKvikReadToken;

typedef struct PgKvikMapping
{
	char		nspace[PGK_NAMESPACE_MAX];
	char		schema_name[NAMEDATALEN];
	char		relation_name[NAMEDATALEN];
	char		key_column[NAMEDATALEN];
	char		value_column[NAMEDATALEN];
	Oid			relation_oid;
	Oid			key_type;
	Oid			value_type;
	Oid			key_ioparam;
	Oid			value_ioparam;
	bool		writable;
	FmgrInfo	key_input;
	FmgrInfo	key_output;
	FmgrInfo	value_input;
	SPIPlanPtr	get_plan;
	SPIPlanPtr	set_plan;
	SPIPlanPtr	delete_plan;
} PgKvikMapping;

extern int	pgk_port;
extern int	pgk_worker_count;
extern int	pgk_cache_entries;
extern char *pgk_bind_address;
extern char *pgk_database;
extern char *pgk_role;
extern char *pgk_auth_token;

extern PgKvikSharedState *pgk_shared;
extern HTAB *pgk_cache_hash;
extern HTAB *pgk_relation_hash;

extern void pgk_require_preload(void);
extern uint64 pgk_config_generation(void);
extern bool pgk_cache_lookup(const PgKvikMapping *mapping,
							 const char *canonical_key,
							 char *value,
							 Size value_capacity,
							 Size *value_len,
							 bool *negative,
							 PgKvikReadToken *token);
extern void pgk_cache_store(const PgKvikMapping *mapping,
							const char *canonical_key,
							const PgKvikReadToken *token,
							const char *value,
							Size value_len,
							bool negative);
extern uint64 pgk_cache_invalidate_namespace(Oid database_oid,
											 const char *nspace);
extern char *pgk_stats_json(void);
extern void pgk_note_database_read(void);
extern void pgk_note_database_write(void);
extern void pg_kvik_worker_main(Datum main_arg);

#endif
