# GSoC 2026 Final Submission - Kodukulla Mohnish Mythreya

**Organization:** JBoss Community

**Project:** Real-Time CDC Integration for LangChain & LangGraph using Debezium (PyDebeziumAI)

---

## Abstract

With rapid advances in AI, many applications rely on retrieval-augmented generation (RAG) over domain data stored in relational databases.
Many current pipelines can become stale because they depend on polling or periodic batch reloads.
PyDebeziumAI is a Python library that integrates [Debezium](https://debezium.io/) Change Data Capture (CDC) with [LangChain](https://www.langchain.com/) and [LangGraph](https://www.langchain.com/langgraph).
It uses Debezium CDC events through an embedded Debezium Engine to keep AI application context synchronized with changes in the underlying database.

The library provides components for converting CDC events into LangChain `Document` objects, transforming and mapping database records, and synchronizing them with vector stores in real time.
It also provides pluggable vector-store adapters, retrieval integrations, example applications, and comprehensive documentation.
During GSoC, the project was extended with multiple vector-store adapters, batch compaction, concurrent processing, resilient event handling, release automation, and a live demonstration application.
This work establishes a bridge between Debezium's change data capture capabilities and modern Python-based AI and retrieval workflows.

---

## Code and Artifacts

List of repositories, releases, and documentation connected with this GSoC project:
* **Source Code Repository**: [debezium/debezium-ai-python](https://github.com/debezium/debezium-ai-python)
* **Documentation**: [Read the Docs - PyDebeziumAI](https://debezium-ai-python.readthedocs.io/)
* **Additional Contributions**:
  * **Debezium Quarkus**: [debezium/debezium-quarkus#68](https://github.com/debezium/debezium-quarkus/pull/68) - Created an `ErrorHandler` for capturing destinations.
  * **Debezium Examples**: [debezium/dbz#1810](https://github.com/debezium/dbz/issues/1810) - Implemented automated end-to-end testing frameworks for multiple example scenarios.

### Ingestion Layer
Exposes `JsonIngestionHandler` and `ConnectRecordIngestionHandler` to interface directly with the Java-embedded Debezium engine. It parses relational event records, routing them through callback loops, and captures transaction operation flags (`c` for create, `u` for update, `d` for delete, `r` for snapshot/read).

### Transformation & Mapping Layer
* **`DocumentBuilder`**: Maps relational rows into LangChain `Document` schemas.
* **`ProjectionPolicy`**: Uses content-template strings (e.g., `Product: $name
Price: $price`) to dynamically structure document contents, and maps row columns directly into document metadata dicts.
* **`IdStrategy`**: Implements `TablePkIdStrategy` and `CompositeIdStrategy` to guarantee deterministic, stable document UUIDs mapping database primary keys.

### Synchronization & Compaction Layer
* **`SyncManager`**: Automates upserts and deletes against the downstream vector stores based on incoming CDC events. Includes custom retry rules and a DeadLetterQueue (DLQ) for failed syncs.
* **CDC Batch Compaction**: Implements a state-aware order-preserving algorithm that collapses multi-mutation sequences for a single primary key in a batch (e.g., `Insert + Update -> single Upsert` or `Update + Delete -> single Delete`) before flushing, eliminating redundant vector database writes.
* **Parallelization**: Integrates Python `ThreadPoolExecutor` to run document transformation and embedding generation concurrently during batch sync operations.

### Vector Store Adapters & Retrieval
Built production-ready adapters with bulk interfaces and metadata filtering:
* **`ChromaAdapter`** ([chroma.py](https://github.com/debezium/debezium-ai-python/blob/main/pydebeziumai/adapters/chroma.py))
* **`MilvusAdapter`** ([milvus.py](https://github.com/debezium/debezium-ai-python/blob/main/pydebeziumai/adapters/milvus.py))
* **`PGVectorAdapter`** ([pgvector.py](https://github.com/debezium/debezium-ai-python/blob/main/pydebeziumai/adapters/pgvector.py))

Exposes retriever tool wrappers to plug synchronization pipelines directly into LangChain retrievers and LangGraph agents.

### Streamlit Live Visual Dashboard
Created an interactive 3-column live dashboard illustrating end-to-end MySQL-to-Milvus CDC replication:
1. **Mutation Console**: GUI form console executing raw SQL transactions on MySQL.
2. **CDC WAL Stream**: Sub-second fragment updating showing Debezium streaming activity.
3. **Semantic Search**: Retrieval interface executing live vector search queries against Milvus-Lite.

---

### Worklog

**Parent Tracker Issue**: [debezium/dbz#2066](https://github.com/debezium/dbz/issues/2066)

#### Initial Repository Setup (Pre-Work)
* **Pull Request**: [#1 - Initial repository setup: CI, formatting, and coding conventions](https://github.com/debezium/debezium-ai-python/pull/1) (Merged)
* **Details**: Established the Python project workspace, Ruff formatter/linter rules, typing standards, and the GitHub Actions CI sanity check workflow.

#### Weeks 1 - 3: Foundation & Core CDC Loop

* **Week 1 (May 25 – May 31): Ingestion & Logical Type Conversion Engine**
  * **Issue**: [debezium/dbz#2003](https://github.com/debezium/dbz/issues/2003)
  * **Pull Requests**:
    * [#3 - Canonical event models and contracts](https://github.com/debezium/debezium-ai-python/pull/3) (Merged)
    * [#4 - Logical type conversion engine and jar setup tool](https://github.com/debezium/debezium-ai-python/pull/4) (Merged)
    * [#5 - Ingestion pipeline handlers and base vector-store adapter](https://github.com/debezium/debezium-ai-python/pull/5) (Merged)
  * **Details**: Integrated embedded JVM classloader bindings via JPype. Coded the `DebeziumEventModel` schemas (backed by Pydantic) to model CDC record structures.

* **Week 2 (June 1 – June 7): Transformation & Mapping Layer**
  * **Issue**: [debezium/dbz#2018](https://github.com/debezium/dbz/issues/2018)
  * **Pull Request**: [#6 - Transformation layer (document builder, ID strategy, and projection policy)](https://github.com/debezium/debezium-ai-python/pull/6) (Merged)
  * **Details**: Designed `DocumentBuilder` converting database CDC events into LangChain `Document` schemas using parameterized projection templates and primary key-based deterministic ID generation.

* **Week 3 (June 8 – June 14): Chroma Vector Adapter & Synchronization Manager**
  * **Issue**: [debezium/dbz#2040](https://github.com/debezium/dbz/issues/2040)
  * **Pull Requests**:
    * [#7 - Core SyncManager for routing CDC operations](https://github.com/debezium/debezium-ai-python/pull/7) (Merged)
    * [#8 - Chroma adapter and end-to-end sync pipeline tests](https://github.com/debezium/debezium-ai-python/pull/8) (Merged)
  * **Details**: Implemented the central `SyncManager` component to coordinate updates/deletes and integrated `ChromaAdapter` for real-time document synchronization.

#### Phase 2: LangChain / LangGraph Integrations & Multi-Adapter Expansion
*(Note: University resumed on July 5, transitioning weekly availability from 35–40 hrs/week to 25–30 hrs/week from Week 6 onward, as outlined in the original proposal's "Other Commitments" section.)*

* **Week 4 (June 15 – June 21): Conversational Chatbot & Retrieval Layer**
  * **Issue**: [debezium/dbz#2074](https://github.com/debezium/dbz/issues/2074)
  * **Pull Requests**:
    * [#9 - Core retrieval layer and LangGraph node support](https://github.com/debezium/debezium-ai-python/pull/9) (Merged)
    * [#10 - Real-time RAG chatbot demonstration example](https://github.com/debezium/debezium-ai-python/pull/10) (Merged)
  * **Details**: Created retriever wrappers to expose vector storage as native LangChain Retrievers. Built a real-time conversational agent using LangGraph and LangChain.

* **Week 5 (June 22 – June 28): Milvus & PGVector Adapters**
  * **Issue**: [debezium/dbz#2128](https://github.com/debezium/dbz/issues/2128)
  * **Pull Requests**:
    * [#11 - PGVector adapter and core logging improvements](https://github.com/debezium/debezium-ai-python/pull/11) (Merged)
    * [#12 - Milvus adapter and integration tests](https://github.com/debezium/debezium-ai-python/pull/12) (Merged)
  * **Details**: Coded the `MilvusAdapter` (using `milvus-lite`) and the `PGVectorAdapter` (leveraging SQLAlchemy/Psycopg), adding integration tests for both.

* **Week 6 (July 6 – July 12): Metadata Filtering & Reactive Agent**
  * **Issue**: [debezium/dbz#2156](https://github.com/debezium/dbz/issues/2156)
  * **Pull Requests**:
    * [#13 - Unified metadata filtering API and adapter translations](https://github.com/debezium/debezium-ai-python/pull/13) (Merged)
    * [#14 - Reactive LangGraph agent demonstration example](https://github.com/debezium/debezium-ai-python/pull/14) (Merged)
  * **Details**: Added structured metadata filtering to retriever queries. Implemented a reactive LangGraph agent scenario.

* **Week 7 (July 13 – July 19): Sphinx Documentation Framework**
  * **Issue**: [debezium/dbz#2198](https://github.com/debezium/dbz/issues/2198)
  * **Pull Requests**:
    * [#15 - Initialize Sphinx framework and API reference docs](https://github.com/debezium/debezium-ai-python/pull/15) (Merged)
    * [#16 - Add quickstart guide, architecture overview and CI check](https://github.com/debezium/debezium-ai-python/pull/16) (Merged)
  * **Details**: Configured autodoc-based reference docs, and created Quickstart and Architecture guides.

* **Week 8 (July 20 – July 26): Performance Compaction & Thread Concurrency**
  * **Issue**: [debezium/dbz#2229](https://github.com/debezium/dbz/issues/2229)
  * **Pull Requests**:
    * [#17 - Implement batching interfaces for vector store adapters and add benchmark script](https://github.com/debezium/debezium-ai-python/pull/17) (Merged)
    * [#18 - Implement batch compaction, sync_batch, and ThreadPoolExecutor parallelization](https://github.com/debezium/debezium-ai-python/pull/18) (Merged)
  * **Details**: Implemented CDC Batch Compaction logic to collapse redundant row modifications in a batch. Added concurrency support using `ThreadPoolExecutor`.

* **Week 9 (July 27 – August 2): Packaging & CI/CD Release Automation**
  * **Issue**: [debezium/dbz#2298](https://github.com/debezium/dbz/issues/2298)
  * **Pull Requests**:
    * [#21 - Configure Hatchling dynamic versioning and create package release guide](https://github.com/debezium/debezium-ai-python/pull/21) (Merged)
    * [#22 - Permanent error bypass and schema skip logic; configure PyPI OIDC release workflow](https://github.com/debezium/debezium-ai-python/pull/22) (Merged)
  * **Details**: Configured `pyproject.toml` dynamic versioning. Set up PyPI publishing with GitHub Actions OIDC Trusted Publishing.

* **Week 10 (August 3 – August 9): Release Verification & Sphinx RTD Integrations**
  * **Issue**: [debezium/dbz#2402](https://github.com/debezium/dbz/issues/2402)
  * **Pull Request**: [#23 - Configure Read the Docs Sphinx path and add manual publish trigger](https://github.com/debezium/debezium-ai-python/pull/23) (Merged)
  * **Details**: Connected Read the Docs build pipeline. Wrote release verification scripts (`tools/verify_release.py`).

* **Week 11 (August 10 – August 16): Streamlit Live Dashboard & Resiliency Hardening**
  * **Issue**: [debezium/dbz#2423](https://github.com/debezium/dbz/issues/2423)
  * **Pull Requests**:
    * [#24 - Streamlit live dashboard, vector idempotency, tombstone handling, and E2E tests](https://github.com/debezium/debezium-ai-python/pull/24) (Open / Under Review)
    * [#25 - Resolve documentation typos, mypy settings, projection metadata leak, sanitizer binary coercion, Milvus security escaping, and publish workflow gates](https://github.com/debezium/debezium-ai-python/pull/25) (Open / Under Review)
  * **Details**: Created Streamlit live CDC console. Handled tombstone events gracefully. Idempotent adapter upserts. Hardened Milvus escaping and publish gates.


---

## Articles & Talks

* **Blog Post (To be delivered)**: A technical blog post introducing PyDebeziumAI to the wider developer community is planned to be delivered and published on the official [Debezium Blog](https://debezium.io/blog/).
* **Community Showcase (To be delivered)**: A live presentation of the project's design and features is planned for the [Debezium Community Showcase](https://docs.google.com/document/d/1f3RlmbI9N7lto8C3HQW_8RBo0dz6jTKZUzmAwPWvGFc/).
* **Zulip Discussion Thread**: Ongoing design debates and project updates are tracked in the [#community-gsoc > PyDebeziumAI](https://debezium.zulipchat.com/#narrow/channel/573881-community-gsoc/topic/PyDebeziumAI/) Zulip topic.

---

## Future Work

* **Common Embedded Core Assembly / Maven-Free Installation**: Separate the shared Debezium embedded dependencies from connector-specific artifacts and publish a pre-built `debezium-embedded-common` archive to Maven Central, allowing `setup_jars.py` to download the common core plus a selected connector archive directly, without requiring a local Maven installation. This work is tracked in issue [debezium/dbz#2466](https://github.com/debezium/dbz/issues/2466) and proposed in PR [debezium/debezium#7859](https://github.com/debezium/debezium/pull/7859).
* **Broaden Debezium Connector Support**: Make adding new Debezium connectors (e.g., PostgreSQL, MongoDB, and others) easier through the Python-side classloader/assembly mechanism.
* **Expand Vector Store Coverage**: Implement adapters for Pinecone, Qdrant, and Weaviate.
* **Pydantic AI Integration**: Integrate PyDebeziumAI with Pydantic AI for structured tool outputs, schema validation, and typed agent state in RAG pipelines.
* **CDC-Driven Session Memory**: Extend the adapter abstraction beyond vector stores to LangGraph memory/checkpoint backends, enabling CDC-driven synchronization of conversational session state and staleness-triggered summarization of long-running sessions.
* **Native Async I/O**: Replace `ThreadPoolExecutor`-based concurrency with an async-native `AsyncSyncManager` and `asyncio` event loop support, to interface directly with async-native vector store client APIs.
* **Schema Evolution & Projection Drift Detection**: Detect source-schema changes that invalidate an existing `ProjectionPolicy` or metadata mapping and surface actionable drift information to developers. Introduce lightweight buffering for tombstone/delete and schema-change events where immediate downstream application could result in inconsistent vector-store state.
* **Conditional Document Ingestion & Selective Embedding**: Add rule-based filtering to skip or defer embedding for specific records (e.g., `category = 'test'`) before they reach the transformation layer.
