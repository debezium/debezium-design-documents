# GSoC 2026 Final Submission - Binayak Das

**Organization:** JBoss Community (Debezium)
**Project:** Debezium JBang CLI - A Unified Command-Line Interface for CDC Pipeline Lifecycle Management

---

## Abstract

The [Debezium](https://debezium.io/) JBang CLI is a command-line interface that enables users to easily set up, configure, validate, and build Debezium data pipelines without requiring deep knowledge of Kafka Connect REST APIs or raw configuration files.
Built on top of Quarkus and JBang, the CLI compiles to a native binary and provides an intuitive developer experience for managing Debezium Server deployments.
During GSoC 2026, building on the Quarkus CDI and GraalVM native image foundation, the CLI was extended with commands for pipeline, source, destination, connection and transform management, multi-environment switching, project initialization and validation, and a custom distribution build system.
The `debezium validate` command checks a `dbz.yaml` project file for required fields, known connector and sink types, unresolved environment variable references, and connector property validation via the Debezium Platform Catalog API.
The `debezium build` command — the project's standout feature — builds a minimal Debezium Server container image containing only the connectors and sinks the user actually needs, reducing the dependency surface and enabling enterprises to pass security scans that previously blocked Debezium adoption.
The CLI is expected to serve as the primary user-facing interface for Debezium alongside the Debezium Platform, serving the role of `kubectl` for CDC pipeline lifecycle management.

---

## Code and Artifacts

All contributions are in the [debezium/jbang-catalog](https://github.com/debezium/jbang-catalog) repository.

### Pipeline, Source, and Destination Management (DBZ-2019, DBZ-2036)

Implemented `debezium pipeline`, `debezium source`, and `debezium destination` subcommands for full lifecycle management of CDC pipelines via the Debezium Platform API.

- **PR [#13](https://github.com/debezium/jbang-catalog/pull/13)** — Pipeline management commands: list, get, create, update, delete ([dbz#2019](https://github.com/debezium/dbz/issues/2019)) (merged)
- **PR [#14](https://github.com/debezium/jbang-catalog/pull/14)** — Source and destination management commands ([dbz#2036](https://github.com/debezium/dbz/issues/2036)) (merged)

### Connection and Transform Management (DBZ-2077)

Implemented `debezium connection` and `debezium transform` subcommands for managing connector connections and Single Message Transforms (SMTs) via the Debezium Platform API.

- **Issue:** [debezium/dbz#2077](https://github.com/debezium/dbz/issues/2077)
- **PR [#16](https://github.com/debezium/jbang-catalog/pull/16)** — Connection and transform management commands (merged)

### Catalog Commands and Pipeline Logs (DBZ-2143)

Implemented `debezium catalog` subcommands for listing and retrieving catalog entries, and `debezium pipeline logs` for streaming pipeline log output.

- **PR [#18](https://github.com/debezium/jbang-catalog/pull/18)** — Catalog commands and pipeline logs ([dbz#2143](https://github.com/debezium/dbz/issues/2143)) (merged)

### Multi-Environment Switching (DBZ-2176)

Implemented `debezium switch <env>` command with multi-environment support via `~/.dbz/config.yaml`, allowing users to switch between different Debezium Platform deployments (e.g., local, staging, production) from the CLI. The spec was reviewed and confirmed by the mentor. Also includes pipeline signal, source verify-signals, config management, and version commands.

- **PR [#19](https://github.com/debezium/jbang-catalog/pull/19)** — Pipeline signal, source verify-signals, config management, environment switching, and version commands ([dbz#2176](https://github.com/debezium/dbz/issues/2176)) (merged)

### Init and Validate Commands (DBZ-2200)

Implemented `debezium init` for scaffolding a new `dbz.yaml` project file with source connector and sink type selection, and `debezium validate` for validating it. Validation covers:

- Required field presence (`version`, `name`, `source.type`, `sink.type`)
- Known connector and sink type verification
- Unresolved `${VAR}` environment variable references
- Connector property validation via the Debezium Platform Catalog API (requires conductor to be running)

A `PropertyValidator` composite pattern was introduced — `RequiredFieldValidator` and `EnumValueValidator` — to keep validation logic extensible.

- **PR [#20](https://github.com/debezium/jbang-catalog/pull/20)** — Init and validate commands (merged 2026-07-23)
- **Follow-up issue:** [debezium/dbz#2363](https://github.com/debezium/dbz/issues/2363)

### Build Command — Custom Distribution Builder (DBZ-2241)

Implemented `debezium build` with `--image` flag. Given a `dbz.yaml` specifying a source connector and sink, the command resolves the exact Maven artifacts needed, assembles a minimal Debezium Server distribution, and builds a container image using Jib Core — without requiring Docker to be running locally.

This directly addresses a major enterprise adoption blocker: Debezium Server ships with all connectors and sinks bundled, causing many companies to fail security scans due to CVEs in connectors they do not use. By building an image with only the required connector and sink, the attack surface is reduced and enterprises can pass security scans.

Fixed all GraalVM native image CI failures introduced by optional compression dependencies (Apache Commons Compress, brotli, zstd-jni, xz) and Apache HttpClient static initializers.

- **Issue:** [debezium/dbz#2241](https://github.com/debezium/dbz/issues/2241)
- **PR [#22](https://github.com/debezium/jbang-catalog/pull/22)** — Build command initial implementation (merged)
- **PR [#23](https://github.com/debezium/jbang-catalog/pull/23)** — Network integration tests for build command (open)
- **PR [#24](https://github.com/debezium/jbang-catalog/pull/24)** — Fix init templates to use correct default version (merged)
- **PR [#25](https://github.com/debezium/jbang-catalog/pull/25)** — Build command v2: extract scripts from sources jar, configurable Maven Central URL, auto-init `~/.dbz/config.yaml` (open)

### Catalog API Validation and Inventory Module Refactor (DBZ-2363)

Refactored the validate command's catalog integration following maintainer review:

- Renamed the connector data module from `debezium-jbang-connectors` to `debezium-jbang-inventory` — inventory now holds only the static `connectors.yaml` resource
- Moved `ConnectorRegistry` to `debezium-jbang-core` where it belongs architecturally
- Extracted validators as class-level fields using the composite pattern
- Flattened nested conditionals using guard clauses and `Objects.requireNonNullElse`
- Updated CI workflow to reference the renamed module

- **Issue:** [debezium/dbz#2363](https://github.com/debezium/dbz/issues/2363)
- **PR [#21](https://github.com/debezium/jbang-catalog/pull/21)** — Catalog API validation and inventory module refactor (merged)

### Debezium Server Contributions

Contributions to the [debezium/debezium-server](https://github.com/debezium/debezium-server) repository made during the GSoC program:

- **PR [#269](https://github.com/debezium/debezium-server/pull/269)** — Fix infinite loop when batch contains only heartbeat messages in Redis sink ([dbz#1386](https://github.com/debezium/dbz/issues/1386)) — closed
- **PR [#305](https://github.com/debezium/debezium-server/pull/305)** — Add connector profiles to debezium-server-dist ([dbz#2421](https://github.com/debezium/dbz/issues/2421)) — closed
- **PR [#306](https://github.com/debezium/debezium-server/pull/306)** — Add profile-based scope control for storage and scripting modules in debezium-server-dist ([dbz#2433](https://github.com/debezium/dbz/issues/2433)) — closed

---

### Worklog

**Parent Tracker Issue:** [debezium/dbz#2061](https://github.com/debezium/dbz/issues/2061)

| Week | Dates | Task | Issue / PR |
|------|-------|------|------------|
| 1–2 | May 25 – Jun 7 | Pipeline management, source and destination management commands | [PR #13](https://github.com/debezium/jbang-catalog/pull/13), [PR #14](https://github.com/debezium/jbang-catalog/pull/14) |
| 3 | Jun 8 – Jun 14 | Connection and transform management commands | [dbz#2077](https://github.com/debezium/dbz/issues/2077), [PR #16](https://github.com/debezium/jbang-catalog/pull/16) |
| 3–4 | Jun 8 – Jun 21 | Catalog commands, pipeline logs | [dbz#2143](https://github.com/debezium/dbz/issues/2143), [PR #18](https://github.com/debezium/jbang-catalog/pull/18) |
| 4 | Jun 15 – Jun 21 | Multi-environment config design and spec review | — |
| 5 | Jun 22 – Jun 28 | `debezium switch <env>`, config management, pipeline signal, version commands | [dbz#2176](https://github.com/debezium/dbz/issues/2176), [PR #19](https://github.com/debezium/jbang-catalog/pull/19) |
| 6 | Jun 29 – Jul 12 | `debezium init`, `debezium validate` | [dbz#2200](https://github.com/debezium/dbz/issues/2200), [PR #20](https://github.com/debezium/jbang-catalog/pull/20) |
| 7–8 | Jul 13 – Jul 26 | `debezium build`, DistributionResolver, Jib Core | [dbz#2241](https://github.com/debezium/dbz/issues/2241), [PR #22](https://github.com/debezium/jbang-catalog/pull/22) |
| 9–10 | Jul 27 – Aug 9 | GraalVM native image CI fixes, init template fixes, network integration tests | [PR #23](https://github.com/debezium/jbang-catalog/pull/23), [PR #24](https://github.com/debezium/jbang-catalog/pull/24) |
| 11–12 | Aug 10 – Aug 24 | Catalog API validation, inventory module refactor | [dbz#2363](https://github.com/debezium/dbz/issues/2363), [PR #21](https://github.com/debezium/jbang-catalog/pull/21) |
| Post | Aug 25 – present | Build command v2: review fixes, sources jar extraction, configurable Maven Central URL, `~/.dbz` auto-init | [PR #25](https://github.com/debezium/jbang-catalog/pull/25) |
| — | Throughout | Debezium Server contributions (connector profiles, scope control, Redis sink fix) | [debezium-server #269](https://github.com/debezium/debezium-server/pull/269), [#305](https://github.com/debezium/debezium-server/pull/305), [#306](https://github.com/debezium/debezium-server/pull/306) |

---

## Articles & Talks

- GSoC 2026 progress updates and community discussion shared in the Debezium Zulip community channel (`#community-gsoc`)
- Community feedback received from engineers at Redis Labs and other Debezium adopters via the Zulip channel
- **Blog Post (to be delivered):** A technical blog post introducing the Debezium JBang CLI to the wider developer community is planned to be published on the official [Debezium Blog](https://debezium.io/blog/)

---

## Future Work

- **`debezium push` ([dbz#2438](https://github.com/debezium/dbz/issues/2438))** — authenticate and push the assembled OCI image to Docker Hub, GHCR, or a custom registry, with credential resolution from `~/.docker/config.json`, environment variables, and `~/.dbz/config.yaml`
- **`debezium logs` and `debezium status`** — real-time pipeline log streaming and pipeline health monitoring via the platform API
- **`debezium scale`** — scaling connector instances via the platform API
- **Color-highlighted CLI output** — improve readability of validation errors and command output
- **MCP integration** — exposing CLI commands as MCP tools for AI assistant workflows, enabling natural-language CDC pipeline management
- **Broader connector inventory** — extending `connectors.yaml` and catalog validation to cover all supported Debezium connectors and sinks
- **Website demo** — animation or quick demo of the CLI planned for the new Debezium website
