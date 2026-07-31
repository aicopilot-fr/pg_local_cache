# pg_local_cache

Встроенный в PostgreSQL RESP2-сервис и bounded row cache. Отдельный
Redis/Valkey-процесс не нужен: TCP listener, shared cache, SQL fallback и
транзакционная инвалидация работают в background workers PostgreSQL.

Статус: production candidate для одного PostgreSQL primary. Код, Docker-образ,
интеграционные тесты и обязательный performance gate рассчитаны на PostgreSQL
16 под Linux. Перед внедрением нагрузочный gate нужно повторить на целевом
железе и с реальным размером значений.

> Расширение и все GUC называются `pg_local_cache`. SQL API находится в схеме
> `local_cache`, а worker role называется `local_cache_worker`, потому что
> штатный PostgreSQL запрещает пользовательские схемы и роли с
> зарезервированным префиксом `pg_`.

## Возможности

- RESP2: `AUTH`, `PING`, `ECHO`, `HELLO 2`, `GET`, `SET`, `DEL`,
  `INVALIDATE`, `STAT`, `INFO`, минимальные `CLIENT`, `COMMAND`, `SELECT 0`
  и `QUIT`;
- positive и negative cache в PostgreSQL shared memory;
- несколько `SO_REUSEPORT` workers и общий cache между ними;
- shared-lock hot path, bounded sampled eviction и O(1) epoch invalidation;
- parameterized saved SPI plans;
- автоматическая инвалидация после SQL `INSERT`, `UPDATE`, `DELETE`,
  `TRUNCATE` и DDL;
- old/new key invalidation при изменении ключа;
- защита от late fill и remap старого relation через version,
  `config_generation` и transaction fences;
- fail-closed, если обязательный trigger удалён, изменён или отключён;
- typmod-aware `varchar`/`bpchar`, только deterministic key collation и
  стандартный B-tree equality opclass;
- statement/lock/idle timeouts, bounded pipeline, AUTH failure limit,
  nonblocking socket output и operational counters;
- отказ от `PREPARE TRANSACTION`, если транзакция меняла mapped table.

## Быстрый запуск в Docker

Нужны Docker с Compose v2 и два локальных secret-файла:

```bash
mkdir -p secrets
umask 077
openssl rand -base64 36 > secrets/postgres_password
openssl rand -base64 48 \
  | tr '+/' '-_' | tr -d '=[:space:]' \
  > secrets/pg_local_cache_auth_token
chmod 600 secrets/postgres_password secrets/pg_local_cache_auth_token

docker compose up --detach --build --wait
docker compose ps
```

По умолчанию оба порта публикуются только на `127.0.0.1`: PostgreSQL на
`5432`, RESP на `6380`. Данные сохраняются в named volume. Healthcheck
проверяет PostgreSQL, extension, все RESP workers, `AUTH` и `PING`.
Runtime-копия AUTH token хранится в отдельном `tmpfs` с mode `0600`, а не в
writable layer контейнера.

На первом запуске можно изменить базу и dedicated role, задав одновременно
`POSTGRES_DB`, такое же `PG_LOCAL_CACHE_DATABASE` и
`PG_LOCAL_CACHE_ROLE`. Init-скрипт создаст непривилегированную роль и выдаст
только базовые права. После инициализации volume смена этих значений требует
ручного создания role/grants либо нового volume.

`POSTGRES_IMAGE` вынесен в build argument. Для воспроизводимого production
build передайте разрешённый вашей организацией digest вместо плавающего тега:

```bash
docker compose build \
  --build-arg POSTGRES_IMAGE=postgres@sha256:<approved-digest>
```

Создание mapping:

```sql
CREATE TABLE public.items (
    id bigint PRIMARY KEY,
    value text NOT NULL
);

GRANT SELECT, INSERT, UPDATE, DELETE
    ON TABLE public.items TO local_cache_worker;

SELECT local_cache.register_mapping(
    'items',
    'public.items',
    'id',
    'value',
    true
);
```

