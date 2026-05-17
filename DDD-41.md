# DDD-41: Host-Based Pipeline Deployment for the Debezium Platform

## Motivation

The Debezium Platform currently supports pipeline deployment exclusively through the Kubernetes-based Debezium Operator. While this works well for teams running Kubernetes, a significant portion of production environments operate on bare-metal servers, cloud VMs (EC2, Azure VMs), and on-premise machines that do not run Kubernetes.

This proposal introduces a host-based deployment path as a first-class, parallel alternative to the existing Kubernetes path. By implementing a second `EnvironmentController` that deploys Debezium Server containers on remote hosts via SSH and a lightweight host-side agent, the Platform becomes environment-agnostic.

## Proposed Changes

### 1. High-Level Architecture

The existing deployment flow is:

```
Stage UI → REST API → PipelineService → Outbox Event → Watcher (Debezium Engine)
  → OutboxParentEventConsumer → PipelineConsumer → EnvironmentController.pipelines().deploy()
    → OperatorPipelineController → PipelineMapper → DebeziumServer CRD → Kubernetes API
```

The host-based path adds a parallel branch at the `EnvironmentController` level:

```
... → PipelineConsumer → RoutingEnvironmentController
    → HostPipelineController → HostPipelineMapper → application.properties
      → HTTP call to Debezium Host Agent on remote host
        → Agent pulls image, starts Docker container → Debezium Server running
```

**Critical architectural note:** The `PipelineConsumer` (in the Watcher) receives a **single** `EnvironmentController` via CDI constructor injection — not a list. This means we cannot simply register two `@ApplicationScoped` controllers; CDI would have an ambiguous dependency. The solution is a **`RoutingEnvironmentController`** that wraps both implementations and delegates based on pipeline-to-host association. See Section 3.

SSH is used **only once** during initial provisioning to install a lightweight Debezium Host Agent. All subsequent lifecycle operations go through the agent's HTTP REST API, mirroring the Kubernetes kubelet pattern.

### 2. New and Modified Modules

#### 2.1 Conductor-Side Changes

New package structure under `src/main/java/io/debezium/platform/`:

```
environment/
├── EnvironmentController.java              ← existing (unchanged)
├── PipelineController.java                 ← existing (unchanged)
├── VaultController.java                    ← existing (unchanged)
├── RoutingEnvironmentController.java       ← [NEW] replaces direct injection
├── logs/
│   └── LogReader.java                      ← existing (unchanged)
├── operator/                               ← existing (unchanged)
│   ├── OperatorEnvironmentController.java
│   ├── OperatorPipelineController.java
│   ├── OperatorVaultController.java
│   ├── PipelineMapper.java
│   └── ...
├── host/                                   ← [NEW PACKAGE]
│   ├── HostEnvironmentController.java      ← [NEW] implements EnvironmentController
│   ├── HostPipelineController.java         ← [NEW] implements PipelineController
│   ├── HostVaultController.java            ← [NEW] implements VaultController
│   ├── HostPipelineMapper.java             ← [NEW] produces application.properties
│   ├── HostDeploymentStatusPoller.java     ← [NEW] @Scheduled status polling
│   ├── logs/
│   │   └── HostLogReader.java              ← [NEW] implements LogReader
│   ├── agent/
│   │   └── HostAgentClient.java            ← [NEW] programmatic HTTP client
│   ├── ssh/
│   │   ├── SshConnectionConfig.java        ← [NEW] connection config record
│   │   ├── SshSessionManager.java          ← [NEW] @ApplicationScoped SSH client
│   │   ├── SshSession.java                 ← [NEW] wraps ClientSession
│   │   └── CommandResult.java              ← [NEW] stdout/stderr/exitCode
│   └── provisioning/
│       ├── HostProvisioningService.java    ← [NEW] runs check pipeline
│       ├── ProvisioningCheck.java          ← [NEW] interface for checks
│       ├── ProvisioningReport.java         ← [NEW] report value object
│       ├── checks/
│       │   ├── ConnectivityCheck.java      ← [NEW] 
│       │   ├── ContainerInstalledCheck.java   ← [NEW (docker)]
│       │   ├── ContainerDaemonCheck.java      ← [NEW -]
│       │   ├── ContainerPermissionsCheck.java ← [NEW -]
│       │   ├── PortAvailabilityCheck.java  ← [NEW]
│       │   ├── DiskSpaceCheck.java         ← [NEW]
│       │   └── AgentDeploymentCheck.java   ← [NEW]
│       └── CheckResult.java               ← [NEW]
data/
└── model/
    ├── HostTargetEntity.java               ← [NEW] JPA entity
    └── HostDeploymentEntity.java           ← [NEW] JPA entity
domain/
├── HostTargetService.java                  ← [NEW] extends AbstractService
├── HostDeploymentService.java              ← [NEW]
├── views/
│   ├── HostTarget.java                     ← [NEW] Blaze EntityView
│   ├── HostDeployment.java                 ← [NEW] Blaze EntityView
│   └── refs/
│       ├── HostTargetReference.java        ← [NEW]
│       └── HostDeploymentReference.java    ← [NEW]
api/
├── HostTargetResource.java                 ← [NEW] REST resource at /hosts
├── dto/
│   ├── HostTargetRequest.java              ← [NEW]
│   └── HostTargetResponse.java             ← [NEW]
└── mapper/
    └── HostTargetMapper.java               ← [NEW] MapStruct mapper
```

#### 2.2 Stage (Frontend) Changes

