#include "postgres.h"

#include <arpa/inet.h>
#include <ctype.h>
#include <errno.h>
#include <fcntl.h>
#include <netinet/in.h>
#include <netinet/tcp.h>
#include <poll.h>
#include <signal.h>
#include <sys/stat.h>
#include <sys/socket.h>
#include <unistd.h>

#include "access/xact.h"
#include "catalog/pg_type_d.h"
#include "executor/spi.h"
#include "lib/stringinfo.h"
#include "mb/pg_wchar.h"
#include "miscadmin.h"
#include "postmaster/bgworker.h"
#include "storage/fd.h"
#include "storage/ipc.h"
#include "storage/latch.h"
#include "utils/builtins.h"
#include "utils/guc.h"
#include "utils/lsyscache.h"
#include "utils/memutils.h"
#include "utils/snapmgr.h"
#include "utils/timeout.h"
#include "utils/timestamp.h"
#include "utils/wait_event.h"
#include "tcop/tcopprot.h"

#include "pg_local_cache.h"
#include "resp.h"

#define PGLC_OUTPUT_BATCH_BYTES (16 * 1024)
#define PGLC_OUTPUT_BUFFER_MAX \
	(PGLC_RESPONSE_MAX + PGLC_OUTPUT_BATCH_BYTES)
#define PGLC_READY_CLIENTS_PER_TURN 8

typedef struct PgLocalCacheClient
{
	int			fd;
	bool		authenticated;
	bool		close_after_flush;
	bool		input_ready;
	uint8		authentication_failures;
	Size		input_start;
	Size		used;
	Size		output_used;
	Size		output_sent;
	TimestampTz last_activity;
	char		input[PGLC_REQUEST_MAX];
	char		output[PGLC_OUTPUT_BUFFER_MAX];
} PgLocalCacheClient;

static MemoryContext mapping_context = NULL;
static MemoryContext command_context = NULL;
static PgLocalCacheMapping *worker_mappings = NULL;
static int	worker_mapping_count = 0;
static uint64 worker_mapping_generation = 0;
static TimestampTz worker_next_mapping_retry = 0;
static char *worker_auth_token = NULL;

static void load_auth_token(void);
static int create_listener(void);
static void run_server(int listener);
static void close_client(PgLocalCacheClient *client);
static void compact_client_input(PgLocalCacheClient *client);
static bool flush_client_output(PgLocalCacheClient *client);
static bool queue_response(PgLocalCacheClient *client,
						   const char *response, Size response_length,
						   bool close_after);
static bool process_client(PgLocalCacheClient *client);
static char *execute_command(PgLocalCacheClient *client,
							 PgLocalCacheRespArg *args, int argc,
							 Size *response_length, bool *close_after);
static char *execute_command_inner(PgLocalCacheClient *client,
								   PgLocalCacheRespArg *args, int argc,
								   Size *response_length, bool *close_after);
static void maybe_reload_mappings(void);
static bool reload_mappings(void);
static PgLocalCacheMapping *find_mapping(const char *nspace);
static bool split_wire_key(const PgLocalCacheRespArg *wire_key,
						   char *nspace, char *raw_key,
						   char **error);
static bool canonicalize_key(PgLocalCacheMapping *mapping, const char *raw_key,
							 Datum *key_value, char **canonical,
							 char **error);
static void ensure_mapping_current(const PgLocalCacheMapping *mapping);
static char *command_get(PgLocalCacheMapping *mapping, const char *raw_key,
						 Size *response_length);
static char *command_set(PgLocalCacheMapping *mapping, const char *raw_key,
						 const PgLocalCacheRespArg *value_arg,
						 Size *response_length);
static char *command_delete(PgLocalCacheMapping *mapping, const char *raw_key,
							Size *response_length);

PGDLLEXPORT void
pg_local_cache_worker_main(Datum main_arg)
{
	int			listener;
	const char *role;

	pqsignal(SIGTERM, die);
	BackgroundWorkerUnblockSignals();

	pglc_require_preload();
	role = (pglc_role != NULL && pglc_role[0] != '\0') ? pglc_role : NULL;
	BackgroundWorkerInitializeConnection(pglc_database, role, 0);
	if (superuser() && !pglc_allow_superuser)
		ereport(FATAL,
				(errmsg("pg_local_cache refuses to run RESP workers as a superuser"),
				 errhint("Create a dedicated LOGIN role and set pg_local_cache.role, or enable pg_local_cache.allow_superuser only for development.")));
	load_auth_token();
	pg_atomic_fetch_add_u64(&pglc_shared->worker_starts, 1);

	mapping_context = AllocSetContextCreate(TopMemoryContext,
										"pg_local_cache mappings",
										ALLOCSET_DEFAULT_SIZES);
	command_context = AllocSetContextCreate(TopMemoryContext,
										"pg_local_cache command",
										ALLOCSET_SMALL_SIZES);
	maybe_reload_mappings();

	listener = create_listener();
	ereport(LOG,
			(errmsg("pg_local_cache worker %d listening on %s:%d for database \"%s\"",
					DatumGetInt32(main_arg), pglc_bind_address, pglc_port,
					pglc_database)));
	run_server(listener);
	close(listener);
	proc_exit(0);
}

