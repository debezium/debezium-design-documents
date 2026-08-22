# GSoC 2026 Final Submission - Kartik Angiras

**Organization:** Red Hat JBoss (Debezium)
**Project:** Debezium Source Connector for Milvus

---

## Abstract

Milvus is one of the most widely deployed open source vector databases, holding the embeddings behind retrieval-augmented generation, semantic search, and recommendation systems. Those embeddings change constantly as documents are re-indexed, products are delisted, and users exercise deletion requests, but Milvus exposes no change stream over its gRPC API. The AI systems built on it therefore cannot tell when their ground truth moved. A RAG pipeline keeps retrieving a chunk that was deleted, and a semantic cache keeps serving answers for content that no longer exists. The same gap hits everything else around the database, including keyword-search mirrors, companion caches, analytics pipelines, and backup and compliance jobs. Teams solve this today with dual writes or batch export, and both fail in known ways. Batch export cannot see deletes at all, because the row is already gone from Milvus by the time the job runs. Dual writes desynchronize on the first partial failure. This project closes that gap with an incubating Debezium source connector for Milvus.

The connector taps the internal message queue channel (pchannel) that Milvus publishes every write to before persisting it, reads Milvus's per-channel checkpoints from etcd to anchor its position, and calls the Milvus gRPC API for collection schemas and snapshot queries. It performs a consistent initial snapshot pinned to the etcd checkpoint TSO, hands off to streaming without loss or duplication, and re-orders events across virtual channels into strict Milvus TSO commit order using the timetick watermark protocol. Vectors are emitted as first-class Debezium logical types and every event carries the standard Debezium envelope, which makes the entire Kafka Connect sink ecosystem a Milvus integration with zero custom code.

---



## Code and Artifacts