```
src/pages/
└── Host/                                   ← [NEW]
    ├── Hosts.tsx                            ← [NEW] Host list page
    ├── HostDetails.tsx                      ← [NEW] Host detail + provisioning status
    ├── CreateHost.tsx                       ← [NEW] Host registration form
    └── index.ts                            ← [NEW]
src/apis/
├── apis.tsx                                ← [MODIFY] add HostTarget types
src/route.tsx                               ← [MODIFY] add /hosts routes
src/appLayout/AppSideNavigation.tsx         ← automatically picks up new routes
```

#### 2.3 Debezium Host Agent (new lightweight Quarkus module)

```
debezium-platform-agent/                    ← [NEW MODULE]
├── pom.xml
└── src/main/java/io/debezium/platform/agent/
    ├── AgentApplication.java               ← @ApplicationPath("/api")
    ├── DeploymentResource.java             ← REST endpoints
    ├── auth/
    │   └── BearerTokenFilter.java          ← [NEW] request filter for auth
    ├── container/
    │   └── ContainerCommandExecutor.java      ← local container CLI wrapper
    ├── config/
    │   └── PipelineConfigWriter.java       ← writes application.properties
    └── model/
        ├── DeployRequest.java
        ├── DeployResponse.java
        └── StatusResponse.java
```

#### 2.4 Database Migration

**[NEW]** `src/main/resources/db/migration/V3.5.0__add_host_deployment.sql`

```sql
create sequence host_target_SEQ start with 1 increment by 50;
create sequence host_deployment_SEQ start with 1 increment by 50;

create table host_target (
-- Table stores SSH/Agent target hosts information
-- Columns: id, name, description, hostname, port, username, auth_type, etc.
);

create table host_deployment (
-- Table stores deployment records of pipelines on specific hosts
-- Columns: id, pipeline_id, host_target_id, container_name, image_version, etc.
);

alter table if exists host_target
    add constraint FK_host_target_vault
    foreign key (credential_id) references vault;

alter table if exists host_deployment
    add constraint FK_host_deployment_pipeline
    foreign key (pipeline_id) references pipeline;

alter table if exists host_deployment
    add constraint FK_host_deployment_host_target
    foreign key (host_target_id) references host_target;
```

Note: `server_port` column added to `host_deployment` to avoid port 8080 conflicts when multiple pipelines run on the same host.

### 3. Core Abstractions — Routing Fix

#### 3.1 The Problem with PipelineConsumer

The `PipelineConsumer` and `VaultConsumer` each receive a **single** `EnvironmentController` via constructor injection:


If we register both `OperatorEnvironmentController` and `HostEnvironmentController` as `@ApplicationScoped`, CDI cannot resolve the ambiguity.

#### 3.2 Solution: RoutingEnvironmentController

**[NEW]** `RoutingEnvironmentController` — the single `@ApplicationScoped` bean that CDI injects everywhere:

```java
package io.debezium.platform.environment;

@ApplicationScoped
public class RoutingEnvironmentController implements EnvironmentController {

    private final OperatorEnvironmentController operatorController;
    private final HostEnvironmentController hostController;
    private final HostDeploymentService hostDeploymentService;

    public RoutingEnvironmentController(...) {
        ...
    }

    @Override
    public PipelineController pipelines() {
        // Returns a RoutingPipelineController that delegates per-pipeline
    }

    @Override
    public VaultController vaults() {
        // Vaults currently only used by operator path
    }
}
```

**[NEW]** `RoutingPipelineController`:

```java
package io.debezium.platform.environment;

public class RoutingPipelineController implements PipelineController {

    private final PipelineController operatorPipelines;
    private final PipelineController hostPipelines;
    private final HostDeploymentService hostDeploymentService;

    @Override all of the methods in PipelineController


    private PipelineController resolveController(Long pipelineId) {
        if (hostDeploymentService.existsByPipelineId(pipelineId)) {
            return hostPipelines;
        }
        return operatorPipelines;
    }
}
```

Both `OperatorEnvironmentController` and `HostEnvironmentController` are changed from `@ApplicationScoped` to `@Dependent` (or use `@Named` qualifiers) so only `RoutingEnvironmentController` is the default `@ApplicationScoped` bean.

**[MODIFY]** `PipelineService.environmentController()` — simplified:

```java
public Optional<EnvironmentController> environmentController(Long id) {
    // The routing controller handles delegation internally
    return findById(id).map(pipeline -> routingController);
}
```

No `instanceof` checks needed. No layer violations.

### 4. Data Model

#### 4.1 JPA Entities

**`HostTargetEntity`** — follows existing patterns (`PipelineEntity`, `VaultEntity`):

```java
package io.debezium.platform.data.model;

@Entity(name = "host_target")
public class HostTargetEntity {
    Key Fields:
    // id, name (unique), description, hostname, port=22, username, 
    // auth_type (SSH_KEY/PASSWORD), agent_port=8090, agent_token, 
    // credential_id (FK to VaultEntity), 
    // provisioning_status, provisioning_report (JSON)

    // Relationships:
    // @ManyToOne VaultEntity credential

    // Getters and setters following existing entity pattern
}
```

**`HostDeploymentEntity`**:

```java
package io.debezium.platform.data.model;

@Entity(name = "host_deployment")
public class HostDeploymentEntity {
    // fields: id, pipeline_id, host_target_id, container_name, image_version,
    // server_port(8080), deployment_status, config_hash

    // relations:
    // pipeline_id -> PipelineEntity (Many-to-One)
    // host_target_id -> HostTargetEntity (Many-to-One)
}
```

