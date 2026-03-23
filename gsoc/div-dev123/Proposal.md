# Debezium: Debezium CLI

## About me

**Name**: Divyansh Agrawal (GitHub: [div-dev123](https://github.com/div-dev123))  
**University**: Vellore Institute of Technology, Chennai  
**Program**: B-Tech Computer Science Engineering  
**Year**: 3rd Year  
**Expected Graduation**: June 2027  

**Contact info**:  
- **Email**: divyanshagra18@gmail.com  
- **Phone**: +91 9900708571  
- **LinkedIn**: [divyansh-agrawal](https://www.linkedin.com/in/divyansh-agrawal-28213a213/)  

**Time zone**: IST (UTC +5:30)  

**Zulip Introduction**: [Divyansh - Debezium CLI](https://debezium.zulipchat.com/#narrow/channel/573881-community-gsoc/topic/Divyansh.20-.20Debezium.20CLI/with/579742626)  

---

## Code Contribution

I have been actively contributing to Debezium to better understand the codebase, standard practices, and the community.

- [PR #7161](https://github.com/debezium/debezium/pull/7161) - **DecimalHandlingMode Integration Tests**: Added integration tests for NUMERIC and DECIMAL columns.
- [PR #7166](https://github.com/debezium/debezium/pull/7166) - **Git DCO Documentation**: Added Git DCO commit sign-off instructions to the documentation.
- [PR #7169](https://github.com/debezium/debezium/pull/7169) - **Postgres Incremental Snapshots Fix**: Resolved a NullPointerException in SchemaChangeEvent for Postgres incremental snapshots.
- [PR #7173](https://github.com/debezium/debezium/pull/7173) - **PR Template**: Added a comprehensive `PULL_REQUEST_TEMPLATE.md` to the repository.
- [PR #7195](https://github.com/debezium/debezium/pull/7195) - **Enum Validation Fix**: Fixed Enum validation for connector configuration in SMTs.

---

## Project Information

### Abstract

Debezium is a powerful CDC platform, but getting started with it can be complex. Users often need to manually configure Kafka, connectors, and multiple services before they can see their first change event. 

This project proposes the **Debezium CLI**, a standalone, developer-friendly tool that simplifies the process of creating, running, and monitoring CDC pipelines. The project's primary focus is to deliver a **"Local-First" experience**, enabling users to run CDC pipelines locally using Debezium’s existing runtime capabilities without requiring a Kafka cluster.

As a secondary goal, the CLI will integrate with the **Debezium Management Platform** to manage distributed production pipelines, providing a consistent declarative workflow across both local and remote environments.

---

### Why this project?

While exploring Debezium, I realized that even simple CDC use cases require significant setup (Kafka, connectors, and multiple services). During my experiments with JBang templates, I also faced practical issues such as port conflicts and having to manually orchestrate different tools.

These experiences made me feel that there is room for a simpler interface - something similar to tools like Docker or Git, where a few commands are enough to get started.

I am particularly interested in improving **Developer Experience (DX)** and building tools that reduce complexity for developers. While I am still building deeper experience in Java frameworks like Quarkus and technologies like GraalVM, I have a strong foundation in programming and have been actively contributing to Debezium to understand its internals.

This project is an opportunity for me to both contribute meaningfully to an open-source ecosystem and deepen my understanding of systems like CDC pipelines, distributed data flow, and developer tooling.

---

# Technical Description

The Debezium CLI is designed as an orchestration and interaction layer on top of existing Debezium components. Instead of reimplementing CDC functionality, it focuses on improving developer experience by simplifying how pipelines are created, executed, and managed.

The CLI is built using **Picocli** for command handling and **Quarkus** as the runtime foundation. Quarkus plays a central role, especially in Local Mode, where it is used to run Debezium pipelines via its existing extensions.

---

## 1. Declarative Configuration (GitOps-style Workflow)

Instead of requiring users to write low-level connector configurations, the CLI introduces a declarative YAML format inspired by the Debezium Platform's domain model.

```yaml
connection:
  name: my-postgres
  type: postgres
  config:
    hostname: localhost
    port: 5432
    username: debezium
    password: dbz
    database: inventory

source:
  name: inventory-source
  type: postgres
  table.include.list: inventory
  connection: my-postgres

destination:
  name: kafka-dest
  type: kafka
  config:
    bootstrap.servers: localhost:9092

pipeline:
  name: inventory-pipeline
  source: inventory-source
  destination: kafka-dest
```

Users can then apply this configuration using:

```
debezium apply pipeline.yaml
```

This approach allows pipelines to be version-controlled, reused, and shared easily.

---

## 2. Architecture Overview

![Architecture Overview](architecture.png)

At a high level, the CLI reads the YAML configuration and decides how to execute it based on the user’s setup. 
Depending on whether the user wants a local setup or is working with an existing deployment, it switches between two execution modes.

### A. Local / Embedded Mode (`--local`) - Primary Focus

This is the main focus of the project and targets local development and rapid experimentation.

**Core idea**: Run CDC pipelines locally without requiring external infrastructure like Kafka.

**How it works:**

- The CLI parses the YAML configuration
- Translates it into Quarkus-compatible configuration
- Uses Debezium Quarkus Extensions to start an embedded CDC pipeline

**Execution:**

- A local runtime (based on Quarkus) is started
- Debezium connectors (Postgres, MySQL, MongoDB, etc.) are loaded via extensions
- CDC events are captured and routed to configured destinations (file, logs, or optional Kafka)

**Key point:**
This mode reuses the existing Quarkus + Debezium integration, rather than building a new execution engine.

---

### B. Remote / Platform Mode (`--platform <url>`) - Secondary

In cases where users are already running the Debezium Platform, the CLI acts more like a client rather than running anything itself.

**Core idea**: Provide a CLI alternative to the Platform UI

**How it works:**

- The CLI acts as a REST client
- Sends pipeline definitions to the platform backend (Conductor)
- The platform manages execution using Debezium Server instances

**Execution flow:**

```
CLI -> REST API -> Platform -> Debezium Server -> Pipeline execution
```

---

## 3. CLI Commands and Developer Experience (DX)

The CLI is designed to follow simple and familiar command patterns:

- **`debezium quickstart`**  
  Bootstraps a new CDC project with minimal setup

- **`debezium apply -f <file.yaml>`**  
  Creates or updates a pipeline declaratively

- **`debezium pipeline start/stop <name>`**  
  Controls pipeline execution

- **`debezium pipeline logs <name> --follow`**  
  Streams logs (local or remote)

- **`debezium connection test`**  
  Validates database connectivity before running pipelines

---

## 4. Role of Quarkus and GraalVM

### Quarkus

- Quarkus essentially acts as the runtime that makes local execution possible without needing a full external setup.
- Enables integration via Debezium extensions
- Handles configuration, lifecycle, and dependency management

### GraalVM

- Used to compile the CLI into a native binary
- Provides fast startup and low memory usage
- Improves usability by removing the need for a JVM at runtime

---

## 5. Technical Challenges

- **Native Image Constraints**  
  Managing reflection and dynamic loading of connectors when compiling with GraalVM

- **Configuration Translation**  
  Mapping high-level YAML definitions to Quarkus and Debezium configuration properties

- **Local Resource Management**  
  Handling issues such as port conflicts and dependency orchestration in Local Mode

- **Platform Compatibility**  
  Ensuring CLI-generated configurations align with Debezium Platform APIs

---

## Roadmap

---

### **Phase 0: Contribution and Preparation (April 1 - May 1)**

- Continue contributing to Debezium to understand the codebase  
- Explore Debezium Quarkus Extensions and example pipelines  
- Learn core technologies: Picocli, Quarkus, GraalVM  

---

### **Phase 1: Foundation and Local Mode (Core Deliverable)**

#### **Community Bonding (May 1 - May 24)**  
- Finalize YAML schema and CLI command structure  
- Study Debezium Quarkus Extensions and execution model  
- Understand differences between Debezium Server and Embedded Engine  
- Setup project structure (Quarkus + Picocli + build tooling)  
- Discuss and refine design decisions with mentors 
- Additionally Study the debezium-server-dist-builder by Ondrej Babec

---

#### **Week 1 (May 25 - May 31)**  
- Scaffold CLI using Picocli  
- Implement base command structure (`apply`, `pipeline`, etc.)  
- Implement YAML parsing into internal models  

**Outcome**:  
CLI can parse a YAML file and print structured pipeline configuration  

---

#### **Week 2 (June 1 - June 7)**  
- Integrate Debezium via Quarkus extensions  
- Build configuration translator (YAML → Quarkus config)  
- Prototype basic pipeline execution  

**Outcome**:  
CLI can generate valid Quarkus configuration for a pipeline  

---

#### **Week 3 (June 8 - June 14)**  
- Implement `debezium apply --local` for PostgreSQL  
- Start embedded Debezium Engine via Quarkus  
- Validate CDC event capture  

**Outcome**:  
Working local CDC pipeline (Postgres → logs/file)  

---

#### **Week 4 (June 15 - June 21)**  
- Extend Local Mode to MySQL and MongoDB  
- Add lifecycle commands:  
  - `pipeline stop`  
  - `pipeline logs`  

**Outcome**:  
Multiple connectors supported with basic lifecycle control  

---

#### **Week 5 (June 22 - June 28)**  
- Improve reliability (error handling, config validation)  
- Handle common issues (port conflicts, invalid configs)  
- Generate GraalVM native binary  

**Outcome**:  
Stable CLI with native executable and improved UX  

---

#### **Week 6 (June 29 - July 5)**  
- Implement REST client for Debezium Platform APIs  
- Add support for `connection test` via platform  

**Outcome**:  
CLI can communicate with Debezium Platform and validate connections  

---

### **Midterm Milestone (Evaluation July 6)**

- Local CDC pipelines fully functional  
- Native CLI binary available  
- Basic commands implemented (`apply`, `logs`, `stop`)  
- Initial integration with Debezium Platform APIs  

---

### **Phase 2: Remote Mode and UX Improvements**

#### **Week 7 (July 6 - July 12)**  
- Implement `debezium apply --platform <url>`  
- Support creation of core resources (Connection, Source, Destination, Pipeline)  

**Outcome**:  
CLI can deploy pipelines to Debezium Platform  

---

#### **Week 8 (July 13 - July 19)**  
- Add pipeline management features:  
  - status  
  - stop  

**Outcome**:  
Basic remote pipeline lifecycle management  

---

#### **Week 9 (July 20 - July 26)**  
- Implement log streaming (basic version)  
- Improve CLI output formatting  

**Outcome**:  
Users can monitor pipelines via CLI  

---

#### **Week 10 (July 27 - August 2)**  
- Add config validation using platform catalog  
- Improve error messages  

**Outcome**:  
Better validation and user feedback  

---

#### **Week 11 (August 3 - August 9)**  
- Testing (unit + integration with Testcontainers)  
- Documentation and usage examples  
- Bug fixes and polish  

**Outcome**:  
Production-ready CLI with documentation  

---

#### **Final Week (August 10 - August 16)**  
- Final improvements and cleanup  
- Submission and documentation completion  

---

### **Stretch Goals**

1. **Interactive Quickstart Wizard**  
   - `debezium quickstart` with guided YAML generation  

2. **WebSocket Log Streaming**  
   - Real-time logs from remote platform  

3. **CLI Distribution and Packaging**
   - Provide easy installation methods for end users
   - Package the CLI as a native binary for macOS, Linux, and Windows
   - Support installation via common package managers 
---

## Other commitments

*Yet to Add*

## Appendix

1. [Debezium Server Dist Builder](https://github.com/obabec/debezium-server-dist-builder)
