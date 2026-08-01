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

- whole-row cache по умолчанию: одна entry хранит всю PostgreSQL-строку, а
  RESP `GET` возвращает её JSON-представление;
- KVik-style keys `CRUD:database.schema.table:{pk-json}` и составные primary
  keys из `1..16` колонок;
- RESP2: `AUTH`, `PING`, `ECHO`, `HELLO 2`, `GET`, `SET`, `DEL`,
  `INVALIDATE`, `STAT`, `INFO`, минимальные `CLIENT`, `COMMAND`, `SELECT 0`
  и `QUIT`;
- positive и negative cache в PostgreSQL shared memory;
- прозрачный fast path для обычного parameterized SQL lookup по primary key:
  `SELECT *` и прямые проекции колонок через тот же libpq/JDBC/ORM driver,
  без cache-specific client;
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
  стандартный B-tree equality opclass для каждой части PK;
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
проверяет PostgreSQL, extension, worker quorum, отсутствие incomplete mappings,
memory/client invariants, `AUTH` и `PING`.
Runtime-копия AUTH token хранится в отдельном `tmpfs` с mode `0600`, а не в
writable layer контейнера.

Production Compose ограничивает PostgreSQL container двумя GiB и одновременно
задаёт extension-owned budget 1024 MiB. Это разные границы: первая защищает весь
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

`POSTGRES_IMAGE` принимает только PostgreSQL 16 image; build явно отвергает
другую major version. Для воспроизводимого production build передайте
разрешённый вашей организацией digest вместо плавающего тега:

```bash
docker compose build \
  --build-arg POSTGRES_IMAGE=postgres@sha256:<approved-digest>
```

Создайте таблицу:

```sql
CREATE TABLE public.items (
    id bigint PRIMARY KEY,
    value text NOT NULL,
    enabled boolean NOT NULL DEFAULT true,
    metadata jsonb
);

GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE public.items TO app_user;
```

В Docker достаточно одной команды. Whole-row mode включён по умолчанию:
команда находит primary key в порядке его index columns, выдаёт
`local_cache_worker` необходимые права и вызывает административный API,
который сам создаёт и проверяет statement guard и row/truncate
`ENABLE ALWAYS` triggers:

```bash
docker compose exec postgres pg_local_cache_attach \
  --table public.items \
  --writable
```

Это административная команда: запускайте её только через trusted deploy/init
под bootstrap/extension owner. Доступ к Docker daemon или `docker exec` уже
эквивалентен root-доступу к этому контейнеру; `app_user` выполнять attach не
должен.

Namespace по умолчанию равен `schema.table`, то есть здесь `public.items`.
Флаг `--writable` включает RESP `SET`/`DEL`; без него mapping read-only.
Составной PK поддерживается непосредственно, если он содержит от 1 до 16
поддерживаемых колонок.

В `CRUD:` wire key используется реальная `database.schema.table`, а не
namespace. Namespace остаётся стабильным административным идентификатором
mapping, именем в метриках и prefix explicit scalar protocol.

Опция `--value-column` явно включает scalar mode. В нём кешируется одна
`NOT NULL` value-колонка, а convenience-команда требует одноколоночный PK:

```bash
docker compose exec postgres pg_local_cache_attach \
  --database app \
  --namespace item-values \
  --table public.items \
  --value-column value \
  --writable
```

Для defaults на sequence/function worker role дополнительно нужны права на
эти объекты. В read-only mode команда выдаёт `SELECT` и отзывает у worker
`INSERT`, `UPDATE`, `DELETE` на source table; detach отзывает table privileges
целиком, но не удаляет ранее выданный schema `USAGE`. Команда не
переназначит без явного `--replace` ни занятый другой таблицей namespace, ни
уже подключённую под другим namespace таблицу. SQL API следует тому же
fail-safe правилу: сначала вызовите `detach_table` для фактически подключённой
таблицы, иначе attach вернёт ошибку и не изменит существующий mapping,
triggers или ACL.

Эквивалентная ручная регистрация для native installation:

