#include "postgres.h"

#include <arpa/inet.h>
#include <errno.h>
#include <fcntl.h>
#include <netinet/in.h>
#include <poll.h>
#include <signal.h>
#include <sys/socket.h>
#include <unistd.h>

#include "access/xact.h"
#include "catalog/pg_type_d.h"
#include "executor/spi.h"
#include "lib/stringinfo.h"
#include "mb/pg_wchar.h"
#include "miscadmin.h"
#include "postmaster/bgworker.h"
#include "storage/ipc.h"
#include "storage/latch.h"
#include "utils/builtins.h"
#include "utils/lsyscache.h"
#include "utils/memutils.h"
#include "utils/snapmgr.h"
#include "utils/timestamp.h"
#include "utils/wait_event.h"

#include "pg_kvik.h"
#include "resp.h"

typedef struct PgKvikClient
{
	int			fd;
	bool		authenticated;
	Size		used;
	char		input[PGK_REQUEST_MAX];
} PgKvikClient;

static volatile sig_atomic_t worker_should_exit = false;
static MemoryContext mapping_context = NULL;
static PgKvikMapping *worker_mappings = NULL;
static int	worker_mapping_count = 0;
static uint64 worker_mapping_generation = 0;
static TimestampTz worker_next_mapping_retry = 0;

static void worker_sigterm(SIGNAL_ARGS);
static int create_listener(void);
static void run_server(int listener);
static void close_client(PgKvikClient *client);
static bool process_client(PgKvikClient *client);
static char *execute_command(PgKvikClient *client,
							 PgKvikRespArg *args, int argc,
							 Size *response_length, bool *close_after);
static char *execute_command_inner(PgKvikClient *client,
								   PgKvikRespArg *args, int argc,
								   Size *response_length, bool *close_after);
static void maybe_reload_mappings(void);
static bool reload_mappings(void);
static PgKvikMapping *find_mapping(const char *nspace);
static bool split_wire_key(const PgKvikRespArg *wire_key,
						   char **nspace, char **raw_key,
						   char **error);
static bool canonicalize_key(PgKvikMapping *mapping, const char *raw_key,
							 Datum *key_value, char **canonical,
							 char **error);
static char *command_get(PgKvikMapping *mapping, const char *raw_key,
						 Size *response_length);
static char *command_set(PgKvikMapping *mapping, const char *raw_key,
						 const PgKvikRespArg *value_arg,
						 Size *response_length);
static char *command_delete(PgKvikMapping *mapping, const char *raw_key,
							Size *response_length);

PGDLLEXPORT void
pg_kvik_worker_main(Datum main_arg)
{
	int			listener;
	const char *role;

	pqsignal(SIGTERM, worker_sigterm);
	BackgroundWorkerUnblockSignals();

	pgk_require_preload();
	role = (pgk_role != NULL && pgk_role[0] != '\0') ? pgk_role : NULL;
	BackgroundWorkerInitializeConnection(pgk_database, role, 0);

	mapping_context = AllocSetContextCreate(TopMemoryContext,
											"pg_kvik mappings",
											ALLOCSET_DEFAULT_SIZES);
	maybe_reload_mappings();

	listener = create_listener();
	ereport(LOG,
			(errmsg("pg_kvik worker %d listening on %s:%d for database \"%s\"",
					DatumGetInt32(main_arg), pgk_bind_address, pgk_port,
					pgk_database)));
	run_server(listener);
	close(listener);
	proc_exit(0);
}

static void
worker_sigterm(SIGNAL_ARGS)
{
	int			save_errno = errno;

	worker_should_exit = true;
	SetLatch(MyLatch);
	errno = save_errno;
}