static void
load_auth_token(void)
{
	const char *inline_token =
		(pglc_auth_token != NULL) ? pglc_auth_token : "";
	const char *token_file =
		(pglc_auth_token_file != NULL) ? pglc_auth_token_file : "";

	if (inline_token[0] != '\0' && token_file[0] != '\0')
		ereport(FATAL,
				(errmsg("set only one of pg_local_cache.auth_token and pg_local_cache.auth_token_file")));

	if (token_file[0] != '\0')
	{
		struct stat file_stat;
		FILE	   *file;
		char	   *buffer;
		Size		length;
		Size		i;
		int			extra;

		if (token_file[0] != '/')
			ereport(FATAL,
					(errmsg("pg_local_cache.auth_token_file must be an absolute path")));
		if (lstat(token_file, &file_stat) != 0)
			ereport(FATAL,
					(errmsg("could not stat pg_local_cache auth token file \"%s\": %m",
							token_file)));
		if (!S_ISREG(file_stat.st_mode))
			ereport(FATAL,
					(errmsg("pg_local_cache auth token file must be a regular file")));
		if (file_stat.st_uid != geteuid())
			ereport(FATAL,
					(errmsg("pg_local_cache auth token file must be owned by the RESP worker operating-system user")));
		if ((file_stat.st_mode & (S_IRWXG | S_IRWXO)) != 0)
			ereport(FATAL,
					(errmsg("pg_local_cache auth token file permissions are too broad"),
					 errhint("Use mode 0600 or 0400.")));

		file = AllocateFile(token_file, "r");
		if (file == NULL)
			ereport(FATAL,
					(errmsg("could not open pg_local_cache auth token file \"%s\": %m",
							token_file)));
		buffer = palloc0(PGLC_AUTH_TOKEN_MAX + 2);
		if (fgets(buffer, PGLC_AUTH_TOKEN_MAX + 2, file) == NULL)
		{
			FreeFile(file);
			ereport(FATAL,
					(errmsg("pg_local_cache auth token file is empty")));
		}
		extra = fgetc(file);
		if (extra != EOF)
		{
			FreeFile(file);
			ereport(FATAL,
					(errmsg("pg_local_cache auth token file must contain exactly one token of at most %d bytes",
							PGLC_AUTH_TOKEN_MAX)));
		}
		if (ferror(file))
		{
			FreeFile(file);
			ereport(FATAL,
					(errmsg("could not read pg_local_cache auth token file \"%s\": %m",
							token_file)));
		}
		if (FreeFile(file) != 0)
			ereport(FATAL,
					(errmsg("could not close pg_local_cache auth token file \"%s\": %m",
							token_file)));

		length = strlen(buffer);
		while (length > 0 &&
			   (buffer[length - 1] == '\n' || buffer[length - 1] == '\r'))
			buffer[--length] = '\0';
		if (length == 0 || length > PGLC_AUTH_TOKEN_MAX)
			ereport(FATAL,
					(errmsg("pg_local_cache auth token must contain 1-%d bytes",
							PGLC_AUTH_TOKEN_MAX)));
		for (i = 0; i < length; i++)
		{
			if (iscntrl((unsigned char) buffer[i]))
				ereport(FATAL,
						(errmsg("pg_local_cache auth token contains a control character")));
		}
		worker_auth_token = buffer;
	}
	else
	{
		if (strlen(inline_token) > PGLC_AUTH_TOKEN_MAX)
			ereport(FATAL,
					(errmsg("pg_local_cache.auth_token exceeds %d bytes",
							PGLC_AUTH_TOKEN_MAX)));
		worker_auth_token = pstrdup(inline_token);
		if (inline_token[0] != '\0')
			ereport(WARNING,
					(errmsg("pg_local_cache.auth_token is configured inline"),
					 errhint("Use pg_local_cache.auth_token_file in production.")));
	}

	if (strcmp(pglc_bind_address, "0.0.0.0") == 0 &&
		strlen(worker_auth_token) < 32)
		ereport(FATAL,
				(errmsg("a non-loopback pg_local_cache listener requires an auth token of at least 32 bytes")));
}

static int
create_listener(void)
{
	int			fd;
	int			enabled = 1;
	int			flags;
	struct sockaddr_in address;

	if (pglc_bind_address == NULL ||
		(strcmp(pglc_bind_address, "127.0.0.1") != 0 &&
		 strcmp(pglc_bind_address, "0.0.0.0") != 0))
		ereport(FATAL,
				(errmsg("pg_local_cache.bind_address must be an IPv4 literal"),
				 errhint("The alpha release supports 127.0.0.1 or 0.0.0.0.")));

	if (strcmp(pglc_bind_address, "0.0.0.0") == 0 &&
		(worker_auth_token == NULL || worker_auth_token[0] == '\0'))
		ereport(FATAL,
				(errmsg("pg_local_cache refuses a non-loopback listener without authentication")));

	fd = socket(AF_INET, SOCK_STREAM, 0);
	if (fd < 0)
		ereport(FATAL,
				(errcode_for_socket_access(),
				 errmsg("could not create pg_local_cache listener socket: %m")));

	if (setsockopt(fd, SOL_SOCKET, SO_REUSEADDR, &enabled, sizeof(enabled)) < 0)
		ereport(FATAL,
				(errcode_for_socket_access(),
				 errmsg("could not set SO_REUSEADDR on pg_local_cache socket: %m")));

	if (pglc_worker_count > 1)
	{
#ifdef SO_REUSEPORT
		if (setsockopt(fd, SOL_SOCKET, SO_REUSEPORT,
					   &enabled, sizeof(enabled)) < 0)
			ereport(FATAL,
					(errcode_for_socket_access(),
					 errmsg("could not set SO_REUSEPORT on pg_local_cache socket: %m")));
#else
		ereport(FATAL,
				(errmsg("pg_local_cache.workers > 1 requires SO_REUSEPORT")));
#endif
	}

	memset(&address, 0, sizeof(address));
	address.sin_family = AF_INET;
	address.sin_port = htons((uint16) pglc_port);
	if (inet_pton(AF_INET, pglc_bind_address, &address.sin_addr) != 1)
		ereport(FATAL,
				(errmsg("invalid pg_local_cache.bind_address \"%s\"",
						pglc_bind_address)));

	if (bind(fd, (struct sockaddr *) &address, sizeof(address)) < 0)
		ereport(FATAL,
				(errcode_for_socket_access(),
				 errmsg("could not bind pg_local_cache to %s:%d: %m",
						pglc_bind_address, pglc_port)));
	if (listen(fd, SOMAXCONN) < 0)
		ereport(FATAL,
				(errcode_for_socket_access(),
				 errmsg("could not listen on pg_local_cache socket: %m")));

	flags = fcntl(fd, F_GETFL, 0);
	if (flags < 0 || fcntl(fd, F_SETFL, flags | O_NONBLOCK) < 0)
		ereport(FATAL,
				(errcode_for_socket_access(),
				 errmsg("could not make pg_local_cache socket nonblocking: %m")));
	return fd;
}

