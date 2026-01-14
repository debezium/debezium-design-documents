# Quarkus Debezium Cache/Search Invalidation

Second‑level cache (L2C) and Search index can boost applications but becomes stale when database changes bypass the ORM without a way to notify applications of the changes. Debezium can capture those external changes from the DB transaction log and trigger near‑real‑time evictions of affected cache entries or a reindex. Debezium Extensions for Quarkus inside a Quarkus app allows registering a `@Capturing` method that can update the corresponding entity on CDC events. We propose a new Quarkus Extension that automatically creates the handlers for refreshing session-based data like hibernate L2C and search.

## Module Organization

A developer should import the extension and the relative datasource for cache invalidation:

```xml
<dependency>
    <groupId>io.debezium</groupId>
    <artifactId>debezium-quarkus-hibernate-cache</artifactId>
    <version>${version.debezium}</version>
</dependency>
<dependency>
    <groupId>io.debezium</groupId>
    <artifactId>debezium-quarkus-postgres</artifactId>
    <version>${version.debezium}</version>
</dependency>
```

and in this way for reindex:

```xml
<dependency>
    <groupId>io.debezium</groupId>
    <artifactId>debezium-quarkus-hibernate-search-reindex</artifactId>
    <version>${version.debezium}</version>
</dependency>
<dependency>
    <groupId>io.debezium</groupId>
    <artifactId>debezium-quarkus-postgres</artifactId>
    <version>${version.debezium}</version>
</dependency>
```

## Extension Configuration

Actually the minimum configuration for the engine extension is the following:


```properties
quarkus.debezium.offset.storage=org.apache.kafka.connect.storage.MemoryOffsetBackingStore
quarkus.debezium.name=native
quarkus.debezium.topic.prefix=native
quarkus.debezium.plugin.name=pgoutput
quarkus.debezium.snapshot.mode=initial

# datasource configuration
quarkus.datasource.db-kind=postgresql
quarkus.datasource.username=<your username>
quarkus.datasource.password=<your password>
quarkus.datasource.jdbc.url=jdbc:postgresql://localhost:5432/hibernate_orm_test
quarkus.datasource.jdbc.max-size=16
```

With the cache-invalidation extension, the configuration should be reduced in the following:

```properties
quarkus.debezium.offset.storage=org.apache.kafka.connect.storage.MemoryOffsetBackingStore

quarkus.datasource.db-kind=postgresql
quarkus.datasource.username=<your username>
quarkus.datasource.password=<your password>
quarkus.datasource.jdbc.url=jdbc:postgresql://localhost:5432/hibernate_orm_test
quarkus.datasource.jdbc.max-size=16
```

which are the datasource common configuration for agroal and the offset storage. The other configuration should be:

- no snapshot
- in memory Schema Changes
- all configuration defaulted and named `invalidation`

## Behaviour

### Hibernate Cache Eviction

The extension should scan for JPA/Hibernate metamodel at startup and identify all the entities that are under L2C (`Cacheable`) and register a generic `@Capturing` handler delegated to evict the cache. Here a snippet that explain the idea:

```java
    @ApplicationScoped
    class CacheInvalidationHandler {
        @PersistenceUnit 
        private EntityManagerFactory emf;

        private DebeziumCacheRegistry registry;

        @Capturing()
        public void evict(CapturingEvent<SourceRecord> record) {
            if (registry.get(record.topic())) {
                Long itemId = ((Struct) record.key()).getInt64(registry.get(record.topic()).key());
                Struct payload = (Struct) record.value();
                Operation op = Operation.forCode(payload.getString("op"));
                if (op == Operation.UPDATE || op == Operation.DELETE) {
                    emf.getCache().evict(Item.class, itemId);
                }
            }
        }

    }

```

Hibernate can load the same entity in many different shapes depending on how it was fetched:

- with a dynamic fetch graph
- with a JOIN FETCH
- with some associations eagerly loaded
- with others left lazy

If we refresh the entity without that context, we'd likely evict it and only reload a subset of the original cached data.

#### Solutions

Evicting a whole region forces Hibernate to rebuild all cached entries from their next natural load, avoiding shape inconsistencies.

```java
sessionFactory.getCache()
    .evictEntityRegion(Item.class);
```

### Hibernate Search Explicit Reindex

The extension should scan for JPA/Hibernate metamodel at startup and identify all the entities that are under index (`Indexed`) and register a generic `@Capturing` handler delegated to re-index the cache.

```java
    @ApplicationScoped
    class SearchReindexHandler {

        @PersistenceContext
        private EntityManager em;

        private DebeziumIndexRegistry registry;

        @Capturing()
        public void evict(CapturingEvent<SourceRecord> record) {
            SearchSession searchSession = Search.session(em);

            if (registry.get(record.topic())) {
                Long itemId = ((Struct) record.key()).getInt64(registry.get(record.topic()).key());
                Struct payload = (Struct) record.value();
                Operation op = Operation.forCode(payload.getString("op"));

                if (op == Operation.INSERT || op == Operation.UPDATE || op == Operation.DELETE) {
                    MyEntity entity = entityManager.find(MyEntity.class, itemId);
                    searchSession.indexingPlan().addOrUpdate(entity);

                }
            }
        }

    }

```


## References

- [Automating Cache Invalidation With Change Data Capture](https://debezium.io/blog/2018/12/05/automating-cache-invalidation-with-change-data-capture/)