```sql
-- Whole-row mapping, namespace public.items, RESP-запись выключена:
SELECT local_cache.attach_table('public.items'::regclass);

-- Whole-row mapping с явными namespace и writable RESP API:
SELECT local_cache.attach_table(
    'public.items'::regclass,
    p_namespace => 'items',
    p_writable => true
);

-- Явный scalar mapping одной value-колонки:
SELECT local_cache.attach_value(
    'public.items'::regclass,
    p_value_column => 'value',
    p_namespace => 'item-values',
    p_writable => true
);

-- Снять mapping по таблице; false означает, что таблица не была подключена:
SELECT local_cache.detach_table('public.items'::regclass);

-- Восстановить triggers и ACL одного существующего mapping:
SELECT local_cache.reconcile_table('public.items'::regclass);

-- Атомарно сверить административную обвязку всех mappings:
SELECT local_cache.reconcile_all();
```

Основной API — одна функция `attach_table(p_relation regclass,
p_writable boolean DEFAULT false, p_namespace text DEFAULT NULL)`. Она находит
весь primary key, выдаёт dedicated worker role минимальные table/schema grants,
создаёт whole-row mapping и возвращает JSON с готовыми
key/GET/SET/DEL/INVALIDATE templates. Для приложения, которому действительно
нужно кешировать только одну колонку, есть отдельная и намеренно явная функция
`attach_value(p_relation, p_value_column, p_namespace, p_writable)` без
автоопределения value column. `detach_table(regclass)` симметрично удаляет
mapping, triggers и table grants и возвращает `true`, если mapping существовал.
`reconcile_table(regclass)` повторно применяет сохранённую конфигурацию одного
mapping, а `reconcile_all()` делает то же для всех mappings и возвращает их
число. Они восстанавливают отсутствующие или выключенные managed triggers и
права worker role, но не меняют namespace, PK/value columns или writable mode.

Attach, detach и reconcile сначала берут `ShareRowExclusiveLock` на уже
разрешённый OID таблицы и держат его до конца транзакции. Поэтому concurrent
rename/drop/recreate не может переключить административную операцию на другой
объект с тем же именем; `reconcile_all()` блокирует relations в порядке OID.

Три зарезервированных trigger получают dependency
`DEPENDS ON EXTENSION pg_local_cache`. Перед изменением API проверяет точные
имя, функцию, тип, arguments и эту provenance-связь. Foreign или unmarked
trigger с зарезервированным именем приводит к fail-closed ошибке и остаётся
нетронутым. Повреждённый, переименованный или выключенный trigger с доказанной
extension-owned dependency автоматически восстанавливается при
`attach_*`/`reconcile_*`; неизвестный объект API не удаляет и не заменяет.

Все функции attach/detach/reconcile/unregister административные: они имеют
`SECURITY DEFINER` и отозваны у `PUBLIC`. Вызывайте их от extension owner либо
выдавайте `EXECUTE` только доверенной deploy-role; обычному application role
они не нужны.

Source table должна быть application-owned permanent table. Attach отклоняет
схемы `pg_*`, `information_schema`, собственную схему расширения и relation,
входящую в любое PostgreSQL extension. Dedicated worker role также не может
владеть source table: она должна оставаться `NOSUPERUSER NOINHERIT` role с
только автоматически выданными ACL. Worker повторяет provenance-проверку при
каждой загрузке mappings, поэтому прямой restore/COPY в `local_cache.mapping`
не обходит эту границу.

### Drop-in SQL fast path без нового драйвера

После `attach_table` приложение может продолжить отправлять обычный SQL через
libpq, JDBC, Npgsql, psycopg или существующий ORM. Для fast path поддерживается
предсказуемая форма lookup по полному primary key:

```sql
PREPARE get_item(bigint) AS
SELECT *
FROM public.items
WHERE id = $1;

EXECUTE get_item(42);

SELECT metadata, value, id, value AS value_copy
FROM public.items
WHERE id = 42
LIMIT 1;
```

Whole-row mapping поддерживает `SELECT *`, любой непустой набор прямых table
columns, их перестановку, повторы и aliases. Predicate должен содержать ровно
по одному equality для каждой PK-колонки; порядок условий произвольный, справа
или слева допустим `Const` либо внешний parameter. Alias таблицы и
необязательный `LIMIT 1` поддерживаются. Для explicit scalar mapping через
`attach_value` projection должна содержать ровно mapped value column.

Join, дополнительные filters, sort, CTE, aggregate, row lock и expressions
над колонками не кешируются. `REPEATABLE READ`, `SERIALIZABLE`, RLS, recovery
и неподдерживаемая форма запроса также идут штатным планом PostgreSQL. Это
fail-open по производительности: расширение не меняет корректный SQL-результат.

