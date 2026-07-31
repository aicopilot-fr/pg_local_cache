#ifndef PG_LOCAL_CACHE_RESP_LIMITS_H
#define PG_LOCAL_CACHE_RESP_LIMITS_H

/*
 * Wire limits shared by the PostgreSQL build and the standalone source tests.
 * Keep this header free of PostgreSQL dependencies.
 */
#define PGLC_REQUEST_MAX 65536
#define PGLC_RESP_MAX_ARGS 16

#endif