static void
run_server(int listener)
{
	PgLocalCacheClient *clients;
	struct pollfd poll_fds[PGLC_MAX_CLIENTS_PER_WORKER + 1];
	int			poll_to_client[PGLC_MAX_CLIENTS_PER_WORKER + 1];
	int			next_ready_client = 0;
	int			i;

	/*
	 * Each client owns a bounded 64 KiB request buffer.  Keep the client
	 * array out of the background worker's small process stack.
	 */
	clients = MemoryContextAllocZero(TopMemoryContext,
									 sizeof(PgLocalCacheClient) *
									 PGLC_MAX_CLIENTS_PER_WORKER);
	for (i = 0; i < PGLC_MAX_CLIENTS_PER_WORKER; i++)
		clients[i].fd = -1;

	for (;;)
	{
		bool		have_buffered_ready = false;
		int			poll_count = 1;
		int			poll_result;
		int			ready_clients_processed = 0;
		int			ready_scan_start = next_ready_client;
		int			latch_result;
		int			step;
		TimestampTz now = GetCurrentTimestamp();

		maybe_reload_mappings();

		/*
		 * A fairness yield leaves complete requests in the client buffer.  Give
		 * a bounded round-robin set of runnable clients one turn before waiting
		 * for more socket events; TCP does not generate another POLLIN edge for
		 * bytes which are already in userspace.
		 */
		for (step = 0; step < PGLC_MAX_CLIENTS_PER_WORKER; step++)
		{
			int			client_index =
				(ready_scan_start + step) % PGLC_MAX_CLIENTS_PER_WORKER;
			PgLocalCacheClient *client = &clients[client_index];

			if (client->fd < 0 || !client->input_ready ||
				client->output_sent < client->output_used)
				continue;
			client->input_ready = false;
			if (!process_client(client))
				close_client(client);
			next_ready_client =
				(client_index + 1) % PGLC_MAX_CLIENTS_PER_WORKER;
			if (++ready_clients_processed >= PGLC_READY_CLIENTS_PER_TURN)
				break;
		}
		if (ready_clients_processed == 0)
			next_ready_client =
				(next_ready_client + 1) % PGLC_MAX_CLIENTS_PER_WORKER;

		poll_fds[0].fd = listener;
		poll_fds[0].events = POLLIN;
		poll_fds[0].revents = 0;
		poll_to_client[0] = -1;

		for (i = 0; i < PGLC_MAX_CLIENTS_PER_WORKER; i++)
		{
			if (clients[i].fd >= 0)
			{
				if (TimestampDifferenceExceeds(clients[i].last_activity,
										  now,
										  pglc_idle_timeout_ms))
				{
					if (clients[i].output_sent < clients[i].output_used)
						pg_atomic_fetch_add_u64(
							&pglc_shared->slow_client_drops, 1);
					close_client(&clients[i]);
					continue;
				}
				poll_fds[poll_count].fd = clients[i].fd;
				poll_fds[poll_count].events =
					(clients[i].output_sent < clients[i].output_used) ?
					POLLOUT : POLLIN;
				poll_fds[poll_count].revents = 0;
				poll_to_client[poll_count] = i;
				poll_count++;
				if (clients[i].input_ready &&
					clients[i].output_sent == clients[i].output_used)
					have_buffered_ready = true;
			}
		}

		poll_result = poll(poll_fds, poll_count,
						   have_buffered_ready ? 0 : 250);
		if (poll_result < 0 && errno != EINTR)
			ereport(LOG,
					(errcode_for_socket_access(),
					 errmsg("pg_local_cache poll failed: %m")));

		latch_result = WaitLatch(MyLatch,
								 WL_LATCH_SET | WL_TIMEOUT |
								 WL_POSTMASTER_DEATH,
								 0,
								 PG_WAIT_EXTENSION);
		ResetLatch(MyLatch);
		if (latch_result & WL_POSTMASTER_DEATH)
			proc_exit(1);
		CHECK_FOR_INTERRUPTS();

		if (poll_result <= 0)
			continue;

		if (poll_fds[0].revents & POLLIN)
		{
			int			accepted = 0;

			while (accepted++ < 32)
			{
				int			client_fd;
				int			slot = -1;
				int			flags;
				int			enabled = 1;

				client_fd = accept(listener, NULL, NULL);
				if (client_fd < 0)
				{
					if (errno == EAGAIN || errno == EWOULDBLOCK)
						break;
					if (errno == EINTR)
						continue;
					ereport(LOG,
							(errcode_for_socket_access(),
							 errmsg("pg_local_cache accept failed: %m")));
					break;
				}

				for (i = 0; i < PGLC_MAX_CLIENTS_PER_WORKER; i++)
				{
					if (clients[i].fd < 0)
					{
						slot = i;
						break;
					}
				}
				if (slot < 0)
				{
					pg_atomic_fetch_add_u64(
						&pglc_shared->rejected_connections, 1);
					close(client_fd);
					continue;
				}

				flags = fcntl(client_fd, F_GETFL, 0);
				if (flags < 0 ||
					fcntl(client_fd, F_SETFL, flags | O_NONBLOCK) < 0)
				{
					pg_atomic_fetch_add_u64(
						&pglc_shared->rejected_connections, 1);
					close(client_fd);
					continue;
				}
				if (setsockopt(client_fd, IPPROTO_TCP, TCP_NODELAY,
							   &enabled, sizeof(enabled)) < 0 ||
					setsockopt(client_fd, SOL_SOCKET, SO_KEEPALIVE,
							   &enabled, sizeof(enabled)) < 0)
				{
					pg_atomic_fetch_add_u64(
						&pglc_shared->rejected_connections, 1);
					close(client_fd);
					continue;
				}

				clients[slot].fd = client_fd;
				clients[slot].input_start = 0;
				clients[slot].used = 0;
				clients[slot].output_used = 0;
				clients[slot].output_sent = 0;
				clients[slot].close_after_flush = false;
				clients[slot].input_ready = false;
				clients[slot].authentication_failures = 0;
				clients[slot].last_activity = GetCurrentTimestamp();
				clients[slot].authenticated =
					worker_auth_token == NULL || worker_auth_token[0] == '\0';
				pg_atomic_fetch_add_u64(&pglc_shared->active_clients, 1);
			}
		}

		for (i = 1; i < poll_count; i++)
		{
			int			client_index = poll_to_client[i];

			if (client_index < 0 || clients[client_index].fd < 0)
				continue;
			if (poll_fds[i].revents & (POLLERR | POLLNVAL))
			{
				close_client(&clients[client_index]);
				continue;
			}
			if (poll_fds[i].revents & POLLOUT)
			{
				if (!flush_client_output(&clients[client_index]) ||
					(clients[client_index].close_after_flush &&
					 clients[client_index].output_sent ==
					 clients[client_index].output_used))
				{
					close_client(&clients[client_index]);
					continue;
				}
				if (clients[client_index].output_sent ==
					clients[client_index].output_used &&
					clients[client_index].input_start <
					clients[client_index].used)
					clients[client_index].input_ready = true;
				if (!(poll_fds[i].revents & POLLHUP))
					continue;
			}
			if (poll_fds[i].revents & POLLIN)
			{
				if (!process_client(&clients[client_index]))
					close_client(&clients[client_index]);
			}
			if (clients[client_index].fd < 0)
				continue;
			if (poll_fds[i].revents & POLLHUP)
			{
				/*
				 * POLLHUP can accompany the final POLLIN.  Drain complete requests
				 * already copied into userspace when their replies were flushed.  A
				 * full hangup with backpressured output cannot make progress and must
				 * close instead of spinning because poll reports HUP unconditionally.
				 */
				if (clients[client_index].input_start <
					clients[client_index].used &&
					clients[client_index].output_sent ==
					clients[client_index].output_used)
				{
					clients[client_index].input_ready = true;
					continue;
				}
				close_client(&clients[client_index]);
			}
		}
	}

	for (i = 0; i < PGLC_MAX_CLIENTS_PER_WORKER; i++)
		close_client(&clients[i]);
	pfree(clients);
}

