# Universal Transaction Buffer

## Motivation

Most Debezium connectors are provided transaction log changes for committed transactions, meaning that the connector can immediately dispatch changes safely.
However, for some connectors like Oracle and iSeries, the transaction logs contain a mix of committed, uncommitted, and rolled back changes.
In these cases, there is not enough information at the start of a transaction to know whether its committed or if it's going to be discarded until Debezium reaches the commit or rollback markers.
Therefore, such connectors need a way to buffer transactions in a memory efficient way.

## Goals

* Design a solution that is generic enough to support Oracle and iSeries 
* Minimize heap usage, relying more heavily on off-heap and disk persistence
* Easily decouple the transaction log read versus change event dispatch

## Options

There are a variety of options we could consider as the basis for such a buffer, including but not limited to solutions such as:

* Infinispan
* Ehcache
* Redis
* Chronicle

In the change data capture world, the database vendor-specific APIs used for reading transaction logs can introduce a certain uncontrollable amount of latency.
The Debezium community often demand as close to real-time latency as possible, so introducing any solution that exacerbates latency can be problematic.
While each of these options provide a wide variety of features, it's critical that performance and efficiency be top-of-mind.

## Requirements

* Serialize a transaction event efficiently to some data store
* Easily playback a transaction's event payloads in chronological order when a commit is observed

## Proposal - Chronicle

