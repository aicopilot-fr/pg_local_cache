#ifndef PG_KVIK_RESP_H
#define PG_KVIK_RESP_H

#include "postgres.h"

#include "pg_kvik.h"

typedef struct PgKvikRespArg
{
	const char *data;
	Size		len;
} PgKvikRespArg;

/*
 * Returns 1 for a complete request, 0 for incomplete input, and -1 for a
 * protocol error. On success, consumed is the number of bytes to remove.
 */
extern int pgk_resp_parse(const char *buffer, Size length,
						  PgKvikRespArg *args, int *argc,
						  Size *consumed, const char **error);

extern bool pgk_resp_arg_equals(const PgKvikRespArg *arg, const char *literal);
extern char *pgk_resp_simple(const char *message, Size *length);
extern char *pgk_resp_error(const char *message, Size *length);
extern char *pgk_resp_integer(int64 value, Size *length);
extern char *pgk_resp_bulk(const char *value, Size value_len, Size *length);
extern char *pgk_resp_null(Size *length);

#endif

