# Quarkus Debezium Extension

Quarkus Debezium Extension should provide a simple way to receive data source events inside a Quarkus Native Application and apply some logic to them (see image) thanks to the engine and an appropriate connector.

![](./DDD-12/dbz-emb.png)

In order to be able to receive those events from the data-source, the actual way to achieve it is to import the `debezium-engine` and a connector like in this way:

```xml
<dependency>
    <groupId>io.debezium</groupId>
    <artifactId>debezium-embedded</artifactId>
    <version>${version.debezium}</version>
</dependency>
<dependency>
    <groupId>io.debezium</groupId>
    <artifactId>debezium-connector-mysql</artifactId>
    <version>${version.debezium}</version>
</dependency>
```
This approach doesn't work out-of-the-box in situations in which [we want to build a native image of the application](https://debezium.io/blog/2025/03/12/superfast-debezium/).

## 1. Two dependency (Debezium quarkus engine and connector separate)
The first solution is similar to the actual way to use it inside an application beside the fact that the dependencies are wrapped into quarkus extensions like:

```xml
<dependency>
    <groupId>io.debezium</groupId>
    <artifactId>debezium-quarkus-embedded</artifactId>
    <version>${version.debezium}</version>
</dependency>
<dependency>
    <groupId>io.debezium</groupId>
    <artifactId>debezium-quarkus-connector-mysql</artifactId>
    <version>${version.debezium}</version>
</dependency>
```

![](./DDD-12/s1-dbz-lib.png)

## 2. One dependency (Debezium quarkus engine with Quarkus connector)
The second solution propose a unique dependency that contains the `engine` and the `connector` like in this way:

```xml
<dependency>
    <groupId>io.debezium</groupId>
    <artifactId>debezium-quarkus-mysql</artifactId>
    <version>${version.debezium}</version>
</dependency>
```

the configuration property delegated to define the connector class should be unavailable and already defined inside the extension:
```txt
connector.class=io.debezium.connector.mysql.MySqlConnector
```

![](./DDD-12/s2-dbz-lib.png)

## Quarkus Debezium Extension configuration
The extension should be configurable like the usual one but with some easy-to-use interface:

- `Debezium Listener`
- `Custom Debezium Converter`

### Quarkus  Debezium Listener

It should be available the possibility to listen events (`INSERT, UPDATE, DELETE...`) from a table like `order`, with a simple annotation like in this way:
```java
import io.debezium.engine.ChangeEvent;
import jakarta.enterprise.context.ApplicationScoped;  


@ApplicationScoped  
class OrderListener {  
  
    @DebeziumListener("order")  
    public void listener(ChangeEvent<String, String> event) {  
        /// some logic to apply 
    }  
}
```

or in batch:

```java
import io.debezium.engine.ChangeEvent;
import jakarta.enterprise.context.ApplicationScoped;  


@ApplicationScoped  
class OrderListener {  
  
    @DebeziumBatchListener("order")  
    public void listener(List<ChangeEvent<String, String>> events) {  
        /// some logic to apply
    }  
}
```

even listen only a certain type of event

```java
import io.debezium.engine.InsertEvent;
import jakarta.enterprise.context.ApplicationScoped;  
import io.debezium.engine.quarkus.Operation.INSERT;

@ApplicationScoped  
class OrderListener {  
  
    @DebeziumBatchListener("order", INSERT)  
    public void listener(List<InsertEvent<String, String>> events) {  
        /// some logic to apply
    }  
}
```

### Custom Debezium Converter
It should be possible to receive data-source events mapped as data classes like:

```java
public record Order(long id, String name, int price) {}
```

```java
import io.debezium.engine.InsertEvent;
import jakarta.enterprise.context.ApplicationScoped;

@ApplicationScoped  
class OrderListener {  
  
    @DebeziumListener("order")  
    public void listener(InsertEvent<String, Order> event) {  
        /// some logic to apply
    }  
}
```

using something similar for the [quarkus kafka library](https://quarkus.io/guides/kafka#jackson-serialization):

```java
package com.acme.order.jackson;

import io.quarkus.debezium.client.serialization.ObjectMapperDeserializer;

public class OrderDeserializer extends ObjectMapperDeserializer<Order> {
    public OrderDeserializer() {
        super(Order.class);
    }
}
```

```properties
xxx.deserializer=com.acme.order.jackson.OrderDeserializer
```

## Considerations
This approach that is inspired by Kafka can be useful for the development of Debezium Server.