static void
close_client(PgLocalCacheClient *client)
{
	if (client->fd >= 0)
	{
		close(client->fd);
		pg_atomic_fetch_sub_u64(&pglc_shared->active_clients, 1);
	}
	client->fd = -1;
	client->input_start = 0;
	client->used = 0;
	client->output_used = 0;
	client->output_sent = 0;
	client->close_after_flush = false;
	client->input_ready = false;
	client->authentication_failures = 0;
	client->authenticated = false;
}

static void
compact_client_input(PgLocalCacheClient *client)
{
	if (client->input_start == 0)
		return;
	if (client->input_start < client->used)
	{
		memmove(client->input, client->input + client->input_start,
				client->used - client->input_start);
		client->used -= client->input_start;
	}
	else
		client->used = 0;
	client->input_start = 0;
}

static bool
flush_client_output(PgLocalCacheClient *client)
{
	bool		wrote = false;

	while (client->output_sent < client->output_used)
	{
		ssize_t		written = send(client->fd,
								   client->output + client->output_sent,
								   client->output_used - client->output_sent,
#ifdef MSG_NOSIGNAL
								   MSG_NOSIGNAL
#else
								   0
#endif
			);

		if (written > 0)
		{
			client->output_sent += (Size) written;
			wrote = true;
			continue;
		}
		if (written < 0 && errno == EINTR)
			continue;
		if (written < 0 && (errno == EAGAIN || errno == EWOULDBLOCK))
		{
			pg_atomic_fetch_add_u64(
				&pglc_shared->output_backpressure_events, 1);
			if (wrote)
				client->last_activity = GetCurrentTimestamp();
			return true;
		}
		return false;
	}
	if (wrote)
		client->last_activity = GetCurrentTimestamp();
	client->output_used = 0;
	client->output_sent = 0;
	return true;
}

static bool
queue_response(PgLocalCacheClient *client,
			   const char *response, Size response_length,
			   bool close_after)
{
	if (client->output_sent != 0 ||
		response_length > sizeof(client->output) - client->output_used)
		return false;
	memcpy(client->output + client->output_used, response, response_length);
	client->output_used += response_length;
	client->close_after_flush |= close_after;
	return true;
}

static bool
finish_client_turn(PgLocalCacheClient *client)
{
	if (client->input_start == client->used)
	{
		client->input_start = 0;
		client->used = 0;
	}
	if (!flush_client_output(client))
		return false;
	return !(client->close_after_flush &&
			 client->output_sent == client->output_used);
}

static bool
process_client(PgLocalCacheClient *client)
{
	bool		read_attempted = false;
	int			commands_processed = 0;

	if (client->output_sent < client->output_used)
		return true;
	client->input_ready = false;
	maybe_reload_mappings();

	for (;;)
	{
		while (client->input_start < client->used)
		{
			PgLocalCacheRespArg args[PGLC_RESP_MAX_ARGS];
			int			argc;
			Size		consumed;
			const char *protocol_error;
			int			parse_result;
			MemoryContext previous_context;
			char	   *response;
			Size		response_length;
			bool		close_after = false;
			bool		queued;

			/*
			 * Reserve room for the largest possible response before executing a
			 * command.  SET and DEL must never be replayed merely because a
			 * nonblocking send could not accept their response.
			 */
			if (sizeof(client->output) - client->output_used <
				PGLC_RESPONSE_MAX)
			{
				if (!flush_client_output(client))
					return false;
				if (client->output_sent < client->output_used)
				{
					client->input_ready = true;
					return true;
				}
			}

			CHECK_FOR_INTERRUPTS();
			parse_result = pglc_resp_parse(
				client->input + client->input_start,
				client->used - client->input_start,
				args, &argc, &consumed, &protocol_error);
			if (parse_result == 0)
			{
				compact_client_input(client);
				if (client->used == sizeof(client->input))
				{
					client->close_after_flush = true;
					return finish_client_turn(client);
				}
				break;
			}
			if (parse_result < 0)
			{
				Size		error_length;
				char	   *error_response;

				pg_atomic_fetch_add_u64(&pglc_shared->protocol_errors, 1);
				error_response = pglc_resp_error(protocol_error, &error_length);
				queued = queue_response(client, error_response,
										error_length, true);
				pfree(error_response);
				if (!queued)
					return false;
				client->input_start = client->used;
				return finish_client_turn(client);
			}

			previous_context = MemoryContextSwitchTo(command_context);
			response = execute_command(client, args, argc,
								   &response_length, &close_after);
			queued = queue_response(client, response, response_length,
								close_after);
			MemoryContextSwitchTo(previous_context);
			if (queued)
				client->input_start += consumed;
			MemoryContextReset(command_context);

			if (!queued)
				return false;
			if (close_after)
			{
				client->input_start = client->used;
				return finish_client_turn(client);
			}

			commands_processed++;
			if (commands_processed >= pglc_max_pipeline_commands)
			{
				client->input_ready = client->input_start < client->used;
				return finish_client_turn(client);
			}
		}

		if (read_attempted)
			break;

		compact_client_input(client);
		for (;;)
		{
			ssize_t		received;

			received = recv(client->fd, client->input + client->used,
								sizeof(client->input) - client->used, 0);
			if (received < 0 && errno == EINTR)
				continue;
			read_attempted = true;
			if (received == 0)
			{
				client->close_after_flush = true;
				return finish_client_turn(client);
			}
			if (received > 0)
			{
				client->used += (Size) received;
				client->last_activity = GetCurrentTimestamp();
				break;
			}
			if (errno == EAGAIN || errno == EWOULDBLOCK)
				return finish_client_turn(client);
			return false;
		}
	}
	return finish_client_turn(client);
}

