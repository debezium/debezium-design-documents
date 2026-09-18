# DDD-6: Debezium Server on K8 (iteration 0)

## Motivation
Currently it is challanging to use Debezium Server within Kubernetes environment as user needs to manage all required k8 resources manually.
This Debezium Design Document  discusses the design of a kubernetes oeprator which would simplify the deployment of Debezium Server into this environment.


## Alternative solutions
An obvious alternative to creating our own operator is a providin a Helm chart. Helm charts are esentially a set of templates with certain level of dynamic logic. 
Considering the amount of different sinks and the overall ocmplexity of DS configuration, this template logic might gradually become too complicated and poorly maintainable. 

An argument supporting the use of Helm chart over operator could be the relative simplicity of the development. However latest versions of Operator SDK implementations are providing  high levels of abstractin which greatly simplifies the development of operators with basic functionality. 
Perhaps more imoportant argument are the deliverables of each solutions. While Helm charts require only descriptors, with operator we wil have to also support the operator application and container. 


Kubernetes operator provides significatly more flexibility and space for further functionality growth. For these reasons the further parts of this DDD are discussing the desing of future Debezium operator.

## Operator Design & Requirements

## Scope & Focus
Do we want to create an operator for Debezium server specifically or more general Debezium operator?
The opeator could potentially support

- Debezium connector for DS
- Debezium connector for KafkaConnect (can we somehow reuse UI backend?)
- Debezium connector for Strimzi? Does it make sense to translate our resource to Strimzi?
- Debezium connectors for DS via strimzi resources
- Debezium UI deployment


Initially will start with support for Debezium Server. However the resources should be designed with the broader scope in mind from beginning 


### Operator SDK options

The two primary options to consider her are Golang's Operator SDK (Golang) or Java Operator Framework (as Quarkus extension) . In case of a java-centered team such as our it makes sense to go with JODSK --  especially since JOSDK has quite active development. 


## Custom Resource Proposal

### DebeziumServerSpec Reference
```yaml
spec:
  version: String
  image: String # exclusive with version
  storage:
    type: persistent | ephemeral  # enum
    claimName: String # only valid and required for persistent
  runtime:
    env: EnvFromSource array # https://kubernetes.io/docs/reference/generated/kubernetes-api/v1.23/#envfromsource-v1-core
    volumes: Volume array # https://kubernetes.io/docs/reference/generated/kubernetes-api/v1.23/#volume-v1-core
  quarkus:
  # quarkus properties 
  format:
    value:
      type: String
      # other format properties
    key:
      type: String
    header:
      type: String
  transforms:
    - type: String
      predicate: String
      negate: Boolean
      config:
        # other transformation properties
  predicates:
    name:
      type: String
      config:
        # other preticate properties
  sink:
    type: String
    config:
      # other sink properties
  source:
    class: String
    config:
      # other source connector properties
```

Resource status will be an array of `Condition`. 
### DebeziumServerStatus Reference
```yaml
status:
    conditions: # Condition array
        - status: String
          type: String
          message: String

```
For start only the `Ready` condition is defined
```
status: True | False
type: Ready
```

### Example Custom Resource Instance
The following is an of the proprosed `DebeziumServer` custom resource. 

```yaml
apiVersion: debezium.io/v1alpha1
kind: DebeziumServer
metadata:
  name: my-debezium
spec:
  image: quay.io/jcechace/debezium-server:latest
  storage:
    type: persistent
    claimName: ds-data-pvc
  runtime:
    env:
      - configMapRef:
          name: ds-env
      - secretRef:
          name: ds-secret-env
    volumes:
      - name: extra
        configMap:
          name: ds-mounts
      - name: extra-secret
        secret:
          secretName: ds-secret-mounts
  quarkus:
    log.console.json: false
    kubernetes-config.enabled: true # enable access to config maps and secrets
    kubernetes-config.secrets: ds-creds # use ds-creds secret in the same namespace
  format:
    value:
      type: json
      config:
        schemas.enable: false
    key:
      type: json
  transforms:
    - name: test
      type: com.example.TestTransform
      predicate: test
      negate: false
      config:
        prop: 42
  predicates:
    - type: com.example.TestPredicate
      config:
        prop: 42
  sink:
    type: kafka
    config:
      producer.bootstrap.servers: dbz-kafka-kafka-bootstrap:9092
      producer.key.serializer: org.apache.kafka.common.serialization.StringSerializer
      producer.value.serializer: org.apache.kafka.common.serialization.StringSerializer
  source:
    class: io.debezium.connector.mongodb.MongoDbConnector
    mongodb.connection.string: mongodb://mongo.debezium.svc.cluster.local:27017/?replicaSet=rs0
    mongodb.username: ${username} # references key from secret
    mognodb.password: ${password}
    database.history: io.debezium.relational.history.FileDatabaseHistory
    tasks.max: 1
    topic.prefix: demo
    database.include.list: inventory
    collection.include.list: inventory.customers
    offset.storage.file.filename: /debezium/data/offsets.dat
    offset.flush.interval.ms: 0
status:
  conditions:
    - status: True
      type: Ready
      message: Server my-debezium is ready 
```


### Handling sensitive configuration
The operator needs to handle senstivie data such as connection credentials securely. 