static int
create_listener(void)
{
	int			fd;
	int			enabled = 1;
	int			flags;
	struct sockaddr_in address;

	if (pgk_bind_address == NULL ||
		(strcmp(pgk_bind_address, "127.0.0.1") != 0 &&
		 strcmp(pgk_bind_address, "0.0.0.0") != 0))
		ereport(FATAL,
				(errmsg("pg_kvik.bind_address must be an IPv4 literal"),
				 errhint("The alpha release supports 127.0.0.1 or 0.0.0.0.")));

	if (strcmp(pgk_bind_address, "0.0.0.0") == 0 &&
		(pgk_auth_token == NULL || pgk_auth_token[0] == '\0'))
		ereport(FATAL,
				(errmsg("pg_kvik refuses a non-loopback listener without pg_kvik.auth_token")));

	fd = socket(AF_INET, SOCK_STREAM, 0);
	if (fd < 0)
		ereport(FATAL,
				(errcode_for_socket_access(),
				 errmsg("could not create pg_kvik listener socket: %m")));

	if (setsockopt(fd, SOL_SOCKET, SO_REUSEADDR, &enabled, sizeof(enabled)) < 0)
		ereport(FATAL,
				(errcode_for_socket_access(),
				 errmsg("could not set SO_REUSEADDR on pg_kvik socket: %m")));

	if (pgk_worker_count > 1)
	{
#ifdef SO_REUSEPORT
		if (setsockopt(fd, SOL_SOCKET, SO_REUSEPORT,
					   &enabled, sizeof(enabled)) < 0)
			ereport(FATAL,
					(errcode_for_socket_access(),
					 errmsg("could not set SO_REUSEPORT on pg_kvik socket: %m")));
#else
		ereport(FATAL,
				(errmsg("pg_kvik.workers > 1 requires SO_REUSEPORT")));
#endif
	}

	memset(&address, 0, sizeof(address));
	address.sin_family = AF_INET;
	address.sin_port = htons((uint16) pgk_port);
	if (inet_pton(AF_INET, pgk_bind_address, &address.sin_addr) != 1)
		ereport(FATAL,
				(errmsg("invalid pg_kvik.bind_address \"%s\"",
						pgk_bind_address)));

	if (bind(fd, (struct sockaddr *) &address, sizeof(address)) < 0)
		ereport(FATAL,
				(errcode_for_socket_access(),
				 errmsg("could not bind pg_kvik to %s:%d: %m",
						pgk_bind_address, pgk_port)));
	if (listen(fd, SOMAXCONN) < 0)
		ereport(FATAL,
				(errcode_for_socket_access(),
				 errmsg("could not listen on pg_kvik socket: %m")));

	flags = fcntl(fd, F_GETFL, 0);
	if (flags < 0 || fcntl(fd, F_SETFL, flags | O_NONBLOCK) < 0)
		ereport(FATAL,
				(errcode_for_socket_access(),
				 errmsg("could not make pg_kvik socket nonblocking: %m")));
	return fd;
}

static void
run_server(int listener)
{
	PgKvikClient *clients;
	struct pollfd poll_fds[PGK_MAX_CLIENTS_PER_WORKER + 1];
	int			poll_to_client[PGK_MAX_CLIENTS_PER_WORKER + 1];
	int			i;

	/*
	 * Each client owns a bounded 64 KiB request buffer.  Keep the client
	 * array out of the background worker's small process stack.
	 */
	clients = MemoryContextAllocZero(TopMemoryContext,
									 sizeof(PgKvikClient) *
									 PGK_MAX_CLIENTS_PER_WORKER);
	for (i = 0; i < PGK_MAX_CLIENTS_PER_WORKER; i++)
		clients[i].fd = -1;

	while (!worker_should_exit)
	{
		int			poll_count = 1;
		int			poll_result;
		int			latch_result;

		maybe_reload_mappings();
		poll_fds[0].fd = listener;
		poll_fds[0].events = POLLIN;
		poll_fds[0].revents = 0;
		poll_to_client[0] = -1;

		for (i = 0; i < PGK_MAX_CLIENTS_PER_WORKER; i++)
		{
			if (clients[i].fd >= 0)
			{
				poll_fds[poll_count].fd = clients[i].fd;
				poll_fds[poll_count].events = POLLIN;
				poll_fds[poll_count].revents = 0;
				poll_to_client[poll_count] = i;
				poll_count++;
			}
		}

		poll_result = poll(poll_fds, poll_count, 250);
		if (poll_result < 0 && errno != EINTR)
			ereport(LOG,
					(errcode_for_socket_access(),
					 errmsg("pg_kvik poll failed: %m")));

		latch_result = WaitLatch(MyLatch,
								 WL_LATCH_SET | WL_TIMEOUT |
								 WL_POSTMASTER_DEATH,
								 0,
								 PG_WAIT_EXTENSION);
		ResetLatch(MyLatch);
		if (latch_result & WL_POSTMASTER_DEATH)
			proc_exit(1);
		if (worker_should_exit)
			break;

		if (poll_result <= 0)
			continue;

		if (poll_fds[0].revents & POLLIN)
		{
			for (;;)
			{
				int			client_fd;
				int			slot = -1;
				int			flags;

				client_fd = accept(listener, NULL, NULL);
				if (client_fd < 0)
				{
					if (errno == EAGAIN || errno == EWOULDBLOCK)
						break;
					if (errno == EINTR)
						continue;
					ereport(LOG,
							(errcode_for_socket_access(),
							 errmsg("pg_kvik accept failed: %m")));
					break;
				}

				for (i = 0; i < PGK_MAX_CLIENTS_PER_WORKER; i++)
				{
					if (clients[i].fd < 0)
					{
						slot = i;
						break;
					}
				}
				if (slot < 0)
				{
					close(client_fd);
					continue;
				}

				flags = fcntl(client_fd, F_GETFL, 0);
				if (flags < 0 ||
					fcntl(client_fd, F_SETFL, flags | O_NONBLOCK) < 0)
				{
					close(client_fd);
					continue;
				}

				clients[slot].fd = client_fd;
				clients[slot].used = 0;
				clients[slot].authenticated =
					pgk_auth_token == NULL || pgk_auth_token[0] == '\0';
			}
		}

		for (i = 1; i < poll_count; i++)
		{
			int			client_index = poll_to_client[i];

			if (client_index < 0 || clients[client_index].fd < 0)
				continue;
			if (poll_fds[i].revents & (POLLERR | POLLHUP | POLLNVAL))
			{
				close_client(&clients[client_index]);
				continue;
			}
			if ((poll_fds[i].revents & POLLIN) &&
				!process_client(&clients[client_index]))
				close_client(&clients[client_index]);
		}
	}

	for (i = 0; i < PGK_MAX_CLIENTS_PER_WORKER; i++)
		close_client(&clients[i]);
	pfree(clients);
}

