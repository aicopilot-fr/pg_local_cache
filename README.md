# pg_kvik

Экспериментальный RESP2-сервис и bounded row cache, работающие внутри
PostgreSQL как C-extension. Отдельный Valkey/Redis-процесс не нужен:
TCP listener, cache и SQL fallback находятся в background workers PostgreSQL.

Статус: **research alpha, не production-ready**. Реализация проверена на
PostgreSQL 16.14 под Linux. Это KVik-inspired прототип, а не wire-compatible
копия KVik.

## Что уже работает

- RESP2: `AUTH`, `PING`, `ECHO`, `HELLO 2`, `GET`, `SET`, `DEL`,
  `INVALIDATE`, `STAT`, `INFO`, минимальные `CLIENT`, `COMMAND`, `SELECT 0`
  и `QUIT`;
- positive и negative cache в PostgreSQL shared memory;
- bounded cache с вытеснением;
- parameterized saved SPI plans на cache miss и для записи;
- автоматическая инвалидация после SQL `INSERT`, `UPDATE`, `DELETE` и
  `TRUNCATE`;
- old/new key invalidation при изменении ключа;
- глобальный fence и reload mappings после DDL;
- fail-closed: namespace перестаёт обслуживаться, если обязательный trigger
  удалён или отключён;
- отклонение `PREPARE TRANSACTION`, если транзакция изменила mapped table;
- token/version fence, не позволяющий позднему cache fill опубликовать
  устаревший результат.

## Модель консистентности

Row trigger только собирает изменённые ключи в памяти backend-транзакции.
В `XACT_EVENT_PRE_COMMIT`, до появления commit visibility, extension:

1. блокирует cache hits для затронутых ключей;
2. увеличивает версии;
3. делает старые positive/negative entries невалидными.

После того как PostgreSQL публикует commit, `XACT_EVENT_COMMIT` повторно
инвалидирует entry и снимает блокировку. При abort блокировка снимается без
публикации данных транзакции. Cache miss запоминает версии до SQL `SELECT` и
заполняет cache только если за время запроса ни одна версия не изменилась.

Гарантия alpha: на одном primary `GET`, начавшийся после завершившегося SQL
или RESP commit, не должен вернуть более старое cached value. Пересекающийся
с commit `GET` может вернуть старое или новое значение. Эта гарантия действует
только пока служебные triggers и event trigger исправны.

## Сборка

Нужны PostgreSQL server headers той же major-версии, компилятор C и PGXS:

```bash
make PG_CONFIG=/path/to/pg_config
sudo make PG_CONFIG=/path/to/pg_config install
```

Добавьте настройки в `postgresql.conf` и перезапустите PostgreSQL:

```conf
shared_preload_libraries = 'pg_kvik'

pg_kvik.database = 'app'
pg_kvik.role = 'kvik_service'
pg_kvik.bind_address = '127.0.0.1'
pg_kvik.port = 6380
pg_kvik.workers = 2
pg_kvik.cache_entries = 4096
pg_kvik.auth_token = 'replace-with-a-long-random-token'
```

После restart:

```sql
CREATE EXTENSION pg_kvik;

CREATE TABLE public.items (
    id bigint PRIMARY KEY,
    value text NOT NULL
);

SELECT kvik.register_mapping(
    'items',
    'public.items',
    'id',
    'value',
    true
);
```

`pg_kvik.role` должен иметь только необходимые `SELECT`, а для writable
mapping — также `INSERT`, `UPDATE` и `DELETE` на mapped tables. Пустой role
означает bootstrap superuser и годится только для локальной разработки.

## Wire API

Wire key имеет вид `namespace:key`. Значение — текстовое представление одного
PostgreSQL column, не произвольный binary blob.

```bash
redis-cli -h 127.0.0.1 -p 6380 -a "$PG_KVIK_TOKEN" SET items:1 hello
redis-cli -h 127.0.0.1 -p 6380 -a "$PG_KVIK_TOKEN" GET items:1
redis-cli -h 127.0.0.1 -p 6380 -a "$PG_KVIK_TOKEN" DEL items:1
redis-cli -h 127.0.0.1 -p 6380 -a "$PG_KVIK_TOKEN" STAT
```