**Project Repository**: [debezium/debezium-connector-milvus](https://github.com/debezium/debezium-connector-milvus)

### Connector skeleton and architectural core

- The standard Debezium connector contract, adapted to a database with no WAL: `MilvusConnector`, `MilvusConnectorTask`, `MilvusConnectorConfig`, `MilvusPartition`, `MilvusOffsetContext`, `MilvusSourceInfo`, `MilvusSourceInfoStructMaker`, `MilvusDatabaseSchema`, `MilvusErrorHandler`, and `MilvusChangeEventSourceFactory`.
- Most of the design work went into deciding what position means. Relational connectors have an LSN or binlog coordinate. Milvus has a timestamp oracle value (TSO) plus an MQ offset, and the two have to be tracked together, because the TSO defines commit order while the offset defines where to resume reading. `MilvusOffsetContext` carries both, plus the snapshot-completion flag that gates the handoff.
- `MilvusConnectorConfig` grew to 24 connector-specific options across grouped sections (connection, etcd, MQ, snapshot, ordering, buffering), with startup validation behind `milvus.startup.validation.enabled` so that misconfiguration fails at task start rather than mid-stream.



### Message queue consumer and offset management

- `MilvusMessageConsumer` and `KafkaMilvusMessageConsumer` subscribe to the raw Milvus pchannel topic on the Kafka MQ backend and expose it as a stream of `RawMilvusMessage`. `SeekPosition` models the three ways streaming can start: from an etcd checkpoint offset, from the earliest available message, or from the latest.
- The consumer does no interpretation. Everything above it treats the pchannel as an opaque byte stream with offsets, which is what made the wire format work below possible to layer on independently.



### CDC deserialization and wire format detection

- Milvus's internal MQ format is not a public contract, so building `MilvusProtoDeserializer` meant reading Message Proto and the Milvus source rather than documentation.
- Milvus writes messages in two shapes depending on version and code path, a msgpack batch envelope and a single protobuf message. `MilvusWireFormatDetector` and the `milvus.wire.format` option (`msgpack_batch`, `proto_single`, `auto`) let one connector handle both, and `MilvusWireFormatMismatchException` produces an actionable error instead of a protobuf parse failure.
- Milvus stores insert data column-wise while Debezium change events are row-wise. `MilvusColumnarPivot`, `MilvusFieldData`, `FieldDefinition`, and `MilvusRow` perform that pivot with explicit field definitions, and `MilvusValueConverter` maps Milvus types to Connect schemas: float vectors to `io.debezium.data.vector.FloatVector`, JSON and geometry fields to `io.debezium.data.Json`, with configurable decimal handling.



### TSO ordering engine

This is the core of the connector and the component that went through the most revisions.

- Milvus multiplexes several virtual channels (vchannels) onto one physical channel, and each vchannel advances its own timetick independently, so events arrive on the pchannel out of TSO order. `TimetickOrderingEngine` buffers events and holds each one until the global watermark, the minimum timetick across all known vchannels, passes that event's TSO. At that point no vchannel can still produce anything older, so the event can be released in strict commit order.

Two details matter in practice.

- **Bounded buffering.** `milvus.buffer.max.events` and `milvus.buffer.max.bytes` cap the buffer, and `MilvusBufferFullException` surfaces backpressure rather than letting the task OOM. A vchannel that stops ticking triggers a force-flush after `milvus.timetick.stall.timeout.ms`, which degrades ordering but never stalls indefinitely.
- **Intra-TSO ordering.** Milvus implements an upsert as a delete plus an insert sharing one TSO. Sorting by TSO alone is not enough; the pair has to emit delete-first, or downstream consumers end with the row deleted. Deletes now sort before inserts within a TSO bucket.



### Etcd checkpoint readers and Metadata clients

- Milvus stores its per-channel checkpoint, the `guarantee_ts` TSO and the corresponding MQ offset, in etcd and exposes it through no API. `ChannelCheckpoint`, `EtcdCheckpointReader`, `JetcdEtcdCheckpointReader`, and `EtcdInternalAPI` read it directly over jetcd, with `milvus.etcd.root.path` and `milvus.etcd.checkpoint.path` accommodating non-default Milvus deployments.
- Alongside it, `MilvusMetadataClient` and `MilvusServiceMetadataClient` wrap the Milvus gRPC API for collection discovery and schema resolution (`CollectionMetadata`, `MilvusCollectionSchema`, `VChannelMetadata`), including regex-based include/exclude list compilation and lazy client initialization so the connector does not open a gRPC connection it may never use.



### Snapshot phase and lossless handoff

- `MilvusSnapshotChangeEventSource`, `MilvusSnapshotter`, and `MilvusSnapshotQueryClient` implement the consistent initial snapshot. The connector reads the etcd checkpoint first, then queries every included collection with `consistency_level=Strong` and `guarantee_ts` pinned to the checkpoint TSO, emitting each row as `op=r`. Because the snapshot is anchored to the same checkpoint that streaming resumes from, the handoff loses nothing and duplicates nothing.
- Schemas are inferred rather than declared: from the Milvus metadata API during the snapshot, and from the first insert event while streaming. On-demand registration was added so that a delete arriving for a collection the connector has not yet seen an insert for still resolves a schema instead of failing.



### Packaging, dispatch, and streaming pipeline

- `MilvusChangeEvent`, `MilvusChangeRecordEmitter`, `MilvusEventDispatcher`, `MilvusEventMetadataProvider`, `MilvusCollectionId`, and `MilvusStreamingChangeEventSource` turn released events into Debezium change records.
- This layer dispatches first and advances the offset second. The connector never commits an offset for an event it has not emitted, which is what makes pause/resume and crash recovery converge.
- `MilvusStreamingPipelineIT` and `MilvusSnapshotHandoffIT` run against a real four-container stack (etcd, MinIO, Kafka, and Milvus configured with `mq.type: kafka`) started and torn down by the Maven build.



### Metrics and observability

- Dedicated JMX metrics for both phases: `MilvusSnapshotChangeEventSourceMetrics` and `MilvusStreamingChangeEventSourceMetrics`, each with its MXBean, wired through `MilvusChangeEventSourceMetricsFactory`.
- Beyond the standard Debezium metrics, these expose Milvus-specific state, including TSO watermark lag and consumer position resolution.



### Heartbeat Mechanism

- Interval-based heartbeats (`heartbeat.interval.ms`) prove liveness and advance offsets on quiet collections. The fix that took longest to find was dispatch ordering again. Heartbeats had to move after the watermark update, or a heartbeat could commit an offset ahead of events still buffered in the ordering engine.

---



## Worklog

**Parent Tracker Issue:** [debezium/dbz#2065](https://github.com/debezium/dbz/issues/2065)

### Initial Repository Setup (Pre-Work)

**Week 1 (May 25 - May 31): Source Connector Documentation**

- **Issue:** [debezium/dbz#1930](https://github.com/debezium/dbz/issues/1930)
- **Pull Request:** [Debezium Source Connector Development Guide](https://github.com/debezium/debezium/pull/7495) *(still under active review as of the submission date — the PR has not yet landed)*

**Details:** A general guide for the development of a Debezium source connector from scratch including core aspects such as streaming, offset, snapshot etc.

### Phase 1: Foundation & Core Streaming Pipeline (Weeks 2 - 8)

**Week 2 (June 1 - June 7): Bootstrapping & Architectural Skeleton**

- **Issue:** [debezium/dbz#2028](https://github.com/debezium/dbz/issues/2028) - Bootstrapping and Architecture Skeleton
- **Pull Request:** [#1 - Streaming Pipeline](https://github.com/debezium/debezium-connector-milvus/pull/1)
- **Details:** Coded `MilvusConnector`, `MilvusConnectorTask`, `MilvusConnectorConfig`, `MilvusPartition`, `MilvusOffsetContext`, `MilvusSourceInfo` and `MilvusSourceInfoStructMaker`, `MilvusDatabaseSchema`, `MilvusErrorHandler`, and `MilvusChangeEventSourceFactory`. Defined the connector's position model as a paired timestamp oracle value (TSO) and MQ offset, since Milvus has no WAL or binlog coordinate to resume from.

**Week 3 (June 8 - June 14): Milvus MQ Wire Protocol Study**

- **Issue:** [debezium/dbz#2068](https://github.com/debezium/dbz/issues/2068) - Message Queue and Offset
- **Details:** Milvus source establishes how `InsertRequest`, `DeleteRequest`, and `TimeTick` messages are framed on the physical channel, and how the per-row `timestamps` field relates to `MsgBase.timestamp`. The findings drove the deserializer design in Weeks 4 and 5, and the `timestamps` detail later turned out to be the fix for a critical data-loss bug.

**Week 4 (June 15 - June 21): Ingestion, Deserialization & Wire Format Detection**

- **Issues:** [debezium/dbz#2068](https://github.com/debezium/dbz/issues/2068), [debezium/dbz#2089](https://github.com/debezium/dbz/issues/2089) - CDC Event Deserialization, [debezium/dbz#2124](https://github.com/debezium/dbz/issues/2124) - Wire Format Detection
- **Pull Request:** [#1 - Streaming Pipeline](https://github.com/debezium/debezium-connector-milvus/pull/1)
- **Details:** Implemented `MilvusMessageConsumer` and `KafkaMilvusMessageConsumer` to subscribe to the raw pchannel topic, with `RawMilvusMessage` and `SeekPosition` modelling checkpoint, earliest, and latest starts. Coded `MilvusProtoDeserializer`, plus `MilvusWireFormatDetector` and the `milvus.wire.format` option so one connector handles both the msgpack-batch and single-protobuf shapes, with `MilvusWireFormatMismatchException` producing an actionable error. Built the column-to-row pivot (`MilvusColumnarPivot`, `MilvusFieldData`, `FieldDefinition`, `MilvusRow`) and `MilvusValueConverter`, mapping float vectors to `io.debezium.data.vector.FloatVector` and JSON/geometry fields to `io.debezium.data.Json`.

**Week 5 (June 22 - June 28): TSO Ordering Engine**

- **Issue:** [debezium/dbz#2129](https://github.com/debezium/dbz/issues/2129) - TimeStamp Oracle Ordering Engine
- **Pull Request:** [#1 - Streaming Pipeline](https://github.com/debezium/debezium-connector-milvus/pull/1)
- **Details:** Designed `TimetickOrderingEngine`, which holds each event until the global watermark, the minimum timetick across all known vchannels, passes that event's TSO. At that point no vchannel can still produce anything older, so the event releases in strict commit order. Added bounded buffering via `milvus.buffer.max.events` and `milvus.buffer.max.bytes` with `MilvusBufferFullException` for backpressure, and a force-flush after `milvus.timetick.stall.timeout.ms` so a silent vchannel degrades ordering instead of stalling the task indefinitely. Also updated module dependencies, added build profiles, and reworked exception handling.

**Week 6 (June 29 - July 5): Packaging, Dispatching & Streaming Pipeline Integration Test**

- **Issue:** [debezium/dbz#2144](https://github.com/debezium/dbz/issues/2144) - Packaging and Dispatching for the streaming pipeline
- **Pull Request:** [#1 - Streaming Pipeline](https://github.com/debezium/debezium-connector-milvus/pull/1)
- **Details:** Coded `MilvusChangeEvent`, `MilvusChangeRecordEmitter`, `MilvusEventDispatcher`, `MilvusEventMetadataProvider`, `MilvusCollectionId`, and `MilvusStreamingChangeEventSource` to turn events released by the ordering engine into Debezium change records. Established the rule that governs this layer: dispatch first, advance the offset second, so the connector never commits an offset for an event it has not emitted, which is what makes pause, resume, and crash recovery converge. Wrote the first integration test, `MilvusStreamingPipelineIT`, against a real four-container stack (etcd, MinIO, Kafka, and Milvus configured with `mq.type: kafka`) started and torn down by the Maven build. This completed the end-to-end streaming pipeline: raw pchannel bytes in, ordered Debezium change events out.

**Week 7 (July 6 - July 12): Value Conversion, Notifications & Offset Seeking**

- **Issue:** [debezium/dbz#2065](https://github.com/debezium/dbz/issues/2065) (Parent Tracker)
- **Pull Request:** [#1 - Streaming Pipeline](https://github.com/debezium/debezium-connector-milvus/pull/1)
- **Details:** Added Debezium notification handling, latest-offset seeking so a connector can start from the head of the pchannel instead of the checkpoint, configurable decimal handling modes, and `List<Float>` support in the FloatVector conversion path. Extended collection registration to carry field types through to the emitted schema.

**Week 8 (July 13 - July 19): Schema Registration, Lifecycle & Streaming Pipeline Merge**

- **Issues:** [debezium/dbz#2065](https://github.com/debezium/dbz/issues/2065) (Parent Tracker), [debezium/dbz#2144](https://github.com/debezium/dbz/issues/2144)
- **Pull Request:** [#1 - Streaming Pipeline](https://github.com/debezium/debezium-connector-milvus/pull/1)
- **Details:** Pivoted schema registration onto explicit `FieldDefinition` and `MilvusRow` types, reworked the connector lifecycle and streaming loop mechanics, upgraded to `3.7.0-SNAPSHOT`, and reorganized the configuration into grouped sections. Addressed review from Chris Cranford and Mario Fiore Vitale: `MILVUS_TOKEN` converted to a `PASSWORD` type field, the consumer closed when streaming ends, the closed flag made `volatile`, the gRPC version sourced from `dependencyManagement`, and an unused `commons-lang3` dependency removed.



### Phase 2: Etcd Checkpoints & Milvus Metadata (Week 9)

**Week 9 (July 20 - July 26): Etcd Checkpoint Readers & Metadata Clients**

- **Issues:** [debezium/dbz#2130](https://github.com/debezium/dbz/issues/2130) - Milvus Metadata, [debezium/dbz#2131](https://github.com/debezium/dbz/issues/2131) - Etcd Checkpoint
- **Pull Request:** [#4 - ETCD Checkpoint Reader and Metadata Clients](https://github.com/debezium/debezium-connector-milvus/pull/4)
- **Details:** Milvus stores its per-channel checkpoint, the `guarantee_ts` TSO and the matching MQ offset, in etcd and exposes it through no API, so the connector reads it directly. Implemented `ChannelCheckpoint`, `EtcdCheckpointReader`, `JetcdEtcdCheckpointReader`, and `EtcdInternalAPI` over jetcd, with `milvus.etcd.root.path` and `milvus.etcd.checkpoint.path` accommodating non-default deployments, and simplified `msgId` handling in the checkpoint model. Wrapped the Milvus gRPC API in `MilvusMetadataClient` and `MilvusServiceMetadataClient` for collection discovery and schema resolution (`CollectionMetadata`, `MilvusCollectionSchema`, `VChannelMetadata`), with pattern matching for collection selection. Work on the checkpoint readers began on July 10 and landed as one pull request this week.



### Phase 3: Snapshot Phase & Streaming Handoff (Week 10)

**Week 10 (July 27 - August 2): Snapshot Phase, Lossless Handoff & Collection Filtering**

- **Issue:** [debezium/dbz#2230](https://github.com/debezium/dbz/issues/2230) - Snapshot Phase and Streaming Handoff
- **Pull Request:** [#5 - Snapshot Phase and Streaming Handoff](https://github.com/debezium/debezium-connector-milvus/pull/5)
- **Details:** Implemented `MilvusSnapshotChangeEventSource`, `MilvusSnapshotter`, and `MilvusSnapshotQueryClient`, querying every included collection with `consistency_level=Strong` and `guarantee_ts` pinned to the etcd checkpoint TSO, so the snapshot-to-streaming handoff neither loses nor duplicates a row. Added on-demand schema registration from the Milvus metadata API so a delete arriving for a collection the connector has not yet seen an insert for still resolves a schema instead of failing, plus robust vchannel resolution with checkpoint retry and handling for missing checkpoints. Added regex-based collection include/exclude list compilation, lazy Milvus client initialization, and snapshot record structure validation. Wrote `MilvusSnapshotHandoffIT` to cover the handoff end to end.



### Phase 4: Observability, Hardening & Production Validation (Weeks 11 - 13)

**Week 11 (August 3 - August 9): Metrics & Heartbeat Mechanism**

- **Issues:** [debezium/dbz#2282](https://github.com/debezium/dbz/issues/2282) - Metrics and Observability, [debezium/dbz#2328](https://github.com/debezium/dbz/issues/2328) - Heartbeat Mechanism, [debezium/dbz#2339](https://github.com/debezium/dbz/issues/2339)
- **Pull Requests:** [#6 - Streaming and Snapshot Metrics and Observability](https://github.com/debezium/debezium-connector-milvus/pull/6), [#7 - Heartbeat Mechanism](https://github.com/debezium/debezium-connector-milvus/pull/7)
- **Details:** Built dedicated JMX metrics for both phases, `MilvusSnapshotChangeEventSourceMetrics` and `MilvusStreamingChangeEventSourceMetrics` with their MXBeans, wired through `MilvusChangeEventSourceMetricsFactory`, including a Milvus-specific TSO watermark lag gauge and consumer position resolution tracking so an operator can tell an idle Milvus apart from a stuck vchannel. The metrics work ran through the end of July and merged at the start of this period. Implemented interval-based heartbeats and connector configuration validation, then moved heartbeat dispatch to after the watermark update so a heartbeat cannot commit an offset ahead of events still buffered in the ordering engine. Building this surfaced a Debezium core bug where `heartbeat.action.query` throws a NullPointerException for non-JDBC connectors instead of failing validation.

**Week 12 (August 10 - August 16): Documentation, Demo Run & Critical Ordering Bugs**

- **Issues:** [debezium/dbz#2346](https://github.com/debezium/dbz/issues/2346) - Add repository README.md, [debezium/dbz#2437](https://github.com/debezium/dbz/issues/2437) - Stalled Channel Watermarks
- **Pull Requests:** [#8 - Add README.md](https://github.com/debezium/debezium-connector-milvus/pull/8)
- **Details:** Wrote the full repository README covering features. Ran the demo pipeline end to end against a live Milvus cluster, which surfaced three critical ordering bugs the unit suite had missed. Began the fixes: TSO fallback in the deserializer and channel-level watermark advancement in the ordering engine.

**Week 13 (August 17 - August 23): Ordering Fix Merge, Test Coverage & Write-Up**

- **Issue:** [debezium/dbz#2437](https://github.com/debezium/dbz/issues/2437)
- **Pull Request:** [#10 - Fix streaming watermark advancement and upsert event ordering](https://github.com/debezium/debezium-connector-milvus/pull/10)
- **Details:** Landed all three fixes with unit coverage: per-row `timestamps[0]` fallback in `MilvusProtoDeserializer`, `TimetickOrderingEngine.updateChannelWatermark` for channel-level timeticks, and delete-before-insert sorting within a TSO bucket. Added `waitForConsumerPositionResolved` to `MilvusSnapshotHandoffIT` for test reliability and removed the unsupported heartbeat action query configuration. Wrote the blog post, the use-case analysis, and this report.



## Articles & Talks

**Blog Post (pending publication):** *"Milvus Source Connector for Debezium"*, drafted and planned for publication on the official Debezium Blog. Covers why vector databases need CDC, a dissection of the Milvus write path and the connector's ordering model, and a complete, reproducible Milvus-to-Kafka to tutorial walkthrough.

**Community Showcase (To be delivered):** A live presentation of the connector's design, and the demo is planned for the Debezium Community Showcase.

---



## Future Work

**Critical Data Type & Embedding Edge Cases:** Boundary bugs in the deserialization path are tracked in [debezium/dbz#2362](https://github.com/debezium/dbz/issues/2362). `BinaryVector` dimension is measured in bits but shared a code branch with `Int8Vector` that used `dim` directly as a byte count, so a dim=128 binary vector was split into 128-byte chunks and most rows produced nothing. `SparseFloatArray.contents` is a per-row list but was passed whole into a single JSON builder, merging every row into one blob. A `DeleteRequest` carrying 100 primary keys became one Delete event whose emitter called `list.get(0)`, silently dropping 99 tombstones. And the `valid_data` null bitmap that Milvus 2.5+ sends alongside nullable fields was never read, so nulls arrived as `0` and `""`, indistinguishable from real values.

**Milvus 2.6 Support & Validation:** *(Tracked under [debezium/dbz#2065](https://github.com/debezium/dbz/issues/2065))*. Milvus 2.6 introduces the [Streaming Service](https://milvus.io/docs/streaming_service.md) and [Woodpecker](https://milvus.io/docs/woodpecker_architecture.md), a purpose-built WAL on object storage that replaces the external message queue. Kafka and Pulsar remain supported backends in 2.6, so the connector's approach should still hold, but three things need verifying against a real 2.6 cluster: that the etcd checkpoint key layout the connector reads is unchanged, since it depends on a Milvus-internal contract rather than a public API; that pchannel framing and the timetick protocol behave identically under the Streaming Service, which now assigns TSOs itself; and that per-row timestamp semantics survive. The pchannel and vchannel model itself is preserved in 2.6, with each pchannel mapping to a WAL stream and each vchannel to a collection shard, so `TimetickOrderingEngine` should carry over unchanged.

**Working Wire-Format Auto-Detection:** *(Tracked under [debezium/dbz#2124](https://github.com/debezium/dbz/issues/2124))* `milvus.wire.format=auto` is the documented default, but the detector is not invoked on the streaming path, so deployments have to set `proto_single` or `msgpack_batch` explicitly. This needs its own issue and is the highest-value fix for new users, since it is the first thing they hit.

---