Note: `fetch = FetchType.LAZY` on `@ManyToOne` to avoid N+1 queries in the status poller. `serverPort` field added for per-pipeline port allocation.

### 5. SSH Layer

Uses Apache MINA SSHD (`org.apache.sshd:sshd-core`, `org.apache.sshd:sshd-sftp`):

```java
package io.debezium.platform.environment.host.ssh;

@ApplicationScoped
public class SshSessionManager {

    private static final long CONNECT_TIMEOUT_MS = 10_000;
    private static final long AUTH_TIMEOUT_MS = 5_000;

    private SshClient client;

    @PostConstruct
    void init() {
        this.client = SshClient.setUpDefaultClient();
        this.client.start();
    }

    public SshSession openSession(SshConnectionConfig config) throws IOException {
       // creates authenticated session
    }

    @PreDestroy
    void shutdown() throws IOException {
        if (client != null) { client.stop(); }
    }
}
```

**`SshSession`** — 

```java
package io.debezium.platform.environment.host.ssh;

public class SshSession implements AutoCloseable {
    private final ClientSession session;

    public CommandResult executeCommand(String command) throws IOException {
        try (ChannelExec channel = session.createExecChannel(command)) {
            // Set output streams BEFORE opening channel to avoid deadlock
            ByteArrayOutputStream stdoutStream = new ByteArrayOutputStream();
            ByteArrayOutputStream stderrStream = new ByteArrayOutputStream();
            channel.setOut(stdoutStream);
            channel.setErr(stderrStream);

            channel.open().verify(10_000);
            // Wait for channel to close (command completion)
            channel.waitFor(EnumSet.of(ClientChannelEvent.CLOSED), 30_000);

            Integer exitCode = channel.getExitStatus();
            return new CommandResult(
                stdoutStream.toString().trim(),
                stderrStream.toString().trim(),
                exitCode != null ? exitCode : -1);
        }
    }

    public void uploadFile(String remotePath, byte[] content) throws IOException {
        try (SftpClient sftp = SftpClientFactory.instance().createSftpClient(session)) {
            // Recursive mkdir — create each path segment
            String[] segments = remotePath.substring(1).split("/");
            StringBuilder current = new StringBuilder();
            for (int i = 0; i < segments.length - 1; i++) {
                current.append("/").append(segments[i]);
                try {
                    sftp.mkdir(current.toString());
                } catch (IOException e) {
                    // Directory may already exist — ignore
                }
            }
            try (OutputStream os = sftp.write(remotePath)) {
                os.write(content);
            }
        }
    }

    @Override
    public void close() throws IOException { session.close(); }
}
```

```java
public record SshConnectionConfig(
    String host, int port, String username,
    HostTargetEntity.AuthType authType,
    String password, String privateKey
) {}

public record CommandResult(String stdout, String stderr, int exitCode) {
    public boolean isSuccess() { return exitCode == 0; }
}
```

### 6. Provisioning System

#### 6.1 Check Interface and Implementations

```java
public interface ProvisioningCheck {
    String name();
    CheckResult run(SshSession session) throws IOException;
    boolean canRemediate();
    void remediate(SshSession session) throws IOException;
}

public record CheckResult(boolean passed, String message) {}

public record ProvisioningReport(
    HostTargetEntity.ProvisioningStatus status,
    List<CheckResult> checkResults,
    Instant timestamp
) {}
```

| Check | SSH Command | Remediation | Notes |
|---|---|---|---|
| `ConnectivityCheck` | `echo debezium-health-check` | None (fail-fast) | |
| `DockerInstalledCheck` | `docker version --format '{{.Server.Version}}'` | `apt-get install -y docker.io` or `yum install -y docker` | Detects OS via `/etc/os-release` |
| `DockerDaemonCheck` | `systemctl is-active docker` | `sudo systemctl start docker && sudo systemctl enable docker` | |
| `DockerPermissionsCheck` | `groups` | `sudo usermod -aG docker $USER` | **Requires reconnect** — after `usermod`, close SSH session and reopen for group to take effect |
| `PortAvailabilityCheck` | `ss -tlnp \| grep :{agentPort}` | None (report conflict) | Checks agent port, not pipeline port |
| `DiskSpaceCheck` | `df -BG / \| tail -1 \| awk '{print $4}'` | None (warn if <2GB) | |
| `AgentDeploymentCheck` | Upload agent JAR/native binary via SFTP, create systemd unit, start service | See section 6.2 | |

#### 6.2 Agent Deployment Details

The agent is packaged as a **native binary** (via `quarkus-maven-plugin` native build) or a **runner JAR** (with JRE pre-check). The provisioning check:

1. Checks if agent is already running: `systemctl is-active debezium-agent`
2. If not, generates a shared Bearer token (UUID), stores it in `HostTargetEntity` via a new `agentToken` field
3. Uploads agent binary to `/opt/debezium/agent/debezium-agent`
4. Creates systemd unit file at `/etc/systemd/system/debezium-agent.service`:
```ini
[Unit]
Description=Debezium Host Agent
After=network.target docker.service

[Service]
Type=simple
ExecStart=/opt/debezium/agent/debezium-agent
Environment=AGENT_PORT={agentPort}
Environment=AGENT_AUTH_TOKEN={generatedToken}
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```
5. Runs `sudo systemctl daemon-reload && sudo systemctl enable debezium-agent && sudo systemctl start debezium-agent`
6. Verifies agent is responding: `curl -s -o /dev/null -w '%{http_code}' http://localhost:{agentPort}/api/status`