static void
close_client(PgKvikClient *client)
{
	if (client->fd >= 0)
		close(client->fd);
	client->fd = -1;
	client->used = 0;
	client->authenticated = false;
}

static bool
send_all(int fd, const char *buffer, Size length)
{
	Size		sent = 0;

	while (sent < length)
	{
		ssize_t		written = send(fd, buffer + sent, length - sent,
#ifdef MSG_NOSIGNAL
								   MSG_NOSIGNAL
#else
								   0
#endif
			);

		if (written > 0)
		{
			sent += (Size) written;
			continue;
		}
		if (written < 0 && errno == EINTR)
			continue;
		if (written < 0 && (errno == EAGAIN || errno == EWOULDBLOCK))
		{
			struct pollfd output_poll;
			int			result;

			output_poll.fd = fd;
			output_poll.events = POLLOUT;
			output_poll.revents = 0;
			result = poll(&output_poll, 1, 5000);
			if (result > 0 && (output_poll.revents & POLLOUT))
				continue;
		}
		return false;
	}
	return true;
}

static bool
process_client(PgKvikClient *client)
{
	ssize_t		received;

	if (client->used == sizeof(client->input))
		return false;

	received = recv(client->fd, client->input + client->used,
					sizeof(client->input) - client->used, 0);
	if (received == 0)
		return false;
	if (received < 0)
	{
		if (errno == EINTR || errno == EAGAIN || errno == EWOULDBLOCK)
			return true;
		return false;
	}
	client->used += (Size) received;

	while (client->used > 0)
	{
		PgKvikRespArg args[PGK_RESP_MAX_ARGS];
		int			argc;
		Size		consumed;
		const char *protocol_error;
		int			parse_result;
		MemoryContext command_context;
		MemoryContext previous_context;
		char	   *response;
		Size		response_length;
		bool		close_after = false;
		bool		sent;

		parse_result = pgk_resp_parse(client->input, client->used,
									  args, &argc, &consumed,
									  &protocol_error);
		if (parse_result == 0)
			return client->used < sizeof(client->input);
		if (parse_result < 0)
		{
			Size		error_length;
			char	   *error_response;

			error_response = pgk_resp_error(protocol_error, &error_length);
			(void) send_all(client->fd, error_response, error_length);
			pfree(error_response);
			return false;
		}

		command_context = AllocSetContextCreate(CurrentMemoryContext,
												"pg_kvik command",
												ALLOCSET_DEFAULT_SIZES);
		previous_context = MemoryContextSwitchTo(command_context);
		response = execute_command(client, args, argc,
								   &response_length, &close_after);
		sent = send_all(client->fd, response, response_length);
		MemoryContextSwitchTo(previous_context);
		MemoryContextDelete(command_context);

		if (!sent || close_after)
			return false;

		if (consumed < client->used)
			memmove(client->input, client->input + consumed,
					client->used - consumed);
		client->used -= consumed;
	}
	return true;
}

static char *
execute_command(PgKvikClient *client, PgKvikRespArg *args, int argc,
				Size *response_length, bool *close_after)
{
	MemoryContext error_context = CurrentMemoryContext;
	char	   *response = NULL;

	PG_TRY();
	{
		response = execute_command_inner(client, args, argc,
										 response_length, close_after);
	}
	PG_CATCH();
	{
		ErrorData  *error_data;
		char	   *message;

		MemoryContextSwitchTo(error_context);
		error_data = CopyErrorData();
		FlushErrorState();
		if (IsTransactionState())
			AbortCurrentTransaction();
		message = psprintf("ERR PostgreSQL: %s", error_data->message);
		response = pgk_resp_error(message, response_length);
		FreeErrorData(error_data);
	}
	PG_END_TRY();
	return response;
}

