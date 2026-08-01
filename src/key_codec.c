#include "postgres.h"

#include "utils/fmgroids.h"
#include "utils/builtins.h"

#include "key_codec.h"
#include "pg_local_cache.h"

static int
pglc_key_length_digits(Size value, char *digits)
{
	char		reversed[3 * sizeof(Size)];
	int		count = 0;
	int		i;

	do
	{
		reversed[count++] = (char) ('0' + (value % 10));
		value /= 10;
	} while (value != 0);
	for (i = 0; i < count; i++)
		digits[i] = reversed[count - i - 1];
	return count;
}

bool
pglc_canonical_key(const Datum *values, const bool *nulls, int key_count,
				   FmgrInfo *output_functions,
					   char *destination, Size destination_capacity,
					   Size *key_len)
{
	char		encoded[PGLC_KEY_MAX];
	Size		used = 0;
	int		component;

	if (key_len != NULL)
		*key_len = 0;
	if (destination != NULL && destination_capacity > 0)
		destination[0] = '\0';
	if (key_count <= 0 || key_count > PGLC_MAX_KEY_COLUMNS ||
		output_functions == NULL || values == NULL || nulls == NULL ||
		destination == NULL || key_len == NULL || destination_capacity == 0)
		return false;

	for (component = 0; component < key_count; component++)
	{
		char	   *rendered;
		char		digits[3 * sizeof(Size)];
		Size		rendered_len;
		int			digits_len;
		Size		part_len;

		if (nulls[component])
			return false;
		rendered = OutputFunctionCall(&output_functions[component],
									  values[component]);
		rendered_len = strlen(rendered);
		/*
		 * bpchar equality ignores trailing ASCII spaces, while bpcharout can
		 * expose a different amount of typmod padding for a query expression,
		 * a stored tuple and a trigger Datum.  Strip it here so every producer
		 * of a logically equal character(n) key reaches the same cache entry.
		 */
		if (output_functions[component].fn_oid == F_BPCHAROUT)
		{
			while (rendered_len > 0 && rendered[rendered_len - 1] == ' ')
				rendered_len--;
		}
		digits_len = pglc_key_length_digits(rendered_len, digits);
		part_len = (Size) digits_len + 1 + rendered_len + 1;
		if (part_len >= PGLC_KEY_MAX || used >= PGLC_KEY_MAX - part_len)
		{
			pfree(rendered);
			return false;
		}

		memcpy(encoded + used, digits, digits_len);
		used += digits_len;
		encoded[used++] = ':';
		if (rendered_len > 0)
		{
			memcpy(encoded + used, rendered, rendered_len);
			used += rendered_len;
		}
		encoded[used++] = ';';
		pfree(rendered);
	}

	if (used >= destination_capacity)
		return false;
	memcpy(destination, encoded, used);
	destination[used] = '\0';
	*key_len = used;
	return true;
}