#### 6.3 Provisioning Service

Note: Uses explicit `try/finally` instead of try-with-resources because the SSH session may be closed and reopened mid-loop (Docker permissions reconnect). Try-with-resources would only auto-close the *original* session reference, leaking the reconnected session.

```java
@ApplicationScoped
public class HostProvisioningService {

    private final SshSessionManager sshManager;
    private final List<ProvisioningCheck> checks;
    private final ManagedExecutor executor;

    public ProvisioningReport provision(SshConnectionConfig config) {
        List<CheckResult> results = new ArrayList<>();
        SshSession session = null;
        try {
            session = sshManager.openSession(config);
            for (ProvisioningCheck check : checks) {
                CheckResult result = check.run(session);
                if (!result.passed() && check.canRemediate()) {
                    check.remediate(session);
                    // DockerPermissionsCheck: need to reconnect for group change
                    if (check instanceof DockerPermissionsCheck) {
                        session.close();
                        // Brief pause for group change to propagate
                        Thread.sleep(1000);
                        session = sshManager.openSession(config);
                    }
                    result = check.run(session);
                }
                results.add(result);
                if (!result.passed()) {
                    return new ProvisioningReport(
                        ProvisioningStatus.FAILED, results, Instant.now());
                }
            }
        } catch (IOException e) {
            results.add(new CheckResult(false, "SSH error: " + e.getMessage()));
            return new ProvisioningReport(
                ProvisioningStatus.FAILED, results, Instant.now());
        } finally {
            if (session != null) {
                try { session.close(); } catch (IOException ignored) {}
            }
        }
        return new ProvisioningReport(
            ProvisioningStatus.READY, results, Instant.now());
    }

    @Transactional
    public void provisionAsync(Long hostTargetId) {
        executor.runAsync(() -> {
            // fetch host, build config, call provision(), update entity
        });
    }
}
```

Note: `DockerPermissionsCheck` remediation requires SSH session reconnect because `usermod -aG docker $USER` only takes effect in new login sessions. The explicit `try/finally` ensures whichever session is current (original or reconnected) is always closed.

### 7. Host Pipeline Mapper

**`HostPipelineMapper`** — analogous to `PipelineMapper` in `io.debezium.platform.environment.operator`. Both consume a `PipelineFlat`; `PipelineMapper` outputs a `DebeziumServer` CRD, `HostPipelineMapper` outputs a flat `application.properties` string.

The mapper reuses the **same prefix resolution logic** and **same special-case field naming** from `PipelineMapper`:

```java
package io.debezium.platform.environment.host;

@ApplicationScoped
public class HostPipelineMapper {

    // Same prefix constants as PipelineMapper lines 69-85
    private static final String DATABASE_PREFIX = "database.";
    ......
    // Same special fields constants as PipelineMapper
    private static final String USERNAME = "username";
    .......
    private final PipelineConfigGroup pipelineConfigGroup;
    private final TableNameResolver tableNameResolver;

    public String map(PipelineFlat pipeline) {
    // Use TreeMap for deterministic key ordering (critical for configHash)
    TreeMap<String, String> props = new TreeMap<>();

    // 1. Source configuration
    var source = pipeline.getSource();
    props.put("debezium.source.connector.class", source.getType());
    if (source.getConnection() != null) {
        ConnectionEntity.Type connType = source.getConnection().getType();
        String prefix = prefixResolver(connType);
        source.getConnection().getConfig().forEach((k, v) ->
            props.put("debezium.source." + getName(connType, k, prefix), String.valueOf(v)));
    }
    // ... remaining source.getConfig() mapped to "debezium.source.[k]"
    // ... mapping of enabled signals and notification channels

    // 2. Sink configuration
    var dest = pipeline.getDestination();
    props.put("debezium.sink.type", dest.getType());
    if (dest.getConnection() != null) {
        String prefix = prefixResolver(dest.getConnection().getType());
        dest.getConnection().getConfig().forEach((k, v) ->
            props.put("debezium.sink." + prefix + k, String.valueOf(v)));
    }
    // ... remaining dest.getConfig() mapped to "debezium.sink.[k]"

    // 3. Offset storage & Schema history (from PipelineConfigGroup)
    props.put("debezium.source.offset.storage", pipelineConfigGroup.offset().storage().type());
    // ... pipelineConfigGroup.offset().storage().config() mapped using tableNameResolver
    
    props.put("debezium.source.schema.history.internal", pipelineConfigGroup.schema().internal());
    // ... pipelineConfigGroup.schema().config() mapped using tableNameResolver

    // 4. Transforms & Predicates (mirrors PipelineMapper.buildTransformation)
    if (!pipeline.getTransforms().isEmpty()) {
        List<String> transformNames = new ArrayList<>();
        for (int i = 0; i < pipeline.getTransforms().size(); i++) {
            var transform = pipeline.getTransforms().get(i);
            String tName = "t" + i;
            transformNames.add(tName);
            props.put("debezium.transforms." + tName + ".type", transform.getType());
            // ... map transform.getConfig() to "debezium.transforms.t[i].[k]"

            // Predicate support (mirrors PipelineMapper predicate handling)
            if (transform.getPredicate() != null) {
                var predicate = transform.getPredicate();
                String pName = "p" + i;
                props.put("debezium.transforms." + tName + ".predicate", pName);
                props.put("debezium.predicates." + pName + ".type", predicate.getType());
                // ... map negate flag and predicate.getConfig()
            }
        }
        props.put("debezium.transforms", String.join(",", transformNames));
    }

    // 5. Quarkus log config
    props.put("quarkus.log.level", pipeline.getDefaultLogLevel());
    pipeline.getLogLevels().forEach((cat, level) ->
        props.put("quarkus.log.category.\"" + cat + "\".level", level));

    // 6. Serialize WITHOUT timestamp to ensure stable configHash
    StringBuilder sb = new StringBuilder();
    props.forEach((k, v) -> sb.append(k).append("=").append(v).append("\n"));
    return sb.toString();
}

// ---------------------------------------------------------
// Helper methods matching PipelineMapper prefix resolution
// ---------------------------------------------------------

}
```

