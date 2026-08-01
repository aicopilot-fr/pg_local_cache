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
- прозрачный fast path для обычного parameterized SQL lookup по primary key:
  тот же libpq/JDBC/ORM driver и тот же `SELECT`, без cache-specific client;
- несколько `SO_REUSEPORT` workers и общий cache между ними;
- bounded single-flight для одновременных cold `GET` одного ключа;
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
- statement/lock/idle timeouts, bounded per-client event-loop budget, AUTH
  failure limit, batched nonblocking socket output и operational counters;
- startup memory budget, global client CAS-limit, bounded per-worker buffers и
  per-transaction dirty-key fallback для защиты от OOM;
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

Production Compose ограничивает PostgreSQL container двумя GiB и одновременно
задаёт extension-owned budget 768 MiB. Это разные границы: первая защищает весь
процесс PostgreSQL через cgroup, вторая ещё до старта отклоняет комбинацию
cache/workers/client buffers, которая не помещается в заявленный бюджет.

`Dockerfile` — multi-stage образ: compiler и PostgreSQL headers остаются в
builder stage, а runtime содержит только PostgreSQL 16, extension, init,
healthcheck и команду подключения таблицы. Собрать образ и проверить CLI:

```bash
docker build --tag pg_local_cache:1.0.0 .
docker run --rm pg_local_cache:1.0.0 pg_local_cache_attach --help
```

Если нужен только прозрачный SQL cache без RESP listener, второй secret и
отдельный порт не требуются. SQL-only Compose выставляет
`pg_local_cache.port=0`, но оставляет shared memory, planner hook,
транзакционные triggers и `local_cache.stats()`:

```bash
mkdir -p secrets
umask 077
openssl rand -base64 36 > secrets/postgres_password
chmod 600 secrets/postgres_password

docker compose -f compose.sql-only.yaml up --detach --build --wait
psql 'postgresql://postgres@127.0.0.1:5432/app'
```

В этом режиме healthcheck не ждёт RESP workers и не читает cache token.
`local_cache_worker` всё ещё создаётся как изолированная техническая роль для
единого attach/mapping API, но сетевых background workers нет.

На первом запуске можно изменить базу и dedicated role, задав одновременно
`POSTGRES_DB`, такое же `PG_LOCAL_CACHE_DATABASE` и
`PG_LOCAL_CACHE_ROLE`. Init-скрипт создаст непривилегированную роль и выдаст
только базовые права. После инициализации volume смена этих значений требует
ручного создания role/grants либо нового volume.

### Обычный PostgreSQL-пользователь

Да: application role может быть обычным `NOSUPERUSER` и выполнять привычные
SQL `SELECT`/`INSERT`/`UPDATE`/`DELETE`. Установленный trigger участвует в той
же транзакции, поэтому публикация инвалидации происходит только при commit, а
rollback ничего не инвалидирует. Права на внутренние trigger functions
application role не нужны.

Не используйте `POSTGRES_USER` как пользователя приложения: официальный
PostgreSQL image создаёт эту bootstrap-role как superuser. Создайте отдельную:

```sql
CREATE ROLE app_user
    LOGIN PASSWORD 'replace-with-a-secret'
    NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS;
GRANT CONNECT ON DATABASE app TO app_user;
GRANT USAGE ON SCHEMA public TO app_user;
```

RESP port — не альтернативный способ войти паролем `app_user`. Он принимает
общий cache token, а все команды выполняются под одной фиксированной
`PG_LOCAL_CACHE_ROLE` (`local_cache_worker`). Token даёт доступ ко всем
зарегистрированным namespaces, `STAT` и `INVALIDATE`; per-user PostgreSQL ACL
и RLS на RESP endpoint не применяются. Для разных trust zones используйте
разные инстансы/token либо отдельный auth proxy.

`POSTGRES_IMAGE` вынесен в build argument. Для воспроизводимого production
build передайте разрешённый вашей организацией digest вместо плавающего тега:

```bash
docker compose build \
  --build-arg POSTGRES_IMAGE=postgres@sha256:<approved-digest>
```

Создайте таблицу:

