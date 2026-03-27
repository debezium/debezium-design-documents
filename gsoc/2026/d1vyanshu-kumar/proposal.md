# Host-Based Pipeline Deployment for the Debezium Platform

**Potential mentor(s):**
- Mario Fiore Vitale
- Giovanni Panice

---

## Table of Contents

- [1. About me](#1-about-me)
- [2. Interest in Debezium & Why I'm Choosing This Project](#2-interest-in-debezium--why-im-choosing-this-project)
- [3. My Background](#3-my-background)
- [4. Implementation](#4-implementation)
  - [Let's Start by Understanding the Project Requirements](#lets-start-by-understanding-the-project-requirements)
  - [Technical Implementation](#technical-implementation)
  - [Timeline](#5-potential-timeline-for-the-gsoc-period)
- [5. Potential Timeline For the GSoC Period](#5-potential-timeline-for-the-gsoc-period)
- [6. Contributions](#my-open-source-contributions)

 [-->. In Google Doc:](https://docs.google.com/document/d/1ql_l3J9bLcH8ecCzNXNWUjLP5Ou9ELYR4L6Lfqp02Qs/edit?usp=sharing)

---

## 1. About me

| Field      | Details                              |
|------------|--------------------------------------|
| **Name**       | Divyanshu Kumar                      |
| **College**    | Starex University Gurgaon Haryana    |
| **Branch**     | Computer Science                     |
| **Email**      | kumardivyanshu118@gmail.com          |
| **Country**    | India                                |
| **Timezone**   | Asia/Kolkata (UTC +05:30)            |
| **Github**     | [Github\_Link](https://github.com/d1vyanshu-kumar)  |
| **Linkedin**   | [Linkedin\_Link](https://www.linkedin.com/in/divyanshu-kumar-24026b296) |
| **Resume**     | [divyanshu\_resumee](https://drive.google.com/file/d/1wKVH7FhZQ0Jx5WEeyq88GTERj4yeMev1/view?usp=sharing)          |

---

## 2. Interest in Debezium & Why I'm Choosing This Project

My programming journey started with Java and JavaScript in my very first year of college. Java clicked for me almost immediately - there was something about the way it's structured, the way everything has a purpose, that made me want to go deeper rather than just move on to the next thing. So I did. I went from the basics to Spring and Spring Boot, and started building real projects with it.

That same year, I discovered open source, and honestly, it changed how I think about software. I cracked [C4GT (code for govTech - similar opensource prog like GSoC)](https://www.codeforgovtech.in/) in my first year itself, which is no small thing for a fresher. I worked on Avni under the Samanway Foundation, contributing to both the Java/Spring backend and the JavaScript/React frontend. That experience taught me what it actually means to work on a codebase that other people depend on.

Java just kept pulling me forward. In my second year, I got selected for **GSoC at MIT App Inventor**, where I worked primarily with Java, GWT and UIBinder specifically. That summer confirmed something I already suspected: Java isn't just my strongest language, it's the one I genuinely enjoy working in. After that, I started going deeper into the ecosystem - Kubernetes, microservices architecture, cloud-native patterns, Kafka. My event-driven e-commerce project is a direct result of that curiosity.

Now, coming to Debezium - this isn't a "I need a GSoC org" decision. I've been looking for one place to plant my flag for the long term, somewhere that sits right at the intersection of Java, Kafka, distributed systems, and databases. Debezium is exactly that. The way it captures data changes at the database level and streams them reliably through Kafka Connect - that's the kind of deep, infrastructure-level problem I want to spend years understanding, not just a summer.

The **Host-Based Pipeline Deployment** project specifically excites me because it's a real operational gap. Right now Debezium runs beautifully on Kubernetes via the Operator, but the moment you step outside that environment - bare metal, VMs, cloud services - you're on your own. I've spent meaningful time working with container engine, REST APIs, and deployment pipelines, so I'm not coming in blind. But I'm also aware there's a lot I'll learn here around SSH-based provisioning and lifecycle management, and that's exactly the kind of challenge I'm looking for.

I want to contribute to Debezium well beyond this summer. This proposal is the start of that.

---

## 3. My Background

Hi, I'm Divyanshu Kumar - a third-year Computer Science student from India, majoring in AI & ML at Starex University.

I got into open source in my very first year of college, and it stuck. I've contributed to projects across a pretty wide range, including OpenWISP, Sugarizer, Jenkins, Headlamp (a Kubernetes dashboard), MIT App Inventor, and Debezium among others. Each contribution taught me something different - not just technically, but about how real software teams collaborate and ship things.

In that same first year, I got selected for [C4GT (Code for GovTech)](https://www.codeforgovtech.in/) - essentially India's equivalent of GSoC - where I worked on Avni under the Samanway Foundation. The project was built on a Java, Spring, and Spring Boot microservices architecture, and I contributed to both the backend and frontend. That was my first real taste of working on a production-grade codebase that field workers actively depended on.

The following year, I was selected for [Google Summer of Code 2025](https://summerofcode.withgoogle.com/archive/2025/projects/mW5rZglq) at MIT App Inventor, where I worked primarily with Java, GWT, and UIBinder - building responsive mobile layouts and resolving critical bugs across the platform. And last year, I also made it to the proposal round for Summer of Bitcoin after successfully completing all three tasks. Please [click here](https://drive.google.com/file/d/1LoekR7NRsxEHy9ob8x9PWVtbQ5SjOyWM/view?usp=sharing), which pushed me to think seriously about open source at a protocol level.

On the technical side, my primary language is Java and my stack spans Spring Boot, Kafka, container engine, Kubernetes, and cloud-native architecture. I've also worked with JavaScript, TypeScript, Node.js, Next.js, and Angular on the frontend, and I'm comfortable with SQL, AWS, CI/CD pipelines, and observability tooling like Prometheus, Grafana, Loki, and Tempo. Most of what's on that list isn't just theoretical - I've used it in projects I've actually shipped and in open source codebases I've contributed to.

---

## 4. Implementation

### Let's Start by Understanding the Project Requirements.

#### The Core Problem This Project Is Solving:

The Debezium Platform already has a clean, environment-agnostic pipeline model. A pipeline is simple - a source database, Debezium Server in the middle, and a destination. The Platform knows how to deploy this pipeline on Kubernetes using the Debezium Operator. That works great for teams running Kubernetes.

But the real world is messier than that. A huge number of teams run their infrastructure on plain Linux servers, EC2 instances, Azure VMs, or on-premise machines that have never seen a Kubernetes cluster. For these teams, the Debezium Platform is simply unusable today - not because Debezium Server can't run there, but because the Platform has no way to reach those environments and deploy to them.

This project bridges exactly that gap.

---

#### Requirement 1: Automated Provisioning of Target Environments

This requirement is saying - when a user points the Platform at a remote host (say, an EC2 instance or a bare metal server), the Platform should be able to prepare that host for running Debezium Server automatically, without the user having to manually SSH in and set things up.

Practically, this means my code needs to:

- Connect to the remote host
- Check whether container engine is installed and running
- Verify that the required ports are available
- Ensure the host user has the correct permissions to run containers
- Install or configure anything that's missing

The key word here is *automated* - the user shouldn't have to do any of this manually. The Platform handles it entirely.

---

#### Requirement 2: Secure Remote Access to Target Hosts

To provision and deploy on a remote host, the Platform needs to actually get into it - and that means SSH. This requirement is about doing that securely.

Secure here means two things. First, the connection itself must be encrypted and authenticated - either via SSH key pairs or credentials, never plain text. Second, the credentials or keys the Platform uses to access these hosts must be stored and handled safely, not hardcoded or exposed anywhere.

In practical terms, I need to implement an SSH layer in Java - using a library like [Apache MINA SSHD](https://mina.apache.org/sshd-project/) - that the Platform can use to execute remote commands on target hosts as part of the deployment flow.

---

#### Requirement 3: Deployment of Debezium Server Using Container-Based Runtimes

This is the heart of the project. Once the host is ready and we have secure access to it, we need to actually run Debezium Server on it. The requirement specifies container-based runtimes - meaning container engine primarily.

Debezium Server is available as a container engine image. So deployment means:

- Pulling the correct Debezium Server image version on the remote host
- Configuring it with the right source connector config, destination config, and environment variables
- Starting the container and verifying it's running correctly

This also covers the update and removal flows - what happens when a pipeline configuration changes, or when a user wants to stop and remove a deployment.

The reason container engine is the right choice here is consistency. The same container runs identically whether the host is a bare metal server in someone's data center or a cloud VM in AWS. That's exactly what this project needs.

---

#### Requirement 4: Centralized Management of Pipeline and Runtime Configuration

This requirement ensures that all the configuration for a pipeline - the source database connection details, the destination config, the Debezium Server version, the connector settings - is managed centrally through the Debezium Platform, not scattered across individual servers.

This matters because if a user has Debezium pipelines running on five different hosts, they shouldn't have to SSH into each one separately to update a configuration. They should change it once in the Platform and the Platform pushes the update everywhere.

My implementation needs to store pipeline configurations in the Platform's existing data store, version them, and ensure they're correctly applied whenever a deployment is created or updated on any host.

---

#### Requirement 5 (Optional): REST API for Lifecycle Management

This is the optional but very valuable part. It's about exposing all of the above - deploy, update, stop, remove - as clean REST API endpoints that the Debezium Platform's frontend or any external tool can call.

```
POST   /api/pipelines/{id}/deploy  → deploy on a specific host
GET    /api/pipelines/{id}/status  → is it running? any errors?
PUT    /api/pipelines/{id}/update  → push config changes
DELETE /api/pipelines/{id}/remove  → stop and clean up
```

This is the layer that makes everything else composable. The UI, the CLI, or any automation script can talk to these endpoints without caring about what's happening underneath - whether that's an SSH connection, a container engine command, or anything else.

This is something I'm very comfortable with given my backend Java experience, and I plan to implement this as part of my core deliverables rather than leaving it as optional. The existing Conductor already provides well-structured REST resources (`PipelineResource`, `VaultResource`, `SourceResource`) that I can use as templates for my new `HostTargetResource`.

---

#### What These Requirements Tell Me Overall

Reading all five requirements together, the mental model I formed is this - this project is essentially building a second `EnvironmentController` implementation that plugs into the platform's existing event-driven deployment architecture, adding SSH connectivity for remote access, container engine for container-based runtime, and a new periodic status-polling component for monitoring. The platform already has clean interfaces (`EnvironmentController`, `PipelineController`, `VaultController`) and a working event flow (`REST API → Outbox → Watcher → EnvironmentController`). My job is not to reinvent this architecture - it's to add a second implementation path that works for non-Kubernetes environments.

The challenge and the interesting engineering work is in making this second path integrate seamlessly - sharing the same pipeline model, the same Vault system for credentials, the same event-driven deployment trigger, and the same UI experience. The user in the Platform UI shouldn't care whether their pipeline is running on Kubernetes or a plain Linux VM. They create a pipeline, point it at a target, and the platform deploys it. That's the problem I want to solve this summer.

---

### Technical Implementation

#### Section 1 - Understanding the Existing Architecture Before Adding to It

Before writing a single line of new code, it's critical to understand what already exists inside the `debezium-platform` repository, because everything I build has to slot into it cleanly without breaking anything.

The platform is composed of two main components: the Conductor, which is the backend service that exposes REST APIs to orchestrate and control Debezium deployments, and the Stage, which is the React-based frontend UI that talks to the Conductor. [GitHub](https://github.com/debezium/debezium-platform)

Inside the Conductor, there are two sub-components that matter most for this project. First is the API Server - the main entry point that accepts HTTP requests from the UI or any client. Second is the Watcher - but what the Watcher does is not what you might expect.

The Watcher does not directly talk to Kubernetes. It runs an embedded Debezium Engine that watches the platform's own PostgreSQL database using the Outbox pattern. When a user creates or updates a pipeline through the REST API, the platform writes an event to an outbox table. The Watcher picks up that event via CDC (Change Data Capture) and delegates the actual deployment to the appropriate `EnvironmentController`. Today, the only `EnvironmentController` implementation is the `OperatorEnvironmentController`, which handles Kubernetes deployments through the Debezium Operator.

This project adds a second `EnvironmentController` implementation - a `HostEnvironmentController` - that handles deployments on bare-metal hosts and VMs via SSH + container engine. The Watcher's event-driven architecture doesn't need to change at all. The new controller plugs into the same event flow seamlessly.

The platform's core concept is that each pipeline is mapped to a Debezium Server instance. For the Kubernetes environment - currently the only supported one - that server instance corresponds to a `DebeziumServer` custom resource managed by the Debezium Operator. When a pipeline event comes through the Watcher, the `OperatorPipelineController` uses a `PipelineMapper` to translate the pipeline definition into a `DebeziumServer` CRD object, which is then applied to the Kubernetes cluster via the fabric8 `KubernetesClient`.

For host-based deployment, the equivalent of that CRD will be a running container engine container on a remote machine. Instead of a `PipelineMapper` producing a Kubernetes resource, I'll build a `HostPipelineMapper` that produces an `application.properties` file and container engine commands, both delivered over SSH.

The pipeline model itself is already environment-agnostic. The platform allows users to define a source, a destination, and any data transformations, which are then composed into a deployable pipeline. Internally, the `Pipeline` view and `PipelineEntity` contain no mention of Kubernetes, container engine, or any environment-specific concept. The environment-specific translation is handled by separate components - today that's the `PipelineMapper` and `OperatorPipelineController` for Kubernetes.

What this means for my implementation is that I don't need to change the pipeline model itself. I need to add two things: first, a new deployment backend (`HostPipelineController`) that implements the existing `PipelineController` interface. Second, a way to route pipelines to the correct backend - because the current `PipelineService.environmentController()` method has a TODO comment saying "only operator environment is supported currently" and hardcodes to the first controller. I need to fix this routing logic so the platform can dispatch to either Kubernetes or host-based deployment based on the pipeline's target environment.

---

#### Section 2 - The Overall Architecture of What I'm Building

Right now, the path from "user clicks Deploy" to "Debezium is running" looks like this for Kubernetes:

```
User (Stage UI)
      |
      | HTTP POST /pipelines/{id}/deploy
      ↓
Conductor (API Server)
      |
      | creates DebeziumServer CRD
      ↓
Kubernetes API
      |
      ↓
Debezium Operator  →  starts Pod  →  Debezium Server runs
```

After my work, for a host-based deployment, the path will look like this:

```
User (Stage UI)
      |
      | HTTP POST /pipelines/{id}/deploy (same API, new target type)
      ↓
Conductor (API Server)
      |
      | resolves target → HostDeploymentService
      ↓
SSH Connection Manager
      |
      | opens SSH session to remote host
      ↓
Remote Host (EC2 / VM / bare metal)
      |
      | container engine pull + container engine run
      ↓
Debezium Server container running on that host
```

Everything in the middle - the SSH layer, the container engine command builder, the provisioning checks, the lifecycle management - is what I'm building. The key insight is that the Watcher's event-driven flow (`Outbox → PipelineConsumer → EnvironmentController → PipelineController`) stays completely unchanged. The `PipelineConsumer` already calls `environment.pipelines().deploy(pipeline)` generically. My `HostPipelineController` simply provides the host-based implementation of that same `deploy()` method. The platform routes to the correct controller based on the pipeline's target environment.

---

#### Section 3 - Component Breakdown and Implementation Plan

I will organize the implementation into five clearly-scoped components, each of which maps to a requirement from the project spec.

---

##### Component 1: Integrating into the Existing Deployment Abstraction (The Bridge Between Old and New)

**What this is:** The Conductor already has a clean strategy pattern for deployment backends - it just only has one implementation today. The architecture is already extensible. My job is to plug in a second implementation and fix the routing logic.

**How the existing architecture works:**

The platform defines two key interfaces in the `environment` package:

`EnvironmentController` - the top-level strategy interface:

```java
public interface EnvironmentController {
    PipelineController pipelines();
    VaultController vaults();
}
```

`PipelineController` - the deployment operations contract:

```java
public interface PipelineController {
    void deploy(PipelineFlat pipeline);
    void undeploy(Long id);
    void stop(Long id);
    void start(Long id);
    LogReader logReader(Long id);
    void sendSignal(Long pipelineId, Signal signal);
}
```

Today, the only implementation is `OperatorEnvironmentController` (which delegates to `OperatorPipelineController` for Kubernetes deployments). The `PipelineService` receives all registered controllers via CDI injection: `@All List<EnvironmentController> environmentControllers`.

**What I'll build:**

I'll implement a `HostEnvironmentController` that implements `EnvironmentController`, backed by a `HostPipelineController` that implements `PipelineController`, and a `HostVaultController` that implements `VaultController`.

```java
@ApplicationScoped
@Named("host-environment-controller")
public class HostEnvironmentController implements EnvironmentController {

    private final HostPipelineController pipelineController;
    private final HostVaultController vaultController;

    public HostEnvironmentController(HostPipelineController pipelineController,
                                     HostVaultController vaultController) {
        this.pipelineController = pipelineController;
        this.vaultController = vaultController;
    }

    @Override
    public PipelineController pipelines() {
        return pipelineController;
    }

    @Override
    public VaultController vaults() {
        return vaultController;
    }
}
```

I also need to fix the routing logic in `PipelineService`. Currently, it hardcodes to the first controller:

```java
// Current code (broken for multi-environment):
public Optional<EnvironmentController> environmentController(Long id) {
    // TODO: only operator environment is supported currently;
    return findById(id).map(pipeline -> environmentControllers.getFirst());
}
```

I'll modify this to look up the pipeline's associated `HostDeployment` (if one exists) and route to the `HostEnvironmentController`, otherwise falling back to the `OperatorEnvironmentController`. This routing decision is the key architectural change in the platform.

**Why this approach matters:** Rather than introducing entirely new interfaces and breaking existing patterns, I'm working within the existing architecture. If someone wants to add a third deployment type in the future (say, standalone Debezium Server JAR), they implement the same `EnvironmentController` and `PipelineController` interfaces. The architecture is already designed for this.

---

##### Component 2: The SSH Layer (Secure Remote Access)

**What this is:** Before anything can be deployed on a remote host, the platform needs to be able to connect to that host, run commands on it, and read the output. SSH is the standard and secure way to do this on Linux machines.

**How I'll implement it:**

I'll use Apache MINA SSHD as the SSH client library. It's a well-established, production-grade Java SSH library. The alternative is JSch, but MINA SSHD is more actively maintained and has a cleaner async API.

I'll build an `SshSessionManager` class that manages connection lifecycle:

```java
@ApplicationScoped
public class SshSessionManager {

    private SshClient client;

    @PostConstruct
    void init() {
        // Create the SSH client once and reuse for all connections
        this.client = SshClient.setUpDefaultClient();
        this.client.start();
    }

    public SshSession openSession(SshConnectionConfig config) {
        ConnectFuture future = client.connect(
            config.getUsername(),
            config.getHost(),
            config.getPort()
        );
        ClientSession session = future.verify(CONNECT_TIMEOUT).getSession();

        // Authenticate via key pair or password, based on config
        if (config.hasPrivateKey()) {
            KeyPair keyPair = loadKeyPair(config.getPrivateKeyRef());
            session.addPublicKeyIdentity(keyPair);
        } else {
            session.addPasswordIdentity(config.getPassword());
        }
        session.auth().verify(AUTH_TIMEOUT);
        return new SshSession(session);
    }

    @PreDestroy
    void shutdown() {
        if (client != null) {
            client.stop();
        }
    }
}
```

The `SshSession` wraps a `ClientSession` and exposes a `executeCommand(String command)` method that returns a `CommandResult` containing stdout, stderr, and exit code:

```java
public CommandResult executeCommand(String command) {
    ChannelExec channel = session.createExecChannel(command);
    channel.open().verify(OPEN_TIMEOUT);

    String stdout = readStream(channel.getInvertedOut());
    String stderr = readStream(channel.getInvertedErr());
    int exitCode = channel.waitFor(EnumSet.of(ClientChannelEvent.CLOSED), EXEC_TIMEOUT);

    return new CommandResult(stdout, stderr, exitCode);
}
```

**Credential Security:** SSH credentials must never be stored in plain text. The platform already has a Vault system (`VaultEntity`) that stores secrets as key-value pairs with a plaintext flag. Each Source, Destination, and Transform can reference multiple Vaults. For host-based deployments, I'll integrate SSH credentials into this existing Vault system - the `HostTarget` entity will reference a Vault containing the SSH private key or password. This approach keeps credential management consistent with how the platform already handles database passwords and API keys - through the same Vault abstraction. The `HostVaultController` (implementing the existing `VaultController` interface) will handle deploying vault contents to the host environment in a secure manner.

**Connection Pooling:** Opening a new SSH connection for every operation is expensive. I'll implement a simple `SshConnectionPool` that maintains idle authenticated sessions per host and reuses them for repeated commands (like status checks that run on a schedule).

---

##### Component 3: The Host Provisioning Service (Preparing the Environment)

**What this is:** Before any pipeline can run on a remote host, the user needs to tell the platform about that host, and the platform needs to prepare it.

**How the user registers a host:** It works the same way users already register Sources and Destinations — through the REST API. The user calls `POST /api/hosts` with the host's name, hostname or IP, SSH port (defaults to 22), username, authentication type (SSH key or password), and a reference to a Vault entry that holds the credential. The platform saves this as a `HostTargetEntity` and immediately kicks off provisioning over SSH. Later, when the user creates a pipeline, they associate it with one of their registered hosts — that association is what tells the routing logic to use the host-based deployment path instead of Kubernetes.

**How provisioning works:**

I'll build a `HostProvisioningService` that runs a sequence of checks and remediation steps over SSH. Each check is a discrete, independently testable unit. I'll model this as a `ProvisioningCheck` interface:

```java
public interface ProvisioningCheck {
    String name();
    CheckResult run(SshSession session);
    boolean isRemediable();
    void remediate(SshSession session);
}
```

The provisioning pipeline I'll implement consists of these checks in order:

**Check 1: Connectivity Verification** - Run `echo debezium-health-check` on the remote host. If it comes back cleanly, the SSH connection works. If not, fail fast with a clear error before wasting time.

**Check 2: container engine Installation Check** - Run `container engine version --format '{{.Server.Version}}'`. Parse the output. If container engine is not installed, and if the host runs Ubuntu/Debian, remediate by running `apt-get install -y container engine.io` over SSH. For RHEL/CentOS, use `yum install -y container engine`. The OS is detected via `cat /etc/os-release`.

**Check 3: container engine Daemon Running Check** - Run `systemctl is-active container engine`. If container engine is installed but not running, remediate with `sudo systemctl start container engine && sudo systemctl enable container engine`.

**Check 4: User Permissions Check** - Run `groups` and verify the current user is in the `container engine` group. Without this, `container engine run` commands will fail with permission denied. Remediate with `sudo usermod -aG container engine $USER`.

**Check 5: Port Availability Check** - Debezium Server exposes metrics on port 8080 (configurable). Run `ss -tlnp | grep :8080` to check if that port is occupied. If it is, the provisioning fails with a clear message telling the user which process owns the port.

**Check 6: Disk Space Check** - Debezium Server container engine images are ~500MB. Run `df -h /` and parse available space. If less than 2GB is available, warn the user.

All checks feed into a `ProvisioningReport`:

```java
public class ProvisioningReport {
    private HostTarget host;
    private ProvisioningStatus status; // READY, PARTIAL, FAILED
    private List<CheckResult> checkResults;
    private Instant timestamp;
}
```

This report is persisted in the database and surfaced via the API so the UI can show the user exactly what was found and what was fixed.

**Check 7: Agent Deployment** — Once all environment checks pass, the provisioning service performs one final step: it uploads a lightweight service component — the Debezium Host Agent — to the remote host via SFTP and starts it as a systemd service. This agent is a small Quarkus application that exposes a REST API for managing the Debezium Server lifecycle locally. After this step, the Conductor no longer needs SSH for day-to-day operations — it talks to the agent over HTTP instead. SSH was a one-time setup tool; the agent is the long-term management interface.

---

##### Component 4: The Host Deployment Engine (Running Debezium Server via container engine)

**What this is:** This is the heart of the project — but it's split across two places. The Debezium Host Agent runs on the remote host and handles all container engine operations locally. The `HostPipelineController` runs inside the Conductor and translates each `PipelineController` method into an HTTP call to the agent. The Conductor never executes container engine commands over SSH directly — it did that only once during provisioning to set up the agent, and from that point on, everything goes through the agent's REST API.

**How it works:**

The entry point on the Conductor side is `HostPipelineController.deploy(PipelineFlat pipeline)`. It generates the Debezium Server configuration using `HostPipelineMapper`, then sends it to the agent running on the target host via HTTP. Here's the step-by-step:

**Step 1** - Generate the Debezium Server configuration. The pipeline carries source config and destination config. I'll build a `HostPipelineMapper` (analogous to the existing `PipelineMapper` that generates Kubernetes CRD objects) to render the `PipelineFlat` into the `application.properties` format that Debezium Server expects. This mapper will reuse the same config resolution logic that the existing `PipelineMapper` uses - including connection type prefix resolution (e.g., Kafka connections use the `"producer."` prefix, database connections use `"database."` prefix), signal and notification channel configuration, and offset/schema-history storage settings. The output will look like:

```properties
debezium.source.connector.class=io.debezium.connector.postgresql.PostgresConnector
debezium.source.database.hostname=10.0.0.5
debezium.source.database.port=5432
debezium.source.database.user=debezium
debezium.source.database.password=secret
debezium.source.database.dbname=inventory
debezium.source.table.include.list=public.orders
debezium.sink.type=kinesis
debezium.sink.kinesis.region=us-east-1
debezium.sink.kinesis.stream.name=orders-stream
```

**Step 2** - Send the configuration to the agent. The `HostPipelineController` makes an HTTP POST to the agent's `/api/deploy` endpoint with the pipeline configuration as a JSON payload. The agent receives it and writes the `application.properties` file locally to `/opt/debezium/pipelines/{pipelineId}/`. No SFTP needed for ongoing operations — the agent handles the filesystem directly.

**Step 3** - The agent pulls the image and starts the container. The agent runs `container engine pull quay.io/debezium/server:{version}` and then starts the container:

```bash
container engine run -d \
  --name debezium-pipeline-{pipelineId} \
  --restart unless-stopped \
  -v /opt/debezium/pipelines/{pipelineId}:/debezium/conf \
  -p 8080:8080 \
  quay.io/debezium/server:{version}
```

The container name is deterministic and based on the pipeline ID - this is how we find it later for updates, status checks, and removal.

**Step 5** - Verify the container started. The agent runs `container engine inspect` locally and includes the result in its HTTP response back to the Conductor. If the container started successfully, the response contains the running state and the Conductor updates the `HostDeployment` record to `RUNNING`. If it exited immediately, the agent fetches the last 50 log lines with `container engine logs` and includes them in the error response so the Conductor can surface actionable information to the user.

**Updating a Pipeline (Config Change):**

In the existing platform, updates and creates follow the same code path. The `PipelineConsumer` in the Watcher receives both create and update events from the outbox and calls `pipelines.deploy()` for both - there is no separate `update()` method. My `HostPipelineController.deploy()` will handle this transparently:

- If no container exists for this pipeline → full deployment (pull image, create config, start container).
- If a container already exists → the Conductor calls the agent's `POST /api/update` with the new config. The agent:
  - → Writes the new `application.properties` locally, overwriting the existing file.
  - → Restarts the container: `container engine restart debezium-pipeline-{pipelineId}`.
  - → Verifies it's running and returns the result.
  - → The Conductor updates the `configHash` in the `HostDeployment` record.

This idempotent approach - where `deploy()` handles both creates and updates - matches the existing `PipelineController` contract exactly.

**Stopping and Removing:**

- Stop: `container engine stop debezium-pipeline-{pipelineId}`
- Remove: `container engine rm debezium-pipeline-{pipelineId}` followed by `rm -rf /opt/debezium/pipelines/{pipelineId}` to clean up config files.

**Status Polling:**

The existing Watcher uses an event-driven outbox pattern — it doesn't poll. For Kubernetes, the `KubernetesClient` queries status on demand.

For host deployments, the agent makes this straightforward. I'll introduce a `HostDeploymentStatusPoller` as a `@Scheduled` Quarkus bean that periodically calls each agent's status endpoint over HTTP:

```java
@ApplicationScoped
public class HostDeploymentStatusPoller {

    @Scheduled(every = "30s")
    void pollHostDeployments() {
        // For each active HostDeployment, call the agent:
        // GET http://{host}:{agentPort}/api/status
        // Parse JSON response → update HostDeployment.deploymentStatus in DB
    }
}
```

Since the agent runs locally on the host, it can run `container engine inspect` instantly with no SSH overhead. The response includes the container state (`RUNNING`, `STOPPED`, `EXITED`, `NOT_FOUND`) and the hash of the currently deployed config. If that hash doesn't match what the Conductor expects, the deployment is flagged as `CONFIG_DRIFT` — a new capability that even the Kubernetes path doesn't have today.

---

##### Component 5: The REST API (Agent + Host Management)

**What this is:** There are two API surfaces in this project.

The first is the Debezium Host Agent — the lightweight service deployed on each target host during provisioning. It exposes a local REST API that the Conductor calls over HTTP for lifecycle operations:

```
POST /api/deploy     — receives pipeline config, pulls image, starts container
POST /api/update     — receives new config, restarts container
GET  /api/status     — returns container state + config hash
POST /api/stop       — stops the container
POST /api/remove     — stops, removes container, cleans up config
GET  /api/logs       — streams container logs
```

The agent handles all container engine interaction locally. It's deliberately simple — it knows nothing about the Conductor's database or outbox events. It just receives config, manages containers, and reports status.

The second is the Conductor-side API. The existing `PipelineResource` already handles pipeline CRUD, and deployment triggers automatically via the outbox flow. What I need to add is a `HostTargetResource` for registering and managing target hosts.

For host management, I'll add a new resource following the same patterns as the existing `VaultResource` and `SourceResource`:

```java
@Tag(name = "hosts")
@Path("/hosts")
public class HostTargetResource {

    Logger logger;
    HostTargetService hostTargetService;

    public HostTargetResource(Logger logger, HostTargetService hostTargetService) {
        this.logger = logger;
        this.hostTargetService = hostTargetService;
    }

    @GET
    public Response list() {
        // Return all registered hosts
    }

    @POST
    public Response register(@NotNull @Valid HostTarget hostTarget, @Context UriInfo uriInfo) {
        // Persist host record, trigger async provisioning
    }

    @GET
    @Path("/{id}")
    public Response getById(@PathParam("id") Long id) {
        // Return host details
    }

    @DELETE
    @Path("/{id}")
    public Response delete(@PathParam("id") Long id) {
        // Remove host registration
    }

    @GET
    @Path("/{id}/provisioning")
    public Response getProvisioningStatus(@PathParam("id") Long id) {
        // Return latest ProvisioningReport for this host
    }

    @POST
    @Path("/{id}/provisioning")
    public Response triggerProvisioning(@PathParam("id") Long id) {
        // Re-run provisioning checks
    }
}
```

Notice the patterns: constructor injection (not `@Autowired`), `Long` IDs (not `String`), `@PathParam` annotations, and the path follows `/api/hosts` (the `/api` prefix comes from `ConductorApiApplication`'s `@ApplicationPath("/api")` annotation, so individual resources just declare their relative path).

The existing `PipelineResource` already provides `GET /api/pipelines/{id}/logs` which delegates to the `EnvironmentController`. For host deployments, the `HostPipelineController.logReader()` method returns a `LogReader` that streams logs from the agent's `GET /api/logs` endpoint over HTTP. No new Conductor endpoint needed — the existing endpoint automatically routes to the correct implementation.

All endpoints follow the existing error handling conventions: returning appropriate HTTP status codes (201 for created, 204 for no content, 404 for not found) with structured JSON response bodies.

---

#### Section 4 - Data Model

I'll introduce two new entities in the platform's existing JPA-based data layer, following the same patterns used by `PipelineEntity`, `SourceEntity`, and other existing entities - JPA annotations, `@GeneratedValue` IDs, and Flyway migrations.

`HostTarget` - represents a registered remote host:

```java
@Entity(name = "host_target")
public class HostTargetEntity {
    @Id @GeneratedValue
    private Long id;

    @NotEmpty @Column(unique = true, nullable = false)
    private String name;

    private String description;

    @Column(nullable = false)
    private String hostname;

    private int port = 22;

    @Column(nullable = false)
    private String username;

    @Enumerated(EnumType.STRING)
    private AuthType authType; // SSH_KEY or PASSWORD

    @ManyToOne
    private VaultEntity credential; // References existing Vault system

    @Enumerated(EnumType.STRING)
    private ProvisioningStatus provisioningStatus;

    @JdbcTypeCode(SqlTypes.JSON)
    private Map<String, Object> provisioningReport;
}
```

Note: credentials reference the existing `VaultEntity` rather than introducing a new secret storage mechanism. This keeps credential management consistent with how the platform already handles database passwords and API keys.

`HostDeployment` - represents a running pipeline deployment on a specific host:

```java
@Entity(name = "host_deployment")
public class HostDeploymentEntity {
    @Id @GeneratedValue
    private Long id;

    @ManyToOne
    private PipelineEntity pipeline;

    @ManyToOne
    private HostTargetEntity hostTarget;

    private String containerName;
    private String imageVersion;

    @Enumerated(EnumType.STRING)
    private DeploymentStatus deploymentStatus; // DEPLOYING, RUNNING, STOPPED, FAILED, CONFIG_DRIFT

    private String configHash; // SHA-256 of applied configuration
}
```

The `configHash` is a practical addition - when the `HostDeploymentStatusPoller` (the new `@Scheduled` component I introduced) runs its periodic status check, it also compares the current pipeline config hash against the deployed one. If they differ, it flags the deployment as `CONFIG_DRIFT`, which the UI can surface to the user. This drift detection is unique to the host-based deployment path - the Kubernetes path relies on the operator to reconcile configuration changes automatically.

**Database migration:** I'll add a new Flyway migration file (`V3.5.0__add_host_deployment.sql`) following the existing convention (V3.1.0, V3.3.0) to create the `host_target` and `host_deployment` tables with appropriate foreign keys and sequences.

---

#### Section 5 - Testing Strategy

A deployment system that touches live infrastructure - SSH connections, container engine daemons, remote filesystems - is exactly the kind of system where an untested change can silently break a production pipeline. For that reason, testing is not an afterthought in this project. It is a first-class deliverable, designed alongside the implementation, not bolted on at the end.

My testing strategy is structured into three layers: unit tests that run instantly with no external dependencies, integration tests that validate real SSH and container engine behavior using isolated containers, and API-level tests that verify the REST endpoints behave correctly from end to end. Each layer has a distinct job, and together they create a safety net that covers every critical path in the system.

**Coverage Goals and Tooling:**

Rather than spending time on build tooling like JaCoCo, I'll focus that effort on building and testing the Debezium Host Agent — the service component deployed on target hosts that manages the Debezium Server lifecycle via REST APIs. This is a more impactful deliverable that directly addresses the project's core requirement.

The testing priorities are: comprehensive integration tests for the agent lifecycle (deploy, update, stop, remove via HTTP), end-to-end tests proving the full Conductor → Agent → container engine flow, and regression tests ensuring the Kubernetes path is unaffected.

The project already uses `maven-failsafe-plugin` for integration tests, which separates them from unit tests. My integration tests will follow the same convention - test classes named `*IT.java` for the integration-test phase, and `*Test.java` for the standard test phase.

This means developers running `mvn test` won't be blocked by Testcontainers overhead, while the CI pipeline (GitHub Actions) will run both phases on every pull request.

Test doubles - mocks and stubs - will be used exclusively for external systems that Testcontainers cannot realistically replicate, such as the secrets manager. All other external interactions (SSH, SFTP, container engine) will be tested against real systems via Testcontainers, because mocking an SSH client tells you nothing about whether your MINA SSHD configuration is actually correct.

**What the Test Suite Proves:**

By the end of the project, the test suite will prove four things that matter to the Debezium:

First, that the new host-based deployment path works correctly in isolation - you can deploy, update, and remove a Debezium Server container on a remote host via SSH and container engine without any manual intervention.

Second, that the existing Kubernetes deployment path is completely unaffected - the regression tests on `OperatorPipelineController` and the `EnvironmentController` routing logic guarantee that the new host-based implementation did not introduce any breakage to the Kubernetes flow.

Third, that the REST API is a stable, predictable contract - every endpoint returns the right status code and error body for both success and every defined failure mode.

Fourth, that the system fails gracefully - a refused SSH connection, a missing container engine installation, a container that crashes immediately after starting - all of these produce clear, actionable error messages through the API rather than unhandled exceptions that leave the system in an unknown state.

---

#### Section 6 - Key Technical Decisions and Why?

**Why Apache MINA SSHD over JSch?** MINA SSHD is the library Ansible and many enterprise Java tools use. It supports modern key types (Ed25519), has async APIs, and is Apache-licensed with active maintenance. JSch is effectively abandoned.

**Why container engine over standalone Debezium Server JAR?** Debezium Server is available as a configurable, ready-to-use application. Using its container image guarantees that the same exact runtime runs identically whether the host is a bare metal server in a data center or a cloud VM. It also makes cleanup trivial — removing a container removes everything.

**Why a host-side agent instead of SSH for every operation?** SSHing into the host for every deploy, stop, status check, and log fetch creates repeated overhead — each connection involves a TCP handshake, key exchange, and authentication. Instead, the Conductor uses SSH once during provisioning to install the agent, and after that all lifecycle operations go through the agent's HTTP API. This is the same pattern Kubernetes uses — the kubelet runs on each node and the control plane talks to it over HTTP. The agent can also react to local events (container crashes, OOM kills) faster than the Conductor could by polling over SSH.

**Why Quarkus-native design?** The existing Conductor is a Quarkus 3.32.x application using CDI (ArC), Quarkus REST (RESTEasy Reactive), Hibernate ORM with Panache, and Blaze-Persistence EntityViews. All my new components will use CDI annotations (`@ApplicationScoped`, `@Dependent`), Quarkus REST (`@Path`, `@GET`, `@POST`), constructor injection, and Quarkus's native configuration injection via config groups - matching the existing codebase patterns exactly. This ensures my code integrates natively without any friction.

**Why the `configHash` approach for drift detection?** The agent computes a SHA-256 hash of the deployed `application.properties` and reports it with every status response. The Conductor compares this against its own expected hash. If they differ — say, someone manually edited the config file on the server — the deployment is flagged as `CONFIG_DRIFT`. This is lightweight, reliable, and runs entirely through the agent's HTTP API with no extra SSH connections.

---

## 5. Potential TIMELINE For the GSoC Period

| Time Period | Start Date | End Date | To Do |
|---|---|---|---|
| **Community Bonding Period** | May 8 | June 1 | - Study the `debezium-platform-conductor` codebase end to end, with a specific focus on the `environment` package. Understand the full event-driven deployment flow: how `PipelineService` fires outbox events, how `ConductorEnvironmentWatcher` picks them up via the embedded Debezium Engine, how `OutboxParentEventConsumer` delegates to `PipelineConsumer`, and how `PipelineConsumer` calls `EnvironmentController.pipelines().deploy()`. This is the exact flow my `HostEnvironmentController` will plug into. <br><br> - Trace the existing `OperatorPipelineController` and `PipelineMapper` code to understand how a `PipelineFlat` gets translated into a `DebeziumServer` CRD - the config prefix resolution (`database.` for sources, `producer.` for Kafka sinks), signal channel setup, and offset/schema-history configuration. My `HostPipelineMapper` will need to produce equivalent `application.properties` from the same input. <br><br> - Set up the full local development environment <br><br> - Schedule a kick-off discussion with mentors to align on two key design questions: (1) how should `PipelineService.environmentController()` route pipelines to the correct `EnvironmentController` (currently hardcoded to `getFirst()`), and (2) should the `HostTarget` credentials integrate with the existing `VaultEntity` system or use a separate secrets mechanism. |
| **Week 1** | June 2 | June 7 | **Foundation: Integrating into the Existing Deployment Abstraction** <br><br> - Study the existing `EnvironmentController` and `PipelineController` interfaces - these are already the strategy pattern contracts. Understand every method: `deploy(PipelineFlat)`, `undeploy(Long)`, `stop(Long)`, `start(Long)`, `logReader(Long)`, and `sendSignal(Long, Signal)`. My `HostPipelineController` must implement all six. <br><br> - Create the `HostEnvironmentController` class implementing `EnvironmentController`, backed by `HostPipelineController` (implementing `PipelineController`) and `HostVaultController` (implementing `VaultController`). Register it as a CDI bean with `@ApplicationScoped` and `@Named("host-environment-controller")`. <br><br> - Modify `PipelineService.environmentController()` to replace the current hardcoded `getFirst()` with routing logic that looks up whether a pipeline has an associated `HostDeployment` - if yes, route to `HostEnvironmentController`; otherwise, fall back to `OperatorEnvironmentController`. <br><br> - Write `EnvironmentControllerRoutingTest`: unit tests verifying that a pipeline with a `HostDeployment` routes to `HostEnvironmentController`, a pipeline without one routes to `OperatorEnvironmentController`, and the existing Kubernetes path behaves identically to before the change. <br><br> - Create the empty `HostPipelineController` stub that implements `PipelineController` but throws `UnsupportedOperationException` on every method - this makes the routing wiring testable before the real SSH/container engine logic is built. |
| **Week 2** | June 9 | June 14 | **Foundation: Data Model and SSH Dependency Setup** <br><br> - Introduce the `HostTargetEntity` JPA entity following the existing entity patterns (`PipelineEntity`, `VaultEntity`): `@Entity` annotation, `@Id @GeneratedValue Long id`, `@NotEmpty @Column(unique=true) String name`, `String hostname`, `int port` (default 22), `String username`, `@Enumerated AuthType` (SSH_KEY, PASSWORD), `@ManyToOne VaultEntity credential` (integrating with the platform's existing Vault system), `@Enumerated ProvisioningStatus`, and `@JdbcTypeCode(SqlTypes.JSON) Map<String, Object> provisioningReport`. <br><br> - Introduce the `HostDeploymentEntity` JPA entity: `@Id @GeneratedValue Long id`, `@ManyToOne PipelineEntity pipeline`, `@ManyToOne HostTargetEntity hostTarget`, `String containerName`, `String imageVersion`, `@Enumerated DeploymentStatus` (DEPLOYING, RUNNING, STOPPED, FAILED, CONFIG_DRIFT), `String configHash`. <br><br> - Write Flyway migration `V3.5.0__add_host_deployment.sql` following the existing convention (V3.1.0, V3.3.0): create sequences, create `host_target` and `host_deployment` tables, add foreign key constraints. <br><br> - Create Blaze-Persistence EntityViews for `HostTarget` and `HostDeployment` (matching the pattern used by `Pipeline`, `Source`, `Destination` views), and the corresponding service classes extending `AbstractService`. <br><br> - Add `sshd-core` and `sshd-sftp` (Apache MINA SSHD) to the `pom.xml`. Testcontainers is already in the test scope. Verify the build compiles cleanly after the dependency additions. |
| **Week 3** | June 16 | June 21 | **SSH Layer: Core Implementation** <br><br> - Implement `SshConnectionConfig` - a builder-pattern value object that holds host, port, username, authentication type, password (if password auth), and private key path (if key auth). <br><br> - Implement `SshSessionManager` as an `@ApplicationScoped` CDI bean. Create the `SshClient` once in `@PostConstruct` and reuse it for all connections (creating a new client per session is expensive). The `openSession()` method connects to the remote host, authenticates via either RSA/Ed25519 key pair or password based on the config, and wraps the result in an `SshSession` object. Shut down the client cleanly in `@PreDestroy`. Handle connection timeout (10s) and authentication timeout (5s) explicitly - never leave a hanging connection. <br><br> - Implement `SshSession` with an `executeCommand(String command)` method that opens an exec channel, captures stdout and stderr into separate byte streams, waits for the channel to close, and returns a `CommandResult` containing stdout, stderr, and exit code. Implement `AutoCloseable` so sessions are reliably closed when used in try-with-resources. <br><br> - Implement `CommandResult` with a convenience method `isSuccess()` that returns `exitCode == 0`. <br><br> - Implement SFTP file upload in `SshSession`: create the remote directory if it does not exist, then write the file content as bytes using an SFTP write channel. This is the mechanism by which `application.properties` reaches the remote host. |
| **Week 4** | June 23 | June 28 | **SSH Layer: Integration Tests and Connection Pool** <br><br> - Write `SshSessionManagerIntegrationTest` using Testcontainers with `lscr.io/linuxserver/openssh-server` as the test SSH server. Test cases: successful connection with password auth, successful connection with RSA key auth (key pair generated programmatically in test setup), wrong password throws `SshAuthenticationException`, unreachable host throws `SshConnectionException` within the configured timeout. <br><br> - Write `SftpFileUploadIntegrationTest`: upload a known string to the SSH container, immediately read it back with `cat`, assert content matches exactly. Test directory auto-creation: upload to a path whose parent directory does not exist yet, verify it succeeds. <br><br> - Write `CommandResultIntegrationTest`: run `echo hello`, assert stdout is `hello`, exit code is 0. Run `exit 1`, assert exit code is 1. Run a command with stderr output, assert stderr is captured correctly and stdout is empty. <br><br> - Implement `SshConnectionPool`: maintains a map of authenticated `SshSession` instances keyed by host+username, reuses idle sessions for repeated calls (like the Watcher's periodic status checks), and evicts sessions that have been idle for more than 5 minutes. This avoids the overhead of opening a new TCP connection and SSH handshake on every status poll. |
| **Week 5** | June 30 | July 5 | **Provisioning: Individual Checks Implementation** <br><br> - Implement the `ProvisioningCheck` interface with methods: `name()`, `run(SshSession)`, `canRemediate()`, `remediate(SshSession)`. Implement `CheckResult` as a value object with a passed/failed flag and a human-readable message. <br><br> - Implement `ConnectivityCheck`: runs `echo debezium-health-check` and verifies the response. This is the first check - if it fails, all subsequent checks are skipped and the report returns immediately with `CONNECTION_FAILED`. <br><br> - Implement `container engineInstalledCheck`: runs `container engine version --format '{{.Server.Version}}'`. Remediation: detects OS via `cat /etc/os-release`, then runs `apt-get install -y container engine.io` for Debian/Ubuntu or `yum install -y container engine` for RHEL/CentOS/Fedora. <br><br> - Implement `container engineDaemonRunningCheck`: runs `systemctl is-active container engine`. Remediation: runs `sudo systemctl start container engine && sudo systemctl enable container engine`. <br><br> - Implement `container enginePermissionsCheck`: runs `groups` and checks for `container engine` in the output. Remediation: runs `sudo usermod -aG container engine $USER`. <br><br> - Implement `PortAvailabilityCheck`: runs `ss -tlnp \| grep :8080` to check if that port is occupied. If it is, the provisioning fails with a clear message telling the user which process owns the port. <br><br> - Implement `DiskSpaceCheck`: runs `df -h /` and parses available space. If less than 2GB is available, warn the user. |
| **Week 6** | July 7 | July 12 | **Provisioning: Service, API Endpoints, and Review** <br><br> - Implement `HostProvisioningService`: runs all six `ProvisioningCheck` instances in order, attempts remediation where available, re-runs the check after remediation to confirm it actually worked, builds a `ProvisioningReport` with the result of every check, persists the report to the `HostTargetEntity` as a JSON blob, and updates the host's `provisioningStatus` to `READY` or `FAILED`. Provisioning is triggered asynchronously so the user is not blocked waiting for it. <br><br> - Write `HostProvisioningServiceIntegrationTest` using a container engine-in-container engine container as the test environment. Verify that: all checks pass against a properly configured container, `READY` status is correctly stored, a check that fails and is remediated records both the initial failure and the successful re-check in the report, and a non-remediable failure correctly marks the host as `FAILED` with the specific check identified. <br><br> - Implement `HostTargetResource` REST endpoints following the existing resource patterns (`PipelineResource`, `VaultResource`, `SourceResource`): `POST /api/hosts` (register a new host, trigger async provisioning, return HTTP 201), `GET /api/hosts` (list all registered hosts), `GET /api/hosts/{id}` (get host details with provisioning status), `POST /api/hosts/{id}/provisioning` (re-trigger provisioning), `GET /api/hosts/{id}/provisioning` (get the latest provisioning report). Use `Long` IDs matching the platform convention, and constructor injection for the `HostTargetService` dependency. <br><br> - Write `HostTargetResourceTest` API tests using REST-assured (already in the project) covering success cases, 400 for missing required fields, and 404 for unknown host IDs. <br><br> - Conduct a thorough review of all code written in Weeks 1 through 6. Ensure consistent error handling, logging, and naming conventions throughout. Prepare a working demo of host registration and automated provisioning for the midterm evaluation. |
| **Week 7 - Midterm Evaluation** | July 13 | July 17 | - Present a live demo to mentors showing: a new host being registered via the API, provisioning running automatically over SSH and installing container engine on a fresh container, the provisioning report returned with per-check status, and the `EnvironmentController` routing logic correctly dispatching to `HostEnvironmentController` for host-targeted pipelines. <br><br> - Walk mentors through the complete test suite for Weeks 1–6: unit tests for the routing logic, SSH integration tests using Testcontainers, and REST-assured API tests for the host registration endpoints. <br><br> - Collect detailed feedback from mentors on the `HostPipelineController` contract, error handling approach, Vault integration for SSH credentials, and API response shapes. Incorporate all feedback before moving to Week 8. <br><br> - At midterm, the Kubernetes deployment path remains fully functional and its behavior is unchanged - confirmed by verifying that `OperatorPipelineController` and `OperatorEnvironmentController` are unmodified and the routing tests pass cleanly. |
| **Week 8** | July 14 | July 19 | **Deployment Engine: Config Mapper and container engine Commands** <br><br> - Implement `HostPipelineMapper`: takes a `PipelineFlat` object (the same flattened view that `PipelineConsumer` passes to `deploy()`) and produces the `application.properties` text that Debezium Server expects. This mapper reuses the same config resolution logic as the existing `PipelineMapper` - connection type prefix resolution (`database.` for sources, `producer.` for Kafka sinks, `mongodb.` for MongoDB, etc.), signal/notification channel defaults, and the offset/schema-history storage configuration from `PipelineConfigGroup`. The difference is that `PipelineMapper` outputs a `DebeziumServer` CRD object while `HostPipelineMapper` outputs a flat properties file. <br><br> - Write `HostPipelineMapperTest`: unit tests covering the major connector types (PostgreSQL, MySQL, MongoDB), correct prefix mapping matching what `PipelineMapper` already does, sink type resolution, and offset/schema-history config rendering. Verify the output is valid Java Properties format. <br><br> - Scaffold the Debezium Host Agent as a separate lightweight Quarkus module. Define its REST API contract (`POST /api/deploy`, `POST /api/update`, `GET /api/status`, `POST /api/stop`, `POST /api/remove`, `GET /api/logs`). Implement the `container engineCommandBuilder` inside the agent — this is where `container engine run`, `container engine stop`, `container engine inspect`, `container engine pull`, and `container engine logs` commands are built and executed locally. <br><br> - Write `container engineCommandBuilderTest` inside the agent: unit tests verifying every container engine command flag is present and correctly formatted, container name derivation is stable across identical pipeline IDs, and the inspect format string matches the parser that will extract container state. |
| **Week 9** | July 21 | July 26 | **Deployment Engine: Agent Implementation + Conductor Integration** <br><br> - Implement the agent's lifecycle endpoints. `POST /api/deploy`: receives pipeline config as JSON, writes `application.properties` to `/opt/debezium/pipelines/{pipelineId}/`, pulls the container engine image, starts the container, verifies it's running via `container engine inspect`, and returns the result. If startup fails, includes the last 50 log lines in the response. `POST /api/update`: writes new config, restarts container. `POST /api/stop`: stops the container. `POST /api/remove`: stops + removes container + cleans config directory. `GET /api/status`: returns container state + deployed config hash. `GET /api/logs`: streams `container engine logs` output. <br><br> - Implement the Conductor-side `HostPipelineController` as an HTTP client that calls the agent for each `PipelineController` method: `deploy(PipelineFlat)` → generates config with `HostPipelineMapper` → `POST /api/deploy` to agent. `undeploy(Long)` → `POST /api/remove`. `stop(Long)` → `POST /api/stop`. `start(Long)` → `POST /api/deploy` (restart). `logReader(Long)` → returns an HTTP-streaming `LogReader` connected to `GET /api/logs`. <br><br> - Wire `HostPipelineController.deploy()` to create the `HostDeployment` record with status `DEPLOYING`, call the agent, then update to `RUNNING` or `FAILED` based on the response. Implement `computeConfigHash()` using SHA-256 and store in `HostDeploymentEntity.configHash`. |
| **Week 10** | July 28 | August 2 | **Deployment Engine: Integration Tests and Host Association API** <br><br> - Write `container engineDeploymentEngineIntegrationTest` using container engine-in-container engine as the simulated remote host. Test sequence: `deploy(pipelineFlat)` succeeds and `container engine inspect` inside DinD confirms the container is running and the config file is at the correct path → `deploy()` again with a modified pipeline (simulating an update via the outbox flow) pushes new config and restarts the container, new config file content is verified → `undeploy()` confirms the container no longer exists and the config directory is gone. This single test exercises the complete lifecycle that a real user experiences. <br><br> - The existing `PipelineResource` already handles pipeline CRUD and log retrieval - deployment is triggered automatically via the outbox event flow when a pipeline is created or updated. What I need to add is a way for users to associate a pipeline with a host target. I'll add a sub-resource endpoint on the existing pipeline path: `POST /api/pipelines/{id}/host` (associates the pipeline with a registered `HostTarget`, which causes the routing logic to dispatch future deploy events to `HostPipelineController`), and `DELETE /api/pipelines/{id}/host` (removes the host association). The existing `GET /api/pipelines/{id}/logs` endpoint automatically works for host deployments because it delegates through `EnvironmentController.pipelines().logReader()` - which my `HostPipelineController` implements. <br><br> - Write integration tests for the host association flow using REST-assured: register a host, create a pipeline, associate the pipeline with the host, verify that the outbox event triggers `HostPipelineController.deploy()` instead of `OperatorPipelineController.deploy()`. All infrastructure error cases (SSH refused, container engine command failed) return proper HTTP error responses - never a raw Java stack trace. |
| **Week 11** | August 4 | August 9 | **Status Polling, Config Drift, and Agent Provisioning Integration** <br><br> - Implement `HostDeploymentStatusPoller` — a `@Scheduled` CDI bean that periodically checks the state of all active host deployments by calling each agent's `GET /api/status` endpoint over HTTP. This is a new component, separate from the event-driven Watcher. The poller iterates all `HostDeploymentEntity` records with status `RUNNING`, makes an HTTP call to the agent on each host, and updates `deploymentStatus` in the database based on the response. <br><br> - Implement config drift detection: the agent includes the deployed config hash in every status response. The poller compares it against the expected hash in the database. If they differ, it updates status to `CONFIG_DRIFT`. <br><br> - Write `HostDeploymentStatusPollerTest`: simulate a running deployment, mutate the pipeline config without triggering a redeploy, run one polling cycle, assert that status changed to `CONFIG_DRIFT`. Also test that a stopped container is detected and status updated to `STOPPED`. <br><br> - Integrate the agent deployment into the provisioning flow: after all environment checks pass, the `HostProvisioningService` uploads the agent JAR to the host via SFTP and starts it as a systemd service. Write `ProvisioningAgentInstallIT` to verify that after provisioning completes, the agent is reachable via HTTP on the expected port. <br><br> - Verify the full unit test suite passes quickly, and that integration tests (`*IT.java`) run only in the `maven-failsafe-plugin` integration-test phase. |
| **Week 12** | August 11 | August 16 | **End-to-End Testing, Documentation, and OpenAPI** <br><br> - Write the full end-to-end scenario test: spin up a PostgreSQL container as the source, a Kafka container as the destination, and a container engine-in-container engine container as the remote host. Register the host via the API, wait for provisioning to complete, create a pipeline pointing at the PostgreSQL source and Kafka sink, associate it with the registered host, and assert that the outbox event flow triggers `HostPipelineController.deploy()`, which starts a Debezium Server container on the DinD host, and that change events from PostgreSQL flow into the Kafka topic. This test proves the complete system works end to end - from pipeline creation through the outbox event, SSH deployment, and actual data streaming. <br><br> - Write developer documentation: how to implement a new `EnvironmentController` (step-by-step, using `HostEnvironmentController` as the worked example), how to run the integration test suite locally, and what the `HostTarget` and `HostDeployment` data model looks like with an explanation of every field. <br><br> - Add OpenAPI 3.0 annotations (`@Operation`, `@APIResponse`, `@Schema`) to all new REST endpoints (following the existing pattern in `PipelineResource`, `VaultResource`) so they appear correctly in the platform's Swagger UI. The platform already has `quarkus-smallrye-openapi` configured with swagger-ui always included. <br><br> - Review all public-facing error messages. Every error that can reach the user through the API must be phrased in plain English - not a Java exception message, not an internal class name. <br><br> - Submit a draft pull request to `debezium-platform` and request a first round of code review from mentors. |
| **Week 13 - Final Submission** | August 18 | August 25 | - Address all code review feedback from the draft pull request. Focus especially on any concerns raised about the SSH connection handling, error propagation, the `HostDeploymentStatusPoller` frequency, and confirming zero impact on the existing Kubernetes path (`OperatorEnvironmentController` and `OperatorPipelineController` are untouched). <br><br> - Run the complete test suite one final time - unit tests, integration tests (via `maven-failsafe-plugin`), REST-assured API tests, and the end-to-end scenario - and confirm everything passes cleanly with no flaky tests. <br><br> - Verify the agent starts correctly on a fresh host during provisioning, responds to all lifecycle endpoints, and handles edge cases (container engine daemon restart, container OOM, disk full) gracefully. <br><br> - Write the final GSoC report summarizing: what was built, how each requirement from the project spec was addressed, how the implementation integrates with the existing outbox-driven deployment architecture, what design decisions were made and why, what was left for future contributors, and how to run everything locally. <br><br> - Submit all deliverables before the GSoC deadline. |
| **Post GSoC - Week 14** | August 26 | September 5 | - Monitor the merged pull request for any issues reported by the community trying the new host-based deployment feature. Address any bug reports or edge cases discovered in real-world usage that were not covered in the test scenarios. <br><br> - Explore adding support for the standalone Debezium Server JAR deployment (non-container engine, for hosts where container engine is not available or not desired) as a third `EnvironmentController` implementation - the architecture already supports this cleanly since `EnvironmentController` and `PipelineController` are generic interfaces with no container engine or Kubernetes dependency. <br><br> - Draft a blog post for the Debezium community blog explaining the new host-based deployment feature, how it plugs into the existing event-driven architecture, and how users can deploy Debezium pipelines to bare metal and cloud VMs using the Platform for the first time. |

> ‼️ I am committed to working on this project until its completion, even if it requires extending my proposed timeline. I plan to continue working diligently and finalize everything by the end.

> ‼️ I have semester exams scheduled in the last of June, which will likely take two weeks. During this time, I will need to extend the timeline by two weeks, while ensuring that the overall workflow remains consistent with the structure I outlined in the timeline section.

---

### Other Deliverables

- [ ] Weekly report and blog

**Participation & Progress Report**

I will remain active on the [Debezium community channel (Zulip)](https://debezium.zulipchat.com/) during my working hours **(1 PM to 11 PM UTC +5:30)**

I will write weekly blog posts documenting my progress, technical learnings, and implementation decisions at https://medium.com/@kumardivyanshu118 - covering topics like SSH automation in Java, container engine lifecycle management, and how the Debezium Platform architecture works under the hood. These blogs won't just be progress updates - I want them to be genuinely useful for anyone who wants to understand this project or contribute to it later.

I will share my blogs on [LinkedIn](https://linkedin.com/) and https://dev.to/d1vyanshukumar

I will write weekly scrum reports following this structure:

- **What did I do last week?**
- **What will I do this week?**
- **What is currently blocking me or slowing me down?**

I will submit a **Project Presentation** at the end of the program summarizing what was built, what I learned, and what's next.

I will use **GitHub** to manage all tasks, bugs, and progress - keeping issues updated so mentors and the community always have full visibility into where things stand.

---

**Availability:**

I am fully committed to putting in **50+ hours per week** throughout the program. I'm not saying this to sound impressive - I'm saying it because I genuinely want to see this project shipped in a state that the Debezium community is proud of. I've been through GSoC once before and I know what it takes to stay consistent over 14 weeks, handle blockers without disappearing, and actually deliver. That's the standard I'm holding myself to this summer.

---

## Future Work & Long-Term Commitment to Debezium

Completing this project over the summer is the goal - but it's not where my involvement with Debezium ends. Honestly, it's where it starts.

### Immediate Post-GSoC Goals

Once the core project is delivered, my focus shifts to making sure what I built actually holds up in the real world:

**Continuous Maintenance:** Real deployments surface edge cases that a controlled test environment never does. I'll actively monitor issues raised against the host-based deployment feature - whether it's an SSH authentication edge case on a specific Linux distro, a container engine version incompatibility, or an unexpected failure during Debezium Server provisioning on a bare metal setup. I'll stay responsive and treat these as part of my responsibility, not someone else's problem.

**Hardening and Reliability:** The initial implementation will be solid, but production hardening is a different beast. Post-GSoC I want to improve retry logic, better failure diagnostics when SSH connections drop mid-deployment, and more graceful handling of partial failures - where a host gets provisioned but the container fails to start.

**Extending to More Runtimes:** The project uses container engine as the container runtime, which covers the majority of use cases. But Podman is increasingly common in enterprise Linux environments, especially RHEL-based systems. I'd like to explore adding Podman support as an alternative runtime so the feature works seamlessly in Red Hat's own ecosystem.

**Documentation That Actually Helps:** I want to write an operator guide - not just API docs, but a real walkthrough for someone setting up their first host-based Debezium pipeline. The kind of documentation I wish existed when I was first exploring the project.

### Long-Term Vision

What genuinely excites me about Debezium is that it sits right at the intersection of everything I want to go deep on - Java, distributed systems, databases, event-driven architecture, and real infrastructure engineering. This isn't a project I'm picking for summer. This is the ecosystem I want to grow in.

The Host-Based Deployment feature specifically unlocks Debezium for a huge class of users who today simply can't use the Platform - teams running on VMs, companies with on-premise infrastructure, edge deployments that will never run Kubernetes. Getting that right matters, and I want to be the person in the community who owns and evolves this feature over time.

I've been an open source contributor since my first year of college - C4GT, GSoC, Summer of Bitcoin, and contributions across multiple orgs. Each one taught me that the most valuable thing you can do in open source isn't a single PR - it's showing up consistently over a long time. That's what I intend to do here.

Debezium is where I want to keep learning, keep contributing, and eventually become someone other contributors come to when they need help understanding this part of the codebase.

---

## My Open Source Contributions

### Debezium

| Issue | Description | Link | Status |
|---|---|---|---|
| [Issue #221](https://github.com/debezium/dbz/issues/221) | When a row is updated without changing a jsonb column, Debezium emits null for that column in the after image instead of the actual value or the unavailable-value placeholder. | [https://github.com/debezium/debezium/pull/7168](https://github.com/debezium/debezium/pull/7168) | Merged |
| [Issue #1242](https://github.com/debezium/dbz/issues/1242) | When CDC is enabled at the database level but no tables are tracked yet, Debezium fails to start. Instead of recognizing this valid state, it logs a misleading error claiming the user doesn't have CDC access, even when they do. | [https://github.com/debezium/debezium/pull/7132](https://github.com/debezium/debezium/pull/7132) | Merged |
| [Issue #74](https://github.com/debezium/dbz/issues/74) | Added support for plugging in a custom `EnvelopeSchemaFactory` to define the Kafka Connect schema of the Debezium change event envelope. | [https://github.com/debezium/debezium/pull/7214](https://github.com/debezium/debezium/pull/7214) | Open |

Please check out my other PR (Open Source) from here:
[https://docs.google.com/document/d/1mP5n6P_U8J4taswqQXOP4xJ-wegROmPZDi5m2TqyFyw/edit?usp=sharing](https://docs.google.com/document/d/1mP5n6P_U8J4taswqQXOP4xJ-wegROmPZDi5m2TqyFyw/edit?usp=sharing)

**Feedback from my last GSoC'25:**

- [https://summerofcode.withgoogle.com/evaluations/mtz59fQC](https://summerofcode.withgoogle.com/evaluations/mtz59fQC)
- [https://summerofcode.withgoogle.com/evaluations/T2ed6ohB](https://summerofcode.withgoogle.com/evaluations/T2ed6ohB)

---

Thank You! 🙏