Обычные межтиповые integer lookup тоже прозрачны: например, PostgreSQL
разбирает `bigint_id = 1` как `int8 = int4`. Fast path использует equality из
того же B-tree opfamily и безопасно расширяет `int2/int4` key expression до
типа PK; потенциально сужающие преобразования остаются на штатном плане.

На тёплом positive entry CustomScan восстанавливает native PostgreSQL row из
shared memory, после чего обычный executor формирует запрошенную проекцию. На
первом miss он выполняет сохранённый в плане штатный unique B-tree IndexScan,
проверяет `ctid` и raw `xmin` на свежем MVCC snapshot и только затем публикует
всю строку. Поэтому специальная SQL-функция, второй порт или cache-aware
driver не нужны.

RESP `GET` на cold miss тоже читает source table. Если строки нет, он
публикует negative entry, поэтому следующий RESP `GET` возвращает null без
повторного SQL до инвалидации. Для обычного SQL negative entry не является
доказательством отсутствия: запрос всё равно идёт в основную таблицу, а
transparent SQL path сам negative entries не публикует.

Закодированная whole-row cache entry ограничена `8192` bytes, включая header
и flattened tuple; worker по возможности сохраняет также готовый JSON. Если
строка не помещается, ordinary SQL возвращает её штатно, но не допускает в
cache. Cold RESP `GET` всё равно возвращает source row без admission, если её
JSON не превышает `64 KiB`; более крупный source value отклоняется до detoast/
JSON-render, а ответ содержит ошибку лимита, не усечённые данные.

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
SELECT * FROM public.items WHERE id = 42 LIMIT 1;
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

# PK можно не повторять в row JSON: wire key авторитетен.
redis-cli -h 127.0.0.1 -p 6380 -a "$PG_LOCAL_CACHE_TOKEN" --raw \
  SET 'CRUD:app.public.items:{"id":1}' \
  '{"value":"hello","enabled":true,"metadata":{"source":"resp"}}'

# Возвращается JSON всей строки, включая id.
redis-cli -h 127.0.0.1 -p 6380 -a "$PG_LOCAL_CACHE_TOKEN" --raw \
  GET 'CRUD:app.public.items:{"id":1}'
```

Обычный `AUTH <token>` и Redis ACL-style
`AUTH <username> <token>` поддерживаются. Во втором варианте `username`
обязан совпасть с настроенным `pg_local_cache.role`; это совместимость формы
команды, а не PostgreSQL password authentication. Обе формы открывают ту же
фиксированную worker role и тот же набор mappings.

`attach_table`, `attach_value`, `detach_table`, `reconcile_table` и
`reconcile_all` закрыты от `PUBLIC`. Выполняйте их от имени extension owner
либо выдавайте `EXECUTE` только полностью доверенной административной роли:
это `SECURITY DEFINER` API управляет triggers и правами worker role.
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

Эта role не должна владеть source tables и не должна получать права через
membership/PUBLIC. `attach_*` требует `NOSUPERUSER NOINHERIT NOCREATEDB
NOCREATEROLE NOREPLICATION NOBYPASSRLS`, проверяет ownership и выдаёт только
необходимые ACL на конкретную application table.

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

### Логическое восстановление

`local_cache.mapping` зарегистрирована как extension config, поэтому
`pg_dump` сохраняет строки с описаниями mappings. PostgreSQL roles принадлежат
всему cluster и не входят в dump отдельной database: сначала создайте
настроенную `pg_local_cache.role`, выдайте ей `CONNECT` и запустите workers
именно с этим именем. Role должна существовать до включения
`shared_preload_libraries` и рестарта PostgreSQL.

Восстановите весь dump, включая секцию post-data. Только после завершения
restore запустите от extension owner отдельную транзакцию reconciliation:

```sql
BEGIN;
GRANT USAGE ON SCHEMA local_cache TO local_cache_worker;
GRANT SELECT ON TABLE local_cache.mapping TO local_cache_worker;
SELECT local_cache.reconcile_all();
COMMIT;