Проверка через `redis-cli`:

```bash
export PG_LOCAL_CACHE_TOKEN="$(
  tr -d '\r\n' < secrets/pg_local_cache_auth_token
)"
redis-cli -h 127.0.0.1 -p 6380 -a "$PG_LOCAL_CACHE_TOKEN" \
  SET items:1 hello
redis-cli -h 127.0.0.1 -p 6380 -a "$PG_LOCAL_CACHE_TOKEN" \
  GET items:1
```

`register_mapping`, `unregister_mapping` и ручная инвалидация закрыты от
`PUBLIC`. Выполняйте их от имени extension owner либо выдавайте права только
отдельной административной роли.

## Нативная установка

Нужны PostgreSQL server headers той же major-версии, C compiler и PGXS:

```bash
make PG_CONFIG=/path/to/pg_config
sudo make PG_CONFIG=/path/to/pg_config install
```

Создайте dedicated LOGIN role, выдайте ему доступ к базе, схеме mapping и
только необходимым таблицам:

```sql
CREATE ROLE local_cache_worker
    LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION;
GRANT CONNECT ON DATABASE app TO local_cache_worker;
```

Файл токена должен быть абсолютным, обычным файлом, принадлежать OS-пользователю
PostgreSQL и не иметь group/other permissions:

```conf
shared_preload_libraries = 'pg_local_cache'
max_worker_processes = 16

pg_local_cache.database = 'app'
pg_local_cache.role = 'local_cache_worker'
pg_local_cache.bind_address = '127.0.0.1'
pg_local_cache.port = 6380
pg_local_cache.workers = 4
pg_local_cache.cache_entries = 16384
pg_local_cache.auth_token_file = '/run/pg_local_cache/auth_token'
```

После полного restart:

```sql
CREATE EXTENSION pg_local_cache;
GRANT USAGE ON SCHEMA local_cache TO local_cache_worker;
GRANT SELECT ON TABLE local_cache.mapping TO local_cache_worker;
```

## Конфигурация

Все параметры имеют context `postmaster` и требуют restart.

| GUC | Default | Назначение |
|---|---:|---|
| `pg_local_cache.port` | `6380` | RESP port; `0` отключает workers |
| `pg_local_cache.bind_address` | `127.0.0.1` | `127.0.0.1` или `0.0.0.0` |
| `pg_local_cache.workers` | `4` | RESP workers, `1..32` |
| `pg_local_cache.cache_entries` | `16384` | Shared entries, `128..65536` |
| `pg_local_cache.idle_timeout_ms` | `300000` | Idle/slow-client deadline |
| `pg_local_cache.statement_timeout_ms` | `2000` | Полный SQL command deadline |
| `pg_local_cache.lock_timeout_ms` | `250` | Lock wait deadline |
| `pg_local_cache.max_pipeline_commands` | `256` | Fairness budget команд за event-loop turn |
| `pg_local_cache.max_dirty_keys` | `4096` | Переход к relation invalidation |
| `pg_local_cache.auth_token_file` | empty | Production AUTH secret |
| `pg_local_cache.allow_superuser` | `off` | Только локальная разработка |

Одна cache entry занимает примерно 8.6 KiB до hash overhead. Значение
`65536`, используемое Docker-профилем, требует около 0.55 GiB только для
cache; планируйте память PostgreSQL и контейнера с запасом. Увеличьте
`max_worker_processes` как минимум на число extension workers, не забирая
слоты у replication/parallel jobs.

Для `0.0.0.0` обязателен token не короче 32 bytes. Inline
`pg_local_cache.auth_token` оставлен только для тестов: secret в production
должен приходить из файла.

## Wire API

Wire key имеет вид `namespace:key`. Значение — текстовое представление одного
PostgreSQL column, не произвольный binary blob.

