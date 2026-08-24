# DDD-61: Elasticsearch Sink Connector

> **Status:** Draft
> **Author:** Chris Cranford
> **Last updated:** 2026-08-24

---

## Motivation

Streaming change events into Elasticsearch is one of the most common CDC destinations, and today Debezium users reach that destination through a general-purpose Kafka-to-Elasticsearch sink.
Those connectors are built for arbitrary Kafka records, which is a reasonable design for the problem they set out to solve; it is simply not the CDC problem.

The consequences are consistent and well documented in user reports:

* **The envelope must be dismantled before it can be used.** `ExtractNewRecordState` has to be applied to flatten `before`/`after`/`op`/`source`, and then `tombstones.on.delete` on the source and `behavior.on.null.values` on the sink must agree, across two separately configured connectors, before a delete propagates at all.
  Misconfigure any one of the three and deletes either vanish or arrive as documents full of nulls.
  The `debezium-examples` repository ships exactly this multi-SMT arrangement as the expected setup.
* **Debezium logical types do not survive.** `org.apache.kafka.connect.data.Decimal` and `io.debezium.data.VariableScaleDecimal` reach the index base64-encoded (`"B8YV"` for `5094.61`), producing `number_format_exception`.
  Temporal logical types, `io.debezium.data.Json`, `io.debezium.data.Bits`, and the geometry types each need deliberate handling and do not get it.
  The common workaround, `decimal.handling.mode=string` at the source, degrades fidelity for every other consumer of that topic.
* **Index naming requires stacking SMTs.** The index name is the topic name, lowercased silently.
  Anything else means `RegexRouter` or `TimestampRouter`, which the Confluent connector supports only under `flush.synchronously=true`, at a documented throughput cost.
* **None of them run outside Kafka Connect.** A meaningful share of Debezium deployments are Debezium Server, frequently with no Kafka broker at all.
  For those users an Elasticsearch sink does not exist.

Debezium already ships JDBC and MongoDB sinks built on the shared `debezium-sink` abstractions.
An Elasticsearch sink completes that family, letting CDC run end to end within one ecosystem, one configuration model, and either runtime.

**A note on comparisons.** This document uses the Confluent Elasticsearch Service Sink (V1) and Elasticsearch Sink V2 as the feature baseline, because matching their capability surface is an explicit requirement (§ Goals).
Two citation rules apply throughout, and they are different rules:

* **Behaviour** is cited only from current published configuration references and documentation, never from issue titles.
  Where this document says V1 or V2 does something, that statement can be checked against a published page.
* **Links to the `confluentinc/kafka-connect-elasticsearch` tracker are cited as evidence of user demand, not as defect claims.**
  They establish that a capability is wanted and how strongly, which is what informs scope here.
  They are third-party reports against versions and deployments we have not reproduced, so nothing in this document asserts that a linked issue is a live defect, and none of it should be restated publicly as one without re-verification against a running instance.

The case for this connector rests on what it does for CDC users, not on comparison.

## Goals

* Consume the **Debezium change event envelope natively** (`op`, `before`, `after`, `source`) so that inserts, updates, deletes and truncates work with no SMT chain and no cross-connector tombstone configuration.
* Consume **non-Debezium structured events** (an arbitrary Kafka Connect `Struct`, `Map`, or schemaless JSON value) as a fully supported first-class mode, so the connector is usable as a general Kafka-to-Elasticsearch sink and not only behind a Debezium source.
* Provide **functional parity with the Confluent V1 and V2 Elasticsearch connectors**: every capability they expose is reachable here, through a Debezium-idiomatic configuration name where the Confluent name encodes a design we do not share.
  § "Feature parity with Confluent V1 and V2" is the checklist, and it records the three capabilities we consciously decline rather than reproduce (`max.in.flight.requests`, Elasticsearch 7.x, and CSFLE), each with its reason.
* Target **Elasticsearch 8.x and 9.x** through the typed `co.elastic.clients` Java API client.
  The deprecated High Level REST Client is not used.
* **Correct ordering with no configuration.** The connector must not destroy the per-key ordering Kafka already guarantees, and must not require the user to reason about versioning to get it.
* **Truthful task health in both directions.** A task that has stopped making progress must not report `RUNNING`; a task that hit a tolerable record-level error must not die.
* **Run unmodified in Kafka Connect and in Debezium Server**, following the pattern the JDBC sink established, with parity between the two as a release criterion.
* Preserve **Debezium logical type fidelity** end to end (decimals, temporals, JSON, UUID, bits, enums, geometry and vectors), and optionally derive the Elasticsearch mapping from the Connect schema rather than leaving it to dynamic inference.

## Non-goals

* **Configuration-name compatibility with the Confluent connectors.** We commit to functional parity and a documented migration mapping, not to inheriting names such as `key.ignore` or `external.resource.usage` that encode a generic-record design.
  A parity matrix and a migration table are deliverables; a drop-in config is not.