SELECT (local_cache.health() ->> 'ready')::boolean AS ready;
```

Для custom `pg_local_cache.role` замените имя в двух `GRANT`.
`reconcile_all()` использует сохранённые описания, заново проверяет relations
и PK, восстанавливает managed triggers и права на source tables, затем
атомарно публикует mapping generation. Проверяйте `health()` после `COMMIT` и
допускайте трафик только когда `ready=true`; workers могут догружать новую
generation асинхронно. Ошибка provenance, отсутствующая relation или
изменившийся PK прерывает операцию без частично применённого commit.

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
| `pg_local_cache.memory_budget_mb` | `384` | Startup budget extension memory, `64..8192 MiB` |
| `pg_local_cache.idle_timeout_ms` | `300000` | Idle/slow-client deadline |
| `pg_local_cache.statement_timeout_ms` | `2000` | Полный SQL command deadline |
| `pg_local_cache.lock_timeout_ms` | `250` | Lock wait deadline |
| `pg_local_cache.singleflight_wait_ms` | `25` | Максимальное ожидание concurrent loader, `0..1000` ms |
| `pg_local_cache.max_pipeline_commands` | `256` | Fairness budget команд за event-loop turn |
| `pg_local_cache.max_dirty_keys` | `4096` | Переход к relation invalidation |
| `pg_local_cache.auth_token_file` | empty | Production AUTH secret |
| `pg_local_cache.allow_superuser` | `off` | Только локальная разработка |
| `pg_local_cache.sql_cache` | `on` | `USERSET`: transparent ordinary-SQL fast path; restart не нужен |

Одна cache entry занимает примерно 9.2 KiB до hash overhead. Значение
`65536`, используемое Docker-профилем, требует около 0.57 GiB только для
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

Основной wire key `1.0.0` совместим с моделью KVik:

```text
CRUD:database.schema.table:{"pk_column":<json-scalar>,...}
```

PK JSON должен содержать все и только primary-key fields. Порядок JSON members
не важен: server приводит значения к типам PostgreSQL и строит canonical key в
порядке PK index. Внутреннее canonical представление должно быть короче `1024`
bytes; общий RESP request ограничен `64 KiB`.

| Команда | Результат |
|---|---|
| `AUTH token` | аутентификация общим cache token |
| `AUTH username token` | та же аутентификация; username должен быть равен фиксированной worker role |
| `GET CRUD:db.schema.table:{pk-json}` | JSON всей строки либо RESP null; cold miss читает table |
| `SET CRUD:db.schema.table:{pk-json} row-json` | whole-row upsert, затем `OK` |
| `DEL CRUD:db.schema.table:{pk-json}` | удаление source row, `0` или `1` |
| `INVALIDATE CRUD:db.schema.table:{pk-json}` | exact-key invalidation |
| `INVALIDATE CRUD:db.schema.table` | table-wide invalidation |
| `INVALIDATE CRUD:db` | invalidation mappings текущей database |
| `INVALIDATE CRUD` | global invalidation всех mappings процесса |
| `STAT` | JSON native counters и KVik-compatible aliases |

Для whole-row `SET` wire key авторитетен: PK fields можно не передавать в
row JSON; совпадающие значения принимаются, несовпадающие отклоняются. Любое
неизвестное поле также отклоняется. Это полная запись, не JSON patch: все
неключевые columns проходят через `jsonb_populate_record`; пропущенное
неключевое поле становится `NULL`, а не сохраняет старое значение. Casts и
ограничения исходной таблицы остаются обязательными. Mapping должен быть
создан с `writable=true`.

Explicit scalar mode через `attach_value` использует ключи
`namespace:scalar-key` и возвращает одно textual value. Он предназначен для
узких value lookups; основной KVik-style API остаётся whole-row.
`INVALIDATE namespace` инвалидирует такой scalar mapping.

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
SELECT local_cache.detach_table('public.items'::regclass);
SELECT local_cache.reconcile_table('public.items'::regclass);
SELECT local_cache.reconcile_all();
```

`STAT` сохраняет подробные native counters и добавляет vocabulary KVik:
`store_size`, `store_memory`, `client_connect`, `client_disconnect`,
`client_requests`, `client_request_errors`, `client_gets`, `client_sets`,
`client_dels`, `cache_hit`, `cache_hit_in_main`, `cache_miss`,
`cache_neg_write_count`, `cache_evict`, `cache_invalidate_entry`,
`cache_invalidate_table`, `pass_to_main`, `sql_meta`, `sql_gets`, `sql_sets`,
`sql_dels` и `sql_result_reuses`. Это aliases над метриками
`pg_local_cache`, а не обещание побитово одинаковой внутренней статистики с
Postgres Pro KVik. `store_memory` — оценка занятых positive+negative fixed
slots, `cache_hit_in_main` — RESP hits в общем shared cache, а `sql_sets` и
`sql_dels` считаются отдельно от client-команд.