static char *
execute_command(PgLocalCacheClient *client, PgLocalCacheRespArg *args, int argc,
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
		if (error_data->elevel >= FATAL || ProcDiePending)
			ReThrowError(error_data);
		disable_all_timeouts(false);
		QueryCancelPending = false;
		if (IsTransactionState())
			AbortCurrentTransaction();
		message = psprintf("ERR PostgreSQL: %s", error_data->message);
		response = pglc_resp_error(message, response_length);
		FreeErrorData(error_data);
	}
	PG_END_TRY();
	return response;
}

static bool
constant_time_token_equals(const PgLocalCacheRespArg *argument)
{
	Size		expected_length = strlen(worker_auth_token);
	Size		max_length = Max(expected_length, argument->len);
	Size		difference = expected_length ^ argument->len;
	Size		i;

	for (i = 0; i < max_length; i++)
	{
		unsigned char expected = i < expected_length ?
			(unsigned char) worker_auth_token[i] : 0;
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
execute_command_inner(PgLocalCacheClient *client, PgLocalCacheRespArg *args, int argc,
					  Size *response_length, bool *close_after)
{
	char		nspace[PGLC_NAMESPACE_MAX];
	char		raw_key[PGLC_KEY_MAX];
	char	   *key_error;
	PgLocalCacheMapping *mapping;
	bool		is_delete;
	bool		is_get;
	bool		is_set;

	if (argc == 0)
		return pglc_resp_error("ERR empty command", response_length);

	if (pglc_resp_arg_equals(&args[0], "AUTH"))
	{
		const PgLocalCacheRespArg *token;
		bool		username_matches = true;

		if (argc != 2 && argc != 3)
			return pglc_resp_error("ERR wrong number of arguments for AUTH",
								  response_length);
		if (argc == 3)
		{
			const char *expected_username =
				(pglc_role != NULL && pglc_role[0] != '\0') ?
				pglc_role : "default";
			Size		expected_length = strlen(expected_username);

			username_matches =
				args[1].len == expected_length &&
				memcmp(args[1].data, expected_username, expected_length) == 0;
		}
		token = &args[argc - 1];
		if (username_matches && constant_time_token_equals(token))
		{
			client->authenticated = true;
			client->authentication_failures = 0;
			return pglc_resp_simple("OK", response_length);
		}
		client->authenticated = false;
		client->authentication_failures++;
		pg_atomic_fetch_add_u64(&pglc_shared->authentication_failures, 1);
		if (client->authentication_failures >= PGLC_MAX_AUTH_FAILURES)
			*close_after = true;
		return pglc_resp_error("WRONGPASS invalid authentication token",
							  response_length);
	}

	if (!client->authenticated)
		return pglc_resp_error("NOAUTH Authentication required",
								  response_length);

	/* Keep the dominant cache commands at the front of the dispatch path. */
	is_get = pglc_resp_arg_equals(&args[0], "GET");
	is_set = !is_get && pglc_resp_arg_equals(&args[0], "SET");
	is_delete = !is_get && !is_set && pglc_resp_arg_equals(&args[0], "DEL");
	if (is_get || is_set || is_delete)
	{
		if ((is_get && argc != 2) ||
			(is_set && argc != 3) ||
			(is_delete && argc != 2))
			return pglc_resp_error("ERR wrong number of arguments",
								  response_length);
		if (!split_wire_key(&args[1], nspace, raw_key, &key_error))
			return pglc_resp_error(key_error, response_length);
		mapping = find_mapping(nspace);
		if (mapping == NULL)
			return pglc_resp_error("ERR unknown pg_local_cache namespace",
								  response_length);
		if (is_get)
			return command_get(mapping, raw_key, response_length);
		if (is_set)
			return command_set(mapping, raw_key, &args[2], response_length);
		return command_delete(mapping, raw_key, response_length);
	}

	if (pglc_resp_arg_equals(&args[0], "PING"))
	{
		if (argc == 1)
			return pglc_resp_simple("PONG", response_length);
		if (argc == 2)
		{
			if (args[1].len > PGLC_VALUE_MAX)
				return pglc_resp_error("ERR PING payload is too large",
									  response_length);
			return pglc_resp_bulk(args[1].data, args[1].len, response_length);
		}
		return pglc_resp_error("ERR wrong number of arguments for PING",
							  response_length);
	}
	if (pglc_resp_arg_equals(&args[0], "ECHO"))
	{
		if (argc != 2)
			return pglc_resp_error("ERR wrong number of arguments for ECHO",
							  response_length);
		if (args[1].len > PGLC_VALUE_MAX)
			return pglc_resp_error("ERR ECHO payload is too large",
								  response_length);
		return pglc_resp_bulk(args[1].data, args[1].len, response_length);
	}
	if (pglc_resp_arg_equals(&args[0], "HELLO"))
	{
		if (argc != 2 || args[1].len != 1 || args[1].data[0] != '2')
			return pglc_resp_error("NOPROTO only RESP2 is supported",
								  response_length);
		return raw_response(
			"*14\r\n"
			"$6\r\nserver\r\n$14\r\npg_local_cache\r\n"
			"$7\r\nversion\r\n$5\r\n1.0.0\r\n"
			"$5\r\nproto\r\n:2\r\n"
			"$2\r\nid\r\n:0\r\n"
			"$4\r\nmode\r\n$10\r\nstandalone\r\n"
			"$4\r\nrole\r\n$6\r\nmaster\r\n"
			"$7\r\nmodules\r\n*0\r\n",
			response_length);
	}
	if (pglc_resp_arg_equals(&args[0], "INFO"))
	{
		const char *info =
			"# Server\r\n"
			"server:pg_local_cache\r\n"
			"pg_local_cache_version:1.0.0\r\n"
			"redis_mode:standalone\r\n";

		if (argc != 1 && argc != 2)
			return pglc_resp_error("ERR wrong number of arguments for INFO",
								  response_length);
		return pglc_resp_bulk(info, strlen(info), response_length);
	}
	if (pglc_resp_arg_equals(&args[0], "QUIT"))
	{
		*close_after = true;
		return pglc_resp_simple("OK", response_length);
	}
	if (pglc_resp_arg_equals(&args[0], "CLIENT"))
	{
		if (argc == 4 && pglc_resp_arg_equals(&args[1], "SETINFO"))
			return pglc_resp_simple("OK", response_length);
		if (argc == 3 && pglc_resp_arg_equals(&args[1], "SETNAME"))
			return pglc_resp_simple("OK", response_length);
		if (argc == 2 && pglc_resp_arg_equals(&args[1], "GETNAME"))
			return pglc_resp_null(response_length);
		if (argc == 2 && pglc_resp_arg_equals(&args[1], "ID"))
			return pglc_resp_integer((int64) MyProcPid, response_length);
		return pglc_resp_error("ERR unsupported CLIENT subcommand",
							  response_length);
	}
	if (pglc_resp_arg_equals(&args[0], "COMMAND"))
		return raw_response("*0\r\n", response_length);
	if (pglc_resp_arg_equals(&args[0], "SELECT"))
	{
		if (argc == 2 && args[1].len == 1 && args[1].data[0] == '0')
			return pglc_resp_simple("OK", response_length);
		return pglc_resp_error("ERR only RESP database 0 is supported",
							  response_length);
	}
	if (pglc_resp_arg_equals(&args[0], "STAT") ||
		pglc_resp_arg_equals(&args[0], "STATS"))
	{
		char	   *json;

		if (argc != 1)
			return pglc_resp_error("ERR wrong number of arguments for STAT",
								  response_length);
		json = pglc_stats_json();
		return pglc_resp_bulk(json, strlen(json), response_length);
	}
	if (pglc_resp_arg_equals(&args[0], "INVALIDATE"))
	{
		char	   *namespace_value;
		PgLocalCacheMapping *invalidate_mapping;
		uint64		count;

		if (argc != 2 || args[1].len == 0 ||
			args[1].len >= PGLC_NAMESPACE_MAX)
			return pglc_resp_error("ERR INVALIDATE expects one namespace",
								  response_length);
		namespace_value = pnstrdup(args[1].data, args[1].len);
		invalidate_mapping = find_mapping(namespace_value);
		if (invalidate_mapping == NULL)
			return pglc_resp_error("ERR unknown pg_local_cache namespace",
								  response_length);
		count = pglc_cache_invalidate_namespace(MyDatabaseId,
											   namespace_value);
		return pglc_resp_integer((int64) count, response_length);
	}

	return pglc_resp_error("ERR unsupported command", response_length);
}

static bool
split_wire_key(const PgLocalCacheRespArg *wire_key, char *nspace,
			   char *raw_key, char **error)
{
	const char *separator;
	Size		namespace_length;
	Size		key_length;

	if (wire_key->len == 0 || wire_key->len >= PGLC_REQUEST_MAX)
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
	if (namespace_length == 0 || namespace_length >= PGLC_NAMESPACE_MAX ||
		key_length == 0 || key_length >= PGLC_KEY_MAX)
	{
		*error = "ERR namespace or key is too long";
		return false;
	}

	memcpy(nspace, wire_key->data, namespace_length);
	nspace[namespace_length] = '\0';
	memcpy(raw_key, separator + 1, key_length);
	raw_key[key_length] = '\0';
	pg_verifymbstr(nspace, namespace_length, false);
	pg_verifymbstr(raw_key, key_length, false);
	return true;
}

static PgLocalCacheMapping *
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
canonicalize_key(PgLocalCacheMapping *mapping, const char *raw_key,
				 Datum *key_value, char **canonical, char **error)
{
	*key_value = InputFunctionCall(&mapping->key_input,
								   (char *) raw_key,
								   mapping->key_ioparam,
								   mapping->key_typmod);
	*canonical = OutputFunctionCall(&mapping->key_output, *key_value);
	if (strlen(*canonical) >= PGLC_KEY_MAX)
	{
		*error = "ERR canonical key is too long";
		return false;
	}
	return true;
}

static void
begin_spi_transaction(void)
{
	char		timeout[32];

	StartTransactionCommand();
	if (SPI_connect() != SPI_OK_CONNECT)
		elog(ERROR, "pg_local_cache could not connect to SPI");
	PushActiveSnapshot(GetTransactionSnapshot());

	snprintf(timeout, sizeof(timeout), "%d", pglc_statement_timeout_ms);
	(void) set_config_option("statement_timeout", timeout,
							 PGC_USERSET, PGC_S_SESSION,
							 GUC_ACTION_LOCAL, true, ERROR, false);
	snprintf(timeout, sizeof(timeout), "%d", pglc_lock_timeout_ms);
	(void) set_config_option("lock_timeout", timeout,
							 PGC_USERSET, PGC_S_SESSION,
							 GUC_ACTION_LOCAL, true, ERROR, false);
	enable_timeout_after(STATEMENT_TIMEOUT, pglc_statement_timeout_ms);
}

static void
ensure_mapping_current(const PgLocalCacheMapping *mapping)
{
	if (mapping->config_generation != pglc_config_generation())
		ereport(ERROR,
				(errcode(ERRCODE_T_R_SERIALIZATION_FAILURE),
				 errmsg("pg_local_cache mapping changed while the command was running"),
				 errhint("Retry the command.")));
}

static void
commit_spi_transaction(void)
{
	PopActiveSnapshot();
	if (SPI_finish() != SPI_OK_FINISH)
		elog(ERROR, "pg_local_cache could not finish SPI");
	if (get_timeout_active(STATEMENT_TIMEOUT))
		disable_timeout(STATEMENT_TIMEOUT, false);
	(void) get_timeout_indicator(STATEMENT_TIMEOUT, true);
	CommitTransactionCommand();
}

static char *
command_get(PgLocalCacheMapping *mapping, const char *raw_key,
			Size *response_length)
{
	Datum		key_value;
	char	   *canonical;
	char	   *key_error = NULL;
	char		cached_value[PGLC_VALUE_MAX];
	Size		cached_length;
	bool		negative;
	PgLocalCacheReadToken token;
	bool		hit;
	Datum		values[1];
	char	   *database_value = NULL;
	Size		database_value_length = 0;
	MemoryContext result_context = CurrentMemoryContext;

	if (!canonicalize_key(mapping, raw_key, &key_value,
						  &canonical, &key_error))
		return pglc_resp_error(key_error, response_length);

	hit = pglc_cache_lookup(mapping, canonical,
						   cached_value, sizeof(cached_value),
						   &cached_length, &negative, &token);
	if (hit)
	{
		if (negative)
			return pglc_resp_null(response_length);
		return pglc_resp_bulk(cached_value, cached_length, response_length);
	}

	values[0] = key_value;
	begin_spi_transaction();
	ensure_mapping_current(mapping);
	if (SPI_execute_plan(mapping->get_plan, values, NULL, true, 1) !=
		SPI_OK_SELECT)
		elog(ERROR, "pg_local_cache GET plan failed");
	ensure_mapping_current(mapping);
	if (SPI_processed == 1)
	{
		database_value = SPI_getvalue(SPI_tuptable->vals[0],
									  SPI_tuptable->tupdesc, 1);
		if (database_value == NULL)
			elog(ERROR, "pg_local_cache mapped value unexpectedly became NULL");
		database_value_length = strlen(database_value);
		if (database_value_length > PGLC_VALUE_MAX)
			ereport(ERROR,
					(errcode(ERRCODE_PROGRAM_LIMIT_EXCEEDED),
					 errmsg("mapped value exceeds pg_local_cache limit of %d bytes",
							PGLC_VALUE_MAX)));
		{
			char	   *copy =
				MemoryContextAlloc(result_context, database_value_length + 1);

			memcpy(copy, database_value, database_value_length + 1);
			database_value = copy;
		}
	}
	commit_spi_transaction();
	pglc_note_database_read();

	if (database_value == NULL)
	{
		pglc_cache_store(mapping, canonical, &token, NULL, 0, true);
		return pglc_resp_null(response_length);
	}

	pglc_cache_store(mapping, canonical, &token,
					database_value, database_value_length, false);
	return pglc_resp_bulk(database_value, database_value_length,
						 response_length);
}

static char *
command_set(PgLocalCacheMapping *mapping, const char *raw_key,
			const PgLocalCacheRespArg *value_arg, Size *response_length)
{
	Datum		key_value;
	Datum		value;
	Datum		values[2];
	char	   *canonical;
	char	   *key_error = NULL;
	char	   *value_text;

	if (!mapping->writable)
		return pglc_resp_error("ERR namespace is read-only", response_length);
	if (value_arg->len > PGLC_VALUE_MAX ||
		memchr(value_arg->data, '\0', value_arg->len) != NULL)
		return pglc_resp_error("ERR value is too large or contains NUL",
							  response_length);
	pg_verifymbstr(value_arg->data, value_arg->len, false);

	if (!canonicalize_key(mapping, raw_key, &key_value,
						  &canonical, &key_error))
		return pglc_resp_error(key_error, response_length);
	value_text = pnstrdup(value_arg->data, value_arg->len);
	begin_spi_transaction();
	ensure_mapping_current(mapping);
	value = InputFunctionCall(&mapping->value_input,
							  value_text,
							  mapping->value_ioparam,
							  mapping->value_typmod);
	values[0] = key_value;
	values[1] = value;

	if (SPI_execute_plan(mapping->set_plan, values, NULL, false, 0) !=
		SPI_OK_INSERT)
		elog(ERROR, "pg_local_cache SET plan failed");
	ensure_mapping_current(mapping);
	commit_spi_transaction();
	pglc_note_database_write();
	return pglc_resp_simple("OK", response_length);
}

static char *
command_delete(PgLocalCacheMapping *mapping, const char *raw_key,
			   Size *response_length)
{
	Datum		key_value;
	Datum		values[1];
	char	   *canonical;
	char	   *key_error = NULL;
	uint64		deleted;

	if (!mapping->writable)
		return pglc_resp_error("ERR namespace is read-only", response_length);
	if (!canonicalize_key(mapping, raw_key, &key_value,
						  &canonical, &key_error))
		return pglc_resp_error(key_error, response_length);
	values[0] = key_value;

	begin_spi_transaction();
	ensure_mapping_current(mapping);
	if (SPI_execute_plan(mapping->delete_plan, values, NULL, false, 0) !=
		SPI_OK_DELETE)
		elog(ERROR, "pg_local_cache DEL plan failed");
	ensure_mapping_current(mapping);
	deleted = SPI_processed;
	commit_spi_transaction();
	pglc_note_database_write();
	return pglc_resp_integer((int64) deleted, response_length);
}

static void
maybe_reload_mappings(void)
{
	uint64		generation = pglc_config_generation();

	if (generation == worker_mapping_generation)
		return;
	if (worker_next_mapping_retry != 0 &&
		GetCurrentTimestamp() < worker_next_mapping_retry)
		return;
	if (reload_mappings())
		worker_next_mapping_retry = 0;
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
		elog(ERROR, "could not prepare pg_local_cache query: %s", query);
	if (SPI_keepplan(plan) != 0)
		elog(ERROR, "could not retain pg_local_cache query plan");
	return plan;
}

static bool
reload_mappings(void)
{
	MemoryContext old_context = CurrentMemoryContext;
	uint64		target_generation = pglc_config_generation();
	bool		success = false;

	PG_TRY();
	{
		int			result;
		uint64		row;
		uint64		mapping_count;
		PgLocalCacheMapping *new_mappings;

		begin_spi_transaction();
		free_mapping_plans();
		worker_mappings = NULL;
		worker_mapping_count = 0;
		MemoryContextReset(mapping_context);

		result = SPI_execute(
			"SELECT m.namespace, c.oid, n.nspname, c.relname, "
			"       m.key_column::text, m.value_column::text, m.writable, "
			"       ka.atttypid, va.atttypid, ka.atttypmod, va.atttypmod "
			"  FROM local_cache.mapping AS m "
			"  JOIN pg_catalog.pg_class AS c ON c.oid = m.relation "
			"  JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace "
			"  JOIN pg_catalog.pg_attribute AS ka "
			"    ON ka.attrelid = c.oid AND ka.attname = m.key_column "
			"   AND ka.attnum > 0 AND NOT ka.attisdropped AND ka.attnotnull "
			"   AND ka.atttypid IN "
			"       ('int2'::regtype, 'int4'::regtype, 'int8'::regtype, "
			"        'text'::regtype, 'varchar'::regtype, 'bpchar'::regtype, "
			"        'uuid'::regtype) "
			"   AND (ka.attcollation = 0 OR EXISTS ("
			"       SELECT 1 FROM pg_catalog.pg_collation AS coll "
			"        WHERE coll.oid = ka.attcollation "
			"          AND coll.collisdeterministic)) "
			"  JOIN pg_catalog.pg_attribute AS va "
			"    ON va.attrelid = c.oid AND va.attname = m.value_column "
			"   AND va.attnum > 0 AND NOT va.attisdropped AND va.attnotnull "
			"   AND va.atttypid IN "
			"       ('int2'::regtype, 'int4'::regtype, 'int8'::regtype, "
			"        'numeric'::regtype, 'bool'::regtype, "
			"        'text'::regtype, 'varchar'::regtype, 'bpchar'::regtype, "
			"        'uuid'::regtype, 'json'::regtype, 'jsonb'::regtype) "
			"  JOIN pg_catalog.pg_trigger AS rt "
			"    ON rt.tgrelid = c.oid "
			"   AND rt.tgname = 'pg_local_cache_row_invalidate' "
			"   AND rt.tgenabled = 'A' AND NOT rt.tgisinternal "
			"   AND rt.tgtype = 29 AND rt.tgnargs = 2 "
			"   AND rt.tgfoid = 'local_cache._row_invalidate()'::regprocedure "
			"   AND rt.tgargs = "
			"       convert_to(m.namespace, current_setting('server_encoding')) "
			"       || decode('00', 'hex') "
			"       || convert_to(m.key_column::text, current_setting('server_encoding')) "
			"       || decode('00', 'hex') "
			"  JOIN pg_catalog.pg_trigger AS tt "
			"    ON tt.tgrelid = c.oid "
			"   AND tt.tgname = 'pg_local_cache_truncate_invalidate' "
			"   AND tt.tgenabled = 'A' AND NOT tt.tgisinternal "
			"   AND tt.tgtype = 32 AND tt.tgnargs = 1 "
			"   AND tt.tgfoid = 'local_cache._truncate_invalidate()'::regprocedure "
			"   AND tt.tgargs = "
			"       convert_to(m.namespace, current_setting('server_encoding')) "
			"       || decode('00', 'hex') "
			" WHERE c.relkind = 'r' AND c.relpersistence = 'p' "
			"   AND NOT c.relrowsecurity AND NOT c.relforcerowsecurity "
			"   AND EXISTS ("
			"       SELECT 1 FROM pg_catalog.pg_index AS i "
			"        JOIN pg_catalog.pg_class AS ic ON ic.oid = i.indexrelid "
			"        JOIN pg_catalog.pg_am AS am "
			"          ON am.oid = ic.relam AND am.amname = 'btree' "
			"        JOIN pg_catalog.pg_opclass AS opc "
			"          ON opc.oid = i.indclass[0] "
			"         AND opc.opcmethod = am.oid "
			"         AND opc.opcdefault "
			"         AND (opc.opcintype = ka.atttypid OR EXISTS ("
			"             SELECT 1 FROM pg_catalog.pg_cast AS pc "
			"              WHERE pc.castsource = ka.atttypid "
			"                AND pc.casttarget = opc.opcintype "
			"                AND pc.castmethod = 'b')) "
			"        WHERE i.indrelid = c.oid "
			"          AND i.indisunique AND i.indimmediate "
			"          AND i.indisvalid AND i.indisready "
			"          AND i.indpred IS NULL "
			"          AND i.indnkeyatts = 1 "
			"          AND i.indkey[0] = ka.attnum) "
			" ORDER BY m.namespace",
			true, 0);
		if (result != SPI_OK_SELECT)
			elog(ERROR, "could not load pg_local_cache mappings");
		if (SPI_processed > PGLC_MAX_MAPPINGS)
			elog(ERROR, "too many pg_local_cache mappings");
		mapping_count = SPI_processed;

		new_mappings = MemoryContextAllocZero(mapping_context,
											  sizeof(PgLocalCacheMapping) *
											  Max((uint64) 1, mapping_count));

		for (row = 0; row < mapping_count; row++)
		{
			HeapTuple	tuple = SPI_tuptable->vals[row];
			TupleDesc	desc = SPI_tuptable->tupdesc;
			PgLocalCacheMapping *mapping = &new_mappings[row];
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
			mapping->key_typmod =
				DatumGetInt32(SPI_getbinval(tuple, desc, 10, &is_null));
			Assert(!is_null);
			mapping->value_typmod =
				DatumGetInt32(SPI_getbinval(tuple, desc, 11, &is_null));
			Assert(!is_null);
			mapping->config_generation = target_generation;

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
			PgLocalCacheMapping *mapping = &new_mappings[row];
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
		if (error_data->elevel >= FATAL || ProcDiePending)
			ReThrowError(error_data);
		disable_all_timeouts(false);
		QueryCancelPending = false;
		if (IsTransactionState())
			AbortCurrentTransaction();
		free_mapping_plans();
		MemoryContextReset(mapping_context);
		worker_mappings = NULL;
		worker_mapping_count = 0;
		worker_next_mapping_retry =
			TimestampTzPlusMilliseconds(GetCurrentTimestamp(), 1000);
		ereport(LOG,
				(errmsg("pg_local_cache mappings are unavailable: %s",
						error_data->message)));
		FreeErrorData(error_data);
	}
	PG_END_TRY();
	return success;
}