| Команда | Результат |
|---|---|
| `GET namespace:key` | bulk value или RESP null |
| `SET namespace:key value` | `INSERT ... ON CONFLICT DO UPDATE`, затем `OK` |
| `DEL namespace:key` | `0` или `1` |
| `INVALIDATE namespace` | инвалидирует зарегистрированный namespace |
| `STAT` | JSON counters cache/SQL/invalidation/connections |

Каждый `SET` и `DEL` — отдельная PostgreSQL-транзакция. Ответ отправляется
после commit. Если TCP-соединение оборвалось до ответа, outcome записи
неизвестен; слепой автоматический retry может повторить операцию.

SQL API:

```sql
SELECT local_cache.stats();
SELECT local_cache.invalidate('items');
SELECT local_cache.unregister_mapping('items');
```

## Консистентность

Row trigger собирает изменённые ключи в памяти backend-транзакции. В
`XACT_EVENT_PRE_COMMIT`, до появления commit visibility, extension резервирует
dirty markers, увеличивает версии и делает старые positive/negative entries
невалидными. После commit marker снимается; при abort снимается без публикации
данных.

Cache miss запоминает key/relation/global/config versions до SQL `SELECT` и
заполняет cache только если ни одна из них не изменилась. Поэтому на одном
primary `GET`, начавшийся после завершившегося SQL или RESP commit, не
возвращает более старое cached value. Пересекающийся с commit `GET` может
вернуть старое или новое committed значение.

Гарантия зависит от исправных extension triggers/event trigger. Worker
проверяет их имя, функцию, type, arguments и `ENABLE ALWAYS`; при расхождении
namespace перестаёт обслуживаться.

Event triggers перезагружают mappings только для DDL, затрагивающего mapped
relation и его зависимые объекты. Обычный и temporary DDL других таблиц не
сбрасывает тёплый cache. Явный `DROP INDEX` постоянного объекта обрабатывается
консервативно как global mapping change, потому что PostgreSQL уже удалил
связь index→table к моменту `sql_drop`.

## Производительность и gate 10k ops/s

Обязательный gate измеряет только успешные warm positive `GET`: persistent
connections, pipeline, ноль cache misses, ноль SQL reads и ноль RESP errors.
Он завершает процесс с ошибкой при результате ниже
`PG_LOCAL_CACHE_MIN_OPS` (default `10000`).

```bash
PG_LOCAL_CACHE_PSQL=/path/to/psql \
PGHOST=127.0.0.1 PGPORT=5432 PGDATABASE=app \
PG_LOCAL_CACHE_RESP_PORT=6380 \
PG_LOCAL_CACHE_AUTH_TOKEN="$PG_LOCAL_CACHE_TOKEN" \
PG_LOCAL_CACHE_BENCH_ROLE=local_cache_worker \
PG_LOCAL_CACHE_BENCH_DURATION=30 \
PG_LOCAL_CACHE_BENCH_CONCURRENCY=16 \
PG_LOCAL_CACHE_BENCH_PIPELINE=32 \
PG_LOCAL_CACHE_BENCH_KEYS=1024 \
PG_LOCAL_CACHE_MIN_OPS=10000 \
make load
```

Контрольный прогон PostgreSQL 16.14 с non-superuser role, 4 workers,
`cache_entries=16384`, 16 clients, pipeline 32, 1024 warm keys и value
128 bytes:

- `246583 ops/s`;
- p50 `1.620 ms`, p95 `4.688 ms`, p99 `6.792 ms`;
- `0` errors, `0` misses и `0` SQL reads во время measurement.

Это не универсальная гарантия для любого CPU/container. CI повторяет
короткий smoke gate `>=10000 warm GET/s`; перед production rollout повторите
30–60 second gate несколько раз на закреплённых CPU целевой машины и
контролируйте p99. `SET`, `DEL`, cold miss и invalidation-heavy workloads
измеряйте отдельно: они включают стоимость PostgreSQL transaction, storage,
WAL и locks.