Основные команды:

| Команда | Результат |
|---|---|
| `GET namespace:key` | bulk value или RESP null |
| `SET namespace:key value` | `INSERT ... ON CONFLICT DO UPDATE`, затем `OK` |
| `DEL namespace:key` | `0` или `1` |
| `INVALIDATE namespace` | очищает весь namespace |
| `STAT` | JSON со счётчиками cache/SQL/invalidation |

Каждый `SET` и `DEL` — отдельная PostgreSQL-транзакция. Ответ отправляется
после commit. Если TCP-соединение оборвалось до ответа, outcome записи
неизвестен: автоматический retry может повторить операцию.

SQL API:

```sql
SELECT kvik.stats();
SELECT kvik.invalidate('items');
SELECT kvik.unregister_mapping('items');
```

## Ограничения alpha

- один primary, одна настроенная database и один фиксированный role;
- только permanent ordinary tables;
- один key column и один value column;
- key types: `int2`, `int4`, `int8`, `text`, `varchar`, `bpchar`, `uuid`;
- key/value должны быть `NOT NULL`;
- key требует full, immediate, single-column `UNIQUE` index;
- RLS, partitions, foreign/temp/unlogged tables и composite keys не
  поддерживаются;
- writable mapping не поддерживает generated/identity key/value columns;
- `SET` передаёт только key/value; другие `NOT NULL` columns требуют default;
- нет TTL, `MGET`, `MULTI/WATCH`, Lua, Pub/Sub, NX/XX/EX/PX и RESP3;
- namespace — 1–63 ASCII символа `[A-Za-z0-9_.-]`;
- canonical key меньше 256 bytes, value не больше 8192 bytes, request buffer
  64 KiB;
- максимум 128 mappings и 128 clients на worker;
- listener только IPv4; `workers > 1` требует `SO_REUSEPORT`;
- нет TLS. Token передаётся открытым текстом. Не выставляйте порт наружу без
  внешнего TLS и firewall;
- cache общий для workers, не реплицируется и очищается при restart/failover;
- standby и multi-primary не поддерживаются;
- все hits используют один global LWLock; eviction и global invalidation
  имеют линейную стоимость;
- SQL miss синхронно блокирует обслуживающий worker, а slow reader может
  задержать его до пяти секунд;
- superuser, отключивший event trigger или изменивший extension internals,
  находится вне модели доверия;
- C-extension работает внутри PostgreSQL: ошибка в нём способна вызвать
  crash recovery всего instance.

KVik использует ключи вида
`CRUD:database.schema.table:{"id":1}` и JSON всей строки. Текущая версия
pg_kvik использует `namespace:scalar-key` и один textual value column.
Полная KVik compatibility потребует отдельного mapping API, whole-row JSON и
composite primary keys; текущий протокол намеренно не маскируется под неё.

## Тесты

`tests/integration.py` ожидает уже установленный, preloaded и запущенный
instance:

```bash
PGKVIK_PSQL=/path/to/psql \
PGHOST=127.0.0.1 PGPORT=5432 PGDATABASE=postgres \
PGKVIK_RESP_PORT=6380 PGKVIK_AUTH_TOKEN=token \
make integration
```

Тест покрывает auth/handshake, fragmented и pipelined RESP, GET/SET/DEL,
positive/negative cache, SQL/DDL/TRUNCATE invalidation, rollback, key move,
RLS/index validation, trigger fail-closed, несколько mappings, 2PC rejection,
race fence, полный cache и multi-row transaction.

Архитектура опирается на штатные
[background workers](https://www.postgresql.org/docs/current/bgworker.html),
[SPI](https://www.postgresql.org/docs/current/spi.html) и
[triggers](https://www.postgresql.org/docs/current/trigger-definition.html).
Для сравнения: документация
[KVik](https://postgrespro.com/docs/enterprise/current/proxima.html) сама
помечает его как экспериментальную функцию.

