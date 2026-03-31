# Debezium: Host-Based Pipeline Deployment for the Debezium Platform

## About Me

1. **Name:** Yang Gao (GitHub: [JerryGao0805](https://github.com/JerryGao0805))
2. **zulip message:** [Zulip link](https://debezium.zulipchat.com/#narrow/channel/573881-community-gsoc/topic/newcomers/near/582504580)
3. **University:** Stevens Institute of Technology — M.S. in Computer Science (Dec 2026 graduation)
4. **Contact:** yanggao0805@gmail.com
5. **Timezone:** EST (GMT-4)
6. **Resume:** [Resume link](https://drive.google.com/file/d/1dbQ4ro6Etd9ZtTyQfATfeMZTfL27WsnP/view?usp=drive_link)

### Mentors

- Mario Fiore Vitale
- Giovanni Panice

---

## Code Contribution

- [**debezium/dbz#1747**](https://github.com/debezium/debezium-platform/pull/323) — Fixed a NullPointerException in `LogStreamingService` where a null log line was passed to the consumer instead of being skipped. This was in the log streaming path that this project directly extends.

- [**debezium/dbz#1752**](https://github.com/debezium/debezium-platform/pull/325) — Fixed copy-paste errors in `GlobalExceptionMapper` where `UnsupportedOperationException` and `IllegalArgumentException` both returned "resource not found" instead of their correct error messages.

- [**debezium/dbz#1759**](https://github.com/debezium/debezium-platform/pull/326) — Fixed mock handler for `/api/connections` endpoint returning `destinationsData` instead of `connectionsData`, causing all connection-related unit tests to silently validate against wrong data.

---

## Project Information

### Abstract

- **Problem:** The Debezium Platform currently only supports deploying pipelines to Kubernetes via the Debezium Operator. Teams running Debezium on bare metal, VMs, or cloud instances cannot use the platform.
- **Solution:** Add a second deployment target — **regular hosts** — by connecting to remote machines over SSH and managing Debezium Server containers remotely.
- **Approach:** Build a new `HostEnvironmentController` that implements the existing `EnvironmentController` interface, reusing the platform's pipeline model, Outbox event flow, and log streaming infrastructure without modifying them.
- **End result:** Users manage pipelines identically regardless of whether they run on Kubernetes or a standalone server — and a single pipeline can be deployed to **both** environments simultaneously.
- **Project size:** Large (~350 hours)

### Why This Project?


**The real-world need:**

- Many teams run Debezium on VMs or bare metal for compliance, cost, or legacy infrastructure reasons
- These teams are locked out of the platform's centralized management, monitoring, and lifecycle features
- The project idea explicitly calls for "consistent deployment, lifecycle management, and operational behavior across heterogeneous environments"

**Personal fit:**

- 3 years of professional experience with **Java, Spring Boot, Docker, Kafka, REST APIs, and Kubernetes/Helm** — directly mapping to this project's stack
- Already familiar with the codebase through my bug fix contributions (log streaming, exception handling, test infrastructure)
- Will use the Community Bonding period to close gaps in Quarkus CDI specifics and SSH library internals

### Technical Description

#### How the Existing System Works

1. **User creates/updates a pipeline** via REST API (`POST /api/pipelines`)
2. `PipelineService` persists the entity and fires a `PipelineEvent` via the **Outbox pattern**
   - `PipelineEvent` extends `AbstractEvent` which implements `ExportedEvent<String, JsonNode>`
3. `PipelineConsumer` picks up the event and calls the `EnvironmentController`:
   ```java
   // PipelineConsumer.accept()
   var pipelines = environment.pipelines();
   payload.ifPresentOrElse(pipelines::deploy, () -> pipelines.undeploy(id));
   ```
4. `OperatorPipelineController.deploy()` uses `PipelineMapper` to convert pipeline config into a `DebeziumServer` Kubernetes custom resource, then applies it via the K8s client
5. Logs flow through `KubernetesLogReader` → `LogStreamingService` → `PipelineLogWebSocket` → frontend

**Key insight:** Steps 1-3 are environment-agnostic. Only steps 4-5 are Kubernetes-specific. This project adds a parallel path at steps 4-5.

#### What This Project Changes

```
                    +-----------------------------------+
                    |        Pipeline Service           |
                    |  (routes via DeploymentTargets)   |
                    +--------+----------------+---------+
                             |                |
              +--------------v----+  +--------v-----------------+
              |   Operator        |  |   Host                   |
              |   Environment     |  |   Environment (NEW)      |
              +------+------------+  +--------+-----------------+
                     |                        |
          +----------v-------+    +-----------v-----------------+
          |  K8s Adapter     |    |  SSH + Docker Adapter (NEW) |
          +------------------+    +-----------------------------+
                  |                           |
          +-------v------+        +-----------v-----------------+
          | Kubernetes   |        | Remote Host                 |
          | Cluster      |        | (bare metal / VM / cloud)   |
          +--------------+        +-----------------------------+
```

**What stays the same:**
- `PipelineEntity`, `SourceEntity`, `DestinationEntity`, `ConnectionEntity`, `TransformEntity` — no schema changes to existing entities
- `PipelineEvent`, `AbstractEvent`, Outbox flow — unchanged
- `PipelineConsumer` — its structure stays the same. It still calls a service method that handles deployment, but that service method now resolves deployment targets internally and routes to the correct controller(s) per target
- `LogStreamingService` — unchanged, already accepts any `LogReader` implementation
- `PipelineLogWebSocket` — unchanged
- Frontend log viewer — unchanged

#### SSH-to-Docker Operation Mapping

Every `PipelineController` method maps to an SSH command on the remote host:

| Interface Method | SSH Command | Details |
|---|---|---|
| `deploy(PipelineFlat)` | `docker run -d --name dbz-pipeline-{id} ...` | Env vars from `HostPipelineMapper`, image pull, port mapping |
| `undeploy(Long id)` | `docker rm -f dbz-pipeline-{id}` | Container identified by naming convention |
| `stop(Long id)` | `docker stop dbz-pipeline-{id}` | Graceful stop |
| `start(Long id)` | `docker start dbz-pipeline-{id}` | Restart stopped container |
| `logReader(Long id)` | `docker logs -f dbz-pipeline-{id}` | Returns `HostLogReader` wrapping SSH output stream |
| `sendSignal(Long id, Signal)` | `curl http://localhost:{port}/api/signals` | Discovers Debezium Server REST API via `docker port` |

**What gets transferred to the target host:** Nothing is copied via SCP/SFTP. The Conductor sends Docker commands over SSH. The Docker daemon on the target host pulls the Debezium Server image from the container registry. Pipeline configuration is injected as `--env` flags. No files, binaries, or config files are transferred.

**Container naming convention:** `dbz-pipeline-{pipelineId}` — stateless lookup, survives Conductor restarts, no need to store container IDs in DB. Each host is a separate machine, so the same pipeline ID on different hosts does not collide.

**What gets built:**

| # | Component | Description | Follows Pattern Of |
|---|-----------|-------------|--------------------|
| 1 | **HostEntity + REST API** | New JPA entity + `/api/hosts` CRUD + `/api/hosts/validate` SSH test endpoint | `ConnectionEntity` + `ConnectionResource` |
| 2 | **SSH Credential Storage** | Encrypted SSH keys/passwords stored via Java Keystore on the Conductor host; DB stores only a credential alias, never raw secrets | New (see [Credential Management](#ssh-credential-management)) |
| 3 | **HostEnvironmentController** | Implements `EnvironmentController`, returns `HostPipelineController` | `OperatorEnvironmentController` |
| 4 | **HostPipelineController** | Implements `PipelineController` — deploy/undeploy/stop/start/logReader/sendSignal via SSH + Docker | `OperatorPipelineController` |
| 5 | **SSH Adapter Service** | Wraps SSH session management — connect, execute, stream, disconnect. Connection pooling, key/password auth | New (Apache SSHD or JSch) |
| 6 | **HostLogReader** | Implements `LogReader` — tails `docker logs -f` over SSH | `KubernetesLogReader` |
| 7 | **HostPipelineMapper** | Converts `PipelineFlat` → Docker run parameters (env vars, volumes). Reuses config prefix logic for 21+ connection types | `PipelineMapper` (K8s) |
| 8 | **Deployment Target Model** | Pipeline-to-Host association allowing a single pipeline to deploy to both K8s and host environments | New (see [Deployment Targets](#deployment-target-model)) |
| 9 | **Host Provisioning** | `/api/hosts/{id}/provision` — check/install Docker, open ports, set up directories via SSH | New (see [Host Provisioning](#host-provisioning)) |
| 10 | **Frontend: Hosts Page** | List, create, edit, validate hosts. PatternFly + React Query. | `Connections` page pattern |
| 11 | **Frontend: Deployment Target Selector** | Deployment target picker in Pipeline Designer creation flow | Existing `PipelineDesigner` atoms |

#### Deployment Target Model

A single pipeline should be deployable to **both** Kubernetes and a host simultaneously (e.g., for migration, testing, or hybrid architectures). Instead of putting an `environmentType` field on `PipelineEntity`, this project introduces a **deployment target** association:

```java
@Entity(name = "deployment_target")
public class DeploymentTargetEntity {
    @Id @GeneratedValue
    private Long id;

    @ManyToOne
    private PipelineEntity pipeline;

    @Enumerated(EnumType.STRING)
    private EnvironmentType type;     // OPERATOR or HOST

    @ManyToOne
    private HostEntity host;          // null when type = OPERATOR
}
```

**How routing works:**
- `PipelineService.environmentController()` is replaced with `PipelineService.deploymentTargets(Long pipelineId)` which returns a list of `(EnvironmentController, target)` pairs
- The service method that handles deploy/undeploy iterates over all targets and calls the corresponding controller for each one
- This means a pipeline can have one target (K8s only, host only) or multiple targets (both)
- `PipelineConsumer`'s structure stays the same — it calls this service method, which handles multi-target routing internally

**API:**

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/api/pipelines/{id}/targets` | List deployment targets |
| `POST` | `/api/pipelines/{id}/targets` | Add a deployment target (type + host ref) |
| `DELETE` | `/api/pipelines/{id}/targets/{targetId}` | Remove a deployment target |

**Backward compatibility:** Pipelines without any deployment target default to the existing Operator behavior — no migration needed for existing data.

#### SSH Credential Management

**Storage — Java Keystore (JKS / PKCS12):**
- A keystore file lives on the Conductor host, protected by a master password from an environment variable (`CONDUCTOR_KEYSTORE_PASSWORD`)
- SSH private keys and passwords are stored as keystore entries, each identified by an **alias**
- The `HostEntity` stores only the alias string (e.g., `"prod-server-key"`), never the raw credential

**Why this approach:**
- Java Keystore is built into the JDK — no external dependencies
- Keys are encrypted at rest (AES-256 by default in PKCS12)
- The database never holds sensitive material — safe even if the DB is compromised
- When the platform's Vault system is implemented in the future, migration is straightforward: swap the keystore backend for the Vault backend behind the same `CredentialProvider` interface

**Interface:**
```java
public interface CredentialProvider {
    SshCredential load(String alias);       // Load key or password by alias
    void store(String alias, SshCredential credential);
    void delete(String alias);
    boolean exists(String alias);
}
```

- Default implementation: `KeystoreCredentialProvider` backed by JKS/PKCS12
- Alternative backends (system keyring, HashiCorp Vault) can be plugged in via CDI without changing the host management code

**HostEntity model:**
```java
@Entity(name = "host")
public class HostEntity {
    @Id @GeneratedValue
    private Long id;

    @NotEmpty @Column(unique = true, nullable = false)
    private String name;

    @NotEmpty
    private String hostname;

    private int port = 22;

    @NotEmpty
    private String username;

    @Enumerated(EnumType.STRING)
    private AuthType authType;            // SSH_KEY or PASSWORD

    private String credentialAlias;        // Reference to keystore entry

    @Enumerated(EnumType.STRING)
    private ContainerRuntime runtime;     // DOCKER
}
```

#### Host Provisioning


**`POST /api/hosts/{id}/provision`** — Connects via SSH and runs a preflight + setup sequence:

| Step | Check | Action if Missing |
|------|-------|-------------------|
| 1 | Is Docker installed? | Install via package manager (apt/yum/dnf) |
| 2 | Is the Docker daemon running? | Start and enable the service |
| 3 | Can the SSH user run Docker? | Add user to `docker` group |
| 4 | Are required ports available? | Report conflicts (no auto-firewall changes) |
| 5 | Is there sufficient disk space? | Warn if below threshold |

**Response:** A structured provisioning report indicating what was checked, what was installed, and any warnings — not a silent pass/fail.

**Why this matters:** Without provisioning, users must manually prepare each host before registering it. This is the #1 friction point for onboarding new hosts, especially at scale.

#### SSH Library Selection

Will evaluate during Community Bonding:

| Library | Pros | Cons |
|---------|------|------|
| **Apache SSHD** | Pure Java, active Apache project, channel-based streaming, good for long-lived sessions | Heavier API surface |
| **JSch** (fork: `com.github.mwiede:jsch`) | Lightweight, widely used, simple API | Original unmaintained; fork is active but smaller community |

**Key criteria:** Compatibility with Quarkus virtual threads (`@RunOnVirtualThread`), support for streaming output (needed for log tailing), and connection pooling.

#### Configuration Mapping Detail

The existing `PipelineMapper` maps pipeline config to K8s `DebeziumServer` CRs using connection-type-specific prefixes:

```
POSTGRESQL  → "database."       (database.hostname, database.port, database.user, ...)
KAFKA       → "producer."       (producer.bootstrap.servers, ...)
MONGODB     → "mongodb"         (mongodb.connection.string, ...)
REDIS       → "redis"           (redis.address, ...)
... (21+ types)
```

The new `HostPipelineMapper` reuses these same prefixes but outputs Docker `--env` flags instead of CR spec fields:

```bash
docker run -d \
  --name dbz-pipeline-42 \
  -e DEBEZIUM_SOURCE_CONNECTOR_CLASS=io.debezium.connector.postgresql.PostgresConnector \
  -e DEBEZIUM_SOURCE_DATABASE_HOSTNAME=db.example.com \
  -e DEBEZIUM_SOURCE_DATABASE_PORT=5432 \
  -e DEBEZIUM_SINK_TYPE=kafka \
  -e DEBEZIUM_SINK_PRODUCER_BOOTSTRAP_SERVERS=kafka:9092 \
  -p 8080:8080 \
  quay.io/debezium/server:latest
```

#### Frontend Integration Detail

**Hosts Page** — follows the existing list page pattern used by Connections, Sources, Destinations:

- React Query with `useQuery<Host[], Error>("hosts", ...)` and 7s auto-refetch
- `HostTable` component (follows `ConnectionTable` / `SourceSinkTable` pattern)
- Row actions: Edit, Delete, Validate connectivity, Provision
- `useDeleteData` hook for mutations
- PatternFly `Table`, `SearchInput`, `Modal`, `EmptyState` components
- i18n translations in `public/locales/en/host.json`
- Credential upload handled via a secure file input — key content is sent to the `CredentialProvider`, only the alias is stored

**Pipeline Designer Update:**

- New Jotai atom: `selectedDeploymentTargetsAtom = atom<DeploymentTarget[]>([])`
- Deployment target section in `ConfigurePipeline.tsx` — user can add multiple targets (Kubernetes, specific host, or both)
- When host target is selected, show host dropdown (populated via `useQuery("hosts", ...)`)
- Synced with JSON code editor via existing `FormSyncManager`

#### Design Tradeoffs

| Decision | Alternative | Rationale |
|----------|-------------|-----------|
| **Agentless (SSH + Docker)** | Agent-based (install agent on host) | Zero host-side setup, lower barrier to entry. Tradeoff: more SSH overhead per operation |
| **Container naming convention** | Store container IDs in DB | Stateless lookup survives Conductor restarts without orphan cleanup logic |
| **Reuse `LogReader` interface** | Separate log streaming path | Zero changes to `LogStreamingService`, WebSocket, and frontend. Tradeoff: coupled to existing streaming contract |
| **Deployment target association** | Single `environmentType` field on pipeline | Supports deploying one pipeline to both K8s and host simultaneously. Tradeoff: slightly more complex schema |
| **Java Keystore for SSH creds** | Store encrypted keys in DB | DB never holds sensitive material; keystore is encrypted at rest; easy to migrate to Vault later |
| **Docker only** | Support bare process deployment | Container isolation is safer, cleaner lifecycle. Stretch goal could add process mode later |

#### Testing Strategy

| Layer | Framework | What's Tested |
|-------|-----------|---------------|
| **SSH Adapter** | `@QuarkusTest` + TestContainers (`atmoz/sftp` image) | Connect, execute, stream output, key auth, timeout handling |
| **CredentialProvider** | `@QuarkusTest` | Store/load/delete SSH keys via keystore; alias resolution |
| **HostPipelineController** | `@QuarkusTest` + TestContainers (SSH + Docker-in-Docker) | Full deploy/undeploy/stop/start cycle against real containers |
| **HostLogReader** | `@QuarkusTest` + TestContainers | Log streaming over SSH, verify lines reach `LogStreamingService` |
| **Host REST API** | `@QuarkusTest` + RestAssured | CRUD operations, validation endpoint, provisioning, error responses |
| **DeploymentTarget API** | `@QuarkusTest` + RestAssured | Multi-target creation, routing, backward compat for targetless pipelines |
| **HostPipelineMapper** | Unit tests (no containers) | Config prefix mapping for all 21+ connection types |
| **Frontend: Hosts page** | Vitest + MSW + Testing Library | List rendering, search, create/edit forms, delete modal, credential upload |
| **Frontend: Target selector** | Vitest + Testing Library | Multi-target selection, atom state, form sync |
| **E2E** | Cypress | Full workflow: create host → create pipeline with host target → deploy → view logs |

---

## Roadmap

### **Phase 1**

**Community Bonding** (May 1 - May 24)

- Deep-dive into codebase with mentors; trace the Outbox event flow end-to-end from `PipelineResource` → `PipelineService` → `PipelineEvent` → `PipelineConsumer` → `OperatorPipelineController`
- Evaluate Apache SSHD vs JSch for Quarkus virtual thread compatibility
- Set up local dev environment with a test VM reachable over SSH
- Draft design document and get mentor sign-off before coding begins
- Continue contributing small fixes to stay active in the community

*Success criteria: Design doc approved; SSH library chosen; local VM reachable over SSH; full test suite green locally.*

##### Week 1 (May 25 - May 31)
- `HostEntity` JPA model (hostname, port, username, auth type, credential alias, container runtime) + Flyway migration
- `CredentialProvider` interface + `KeystoreCredentialProvider` implementation
- `HostResource` — REST API at `/api/hosts` (CRUD endpoints + credential upload)
- Unit tests for host API and credential provider

##### Week 2 (June 1 - June 7)
- SSH adapter service: connect, disconnect, execute command, key-based auth, password auth, output streaming, connection pooling
- `/api/hosts/validate` — SSH connectivity test endpoint
- `DeploymentTargetEntity` model + Flyway migration + REST API (`/api/pipelines/{id}/targets`)
- Integration tests with TestContainers (PostgreSQL + SSH server image)

##### Week 3 (June 8 - June 14)
- `HostEnvironmentController` + `HostPipelineController`: implement `deploy()` and `undeploy()`
- `HostPipelineMapper`: convert `PipelineFlat` → Docker run parameters (env vars, port mapping, image pull)
- Update `PipelineService` routing: resolve deployment targets, select correct controller per target
- Integration tests for the full deploy/undeploy cycle

**Milestone 1: A pipeline can be deployed to a remote host via SSH + Docker through the deployment target API.**

##### Week 4 (June 15 - June 21)
- `HostPipelineController`: implement `stop()` and `start()`
- `HostLogReader`: implement `LogReader` interface — tail `docker logs -f` over SSH
- Integrate with `LogStreamingService` and verify logs flow through existing WebSocket endpoint

##### Week 5 (June 22 - June 28)
- Signal delivery: discover Debezium Server REST API via `docker port`, send signals through it
- Host provisioning: `/api/hosts/{id}/provision` endpoint — check/install container runtime, verify Docker daemon, check disk space
- Unit and integration tests for log streaming, signals, and provisioning

##### Week 6 (June 29 - July 5)
- Multi-target deployment: test deploying same pipeline to both Operator and Host targets simultaneously
- SSH timeout handling, container crash detection, improved error messages
- Edge case testing: host unreachable, SSH disconnects mid-operation, container OOM

**Milestone 2: Full pipeline lifecycle + multi-target deployment working. All backend components complete.**

### **Phase 2** — Midterm Evaluation (July 6 - July 10)

*Midterm deliverable: full backend — deploy, undeploy, stop, start, logs, signals, host provisioning, multi-target routing, credential management. A single pipeline can deploy to both K8s and host targets.*

##### Week 7 (July 6 - July 12)
- **Frontend: Hosts page** — list, create, edit, delete, validate connectivity, trigger provisioning
- Credential upload flow (file input → `CredentialProvider` → alias stored)
- Follow existing PatternFly + React Query patterns (match `Connections` page structure)
- MSW mock handlers for `/api/hosts`; Vitest unit tests

##### Week 8 (July 13 - July 19)
- **Frontend: Pipeline Designer** — deployment target selector (multi-target: K8s, host, or both)
- New Jotai atoms for deployment targets; wire into `ConfigurePipeline.tsx` form and `FormSyncManager`
- Vitest tests for target selection flow

##### Week 9 (July 20 - July 26)
- Cypress E2E tests: full workflow from host creation through pipeline deployment and log viewing
- API documentation for new endpoints
- Code cleanup, address review feedback from mentors
- i18n translations for host-related UI strings

**Milestone 3: Feature complete, tested, and documented.**

##### Week 10 (July 27 - August 2)
- Benchmark SSH throughput and deploy latency
- Compare log streaming performance: `HostLogReader` vs `KubernetesLogReader`
- Profile and optimize bottlenecks (SSH connection reuse, Docker command batching)

##### Week 11 (August 3 - August 9)
- Stress test concurrent pipeline deployments on a single host
- Validate behavior under slow/unstable SSH connections
- Document performance results and known limitations

##### Week 12 (August 10 - August 16)
- Buffer for remaining review feedback and unfinished items
- Final code cleanup and documentation polish

##### **Final Week** (August 17 - August 24)
- Submit final work product
- Final report and summary writeup

**Final Mentor Evaluation: August 24 - August 31**

---

## Other Commitments

- No other major commitments during summer 2026
- Can dedicate **30-35 hours per week** throughout the GSoC period
- Available for weekly mentor sync calls in both EST and CET timezones, and flexible for additional calls as needed.

---

## Appendix

### Background

I am a Master's student in Computer Science at Stevens Institute of Technology (expected Dec 2026) with 3 years of professional experience in distributed systems and cloud-native development. My core stack — Java, Spring Boot, Docker, Kafka, REST API design, and Kubernetes/Helm — maps directly to the technologies used in the Debezium Platform.

For areas where I have less direct experience (Apache SSHD integration, Quarkus-specific CDI patterns), I will use the Community Bonding period to close gaps before coding begins. My track record of ramping up quickly on new frameworks is reflected in my existing contributions to this project, which I completed within weeks of first reading the codebase.

I would be honored to be part of this project. I understand how impactful it is, and I will devote my full effort to it and be responsible for the work I take on.

### References

- [Debezium Platform GitHub](https://github.com/debezium/debezium-platform)
- [Debezium Operator](https://github.com/debezium/debezium-operator)
- [Quarkus Debezium Extension Design Doc (DDD-12)](https://github.com/debezium/debezium-design-documents/blob/main/DDD-12.md)
- [Apache SSHD Documentation](https://mina.apache.org/sshd-project/)
- [Debezium Server Configuration (Environment Variable Mapping)](https://debezium.io/documentation/reference/stable/operations/debezium-server.html)
- [Debezium Platform Stage (Frontend)](https://github.com/debezium/debezium-platform-stage)
- [Debezium Architecture — Change Data Capture Overview](https://debezium.io/documentation/reference/stable/architecture.html)
- [Quarkus CDI — Contexts and Dependency Injection Guide](https://quarkus.io/guides/cdi)
- [Testcontainers — Docker Module for Integration Testing](https://java.testcontainers.org/modules/docker_compose/)
- [GSoC Project Idea — Host-Based Pipeline Deployment](https://github.com/debezium/debezium/blob/main/GSOC.md)