```sql
CREATE TABLE public.items (
    id bigint PRIMARY KEY,
    value text NOT NULL
);

GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE public.items TO app_user;
```

В Docker достаточно одной команды. Она находит одиночный primary key,
выдаёт `local_cache_worker` необходимые права и вызывает административный
`register_mapping`, который сам создаёт и проверяет statement guard и
row/truncate `ENABLE ALWAYS` triggers:

```bash
docker compose exec postgres pg_local_cache_attach \
  --table public.items \
  --writable
```

Это административная команда: запускайте её только через trusted deploy/init
под bootstrap/extension owner. Доступ к Docker daemon или `docker exec` уже
эквивалентен root-доступу к этому контейнеру; `app_user` выполнять attach не
должен.

Для таблицы ровно из PK и одной value-колонки namespace и value column
выводятся автоматически (`items`, `value`). Если колонок больше, укажите их
явно:

```bash
docker compose exec postgres pg_local_cache_attach \
  --database app \
  --namespace items \
  --table public.items \
  --value-column value \
  --writable
```

Составной PK намеренно не поддерживается. По умолчанию mapping read-only;
флаг `--writable` включает RESP `SET`/`DEL`. Если другие `NOT NULL` columns
имеют defaults на sequence/function, worker role дополнительно нужны права на
эти объекты. Команда только добавляет ACL и не отзывает старые grants. Она не
переназначит занятый другой таблицей namespace без явного `--replace`.

Эквивалентная ручная регистрация для native installation:

```sql
-- Для таблицы из PK + одной value column достаточно relation:
SELECT local_cache.attach_table('public.items'::regclass);

-- Явная value column, namespace и writable RESP API:
SELECT local_cache.attach_table(
    'public.items'::regclass,
    'value',
    'items',
    true
);
```

`attach_table` сам находит одиночный primary key, выдаёт dedicated worker role
минимальные table/schema grants, регистрирует mapping и возвращает JSON с
готовыми key/GET/SET/DEL templates. Это административная функция с
`SECURITY DEFINER`, отозванная у `PUBLIC`; application role она не нужна.

Низкоуровневый `register_mapping(namespace, relation, key_column,
value_column, writable)` оставлен для миграций, которым нужен полный контроль
ACL. В этом случае grants worker role выдаются вручную.

### Drop-in SQL fast path без нового драйвера

После `attach_table` приложение может продолжить отправлять обычный SQL через
libpq, JDBC, Npgsql, psycopg или существующий ORM. Для fast path поддерживается
узкая и предсказуемая форма lookup:

```sql
PREPARE get_item(bigint) AS
SELECT value
FROM public.items
WHERE id = $1;

EXECUTE get_item(42);
```

Также поддерживается `LIMIT 1`. Projection должна содержать ровно mapped
value column, а predicate — ровно equality по mapped PK с `Const` или внешним
parameter. Alias таблицы допустим. Join, `SELECT *`, дополнительные filters,
sort, CTE, aggregate, row lock и expression над value не кешируются: PostgreSQL
просто строит штатный план и возвращает правильный результат. Это fail-open по
производительности, а не по данным.

Обычные межтиповые integer lookup тоже прозрачны: например, PostgreSQL
разбирает `bigint_id = 1` как `int8 = int4`. Fast path использует equality из
того же B-tree opfamily и безопасно расширяет `int2/int4` key expression до
типа PK; потенциально сужающие преобразования остаются на штатном плане.

На тёплом positive entry CustomScan возвращает value из shared memory. На
первом miss он выполняет сохранённый в плане штатный unique B-tree IndexScan,
проверяет `ctid` и raw `xmin` на свежем MVCC snapshot и только затем публикует
positive value. Поэтому специальная функция, второй порт или cache-aware
driver не нужны. Отсутствующая строка и cached negative entry всегда идут в
основную таблицу; transparent SQL path намеренно не публикует negative cache.

Безопасные fallback обязательны:

- `REPEATABLE READ` и `SERIALIZABLE` читают только PostgreSQL;
- после собственного `INSERT`/`UPDATE`/`DELETE` в текущей транзакции cache
  обходится до её завершения;
