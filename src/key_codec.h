#ifndef PGLC_KEY_CODEC_H
#define PGLC_KEY_CODEC_H

#include "postgres.h"

#include "fmgr.h"

/*
 * Encode each key component as <decimal byte length>:<bytes>; so composite
 * primary keys cannot alias through separators contained in type output.
 */
extern bool pglc_canonical_key(const Datum *values,
							   const bool *nulls,
							   int key_count,
							   FmgrInfo *output_functions,
							   char *destination,
							   Size destination_capacity,
							   Size *key_len);

#endif