**Key points:**
1. Uses `TreeMap` instead of `Properties` for deterministic ordering — no timestamp comment that breaks `configHash`
2. Serializes manually (`key=value\n`) instead of `Properties.store()` — stable SHA-256 hashing
3. Includes the `getName()` special-casing for `USERNAME`/`DATABASE` 
4. Handles all 20+ `ConnectionEntity.Type` cases including SQL Server database name variant
5. Full transform/predicate mapping with indexed naming (`t0`, `t1`, `p0`, `p1`)

### 7.1 HostPipelineController — The Orchestrator

`HostPipelineController` implements `PipelineController` and is the class that ties the mapper, agent client, and deployment service together:

```java
package io.debezium.platform.environment.host;

@Dependent
public class HostPipelineController implements PipelineController {

    private final HostPipelineMapper mapper;
    private final HostAgentClient agentClient;
    private final HostDeploymentService deploymentService;
    private final Logger logger;

    public HostPipelineController(...) {
      ...
    }

    @Override
    public void deploy(PipelineFlat pipeline) {
        var deployment = deploymentService.findByPipelineId(pipeline.getId())
            .orElseThrow(() -> new DebeziumException(
                "No host deployment found for pipeline " + pipeline.getId()));
        var host = deployment.getHostTarget();

        // 1. Generate application.properties from pipeline definition

        // 2. Compute SHA-256 hash for drift detection

        // 3. Mark as DEPLOYING before sending to agent

        // 4. Send deploy request to agent on remote host

        // 5. Mark as RUNNING on success
            
    }

    @Override
    public void undeploy(Long id) {
        deploymentService.findByPipelineId(id).ifPresent(deployment -> {
            try {
                agentClient.remove(deployment.getHostTarget(), id);
                deploymentService.updateStatus(deployment, DeploymentStatus.STOPPED, null);
            } catch (Exception e) {
                logger.errorf(e, "Failed to undeploy pipeline %d", id);
            }
        });
    }

    @Override
    public void stop(Long id) {
        deploymentService.findByPipelineId(id).ifPresent(deployment -> {
            agentClient.stop(deployment.getHostTarget(), id);
            deploymentService.updateStatus(deployment, DeploymentStatus.STOPPED, null);
        });
    }

    @Override
    public void start(Long id) {
        deploymentService.findByPipelineId(id).ifPresent(deployment -> {
            String config = mapper.map(/* re-fetch pipeline flat */);
            agentClient.deploy(deployment.getHostTarget(), id, config,
                deployment.getImageVersion(), deployment.getServerPort());
            deploymentService.updateStatus(deployment, DeploymentStatus.RUNNING,
                sha256(config));
        });
    }

    @Override
    public LogReader logReader(Long id) {
        return deploymentService.findByPipelineId(id)
            .map(d -> new HostLogReader(agentClient, d.getHostTarget(), id))
            .orElseThrow(() -> new DebeziumException(
                "No host deployment for pipeline " + id));
    }

    @Override
    public void sendSignal(Long pipelineId, Signal signal) {
        // Host-deployed pipelines receive signals via the Debezium Server REST API
        // running inside the container, proxied through the agent
        deploymentService.findByPipelineId(pipelineId).ifPresent(deployment ->
            agentClient.forwardSignal(deployment.getHostTarget(), pipelineId, signal));
    }

    private static String sha256(String input) {
        try {
            var digest = java.security.MessageDigest.getInstance("SHA-256");
            byte[] hash = digest.digest(input.getBytes(java.nio.charset.StandardCharsets.UTF_8));
            StringBuilder hex = new StringBuilder();
            for (byte b : hash) { hex.append(String.format("%02x", b)); }
            return hex.toString();
        } catch (java.security.NoSuchAlgorithmException e) {
            throw new RuntimeException(e); // SHA-256 is always available
        }
    }
}
```

### 7.2 HostVaultController — No-Op (By Design)

```java
package io.debezium.platform.environment.host;

@Dependent
public class HostVaultController implements VaultController {

    @Override
    public void deploy(Vault vault) {
        // No-op. Host-based deployments handle secrets differently:
        // - SSH credentials are stored in VaultEntity and resolved during provisioning
        // - Pipeline secrets (database passwords) are embedded in application.properties
        //   which is written directly to the host filesystem via the agent
        // There is no equivalent of Kubernetes Secrets to deploy.
    }

    @Override
    public void undeploy(Long id) {
        // No-op. See deploy() rationale above.
    }
}
```

**Why no-op?** In Kubernetes, `OperatorVaultController` deploys secrets as Kubernetes Secret objects that Pods reference. On bare-metal hosts, there's no equivalent secret store. Pipeline credentials are resolved at config-generation time by `HostPipelineMapper` and written directly into `application.properties`. SSH credentials for host access are stored in the existing `VaultEntity` and resolved by `HostProvisioningService` when opening SSH sessions. The `OperatorVaultController` in the existing codebase is ALSO effectively empty (methods have empty bodies) — so this is consistent.