- entry с `xmin`, невидимым statement snapshot, не используется;
- FullXID age fence не позволяет raw 32-bit `xmin` пережить неоднозначное
  окно transaction-ID wraparound;
- disabled/изменённые triggers, RLS, recovery и неподдерживаемая форма запроса
  отключают fast path;
- commit invalidation и self-fill защищены одной системой
  key/relation/global/config version fences; rollback не публикует invalidation.

Application role нужны только обычные права на source table. Planner читает
mapping внутри extension, но стандартная проверка ACL исходного `SELECT`
остаётся обязательной; `USAGE` на `local_cache` приложению не выдаётся.
Session kill switch доступен без superuser:

```sql
SET pg_local_cache.sql_cache = off;
-- или только для текущей транзакции
SET LOCAL pg_local_cache.sql_cache = off;
```

Проверить выбор fast path можно тем же запросом:

```sql
EXPLAIN (ANALYZE, COSTS OFF)
SELECT value FROM public.items WHERE id = 42 LIMIT 1;
```

В плане будет `Custom Scan (pg_local_cache_sql)` с `Cache Namespace`, а при
`ANALYZE` — `Cache Hits`, `Cache Misses` и `Cache Bypasses`. Суммарные
`sql_cache_hits`, `sql_cache_misses`, `sql_cache_fills` и
`sql_cache_bypasses` доступны в `local_cache.stats()`.

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

`register_mapping` и `unregister_mapping` закрыты от `PUBLIC`. Выполняйте их
от имени extension owner либо выдавайте `EXECUTE` только полностью доверенной
административной роли: это `SECURITY DEFINER` API управляет triggers.
`local_cache.invalidate(text)` дополнительно требует реального superuser в C,
поэтому одного `GRANT EXECUTE` для обычной роли недостаточно. Обычным
application roles ручная инвалидация не нужна.

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
    LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION
    NOBYPASSRLS;
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

Все параметры, кроме явно отмеченного session GUC, имеют context `postmaster`
и требуют restart.

| GUC | Default | Назначение |
|---|---:|---|
| `pg_local_cache.port` | `6380` | RESP port; `0` отключает workers |
| `pg_local_cache.bind_address` | `127.0.0.1` | `127.0.0.1` или `0.0.0.0` |
| `pg_local_cache.workers` | `4` | RESP workers, `1..32` |
| `pg_local_cache.cache_entries` | `16384` | Shared entries, `128..65536` |
| `pg_local_cache.relation_states` | `1024` | Shared namespace states, `128..8192` |
| `pg_local_cache.max_clients` | `256` | Глобальный предел RESP clients, `1..4096` |
| `pg_local_cache.max_clients_per_worker` | `64` | Заранее выделенные slots/worker, `1..128` |
| `pg_local_cache.memory_budget_mb` | `256` | Startup budget extension memory, `64..8192 MiB` |
| `pg_local_cache.idle_timeout_ms` | `300000` | Idle/slow-client deadline |
| `pg_local_cache.statement_timeout_ms` | `2000` | Полный SQL command deadline |
| `pg_local_cache.lock_timeout_ms` | `250` | Lock wait deadline |
| `pg_local_cache.singleflight_wait_ms` | `25` | Максимальное ожидание concurrent loader, `0..1000` ms |
| `pg_local_cache.max_pipeline_commands` | `256` | Fairness budget команд за event-loop turn |
| `pg_local_cache.max_dirty_keys` | `4096` | Переход к relation invalidation |
| `pg_local_cache.auth_token_file` | empty | Production AUTH secret |
| `pg_local_cache.allow_superuser` | `off` | Только локальная разработка |
| `pg_local_cache.sql_cache` | `on` | `USERSET`: transparent ordinary-SQL fast path; restart не нужен |

Одна cache entry занимает примерно 8.6 KiB до hash overhead. Значение
`65536`, используемое Docker-профилем, требует около 0.55 GiB только для
cache. Каждый RESP slot содержит фиксированные request/output buffers;
`memory_budget_mb` учитывает shared hashes и все такие slots во всех workers и
останавливает postmaster с понятной ошибкой, если план больше budget. Он не
учитывает основной PostgreSQL, `shared_buffers`, `work_mem` и память обычных
backend-процессов, поэтому cgroup/container limit и запас всё равно обязательны. Увеличьте
`max_worker_processes` как минимум на число extension workers, не забирая
слоты у replication/parallel jobs.