## Консистентность

Row trigger канонизирует весь OLD/NEW primary key и собирает изменённые ключи
в памяти backend-транзакции. В `XACT_EVENT_PRE_COMMIT`, до появления commit
visibility, extension резервирует dirty markers, увеличивает версии и делает
старые positive/negative whole-row entries невалидными. После commit marker
снимается; при abort снимается без публикации данных. Изменение любого PK field
инвалидирует и старый, и новый composite key.

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

В отличие от асинхронного WAL-consumer подхода, здесь invalidation является
частью той же транзакции на одном primary: завершившийся commit уже прошёл
pre-commit fence, а rollback не публикует invalidation. Цена этой более
сильной локальной границы — обязательные extension triggers и отсутствие
multi-primary/standby serving.

Гарантия зависит от исправных extension triggers/event trigger. Worker и SQL
path проверяют их имя, функцию, type, полный список PK arguments и
`ENABLE ALWAYS`; при расхождении mapping перестаёт обслуживаться. Row payload
содержит CRC32C и fingerprint tuple descriptor, поэтому старые/corrupt bytes
не декодируются после DDL как строка новой формы.

Event triggers перезагружают mappings для DDL, затрагивающего mapped relation
и его зависимые объекты. Изменение type/output semantics (`pg_type`,
`pg_proc`, `pg_cast`, `pg_collation`) консервативно инвалидирует весь cache,
чтобы ранее сохранённый row JSON не устарел, например после rename enum value.
Обычный и temporary DDL других таблиц не сбрасывает тёплый cache. Явный
`DROP INDEX` постоянного объекта тоже обрабатывается как global mapping change,
потому что PostgreSQL уже удалил связь index→table к моменту `sql_drop`.

Committed `DROP TABLE` удаляет mapping по точному OID в той же DDL-транзакции,
выполняет relation forget и освобождает namespace для нового attach. Rollback
`DROP TABLE` откатывает и удаление mapping. Таблица, позднее созданная под тем
же schema/name, получает другой OID и не подключается автоматически.

## Производительность и gate 10k ops/s

Обязательный `make load` gate измеряет explicit scalar hot path: только
успешные warm positive `GET namespace:key` одного textual value через
persistent connections и pipeline. Он требует ноль cache misses, SQL reads и
RESP errors и завершается с ошибкой при результате ниже
`PG_LOCAL_CACHE_MIN_OPS` (default `10000`). Whole-row RESP и ordinary-SQL
имеют отдельные gates, поэтому разные payload и protocol costs не смешиваются.

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

CI повторяет короткие независимые gates `>=10000 ops/s` для scalar GET,
whole-row GET, prepared ordinary SQL и unnamed extended ordinary SQL. Перед
production rollout повторите 30–60 second gate несколько раз на закреплённых
CPU целевой машины и контролируйте p99. `SET`, `DEL`, cold miss и
invalidation-heavy workloads измеряются отдельно: они включают стоимость
PostgreSQL transaction, storage, WAL и locks и не обязаны проходить read gate.

В explicit scalar suite ordinary-SQL lane имеет независимый fail-closed gate
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

`benchmarks/whole_row.py` сохраняет отдельные `whole-row.json` и
`whole-row.md`. В нём есть wire-identical `resp_full_row` для
`pg_local_cache`/Valkey/Redis, sweep размера whole-row payload и ordinary-SQL
lanes `select_star`, `reordered_projection` и
`composite_predicate_reordered` против stock PostgreSQL.

## Сравнение с Valkey, Redis и прямым PostgreSQL

`benchmarks/compare.py` — воспроизводимый explicit-scalar comparative suite,
а `benchmarks/whole_row.py` — основной KVik-style whole-row suite. Они
используют следующие зафиксированные цели:

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

Последний полный GitHub Actions прогон текущего harness дал следующие median
throughput values на shared runner:

