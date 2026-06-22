# DDD-39: PostgreSQL LISTEN/NOTIFY Connector Mode

## Motivation

The existing Debezium PostgreSQL connector uses logical replication slots to capture change events. This is a robust and complete solution but carries non-trivial operational overhead: it requires a replication-enabled database user, consumes WAL resources, and mandates careful slot lifecycle management to prevent WAL accumulation.

Many PostgreSQL deployments already use `LISTEN`/`NOTIFY` as a lightweight internal signalling mechanism -- particularly for event-driven architectures where row changes trigger downstream processing. For these use cases, the full logical replication machinery is disproportionate.

This document proposes a `listen-notify` mode for the Debezium PostgreSQL connector using PostgreSQL's native `LISTEN`/`NOTIFY` protocol as the change capture transport. The design is built around a **stateless, horizontally scalable** connector model: all state lives in the database, every instance is identical, and no coordination between instances is required.

Related issue: [DBZ-2127](https://github.com/debezium/dbz/issues/2127)

---

## Goals

### Primary
- Provide a lightweight alternative to logical replication for `LISTEN`/`NOTIFY` workloads
- **Stateless connector instances** -- all state lives in `debezium_outbox`, no local instance state
- **Horizontal scaling** -- spin up any number of instances; load distributes automatically via `FOR UPDATE SKIP LOCKED`
- At-least-once delivery with a clear idempotency contract
- Connector manages the notify **function** lifecycle only -- trigger attachment is the user's responsibility
- Integrate as a mode of the existing PostgreSQL connector, not a separate connector -- modelled on the MySQL/MariaDB shared-base pattern

### Non-goals
- Full DDL capture
- Exactly-once delivery
- Replacement of the logical replication mode -- this is purely additive

---

## Core Design Principle: NOTIFY as Wake-Up Signal

The fundamental architectural decision is the strict separation of concerns between `NOTIFY` and the outbox:

```
NOTIFY  = wake-up signal only (broadcast, ephemeral, best-effort)
Outbox  = single source of truth (durable, ordered, exclusively claimed)
```

`NOTIFY` tells instances that work is available. The outbox table is where the work actually lives. This separation means:

- Instances carry **zero local state** -- nothing is lost if an instance dies
- `NOTIFY` being broadcast to all instances is **harmless** -- only one instance claims each outbox row
- `NOTIFY` being lost during downtime is **harmless** -- a polling interval sweeps the outbox as a safety net
- Adding or removing instances requires **no coordination** -- each instance independently races for outbox rows

---

## Proposed Solution

### Architecture Overview

```
+------------------------------------------------------------------+
|                      PostgreSQL Database                         |
|                                                                  |
|  +--------------+     +--------------------------------------+   |
|  |  User Table  |---->|  User-defined trigger                |   |
|  |              |     |  EXECUTE FUNCTION debezium_ln_notify |   |
|  +--------------+     +------------------+-------------------+   |
|                                          |                       |
|              +---------------------------v-------------------+   |
|              |         debezium_ln_notify()                  |   |
|              |     [connector-managed function]              |   |
|              |                                               |   |
|              |  1. pg_notify(channel, row_to_json payload)   |   |
|              |  2. INSERT INTO debezium_outbox               |   |
|              +---------------------------+-------------------+   |
|                                          |                       |
|                    +---------------------v-----------+           |
|                    |         debezium_outbox         |           |
|                    |     single source of truth      |           |
|                    |     all state lives here        |           |
|                    +---------------------+-----------+           |
+--------------------------------------------------+---------------+
                                          |
          +-----------------------+-------+-------+----------------+
          |                       |               |                |
          v                       v               v                v
+------------------+   +------------------+   +------------------+
|   Instance A     |   |   Instance B     |   |   Instance C     |
|                  |   |                  |   |                  |
|  LISTEN channels |   |  LISTEN channels |   |  LISTEN channels |
|  OutboxSweeper   |   |  OutboxSweeper   |   |  OutboxSweeper   |
|  ReconnectPolicy |   |  ReconnectPolicy |   |  ReconnectPolicy |
|                  |   |                  |   |                  |
|  stateless       |   |  stateless       |   |  stateless       |
|  identical       |   |  identical       |   |  identical       |
+--------+---------+   +--------+---------+   +--------+---------+
         |                      |                       |
         +----------------------v-----------------------+
                                |
                   FOR UPDATE SKIP LOCKED
                   one instance wins per row
                                |
               +----------------v----------------+
               |     Debezium Event Pipeline     |
               +---------------------------------+
```

---

### Code Structure

Modelled on the MySQL/MariaDB shared-base pattern:

```
PostgresConnector (existing base)
    +-- SlotBasedConnector          (existing -- logical replication)
    +-- ListenNotifyConnector       (new -- this DDD)
            +-- TriggerFunctionManager   -- function DDL lifecycle only
            +-- OutboxSweeper            -- primary processing engine
            +-- ReconnectPolicy          -- backoff + re-LISTEN on reconnect
            +-- PollingScheduler         -- fallback sweep when NOTIFY missed
```

Shared with the existing connector:
- Schema registry and evolution handling
- Event record formatting and Kafka producer integration
- Configuration parsing
- Offset storage and management

---

### Component Details

#### TriggerFunctionManager

The connector owns **only the function**. Trigger attachment -- which table, which operations -- is the user's responsibility. This keeps connector permissions minimal and gives users full control.

**On startup -- connector installs the function:**

```sql
CREATE OR REPLACE FUNCTION debezium_ln_notify()
RETURNS trigger AS $$
DECLARE
    payload jsonb;
BEGIN
    -- row_to_json(NEW) serialises the entire row after the change.
    -- row_to_json(OLD) serialises the entire row before the change.
    -- New columns appear in the payload automatically on schema change.
    payload := json_build_object(
        'connector',  TG_ARGV[1],
        'table',      TG_TABLE_NAME,
        'op',         TG_OP,
        'ts',         extract(epoch from clock_timestamp()),
        'id',         gen_random_uuid(),
        'before',     CASE WHEN TG_OP = 'INSERT' THEN NULL ELSE row_to_json(OLD) END,
        'after',      CASE WHEN TG_OP = 'DELETE' THEN NULL ELSE row_to_json(NEW) END
    )::jsonb;

    -- 1. fire NOTIFY as wake-up signal (best-effort, ephemeral)
    PERFORM pg_notify(TG_ARGV[0], payload::text);

    -- 2. write to outbox as durable record (always, regardless of active listeners)
    INSERT INTO debezium_outbox
        (connector_name, channel, table_name, op, payload)
    VALUES
        (TG_ARGV[1], TG_ARGV[0], TG_TABLE_NAME, TG_OP, payload);

    RETURN COALESCE(NEW, OLD);
END;
$$ LANGUAGE plpgsql;
```

**User attaches the function -- their choice of table and operations:**

```sql
-- INSERT only
CREATE TRIGGER orders_dbz_trigger
    AFTER INSERT ON orders
    FOR EACH ROW
    EXECUTE FUNCTION debezium_ln_notify(
        'dbz.orders',      -- channel name       (TG_ARGV[0])
        'my-connector'     -- connector name      (TG_ARGV[1])
    );

-- UPDATE only
CREATE TRIGGER invoices_dbz_trigger
    AFTER UPDATE ON invoices
    FOR EACH ROW
    EXECUTE FUNCTION debezium_ln_notify('dbz.invoices', 'my-connector');

-- all operations including DELETE
CREATE TRIGGER audit_dbz_trigger
    AFTER INSERT OR UPDATE OR DELETE ON audit_log
    FOR EACH ROW
    EXECUTE FUNCTION debezium_ln_notify('dbz.audit_log', 'my-connector');
```

**On shutdown -- connector drops only the function:**

```sql
DROP FUNCTION IF EXISTS debezium_ln_notify();
```

User-defined triggers are intentionally left intact on shutdown. The user created them; the user removes them.

---

#### debezium_outbox -- Single Source of Truth

All event state lives here. No connector instance holds any local event state.

```sql
CREATE TABLE debezium_outbox (
    id              UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    connector_name  TEXT        NOT NULL,
    channel         TEXT        NOT NULL,
    table_name      TEXT        NOT NULL,
    op              TEXT        NOT NULL,
    payload         JSONB       NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    consumed_at     TIMESTAMPTZ,
    idempotency_key TEXT        GENERATED ALWAYS AS (id::text) STORED
);

-- partial index on pending rows only -- stays small as table grows
CREATE INDEX debezium_outbox_pending_idx
    ON debezium_outbox (connector_name, created_at)
    WHERE consumed_at IS NULL;
```

---

#### OutboxSweeper -- Primary Processing Engine

The sweeper is not a recovery mechanism -- it **is** the processing mechanism. `NOTIFY` triggers an immediate sweep ahead of the polling schedule. The polling schedule ensures correctness when `NOTIFY` is missed.

```java
public class OutboxSweeper {

    public void sweep(String connectorName, int batchSize) {
        while (true) {
            List<OutboxRow> batch = claimBatch(connectorName, batchSize);
            if (batch.isEmpty()) break;

            for (OutboxRow row : batch) {
                try {
                    pipeline.emit(row);
                    // mark consumed ONLY after pipeline confirms delivery
                    markConsumed(row.id());
                } catch (Exception e) {
                    // do not mark consumed -- row retried on next sweep
                    log.warn("Failed to emit row {}, will retry", row.id(), e);
                }
            }
        }
    }

    private List<OutboxRow> claimBatch(String connectorName, int batchSize) {
        // FOR UPDATE SKIP LOCKED is the horizontal scaling primitive.
        // Each instance claims a distinct batch atomically.
        // No coordination between instances required.
        return jdbc.query("""
            SELECT * FROM debezium_outbox
            WHERE consumed_at IS NULL
              AND connector_name = ?
            ORDER BY created_at ASC
            LIMIT ?
            FOR UPDATE SKIP LOCKED
            """, connectorName, batchSize);
    }

    private void markConsumed(UUID id) {
        jdbc.update("""
            UPDATE debezium_outbox
            SET consumed_at = clock_timestamp()
            WHERE id = ?
            """, id);
    }
}
```

---

#### ReconnectPolicy

Uses pgjdbc-ng's `PGNotificationListener.closed()` callback, which fires when the connection drops due to Postgres restart, host reboot, or network failure. On reconnect, channel subscriptions are restored from configuration -- not from local memory -- preserving statelessness.

```java
connection.addNotificationListener(new PGNotificationListener() {

    @Override
    public void notification(int processId, String channel, String payload) {
        // dispatch off pgjdbc-ng I/O thread immediately.
        // blocking ops on this thread degrade notification throughput.
        dispatchExecutor.submit(() -> outboxSweeper.sweep(connectorName, batchSize));
    }

    @Override
    public void closed() {
        connected.set(false);
        scheduleReconnect(0);
    }
});

private void scheduleReconnect(int attempt) {
    long backoff = Math.min(initialBackoffMs * (1L << Math.min(attempt, 10)), maxBackoffMs);
    scheduler.schedule(() -> {
        try {
            // re-subscribe from config -- no local state involved
            connection = dataSource.getConnection().unwrap(PGConnection.class);
            connection.addNotificationListener(this);
            for (String channel : config.getChannels()) {
                connection.createStatement().execute("LISTEN " + channel);
            }
            connected.set(true);

            // sweep immediately to catch events missed during downtime
            outboxSweeper.sweep(connectorName, batchSize);
        } catch (Exception e) {
            scheduleReconnect(attempt + 1);
        }
    }, backoff, TimeUnit.MILLISECONDS);
}
```

---

#### PollingScheduler -- Correctness Safety Net

`NOTIFY` is best-effort. The polling scheduler ensures no event is permanently stuck regardless of notification delivery:

```java
// fires on fixed interval regardless of NOTIFY activity
scheduler.scheduleAtFixedRate(
    () -> outboxSweeper.sweep(connectorName, batchSize),
    0, pollIntervalMs, TimeUnit.MILLISECONDS
);
```

With polling in place, `NOTIFY` is a latency optimisation -- it makes delivery faster but is never required for correctness.

---

### Horizontal Scaling

Scaling requires no configuration change, no partition assignment, and no leader election. Each instance is identical.

```
Scale up:      start new instance -> begins listening and sweeping immediately
Scale down:    stop any instance  -> remaining instances claim its pending rows
Instance crash: unclaimed rows   -> other instances sweep them on next cycle
```

**Load distribution under concurrent instances:**

```
outbox contains 100 unprocessed rows

Instance A claims rows 1-25   (FOR UPDATE SKIP LOCKED)
Instance B claims rows 26-50  (FOR UPDATE SKIP LOCKED)
Instance C claims rows 51-75  (FOR UPDATE SKIP LOCKED)
Instance D claims rows 76-100 (FOR UPDATE SKIP LOCKED)

all four process in parallel -- no coordination required
```

---

### Trigger Maintenance

The connector installs and manages `debezium_ln_notify()` only. Users manage trigger attachment.

| Concern | Resolution |
|---|---|
| Function already exists on startup | `CREATE OR REPLACE` -- always idempotent |
| User removes trigger externally | Events stop arriving on that channel -- no connector impact |
| User adds new trigger at runtime | Events arrive on new channel -- connector picks up if channel is in config |
| Multiple connector instances | Function is shared -- no naming collision; outbox ownership scoped by `connector_name` |
| Required permissions | `CONNECT`, `EXECUTE` on `debezium_ln_notify()`, `INSERT` on `debezium_outbox` |

---

### At-Least-Once Delivery Guarantee

```
Row changes in user table
    -> trigger fires debezium_ln_notify()
    -> outbox row written (consumed_at = NULL)    <- durable, always
    -> pg_notify() fires                          <- best-effort, may be lost

If connector is live:
    -> NOTIFY wakes sweeper immediately
    -> sweeper claims row via FOR UPDATE SKIP LOCKED
    -> pipeline emits event
    -> consumed_at = now()                        <- confirmed

If connector is down during NOTIFY:
    -> NOTIFY lost (ephemeral)
    -> outbox row remains (consumed_at = NULL)    <- safe
    -> on reconnect: immediate sweep
    -> OR polling interval fires: sweep
    -> row claimed, emitted, consumed

If connector crashes after claim but before confirm:
    -> transaction rolls back
    -> row remains unclaimed
    -> next sweep on any instance reclaims it
    -> redelivered with same idempotency_key
    -> downstream deduplicates on idempotency_key
```

---

### Schema Evolution

`row_to_json()` serialises the full row at trigger fire time. Schema changes propagate automatically to the payload.

| Change | Behaviour |
|---|---|
| Column added | Appears in payload on next event -- no action needed |
| Column removed | Disappears from payload -- consumers must tolerate missing fields |
| Column renamed | Breaking change -- treated as remove + add, consumers require update |
| Table renamed | User must recreate their trigger on the new table name |
| Type changed | Postgres handles serialisation -- consumers must handle new JSON type |

Schema snapshots are published to the Debezium schema registry on connector startup and on detected schema changes via `information_schema.columns` comparison against a cached snapshot.

---

### Outbox Maintenance

Consumed rows are retained for a configurable period for debugging and replay, then purged:

```sql
DELETE FROM debezium_outbox
WHERE consumed_at IS NOT NULL
  AND consumed_at < now() - make_interval(days => :retentionDays);
```

---

## Configuration

```properties
# Enable listen-notify mode
debezium.source.plugin.name=listen_notify

# Channels to listen on -- must match TG_ARGV[0] in user-defined triggers
debezium.source.listen.notify.channels=dbz.orders,dbz.invoices,dbz.audit_log

# Connector name -- must match TG_ARGV[1] in user-defined triggers
debezium.source.listen.notify.connector.name=my-connector

# Reconnect policy
debezium.source.listen.notify.reconnect.initial.backoff.ms=1000
debezium.source.listen.notify.reconnect.max.backoff.ms=30000

# Outbox sweep
debezium.source.listen.notify.sweep.batch.size=100
debezium.source.listen.notify.poll.interval.ms=5000
debezium.source.listen.notify.outbox.retention.days=7
```

---

## Backward Compatibility

- No changes to slot-based connector behaviour
- New mode is opt-in via `plugin.name=listen_notify`
- Existing configuration semantics fully preserved
- Shared base connector code is refactored, not replaced

---

## Limitations vs Slot-Based Connector

| Capability | Slot-based | Listen/Notify |
|---|---|---|
| DDL change capture | Yes | No |
| Exactly-once delivery | Yes | No (at-least-once) |
| Replication privilege required | Yes | No |
| WAL overhead | Yes | No |
| Horizontal scaling | Via Kafka partitions | Via FOR UPDATE SKIP LOCKED |
| Trigger ownership | Connector | User |
| Connector state | Slot position | Stateless -- outbox is state |
| Operational complexity | Higher | Lower |

---

## Alternative Approaches Considered

**Pure LISTEN/NOTIFY without outbox:** No durability, no horizontal scaling, no at-least-once guarantee. Rejected.

**Stateful channel ownership per instance:** Requires coordination, leader election, rebalancing on scale events. Rejected in favour of stateless outbox model.

**Separate connector class:** Duplicates significant connector infrastructure. Rejected per maintainer guidance -- shared-base pattern preferred.

**Connector-owned triggers:** Requires `TRIGGER` privilege on every managed table. Rejected -- user ownership is lower privilege and more flexible.

**Polling only without NOTIFY:** Correct but higher latency. NOTIFY retained as latency optimisation with polling as safety net.

---

## References

- [DBZ-2127: Original feature proposal](https://github.com/debezium/dbz/issues/2127)
- [pgjdbc-ng Asynchronous Notifications -- section 9.2](https://impossibl.github.io/pgjdbc-ng/docs/current/user-guide/)
- [PostgreSQL NOTIFY](https://www.postgresql.org/docs/current/sql-notify.html)
- [PostgreSQL LISTEN](https://www.postgresql.org/docs/current/sql-listen.html)
- [PostgreSQL FOR UPDATE SKIP LOCKED](https://www.postgresql.org/docs/current/sql-select.html#SQL-FOR-UPDATE-SHARE)
- [Debezium MySQL/MariaDB connector](https://github.com/debezium/debezium/tree/main/debezium-connector-mysql)