static bool
constant_time_token_equals(const PgKvikRespArg *argument)
{
	Size		expected_length = strlen(pgk_auth_token);
	Size		max_length = Max(expected_length, argument->len);
	Size		difference = expected_length ^ argument->len;
	Size		i;

	for (i = 0; i < max_length; i++)
	{
		unsigned char expected = i < expected_length ?
			(unsigned char) pgk_auth_token[i] : 0;
		unsigned char actual = i < argument->len ?
			(unsigned char) argument->data[i] : 0;

		difference |= expected ^ actual;
	}
	return difference == 0;
}

static char *
raw_response(const char *value, Size *length)
{
	char	   *response = pstrdup(value);

	*length = strlen(value);
	return response;
}

static char *
execute_command_inner(PgKvikClient *client, PgKvikRespArg *args, int argc,
					  Size *response_length, bool *close_after)
{
	char	   *nspace;
	char	   *raw_key;
	char	   *key_error;
	PgKvikMapping *mapping;

	if (argc == 0)
		return pgk_resp_error("ERR empty command", response_length);

	if (pgk_resp_arg_equals(&args[0], "AUTH"))
	{
		const PgKvikRespArg *token;
		bool		username_matches = true;

		if (argc != 2 && argc != 3)
			return pgk_resp_error("ERR wrong number of arguments for AUTH",
								  response_length);
		if (argc == 3)
		{
			const char *expected_username =
				(pgk_role != NULL && pgk_role[0] != '\0') ?
				pgk_role : "default";
			Size		expected_length = strlen(expected_username);

			username_matches =
				args[1].len == expected_length &&
				memcmp(args[1].data, expected_username, expected_length) == 0;
		}
		token = &args[argc - 1];
		if (username_matches && constant_time_token_equals(token))
		{
			client->authenticated = true;
			return pgk_resp_simple("OK", response_length);
		}
		client->authenticated = false;
		return pgk_resp_error("WRONGPASS invalid authentication token",
							  response_length);
	}

	if (!client->authenticated)
		return pgk_resp_error("NOAUTH Authentication required",
							  response_length);

	if (pgk_resp_arg_equals(&args[0], "PING"))
	{
		if (argc == 1)
			return pgk_resp_simple("PONG", response_length);
		if (argc == 2)
			return pgk_resp_bulk(args[1].data, args[1].len, response_length);
		return pgk_resp_error("ERR wrong number of arguments for PING",
							  response_length);
	}
	if (pgk_resp_arg_equals(&args[0], "ECHO"))
	{
		if (argc != 2)
			return pgk_resp_error("ERR wrong number of arguments for ECHO",
								  response_length);
		return pgk_resp_bulk(args[1].data, args[1].len, response_length);
	}
	if (pgk_resp_arg_equals(&args[0], "HELLO"))
	{
		if (argc != 2 || args[1].len != 1 || args[1].data[0] != '2')
			return pgk_resp_error("NOPROTO only RESP2 is supported",
								  response_length);
		return raw_response(
			"*14\r\n"
			"$6\r\nserver\r\n$7\r\npg_kvik\r\n"
			"$7\r\nversion\r\n$5\r\n0.1.0\r\n"
			"$5\r\nproto\r\n:2\r\n"
			"$2\r\nid\r\n:0\r\n"
			"$4\r\nmode\r\n$10\r\nstandalone\r\n"
			"$4\r\nrole\r\n$6\r\nmaster\r\n"
			"$7\r\nmodules\r\n*0\r\n",
			response_length);
	}
	if (pgk_resp_arg_equals(&args[0], "INFO"))
	{
		const char *info =
			"# Server\r\n"
			"server:pg_kvik\r\n"
			"pg_kvik_version:0.1.0\r\n"
			"redis_mode:standalone\r\n";

		if (argc != 1 && argc != 2)
			return pgk_resp_error("ERR wrong number of arguments for INFO",
								  response_length);
		return pgk_resp_bulk(info, strlen(info), response_length);
	}
	if (pgk_resp_arg_equals(&args[0], "QUIT"))
	{
		*close_after = true;
		return pgk_resp_simple("OK", response_length);
	}
	if (pgk_resp_arg_equals(&args[0], "CLIENT"))
	{
		if (argc == 4 && pgk_resp_arg_equals(&args[1], "SETINFO"))
			return pgk_resp_simple("OK", response_length);
		if (argc == 3 && pgk_resp_arg_equals(&args[1], "SETNAME"))
			return pgk_resp_simple("OK", response_length);
		if (argc == 2 && pgk_resp_arg_equals(&args[1], "GETNAME"))
			return pgk_resp_null(response_length);
		if (argc == 2 && pgk_resp_arg_equals(&args[1], "ID"))
			return pgk_resp_integer((int64) MyProcPid, response_length);
		return pgk_resp_error("ERR unsupported CLIENT subcommand",
							  response_length);
	}
	if (pgk_resp_arg_equals(&args[0], "COMMAND"))
		return raw_response("*0\r\n", response_length);
	if (pgk_resp_arg_equals(&args[0], "SELECT"))
	{
		if (argc == 2 && args[1].len == 1 && args[1].data[0] == '0')
			return pgk_resp_simple("OK", response_length);
		return pgk_resp_error("ERR only RESP database 0 is supported",
							  response_length);
	}
	if (pgk_resp_arg_equals(&args[0], "STAT") ||
		pgk_resp_arg_equals(&args[0], "STATS"))
	{
		char	   *json;

		if (argc != 1)
			return pgk_resp_error("ERR wrong number of arguments for STAT",
								  response_length);
		json = pgk_stats_json();
		return pgk_resp_bulk(json, strlen(json), response_length);
	}
	if (pgk_resp_arg_equals(&args[0], "INVALIDATE"))
	{
		char	   *namespace_value;
		uint64		count;

		if (argc != 2 || args[1].len == 0 ||
			args[1].len >= PGK_NAMESPACE_MAX)
			return pgk_resp_error("ERR INVALIDATE expects one namespace",
								  response_length);
		namespace_value = pnstrdup(args[1].data, args[1].len);
		count = pgk_cache_invalidate_namespace(MyDatabaseId,
											   namespace_value);
		return pgk_resp_integer((int64) count, response_length);
	}

	if (!pgk_resp_arg_equals(&args[0], "GET") &&
		!pgk_resp_arg_equals(&args[0], "SET") &&
		!pgk_resp_arg_equals(&args[0], "DEL"))
		return pgk_resp_error("ERR unsupported command", response_length);

	if ((pgk_resp_arg_equals(&args[0], "GET") && argc != 2) ||
		(pgk_resp_arg_equals(&args[0], "SET") && argc != 3) ||
		(pgk_resp_arg_equals(&args[0], "DEL") && argc != 2))
		return pgk_resp_error("ERR wrong number of arguments", response_length);

	if (!split_wire_key(&args[1], &nspace, &raw_key, &key_error))
		return pgk_resp_error(key_error, response_length);

	maybe_reload_mappings();
	mapping = find_mapping(nspace);
	if (mapping == NULL)
		return pgk_resp_error("ERR unknown pg_kvik namespace",
							  response_length);

	if (pgk_resp_arg_equals(&args[0], "GET"))
		return command_get(mapping, raw_key, response_length);
	if (pgk_resp_arg_equals(&args[0], "SET"))
		return command_set(mapping, raw_key, &args[2], response_length);
	return command_delete(mapping, raw_key, response_length);
}