* **OpenSearch.** The APIs and clients have diverged far enough that pretending otherwise misleads users.
  If demanded, it belongs in a sibling module with its own client.
  Demand is real: [#583](https://github.com/confluentinc/kafka-connect-elasticsearch/issues/583) on the Confluent connector's tracker runs to 12 comments, so the answer is "a sibling module", not "no".
* **Elasticsearch 7.x.** A consequence of the 8.x client baseline (§10.1), not an oversight: compatibility is forward-only, so an 8.x client cannot address a 7.x cluster.
  Confluent V2 made the same cut (`elastic.server.version` accepts only `V8` and `V9`); V1 documented "7.x and later", so this is stated explicitly for readers migrating from it.
* **Client-Side Field Level Encryption.** V2 exposes `csfle.enabled` alongside `auto.register.schemas` and `use.latest.version`.
  CSFLE depends on Confluent Schema Registry and Confluent-licensed components, which Requirement 4 excludes.
  This is a declined capability rather than a converter concern, and is listed here so the parity matrix does not have to mis-file it.
* **Log/observability ingestion patterns** (append-only, time-partitioned, server-generated IDs) as the connector's centre of gravity.
  Data streams and generated IDs are supported for parity (§ "Resource naming, routing and resource types"), but the design optimises for CDC-shaped traffic: moderate volume, high value per record, mutable documents.
* **Reading from Elasticsearch.** This is a sink only.

## Requirements

1. The connector must never lose or silently corrupt a document.
   No condition that can do so may be handled by logging at `WARN` and continuing.
2. Every configuration property must be validated at startup, and a combination that cannot work must fail at startup with a message naming both properties, not at first write, and never by silently ignoring one of them.
   **A validation of this kind fires only on values the user set explicitly.** Where a connector-level default conflicts with something the user did choose, the default yields, the resolved value is logged at `WARN` at startup naming the property and the reason, and startup proceeds.
   No user should have to switch off a default in order to reach a documented feature; a default that forces that is a design defect, not a validation to be satisfied.
3. All Elasticsearch-specific behaviour must operate on `io.debezium.sink.DebeziumSinkRecord`, so that the same code path serves both runtimes.
4. The connector must not require Confluent Schema Registry, Avro, or any Confluent-licensed component.
5. Elasticsearch 8.x and 9.x must both be supported from a single distributed artifact.

---

## Proposed Changes

### 1. Architecture and module layout

Everything Elasticsearch-specific lives in one runtime-agnostic sink implementation built on the shared `debezium-sink` abstractions.
Each runtime contributes a thin adapter.

```
  Kafka Connect                                   Debezium Server
  ─────────────                                   ───────────────
  SinkRecord                                      BatchEvent (wraps SourceRecord)
      │                                                 │
  ElasticsearchSinkConnector                      ElasticsearchChangeConsumer
  ElasticsearchSinkConnectorTask                  (@Named("elasticsearch") @Dependent)
      │                                                 │ ChangeEventToSinkRecordConverter
      └──────────────────────┬──────────────────────────┘
                             v   Collection<SinkRecord>
              ElasticsearchChangeEventSink extends AbstractChangeEventSink
                             │
                             │  createSinkRecord()  -> KafkaDebeziumSinkRecord
                             │                        (envelope / flattened / CloudEvents aware)
                             │  buffer              -> io.debezium.sink.batch.DeduplicatingBuffer
                             │
                             v   doWriteBatch(Batch)
        ┌────────────────────┴─────────────────────────────────────────────┐
        │  RecordAdapter          envelope | flattened | plain -> Operation │
        │  DocumentIdStrategy     record -> _id                             │
        │  ResourceResolver       record -> index / alias / data stream     │
        │  DocumentConverter      Struct + Schema -> JSON (logical types)   │
        │  RoutingResolver        record -> _routing                        │
        │  PipelineResolver       record -> ingest pipeline                 │
        │  VersionStrategy        record -> ordering key (opt-in)           │
        │  MappingManager         Connect schema -> index template          │
        └────────────────────┬─────────────────────────────────────────────┘
                             v
              ElasticsearchBulkWriter  (co.elastic.clients.elasticsearch)
                             │   exactly one bulk request in flight per task
                             v
              BulkResponseClassifier
                   ├─ success
                   ├─ transient     -> AdaptiveThrottle, bounded retry
                   ├─ record-level  -> io.debezium.dlq.ErrorReporter
                   └─ fatal         -> fail fast with a diagnostic message
```

**Module layout.**

| Repository / module | Contents |
|---|---|
| `debezium-connector-elasticsearch` | **All connector work.** Everything in §2 through §13, operating on `DebeziumSinkRecord`; `ElasticsearchChangeEventSink extends AbstractChangeEventSink`; the Connect `SinkConnector`/`SinkTask` pair |
| `debezium-server/debezium-server-elasticsearch` | **Only the Debezium Server adapter and its scaffolding**: `ElasticsearchChangeConsumer`, `ChangeEventToSinkRecordConverter`, the Quarkus/native-image reflection configuration, `ComponentMetadataProvider` registration, and the module's own integration tests |

This split matches `debezium-server-jdbc` exactly and is deliberate: the adapter is thin enough that coupling this connector's release cadence to `debezium-server` costs little, while keeping the sink logic here means the Server path can never drift from the Connect path; the adapter delegates to `ElasticsearchSinkConnectorTask` rather than reimplementing anything (§14).

**Baseline.** The connector builds against Debezium **3.7.0-SNAPSHOT** and tracks it forward.
That baseline is at or above the `DeduplicatingBuffer` flush fix that §7.2 depends on for correctness, and includes the `io.debezium.dlq.ErrorReporter` machinery §9.2 uses.

**Stating the runtime-portability invariant correctly.** It is tempting to require that the core never import `org.apache.kafka.connect.*`.
That is not the pattern `debezium-sink` uses, and adopting it would put this connector at odds with the shared abstractions it is built on.
`io.debezium.sink.spi.ChangeEventSink.put()` takes `Collection<SinkRecord>`; `DebeziumSinkRecord` is defined in terms of Connect's `Struct` and `Schema` (with a `@TODO` in the upstream source acknowledging this); and `io.debezium.server.jdbc.JdbcChangeConsumer` delegates directly to `JdbcSinkConnectorTask`, converting each `BatchEvent` into a `SinkRecord` first.
Connect's data model is the shared currency of `debezium-sink` in both runtimes.

The invariant we *can* hold, and the one that actually buys runtime portability, is narrower and enforceable by an ArchUnit rule:

> No Elasticsearch-specific class may reference `org.apache.kafka.connect.sink.SinkTask`, `SinkTaskContext`, `ErrantRecordReporter`, or `org.apache.kafka.connect.runtime.*`.
> Connect *runtime* coupling is confined to the Connect adapter; Connect *data* types arrive only through `DebeziumSinkRecord` and the `io.debezium.dlq` abstraction.

### 2. Record model: two input contracts

The connector accepts two input contracts, resolved per record.
`event.format` selects the behaviour; `auto` is the default and is what almost every deployment should use.

| `event.format` | Meaning |
|---|---|
| `auto` (default) | Detect per record. A Debezium envelope, a flattened Debezium record, and a plain record are each recognised and handled by their own rules. |
| `debezium` | Require a Debezium envelope or flattened Debezium record. A plain record is a record-level error, not a silent pass-through. |
| `plain` | Treat every value as an opaque document body regardless of shape, even if it looks like an envelope. |

**Detection** reuses logic already present in `io.debezium.bindings.kafka.KafkaDebeziumSinkRecord` rather than reimplementing it:

* `isDebeziumMessage()`: the value schema name matches `Envelope.isEnvelopeSchema(...)`, or the schema is unnamed but carries an `op` field.
* `isFlattened()`: a non-tombstone record whose value is a `Struct` that is not an envelope.
* CloudEvents-wrapped values are unwrapped automatically via `cloud.events.schema.name.pattern`, so `debezium.format.value=cloudevents` and the Connect CloudEvents converter both work without additional configuration.

**`tombstone.mode`** = `auto` (default) | `ignore` | `delete` | `fail` governs what a null-valued record means.
`auto` derives the answer from the detected input contract rather than from a global switch, which is what the per-contract defaults in §2.1 and §2.3 describe: `ignore` on the envelope path, where the `d` event is authoritative, and `delete` on the flattened and plain paths, where the tombstone is the only delete signal.
The explicit values apply uniformly regardless of the detected shape.

#### 2.1 Debezium envelope

| `op` | Action |
|---|---|
| `c`, `r` | Write `after` (`index`, `create`, or `update`+`doc_as_upsert`, per `write.method`) |
| `u` | Write `after` |
| `d` | `delete` by `_id`, derived from the record key |
| `t` (truncate) | Per `truncate.mode`: `ignore` (default), `delete_by_query` on the resolved resource, `recreate`, or `fail` (§5.1) |
| `m` (message) | Ignored; logged at `DEBUG` |
| tombstone (null value) | Per `tombstone.mode`; `ignore` is the default on this path |

Ignoring tombstones by default on the envelope path is deliberate.
The `d` event is authoritative and already performed the delete; processing both produces a redundant delete whose ordering is indistinguishable from the real one.
This is the opposite default from the flattened and plain paths, where the tombstone *is* the only delete signal, so the default is derived from the detected shape, not from a global switch the user has to reason about.

#### 2.2 Flattened Debezium record (`ExtractNewRecordState` applied)

Users who already run the SMT chain must not have to unwind it.
When the record is flat but carries Debezium metadata, the connector uses it:

* `__op` / `__deleted` (or the configured `add.fields` prefix) determine the operation.
* `__source_db`, `__source_schema`, `__source_table`, `__source_ts_ms`, `__source_lsn` and friends are available to `ResourceResolver` and `VersionStrategy` exactly as `source` would be.
* Where the SMT emitted a tombstone rather than a `__deleted` marker, the tombstone drives the delete.

Metadata carried in **headers** rather than value fields (`ExtractNewRecordState` with `add.headers`) is read from `DebeziumSinkRecord.kafkaHeader()`.

#### 2.3 Plain structured event

Any Connect `Struct`, `Map`, or schemaless JSON value.
This is a supported mode, not a degraded one; it is how the connector serves non-Debezium topics.

| Record shape | Action |
|---|---|
| Non-null value | Write the whole value as the document body |
| Null value, non-null key (tombstone) | Per `tombstone.mode`: `delete` is the default on this path |
| Null value, null key | Per `tombstone.mode`; `fail` by default, because there is nothing to act on |

Schemaless values (`Map`, or `JsonConverter` with `schemas.enable=false`) are written through unchanged.
The logical-type conversions in §6 require a schema and are simply not applicable; the connector says so once at startup at `INFO` rather than pretending the conversions are in effect.

### 3. Document identity

`_id` is derived through the shared `primary.key.mode` / `primary.key.fields` properties already defined by `io.debezium.sink.SinkConnectorConfig`, so identity is configured the same way here as in the JDBC and MongoDB sinks.

| `primary.key.mode` | `_id` derivation | Confluent equivalent |
|---|---|---|
| `record_key` (default) | The record key. A primitive key is stringified; a `Struct` key uses the fields in `primary.key.fields`, or all key fields if unset, joined by `document.id.separator`. | `key.ignore=false` |
| `record_value` | Fields named by `primary.key.fields`, taken from the document body. | `key.ignore=false` + an SMT |
| `record_header` | Fields named by `primary.key.fields`, taken from record headers. | `external.version.header`-adjacent; no direct equivalent |
| `kafka` | `topic+partition+offset`. | `key.ignore=true` |
| `none` | No `_id` is sent; Elasticsearch generates one. | `use.autogenerated.ids=true` |

Rules and validations:

* `document.id.separator` (default `:`) joins composite key fields, in the order given by `primary.key.fields`, or in schema field order when that is unset.
  The order is fixed and documented, because a reordering silently changes every `_id` in the index.
* `primary.key.mode=none` **cannot** be combined with `delete.enabled=true`, `write.method=upsert`, or `write.method=update`: there is no identity to delete or update.
  Setting any of the three explicitly is a startup error.
  Both, however, hold defaults that would otherwise conflict (`delete.enabled=true` per §5, `write.method=upsert`), so per Requirement 2 they resolve instead to `delete.enabled=false` and `write.method=create`, with the startup `WARN` that requirement calls for.
  `primary.key.mode=none` is therefore usable on its own, which is what an append-only index wants, rather than demanding that the user first disable two unrelated defaults.
  It also forfeits idempotent redelivery, which is stated in the property description rather than buried in prose.
* `primary.key.mode=kafka` with `delete.enabled=true` is permitted but warned about at startup: a delete keyed on `topic+partition+offset` cannot address the document a previous offset created.
* An `_id` longer than Elasticsearch's 512-byte limit is a record-level error routed to the error handler with the offending length and the topic/partition/offset, not an opaque ES rejection.
* A null `_id` under any mode other than `none` is a record-level error.

`primary.key.mode` also drives batch reduction: `AbstractChangeEventSink` treats the buffer as keyed whenever `primary.key.mode != none`, which is exactly the condition under which per-`_id` reduction is meaningful (§7).

#### 3.1 Kafka metadata as document fields

Consuming the record key to derive `_id` consumes it *only* for that.
Users regularly want the key present in the document as well, either to search on it or because `_id` is not returned by every query shape, and it is a standing request on the Confluent connector's tracker ([#704](https://github.com/confluentinc/kafka-connect-elasticsearch/issues/704), "Capture Kafka key without using it as ID").
The same need covers the record's coordinates as an audit trail, which is a routine CDC requirement.

`document.metadata.fields` names which of `key`, `topic`, `partition`, `offset`, `timestamp` to project into the document body, and `document.metadata.prefix` (default `_kafka_`) namespaces them away from source columns.
Empty by default, so nothing is added unless asked for.
A `Struct` key is projected as a nested object; a primitive key as a scalar.
Projected fields participate in mapping generation (§6.3) like any other field, and are applied after `field.include.list` / `field.exclude.list` so an exclusion list written for source columns cannot accidentally strip them.

### 4. Resource naming, routing and resource types

#### 4.1 Naming

Naming reuses `collection.name.format` and `collection.naming.strategy` from the shared config, so `${topic}` remains the default and a custom `CollectionNamingStrategy` remains the extension point.
The Elasticsearch connector extends the placeholder vocabulary, which is what removes the need for routing SMTs:

| Placeholder | Source |
|---|---|
| `${topic}` | Topic name |
| `${source.db}`, `${source.schema}`, `${source.table}`, `${source.connector}` | The envelope `source` block, or the `__source_*` fields on a flattened record |
| `${field:path.to.field}` | A field in the document body, dotted path |
| `${header:name}` | A record header |
| `${date:pattern}` | The event timestamp formatted with a `java.time` pattern, in `resource.name.timezone` (default `UTC`). This is what `TimestampRouter` was being used for |

A placeholder that cannot be resolved for a given record is a record-level error, not an empty string spliced into an index name.

Elasticsearch index names are constrained (lowercase; no `\ / * ? " < > | ,` `#`, space; not starting with `-`, `_`, `+`; not `.` or `..`; ≤ 255 bytes).
The Confluent connector lowercases silently.
We do not:

`resource.name.invalid.handling` = `fail` (default) | `sanitize` | `error_handler`

* `fail`: a name that cannot be used stops the task at startup where the format is statically known, or is a record-level error where it is not, with the offending name and the specific rule it broke.
* `sanitize`: lowercase and replace disallowed characters with `resource.name.replacement` (default `_`), logging each *distinct* transformation once at `WARN`.
  Opt-in, because a silent rename can merge two logically distinct sources into one index.
* `error_handler`: route the record to the DLQ.

#### 4.2 Resource types

Confluent V2 replaced V1's `external.resource.usage` with an `auto.create` / `resource.type` pair.
We adopt the V2 shape, which is the better one:

| Property | Values | Default |
|---|---|---|
| `resource.type` | `index`, `alias_index`, `data_stream`, `alias_data_stream` | `index` |
| `resource.auto.create` | `true` / `false` | `true` |
| `topic.to.resource.mapping` | `topic1:resource1,topic2:resource2` | no default |

`topic.to.resource.mapping` is an explicit override consulted before `collection.name.format`, **unconditionally**.
This differs from V2, where the property applies only when `auto.create=false`.
V2 treats it as a way to address pre-created resources, and Confluent's own migration tool drops the property outright when auto-create is on.
Ours is an override of the naming strategy, which is the more useful reading and does not require the user to hold two properties in mind at once; § 13 records the difference so a migrating user is not surprised by a mapping that used to be a no-op.
V1's `max.external.resource.mappings` cap is not carried over: it exists to bound a cloud control plane, not to protect the connector.

When `resource.auto.create=true` the connector creates the index or data stream **and** applies the generated mapping (§6.3) as one step, and skips both if the resource already exists.
This matches V2, which combines the two steps, rather than V1, which runs resource creation and mapping application as independent flows.

**`resource.auto.create=false` means no existence call at all**, not "check but do not create".
Deployments that pre-create indices with their own analyzers and mappings have asked for exactly this against the Confluent connector, both to avoid an existence check per resource ([#877](https://github.com/confluentinc/kafka-connect-elasticsearch/issues/877)) and because that check behaves unexpectedly when writing through an alias ([#64](https://github.com/confluentinc/kafka-connect-elasticsearch/issues/64)).
Setting `false` asserts that the resource exists; if it does not, the first write fails with the Elasticsearch error, which is the outcome the operator asked for by disabling creation.

#### 4.3 Data streams

Data streams accept only `create` operations and require an `@timestamp` field.
That is fundamentally incompatible with update and delete propagation, and neither Confluent connector surfaces the conflict before the first write.
We do not:

* `resource.type=data_stream` or `alias_data_stream` forces `write.method=create`.
  Any other explicit `write.method` is a **startup** error.
* `resource.type=data_stream` combined with an **explicit** `delete.enabled=true`, or with any `truncate.mode` other than `ignore`, is a **startup** error.
  `delete.enabled` defaults to `true` here (§5), so on a data-stream target it resolves to `false` under Requirement 2 rather than failing a configuration the user never wrote.
  The startup `WARN` names the data stream and says that deletes and truncates cannot be propagated to one, which is the fact the operator needs.
* When the source is a Debezium envelope, a `u` or `d` event arriving on a data-stream target is a record-level error with a message that names the incompatibility, rather than an ES rejection.
* `data.stream.type` (`logs`/`metrics`/custom), `data.stream.dataset`, `data.stream.namespace` (default `${topic}`) compose the name as `{type}-{dataset}-{namespace}`, matching both Confluent versions.
  `data.stream.timestamp.field` names the field(s) to map onto `@timestamp`; the first present in the record wins; if unset, the Kafka record timestamp is used.
  V1's `NONE` sentinel for `data.stream.type` has no equivalent: the resource *kind* is `resource.type`, and `data.stream.*` simply does not apply when it is not a data stream.
  V2 reached the same conclusion, removing `NONE` and defaulting the property to `LOGS`.
* **The three-part name is a default, not a constraint.** `{type}-{dataset}-{namespace}` is a naming convention Elasticsearch encourages, not one it enforces, and users have asked for more latitude here ([#744](https://github.com/confluentinc/kafka-connect-elasticsearch/issues/744), "Data Stream naming is far too restrictive").
  When `collection.name.format` is set explicitly, it names the data stream directly and the three-part composition is bypassed, with the §4.1 placeholder vocabulary available as usual.
  The composed form remains the default because it is what a user who has not thought about it should get.
* **Time-series data streams (TSDS) are out of scope for v1.** A TSDS requires `index.mode: time_series`, declared dimension fields and a routing path, none of which is derivable from a Connect schema, and it further restricts write behaviour on top of the data-stream restrictions above.
  Users have reported reaching this only at runtime ([#723](https://github.com/confluentinc/kafka-connect-elasticsearch/issues/723)), so we detect `index.mode: time_series` on the resolved resource and fail at startup naming it as unsupported.
  A user who has authored a TSDS template deliberately can reach it through `mapping.mode=none`, at which point the connector is not making claims about the mapping.

#### 4.4 Routing and ingest pipelines

Both are long-standing requests against the Confluent connector, and both are one field on a bulk operation:

* `index.routing.field`: a dotted field path whose value becomes `_routing`.
  A record missing that field is a record-level error (routing silently defaulting to `_id` puts documents on the wrong shard, which is unrecoverable without a reindex).
* `ingest.pipeline`: a static pipeline name or a placeholder expression using the § 4.1 vocabulary, e.g. `pipeline-${source.table}`.
* `ingest.pipeline.validate` (default `true`): verify at startup that each statically-known pipeline exists.

### 5. Write semantics

| `write.method` | Bulk action | Notes |
|---|---|---|
| `upsert` (default) | `update` with `doc_as_upsert: true` | Creates or merges. Ordering is enforced by the scripted guard, not by native external versioning (§7.4). |
| `index` | `index` | Full replacement. Compatible with native external versioning (§7.4). |
| `create` | `create` | Fails if the document exists. Required for data streams. Compatible with native external versioning. |
| `update` | `update` without `doc_as_upsert` | Fails if the document does not exist. Ordering is enforced by the scripted guard. |

`upsert` is the default because a CDC stream that begins mid-table (a re-snapshot, a `snapshot.mode=schema_only` start, an incremental snapshot overlapping streaming) produces updates for documents the sink has never seen.
Under `index` those updates would each write a whole document built from the event's `after` block, so wherever the source emits a partial `after` the result is a document with fields missing rather than a document with fields merged.
`upsert` is also the semantics users expect from "keep this index in sync with that table".

Additional controls:

* `delete.enabled` (shared config, default `false` upstream).
  **We default it to `true`**, because a CDC sink that silently drops deletes is precisely the failure this connector exists to eliminate; the change of default is recorded in § Backward Compatibility.
* `write.method.per.operation`: an optional map, e.g. `c:create,u:upsert`, for the read-repair pattern where creates must not overwrite.
  Validated against the `write.method` constraints above.
* **`truncate.enabled` and `truncate.mode` come from different places, and it matters which one governs.**
  `truncate.enabled` is a shared property defined by `io.debezium.sink.SinkConnectorConfig` (boolean, default `false`), and `AbstractChangeEventSink.put()` gates on it directly: when it is `false` a `t` event is discarded before any Elasticsearch code runs.
  `truncate.mode` is introduced by this document; nothing in `debezium-sink` defines it.
  The two overlap, because `truncate.mode=ignore` is already the off switch, and leaving them free to disagree would let the inherited gate discard a truncate that the Elasticsearch configuration explicitly asked for.
  So `truncate.mode` governs, and `truncate.enabled` is **derived** from it: `false` when `truncate.mode=ignore`, `true` otherwise.
  An explicit `truncate.enabled` contradicting the derived value is a startup error naming both, per Requirement 2.
* **The inherited gate must not stay silent.** `AbstractChangeEventSink` discards a disabled truncate, and a disabled delete, at `DEBUG` and continues.
  That is below the bar Requirement 1 sets, and it is inherited behaviour rather than something this connector chooses.
  The connector compensates: a single startup `WARN` states that truncate or delete events will be discarded and names the property responsible, and discarded events are counted in the metrics of §11, so the condition is visible without turning on `DEBUG` logging.
  If the shared gate later reports itself, this compensation can be removed.

**`upsert` and `update` require `_source` on the target.** Both use the Update API, which reconstructs the document from `_source`; an index whose mapping disables it cannot serve them.
Users have reported reaching this only at write time ([#405](https://github.com/confluentinc/kafka-connect-elasticsearch/issues/405)).
We check it instead: before the first write to a resource, if `_source` is disabled and the effective `write.method` for that resource is `upsert` or `update`, the task fails with a message naming the resource, the `_source` setting, and the two write methods (`index`, `create`) that would work.
Where `resource.auto.create=true` and the connector generated the mapping, the condition cannot arise; the check exists for user-owned templates, which is exactly where it does arise.

#### 5.1 Truncate, and the acknowledgement it requires

`truncate.mode` = `ignore` (default) | `delete_by_query` | `recreate` | `fail`

| `truncate.mode` | On a `t` event | Gates it must pass |
|---|---|---|
| `ignore` (default) | The event is discarded and the resource is untouched. This is the value from which `truncate.enabled=false` is derived (§5), so the discard happens in the shared base class before any Elasticsearch code runs | none |
| `delete_by_query` | A `delete_by_query` against the resolved resource, issued only after the in-flight bulk request has been drained, with `wait_for_completion=true`, `conflicts=abort` and `refresh=true` | `truncate.allowed.resources` must match the resolved name, and `truncate.allow.wildcard` if the pattern is `*`-equivalent |
| `recreate` | The index is deleted outright and `resource.auto.create` rebuilds it from the generated template | The same allow-list gates, plus `resource.auto.create=true` and `mapping.mode != none`. Rejected at startup for `resource.type=alias_index` and for both data-stream types |
| `fail` | The task stops, for deployments that would rather handle a truncate manually | none |

Any value other than `ignore` is also a startup error when `resource.type` is a data stream (§4.3).
The rest of this section explains why the two destructive modes are gated the way they are.

A truncate is the only operation this connector performs that can destroy data it did not write.
`delete_by_query` removes every document in the resolved resource, including documents produced by another connector, another application, or a backfill.
Making that reachable by flipping one boolean is not proportionate to the consequence.

**The acknowledgement is an allow-list, not a confirmation flag.** `truncate.confirm.destructive=true` would be copy-pasted between configurations within a week and would stop carrying meaning; it can be satisfied without knowing what it authorises.
So:

* **`truncate.allowed.resources`**: required, with no default, whenever `truncate.mode` is `delete_by_query` or `recreate`.
  A comma-separated list of exact names or glob patterns matched against the **resolved resource name**, not the topic, because the topic is not what gets emptied, and §4.1 naming can map many topics onto one index.
  * Empty or unset while a destructive mode is configured is a **startup** error.
  * A truncate event whose resolved resource does not match any entry is a **record-level** error routed to the error handler, naming the resource and the configured list.
    It is not silently ignored: the source said "this table was truncated", and quietly diverging from that is worse than stopping.
* **`truncate.allow.wildcard`** (default `false`): a bare `*`, or any pattern that matches every resource the connector's naming configuration could produce, is rejected at startup unless this is set.
  Setting it logs a startup `WARN` enumerating the resources currently matched.

The property cannot be satisfied without stating the blast radius, which is what distinguishes an acknowledgement from a ceremony.

**Execution semantics.** A truncate is not a bulk item and must not be pipelined against one:

1. The in-flight bulk request is awaited and its response fully processed.
2. The buffer is drained: `DeduplicatingBuffer.truncate()` already discards pending records for that resource, since the truncate would erase them anyway.
3. The truncate is issued and **waited on to completion** (`wait_for_completion=true`), with `conflicts=abort` and `refresh=true`.
4. Only then does the next bulk request get built.

This preserves the one-request-in-flight invariant of §7.2 rather than carving out an exception to it.
`conflicts=abort` is chosen over the default `proceed` deliberately: a concurrent write during the scan makes the truncate fail loudly and stop the task, instead of applying to part of the index and reporting success.

**What `delete_by_query` still cannot promise, stated rather than hidden:** it deletes the documents visible in the snapshot taken when the query starts.
A document written by another producer between that snapshot and completion survives the truncate.
There is no setting that fixes this; it is a property of the API.

**`recreate`** deletes the index outright and lets `resource.auto.create` rebuild it from the generated template.
Where the connector exclusively owns the resource this is *safer* than `delete_by_query` (it is atomic, has no conflict window, and reclaims disk immediately), so it is offered as a first-class mode rather than an afterthought.
It requires `resource.auto.create=true` and `mapping.mode != none` (otherwise the recreated index loses its mapping), is rejected at startup for `resource.type=alias_index` and both data-stream types, and is subject to the same `truncate.allowed.resources` gate.

`fail` stops the task on any truncate event, for deployments that would rather handle it manually.

### 6. Type conversion and mapping

#### 6.1 Logical type registry

`DocumentConverter` walks the Connect schema and converts by logical type name.
This is the largest single fidelity gap against the Confluent connectors and it is closed by explicit handling rather than by a generic JSON writer.

| Connect / Debezium type | JSON output | Controlled by |
|---|---|---|
| `org.apache.kafka.connect.data.Decimal` | JSON number; string when the value exceeds IEEE-754 double precision, or always under `string` | `decimal.output.mode` = `numeric` (default) \| `string` \| `double` |
| `io.debezium.data.VariableScaleDecimal` | As above, reconstructed from `scale` + `value` | `decimal.output.mode` |
| `io.debezium.time.Date`, `connect.data.Date` | `yyyy-MM-dd` | `temporal.output.mode` |
| `io.debezium.time.Time`, `MicroTime`, `NanoTime`, `connect.data.Time` | `HH:mm:ss[.SSSSSSSSS]` | `temporal.output.mode` |
| `io.debezium.time.Timestamp`, `MicroTimestamp`, `NanoTimestamp`, `connect.data.Timestamp` | ISO-8601 (default) or epoch millis | `temporal.output.mode` = `iso8601` \| `epoch_millis` \| `epoch_nanos` |
| `io.debezium.time.ZonedTimestamp`, `ZonedTime` | Passed through; already ISO-8601 with offset | n/a |
| `io.debezium.time.Year` | Integer | n/a |
| `io.debezium.time.Interval`, `io.debezium.time.MicroDuration` | ISO-8601 duration string, or numeric micros | `interval.output.mode` = `string` (default) \| `numeric` |
| `io.debezium.data.Json` | **Embedded JSON**, not a quoted string | `json.output.mode` = `object` (default) \| `string` |
| `io.debezium.data.Xml` | String | n/a |
| `io.debezium.data.Uuid` | String (mapped `keyword`) | n/a |
| `io.debezium.data.Bits` | Base64 (default), boolean array, or integer where width ≤ 64 | `bits.output.mode` |
| `io.debezium.data.Enum` | String | n/a |
| `io.debezium.data.EnumSet` | Array of strings | n/a |
| `io.debezium.data.Ltree` | String | n/a |
| `io.debezium.data.geometry.Point`, `Geometry`, `Geography` | GeoJSON object (mapped `geo_point` / `geo_shape`) | `geometry.output.mode` = `geojson` (default) \| `wkb` |
| `io.debezium.data.vector.FloatVector`, `DoubleVector` | JSON array of numbers (mapped `dense_vector`) | `vector.output.mode` |
| `io.debezium.data.vector.SparseDoubleVector` | Object of index-to-value pairs (mapped `sparse_vector`) | `vector.output.mode` |
| Connect `BYTES` (no logical type) | Base64 or hex | `binary.output.mode` = `base64` (default) \| `hex` |
| Connect `STRUCT` | Nested JSON object | n/a |
| Connect `ARRAY` | JSON array | n/a |
| Connect `MAP` with string keys | `{"k":"v"}` compact, or `[{"key":..,"value":..}]` | `map.output.mode` = `compact` (default) \| `entries` (Confluent's `compact.map.entries`) |
| Connect `MAP` with non-string keys | `[{"key":..,"value":..}]`; compact form is impossible | n/a |

Two cross-cutting rules:

* `null.value.handling` = `omit` (default) | `write_null`.
  Omitting nulls keeps upserts from clobbering fields the source did not send; `write_null` is available for sources whose `after` block is always complete and where an explicit null must overwrite.
* `field.include.list` / `field.exclude.list` from the shared config apply before conversion, so excluded fields never reach Elasticsearch and never influence the generated mapping.

#### 6.2 Field name sanitisation

Elasticsearch rejects field names that are empty, exceed 255 UTF-8 bytes, or (in strict mappings) collide with a differently-typed existing field.
Dotted names create implicit object nesting, which is a real hazard for column names like `user.name`.

`field.name.adjustment.mode` = `none` (default) | `avro` | `avro_unicode` | `elasticsearch`, where `elasticsearch` replaces `.` with `field.name.separator.replacement` (default `_`).
This reuses the property name Debezium sources already expose, so the concept is familiar.

#### 6.3 Mapping management

Dynamic mapping infers each field's type from the first document that carries it, and Elasticsearch then enforces one type per field name per index forever.
That turns any schema evolution into `mapper_parsing_exception`.
Since we have the Connect schema, we can do better.

`mapping.mode` = `none` | `create_if_absent` (default) | `overwrite`

* `create_if_absent`: before first write to a resource, generate the mapping from the Connect schema and `PUT` it if nothing already covers the pattern.
* `overwrite`: always `PUT` the generated mapping.
  A template change does not retroactively alter existing indices; this is stated in the property description rather than left to be discovered.
* `none`: leave mapping entirely to Elasticsearch.
  This is a first-class escape hatch, not a degraded mode: a schema-derived mapping is a guess about search intent, and users who have already modelled their index know better than we do.

**The connector owns mappings. It does not own lifecycle.** `mapping.mode` generates a **component template containing only `mappings`**, named `debezium-<connector-name>-<resource>`, and composes it into a generated index template only when no user-authored template already matches the pattern.
Where one does, the connector ensures the component template exists and lets the user's template compose it; user templates always win on priority.
Two properties complete the seam:

* `mapping.composed.of`: a list of user-managed component template names to compose into the generated index template, ahead of the connector's own.
* `mapping.settings`: a narrow passthrough for the few index settings that materially affect a sink (`number_of_shards`, `number_of_replicas`, `refresh_interval`, `index.default_pipeline`).
  Empty by default.

**ILM, data-stream lifecycle, rollover aliases and retention policies are explicitly out of scope**, and `mapping.composed.of` is how they are reached: the user authors a lifecycle component template once, in Elasticsearch, and names it here.
That recipe is documented alongside the property.
Three reasons this is the right boundary rather than a deferral:

1. **It would raise the privilege floor for everyone.** Applying an ILM policy requires the `manage_ilm` cluster privilege; generating mappings requires only `manage_index_templates` and `auto_configure` on the index pattern.
   Owning lifecycle would make every deployment's connector credentials broader, including the deployments that never wanted the feature.
2. **ILM deletes indices.** A sink connector that can attach a delete-phase policy to an index as a side effect of a mapping decision is exactly the class of silent, unrecoverable action this design exists to eliminate.
3. **Nothing in the Connect schema implies a retention policy.** Mapping generation *derives* from information the connector actually holds.
   Lifecycle would be policy the user has to express in configuration regardless, and expressing it in Elasticsearch makes it reusable across connectors and visible to their existing cluster tooling, rather than trapped in one connector's config.

A reindex-assist mode that *acts* on an incompatible mapping change, rather than only diagnosing it, is recorded under § Future Work.

Connect schema to Elasticsearch field type mappings:

| Connect schema | ES type |
|---|---|
| `INT8`/`INT16` | `short` |
| `INT32` | `integer` |
| `INT64` | `long` |
| `FLOAT32` | `float` |
| `FLOAT64` | `double` |
| `BOOLEAN` | `boolean` |
| `STRING` | `text` with a `keyword` sub-field, or `keyword`, per `string.mapping.mode` |
| `BYTES` | `binary` |
| `Decimal` | `scaled_float` with `scaling_factor` from the schema scale, or `keyword` under `decimal.output.mode=string` |
| Temporal types | `date` with the matching `format`, or `date_nanos` for nanosecond precision |
| `Json` | `object` (or `flattened` under `json.mapping.mode=flattened`) |
| `Uuid`, `Enum`, `Ltree`, `Xml` | `keyword` |
| `Point`/`Geometry`/`Geography` | `geo_point` / `geo_shape` |
| `FloatVector`/`DoubleVector` | `dense_vector` with `dims` from the schema |
| `SparseDoubleVector` | `sparse_vector` |
| `STRUCT` | `object`, or `nested` per `struct.mapping.mode` |

`mapping.dynamic` (default `strict_but_dlq`) sets `dynamic` on the generated mapping:

* `strict_but_dlq` sets `dynamic: strict` in Elasticsearch, so an unmapped field is rejected rather than silently typed; the connector catches `strict_dynamic_mapping_exception` and routes the record to the error handler naming the unmapped field.
  This is the "silent beats neither" position: an unexpected field is surfaced, not guessed at.
* `true` / `false` / `runtime` are passed through to Elasticsearch.

**Compatible evolution.** The Confluent connectors advertise support for backward, forward and fully compatible schema changes, so the behaviour has to be stated rather than left implicit.
What a schema change does here depends on the change and on `mapping.mode`:

| Schema change | `mapping.mode=create_if_absent` | `mapping.mode=overwrite` | `mapping.mode=none` |
|---|---|---|---|
| **Field added** | Component template regenerated and applied on next resolution; the new field is mapped from its Connect type | Same, applied every time | Depends on `mapping.dynamic`: under the `strict_but_dlq` default the record is routed to the error handler naming the unmapped field, rather than being guessed at |
| **Field removed** | No mapping change; Elasticsearch retains the mapping for absent fields, and documents simply omit it | Same | Same |
| **Type widened** (`INT32` to `INT64`, `FLOAT32` to `FLOAT64`) | Detected as a conflict and reported: Elasticsearch cannot widen a field in place | Same | Surfaces as a `document_parsing_exception` per record, routed by §9.1 |
| **Type narrowed or changed** | Same conflict path | Same | Same |
| **Field made optional / default added** | No mapping impact; `null.value.handling` governs whether the field is written | Same | Same |
| **Nested struct added** | Mapped per `struct.mapping.mode` | Same | Per `mapping.dynamic` |

The short version: **additive changes are absorbed, type changes are not**, and that is a property of Elasticsearch, not of this connector.
Field additions are the overwhelmingly common case in CDC (a new column), which is why the default mode handles them silently and the type-change case gets a diagnostic rather than a retry loop.

**Incompatible evolution.** When a generated mapping conflicts with the existing one, the connector detects it at template-application time and fails with a message naming the field, the existing type and the incoming type, rather than emitting one raw ES parse error per document forever.
Elasticsearch cannot change a field's type in place; the connector says so and names reindexing as the remedy instead of retrying.

`schema.ignore` / `topic.schema.ignore` from the Confluent connectors map onto `mapping.mode=none`, globally or via a per-topic override (§12.9).

### 7. Ordering and delivery

#### 7.1 What is already guaranteed

Debezium keys each change event by the row's primary key (or `message.key.columns`), and the default partitioner hashes that key.
Every event for a given row therefore lands in one partition, and Kafka preserves order within it.
Kafka Connect assigns each partition to exactly one task and delivers records to `put()` in per-partition order.
**Every event for a given `_id` arrives at a single task, in order.** In Debezium Server the guarantee is simpler still: the engine delivers to one `ChangeConsumer` in source order, with no partitions or rebalancing.

Order is not preserved *across* keys, but different keys are different documents and their relative order is irrelevant to a document store.

The connector's obligation is therefore not to establish ordering, only to avoid destroying it.
There are exactly two ways it could:

1. **Pipelining.** Issuing bulk request N+1 before N has responded lets the network and Elasticsearch apply them out of order.
   This is the `max.in.flight.requests` pattern (V1 defaults to 5) and is the entire source of the well-known corruption scenario: records that arrived in perfect order, reordered by the connector itself.
2. **Partial-failure retry.** Within one bulk request, items for the same `_id` route to the same shard and apply sequentially, so a single request is order-safe as sent.
   But if item *i* for `_id` X is rejected (a 429, say) while a later item *j* for the same `_id` succeeds, retrying *i* applies older state over newer.

#### 7.2 Decision

* **One bulk request in flight per task.** The next request is built only after the previous response is fully processed.
  Ordering then holds by construction: no `_id` tracking, no external versioning, no reconstruction.
  Throughput is `batch_size / round-trip latency`: a 2,000-document batch at ~100 ms is ~20k docs/sec per task, comfortably beyond typical CDC volume.
  Additional throughput comes from partitions and tasks, which scale without touching ordering.

  `max.in.flight.requests` is therefore **not offered**.
  This is a deliberate omission, not an oversight: adding it later would require re-solving ordering, and it is the one Confluent property whose functionality we consciously decline to reproduce.
  It is called out as such in the parity matrix.

* **Reduce each batch to at most one write per `_id`.** This uses the existing `io.debezium.sink.batch.DeduplicatingBuffer`, which `AbstractChangeEventSink` already selects by default (`keyed.message.batch.mode=deduplication`) whenever `primary.key.mode != none`.
  It is last-write-wins per key within the batch, which is exactly right for a document store: Elasticsearch holds current state, so intermediate states of a key inside one batch have no observable value.
  Beyond the throughput win on hot keys, this removes hazard (2) entirely: with one item per `_id` per request, a retried item cannot be overtaken by a sibling.

* **`keyed.message.batch.mode=passthrough` is rejected at startup.** For the JDBC sink, passthrough is a legitimate default and reduction is a throughput optimisation.
  Here, reduction is part of the correctness argument.
  Supporting passthrough would reintroduce hazard (2) as a configuration.
  If it is ever needed, it must arrive with its own answer for partial-failure retry ordering.

* **A create-through-delete sequence reduces to a delete, not to nothing.** The buffer's last-write-wins semantics give this for free: `c,u,d` retains the `d`.
  This is the correct behaviour against Elasticsearch: a relational sink can infer that `insert+update+delete` needs no operation because the row demonstrably did not exist beforehand, but an index may still hold a document for that `_id` from an earlier run, a re-snapshot, or key reuse.
  Deleting an absent document is benign (a bulk item `result: not_found`, treated as success), so always emitting the terminal delete is the safe reduction.
  The corresponding assertion is a required test (§ Testing).

* **`version.strategy=none` is the default.** No versioning of either kind, no version conflicts to swallow, no source-position normalisation, and no per-document script on the write path.
  Deletes need no special handling because they are ordered like everything else.

#### 7.3 When `_id` is not the record key

Deriving `_id` from a payload field is legitimate (`primary.key.mode=record_value`), but it breaks the alignment everything above depends on: events for one document now live in several partitions, and Kafka never ordered them relative to each other.
Two mechanisms suggest themselves and neither works:

* **Restricting to one task** removes concurrent writers but does not establish ordering.
  The task polls multiple partitions and `put()` interleaves them arbitrarily, so the reduction buffer selects an arbitrary event as terminal rather than the newest.
  It replaces a race with a deterministic-looking but equally arbitrary outcome (worse, because it conceals the problem) while capping throughput at one task.
* **A cross-task coordinator** provides mutual exclusion, which is not ordering.
  Holding a lock on `_id` X still leaves open which of two events is newer, and that answer is not in the stream.
  Concurrency is not the harm here: Elasticsearch serialises writes per document per shard, so multiple tasks never produce a torn document.
  The harm is that the newest event may not win.

The missing ordering information exists in exactly one place: the source database's LSN/SCN/GTID in the envelope's `source` block.
Elasticsearch external versioning exists precisely to resolve out-of-order arrivals from such a value.
So:

* When `primary.key.mode` is not `record_key`, the connector **requires an explicit ordering decision at startup**: `document.id.non.key.ordering` = `version` (use `version.strategy`, enforced by whichever mechanism §7.4 selects) or `last_write_wins` (acknowledge arbitrary resolution).
  There is no default.
  Misconfiguring this silently is exactly the failure mode this connector exists to eliminate.
  `document.id.non.key.ordering=version` additionally requires `version.strategy` to be something other than `none`, since `none` supplies no ordering key: the pair is a startup error naming both, rather than a `version` setting that quietly degrades to last-write-wins.
  `version.strategy` has no default here beyond the global `none`, so this is a combination a user reaches by setting one property and forgetting the other.
* The same validation applies when a `PartitionRouting` SMT repartitions on a non-key field, which the connector detects from the SMT chain where it can and documents where it cannot.
* **Documented limits:** LSNs are comparable only within one source database, so `_id` collisions spanning different source databases have no recoverable order and are unsupported.
  Several topics feeding one index with colliding `_id`s is likewise undefined by construction.

#### 7.4 External versioning, where it applies

`version.strategy` = `none` (default) | `source_lsn` | `source_ts_ms` | `record_header` | `kafka_offset`

* `source_lsn` derives the ordering key from the source commit position. The name is historical; the strategy covers every source position type, not only write-ahead-log LSNs.
* `record_header` reads a numeric header, matching Confluent's `external.version.header`.
* `kafka_offset` reproduces the Confluent default.
  It is offered for migration parity and its description states plainly that offsets are comparable only within a partition, so it adds nothing where ordering was already guaranteed and is not meaningful where it was not.

**Two enforcement mechanisms, because one `long` is not always enough.**

Elasticsearch offers two ways to make a write conditional on the incoming record being newer, and they differ in more than performance:

| | Native external versioning | Scripted guard |
|---|---|---|
| Mechanism | `version` plus `version_type=external` on an `index` operation | `update` carrying a Painless script that compares a stored key field |
| Ordering key | A single non-negative `long`, ceiling `Long.MAX_VALUE` | Any shape: a `long`, or an ordered tuple of them |
| Compatible `write.method` | `index`, `create` | `upsert`, `update` |
| A stale write becomes | `version_conflict_engine_exception` | `ctx.op = "noop"` |
| Cost | None beyond the write itself | One script execution per document; requires `_source` (§5) |

The Update API does not accept `version_type=external`, so the native mechanism cannot protect `upsert`, which is our default (§5).
The scripted guard has the inverse property: it operates *through* the Update API, so it is the mechanism that protects the default write path.
Neither alone covers the whole surface, so the connector offers both.

`version.enforcement` = `auto` (default) | `external` | `script` selects between them.
`auto` chooses `external` when the ordering key normalises to a single `long` and `write.method` is `index` or `create`, and `script` in every other case.
The explicit values force the choice, and a combination that cannot work is a startup error naming both properties.

**The ordering key does not have to be a scalar.** Source positions are not uniformly 64-bit, and requiring that they be is what makes several sources unimplementable:

| Source | Native position | Fits one `long`? | Tuple form |
|---|---|---|---|
| Postgres | 64-bit LSN | Yes | not needed |
| Oracle | SCN, modelled as `BigInteger` in Debezium | Usually | `[commitScn, scn, eventOrdinal]`, since an SCN alone does not order events within a transaction |
| MongoDB | Resume token; `clusterTime` is 32-bit seconds plus 32-bit increment | Replica set only | `[clusterTimeSeconds, increment]` |
| SQL Server | 10-byte LSN (VLF sequence : log block offset : slot) | **No**, 80 bits | `[vlfSequence, logBlockOffset, slotNumber]` |
| MySQL, binlog coordinates | Binlog file plus position | Only by packing, which does not survive a binlog reset or a failover renumbering files | `[fileOrdinal, position]` |
| MySQL, GTID sets | Partially ordered set | **No** | **None. No total order exists at any width** (§ Concerns / Gaps) |

Tuples are compared element-wise, left to right, and both sides must have the same arity; a mismatch is a record-level error rather than a coin flip.

**A tuple, not a delimited string.** An encoding such as `"txId.commitScn.eventScn"` compares correctly only when every component is fixed-width zero-padded, and is silently wrong otherwise, because `"10"` sorts before `"9"`.
Since the comparison already happens inside a script, the key is stored as an array of longs and compared element-wise: no padding decisions, no per-component ceiling, and the ordering rule is explicit rather than an emergent property of collation.
Elasticsearch's `version` *field type* is unrelated to any of this; it is a `keyword` specialisation for semantic version strings and plays no part in write concurrency control.

Where versioning *is* enabled, deletes carry the same key as writes, and conflicts are surfaced through a `version_conflicts` metric and `version.conflict.mode` = `skip` | `warn` | `fail`, never silently discarded.
Under the scripted guard a rejected write is a `noop` result rather than an exception, so the classifier of §9.1 counts it toward the same metric rather than treating it as a success.

#### 7.5 Delivery semantics

The connector provides **exactly-once delivery to Elasticsearch**, on the same basis the Confluent connector claims it: Kafka Connect gives at-least-once delivery, and an Elasticsearch write addressed by a stable `_id` is idempotent, so a redelivered record converges on the same document rather than producing a second one.
Stating this in the same terms the Confluent connector uses is deliberate: the guarantee is genuinely equivalent, and a weaker-sounding phrasing for identical behaviour would misrepresent the connector in exactly the comparison users are making.

**The note that belongs with the claim.** The guarantee is a property of *document state*, and it holds only while `_id` is derived from something stable in the record:

* `primary.key.mode=record_key` (default), `record_value` and `record_header` all satisfy this, and are the modes the guarantee is written for.
* `primary.key.mode=kafka` derives `_id` from `topic+partition+offset`. Offsets are stable under redelivery, so this also converges, but it cannot survive a topic being recreated or offsets being reset, after which the same logical row occupies a new `_id`.
* `primary.key.mode=none` forfeits it outright: Elasticsearch generates a fresh `_id` per write, so a redelivered record *is* a duplicate document.
  §3 already states this; it is repeated here because this is the section a reader consults when comparing guarantees.

Ordering is a separate property, established in §7.1 to §7.3, and idempotence does not supply it: replaying two events for one `_id` out of order converges on whichever arrived last.
That is why §7.2 removes reordering by construction rather than relying on idempotent writes to paper over it.

### 8. Backpressure, batching and throughput

Flush-timeout task failures and 429 storms are among the most frequently raised operational topics on the Confluent connector's tracker.
The design targets both.

* **A single bulk request in flight** (§7), over a batch already reduced to one write per `_id`.
  Retries of individual items are therefore always safe to re-apply.
* **429 / `es_rejected_execution_exception` is a throttle signal, not an error.** `AdaptiveThrottle` halves the effective batch size on rejection and recovers additively toward `batch.size` after a configurable number of clean responses.
  Throttle retries are **not** counted against `max.retries`, because counting a backpressure signal as a failure is what converts a busy cluster into a dead task.
* **Buffer-full and flush-timeout pause the consumer rather than failing the task.** A task should die only when it cannot make progress at all.
  The pause mechanism is a runtime-adapter concern expressed as an SPI: under Connect it is `SinkTaskContext.pause()`; under Debezium Server it is deferred completion of the batch.
  The *decision* to pause lives in the core.
* **No asynchronous flush mode.** V1 flushes asynchronously by default and consequently supports topic-mutating SMTs only under `flush.synchronously=true`.
  Because we are synchronous by construction, that trade-off does not arise: such SMTs work unconditionally, and offsets are committed only for records confirmed by a bulk response.
  In practice § 4.1 should make routing SMTs unnecessary for naming anyway.
* **Batch controls:** `batch.size` (records, shared config, default 500; see § Open Questions on raising it for this sink), `bulk.size.bytes` (default 5 MiB, `-1` to disable) so a batch of large documents cannot exceed `http.max_content_length`, and `linger.ms` (default 0) to accumulate partial batches under low throughput.
  Note that V1's `linger.ms` default is 1 rather than 0; the property is equivalent, the default is not, and § 13 records it as such.
* **An optional rate ceiling.** `AdaptiveThrottle` is reactive: it responds *after* Elasticsearch returns a 429.
  Operators sharing a cluster with other workloads have asked for a proactive ceiling ([#732](https://github.com/confluentinc/kafka-connect-elasticsearch/issues/732), [#737](https://github.com/confluentinc/kafka-connect-elasticsearch/issues/737)), so that the connector never becomes the reason another application sees rejections.
  `max.requests.per.second` and `max.bytes.per.second` (both unset by default, meaning no ceiling) cap the request loop from above.
  They compose with the throttle rather than replacing it: the effective rate is the lower of the ceiling and whatever the throttle has currently backed off to, and a configured ceiling is reported in the metrics of §11 so a throughput investigation can see it.

### 9. Error handling and task health

#### 9.1 Classification

Every bulk item outcome is classified into one of four buckets.
The classifier is a table keyed on HTTP status plus the ES error `type`, so it is auditable and extensible rather than a fixed hard-coded set.

| Bucket | Examples | Action |
|---|---|---|
| **Success** | `created`, `updated`, `deleted`, `noop`, and `not_found` on a delete | Advance |
| **Transient** | 429 `es_rejected_execution_exception`, 503 `unavailable_shards_exception`, `node_not_connected_exception`, `circuit_breaking_exception`, socket/read timeouts | Throttle and retry per §8 |
| **Record-level, permanent** | `mapper_parsing_exception`, `document_parsing_exception`, `strict_dynamic_mapping_exception`, `illegal_argument_exception`, `version_conflict_engine_exception` (mode-dependent), `document_missing_exception` on a non-upsert update | Route to `ErrorReporter` |
| **Fatal** | 401/403 `security_exception`, `cluster_block_exception` (index read-only), `index_closed_exception`, unsupported cluster version | Fail fast with a diagnostic message |

`error.classification.overrides` lets an operator move a specific ES error `type` between buckets without waiting for a release.
It is a comma-separated list of `<error-type>:<bucket>` pairs, where `<error-type>` is the Elasticsearch error `type` string exactly as it appears in a bulk item response and `<bucket>` is one of `transient`, `record`, or `fatal`; for example `circuit_breaking_exception:record,illegal_argument_exception:transient`.
`success` is deliberately not an assignable bucket: reclassifying a failure as a success is how a document is lost silently, which Requirement 1 forbids.
An unknown error type is accepted, because the point is to react to something the connector has not seen before, but an unknown bucket name is a startup error.
This is a hedge against the fixed-set approach V1 takes with `MALFORMED_DOC_ERRORS`, where an error type outside the set cannot be routed differently without a code change.

**The default for an unmatched outcome is `Fatal`, not `Transient`.** An exception from the bulk path that the table above does not classify, the `NullPointerException` of §10.1.1 being the canonical example, is treated as fatal and fails the task with the exception attached.
Defaulting the unknown case to transient is precisely how a connector arrives at retrying forever while reporting `RUNNING`, and no unclassified condition is well enough understood to be assumed recoverable.
An operator who knows better moves the specific error into the transient bucket with `error.classification.overrides`.

#### 9.2 One error path, both runtimes

Both runtimes share one error path, because `debezium-sink` provides one.
`io.debezium.dlq.ErrorReporter` is runtime-neutral, with `ErrorReporters.fromContext(SinkTaskContext)` binding it to Connect's KIP-610 `ErrantRecordReporter` and `ErrorReporters.nop()` as the fallback.
`AbstractChangeEventSink` routes through it, including re-driving a failed batch record by record to isolate the offending one so that healthy records written alongside it are not discarded.

We use that machinery rather than building a parallel mechanism that could disagree with it:

* **Kafka Connect**: standard `errors.tolerance` / `errors.deadletterqueue.*`.
  `ErrorReporters.validateConfiguration` already rejects the contradictory `errors.deadletterqueue.topic.name` + `errors.tolerance=none` combination.
* **Debezium Server**: the adapter supplies an `ErrorReporter` implementation selected by `errors.handler` = `fail` (default) | `log` | `index`, where `index` writes the record and the Elasticsearch error to a dedicated `errors.index` (default `${resource}-dlq`).
  This is a small amount of code sitting behind an interface that already exists, and it gives Server users a real dead-letter path.

The DLQ record carries the original record *and* the Elasticsearch error type, reason and status, so a DLQ consumer can triage without correlating against connector logs.

We also override `AbstractChangeEventSink.isRetriableWriteException` so that transient Elasticsearch failures propagate to the retry machinery instead of being reported as errant records; the base class defaults to `false`, which would DLQ a whole batch on a cluster hiccup.

#### 9.3 Health from observed progress

Thread liveness is not health.
A task that has been retrying the same bulk request for longer than `progress.stall.timeout.ms` (default 5 minutes), or that holds a non-empty buffer that has received no successful response within that window, transitions to `FAILED` with the last Elasticsearch error attached, rather than sitting in `RUNNING` making no progress.
A task reporting `RUNNING` while making no progress is one of the more frequently raised topics on the Confluent connector's tracker, which is why health is defined here in terms of observed progress rather than thread liveness.

`log.sensitive.data` (default `false`, matching V1) gates whether document bodies appear in error logs.
When `false`, failures log the record coordinates, `_id`, index and ES error, but never the payload.

### 10. Client, connection, security

#### 10.1 Client and version support

Build on `co.elastic.clients:elasticsearch-java`, the typed Java API client.
The deprecated `elasticsearch-rest-high-level-client` is not used.

**Supporting 8.x and 9.x from one artifact.** Elastic's client compatibility policy is forward-only across one major: an 8.x client can talk to a 9.x cluster through REST API compatibility headers (`Accept: application/vnd.elasticsearch+json; compatible-with=8`), but a 9.x client is not supported against an 8.x cluster.
Therefore:

* Compile against the **latest 8.x client line**, which covers 8.x natively and 9.x via compatibility headers.
  This is the same mechanism Confluent's V2 connector exposes through its `elastic.server.version` switch.
* `connection.api.compatibility.mode` = `auto` (default) | `enabled` | `disabled`.
  `auto` sets the headers when the handshake reports a cluster major greater than the client's.
* Perform a **cluster version handshake at startup** and fail with a clear message on an unsupported version, rather than failing obscurely at first write.
* Revisit the client line when Elasticsearch 8.x reaches end of life; the alternative of shipping two artifacts is recorded under § Rejected Alternatives.

**Elasticsearch 7.x is out of scope**, as a consequence of the 8.x client baseline rather than an oversight: compatibility is forward-only, so an 8.x client cannot address a 7.x cluster.
Confluent V2 made the same cut, its `elastic.server.version` accepting only `V8` and `V9`, while V1 documented "7.x and later".
This is restated in § Non-goals so that a reader comparing against V1 sees it as a decision.

##### 10.1.1 The handshake failure contract

The handshake is the connector's own bootstrap, and it is subject to Requirement 1 exactly like the write path.
A report against the Confluent connector describes what the absence of such a contract can look like: [#929](https://github.com/confluentinc/kafka-connect-elasticsearch/issues/929) recounts a *transient* failure of the startup version probe leaving Elasticsearch 8 compatibility mode disabled for the life of the task, after which bulk requests failed with a `NullPointerException` while the task continued to report `RUNNING`.
We have not reproduced it, and it is cited here as a failure shape worth designing against rather than as a defect claim.
Adopting `connection.api.compatibility.mode=auto` without stating the contract below would leave the same shape available to us.

The invariant is that **no code path may turn a missing probe result into a running task.** Concretely:

1. **The two failure outcomes are distinct and neither is silent.**
   A probe that *succeeds and reports an unsupported version* is a configuration error: fail at startup, naming the cluster version and the supported range.
   A probe that *fails to answer* is a transport error: retry within the `max.retries` / `retry.backoff.ms` / `retry.backoff.max.ms` budget, and if the budget is exhausted fail the task with the transport error attached.
   There is no fallback to a default or assumed compatibility mode in either case.
2. **A failed probe is never cached.** Only a successful result is memoised; a failure clears any cached value rather than writing one.
   This is the specific behaviour described in #929.
3. **Version-shaped errors force a re-probe.** A bulk response carrying a media-type or compatibility error invalidates the cached version and re-probes before the next request, bounded by `max.retries` and then fatal.
   This also covers a cluster upgraded underneath a long-running task.
4. **The resolved state is observable.** The detected cluster version and the effective compatibility mode are logged once at `INFO` at startup and exposed as metrics (§11).
   #929 was difficult to diagnose principally because the effective mode was invisible from outside the task.

`enabled` and `disabled` still perform the probe; they override only which headers are sent, not whether the supported-version gate runs.

A side benefit worth stating: because the probe is fail-fast, a non-Elasticsearch endpoint, whether OpenSearch or a gateway that does not proxy the root API, is rejected at startup with a clear message rather than surfacing as confusing errors at first write.
[#677](https://github.com/confluentinc/kafka-connect-elasticsearch/issues/677) is a report of that situation reaching the user without a clear message.

#### 10.2 Connection and authentication

| Property | Notes |
|---|---|
| `connection.url` | Comma-separated list of URLs. Multiple URLs are supported (V1 allowed a list; V2 narrowed to one, but we keep the list). A **context path** is honoured, e.g. `https://gateway.internal/es`, for clusters behind a reverse proxy. Users have reported difficulty with this shape elsewhere ([#609](https://github.com/confluentinc/kafka-connect-elasticsearch/issues/609)), so it is stated here and covered by a test rather than left to be inferred. |
| `connection.cloud.id` | Elastic Cloud; mutually exclusive with `connection.url`, validated at startup. |
| `connection.auth.mode` | `none` \| `basic` (default when credentials are present) \| `api_key` \| `bearer` \| `kerberos` \| `custom` |
| `connection.username`, `connection.password` | `basic` |
| `connection.api.key` | `api_key` (V2's `api.key.value`) |
| `connection.bearer.token` | `bearer` |
| `connection.kerberos.principal`, `connection.kerberos.keytab` | `kerberos` (V1's `kerberos.user.principal` / `kerberos.keytab.path`) |
| `connection.credentials.provider.class` | `custom`; an SPI so that AWS SigV4 and other cloud-vendor schemes live outside the core |
| `connection.headers` | Static headers, e.g. for a gateway |
| `connection.compression` | GZip; requires `http.compression` on the cluster |
| `connection.timeout.ms` | Connect timeout, default 5000 (V1: 1000) |
| `connection.read.timeout.ms` | Socket read timeout, default 60000 (V1: 3000) |
| `connection.idle.timeout.ms` | Drop idle connections before the server does, default 60000 |
| `connection.sniff.enabled` | Node sniffing, default `false` |

An `auth.mode` that does not match the credentials supplied is a startup error naming both, not a silent fall-through to unauthenticated, which is how credential typos become anonymous writes.

**The timeout defaults are a fix, not a preference.** V1 ships `connection.timeout.ms=1000` and `read.timeout.ms=3000`, and those values are the direct cause of the most-commented issue on its tracker: [#381](https://github.com/confluentinc/kafka-connect-elasticsearch/issues/381), "Read timed out when trying to check if index exists", at 36 comments, along with [#534](https://github.com/confluentinc/kafka-connect-elasticsearch/issues/534) and [#603](https://github.com/confluentinc/kafka-connect-elasticsearch/issues/603).
A three-second socket timeout is shorter than a routine bulk request against a loaded cluster.
The reasoning is recorded in the property descriptions so that a default which is visibly a bug fix is not later "tuned" back toward the value that caused the bug.

**Kerberos is a migration lifeline, not a nicety.** V1 supports it through `kerberos.user.principal` / `kerberos.keytab.path`; **V2 removed it**, admitting only `NONE`, `BASIC` and `API_KEY` in its `auth.type`.
A Kerberos-authenticated V1 deployment therefore has no vendor migration path at all ahead of V1's April 2027 end of life.
Supporting `connection.auth.mode=kerberos` is what makes this connector a destination for that population, and it is called out here rather than left as one row of the parity matrix.

#### 10.3 TLS and proxy

`connection.tls.*` covers keystore and truststore location/password/type, key password, enabled protocols, cipher suites, and `connection.tls.ca.fingerprint` for Elastic's self-signed-CA bootstrap flow.
`connection.proxy.host`, `.port`, `.username`, `.password` cover the V1 proxy properties.

**Verification is three-valued, not a boolean.** `connection.tls.verification.mode` = `full` (default) | `certificate` | `none`, deliberately reusing Elastic's own vocabulary from `elasticsearch.yml` and Beats so that users transfer what they already know rather than learning a connector-specific name:

* `full`: verify the certificate chain and that the hostname matches.
* `certificate`: verify the chain but not the hostname. This is the case a `hostname.verification=false` boolean would have covered.
* `none`: no verification at all. Requested repeatedly against the Confluent connector, which exposes no equivalent property ([#843](https://github.com/confluentinc/kafka-connect-elasticsearch/issues/843), [#861](https://github.com/confluentinc/kafka-connect-elasticsearch/issues/861)), for development clusters and self-signed deployments that predate a managed CA.

Anything other than `full` logs a `WARN` at startup naming the mode and the cluster it applies to.
The description for `none` states plainly that it defeats TLS against an active attacker, and that `connection.tls.ca.fingerprint` is the supported way to trust a self-signed cluster without abandoning verification.

### 11. Observability

* **Metrics** (JMX, via the Debezium sink metrics base): records written, records skipped, records reported to the error handler, batches written, bulk request latency percentiles, bulk item outcomes by classification bucket, throttle events, current effective batch size, retry count, version conflicts, buffer depth, and, importantly for §9.3, milliseconds since the last successful bulk response.
* **Resolved connection state**, exposed as attributes rather than left to log archaeology: the detected cluster version, the effective API compatibility mode, and any configured rate ceiling (§8).
  §10.1.1 explains why: in [#929](https://github.com/confluentinc/kafka-connect-elasticsearch/issues/929) the reported difficulty was largely that the effective compatibility mode could not be seen from outside the task.
* **OpenLineage.** The connector emits lineage events through `debezium-openlineage-api`, naming the resolved Elasticsearch resources as output datasets, so an end-to-end source-to-sink lineage graph is available without extra wiring.

---

### 12. Configuration surface

Properties are defined once, in runtime-neutral terms.
Kafka Connect uses the bare name; Debezium Server uses the `debezium.sink.elasticsearch.` prefix.

**12.1 Connection**: `connection.url`, `connection.cloud.id`, `connection.auth.mode`, `connection.username`, `connection.password`, `connection.api.key`, `connection.bearer.token`, `connection.kerberos.principal`, `connection.kerberos.keytab`, `connection.credentials.provider.class`, `connection.headers`, `connection.compression`, `connection.timeout.ms`, `connection.read.timeout.ms`, `connection.idle.timeout.ms`, `connection.sniff.enabled`, `connection.api.compatibility.mode`, `connection.tls.*` (including `connection.tls.verification.mode`, `full`), `connection.proxy.*`

**12.2 Input contract**: `event.format` (`auto`), `cloud.events.schema.name.pattern` (shared), `tombstone.mode` (`auto`)

**12.3 Identity**: `primary.key.mode` (shared, `record_key`), `primary.key.fields` (shared), `document.id.separator` (`:`), `document.id.non.key.ordering`, `document.metadata.fields` (empty), `document.metadata.prefix` (`_kafka_`)

**12.4 Naming and resources**: `collection.name.format` (shared, `${topic}`), `collection.naming.strategy` (shared), `resource.name.timezone` (`UTC`), `resource.name.invalid.handling` (`fail`), `resource.name.replacement` (`_`), `resource.type` (`index`), `resource.auto.create` (`true`), `topic.to.resource.mapping`, `data.stream.type`, `data.stream.dataset`, `data.stream.namespace` (`${topic}`), `data.stream.timestamp.field`, `index.routing.field`, `ingest.pipeline`, `ingest.pipeline.validate` (`true`)

**12.5 Writes**: `write.method` (`upsert`), `write.method.per.operation`, `delete.enabled` (`true`), `truncate.enabled` (shared, `false`; derived from `truncate.mode`, §5), `truncate.mode` (`ignore`), `truncate.allowed.resources` (no default; required by the destructive modes), `truncate.allow.wildcard` (`false`)

**12.6 Ordering**: `version.strategy` (`none`), `version.enforcement` (`auto`), `version.conflict.mode` (`skip`)

**12.7 Batching and flow control**: `batch.size` (shared), `bulk.size.bytes` (5 MiB), `linger.ms` (0), `flush.timeout.ms` (180000), `max.retries` (5), `retry.backoff.ms` (100), `retry.backoff.max.ms` (30000), `progress.stall.timeout.ms` (300000), `max.requests.per.second` (unset), `max.bytes.per.second` (unset)

**12.8 Types and mapping**: `decimal.output.mode`, `temporal.output.mode`, `interval.output.mode`, `json.output.mode`, `bits.output.mode`, `binary.output.mode`, `geometry.output.mode`, `vector.output.mode`, `map.output.mode`, `null.value.handling`, `field.name.adjustment.mode`, `field.name.separator.replacement`, `field.include.list` (shared), `field.exclude.list` (shared), `mapping.mode` (`create_if_absent`), `mapping.dynamic` (`strict_but_dlq`), `mapping.composed.of`, `mapping.settings`, `string.mapping.mode`, `struct.mapping.mode`, `json.mapping.mode`

**12.9 Errors**: `errors.handler` (Server only), `errors.index`, `error.classification.overrides`, `log.sensitive.data` (`false`); under Connect, the standard `errors.tolerance` / `errors.deadletterqueue.*`

**12.10 Per-topic overrides.** Confluent expresses these as parallel list properties (`topic.key.ignore`, `topic.schema.ignore`).
We generalise instead: any property in 12.2 through 12.8 may be overridden per topic as `topic.<topic-name>.<property>`.
That covers both Confluent list properties and everything else, with one rule to learn.
Overrides are resolved at startup and an override naming an unknown property is a startup error.

---

### 13. Feature parity with Confluent V1 and V2

Every capability of both connectors, and where it lands here.
"Deliberately not offered" appears once, and § 7.2 explains why.

| Confluent property (V1 / V2) | Debezium Elasticsearch equivalent |
|---|---|
| `connection.url` | `connection.url` (list retained) |
| `connection.username` / `connection.password` | `connection.username` / `connection.password` with `connection.auth.mode=basic` |
| `auth.type` (V2) | `connection.auth.mode`, superset. V2 admits only `NONE`, `BASIC`, `API_KEY`; we add `bearer`, `kerberos`, `custom` |
| `api.key.value` (V2) | `connection.api.key` |
| `elastic.server.version` (V2) | Auto-detected by handshake (§10.1.1); `connection.api.compatibility.mode` to override |
| `connection.compression` | `connection.compression` |
| `connection.timeout.ms` | `connection.timeout.ms` |
| `read.timeout.ms` | `connection.read.timeout.ms` |
| `max.connection.idle.time.ms` | `connection.idle.timeout.ms` |
| `elastic.security.protocol` (V1) / `elastic.ssl.enabled` (V2) | Implied by the URL scheme plus `connection.tls.*` |
| `elastic.https.ssl.*` | `connection.tls.*` |
| `kerberos.user.principal` / `kerberos.keytab.path` (V1) | `connection.kerberos.principal` / `.keytab`. **V1 only: V2 removed Kerberos**, so these deployments have no vendor migration path (§10.2) |
| `proxy.host` / `.port` / `.username` / `.password` (V1) | `connection.proxy.*` |
| `batch.size` | `batch.size` |
| `bulk.size.bytes` (V1) | `bulk.size.bytes` |
| `linger.ms` (V1) | `linger.ms`. V1 defaults it to 1, we default to 0; **V2 removed it** in favour of framework-level batching |
| `max.buffered.records` (V1) | Not needed: one request in flight bounds the buffer to one batch |
| `flush.timeout.ms` (V1) | `flush.timeout.ms`, but it **pauses** rather than failing the task; **V2 removed it** in favour of a framework-level timeout |
| `flush.synchronously` (V1) | Always synchronous; the property has no meaning here |
| `max.in.flight.requests` (V1) | **Deliberately not offered** (§7.2) |
| `max.retries` / `retry.backoff.ms` | `max.retries` / `retry.backoff.ms` / `retry.backoff.max.ms`; throttles excluded from the count |
| `log.sensitive.data` (V1) | `log.sensitive.data` |
| `key.ignore` | `primary.key.mode=kafka` |
| `topic.key.ignore` | `topic.<name>.primary.key.mode` |
| `use.autogenerated.ids` | `primary.key.mode=none` |
| `external.version.header` | `version.strategy=record_header` |
| `schema.ignore` | `mapping.mode=none` |
| `topic.schema.ignore` | `topic.<name>.mapping.mode` |
| `compact.map.entries` | `map.output.mode` |
| `drop.invalid.message` (V1) | `errors.tolerance=all` + DLQ, which retains the record and the Elasticsearch error rather than dropping both. **V2 removed it** and fails the task on preprocessing errors instead |
| `behavior.on.null.values` | `tombstone.mode`, defaulted from the detected input contract |
| `behavior.on.malformed.documents` | Error classification (§9.1) + `errors.tolerance`. V1 offers `ignore`/`warn`/`fail`; V2 dropped `warn` |
| `write.method` (`INSERT`/`UPSERT`) | `write.method` (`index`/`upsert`), plus `create` and `update` |
| `auto.create` (V2) | `resource.auto.create` |
| `resource.type` (V2) | `resource.type` |
| `topic.to.resource.mapping` (V2) | `topic.to.resource.mapping`, but **unconditional**. V2 honours it only when `auto.create=false` (§4.2) |
| `external.resource.usage` (V1) | Split into `resource.type` + `resource.auto.create`, as V2 did |
| `topic.to.external.resource.mapping` (V1) | `topic.to.resource.mapping` |
| `max.external.resource.mappings` (V1) | Not carried over; a control-plane concern |
| `auto.create.indices.at.start` (V1) | `resource.auto.create` |
| `data.stream.type` / `.dataset` / `.namespace` / `.timestamp.field` | Same names. V1's `NONE` sentinel has no equivalent: `resource.type` decides the resource kind (§4.3). V2 likewise removed `NONE` and defaults to `LOGS` |
| `errors.tolerance`, `errors.deadletterqueue.*` | Same names under Connect; `errors.handler` under Server. V2 routes to error topics rather than a DLQ, so a V2 migrant is changing model, not just names |
| `external.version.header` carrying a record timestamp | `version.strategy=source_ts_ms` covers the "use the record timestamp as the version" request ([#81](https://github.com/confluentinc/kafka-connect-elasticsearch/issues/81)) with the source commit time, which is the more meaningful clock |
| `csfle.*`, `auto.register.schemas`, `use.latest.version` (V2) | **Declined**, not deferred: CSFLE requires Confluent Schema Registry and Confluent-licensed components, which Requirement 4 excludes (§ Non-goals) |
| `input.data.format`, `value.converter.*`, `schema.context.name` (V2, Cloud) | Kafka Connect converter configuration; not connector properties |
| `consumer.override.*`, `tasks.max`, `kafka.*` (V2, Cloud) | Kafka Connect / Confluent Cloud framework properties |

**Capabilities V1 has that V2 dropped, which we retain.** Migration paths run in both directions, and V2 is not a superset of V1.
Each of these is a reason a V1 deployment might land here rather than on V2 before V1's April 2027 end of life:

| Capability | V1 | V2 | Here |
|---|---|---|---|
| Kerberos authentication | yes | **removed** | `connection.auth.mode=kerberos` |
| Multiple connection URLs | yes | **single URL only** | `connection.url` accepts a list |
| Elasticsearch 7.x | yes | no | no (§ Non-goals) |
| `linger.ms` | yes | **removed** | `linger.ms` |
| `flush.timeout.ms` | yes | **removed** | `flush.timeout.ms`, and it pauses instead of failing |
| Tolerating a malformed document | `drop.invalid.message` | **removed**, task fails | `errors.tolerance` + DLQ, with the ES error attached |
| `warn` on malformed documents | yes | **removed** | Error classification (§9.1), which is finer-grained than all three |

**Capabilities neither connector offers, added here:** native envelope consumption, truncate handling, Debezium logical-type fidelity, schema-derived index templates, `_routing`, ingest pipelines, placeholder-based index naming with source metadata and date patterns, generalised per-topic overrides, source-position external versioning, progress-based health, Kafka metadata as document fields, a proactive rate ceiling, three-valued TLS verification, and execution under Debezium Server.

---

### 14. Debezium Server integration

The JDBC sink established the pattern and we follow it exactly rather than inventing one.
`io.debezium.server.jdbc.JdbcChangeConsumer` is a CDI bean (`@Named("jdbc") @Dependent`) extending `BaseChangeConsumer`; it converts each `BatchEvent` to a `SinkRecord` via `ChangeEventToSinkRecordConverter` and delegates to `JdbcSinkConnectorTask`.
Delegating to the Connect task, rather than duplicating the sink logic, is what keeps the two runtimes from drifting.

`ElasticsearchChangeConsumer` mirrors this: `@Named("elasticsearch") @Dependent`, reading `debezium.sink.elasticsearch.*`, delegating to `ElasticsearchSinkConnectorTask`, and committing each `BatchEvent` only after the bulk response confirms it.

**This module lives in the `debezium-server` repository**, as `debezium-server-elasticsearch`, alongside `debezium-server-jdbc`.
It contains the change consumer, `ChangeEventToSinkRecordConverter`, the Quarkus and native-image reflection configuration, the `ComponentMetadataProvider` service registration, and its own integration tests; nothing else.
Every other component in this document lives in `debezium-connector-elasticsearch`.
The boundary is deliberate: because the consumer delegates to the Connect task rather than reimplementing sink behaviour, the surface that can drift between the two repositories is small enough to be covered by the runtime-parity suite.

Four consequences for the sections above:

1. **Value format.** In Debezium Server the payload format is configurable (`debezium.format.value` = `json`, `avro`, `protobuf`, `cloudevents`, `connect`).
   §2 and §6 need a Connect `Struct` *with its schema*: base64 decimals and temporal logical types cannot be recovered from bare JSON.
   As the JDBC sink does, the connector **requires `debezium.format.value=connect` and `debezium.format.key=connect`** and **fails at startup with an explicit message** naming the offending property, rather than silently degrading every field to a string.
   (`cloudevents` is additionally accepted because `KafkaDebeziumSinkRecord` unwraps it to a `Struct`.)
2. **Ordering.** The §7 argument holds more simply here: the engine delivers to a single consumer in source order, with no partitions or rebalancing.
   One-bulk-in-flight plus per-`_id` reduction gives the same guarantee in both runtimes, a further argument for keeping that logic in the shared sink rather than the adapter.
3. **Backpressure.** `SinkTaskContext.pause()` does not exist under Server; flow control comes from deferring batch completion.
   Expressed as the SPI in §8; the adaptive-throttle logic itself stays in the core.
4. **Error handling.** Resolved by `io.debezium.dlq.ErrorReporter` (§9.2).
   The Server adapter supplies `errors.handler` = `fail` | `log` | `index`.

**Parity is a release criterion, not an aspiration.** Any feature that cannot work under Debezium Server must be documented as Connect-only at the point of introduction, with a startup warning when configured under Server.
No feature ships to one runtime without a recorded decision for the other.

---

### 15. Implementation steps

Ordered by priority.
Steps 1 to 8 constitute a usable v1.

1. **Configuration and validation skeleton.** `ElasticsearchSinkConnectorConfig` extending `SinkConnectorConfig`, the full §12 surface, and the cross-property startup validations (data stream vs. `write.method`/deletes; `version.enforcement` vs. `write.method` and key shape; `primary.key.mode=none` vs. deletes/upserts; non-key `_id` vs. `document.id.non.key.ordering`; `document.id.non.key.ordering=version` vs. `version.strategy=none`; `truncate.enabled` vs. the value derived from `truncate.mode`; MySQL GTID mode vs. `document.id.non.key.ordering=version`; auth mode vs. credentials; `keyed.message.batch.mode=passthrough` rejection).
   Validation is step 1 because every later step depends on being able to reject a bad combination loudly.
2. **Connect adapter and client bootstrap.** `ElasticsearchSinkConnector` and `ElasticsearchSinkConnectorTask`, `co.elastic.clients` construction from the connection properties, TLS/proxy/auth wiring, and the startup cluster-version handshake with the failure contract of §10.1.1.
   This step also ships the `META-INF/services` manifests for Kafka Connect's `plugin.discovery=SERVICE_LOAD`, default from Kafka 3.6.
   It is a few lines of packaging, and omitting it is an adoption blocker on current Connect: it remains an open request on the Confluent connector ([#812](https://github.com/confluentinc/kafka-connect-elasticsearch/issues/812)), where load problems on Connect 4.1.0 have also been reported ([#897](https://github.com/confluentinc/kafka-connect-elasticsearch/issues/897)).
3. **`ElasticsearchChangeEventSink extends AbstractChangeEventSink`** with `doWriteBatch` and `isRetriableWriteException`, wired to `DeduplicatingBuffer` and `ErrorReporters.fromContext`.
4. **`RecordAdapter`**: the three input contracts of §2 and the operation mapping.
5. **`DocumentIdStrategy`** and **`ResourceResolver`**: §3 and §4.1 to §4.2, including name validation.
6. **`DocumentConverter`**: the §6.1 logical-type registry, `null.value.handling`, field-name adjustment.
7. **`ElasticsearchBulkWriter` and `BulkResponseClassifier`**: single-in-flight bulk execution, the §9.1 classification table, DLQ routing, bounded retry.
8. **`AdaptiveThrottle` and progress-based health**: §8 and §9.3, plus the metrics of §11.
9. **`MappingManager`**: §6.3 component-template generation, `mapping.composed.of`, `strict_but_dlq`, conflict diagnostics.
10. **Data streams**: §4.3, including the startup and per-record validations.
11. **Routing and ingest pipelines**: §4.4.
12. **`TruncateHandler`**: §5.1, including the `truncate.allowed.resources` gate, the drain/execute/resume sequencing, and `recreate`.
13. **`VersionStrategy`**: §7.4, including source-position extraction per connector, both enforcement mechanisms (native external versioning and the scripted guard) behind `version.enforcement`, tuple comparison, and the `document.id.non.key.ordering=version` path.
14. **Debezium Server adapter**: §14.
    Lands in the `debezium-server` repository as `debezium-server-elasticsearch`; nothing else in this list does.
15. **Per-topic override resolution**: §12.10.
16. **OpenLineage emission**: §11.
17. **Documentation**: connector reference with both property forms, and the Confluent V1/V2 migration mapping from §13.

---

## Testing

* **Ordering and consistency.** Replay interleaved updates and deletes for the same key with batch boundaries falling mid-key, asserting final document state matches the source table.
  Two specific cases: delete-overtakes-update, and **partial bulk failure**; for the latter, inject a 429 on one item of a multi-item batch and assert the retry cannot apply stale state (§7.1 hazard 2).
  Batch reduction must be asserted to preserve terminal semantics: a run ending `u,u,d` reduces to a delete, a run ending `d,c` to a write, and `c,u,d` to a delete rather than to a no-op.
* **Startup validation.** Each combination listed in implementation step 1 must fail, with a test asserting the message names both offending properties.
* **Defaults never block a documented feature.** The mirror image of the previous case, and the one that regresses quietly: for each feature reachable by a single property (`primary.key.mode=none`, `resource.type=data_stream`, each `truncate.mode`, each `write.method`), a configuration setting only that property and the connection details must start successfully.
  A test that only asserts the failures of Requirement 2 will happily pass a connector that no user can configure.
* **Truncate safety (§5.1).** A destructive `truncate.mode` without `truncate.allowed.resources` must fail at startup; a bare `*` without `truncate.allow.wildcard` must fail at startup; a truncate event whose resolved resource is outside the list must reach the error handler and leave the index intact.
  Separately, a truncate arriving while a bulk request is in flight must not be issued until that response is processed, and a concurrent write during `delete_by_query` must surface as an aborted truncate rather than a partial one.
* **Input contracts.** The same logical change delivered as an envelope, as a flattened record with `__op` fields, as a flattened record with `__op` headers, as a CloudEvents envelope, and as a plain record, must all converge on the same index state.
* **Type fidelity.** Round-trip every Debezium logical type from each supported source connector and assert both the indexed JSON and the inferred or generated mapping.
  This suite is the regression barrier for the fidelity claims in §6.
* **Non-key `_id`.** Feed one `_id` from two partitions with known source positions, out of arrival order; assert `version` mode converges on the latest event, and that `last_write_wins` is documented as making no such promise.
  Run it under both enforcement mechanisms of §7.4, and with a tuple key as well as a scalar one, since the SQL Server and MySQL binlog paths only exist in tuple form.
  Assert that a tuple arity mismatch is a record-level error rather than an arbitrary winner, and that MySQL in GTID mode is rejected at startup instead of ordering on a partial order.
  Benchmark the scripted guard against the plain `index` path, since § Concerns / Gaps leaves that cost unquantified.
* **Failure modes.** Cluster unavailability mid-batch, 429 storms, mapping conflicts, malformed documents, and `strict` mapping rejections, under each `errors.tolerance` setting, asserting both DLQ contents and the resulting task state.
* **Health.** A wedged bulk request must transition the task to `FAILED` within `progress.stall.timeout.ms` with the last Elasticsearch error attached.
* **Confluent parity.** A test matrix that exercises each row of §13, so parity is a verified property rather than a documented intention.
* **Runtime parity.** The ordering, type-fidelity and failure-mode suites run against *both* runtimes from a shared test base.
  Plus an ArchUnit rule for the §1 invariant, and a test that a non-`connect` `debezium.format.value` fails loudly under Server.
* **Benchmarking** on CDC-shaped rather than log-shaped load, to fix the defaults marked provisional in §12 and to confirm the §7.2 throughput reasoning.
* **Version matrix.** Integration tests against Elasticsearch 8.x and 9.x via Testcontainers, including the compatibility-header path.
* **Handshake failures (§10.1.1).** A version probe that fails transiently must retry and then fail the task with the transport error, never start with an assumed compatibility mode, and never cache the failure; an unsupported version must fail at startup naming the version and the supported range; a media-type error on a bulk response must force a re-probe; and an unclassified exception from the bulk path must fail the task rather than retry forever.
  These cover the failure shape described in [#929](https://github.com/confluentinc/kafka-connect-elasticsearch/issues/929).
* **Connection details that regress quietly.** Three behaviours that are invisible until a specific deployment shape exercises them, and that users have reported struggling with elsewhere: `connection.compression` actually compressing the request body ([#491](https://github.com/confluentinc/kafka-connect-elasticsearch/issues/491)); externalised secrets resolving through Connect's config providers and the Debezium Server equivalent ([#547](https://github.com/confluentinc/kafka-connect-elasticsearch/issues/547)); and a `connection.url` carrying a context path reaching the right endpoint ([#609](https://github.com/confluentinc/kafka-connect-elasticsearch/issues/609)).
* **Schema evolution (§6.3).** The compatible-evolution matrix asserted row by row against a live cluster, since "additive changes are absorbed" is a claim the parity discussion rests on.

## Backward Compatibility

The connector is new, so there is no prior contract to preserve.
Two points are still worth recording:

* **`delete.enabled` defaults to `true` here**, where `SinkConnectorConfig` defaults it to `false`.
  This diverges from the JDBC sink and must be stated prominently in the documentation.
  The rationale is that a CDC sink that silently drops deletes is the exact failure this connector exists to eliminate; a user who wants an append-only index sets it to `false` explicitly.
* **`keyed.message.batch.mode` is fixed to `deduplication`.** The shared property is accepted so that a copied JDBC configuration does not fail with "unknown property", but `passthrough` is rejected with an explanation rather than silently honoured.
* **`truncate.enabled` is derived rather than set.** The shared property remains accepted and readable, but `truncate.mode` is what an Elasticsearch user configures, and a contradictory explicit value is rejected (§5).
  A JDBC configuration carrying `truncate.enabled=true` therefore needs a `truncate.mode` alongside it, which is intentional: the JDBC property authorises a `TRUNCATE TABLE`, whereas here it would authorise deleting documents this connector never wrote (§5.1).

Once released, `primary.key.mode`, `document.id.separator`, and the §4.1 placeholder vocabulary become effective compatibility surfaces: changing any of them changes every `_id` or index name the connector produces.

## Concerns / Gaps

* **Source-position normalisation is per-connector work, and one source has no answer at all.**
  The tuple form in §7.4 removes the width problem: SQL Server's 80-bit LSN and MySQL's binlog file plus position both express as ordered tuples without being packed into 64 bits.
  Three things survive that:
  * **MySQL in GTID mode has no total order.** A GTID set is partially ordered by construction, so no key of any shape resolves two arbitrary events. Deployments that need §7.3 against MySQL must order on binlog coordinates instead, and the connector says so at startup rather than producing an arbitrary winner.
  * **Each remaining source needs its own extraction and its own tests.** That is five code paths, and a wrong one fails quietly: a key that compares in the wrong direction is discarded by the guard as stale, which looks exactly like correct suppression of an out-of-order event.
  * **The scripted guard costs throughput** on every write, since it replaces a plain bulk `index` with a per-document script execution. The size of that cost is unquantified until the benchmark in § Testing.

  This is why `version.strategy=none` remains the default, and why Open Question 1 recommends deferring the §7.3 support that depends on it.
* **The shared base class discards records at `DEBUG`.** `AbstractChangeEventSink.put()` skips a truncate when `truncate.enabled` is false, a delete when `delete.enabled` is false, and a tombstone under `primary.key.mode=record_value`/`record_header`, each with a `DEBUG` log and `continue`.
  Requirement 1 asks for more than that, and these paths are inherited rather than chosen.
  §5 compensates at the connector level with a startup `WARN` and a discarded-event metric, but the compensation is a report about a decision taken elsewhere, not a veto over it.
  The tombstone skip is the one that deserves a second look during implementation, because §7.3 makes `record_value` and `record_header` supported modes and this base-class behaviour silently removes deletes from both.
  Raising it upstream is preferable to working around it here.
* **Should `truncate.mode` be normalised into `debezium-sink` rather than invented here?**
  This document introduces `truncate.mode` (§5.1) as a connector-local enum while the framework expresses the same concept as the shared boolean `truncate.enabled`.
  §5 resolves the conflict by deriving one from the other, but that is a patch inside one connector, not a decision about the shared contract, and it leaves the sink family describing one capability two ways.
  The evidence that the shared contract is unsettled is already visible: three connectors implement `SinkConnectorConfig` (JDBC, MongoDB and this one), yet only JDBC acts on `isTruncateEnabled()`.
  `MongoDbSinkConnectorConfig` reads the property and exposes it, while `MongoDbChangeEventSink` has no truncate path at all, so there the property is accepted and silently ignored.
  The open question is which shape is the general one.
  A boolean is sufficient wherever a collection has exactly one way to be emptied, which is the relational case: `TRUNCATE TABLE` and nothing else.
  Elasticsearch has several, differing in atomicity, blast radius and cost, which is what produced the enum.
  If the enum is the general shape and the boolean is its degenerate case, then `truncate.mode` belongs in `SinkConnectorConfig` with a per-sink value set, and `truncate.enabled` becomes a deprecated alias.
  The same question applies to `delete.enabled`, and more sharply to `truncate.allowed.resources`: the hazard it guards against, destroying data the connector did not write, is not Elasticsearch-specific, and a JDBC `TRUNCATE TABLE` has exactly the same property today with no equivalent gate.
  The cost is asymmetric, which is why this is raised now rather than left to implementation.
  Normalising after release means renaming a shipped property; deciding now means one change to `debezium-sink` that has to serve JDBC and MongoDB as well, which is outside this document's scope but not outside its timing.
  Recommendation: raise it with the sink maintainers before this connector ships, keep `truncate.mode` connector-local for v1, and treat the name as provisional rather than building anything that would make lifting it harder.
* **`debezium-sink` is still evolving.** `SinkConnectorConfig` carries an internal `enable.sces` flag for the shared sink framework, and `DebeziumSinkRecord` carries a `@TODO` about replacing Connect's `Struct`/`Schema`.
  Baselining on 3.7.0-SNAPSHOT (§1) means tracking that churn rather than avoiding it; the type-fidelity and ordering suites are what will catch a breaking change.
* **`delete_by_query` cannot be made safe in general.** §5.1 removes the ordering hazard by draining and waiting, and `conflicts=abort` converts a concurrent write into a loud failure, but a document written by another producer during the scan still survives the truncate.
  That is a property of the API, not a gap in the design, and `recreate` is the alternative where the connector owns the resource outright.
* **Single-in-flight throughput** is bounded by round-trip latency.
  The reasoning in §7.2 says the margin is wide for CDC volumes; if a workload disproves it, the answer is larger batches or more tasks, not pipelining.

## Risks

* **Elasticsearch 8.x end of life** will force the client-line decision in §10.1 to be revisited, potentially requiring two artifacts or a hard 9.x-only cut.
* **Parity scope.** §13 is a large surface.
  Under-delivering on it after claiming parity is worse than scoping it explicitly, which is why the parity test matrix is a listed deliverable and `max.in.flight.requests` is called out as a conscious omission rather than quietly dropped.
* **Schema-derived mappings can be wrong for a user's search needs.** A `text`+`keyword` default is a guess about intent.
  `mapping.mode=none` must remain a first-class, well-documented escape hatch.
* **Cross-repository release coupling.** With the Server adapter in `debezium-server` (§1), a connector change that alters `ElasticsearchSinkConnectorTask`'s startup contract needs a coordinated change there.
  The mitigation is that the adapter delegates rather than reimplements, so its surface area against the connector is small, but the runtime-parity suite has to run in both repositories for that to hold.
* **Baselining on a snapshot.** Building against 3.7.0-SNAPSHOT means upstream churn in `debezium-sink` can break this build without warning.
  Accepted deliberately: the alternative is pinning below the `DeduplicatingBuffer` flush fix that §7.2 depends on for correctness.

## Rejected Alternatives

* **Reconstructing ordering with versioning by default.** Rejected: ordering is already guaranteed upstream for key-derived `_id`s, so the mechanism would buy nothing in the common case while costing something in every case.
  Native external versioning is unavailable on the Update API that our default `write.method=upsert` uses, and the scripted guard that does work there adds a per-document script execution to every write.
  The Kafka offset, the only version available without extra configuration, is comparable only within a partition.
  Versioning is retained only where it is genuinely the missing information (§7.3 and §7.4).
* **Pipelined bulk requests with `max.in.flight.requests`.** Rejected: it is the direct cause of the documented reordering corruption, and it buys throughput we can obtain from partitions and tasks without cost to correctness.
* **A cross-task coordinator for non-key `_id`s.** Rejected: mutual exclusion is not ordering (§7.3).
  It adds a per-document round trip and a new failure domain without answering the actual question.
* **Restricting the connector to one task when `_id` is not key-derived.** Rejected: it makes the outcome look deterministic while remaining arbitrary, which is worse than requiring an explicit decision.
* **A connector-specific DLQ mechanism.** Rejected: `io.debezium.dlq.ErrorReporter` already exists and is runtime-neutral.
  A parallel mechanism could disagree with `errors.tolerance`.
* **Forbidding Kafka Connect imports in the core.** Rejected in that form: `debezium-sink` itself is built on Connect's data model in both runtimes.
  Replaced with the narrower, enforceable invariant in §1.
* **Shipping two artifacts, one per Elasticsearch major.** Rejected for v1: doubles the build and test matrix and the user-facing choice, to avoid one compatibility header.
  Reconsider at 8.x EOL.
* **Inheriting Confluent property names for drop-in compatibility.** Rejected: names such as `key.ignore` and `external.resource.usage` encode a generic-record design.
  A documented migration mapping (§13) delivers the migration path without carrying the model.
* **A general-purpose `flush.synchronously` switch.** Rejected: there is nothing to switch, because synchronous flushing is a consequence of the ordering design.
* **Managing ILM and data-stream lifecycle policies from `mapping.mode`.** Rejected: it would raise the required cluster privileges for every deployment, hand a sink connector the ability to attach index-deleting policies as a side effect, and express as connector config something the Connect schema cannot imply.
  `mapping.composed.of` reaches the same outcome with the user owning the policy (§6.3).
* **`truncate.confirm.destructive=true` as the destructive-truncate acknowledgement.** Rejected: a boolean can be satisfied without knowing what it authorises, and gets copied between configurations until it means nothing.
  `truncate.allowed.resources` cannot be satisfied without naming the blast radius (§5.1).
* **Keeping the connector's Debezium baseline on a released version.** Rejected: the baseline must be at or above the `DeduplicatingBuffer` flush fix, on which §7.2 depends for correctness rather than throughput. 3.7.0-SNAPSHOT, tracked forward.

## Open Questions

1. **Does non-key `_id` support (§7.3) ship in v1**, or does v1 restrict `primary.key.mode` to `record_key`/`kafka`/`none` and defer `record_value`/`record_header`?
   Deferring is the leaner v1 and removes LSN normalisation from the critical path.
   Recommendation: defer, and ship the startup validation that rejects the unsupported modes with a message pointing at the follow-up.
2. **Default `batch.size`.** The shared default is 500; V1 uses 2000 and V2 uses 50.
   With a single request in flight, batch size is the primary throughput lever, which argues for a larger default than 500.
   Set by benchmark (§ Testing).
   The benchmark should be run knowing what it is protecting against.
   The community's position on V1's defaults ([#136](https://github.com/confluentinc/kafka-connect-elasticsearch/issues/136)) is that the hazard is not batch size on its own but the *interaction* of `batch.size=2000`, `max.buffered.records=20000` and a fixed `flush.timeout.ms`: a task that cannot flush the accumulated buffer inside the window fails, which is the condition users describe in [#103](https://github.com/confluentinc/kafka-connect-elasticsearch/issues/103) and [#571](https://github.com/confluentinc/kafka-connect-elasticsearch/issues/571).
   §8 removes that interaction, because a flush timeout here pauses the consumer rather than failing the task, and one request in flight bounds the buffer to a single batch.
   A larger default is therefore safe here in a way it was not for V1, and that is the reason to raise it rather than a preference for throughput.
3. **`string.mapping.mode` default.** `text`+`keyword` is the flexible choice and doubles index size; `keyword` is the CDC-correct choice for identifier-like columns.
   Possibly decide per column using source metadata (length, whether it is part of a key) rather than globally.

**Settled.** Recorded here so they are not reopened without new information:

* *Which `debezium-sink` version to baseline against*: **3.7.0-SNAPSHOT**, tracked forward (§1).
* *Where the Debezium Server adapter lives*: **`debezium-server`**, as `debezium-server-elasticsearch`, holding only the change consumer and its scaffolding.
  All other work is in `debezium-connector-elasticsearch` (§1, §14, implementation step 14).
* *Whether `mapping.mode` manages lifecycle policies*: **no**.
  The connector owns mappings and exposes `mapping.composed.of` so users compose their own lifecycle component templates (§6.3).
* *Whether destructive truncate needs a second acknowledgement*: **yes, as an allow-list** (`truncate.allowed.resources`), not a confirmation boolean (§5.1).

## Future Work

* **AWS SigV4 as a shipped `connection.credentials.provider.class` implementation, scheduled for phase 2.**
  This is the highest-demand *authentication* request on the Confluent connector's tracker: [#60](https://github.com/confluentinc/kafka-connect-elasticsearch/issues/60) has been open since 2017 with 11 reactions and 14 comments, joined by [#514](https://github.com/confluentinc/kafka-connect-elasticsearch/issues/514).
  Amazon-managed Elasticsearch is a documented V1 limitation, and V2's published `auth.type` values do not include a SigV4 option.
  v1 ships the SPI (§10.2) so that the capability is reachable by a user-supplied class; phase 2 ships a supported implementation so it is reachable out of the box.
  Recording it as phase 2 rather than as an open-ended "once the SPI is proven" is deliberate: nothing else is queued to prove the SPI, so that phrasing would have deferred it indefinitely.
* Semantic-search ergonomics: mapping Debezium vector types to `dense_vector` with configurable similarity and index options, and optionally driving an inference ingest pipeline, a natural pairing with Debezium's vector support and the cache/search-invalidation work in DDD-16.
* A reindex-assist mode that detects an incompatible mapping change and drives a reindex-to-new-index-plus-alias-swap, instead of only diagnosing the conflict (§6.3).
* Cross-cluster replication awareness: refusing to write to a follower index, which currently surfaces as an opaque cluster block.

## References

* [Confluent Elasticsearch Service Sink (V1) configuration reference](https://docs.confluent.io/kafka-connectors/elasticsearch/current/configuration_options.html)
* [Confluent Elasticsearch Sink V2 for Confluent Cloud](https://docs.confluent.io/cloud/current/connectors/cc-elasticsearch-sink-v2/cc-elasticsearch-sink-v2.html): features, V1-to-V2 migration, changed behaviour, and full property reference
* [Elasticsearch API compatibility](https://www.elastic.co/docs/reference/elasticsearch/rest-apis/compatibility): REST API compatibility is honoured across exactly one major version
* [Elasticsearch Java API Client](https://www.elastic.co/docs/reference/elasticsearch/clients/java) and [elastic/elasticsearch-java](https://github.com/elastic/elasticsearch-java)
* Update API vs. external versioning: [elastic/elasticsearch#5661](https://github.com/elastic/elasticsearch/issues/5661), [#25996](https://github.com/elastic/elasticsearch/issues/25996), [#69232](https://github.com/elastic/elasticsearch/issues/69232)
* [Index API](https://www.elastic.co/guide/en/elasticsearch/reference/current/docs-index_.html): the `version` parameter "must be a non-negative long number", which is the constraint §7.4's tuple key works around
* [Update API](https://www.elastic.co/guide/en/elasticsearch/reference/current/docs-update.html): scripted updates, `ctx.op = "noop"`, `upsert` and `scripted_upsert`, the basis of §7.4's scripted guard
* [`debezium-sink` module source](https://github.com/debezium/debezium/tree/main/debezium-sink/src/main/java/io/debezium): `AbstractChangeEventSink`, `SinkConnectorConfig`, `DebeziumSinkRecord`, `bindings/kafka/KafkaDebeziumSinkRecord`, `dlq/ErrorReporter`, `batch/DeduplicatingBuffer`, `naming/CollectionNamingStrategy`
* [`debezium-server-jdbc` adapter](https://github.com/debezium/debezium-server/tree/main/debezium-server-jdbc): `JdbcChangeConsumer`, `ChangeEventToSinkRecordConverter`
* [Debezium JDBC connector documentation](https://debezium.io/documentation/reference/stable/connectors/jdbc.html) and [JDBC sink batch support / reduction buffer](https://debezium.io/blog/2023/12/20/JDBC-sink-connector-batch-support/)
* [Debezium Server documentation](https://debezium.io/documentation/reference/stable/operations/debezium-server.html)
* [Debezium partition routing SMT](https://debezium.io/documentation/reference/stable/transformations/partition-routing.html)
* [Dainius Jocas: Preventing data corruption in Elasticsearch with the Kafka Connect ES sink](https://www.jocas.lt/blog/post/kc_es_data_consistency/)
* [rmoff: Kafka Connect and Elasticsearch](https://rmoff.net/2019/10/07/kafka-connect-and-elasticsearch/)
* [debezium-examples `unwrap-smt/es-sink.json`](https://github.com/debezium/debezium-examples/blob/main/unwrap-smt/es-sink.json): the multi-SMT arrangement §4.1 removes