### 8. Debezium Host Agent

A minimal Quarkus application deployed to each target host via SFTP during provisioning. It exposes a local REST API secured with a Bearer token:

| Endpoint | Method | Description |
|---|---|---|
| `/api/deploy` | POST | Receives pipeline config, writes `application.properties`, pulls image, starts container |
| `/api/update` | POST | Writes new config, restarts container |
| `/api/status` | GET | Returns container state + deployed config hash |
| `/api/stop` | POST | Stops the container |
| `/api/remove` | POST | Stops + removes container + cleans config directory |
| `/api/logs` | GET | Streams Docker container logs |

**Authentication:** Every request must include `Authorization: Bearer {token}` header. The token is generated during provisioning and stored in the Conductor's `HostTargetEntity`. The agent validates it via a JAX-RS `@Provider` `ContainerRequestFilter`.

```java
package io.debezium.platform.agent;

@Path("/api")
@ApplicationScoped
public class DeploymentResource {

    private final DockerCommandExecutor docker;
    private final PipelineConfigWriter configWriter;

    @POST @Path("/deploy")
    public Response deploy(DeployRequest request) {
        String pipelineDir = "/opt/debezium/pipelines/" + request.pipelineId();
        configWriter.write(pipelineDir + "/application.properties", request.config());

        docker.execute("docker pull quay.io/debezium/server:" + request.imageVersion());

        String containerName = "debezium-pipeline-" + request.pipelineId();
        // Idempotent remove — ignore failure if container doesn't exist
        docker.execute("docker rm -f " + containerName + " 2>/dev/null || true");

        int serverPort = request.serverPort();  // Per-pipeline port
        docker.execute(String.format(
            "docker run -d --name %s --restart unless-stopped " +
            "-v %s:/debezium/conf -p %d:8080 quay.io/debezium/server:%s",
            containerName, pipelineDir, serverPort, request.imageVersion()));

        // Verify
        var inspect = docker.execute(
            "docker inspect -f '{{.State.Running}}' " + containerName);
        if (!"true".equals(inspect.stdout().trim())) {
            var logs = docker.execute("docker logs --tail 50 " + containerName);
            return Response.serverError()
                .entity(new DeployResponse(false, logs.stdout())).build();
        }
        return Response.ok(new DeployResponse(true, "")).build();
    }

    @GET @Path("/status")
    public Response status(@QueryParam("pipelineId") Long pipelineId) {
        String containerName = "debezium-pipeline-" + pipelineId;
        var inspect = docker.execute(
            "docker inspect -f '{{.State.Status}}' " + containerName);
        String configHash = computeFileHash(
            "/opt/debezium/pipelines/" + pipelineId + "/application.properties");
        return Response.ok(
            new StatusResponse(inspect.stdout().trim(), configHash)).build();
    }

    // stop(), remove(), logs() follow same pattern
}
```

**Key points:**
1. `docker rm -f ... 2>/dev/null || true` — prevents exit code 1 on first deploy
2. Uses `request.serverPort()` instead of hardcoded `8080` — avoids port conflicts
3. Bearer token auth via `BearerTokenFilter` `@Provider` — secures the agent API

### 9. Host Agent Client (Conductor-side)

**Cannot use MicroProfile `@RegisterRestClient`** because each host has a different URL. Must use programmatic HTTP client:

```java
package io.debezium.platform.environment.host.agent;

@ApplicationScoped
public class HostAgentClient {

    private final io.vertx.mutiny.ext.web.client.WebClient webClient;

    public HostAgentClient(Vertx vertx) {
        this.webClient = WebClient.create(vertx);
    }

    public void deploy(HostTargetEntity host, Long pipelineId,
                       String config, String imageVersion, int serverPort) {
        var body = new DeployRequest(pipelineId, config, imageVersion, serverPort);
        webClient.post(host.getAgentPort(), host.getHostname(), "/api/deploy")
            .putHeader("Authorization", "Bearer " + host.getAgentToken())
            .sendJson(body)
            .await().atMost(Duration.ofSeconds(60));
    }

    public StatusResponse getStatus(HostTargetEntity host, Long pipelineId) {
        return webClient.get(host.getAgentPort(), host.getHostname(),
                "/api/status?pipelineId=" + pipelineId)
            .putHeader("Authorization", "Bearer " + host.getAgentToken())
            .send()
            .await().atMost(Duration.ofSeconds(10))
            .bodyAsJson(StatusResponse.class);
    }

    // stop(), remove(), start() follow same pattern
}
```

Uses Vert.x `WebClient` which supports dynamic URLs natively. Quarkus already includes Vert.x.

### 10. Status Polling and Config Drift Detection

```java
package io.debezium.platform.environment.host;

@ApplicationScoped
public class HostDeploymentStatusPoller {

    private final HostDeploymentService deploymentService;
    private final HostAgentClient agentClient;

    @Scheduled(every = "30s")
    @Transactional  // Required — poller updates DB
    void pollHostDeployments() {
        var activeDeployments = deploymentService.findByStatus(
            DeploymentStatus.RUNNING);
        for (var deployment : activeDeployments) {
            try {
                var status = agentClient.getStatus(
                    deployment.getHostTarget(),
                    deployment.getPipeline().getId());
                if ("exited".equals(status.containerState())
                    || "not_found".equals(status.containerState())) {
                    deploymentService.updateStatus(
                        deployment, DeploymentStatus.FAILED, null);
                } else if (deployment.getConfigHash() != null
                    && !deployment.getConfigHash().equals(status.configHash())) {
                    deploymentService.updateStatus(
                        deployment, DeploymentStatus.CONFIG_DRIFT, null);
                }
            } catch (Exception e) {
                deploymentService.updateStatus(
                    deployment, DeploymentStatus.FAILED, null);
            }
        }
    }
}
```