`max_clients` не может быть больше `workers * max_clients_per_worker`.
Подключение резервирует общий slot атомарно до назначения socket клиенту;
любой setup error, disconnect или завершение worker возвращает reservation.
`max_dirty_keys` имеет hard maximum `16384`: после достижения лимита
транзакция переходит на relation-wide invalidation, сохраняя commit/rollback
целостность без неограниченного роста backend memory.

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

RESP batching объединяет только системные вызовы отправки, а не PostgreSQL
транзакции. Команды исполняются последовательно, input cursor сдвигается лишь
после постановки ответа в bounded output buffer, поэтому `EAGAIN` не приводит
к внутреннему replay `SET`/`DEL`. `max_pipeline_commands` ограничивает работу
одного клиента за event-loop turn и не является wire protocol limit.

SQL API:

```sql
SELECT local_cache.stats();
SELECT * FROM local_cache.metrics();
SELECT local_cache.health();
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

При одновременном cold `GET` первый worker получает versioned load lease.
Followers ждут не больше `singleflight_wait_ms`: если лидер успел, они берут
его результат без SQL; если нет, выполняют собственный SQL для ответа, но не
имеют права публиковать его поверх лидера. `0` отключает ожидание и уменьшает
head-of-line blocking, ценой возможных duplicate reads. Crash/FATAL лидера не
закрепляет entry навсегда: lease истекает, а новый generation fence запрещает
late fill и ABA после eviction/recreate.

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

Полный контрольный прогон PostgreSQL 16.14 с non-superuser role, 4 workers,
`cache_entries=65536`, 16 clients, pipeline 32, 16384 warm keys, value
128 bytes, 15 секунд warmup и 3 measured run по 120 секунд:

- median `239292 ops/s`, range `236072–243811`, CV `1.32%`;
- median p50 `1.693 ms`, p95 `4.265 ms`, p99 `6.951 ms`;
- `0` errors, `0` misses и `0` SQL reads во всех measured run.

До batching/input-cursor/context-reuse оптимизаций тот же полный workload дал
median `64918 ops/s`, p50 `6.082 ms`, p95 `16.388 ms`, p99 `26.943 ms`.
Наблюдаемый raw throughput вырос в `3.69x` (`+268.6%`), а p50/p95/p99
уменьшились на `72.2%`/`74.0%`/`74.2%`. Это два отдельных запуска на shared
двухъядерном GitHub runner. Поэтому результат является regression evidence
для этой оптимизации, а не универсальной гарантией для любого CPU/container.

CI повторяет короткий smoke gate `>=10000 warm GET/s`; перед production
rollout повторите 30–60 second gate несколько раз на закреплённых CPU целевой
машины и контролируйте p99. `SET`, `DEL`, cold miss и invalidation-heavy
workloads измеряйте отдельно: они включают стоимость PostgreSQL transaction,
storage, WAL и locks.

Когда включён ordinary-SQL lane, у него есть независимый fail-closed gate
`PGLC_BENCH_SQL_MIN_OPS` (default `10000`): проверяются не только ops/s, но и
ровно один `sql_cache_hit` на каждый успешный timed `SELECT`, ноль miss/fill/
bypass и отдельный cold miss→fill→hit proof.

Дополнительные методологически раздельные сценарии — cold/warm GET,
same-key stampede, RESP SET/DEL, direct/transparent prepared SQL, отдельный
unnamed extended-protocol SQL (`Parse/Bind/Execute` на каждый lookup),
обязательный ordinary-SQL cold miss→fill→hit, mapped/stock SQL writes и
post-commit validation — описаны в
[benchmarks/SCENARIOS.md](benchmarks/SCENARIOS.md).

Prepared и extended SQL результаты не объединяются. У extended lane свой
fail-closed порог `PGLC_BENCH_SQL_EXTENDED_MIN_OPS`; если он не задан, он
наследует `PGLC_BENCH_SQL_MIN_OPS` (по умолчанию те же `10000 ops/s`).

## Сравнение с Valkey, Redis и прямым PostgreSQL

В `benchmarks/` есть воспроизводимый comparative suite со следующими
зафиксированными целями:

- `pg_local_cache` на `postgres:16.14-bookworm`;
- `valkey/valkey:9.1.1-trixie`;
- `redis:8.8.1-trixie`;
- отдельный stock `postgres:16.14-bookworm` без загруженного расширения через
  `pgbench -M prepared` как SQL reference.

Для всех трёх RESP-сервисов используется один и тот же multiprocess Python
client, byte-identical `GET`, TCP через одну Docker network, одинаковые ключи,
value size, число persistent connections и pipeline. Каждый ответ проверяется.
Данные загружаются и прогреваются до таймера; у Valkey/Redis отключены RDB/AOF,
а у `pg_local_cache` каждый measured run обязан иметь ноль cache misses и SQL
reads. Порядок сервисов вращается между повторами. Key count должен делиться
на число connections, а keys на connection — на pipeline depth: harness
закрепляет за каждым process непересекающийся stride и тестом доказывает
полное, равновесное покрытие заявленного working set.

Полный запуск по умолчанию: 16 clients, pipeline 32, 16384 ключа по 128 bytes,
15 секунд warmup и три measured run по 120 секунд:

```bash
bash benchmarks/run.sh
```

Проверенный полный прогон 2026-07-31 на 2 CPU quota для client и каждого
server container:

| Target | Median ops/s | Min–max ops/s | CV | p50 | p95 | p99 |
|---|---:|---:|---:|---:|---:|---:|
| pg_local_cache | 239292 | 236072–243811 | 1.32% | 1.693 ms | 4.265 ms | 6.951 ms |
| Valkey 9.1.1 | 235349 | 233980–235787 | 0.33% | 1.886 ms | 4.363 ms | 6.850 ms |
| Redis 8.8.1 | 238019 | 235799–240762 | 0.85% | 1.849 ms | 4.358 ms | 6.694 ms |

`pg_local_cache` оказался на `1.7%` выше median Valkey и на `0.5%` выше
Redis, но такая малая разница находится внутри вариативности shared runner и
не является доказательством engine ranking. Полные отчёты сохранены в
репозитории: [до оптимизации](benchmarks/reference/2026-07-31-before-batching.md)
([JSON](benchmarks/reference/2026-07-31-before-batching.json)) и
[после оптимизации](benchmarks/reference/2026-07-31-transaction-safe-batching.md)
([JSON](benchmarks/reference/2026-07-31-transaction-safe-batching.json)).
Исходный полный прогон также доступен в
[GitHub Actions run 30660130760](https://github.com/aicopilot-fr/pg_local_cache/actions/runs/30660130760).

Число stock PostgreSQL `47974 ops/s` из этих исторических артефактов
считать валидным нельзя: runner ошибочно передавал `pgbench -d`, то есть
включал per-command debug logging. RESP-замеры `pg_local_cache`/Valkey/Redis
этим дефектом не затронуты. Флаг удалён, source-тест запрещает его возврат;
новое stock PostgreSQL и ordinary-SQL значение будет опубликовано только
после полного повторного прогона исправленного harness.

Результат сохраняется в `benchmarks/results/comparison.json` и
`comparison.md`: все отдельные run, median/min/max/CV, p50/p95/p99,
утилизация CPU client quota, версии, image tag и digest/ID, identity
benchmark-runner, Git revision и SHA-256 harness. Если client достигает 90%
своей CPU quota, report явно помечает результат как lower bound, непригодный
для engine ranking. Короткий smoke, который нельзя публиковать как performance
result:

```bash
PGLC_BENCH_DURATION=5 \
PGLC_BENCH_WARMUP_SECONDS=1 \
PGLC_BENCH_REPETITIONS=1 \
PGLC_BENCH_KEYS=1024 \
bash benchmarks/run.sh
```

По умолчанию сравнивается deployment profile с четырьмя
`pg_local_cache` workers. Для one-worker lane и latency без batching:

```bash
PGLC_BENCH_PG_LOCAL_CACHE_WORKERS=1 \
PGLC_BENCH_PIPELINE=1 \
bash benchmarks/run.sh
```

Stock PostgreSQL не смешивается с RESP-таблицей: `pgbench` использует другой
client и PostgreSQL extended protocol. Он выполняет тот же lookup по primary
key в prepared pipeline и показывает value lookups/s. SELECT внутри одного
pipeline разделяют implicit transaction/snapshot, поэтому этот результат
амортизирует больше SQL overhead и не считается wire-identical конкурентом.
RESP latency — время от отправки всего pipeline до завершения каждого ответа,
включая очередь за более ранними ответами, а не server service time одной
команды. p50/p95/p99 считаются по детерминированным per-connection reservoirs
Algorithm R (по умолчанию суммарно до 200000 samples) из всего measured
interval; при слиянии sample получает вес по числу завершённых операций
connection. Поэтому медленные connections не перевешивают быстрые, а хвост
прогона не вытесняет его начало.

Benchmark client по умолчанию ограничен `3 GiB`. Это учитывает пиковую память
Python при слиянии latency reservoirs и переходе к `pgbench`; лимит и число
samples можно изменить через `PGLC_BENCH_CLIENT_MEMORY` и
`PGLC_BENCH_MAX_LATENCY_SAMPLES`. Если процесс будет убит до записи JSON,
runner сохранит `process-failure.txt` для CI artifact.

Suite следует официальным рекомендациям не сравнивать разные хранилища
разными benchmark clients
([Valkey](https://valkey.io/topics/benchmark/),
[Redis](https://redis.io/docs/latest/operate/oss_and_stack/management/optimization/benchmarks/))
и использовать длинные повторные прогоны
([PostgreSQL pgbench](https://www.postgresql.org/docs/16/pgbench.html)).
CPU quota не означает CPU affinity, поэтому shared GitHub runner пригоден для
регрессий и артефактов, но не для маркетингового рейтинга. Для
publication-quality результата закрепите server/client на разных физических
CPU, исключите swap и фоновые процессы и повторите suite на целевом железе.

Warm GET также не сравнивает главную семантическую разницу: Valkey/Redis
хранят управляемую приложением копию, а `pg_local_cache` читает
PostgreSQL-owned row и автоматически инвалидирует её в транзакции. Отдельно
нужно измерять cold miss, writes, WAL и invalidation-heavy workloads.

## Тесты и CI

Полный Docker smoke собирает образ, поднимает чистый volume, проверяет health,
запускает расширенный integration suite и warm-cache benchmark:

```bash
PG_LOCAL_CACHE_SMOKE_DURATION=10 \
PG_LOCAL_CACHE_SMOKE_MIN_OPS=10000 \
bash tests/docker_smoke.sh
```

Отдельный tokenless smoke поднимает `compose.sql-only.yaml`, проверяет ноль
RESP workers и запускает transparent-SQL suite под реальным `LOGIN
NOSUPERUSER` пользователем:

```bash
bash tests/docker_sql_only_smoke.sh
```

Нативные source-level тесты компилируют реальный `src/resp.c` вне PostgreSQL и
проверяют parser/encoder, каждый корректный усечённый prefix, malformed input,
binary/NUL payload, граничные размеры и 30000 deterministic random,
mutation и truncation cases. Обычный и ASan+UBSan прогоны:

```bash
make source-test
make source-sanitize
make benchmark-test
```

Это 296215 C assertions в каждом режиме плюс unit tests самого comparative
harness. Wire limits подключаются из одного общего header, поэтому test и
production build не могут разойтись по `PGLC_REQUEST_MAX` или числу RESP
arguments.

Integration suite покрывает AUTH limits, malformed/fragmented/pipelined RESP,
точный порядок и resume после fairness yield, half-close, принудительный
socket backpressure без replay мутаций, GET/SET/DEL, positive/negative cache,
точные commit/rollback invalidation deltas, SQL/DDL/TRUNCATE invalidation,
отсутствие сброса на unrelated/TEMP DDL, key move, trigger integrity, typmod,
deterministic collation, value-type allowlist, remap fence, relation-state GC,
timeouts, каждый настроенный worker PID, full cache, race fence и 2PC
rejection.

Отдельный transparent-SQL black-box suite работает под реальной
`LOGIN NOSUPERUSER` role без `USAGE` на `local_cache`. Он проверяет cold
self-fill, warm hit, prepared parameter и `LIMIT 1`, `EXPLAIN`, session GUC,
отсутствующую строку, rollback/commit invalidation, own-write bypass,
`REPEATABLE READ` fallback, неподдерживаемый query fallback и сохранение ACL.

Workflow `.github/workflows/ci.yml` выполняет независимые профили на push,
PR и ручном запуске: source unit/sanitizers, односекундный Docker smoke всего
comparative stack и `pgbench`, correctness с cache `128`, включённым 2PC и
нестандартной database/role, а также throughput с production-профилем cache
`65536` и gate 10k. Отдельный
`.github/workflows/benchmark.yml` запускается вручную и ежемесячно, не
сравнивает конкурентов через pass/fail и публикует JSON/Markdown artifact.

## Наблюдаемость

`STAT` и `local_cache.stats()` возвращают:

- active/expired loading leases, positive/negative/dirty entries и relation
  states;
- hits, misses, negative hits, evictions;
- transparent SQL hits, misses, verified fills и safety bypasses;
- single-flight leader/waiter/reuse/timeout counters;
- database reads/writes и invalidations;
- active/rejected connections, AUTH/protocol failures, output backpressure
  events и slow-client drops;
- worker starts и pending forget markers.

Для частого scrape используйте `local_cache.metrics()`: это типизированная
однострочная O(1) функция, которая не сканирует все cache entries. Тяжёлый
`stats()` оставлен для диагностики. `health()` также O(1) и сообщает worker
quorum, client capacity и memory-budget invariant. Все три функции отозваны у
`PUBLIC`; отдельной monitor role выдаются только `USAGE` схемы и `EXECUTE`.

Готовый optional stack находится в `compose.monitoring.yaml`: отдельная
`local_cache_monitor` role, `postgres_exporter`, Prometheus rules и
provisioned Grafana dashboard. Создайте дополнительные secrets и запустите
overlay:

```bash
install -d -m 0700 secrets
openssl rand -base64 36 | tr -d '\n' > secrets/monitor_password
openssl rand -base64 36 | tr -d '\n' > secrets/grafana_admin_password
chmod 0444 secrets/monitor_password secrets/grafana_admin_password

