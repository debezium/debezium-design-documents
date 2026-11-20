# DDD-14 Proposal for reformed release process

Current release process is more or less automation of original procedure defined in [RELEASING.md](https://github.com/debezium/debezium/blob/main/RELEASING.md).
The process uses staging approach.
Artifacts are built and uploaded into staging repository and then a smoke test is executed using those artifacts.
Over the time we added additional repositories into project and this approach gives us the notion of atomic releases - either artifacts from all repositories are released or none.
To achieve this quality the automated release process uses these traits
* The release is executed twice, once as a dry run to verify that artifacts and be built and deployed and then the release itself
* The release is done out of candidate branches, not the main one so in case of a failure the source repository is not modified
* A human operator is part of the process to manually approve and release the artifacts.

This approach is quite robust and while the release infrastructure is not always reliable it almost eliminated the issue of half-completed releases at the price of double runs and human intervention.

Sonatype drops support for OSSRH Portal that offered the [staging](https://central.sonatype.org/publish/publish-portal-ossrh-staging-api/) functionality.
New [portal](https://central.sonatype.org/) provides a different approach called [deployment bundles](https://central.sonatype.org/publish/publish-portal-api/#uploading-a-deployment-bundle).
As part of publication the portal verifies a set of [requirements](https://central.sonatype.org/publish/requirements/) to which the bundle must conform.
One of them is the check of dependencies.
The issue is that if two repositories are deployed separately and depend on each other the depending one fails validation till referred one is published.
This breaks atomicity of releases.

As an alternative it is possible to build multiple repositories and upload them as a single bundle.
The problem is that the size of the bundle is limited to 1 GB.

The advantage of the new API is that the bundles can be published automatically by an API call.

## Problems with current release process
* Atomic releases are not possible
    * There is dependency between repositories which brekas validation
    * Debezium Server bundle can be very large so we can get near the size limit
* Process runs twice which adds to release execution time
* Human intervention is necessary

## Goal
* Make process compatible with new Maven portal
* Keep the dry run but use it for process testing only, not as a part of release
* Remove the need of human intervention

# Proposed release process

## Assumptions
* Jira is curated
* Two PRs with documentation updates are available in core repository and documentation repository
    * changelog, release notes, list of contributors, ...

## Release pipeline jobs

### Validation
* Documentation PRs exist and contains change for the given version
* Documentation PR is based on the latest `main` in core
* Jira
    * Release exists
    * All issues are `Resolved`/`Closed`
    * All issues have `Component` set
* Validation is skipped for dry run

### Build and release connectors
* Create candidate branch out of core documentation PR
* Create candidate branches for each of non-core connector from `main`
* Perform `release:prepare` and `release:perform` on all of them
    * Deployment bundles are only built but not pushed
* Execute `release:prepare` on Debezium Server, Operator and Platform to verify that they are buildable
* Create and upload aggregate deployment bundle
* Execute smoke test - based on containers built from aggregated bundle
* Publish aggregate deployment
* Merge all candidate branches into `main`
* Merge documentation repository PR

### Build and release Debezium Server
* Create candidate branch from `main`
* Perform `release:prepare` and `release:perform`, bundle is automatically published
* Merge candidate branch into `main`

### Build and release Debezium Operator
Same as Debezium Server

### Build and release Debezium Platform
Same as Debezium Server

### Docker images
* Update Dockerfiles to point to the new artifacts
* Build and push images

### Deploy Helm charts
* Keep current build
