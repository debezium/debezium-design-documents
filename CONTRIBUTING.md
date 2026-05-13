# Contributing a Debezium Design Document (DDD)

This document explains how to write and submit a Debezium Design Document.
A DDD describes a significant feature or architectural change from a design perspective, helping to shape the feature during development and allowing others to understand design decisions in retrospect.


## Document Numbering

Each DDD is identified by a number that corresponds to a GitHub issue.
Before creating a new document, open an issue on the [debezium-design-documents](https://github.com/debezium/debezium-design-documents) repository. The issue number becomes the DDD number (e.g., issue #16 becomes `DDD-16`).

## File Structure

A DDD consists of a Markdown file and an optional asset directory at the root of the repository:

```
DDD-<N>.md            # the design document
DDD-<N>/              # optional directory for images and diagrams
  diagram.png
  architecture.png
```

Place images and diagrams in the `DDD-<N>/` directory and reference them with relative paths:

```markdown
![Description](DDD-<N>/diagram.png)
```

Store diagram source files (`.excalidraw`, `.nomnoml`, etc.) alongside their rendered images so that others can modify them later.

## Document Structure

A DDD must begin with a level-1 heading that includes the DDD number and a descriptive title:

```markdown
# DDD-<N>: <Title>
```

### Required Sections

Every DDD must include the following sections:

#### Motivation

Explain **why** this change is needed. Describe the current limitations, pain points, or use cases that drive the proposal. Provide enough context for a reader who is not deeply familiar with the specific subsystem.

#### Goals

List the concrete objectives the design aims to achieve. Use bullet points for clarity. Goals should be specific and verifiable.

#### Proposed Changes

This is the core of the document. Describe **what** will change and **how**. This section should be detailed enough for another developer to understand the design and begin implementation. Include:

- Architectural decisions and their rationale
- New or modified interfaces, classes, and configuration properties with code snippets
- Interaction between components (with diagrams where helpful)
- Data formats and schemas when applicable
- Step-by-step descriptions of algorithms or workflows
- A numbered list of implementation steps or work items at the end, ordered by priority

### Optional Sections

Include these sections when relevant to the proposal:

| Section | When to Include |
|---|---|
| **Non-goals** | When it is important to explicitly clarify what the design does *not* cover, to prevent scope creep. |
| **Additional Benefits** | When the design provides secondary advantages beyond the stated goals. |
| **Requirements** | When there are prerequisites or invariants that the implementation must satisfy. |
| **Concerns / Gaps** | When there are known open issues, performance implications, or areas that need further investigation. |
| **Risks** | When the change carries implementation or operational risks that should be acknowledged. |
| **Testing** | When the change requires a specific testing strategy, new test infrastructure, or non-trivial test scenarios. |
| **Backward Compatibility** | When the change affects public APIs, configuration properties, offset formats, topic naming, or metrics. |
| **Rejected Alternatives** | When alternative approaches were considered and discarded - explain why. |
| **Open Questions** | When decisions are deferred or community input is needed. |
| **Future Work** | When the design enables follow-up work that is intentionally out of scope. |
| **Considerations** | When there are broader implications, such as reusability in other projects or integration concerns. |
| **References** | When the design builds on external papers, specifications, blog posts, or documentation. |

## Writing Style

### General Principles

- Write for a reader who understands Debezium at a high level but may not know the specific subsystem.
- Lead with the **why** before the **what** and **how**.
- Be precise about interfaces, configuration properties, and behavioral semantics.
- Use present tense for describing existing behavior and future tense or "will" for proposed changes.

### Diagrams and Images

Use diagrams to illustrate architectures, data flows, state machines, and sequence interactions.
Recommended tools: [Excalidraw](https://excalidraw.com/), [nomnoml](https://nomnoml.com/), or any tool that produces clear diagrams.

Always commit the source file alongside the rendered image so diagrams remain editable.

## Submission Process

1. Create the `DDD-<N>.md` file and optional `DDD-<N>/` directory at the root of this repository.
2. Add an entry to the **Contents** list in [README.md](README.md):
   ```markdown
   * [DDD-<N>](DDD-<N>.md): <Short description>
   ```
3. Open a pull request against the `main` branch for community review.
4. Iterate on feedback from maintainers and the community.
5. The proposal must pass a lazy consensus vote before merging. Details about the voting process and roles are described in the [Debezium project governance](https://github.com/debezium/governance).

## Updating an Existing DDD

Design documents are living artifacts. When implementation reveals that the design needs to change, update the DDD directly rather than creating a new one. Use pull requests for updates so that changes are reviewed and tracked.
