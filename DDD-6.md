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


### Custom Resource Proposal
The following is a proposed example of `DebeziumServer` custom resource instance (in this). 

```yaml
apiVersion: debezium.io/v1alpha1
kind: DebeziumServer
metadata: 
    name: my-debezium
spec:
    version: "2.2"
    storage:
      type: persistent 
      claimName: ds-data-pvc
    quarkus:
      log.console.json: false
    format:
      value:
        type: json
        schemas.enable: false
    transforms:
      - name: test
        type: com.example.TestTransform
        predicate: test
        negate: false
        prop: 42
    sink:
      type: kafka
      producer.bootstrap.servers: dbz-kafka-kafka-bootstrap:9092
      producer.key.serializer: org.apache.kafka.common.serialization.StringSerializer
      producer.value.serializer: org.apache.kafka.common.serialization.StringSerializer
    source:
      class: io.debezium.connector.mongodb.MongoDbConnector
      mongodb.connection.string: mongodb://debezium:dbz@mongo.debezium.svc.cluster.local:27017/?replicaSet=rs0
      database.history: io.debezium.relational.history.FileDatabaseHistory
      tasks.max: 1
      topic.prefix: demo
      database.include.list: inventory
      collection.include.list: inventory.customers
      offset.storage.file.filename: /debezium/data/offsets.dat
      offset.flush.interval.ms: 0 
Status:
    conditions:
        Status: True
        Type: Ready
        Message: Server my-debezium is operational 
```


## Open Questions

1) Do we need a service? What for?
2) How to handle "Templating" to allow customization of deplyments outside of the scope allowed by the operator 
    - This is needed due to reconciliation (JOSDK doesn't allow merge reconciliations)
    -  It also solves a problem of different requirements between sinks (e.g. only some require environment variables)
3) Dynamic vs Static cnfiguration typing
    - Per sink type configuration with specific properties
    - Single sink configurationion with arbitrary properties in form of `Map<String, Object>`
4) Per namespace / all namespaces / both?