The [Chronicle Software](https://github.com/OpenHFT) organization provides two unique solutions, one called [Chronicle Queue](https://github.com/OpenHFT/Chronicle-Queue) and the other [Chronicle Map](https://github.com/OpenHFT/Chronicle-Map).
When comparing the requirements with the two implementations, I believe that the queue solution is a better fit for our needs.

Chronicle queue is a disk-based persistent commit log, which works similarly to Kafka.
A producer (aka `ExcerptAppender`) writes a document to the queue, and consumers (aka `ExcerptTailer`) consumes changes from the queue.

In addition, the queue framework supports multi-producer / multi-consumer patterns out of the box.
This means we could easily decouple transaction log reading from event parsing and dispatch.

### Transaction Reader

During the streaming phase, we would start a secondary thread that performs the transaction log reading operations.
This thread would use the database API to gather all the events, and push the data onto the queue.
Data is appended to the queue using the `ExcertAppender`, like as follows:

```java
final ChronicleQueue queue = SingleChronicleQueueBuilder.single(Testing.Files.datadir()).build();
try (ExcerptAppender appender = queue.createAppender()) {
    try (DocumentContext context = appender.writeDocument()) {
        context.wire()
            .write("xid").writeString(transactionId)
            .write("type").writeInt(eventType)
            ...;
    }
} 
```

Conceptually, each event would have a defined structure of fields and values that would be appended.
The queue API supports writing boolean, byte, char, short, int, long, float, double, or string data types.
In addition, you can also write a series of bytes using compression for smaller storage requirements, which would be great for things like clob, blob, and perhaps string data types.

In pseudo code, the transaction reader thread might look something like this:

```java
executor.submit(() -> {
    while (context.isRunning()) {
        final ResultSet changes = fetchChangesFromSourceDatabase();
        while (changes.next()) {
            try (DocumentContext dc = queue.getDocumentContext()) {
                WireOut wire = dc.wire()
                        .write("xid").writeString(changes.getString(TRANSACTION_ID))
                        .wire("type").writeInt(changes.getInt(EVENT_TYPE));
                
                switch (EventType.from(changes.getInt(EVENT_TYPE))) {
                    INSERT, UPDATE, DELETE -> wire("sql").writeString(changes.getString(REDO_SQL));
                    SELECT_LOB_LOCATOR -> ...;
                    LOB_WRITE -> ...;
                    XML_BEGIN -> ...;
                    ...
                }   
            }
        }
    }
});
```

Note: The above code shows the Chronicle API to illustrate its simplicity. Ideally we would prefer to place this behind some abstraction.

The Chronicle Queue API also supports writing objects that extend `SelfDescribingMarshallable`, allowing for a more object-oriented driven approach.
In this way, you would choose to deserialize each JDBC row into an object representation and then write that to the queue instead.

When it comes to read position offset management, there are two ways the reader thread could manage this:

* Position state is transient. Upon connector restart, the queue files are removed from disk, and reading resumed from the connector offset position (like today).
* Position state is persistent, and upon restart, the resume position is based on the last read position the thread captured. If we maintain a second offset value for the reader position, we could choose to fallback to the offset position like we use today if there are no queue files persisted on disk, or resume from the last read position if queue files do. This provides the most efficient management and integration between relying on the persisted queues versus re-reading data that may no longer be available.

### Queue dispatcher

The queue dispatcher would be a loop that runs inside the main streaming thread.
This loop is designed to read the events pushed onto the queue:

1. When a `START` / `BEGIN` event is observed, the queue's position is registered in a heap-driven map based on the transaction identifier to track active transactions.
2. When a `ROLLBACK` event is observed, the transaction entry is removed from the heap-driven map of active transactions.
3. When a `COMMIT` event is observed, the streaming loop then iterates all transaction changes from the queue, creating change events, and removes the transaction from the active transaction map. It would do this by using a secondary `ExcerptTailer` instance to read between the transaction start position and the commit position, dispatching events that have the same XID as the commit event just read.

The dispatcher loop would use a `ExcerptTailer` to listen to changes on the queue, as shown here:

```java
try (ExcerptTailer tailer = getQueue().createTailer()) {
    while (context.isRunning()) {
        try (DocumentContext dc = tailer.readingDocument()) {
            if (!dc.isPresent()){
                pauseWhileNoDataIsAvailable();
                continue;
            }
            
            // Read the current queue offset, useful for storing the start offset for transactions
            // and defining the upper boundary for commits when 
            final long queueIndex = dc.index();
            
            // Assuming each event is always serialized with a tranasction id / event type tuple
            final String xid = dc.wire().read("xid").readString();
            final int eventType = dc.wire().read("type").readInt();
        
            switch (eventType) {
                BEGIN -> registerActiveTransaction(xid, queueIndex);
                ROLLBACK -> unregisterActiveTransaction(xid);
                COMMIT -> replayTransactionEvents(xid, getTransactionStartIndex(xid), queueIndex);
            }
        }
    }    
}
```

Note: The above code shows the Chronicle API to illustrate its simplicity. Ideally we would prefer to place this behind some abstraction.

### Queue cleanup

The Chronicle Queue API uses `.cp4` files on disk to persist the commit log state.
The logs can be configured with rolling strategies that support hourly, daily, or weekly rolling.

When creating the queue, a `StoreFileListener` implementation can be attached to the queue instance that will be notified when a new `.cp4` file is added or when files are no longer used.
When the connector task's offset commit callback fires, we can provide the most up-to-date offset information to the listener, which can use this information to safely remove `.cp4` files that are no longer needed.
This provides full lifecycle management of the queue's persistence based on the offsets written to Kafka, which ties nicely into storing a secondary position in the offsets for the database reader thread, so that the queue can be reused across connector restarts.

Without adding a `StoreFileListener`, the queue files would need to be removed upon startup before creating the queue, or if left on disk, they'll be retained indefinitely.

### Benefits

The connector utilizes a fast commit log, which [performs faster than Kafka](https://github.com/OpenHFT/Chronicle-Queue#chronicle-queue-vs-kafka).
We separate the transaction read loop from the transaction dispatch loop, providing higher throughput capabilities than what we do today.

This solution also avoids utilizing heavyweight heap or off-heap storage, keeping the connector's overall footprint low.
During the transaction reader loop, the JDBC result set is serialized into the queue.
During the dispatch loop, the connector reads from the queue's log, much like a Kafka consumer, emitting events in chronological read order, without needing to maintain the entire transaction in memory.

Finally, Oracle's new unbuffered implementation is designed based on the premise of reading events and dispatching them immediately whereas the buffered implementation operates quite a bit differently.
By using Chronicle in this case and replacing Infinispan and Ehcache with this approach, the dispatch handling code can be reused across the buffered and unbuffered implementations.
That's because the only difference is what is responsible for feeding the changes into the immediate dispatch loop.
This reduces technical debt of the Oracle connector, reduces the complexity and wide dependency list, for a simple trade-off of disk persistence, which is most likely being used by Infinispan and Ehcache even today already.
