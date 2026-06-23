# Debezium Sink Instrumentation

Actually In debezium server the code necessary to sink change data captures was the following:

```java
@Named("my-sink")
@Dependent
public class MySinkChangeConsumer extends BaseChangeConsumer implements DebeziumEngine.ChangeConsumer<ChangeEvent<Object, Object>> {

    @PostConstruct
    void start() {
        // crate resources
    }

    @PreDestroy
    void stop() {
       // destroy resources
    }

    @Override
    public void handleBatch(final List<ChangeEvent<Object, Object>> records,
                            final RecordCommitter<ChangeEvent<Object, Object>> committer)
            throws InterruptedException {

        for (ChangeEvent<Object, Object> record : records) {
                committer.markProcessed(record);
        }

        committer.markBatchFinished();
    }

}
```

Now with the [quarkus extensions](https://github.com/debezium/dbz/issues/1484) you can do the same with the following:

```java
@ApplicationScoped
public class CaptureHandler {

    @Capturing
    public void capture(CapturingEvents<BatchEvent> events) {
        events.records().forEach(event -> event.commit());
    }

}
```

in the new implementation, the following features are missed:

- configuration definition
- pre-engine sink instrumentation
- post-engine sink instrumentation

The features should be available in the extensions and in debezium server.

## Debezium Sink Configuration Mapping

Based on [smallrye-config](https://smallrye.io/smallrye-config/3.11.1/config/mappings/), a developer should be able to define mapping rules for the Sink that are compatible with `quarkus` and `debezium server`. It should be available an annotation `DebeziumSinkConfiguration` with a prefix value that define the root mapping of the configuration elements. Based on the case (debezium server or general quarkus extension) the configuration is prefixed with `debezium.sink` or `quarkus.debezium.sink` like:

```java
package io.debezium.sink.kafka.configuration;

import io.debezium.quarkus.spi.DebeziumSinkConfiguration;
import io.smallrye.config.WithName;


@DebeziumSinkConfiguration("kafka")
interface DebeziumKafkaConfiguration {

    @WithName("wait.message.delivery.timeout.ms")
    String waitMessageDeiveryTimeoutMs;
    
    // [...]
}
```

the result mapping:

- [Debezium Server] `debezium.sink.kafka.wait.message.delivery.timeout.ms=1`
- [Quarkus Extension] `quarkus.debezium.sink.kafka.wait.message.delivery.timeout.ms=1`

## Sink Instrumentation

To work properly, the sink must be able to create objects or resources before the engine starts which must be available inside the runtime. Very similar to [CDI producers](https://jakarta.ee/learn/docs/jakartaee-tutorial/current/cdi/cdi-basic/cdi-basic.html#_injecting_objects_by_using_producer_methods), a developer should be able to create resources that are inside the debezium context:

```java
@DebeziumContext
public KafkaSinkFactory {
    private final DebeziumKafkaConfiguration configuration;

    public KafkaSinkFactory(DebeziumKafkaConfiguration configuration) {
       this.configuration = configuration; 
    }

    @BeforeCapturing
    public KafkaProducer create() {
        return new KafkaProducer(configuration);
    }
}
```


In the same way, it must be possible to destroy or close gracefully resources right after the engine stop to work:


```java
@DebeziumContext
public KafkaSinkShutdownHandler {
    private final KafkaProducer kafkaProducer;

    public KafkaSinkShutdownHandler(KafkaProducer kafkaProducer) {
        this.kafkaProducer = kafkaProducer;
    }

    @AfterCapturing
    public void shutdown() {
        try {
            kafkaProducer.close();
        } catch(Exception e) {
            // in some cases...you want a retry strategy
        }
    }
}