docker compose -f compose.yaml -f compose.monitoring.yaml \
  up --detach --build --wait
```

Grafana доступна только на `127.0.0.1:3000`; exporter и Prometheus наружу не
публикуются. Opt-in profile `host-metrics` включает privileged cAdvisor для
реального container working set и OOM events. Подробности и команды проверки
alert rules: [monitoring/README.md](monitoring/README.md).

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
- RLS, table inheritance/partitions, foreign/temp/unlogged tables,
  nondeterministic collations и composite keys не поддерживаются; worker SQL
  дополнительно использует `ONLY`, чтобы поздно добавленный child не изменил
  семантику уже подготовленного mapping;
- mapped relation должна быть standalone: нельзя подключить ни parent с
  children, ни inheritance child, ни declarative partition;
- `ROLLBACK TO SAVEPOINT` после записи в mapped table может оставить только
  консервативный dirty marker до конца внешней транзакции. Это способно дать
  лишний bypass/invalidation (и консервативно отклонить 2PC), но не stale read;
- transparent SQL fast path дополнительно не принимает `bpchar` key и
  кеширует только точный scalar `SELECT value WHERE pk = Const/$1` с
  необязательным `LIMIT 1`; все прочие запросы штатно исполняет PostgreSQL;
- writable mapping не поддерживает generated/identity key/value columns;
- `SET` передаёт только key/value; другие `NOT NULL` columns требуют default;
- нет TTL, `MGET`, `MULTI/WATCH`, Lua, Pub/Sub, NX/XX/EX/PX и RESP3;
- namespace: 1–63 ASCII `[A-Za-z0-9_.-]`, key <256 bytes, value <=8192 bytes,
  request <=64 KiB;
- максимум 128 mappings; `1..128` client slots на worker и отдельный
  глобальный `max_clients`;
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
