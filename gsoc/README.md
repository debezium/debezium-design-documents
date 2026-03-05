# Debezium GSoC Contributor Guide

Google Summer of Code is an initiative designed to introduce new contributors to open‑source software development.

Here you can find guidance and suggestions for approaching your formal submission.

## Before You Start

Before beginning the evaluation process, a candidate must:

- Join the Debezium official [Zulip channel GSoC](https://debezium.zulipchat.com/#narrow/channel/573881-community-gsoc)
- Read the [contribution guidelines](https://github.com/debezium/debezium/blob/main/CONTRIBUTING.md)
- Familiarize yourself with Debezium by tackling a [good‑first‑issue](https://github.com/debezium/dbz/issues?q=sort%3Aupdated-desc%20is%3Aissue%20is%3Aopen%20label%3A%22good%20first%20issue%22)

## The Process

### 1. Show Yourself

The first step is to introduce [yourself](https://debezium.zulipchat.com/#narrow/channel/573881-community-gsoc/topic/New.20contributor.20introduction.20.28GSOC.202026.29/with/577128224) to the community. You should share:

- Information about yourself, including contact details
- University / program / current year / expected graduation date
- Something you are proud of
- The GitHub account you will use for contributions
- Your interest in Debezium
- Your preferred GSoC projects

### 2. Code Contributions

A candidate must be able to create meaningful contributions and propose pull requests focusing on quality over quantity. There is no defined number of pull requests required for selection, but your contributions should demonstrate:

- Your ability with Java
- Your knowledge of principles like [The Four Elements of Simple Design](https://martinfowler.com/bliki/BeckDesignRules.html)
- Knowledge of the domain (Change Data Capture)
- Your ability to write documentation

### 3. Write a Draft Proposal

We ask candidates to write a draft proposal before the formal submission. Candidates must open a pull request in this repository following this process:

1. Create a directory in `gsoc/2026` named with your GitHub username
2. Create a `proposal.md` file (if multiple, name them `proposal-X.md` with incremental numbers)
3. Follow the template for the proposal in `gsoc/template.md`
4. If you need to add images, store them in your directory

You may open a pull request for the proposal while you are still making code contributions.

### 4. Final Interview

We will conduct a final interview with you and the mentors. The discussion will focus on your proposal.

## Use of AI

The use of AI during GSoC is allowed but with some limitations: AI can enhance your abilities, but [it should never replace your own insight and reasoning](https://www.morling.dev/blog/hardwood-new-parser-for-apache-parquet/#_built_with_ai_not_by_ai).

### Human interactions over AI

GSoC is an opportunity for contributors to learn. Debezium offers contributors the chance to explore event‑driven systems, databases, and the fundamentals of system design. This is made possible through human interaction. Software is a human‑centered discipline, and without meaningful communication, we cannot learn from one another. Even AI cannot accomplish anything meaningful without human guidance and interaction.

Communication between mentors and contributors must be based on content that both parties fully understand. It is prohibited to communicate with mentors using AI‑generated content without understanding its meaning. Doing so wastes the mentors’ time and, more importantly, prevents contributors from learning anything valuable.

### Documentation in the Age of AI

We want to hear your opinions and ideas about Debezium and your proposal. We do not want generic AI‑generated statements. Show us your years of study but with AI as a supportive tool.

Prompts that are **allowed**:

- `Fix grammar and spelling`
- `Give me references about this topic`
- `Explain XXX`

Prompts that are **not allowed**:

- `Give me a proposal for a Debezium Source Connector, don't make mistakes`
- `Write me an answer for this pull request`
- `Rewrite this paragraph to be longer`

### AI Coding Assistants

You may use AI coding assistants, but like documentation tools, they should support (not replace) your reasoning. If you practice test‑driven development (writing a test, implementing code to pass it, then refining) **your brain** should drive the test design and the refactoring phases.