**Key points:** Added `@Transactional` annotation. Added null check on `configHash`.

### 11. REST API — Host Target Resource

Following exact patterns from `VaultResource` and `PipelineResource`:

```java
package io.debezium.platform.api;

@Tag(name = "hosts")
@OpenAPIDefinition(info = @Info(title = "Host Target API",
    description = "CRUD operations over Host Target resource",
    version = "0.1.0",
    contact = @Contact(name = "Debezium",
        url = "https://github.com/debezium/debezium")))
@Path("/hosts")
public class HostTargetResource {

    Logger logger;
    HostTargetService hostTargetService;
    HostTargetMapper mapper;

    public HostTargetResource(...) {
        ...
    }

    @GET
    public Response list() {
        return Response.ok(
            mapper.toResponseList(hostTargetService.list())).build();
    }

    @POST
    public Response register(@NotNull @Valid HostTargetRequest request,
                             @Context UriInfo uriInfo) {
        var view = hostTargetService.createEmpty();
        mapper.applyToView(request, view);
        var created = hostTargetService.create(view);
        URI uri = uriInfo.getAbsolutePathBuilder()
            .path(Long.toString(created.getId())).build();
        return Response.created(uri)
            .entity(mapper.toResponse(created)).build();
    }

    @GET @Path("/{id}")
    public Response getById(@PathParam("id") Long id) {
        return hostTargetService.findById(id)
            .map(mapper::toResponse)
            .map(dto -> Response.ok(dto).build())
            .orElseGet(() -> Response.status(
                Response.Status.NOT_FOUND).build());
    }

    @DELETE @Path("/{id}")
    public Response delete(@PathParam("id") Long id) {
        hostTargetService.delete(id);
        return Response.status(Response.Status.NO_CONTENT).build();
    }

    @GET @Path("/{id}/provisioning")
    public Response getProvisioningStatus(@PathParam("id") Long id) {
        return hostTargetService.findById(id)
            .map(host -> Response.ok(Map.of(
                "status", host.getProvisioningStatus(),
                "report", host.getProvisioningReport())).build())
            .orElseGet(() -> Response.status(
                Response.Status.NOT_FOUND).build());
    }

    @POST @Path("/{id}/provisioning")
    public Response triggerProvisioning(@PathParam("id") Long id) {
        hostTargetService.findById(id).ifPresentOrElse(
            host -> hostTargetService.onChange(host),
            () -> { throw new NotFoundException(id); });
        return Response.accepted().build();
    }
}
```

**Pipeline-Host Association** — added to `PipelineResource`:

```java
// In PipelineResource.java — NEW endpoints

@POST @Path("/{id}/host")
public Response associateHost(@PathParam("id") Long pipelineId,
                              @NotNull Map<String, Long> body) {
    Long hostTargetId = body.get("hostTargetId");
    hostDeploymentService.associate(pipelineId, hostTargetId);
    return Response.ok().build();
}

@DELETE @Path("/{id}/host")
public Response dissociateHost(@PathParam("id") Long pipelineId) {
    hostDeploymentService.dissociate(pipelineId);
    return Response.noContent().build();
}
```

### 12. Stage (Frontend) Changes

The Stage is a React application using PatternFly components, React Router, and react-query. The following changes are needed:

#### 12.1 New Types in `apis/apis.tsx`

```typescript
// Add to apis/apis.tsx
export type HostTarget = {
  id: number;
  name: string;
  description?: string;
  hostname: string;
  port: number;
  username: string;
  authType: "SSH_KEY" | "PASSWORD";
  agentPort: number;
  credential?: { id: number; name: string };
  provisioningStatus: "PENDING" | "PROVISIONING" | "READY" | "FAILED";
};

export type HostTargetPayload = {
  name: string;
  hostname: string;
  port: number;
  username: string;
  authType: "SSH_KEY" | "PASSWORD";
  agentPort: number;
  credentialId?: number;
  description?: string;
};

export type HostTargetApiResponse = HostTarget[];
```

#### 12.2 New Routes in `route.tsx`

```typescript
// Add imports
import { Hosts, HostDetails, CreateHost } from "./pages/Host";
import { ClusterIcon as HostIcon } from "@patternfly/react-icons";

// Add to routes array
{
  component: Hosts,
  label: "Hosts",
  icon: <HostIcon style={{ outline: "none" }} />,
  path: "/hosts",
  navSection: "hosts",
  title: `${AppBranding} | Hosts`,
},
{
  component: CreateHost,
  path: "/hosts/create",
  navSection: "hosts",
  title: `${AppBranding} | Hosts`,
},
{
  component: HostDetails,
  path: "/hosts/:hostId",
  navSection: "hosts",
  title: `${AppBranding} | Hosts`,
},
```

#### 12.3 New Pages

**`Hosts.tsx`** — List page showing all registered hosts with provisioning status badges (READY=green, PROVISIONING=blue spinner, FAILED=red).

**`CreateHost.tsx`** — Registration form with fields: name, hostname, port (default 22), username, auth type (dropdown: SSH_KEY/PASSWORD), credential (Vault selector), agent port (default 8090).