static bool
split_wire_key(const PgKvikRespArg *wire_key, char **nspace,
			   char **raw_key, char **error)
{
	const char *separator;
	Size		namespace_length;
	Size		key_length;

	if (wire_key->len == 0 || wire_key->len >= PGK_REQUEST_MAX)
	{
		*error = "ERR invalid key";
		return false;
	}
	if (memchr(wire_key->data, '\0', wire_key->len) != NULL)
	{
		*error = "ERR NUL bytes are not supported in keys";
		return false;
	}

	separator = memchr(wire_key->data, ':', wire_key->len);
	if (separator == NULL)
	{
		*error = "ERR key must use namespace:key format";
		return false;
	}
	namespace_length = (Size) (separator - wire_key->data);
	key_length = wire_key->len - namespace_length - 1;
	if (namespace_length == 0 || namespace_length >= PGK_NAMESPACE_MAX ||
		key_length == 0 || key_length >= PGK_KEY_MAX)
	{
		*error = "ERR namespace or key is too long";
		return false;
	}

	*nspace = pnstrdup(wire_key->data, namespace_length);
	*raw_key = pnstrdup(separator + 1, key_length);
	pg_verifymbstr(*nspace, namespace_length, false);
	pg_verifymbstr(*raw_key, key_length, false);
	return true;
}

static PgKvikMapping *
find_mapping(const char *nspace)
{
	int			i;

	for (i = 0; i < worker_mapping_count; i++)
	{
		if (strcmp(worker_mappings[i].nspace, nspace) == 0)
			return &worker_mappings[i];
	}
	return NULL;
}

static bool
canonicalize_key(PgKvikMapping *mapping, const char *raw_key,
				 Datum *key_value, char **canonical, char **error)
{
	*key_value = InputFunctionCall(&mapping->key_input,
								   (char *) raw_key,
								   mapping->key_ioparam,
								   -1);
	*canonical = OutputFunctionCall(&mapping->key_output, *key_value);
	if (strlen(*canonical) >= PGK_KEY_MAX)
	{
		*error = "ERR canonical key is too long";
		return false;
	}
	return true;
}

static void
begin_spi_transaction(void)
{
	StartTransactionCommand();
	if (SPI_connect() != SPI_OK_CONNECT)
		elog(ERROR, "pg_kvik could not connect to SPI");
	PushActiveSnapshot(GetTransactionSnapshot());
}