| Lane | Target | Median ops/s |
|---|---|---:|
| Whole-row RESP GET | pg_local_cache | 106948 |
| Whole-row RESP GET | Valkey | 118387 |
| Whole-row RESP GET | Redis | 123790 |
| Ordinary SQL prepared | pg_local_cache cached fast path | 70275 |
| Ordinary SQL unnamed extended | pg_local_cache cached fast path | 18985 |
| Transactional RESP SET | pg_local_cache | 5456 |
| Transactional RESP DEL | pg_local_cache | 5520 |

Все read lanes прошли независимый gate `10000 ops/s`. `SET` и `DEL` приведены
отдельно и не сравниваются с in-memory writes Valkey/Redis: каждая операция
`pg_local_cache` включает настоящую PostgreSQL-транзакцию, WAL и commit-time
invalidation. Значения доступны в
[GitHub Actions run 30706923498](https://github.com/aicopilot-fr/pg_local_cache/actions/runs/30706923498),
но shared runner пригоден для regression evidence, а не для универсального
engine ranking или capacity promise.

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
PostgreSQL-owned source data и автоматически инвалидирует cache в транзакции.
Отдельно нужно измерять cold miss, whole-row encode/JSON, writes, WAL и
invalidation-heavy workloads.

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
binary/NUL payload, граничные размеры и deterministic random, mutation и
truncation cases. Отдельные contract tests проверяют canonical composite key,
versioned whole-row payload, CRC/descriptor guards, SQL API, planner и KVik
command surface. Обычный и ASan+UBSan прогоны:

```bash
make source-test
make source-sanitize
make benchmark-test
```

Это сотни тысяч C assertions в каждом режиме плюс unit tests benchmark
harnesses. Wire limits подключаются из одного общего header, поэтому test и
production build не могут разойтись по `PGLC_REQUEST_MAX` или числу RESP
arguments.

Integration suite покрывает AUTH обеих форм и limits,
malformed/fragmented/pipelined RESP,
точный порядок и resume после fairness yield, half-close, принудительный
socket backpressure без replay мутаций, GET/SET/DEL, positive/negative cache,
точные commit/rollback invalidation deltas, SQL/DDL/TRUNCATE invalidation,
отсутствие сброса на unrelated/TEMP DDL, key move, trigger integrity, typmod,
deterministic collation, value-type allowlist, remap fence, relation-state GC,
timeouts, каждый настроенный worker PID, full cache, race fence и 2PC
rejection.

Отдельный whole-row black-box suite проверяет JSON всей строки, composite PK,
canonicalization независимо от порядка JSON fields, omitted/matching/mismatched
PK в `SET`, unknown columns, четыре `INVALIDATE` scope, negative cache,
`SELECT *`, reordered projections, prepared parameters, transactional PK move,
oversized-row bypass и KVik `STAT` vocabulary.

Отдельный transparent-SQL black-box suite работает под реальной
`LOGIN NOSUPERUSER` role без `USAGE` на `local_cache`. Он проверяет cold
self-fill, warm hit, direct projections, prepared parameters и `LIMIT 1`,
`EXPLAIN`, session GUC,
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

Для частого scrape используйте типизированную однострочную
`local_cache.metrics()`: она O(1), не сканирует cache entries и включает как
основные counters, так и число workers, не загрузивших текущую mapping
generation. Тяжёлый JSON `stats()` оставлен для диагностики. `health()` также
O(1) и сообщает worker quorum, incomplete mappings, client capacity и
memory-budget invariant. Поэтому `ready=true` означает, что каждый настроенный
RESP-воркер уже загрузил текущую полную конфигурацию. Эти функции отозваны у
`PUBLIC`; из прав на объекты схемы `local_cache` отдельной monitor role
выдаются только `USAGE` и `EXECUTE` (плюс `CONNECT` к БД и стандартное
членство `pg_monitor` для PostgreSQL-метрик).

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
- committed `DROP TABLE` намеренно забывает mapping; recreated table нужно
  подключить заново, даже если schema/name совпали;
- attach/detach/reconcile берут `ShareRowExclusiveLock` до конца транзакции и
  могут ждать concurrent DDL/DML либо завершиться по настроенному
  `lock_timeout`;
- три trigger names `pg_local_cache_statement_guard`,
  `pg_local_cache_row_invalidate` и `pg_local_cache_truncate_invalidate`
  зарезервированы на каждой mapped table; foreign/unmarked trigger с таким
  именем вызывает fail-closed ошибку и не изменяется, а повреждённый
  extension-owned trigger автоматически пересоздаётся;
- source table не может находиться в `pg_*`, `information_schema` или схеме
  `pg_local_cache` и не может быть member-объектом любого extension; worker
  повторно проверяет это вместе с ownership managed triggers при загрузке;
- dedicated worker role не может владеть mapped table или быть superuser и
  должна оставаться `NOINHERIT NOCREATEDB NOCREATEROLE NOREPLICATION
  NOBYPASSRLS`;
- primary key из `1..16` `NOT NULL` колонок;
- в whole-row mode имена database, schema и table не могут содержать `.` или
  `:`, поскольку эти символы являются разделителями KVik wire key; explicit
  scalar mode адресуется через `namespace:key`;
- key types: `int2`, `int4`, `int8`, `text`, `varchar`, `bpchar`, `uuid`;
- PK должен быть valid/ready immediate non-partial B-tree primary index с
  default equality opclass для каждой колонки;
- RLS, table inheritance/partitions, foreign/temp/unlogged tables,
  views и nondeterministic key collations не поддерживаются; worker SQL
  дополнительно использует `ONLY`, чтобы поздно добавленный child не изменил
  семантику уже подготовленного mapping;
- mapped relation должна быть standalone: нельзя подключить ни parent с
  children, ни inheritance child, ни declarative partition;
- `ROLLBACK TO SAVEPOINT` после записи в mapped table может оставить только
  консервативный dirty marker до конца внешней транзакции. Это способно дать
  лишний bypass/invalidation (и консервативно отклонить 2PC), но не stale read;
- transparent SQL fast path требует точные equality clauses по всем PK fields
  и только прямые column projections; expressions, дополнительные predicates,
  joins и другие сложные формы штатно исполняет PostgreSQL;
- whole-row cache payload ограничен `8192` bytes: слишком широкая строка
  корректно bypass-ится в ordinary SQL и возвращается cold RESP `GET` без
  admission до лимита JSON `64 KiB`; сверх него RESP возвращает limit error;
- writable whole-row mapping поддерживает identity PK и generated non-key
  columns (`SET` передаёт identity из wire key и даёт PostgreSQL пересчитать
  generated fields), но generated PK запрещён;
- whole-row `SET` является полной заменой неключевых fields, а не partial
  patch; table casts, `NOT NULL`, checks, foreign keys и другие constraints
  применяются PostgreSQL;
- `attach_value` требует одноколоночный primary key и отдельную `NOT NULL`
  value column типа
  `int2`, `int4`, `int8`, `numeric`, `bool`, `text`, `varchar`, `bpchar`,
  `uuid`, `json` или `jsonb`; domain/enum/composite value запрещены;
- нет TTL, `MGET`, `MULTI/WATCH`, Lua, Pub/Sub, NX/XX/EX/PX и RESP3;
- namespace: 1–63 ASCII `[A-Za-z0-9_.-]`, точное имя `CRUD` зарезервировано;
  configured database должна совпадать с текущей; canonical PK <1024 bytes,
  row/scalar cache payload <=8192 bytes, request/row response <=64 KiB;
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

Контракт `1.0.0` закрывает центральную
[KVik-модель](https://postgrespro.com/docs/enterprise/current/proxima.html):
`CRUD:` key, JSON всей строки,
composite PK, `GET` fallback/negative cache, writable `SET`/`DEL`, четыре
уровня `INVALIDATE` и знакомые имена `STAT`. Это всё ещё не побитовая замена
Postgres Pro KVik: нет встроенного TLS (нужен proxy), обслуживаются одна
database и одна фиксированная role, нет cache mappings для views, standby
serving или multi-primary. TTL отсутствует и здесь, и в KVik-модели; это не
Redis replacement. `AUTH username token` не проверяет пароль обычного
PostgreSQL-пользователя, а KVik `STAT` names являются aliases над локальными
counters. Explicit scalar keys используют отдельный `namespace:scalar-key`
contract и не меняют основной whole-row wire format.

Архитектура использует штатные PostgreSQL
[background workers](https://www.postgresql.org/docs/16/bgworker.html),
[SPI](https://www.postgresql.org/docs/16/spi.html) и
[triggers](https://www.postgresql.org/docs/16/trigger-definition.html).