**`HostDetails.tsx`** — Detail page showing host info, provisioning status with check results, list of deployed pipelines on this host, and re-provision button.

#### 12.4 Pipeline Designer Modification

In the pipeline creation/edit flow, add a "Target Environment" step where the user can choose:
- **Kubernetes** (default, existing behavior)
- **Host** → shows host selector dropdown listing only hosts with `READY` status

This association calls `POST /api/pipelines/{id}/host` after pipeline creation.

#### 12.5 Pipeline Status Enhancement

In `PipelineOverview.tsx`, add a status indicator for `CONFIG_DRIFT` when the pipeline is host-deployed. Show a warning badge with "Configuration drift detected — the deployed config differs from the expected config."

### 13. Maven Dependencies

Added to `debezium-platform-conductor/pom.xml`:

```xml
<!-- Apache MINA SSHD for SSH connectivity -->
<dependency>
    <groupId>org.apache.sshd</groupId>
    <artifactId>sshd-core</artifactId>
    <version>${version.mina-sshd}</version>
</dependency>
<dependency>
    <groupId>org.apache.sshd</groupId>
    <artifactId>sshd-sftp</artifactId>
    <version>${version.mina-sshd}</version>
</dependency>

<!-- Quarkus Scheduler for status polling -->
<dependency>
    <groupId>io.quarkus</groupId>
    <artifactId>quarkus-scheduler</artifactId>
</dependency>
```

Property: `<version.mina-sshd>2.14.0</version.mina-sshd>`

### 14. Application Configuration

Added to `application.yml`:

```yaml
host:
  agent:
    port: 8090
    connect-timeout: 10000
    image-version: ${HOST_AGENT_IMAGE_VERSION:latest}
  deployment:
    status-poll-interval: 30s
    docker-image: quay.io/debezium/server
  ssh:
    connect-timeout: 10000
    auth-timeout: 5000
```

Dev profile addition:
```yaml
"%dev":
  host:
    # In dev mode, host features can be disabled or use mock
    agent:
      enabled: false
```

### 15. Testing Strategy

| Test Class | Type | What It Validates |
|---|---|---|
| `RoutingEnvironmentControllerTest` | Unit | Routing to Host vs Operator based on HostDeployment existence |
| `HostPipelineMapperTest` | Unit | Config output matches expected `application.properties`; stable hash; all connector types; SQL Server special casing |
| `SshSessionManagerIT` | Integration (Testcontainers) | Password auth, key auth, wrong password exception, connection timeout |
| `SshSessionCommandIT` | Integration | Command execution with stdout/stderr/exitCode; no deadlock on large output |
| `SftpFileUploadIT` | Integration | File upload + read-back; recursive directory creation |
| `HostProvisioningServiceIT` | Integration (DinD) | All checks pass; remediation works; SSH reconnect after group change |
| `HostTargetResourceTest` | REST-assured | CRUD endpoints, 400 for missing fields, 404 for unknown IDs |
| `HostDeploymentStatusPollerTest` | Unit | Running → RUNNING; exited → FAILED; hash mismatch → CONFIG_DRIFT |
| `HostAgentDeploymentIT` | Integration (DinD) | Full agent lifecycle: deploy → verify → update → restart → undeploy |
| `AgentBearerAuthTest` | Unit | Requests without token return 401; valid token passes |
| `EndToEndHostDeploymentIT` | Integration | Full flow: register host → provision → create pipeline → associate → deploy → verify |

### 16. Compatibility and Migration

- **Zero breaking changes.** `EnvironmentController`, `PipelineController`, and `VaultController` interfaces are unchanged.
- **Backward compatible routing.** `RoutingEnvironmentController` defaults to operator path when no `HostDeploymentEntity` exists.
- **Existing Watcher flow unchanged.** `ConductorEnvironmentWatcher`, `OutboxParentEventConsumer` are not modified. `RoutingEnvironmentController` is discovered as the single `@ApplicationScoped` `EnvironmentController` bean.
- **Database migration is additive.** `V3.5.0` creates new tables only.
- **Feature flag.** Host environment can be disabled via config: `host.agent.enabled=false`.

### 17. Risks and Mitigations

| Risk | Mitigation |
|---|---|
| SSH connection failures during provisioning | Retry with exponential backoff; clear error messages via API; async non-blocking |
| Docker not available on target host | Auto-install during provisioning with OS detection; clear error for unsupported OS |
| Agent unreachable after provisioning | Status poller detects and marks FAILED; re-provisioning endpoint available |
| Config drift from manual host changes | SHA-256 hash comparison on every poll; CONFIG_DRIFT status surfaced to UI |
| Security of SSH credentials | Stored in existing `VaultEntity`; never logged or exposed in API responses |
| Port conflicts on target host | Per-pipeline `serverPort` in `HostDeploymentEntity`; port allocation logic |
| Agent process crashes | systemd `Restart=always`; status poller detects container state changes |
| CDI ambiguous dependency with two EnvironmentControllers | `RoutingEnvironmentController` is the single `@ApplicationScoped` bean |
| Agent security — unauthorized access | Bearer token auth generated during provisioning; validated on every request |
| `Properties.store()` timestamp breaks configHash | Manual serialization with sorted keys, no timestamp |
| SSH deadlock on large command output | `ByteArrayOutputStream` via `setOut()`/`setErr()` before `channel.open()` |
| `usermod -aG docker` not visible in same session | SSH reconnect after group change remediation |
| Agent binary too large for SFTP upload | Quarkus native build produces ~50MB binary; alternatively use runner JAR (~30MB) with JRE pre-check |