static void
commit_spi_transaction(void)
{
	PopActiveSnapshot();
	if (SPI_finish() != SPI_OK_FINISH)
		elog(ERROR, "pg_kvik could not finish SPI");
	CommitTransactionCommand();
}

static char *
command_get(PgKvikMapping *mapping, const char *raw_key,
			Size *response_length)
{
	Datum		key_value;
	char	   *canonical;
	char	   *key_error = NULL;
	char		cached_value[PGK_VALUE_MAX];
	Size		cached_length;
	bool		negative;
	PgKvikReadToken token;
	bool		hit;
	Datum		values[1];
	char	   *database_value = NULL;
	Size		database_value_length = 0;

	if (!canonicalize_key(mapping, raw_key, &key_value,
						  &canonical, &key_error))
		return pgk_resp_error(key_error, response_length);

	hit = pgk_cache_lookup(mapping, canonical,
						   cached_value, sizeof(cached_value),
						   &cached_length, &negative, &token);
	if (hit)
	{
		if (negative)
			return pgk_resp_null(response_length);
		return pgk_resp_bulk(cached_value, cached_length, response_length);
	}

	values[0] = key_value;
	begin_spi_transaction();
	if (SPI_execute_plan(mapping->get_plan, values, NULL, true, 1) !=
		SPI_OK_SELECT)
		elog(ERROR, "pg_kvik GET plan failed");
	if (SPI_processed == 1)
	{
		database_value = SPI_getvalue(SPI_tuptable->vals[0],
									  SPI_tuptable->tupdesc, 1);
		if (database_value == NULL)
			elog(ERROR, "pg_kvik mapped value unexpectedly became NULL");
		database_value_length = strlen(database_value);
		if (database_value_length > PGK_VALUE_MAX)
			ereport(ERROR,
					(errcode(ERRCODE_PROGRAM_LIMIT_EXCEEDED),
					 errmsg("mapped value exceeds pg_kvik limit of %d bytes",
							PGK_VALUE_MAX)));
		database_value = pnstrdup(database_value, database_value_length);
	}
	commit_spi_transaction();
	pgk_note_database_read();

	if (database_value == NULL)
	{
		pgk_cache_store(mapping, canonical, &token, NULL, 0, true);
		return pgk_resp_null(response_length);
	}

	pgk_cache_store(mapping, canonical, &token,
					database_value, database_value_length, false);
	return pgk_resp_bulk(database_value, database_value_length,
						 response_length);
}

static char *
command_set(PgKvikMapping *mapping, const char *raw_key,
			const PgKvikRespArg *value_arg, Size *response_length)
{
	Datum		key_value;
	Datum		value;
	Datum		values[2];
	char	   *canonical;
	char	   *key_error = NULL;
	char	   *value_text;

	if (!mapping->writable)
		return pgk_resp_error("ERR namespace is read-only", response_length);
	if (value_arg->len > PGK_VALUE_MAX ||
		memchr(value_arg->data, '\0', value_arg->len) != NULL)
		return pgk_resp_error("ERR value is too large or contains NUL",
							  response_length);
	pg_verifymbstr(value_arg->data, value_arg->len, false);

	if (!canonicalize_key(mapping, raw_key, &key_value,
						  &canonical, &key_error))
		return pgk_resp_error(key_error, response_length);
	value_text = pnstrdup(value_arg->data, value_arg->len);
	begin_spi_transaction();
	value = InputFunctionCall(&mapping->value_input,
							  value_text,
							  mapping->value_ioparam,
							  -1);
	values[0] = key_value;
	values[1] = value;

	if (SPI_execute_plan(mapping->set_plan, values, NULL, false, 0) !=
		SPI_OK_INSERT)
		elog(ERROR, "pg_kvik SET plan failed");
	commit_spi_transaction();
	pgk_note_database_write();
	return pgk_resp_simple("OK", response_length);
}

static char *
command_delete(PgKvikMapping *mapping, const char *raw_key,
			   Size *response_length)
{
	Datum		key_value;
	Datum		values[1];
	char	   *canonical;
	char	   *key_error = NULL;
	uint64		deleted;

	if (!mapping->writable)
		return pgk_resp_error("ERR namespace is read-only", response_length);
	if (!canonicalize_key(mapping, raw_key, &key_value,
						  &canonical, &key_error))
		return pgk_resp_error(key_error, response_length);
	values[0] = key_value;

	begin_spi_transaction();
	if (SPI_execute_plan(mapping->delete_plan, values, NULL, false, 0) !=
		SPI_OK_DELETE)
		elog(ERROR, "pg_kvik DEL plan failed");
	deleted = SPI_processed;
	commit_spi_transaction();
	pgk_note_database_write();
	return pgk_resp_integer((int64) deleted, response_length);
}