## Тесты и CI

Полный Docker smoke собирает образ, поднимает чистый volume, проверяет health,
запускает расширенный integration suite и warm-cache benchmark:

```bash
PG_LOCAL_CACHE_SMOKE_DURATION=10 \
PG_LOCAL_CACHE_SMOKE_MIN_OPS=10000 \
bash tests/docker_smoke.sh
```

Integration suite покрывает AUTH limits, malformed/fragmented/pipelined RESP,
GET/SET/DEL, positive/negative cache, SQL/DDL/TRUNCATE invalidation, отсутствие
сброса на unrelated/TEMP DDL, rollback, key move, trigger integrity, typmod,
deterministic collation, value-type allowlist, remap fence, relation-state GC,
timeouts, каждый настроенный worker PID, full cache, race fence и 2PC
rejection.

Workflow `.github/workflows/ci.yml` выполняет два независимых профиля на push,
PR и ручном запуске: correctness с cache `128`, включённым 2PC и нестандартной
database/role; throughput с production-профилем cache `65536` и gate 10k.

## Наблюдаемость

`STAT` и `local_cache.stats()` возвращают:

- active/positive/negative/dirty entries и relation states;
- hits, misses, negative hits, evictions;
- database reads/writes и invalidations;
- active/rejected connections, AUTH/protocol failures, slow-client drops;
- worker starts и pending forget markers.

Следите за hit ratio, неожиданными SQL reads, evictions, rejected connections,
timeout errors и рестартами workers. Cache недолговечен и прогревается заново
после restart/failover.

## Ограничения и security boundary

- один primary, одна настроенная database и один фиксированный worker role;
- только permanent ordinary tables;
- один `NOT NULL` key и один `NOT NULL` value column;
- key types: `int2`, `int4`, `int8`, `text`, `varchar`, `bpchar`, `uuid`;
- value types: `int2`, `int4`, `int8`, `numeric`, `bool`, `text`, `varchar`,
  `bpchar`, `uuid`, `json`, `jsonb`; domain, enum и composite запрещены;
- key требует full immediate single-column `UNIQUE` B-tree с default equality;
- RLS, partitions, foreign/temp/unlogged tables, nondeterministic collations и
  composite keys не поддерживаются;
- writable mapping не поддерживает generated/identity key/value columns;
- `SET` передаёт только key/value; другие `NOT NULL` columns требуют default;
- нет TTL, `MGET`, `MULTI/WATCH`, Lua, Pub/Sub, NX/XX/EX/PX и RESP3;
- namespace: 1–63 ASCII `[A-Za-z0-9_.-]`, key <256 bytes, value <=8192 bytes,
  request <=64 KiB;
- максимум 128 mappings и 128 clients на worker;
- listener только IPv4; несколько workers требуют `SO_REUSEPORT`;
- не выдавайте untrusted ролям DDL-права в application schemas: явный
  permanent `DROP INDEX` вызывает консервативную global invalidation;
- встроенного TLS нет. Token идёт открытым текстом: оставляйте RESP в private
  network/localhost либо ставьте TLS proxy и firewall;
- standby/multi-primary не поддерживаются;
- C-extension выполняется внутри PostgreSQL; crash в native code способен
  вызвать recovery всего instance.

KVik использует keys вида `CRUD:database.schema.table:{"id":1}` и JSON всей
строки. `pg_local_cache` использует `namespace:scalar-key` и один textual value
column. Полная KVik compatibility потребует whole-row JSON, composite primary
keys и отдельного mapping API; текущий протокол намеренно не выдаётся за неё.

Архитектура использует штатные PostgreSQL
[background workers](https://www.postgresql.org/docs/16/bgworker.html),
[SPI](https://www.postgresql.org/docs/16/spi.html) и
[triggers](https://www.postgresql.org/docs/16/trigger-definition.html).