static void
maybe_reload_mappings(void)
{
	uint64		generation = pgk_config_generation();
	TimestampTz now = GetCurrentTimestamp();

	if (generation != worker_mapping_generation &&
		(worker_next_mapping_retry == 0 || now >= worker_next_mapping_retry))
	{
		if (reload_mappings())
			worker_next_mapping_retry = 0;
	}
}

static void
free_mapping_plans(void)
{
	int			i;

	for (i = 0; i < worker_mapping_count; i++)
	{
		if (worker_mappings[i].get_plan)
			SPI_freeplan(worker_mappings[i].get_plan);
		if (worker_mappings[i].set_plan)
			SPI_freeplan(worker_mappings[i].set_plan);
		if (worker_mappings[i].delete_plan)
			SPI_freeplan(worker_mappings[i].delete_plan);
	}
}

static SPIPlanPtr
prepare_kept_plan(const char *query, int nargs, Oid *types)
{
	SPIPlanPtr	plan = SPI_prepare(query, nargs, types);

	if (plan == NULL)
		elog(ERROR, "could not prepare pg_kvik query: %s", query);
	if (SPI_keepplan(plan) != 0)
		elog(ERROR, "could not retain pg_kvik query plan");
	return plan;
}

static bool
reload_mappings(void)
{
	MemoryContext old_context = CurrentMemoryContext;
	uint64		target_generation = pgk_config_generation();
	bool		success = false;

	PG_TRY();
	{
		int			result;
		uint64		row;
		uint64		mapping_count;
		PgKvikMapping *new_mappings;

		begin_spi_transaction();
		free_mapping_plans();
		worker_mappings = NULL;
		worker_mapping_count = 0;
		MemoryContextReset(mapping_context);

		result = SPI_execute(
			"SELECT m.namespace, c.oid, n.nspname, c.relname, "
			"       m.key_column::text, m.value_column::text, m.writable, "
			"       ka.atttypid, va.atttypid "
			"  FROM kvik.mapping AS m "
			"  JOIN pg_catalog.pg_class AS c ON c.oid = m.relation "
			"  JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace "
			"  JOIN pg_catalog.pg_attribute AS ka "
			"    ON ka.attrelid = c.oid AND ka.attname = m.key_column "
			"   AND ka.attnum > 0 AND NOT ka.attisdropped AND ka.attnotnull "
			"   AND ka.atttypid IN "
			"       ('int2'::regtype, 'int4'::regtype, 'int8'::regtype, "
			"        'text'::regtype, 'varchar'::regtype, 'bpchar'::regtype, "
			"        'uuid'::regtype) "
			"  JOIN pg_catalog.pg_attribute AS va "
			"    ON va.attrelid = c.oid AND va.attname = m.value_column "
			"   AND va.attnum > 0 AND NOT va.attisdropped AND va.attnotnull "
			"  JOIN pg_catalog.pg_trigger AS rt "
			"    ON rt.tgrelid = c.oid "
			"   AND rt.tgname = 'pg_kvik_row_invalidate' "
			"   AND rt.tgenabled = 'A' AND NOT rt.tgisinternal "
			"   AND rt.tgfoid = 'kvik._row_invalidate()'::regprocedure "
			"  JOIN pg_catalog.pg_trigger AS tt "
			"    ON tt.tgrelid = c.oid "
			"   AND tt.tgname = 'pg_kvik_truncate_invalidate' "
			"   AND tt.tgenabled = 'A' AND NOT tt.tgisinternal "
			"   AND tt.tgfoid = 'kvik._truncate_invalidate()'::regprocedure "
			" WHERE c.relkind = 'r' AND c.relpersistence = 'p' "
			"   AND NOT c.relrowsecurity AND NOT c.relforcerowsecurity "
			"   AND EXISTS ("
			"       SELECT 1 FROM pg_catalog.pg_index AS i "
			"        WHERE i.indrelid = c.oid "
			"          AND i.indisunique AND i.indimmediate "
			"          AND i.indisvalid AND i.indisready "
			"          AND i.indpred IS NULL "
			"          AND i.indnkeyatts = 1 "
			"          AND i.indkey[0] = ka.attnum) "
			" ORDER BY m.namespace",
			true, 0);
		if (result != SPI_OK_SELECT)
			elog(ERROR, "could not load pg_kvik mappings");
		if (SPI_processed > PGK_MAX_MAPPINGS)
			elog(ERROR, "too many pg_kvik mappings");
		mapping_count = SPI_processed;

		new_mappings = MemoryContextAllocZero(mapping_context,
											  sizeof(PgKvikMapping) *
											  Max((uint64) 1, mapping_count));

		for (row = 0; row < mapping_count; row++)
		{
			HeapTuple	tuple = SPI_tuptable->vals[row];
			TupleDesc	desc = SPI_tuptable->tupdesc;
			PgKvikMapping *mapping = &new_mappings[row];
			bool		is_null;

			strlcpy(mapping->nspace, SPI_getvalue(tuple, desc, 1),
					sizeof(mapping->nspace));
			mapping->relation_oid =
				DatumGetObjectId(SPI_getbinval(tuple, desc, 2, &is_null));
			Assert(!is_null);
			strlcpy(mapping->schema_name, SPI_getvalue(tuple, desc, 3),
					sizeof(mapping->schema_name));
			strlcpy(mapping->relation_name, SPI_getvalue(tuple, desc, 4),
					sizeof(mapping->relation_name));
			strlcpy(mapping->key_column, SPI_getvalue(tuple, desc, 5),
					sizeof(mapping->key_column));
			strlcpy(mapping->value_column, SPI_getvalue(tuple, desc, 6),
					sizeof(mapping->value_column));
			mapping->writable =
				DatumGetBool(SPI_getbinval(tuple, desc, 7, &is_null));
			Assert(!is_null);
			mapping->key_type =
				DatumGetObjectId(SPI_getbinval(tuple, desc, 8, &is_null));
			Assert(!is_null);
			mapping->value_type =
				DatumGetObjectId(SPI_getbinval(tuple, desc, 9, &is_null));
			Assert(!is_null);

			{
				Oid			input_function;
				Oid			output_function;
				bool		is_varlena;

				getTypeInputInfo(mapping->key_type, &input_function,
								 &mapping->key_ioparam);
				getTypeOutputInfo(mapping->key_type, &output_function,
								  &is_varlena);
				fmgr_info_cxt(input_function, &mapping->key_input,
							  mapping_context);
				fmgr_info_cxt(output_function, &mapping->key_output,
							  mapping_context);
				getTypeInputInfo(mapping->value_type, &input_function,
								 &mapping->value_ioparam);
				fmgr_info_cxt(input_function, &mapping->value_input,
							  mapping_context);
			}
		}

		worker_mappings = new_mappings;
		worker_mapping_count = (int) mapping_count;

		/*
		 * SPI_prepare changes the global SPI_tuptable pointer.  Copy every
		 * catalog row first, then build plans in a separate pass.
		 */
		for (row = 0; row < mapping_count; row++)
		{
			PgKvikMapping *mapping = &new_mappings[row];
			char	   *qualified_relation;
			const char *quoted_key;
			const char *quoted_value;
			char	   *get_query;
			Oid			get_types[1];
			char	   *set_query;
			Oid			set_types[2];
			char	   *delete_query;
			Oid			delete_types[1];

			qualified_relation = quote_qualified_identifier(
				mapping->schema_name, mapping->relation_name);
			quoted_key = quote_identifier(mapping->key_column);
			quoted_value = quote_identifier(mapping->value_column);

			get_query = psprintf("SELECT %s::text FROM %s "
								 "WHERE %s = $1 LIMIT 1",
								 quoted_value, qualified_relation, quoted_key);
			get_types[0] = mapping->key_type;
			mapping->get_plan = prepare_kept_plan(get_query, 1, get_types);

			if (mapping->writable)
			{
				set_query = psprintf(
					"INSERT INTO %s (%s, %s) VALUES ($1, $2) "
					"ON CONFLICT (%s) DO UPDATE SET %s = EXCLUDED.%s",
					qualified_relation, quoted_key, quoted_value,
					quoted_key, quoted_value, quoted_value);
				set_types[0] = mapping->key_type;
				set_types[1] = mapping->value_type;
				mapping->set_plan = prepare_kept_plan(set_query, 2,
													  set_types);

				delete_query = psprintf("DELETE FROM %s WHERE %s = $1",
										qualified_relation, quoted_key);
				delete_types[0] = mapping->key_type;
				mapping->delete_plan = prepare_kept_plan(delete_query, 1,
														 delete_types);
			}
		}

		commit_spi_transaction();
		worker_mapping_generation = target_generation;
		worker_next_mapping_retry = 0;
		success = true;
	}
	PG_CATCH();
	{
		ErrorData  *error_data;

		MemoryContextSwitchTo(old_context);
		error_data = CopyErrorData();
		FlushErrorState();
		if (IsTransactionState())
			AbortCurrentTransaction();
		free_mapping_plans();
		MemoryContextReset(mapping_context);
		worker_mappings = NULL;
		worker_mapping_count = 0;
		worker_next_mapping_retry =
			TimestampTzPlusMilliseconds(GetCurrentTimestamp(), 1000);
		ereport(LOG,
				(errmsg("pg_kvik mappings are unavailable: %s",
						error_data->message)));
		FreeErrorData(error_data);
	}
	PG_END_TRY();
	return success;
}
