# Efficient Heap-based Transaction Buffer

## Motivation

Most Debezium connectors read transaction logs that only expose committed transactions, which allows the connector to dispatch each change as soon as it is read.
However, for connectors like Oracle and Informix, the transaction logs contain a mix of committed, uncommitted, and rolled back changes.
In these cases, there is not enough information at the start of a transaction to know whether it will be committed or discarded; that is only known once the commit or rollback arrives at the end of the transaction.
Therefore, these connectors need a way to efficiently buffer in-flight transactions in memory, ideally with ways for users to tune how the buffer behaves.

## Goals

* Design a solution that is generic enough to support different connectors (targeting Informix and Oracle)
* Minimize heap usage, relying on advanced techniques to keep the footprint small

## Requirements

* Transaction events must retain their order
* Serialization overhead should be negligible, keeping throughput comparable with the current map-based approach
* Column values dominate heap requirements, so provide an optional compression pass to store large transactions efficiently, which the user can turn off; see [Compression](#compression)
* User configurable payload chunk size, defaults to 64KB
* User configurable cache growth cap, defaults to 65,536 (64K) slots; see [Growth cap](#growth-cap)
* Store 100M reasonably sized events in heap in under 50GB
* An option that can ship freely with both upstream and downstream builds of Debezium

## Proposal

Both Oracle and Informix use a similar approach today.
There is a _transaction_ object that holds metadata about the transaction, such as its starting position, start time, and other details.
In addition, either adjacent to or within the transaction, there is a collection of _event_ objects, one for each change seen for that transaction.

There is nothing inherently wrong with this approach, but it is highly inefficient at scale.
Every event is a small object that references several more small objects (its position, row id, column values, and so on), and Java pays a header, alignment padding, and a reference for each one.
When a transaction holds millions of events, this bookkeeping costs more heap than the data it describes, and it gives the garbage collector hundreds of millions of objects to trace.

The proposal does not deviate from the broader approach of a transaction that owns its events.
Instead, it focuses on changing how **all the data** is stored within the transaction itself:

* Event metadata is stored in parallel primitive arrays, one array per field, rather than one object per event.
  An event is no longer an object; it is an index into those arrays.
* Everything else an event carries, which for most events is its column values, is serialized into large, fixed-size byte chunks that are shared by many events, rather than being held as individual `String` or `Object` instances.
* A chunk is optionally compressed once it fills, since a full chunk is never written to again.

We will first take a look at a connector-centric flavor, showcasing the internal storage pattern, which is at the heart of the change.
Then we'll follow up with a section that describes the more generalized flavor for all connectors.

### Oracle-centric design

At the heart of the design is a class called `PackedTransaction`, which is the evolution of the classic `Transaction`.
It is presented in pieces below; all the fragments that follow the first listing are members of this one class.

#### Transaction state

```java
public class PackedTransaction {
    // Immutable transaction metadata
    final String transactionId;
    final BigInteger startScn; // anchor scn all event scn deltas are based
    final long startTime;
    final String userName;
    final String clientId;
    final int redoThread;

    private int eventCount = 0;
    
    // Per-event metadata, ~59 bytes across all arrays
    private byte[] type = new byte[8];
    private long[] scnDelta = new long[8]; // scn delta +/- from transaction startScn
    private long[] changeTime = new long[8];
    private long[] rsIdBlockSeq = new long[8]; // "0x000abc.00000def" packed, 56 bits
    private short[] rsIdOffset = new short[8]; // ".0010"
    private long[] rowIdHi = new long[8]; // RowIdCodec.Packed hi bits
    private long[] rowIdLo = new long[8]; // RowIdCodec.Packed lo bits
    private int[] table = new int[8]; // TableId interning via TableDictionary 
    private int[] payloadChunks = new int[8]; // -1 carries no payload
    private int[] payloadOffset = new int[8];
    private int[] payloadLength = new int[8];
    
    // Memory chunks with serialized event payloads
    private final List<PayloadChunk> chunks = new ArrayList<>();

    // ... methods shown in the sections that follow
}
```

The state falls into three groups.

The first group is the immutable transaction metadata, which is the same information the connector tracks about a transaction today.
The one field worth calling out is `startScn`, which doubles as the anchor that every event's SCN is stored relative to; more on this under [SCN deltas](#scn-deltas).

The second group is the per-event metadata.
Rather than allocating an object per event, each metadata field has its own array, and the event's id (its position in the transaction, `0` to `eventCount - 1`) is the index into every one of them.
Reading event `n` means reading `type[n]`, `scnDelta[n]`, and so on.
Because the id is the insertion position, event order is retained without any additional structure.

Each field is packed into the smallest primitive that represents it losslessly:

| Array | Type | Bytes per event | Contents |
|---|---|---|---|
| `type` | `byte` | 1 | The event type |
| `scnDelta` | `long` | 8 | The event's SCN, as a signed offset from `startScn` |
| `changeTime` | `long` | 8 | The event's change time |
| `rsIdBlockSeq` | `long` | 8 | The leading two segments of the `rs_id`, packed |
| `rsIdOffset` | `short` | 2 | The trailing segment of the `rs_id` |
| `rowIdHi`, `rowIdLo` | `long` | 16 | The `ROWID` as the two halves of `RowIdCodec.Packed` |
| `table` | `int` | 4 | The `TableId`, interned to an integer by `TableDictionary` |
| `payloadChunks` | `int` | 4 | Which chunk holds the event's payload, or `-1` for none |
| `payloadOffset` | `int` | 4 | Where the payload starts within that chunk |
| `payloadLength` | `int` | 4 | How many bytes the payload occupies |
| Total | | 59 | |

Two of these replace objects that are surprisingly expensive in the current layout.
The `rs_id` is a fixed-format string of three hexadecimal segments of 6, 8, and 4 digits, such as `0x000abc.00000def.0010`.
The first two segments are 56 bits and pack into a single `long`, and the third is 16 bits and fits a `short`, so the value is kept as 10 bytes of primitives rather than as a `String` with its backing byte array.
The `TableId` repeats across nearly every event in a transaction, so `TableDictionary` assigns each distinct table an integer once, and events store only that integer.

The third group is the list of `PayloadChunk` instances that hold each event's serialized _payload_.
The payload is everything about an event that is not in the metadata arrays.
For a DML event that is its old and new column values, but other event types carry other things, which is covered under [Payload encoding](#payload-encoding).
The last three metadata arrays are what tie an event to its payload: a chunk index, an offset, and a length.
Events that carry no payload record a chunk index of `-1` and consume no chunk space.

#### Appending events

```java
/**
 * Appends an event read from JDBC to this transaction.
 * 
 * @param event the parsed event, never {@code null}
 * @param tableIdDict the table id dictionary for interning, never {@code null}
 * @return the index within the immutable event metadata arrays
 */
int append(LogMinerEvent event, TableDictionary tableIdDict) {
    ensureCapacity(eventCount + 1);
    
    // Compute eventId and write event data into per-event metadata arrays by eventId
    final int eventId = eventCount++;
    
    // Serializes event data, similar to Ehcache Serdes used today, minus metadata fields
    final byte[] payload = OraclePayloadCodec.encode(event);
    if (payload == null) {
        // Events like INTERNAL, ROLLBACK TO SAVEPOINT, etc. have no payload
        payloadChunks[eventId] = -1;
    }
    else {
        final PayloadChunk chunk = writableChunk(payload.length);
        payloadChunks[eventId] = chunks.size() - 1; // already sized earlier
        payloadOffset[eventId] = chunk.append(payload);
        payloadLength[eventId] = payload.length;
    }
    return eventId;
}

int eventCount() {
    return eventCount;
}
```

Appending an event has two halves.

The input is the `LogMinerEvent` that the connector already builds for every row it reads, such as a `DmlEvent` once the DML parser has produced the column values, or a `LobWriteEvent` for a LOB fragment.
Nothing changes about how events are parsed; what changes is that the event object is no longer retained.
It is unpacked into the transaction and becomes garbage as soon as `append` returns.

The metadata half claims the next event id and writes the fields common to every `LogMinerEvent` into the metadata arrays at that index.
Those individual writes are elided from the listing, as each is a single array store, with `packScn` and `RowIdCodec` handling the two fields that need conversion.

The payload half only applies to events that carry something beyond their metadata.
`OraclePayloadCodec` serializes it into a single byte array, in the same spirit as the Serdes used by the Ehcache buffer today, except that the metadata fields are excluded because they already live in the arrays.
The encoded bytes are copied to the tail of the current writable chunk, and the event records where they landed.
The temporary `payload` array is also garbage as soon as `append` returns, so the only long-lived cost of an event's payload is the bytes it occupies within the chunk.

The supporting helpers are straightforward:

```java
private PayloadChunk writableChunk(int bytes) {
    if (chunks.isEmpty() || !chunks.get(chunks.size() - 1).fits(bytes)) {
        if (compressed && !chunks.isEmpty()) {
            // Seal on fill: about 0.1 ms per 64 KB, paid in the read loop
            chunks.get(chunks.size() - 1).seal();
        }
        // An event larger than a chunk gets a chunk of its own, sized to fit
        chunks.add(new PayloadChunk(Math.max(PayloadChunk.CAPACITY, bytes)));
    }
    return chunks.get(chunks.size() - 1);
}

private void ensureCapacity(int needed) {
    if (needed > type.length) {
        // Doubles until the step reaches growthMax, then grows by growthMax each time
        final int size = Math.max(needed, type.length + Math.min(type.length, growthMax));
        type = Arrays.copyOf(type, size);
        scnDelta = Arrays.copyOf(scnDelta, size);
        // ... every other array the same way
    }
}
```

`writableChunk` returns the chunk at the tail of the list, starting a new one when the encoded event does not fit in the space that remains.
An event's payload is never split across chunks, which is what allows a single offset and length to describe it.
The one case that needs care is an event whose encoded payload exceeds the chunk capacity, such as a wide row of large character columns, since it would not fit an empty chunk either.
Such an event is given a chunk of its own, sized to fit exactly.
That chunk is full as soon as it is written, so the next event seals it and starts a regular chunk, and nothing else in the design needs to know that chunk sizes can differ.
When a chunk is replaced, it is sealed first if compression is enabled, which is the point where compression happens; see [Payload chunks](#payload-chunks) and [Compression](#compression).

`ensureCapacity` grows all the metadata arrays together, so that a single capacity check covers every field.
A transaction starts at 8 slots so that the many small transactions a connector sees stay small.
How the arrays grow from there is governed by the growth cap.

#### Growth cap

Doubling is the usual way to grow an array, but it is a poor fit for a transaction that becomes very large.
Each doubling allocates as much again as the transaction already holds, so a large transaction that has just doubled has close to half of every array unused, and that waste is held until the transaction ends.

The growth cap bounds this.
It is a user configurable property, e.g. `transaction.buffer.growth.max`, that defaults to 65,536 (64K) and is expressed in event slots.
In the listing above, `growthMax` is the configured value, handed to the transaction when it is created.
The arrays double while the step is below the cap, and once the cap is reached they only ever grow by the cap.
With the default, the capacity runs 8, 16, 32, and so on up to 65,536, and from there 131,072, 196,608, 262,144, adding 65,536 each time.

Because the arrays double until they reach the cap, the unused slots in a transaction never exceed the smaller of its own size and the cap.
This has two consequences:

* Small transactions are unaffected by the cap, however high it is set.
  A transaction of 20 events holds 32 slots whether the cap is 64 or 65,536.
* Only a transaction that has already grown past the cap can carry a full cap of unused slots, and by then it is a small fraction of what the transaction holds.
  At 59 bytes per slot the default cap is about 3.8 MB, against about 118 MB of metadata for a transaction of 2 million events, or roughly 3 percent.

The cap trades memory for copying.
Every growth step copies each array in full, so while doubling copies an amount proportional to the transaction's size, growing by a fixed step copies an amount proportional to the square of it.
A low cap is therefore expensive for exactly the transactions it is meant to help.
For a single transaction of 2 million events, at 59 bytes per slot:

| Cap in slots | Most unused per transaction | Growth steps | Total copied |
|---|---|---|---|
| 64 | about 3.7 KB | about 31,000 | about 1.8 TB |
| 256 | about 15 KB | about 7,800 | about 460 GB |
| 4,096 | about 236 KB | about 500 | about 29 GB |
| 65,536 (default) | about 3.8 MB | 43 | about 1.8 GB |
| No cap, doubling | about 62 MB | 18 | about 0.12 GB |

The steps matter beyond the bytes copied.
Once a transaction is large, each of its arrays is several megabytes, so every growth step is a set of large contiguous allocations that leaves the previous arrays behind as garbage, which G1 handles as humongous objects.

The default of 65,536 keeps the unused space of a large transaction to a few percent while holding the copy work within an order of magnitude or so of doubling.
Lowering it saves very little memory and costs a great deal of copying, so the property is mainly useful for raising the cap further where extremely large transactions are routine.

#### SCN deltas

```java
private long packScn(Scn value) {
    // Signed. The magnitude is bounded by the SCN span the transaction's rows cover, which is
    // its lifetime times the SCN rate; longValueExact throws only past 63 bits, i.e. corruption.
    return value.asBigInteger().subtract(startScn).longValueExact();
}

private Scn unpackScn(int id) {
    // Base plus delta: one BigInteger add per emitted event, none for events that are skipped
    return new Scn(startScn.add(BigInteger.valueOf(scnDelta[id])));
}
```

An `Scn` is a 32 byte object per event in the current layout.
Storing the raw SCN as a `long` is not an option, because the connector supports SCN values up to 2^64 - 1, which a signed `long` cannot hold, and an SCN only ever increases, so a database that crosses that width stays there permanently.
Every event in a transaction has an SCN close to the transaction's own starting SCN, however, so the packed layout stores only the difference as a `long` and keeps the single `BigInteger` anchor on the transaction.
That is 8 bytes per event for any SCN width, with no change of mode when the width is crossed.

The delta is signed because the rows of a transaction are not SCN-ordered: they can move both forward and backward relative to `startScn`.
Its magnitude is bounded by how much SCN range the transaction spans, which is a function of how long the transaction lives and how quickly the database advances the SCN, so a delta that does not fit in 63 bits cannot occur in practice.
`longValueExact` turns that impossible case into an exception rather than a silently wrong position.

The full `Scn` is only rebuilt when an event is emitted at commit time, so rolled back transactions and events never pay for it.

#### Truncation

```java
/**
 * Oracle requires during read operations to truncate a transaction back to a lower position because
 * a block needs to be re-read due to an exception. 
 * 
 * @param newSize the new expected event size
 */
void truncateTo(int newSize) {
    if (newSize >= eventCount) {
        return;
    }
    
    int lastKept = newSize - 1;
    while (lastKept >= 0 && payloadChunks[lastKept] < 0) {
        lastKept--;
    }
    if (lastKept < 0) {
        chunks.clear();
    }
    else {
        final int keepChunk = payloadChunks[lastKept];
        chunks.subList(keepChunk + 1, chunks.size()).clear();
        chunks.get(keepChunk).resetTo(payloadOffset[lastKept] + payloadLength[lastKept]);
    }
    
    eventCount = newSize;
}
```

Oracle needs the ability to discard the tail of a transaction.
When a mining pass fails part-way through, the events it appended cannot be trusted and the same range is mined again, so the transaction must be returned to exactly the state it had before the pass began.
How this is driven is covered under [The transaction cache](#the-transaction-cache).

Because events are append-only and ordered, truncation is cheap and does not touch the events that are kept:

* The metadata arrays need no cleanup.
  Lowering `eventCount` makes the trailing slots unreachable, and later appends overwrite them.
* The payload chunks are trimmed by finding the last retained event that carries a payload, skipping backward over any events without one.
  Every chunk after that event's chunk is dropped, and that event's chunk is reset so that its next write position is the byte immediately after the event's payload.
* If no retained event carries a payload, every chunk is dropped.

#### Metadata visitor

```java
/**
 * A metadata walker visitor pattern for partial rollback pass.
 */
interface MetadataVisitor {
    void visit(int id, byte type, int table, long rowIdHi, long rowIdLo, long rsIdBlockSeq);
}

/**
 * Helper to iterate all event metadata records using the visitor.
 * 
 * @param visitor the visitor to use, should not be {@code null}
 */
void forEachMetadata(MetadataVisitor visitor) {
    for (int id = 0; id < eventCount; id++) {
        visitor.visit(id, type[id], table[id], rowIdHi[id], rowIdLo[id], rsIdBlockSeq[id]);
    }
}
```

Partial rollbacks (`ROLLBACK TO SAVEPOINT`) are resolved at commit time by matching each undo event to the change it reverses.
This is today's `findRolledBackRange` logic expressed over array indices.
The undo events are applied in forward order, and each one searches backward for the change it reverses, so that every search sees the outcome of the undo events before it.
That matching only needs the event type, table, row id, and redo position, all of which are metadata.

It also needs to recognize the undo events themselves, which is a requirement this design places on the event type.
Today, LogMiner reports an undo as an ordinary INSERT, UPDATE, or DELETE row with its rollback flag set, and the connector turns it into a `RollbackToSavepointEvent` that keeps that event type; the two are told apart by their Java class.
There is no Java class per event in the packed layout, so `EventType` is extended with connector-assigned values for undo events, one for each operation being reversed.
The `type` byte is then sufficient on its own to identify both that an event is an undo and what it undoes.

The visitor exposes exactly those fields as primitives, so the rollback pass allocates nothing and never decodes a payload.

#### Payload encoding

The metadata arrays hold the fields that every `LogMinerEvent` has.
What an event carries beyond those depend on its class, and the Oracle connector has a good number of them:

| Event class | Payload |
|---|---|
| `DmlEvent`, `TruncateEvent` | Old and new column values |
| `RedoSqlDmlEvent` | Old and new column values, redo SQL |
| `SelectLobLocatorEvent` | Old and new column values, column name, binary flag |
| `ExtendedStringBeginEvent` | Old and new column values, column name |
| `XmlBeginEvent` | Old and new column values, column name, transaction sequence |
| `LobWriteEvent` | Data, offset, length |
| `XmlWriteEvent` | XML, length |
| `ExtendedStringWriteEvent` | Data |
| `XmlEndEvent` | Transaction sequence |
| `LobEraseEvent`, `RollbackToSavepointEvent`, `LogMinerEvent` | None |

These are the same fields that the per-class `SerdesProvider` implementations of the Ehcache buffer write today, after the common metadata.
`OraclePayloadCodec` is those providers with the common metadata removed, selected by event type:

```java
final class OraclePayloadCodec {
    /**
     * @return the encoded payload, or {@code null} if the event has nothing beyond its metadata
     */
    static byte[] encode(LogMinerEvent event) {
        final PayloadOutput out = new PayloadOutput();
        switch (event.getEventType()) {
            case INSERT, UPDATE, DELETE -> {
                final DmlEvent dml = (DmlEvent) event;
                out.writeObjectArray(dml.getOldValues());
                out.writeObjectArray(dml.getNewValues());
            }
            case SELECT_LOB_LOCATOR -> {
                final SelectLobLocatorEvent locator = (SelectLobLocatorEvent) event;
                out.writeObjectArray(locator.getOldValues());
                out.writeObjectArray(locator.getNewValues());
                out.writeString(locator.getColumnName());
                out.writeBoolean(locator.isBinary());
            }
            case LOB_WRITE -> {
                final LobWriteEvent lobWrite = (LobWriteEvent) event;
                out.writeString(lobWrite.getData());
                out.writeInt(lobWrite.getOffset());
                out.writeInt(lobWrite.getLength());
            }
            // ... the remaining event types that carry a payload
            default -> {
                // Undo events, LOB_ERASE, and the like are metadata only
                return null;
            }
        }
        return out.toByteArray();
    }

    /**
     * @param in the event's payload, or {@code null} if it has none
     */
    static LogMinerEvent decode(EventType type, Scn scn, TableId tableId, String rowId, String rsId, Instant changeTime, PayloadInput in) {
        return switch (type) {
            case INSERT, UPDATE, DELETE -> new DmlEvent(type, scn, tableId, rowId, rsId, changeTime,
                    in.readObjectArray(), in.readObjectArray());
            case SELECT_LOB_LOCATOR -> new SelectLobLocatorEvent(type, scn, tableId, rowId, rsId, changeTime,
                    in.readObjectArray(), in.readObjectArray(), in.readString(), in.readBoolean());
            case LOB_WRITE -> new LobWriteEvent(type, scn, tableId, rowId, rsId, changeTime,
                    in.readString(), in.readInt(), in.readInt());
            // ... the remaining event types
        };
    }
}
```

`PayloadOutput` and `PayloadInput` stand in for the existing `SerializerOutputStream` and `SerializerInputStream`, whose handling of the types found in parsed column values carries over unchanged.

Three things are worth pointing out.

First, the event type is the only discriminator, so it must identify the shape of the payload unambiguously.
That is not quite true of `EventType` today, where the Java class carries part of the distinction: a `DmlEvent`, a `RedoSqlDmlEvent`, and a `RollbackToSavepointEvent` can all be an `UPDATE`.
The Ehcache Serdes work around this by writing the event's fully qualified class name ahead of every event, which is over 50 bytes each time.
Here the distinction moves into `EventType` instead, as described for undo events under [Metadata visitor](#metadata-visitor), which costs nothing, because the `type` byte is stored either way.
`RedoSqlDmlEvent` can be handled the same way, or left to the connector configuration that enables it, since that cannot change during the life of a heap buffer.

Second, `decode` is given the metadata rather than reading it from the payload.
`drain` rebuilds the `Scn`, row id, `rs_id`, and `TableId` from the arrays and passes them in, so that what comes out is the same `LogMinerEvent` that went into `append`, of the same class.
Everything downstream of the buffer, the `TransactionCommitConsumer` in particular, is unaffected by how the event was stored in the meantime.

Third, all of this is connector code.
`PackedTransaction` only ever sees a `byte[]` and a length, which is what allows the storage to be shared with connectors whose events look nothing like Oracle's.

#### Payload chunks

```java
final class PayloadChunk {
    static final int CAPACITY = 64 * 1024; // todo: ideally be configurable
    private static final LZ4Compressor COMPRESSOR = LZ4Factory.fastestInstance().fastCompressor();
    private static final LZ4FastDecompressor DECOMPRESSOR = LZ4Factory.fastestInstance().fastDecompressor();

    private byte[] data;
    private int length; // raw bytes used; kept after sealing so decompress knows the size
    private boolean sealed;

    PayloadChunk(int capacity) {
        data = new byte[capacity];
    }

    boolean fits(int bytes) {
        return !sealed && length + bytes <= data.length;
    }

    int append(byte[] encoded) {
        final int offset = length;
        System.arraycopy(encoded, 0, data, offset, encoded.length);
        length += encoded.length;
        return offset;
    }

    void resetTo(int newLength) {
        reopen();
        length = newLength;
    }

    void seal() {
        if (!sealed) {
            data = COMPRESSOR.compress(data, 0, length);
            sealed = true;
        }
    }

    /** Undo of a truncation into a sealed chunk: at most one per truncated transaction. */
    void reopen() {
        if (sealed) {
            final byte[] raw = new byte[Math.max(CAPACITY, length)];
            DECOMPRESSOR.decompress(data, 0, raw, 0, length);
            data = raw;
            sealed = false;
        }
    }

    byte[] open() {
        return sealed ? DECOMPRESSOR.decompress(data, length) : data;
    }
}
```

A `PayloadChunk` is an append-only byte buffer of a fixed capacity, 64KB by default, that holds the encoded payloads of as many consecutive events as fit.
The capacity is only ever larger for a chunk dedicated to a single oversized event.
Packing many events into one array is where most of the heap saving comes from: the payloads of hundreds of events share a single array header instead of each column value being its own object.

A chunk has a simple lifecycle:

1. **Open.** The chunk accepts appends until an event arrives that does not fit in the remaining space.
2. **Sealed.** When compression is enabled, `PackedTransaction` seals the chunk before starting the next one.
   Sealing compresses the used portion with LZ4 and replaces the 64KB buffer with the compressed bytes, releasing the original buffer.
   A sealed chunk is not written to again, truncation aside, so compression is a one-time cost of about 0.1 ms per chunk, paid in the read loop.
3. **Read.** `open()` returns the raw bytes, decompressing if the chunk is sealed, so that events can be decoded at commit time.

The raw `length` is retained after sealing because the LZ4 fast decompressor must be told the exact size of the output it is restoring.

Operating at chunk granularity is deliberate.
Compressing each event individually would give LZ4 too little input to find redundancy, while a chunk holds many rows of the same tables, whose values tend to repeat.
It also bounds the cost of reading: events are drained in order, so a transaction can be decompressed one chunk at a time, keeping only one chunk's worth of raw bytes live regardless of the transaction's size.

`reopen` exists solely for truncation.
If the truncation point falls inside a chunk that was already sealed, that chunk is decompressed back into a full-size buffer so that appends can resume from the new end.
The buffer is sized to the larger of the capacity and the chunk's raw length, which keeps an oversized chunk restorable.
A truncation lands in exactly one chunk, so this happens at most once per truncated transaction.

#### Compression

Compression is an optimization layered on top of the layout, not a prerequisite for it.
As the sizing below shows, the majority of the saving comes from serializing payloads into chunks at all, so the design remains worthwhile without it.

It is therefore controlled by a user configurable boolean property, e.g. `transaction.buffer.compressed`, that defaults to `true`.
Like `growthMax`, the configured value is handed to the transaction when it is created, and appears as `compressed` in `writableChunk`.
That one check is the entire switch.
When the property is `false`, no chunk is ever sealed, and every other code path already handles an unsealed chunk: `fits` rejects an event that does not fit, `open` returns the buffer as it is, and `reopen` has nothing to do.
A full chunk simply stays in memory as its raw 64KB buffer.

The trade-off is heap against CPU:

* Enabled, payloads occupy roughly half the heap, assuming the 2x ratio used in the sizing below: about 150 bytes per event rather than about 245, or about 3 GB rather than about 5 GB for the sizing workload.
  The cost is about 0.1 ms in the read loop for every 64KB of payload, a decompression of each chunk when its transaction commits, and the occasional `reopen` when a truncation lands in a sealed chunk.
* Disabled, none of that CPU is spent, and the full saving of the layout itself still applies.

Disabling it makes sense where heap is plentiful and the connector is bound by CPU, or where the payloads do not compress, which is the case for data that is already compressed or encrypted, such as most binary LOB content.
LZ4 gains little on such data while still costing the time to attempt it.

#### The transaction cache

The connector keeps its in-flight transactions in a `PackedTransactionCache`, a thin wrapper around a map keyed by transaction id:

```java
final class PackedTransactionCache {
    private final Map<String, PackedTransaction> transactions = new HashMap<>();
    private final Map<String, Integer> countAtBatchStart = new HashMap<>();
    private final TableDictionary tables = new TableDictionary();

    /**
     * Called at the start of a mining pass.
     */
    void beginBatch() {
        countAtBatchStart.clear();
    }

    void append(PackedTransaction tx, LogMinerEvent event) {
        countAtBatchStart.putIfAbsent(tx.transactionId, tx.eventCount());
        tx.append(event, tables);
    }

    /**
     * Called at the end of a mining pass.
     */
    void completeBatch() {
        countAtBatchStart.clear();
    }

    /**
     * Called when an ORA-00310 error is thrown during a read pass.
     * <p>
     * This discards the current batch pass from the cache as it cannot be trusted due to the exception.
     * The next mining pass will re-create the same batch pass again, and this just restores things as they were.
     */
    void discardBatch() {
        countAtBatchStart.forEach((id, n) -> {
            final PackedTransaction tx = transactions.get(id);
            if (tx != null) {
                tx.truncateTo(n);
            }
        });
        countAtBatchStart.clear();
    }

    void commit(String transactionId, TransactionCommitConsumer consumer) throws InterruptedException {
        final PackedTransaction tx = transactions.remove(transactionId);
        // Forward pass over metadata only: today's findRolledBackRange logic, producing skip bits
        // instead of deleting entries. Rolled-back events are never decoded.
        final BitSet skip = SavepointRollbacks.resolve(tx);
        tx.drain(skip, tables, event -> {
            // the event is rebuilt as the commit consumer expects; LOB and XML chains merge here as today
            consumer.accept(event, null, 0L);
        });
    }
}
```

The cache has two responsibilities beyond holding the map.
It owns the single `TableDictionary`, so that a table is interned once for the connector rather than once per transaction, and it coordinates the two operations that span transactions: batches and commits.

**Batches.**
`beginBatch`, `completeBatch`, and `discardBatch` give a log read pass transactional semantics, where `completeBatch` is synonymous with commit and `discardBatch` with rollback.
The first time a transaction is appended to during a pass, the cache records the event count it had beforehand; `putIfAbsent` ensures that later appends in the same pass do not overwrite that mark.
If the pass completes, the marks are simply forgotten.
If the pass fails with an `ORA-00310`, `discardBatch` truncates every transaction touched by the pass back to its mark.
A transaction that first appeared during the failed pass has a mark of zero, so it is emptied.
The next pass mines the same range again and rebuilds the same events, so nothing is lost and nothing is duplicated.
This is a requirement for https://github.com/debezium/dbz/issues/2504.

**Commits.**
When a commit is observed, the transaction is removed from the cache and emitted in two passes:

1. `SavepointRollbacks.resolve` walks the event metadata using the visitor and applies the partial rollback matching the connector performs today.
   Instead of deleting the rolled back events, it produces a `BitSet` with one skip bit per event.
2. `drain` walks the events in order, ignoring any event whose skip bit is set.
   For each remaining event, it rebuilds the `Scn`, row id, `rs_id`, and `TableId` from the metadata, and has `OraclePayloadCodec.decode` combine them with the event's payload from its chunk.
   The callback receives the same `LogMinerEvent` that `TransactionCommitConsumer` expects today, so LOB and XML event chains are merged exactly as they are now.
   The body of `drain` is not shown in the `PackedTransaction` listing, as it is the inverse of `append`.

The important property is that payloads are decoded lazily and only once, at the moment they are dispatched.
An event that was rolled back to a savepoint is never decoded, and a transaction that is rolled back entirely can be dropped from the map without reading any of its chunks.

#### Sizing changes

The following compares the heap cost of the current in-memory cache against the packed layout.
The numbers assume:

* A JVM with compressed oops and 8-byte object alignment
* 10 transactions of 2 million events each, 20 million events in total
* Rows of 10 columns, each holding a 10-character string value
* Half of the events are INSERTs and half are UPDATEs, with all columns logged
* The parser stores values as Strings, which is what `LogMinerDmlParser` produces today
* Every event is a DML event, so its payload is its column values; the tables below therefore refer to values rather than payloads

Treat totals as within about 30 percent; the ratios are the durable part.

Per event using the current memory cache:

| Piece | Bytes |
|---|---|
| DmlEvent object | 48 |
| Scn | 32 |
| Packed row id record | 32 |
| rs_id String plus byte array | 64 |
| Entry record plus list slot | 28 |
| HashMap node, boxed key, table slot | 52 |
| Metadata subtotal | 256 |
| Values: 10 or 20 Strings at 56 each plus arrays | 616 to 1232 |
| Blended total | about 1180 |

An INSERT carries 10 values and an UPDATE carries 20 (old and new), which is where the value range comes from; the blended total is the average across the even mix of the two.

Per event using `PackedTransaction`:

| Piece | Bytes |
|---|---|
| Metadata columns | 59 |
| Values serialized, 12 bytes per string | 130 to 240 |
| Blended total, uncompressed | about 245 |
| Blended total, sealed chunks at 2x LZ4 | about 150 |

For 10 transactions of 2 million events:

| Layout | Metadata | Values | Total heap |
|---|---|---|---|
| Current | 5.1 GB | 18 GB | about 23 GB |
| Packed, no LZ4 | 1.2 GB | 3.7 GB | about 5 GB |
| Packed, sealed at 2x | 1.2 GB | 1.9 GB | about 3 GB |

This is roughly a 4.5x reduction from the layout alone and 7x with compression, and in both cases the saving is dominated by values rather than metadata.
A 10-character String costs 56 bytes as an object and 12 bytes serialized, before any compressor is involved.
This is why the serialized value chunks are worth doing even if LZ4 never ships or is disabled.

Scaling the per-event figures to the 100 million event requirement gives about 24.5 GB uncompressed and about 15 GB with sealed chunks for events of this shape, both within the 50 GB target.

#### Garbage collection

There is a second benefit that the byte count does not show.
The current layout is about 27 objects per event, so 20 million events is over 500 million live objects.
The packed layout is tens of thousands of arrays, nearly all of them 64KB value chunks.
Most of what a garbage collector does is proportional to the number of live objects and the references between them, not to the bytes they occupy, so the collector's workload falls by far more than the 4.5x to 7x that the heap numbers suggest.
The following describes G1, the default collector, and the mechanisms involved; none of it has been measured yet.

**Young collections.**
A generational collector is built on the assumption that most objects die shortly after they are allocated.
A young collection pauses the application and copies whatever is still alive out of the young generation, so its pause time is driven by how much survives, not by how much was allocated.
A transaction buffer built from objects is the worst case for this assumption: every event, together with its `Scn`, row id, `String` values, and arrays, is still referenced by the buffer when the next young collection runs, so all of it survives.
Each object is copied between survivor spaces, eventually promoted to the old generation, and every reference to it is updated along the way.
During a large transaction the connector is therefore paying, in pauses, to copy roughly everything it has read since the previous collection.

The packed layout inverts this.
Appending an event stores primitives into arrays that already exist and copies bytes into a chunk that already exists.
The objects allocated while reading the row, such as the parsed values and the temporary `encodedValues` array, are garbage the moment `append` returns, and they die in the young generation at no cost, which is exactly the behavior the collector is designed for.
The only long-lived allocations are the chunks themselves, one per several hundred events, and copying a chunk is a single block copy with no references inside it to update.

**Old-to-young references.**
To collect the young generation without scanning the whole heap, G1 tracks every reference from an old region into a young one, and scans those as part of each young pause.
In the current layout, the buffer's long-lived lists and maps are in the old generation and are continually pointed at newly allocated events, which produces a steady stream of exactly these references.
Primitive arrays hold no references at all.
The only reference the packed layout adds as it grows is the entry in a transaction's `chunks` list, once per chunk.

**Concurrent marking.**
To reclaim the old generation, G1 walks the entire graph of live objects.
This walk runs concurrently with the application rather than in a pause, but it is not free: it occupies GC threads that compete with the connector for CPU, and its duration is proportional to the number of objects and references it must visit.
With 500 million live objects, every cycle is long.
A primitive array, in contrast, is marked in a single step regardless of its size, because there is nothing inside it to follow, so tens of thousands of arrays make for a marking cycle that is short and cheap.
The mixed collections that follow a cycle copy live objects out of old regions during a pause, so they scale with the object count in the same way.

**Full collections.**
The cycle length matters because of what happens when marking loses the race.
If the connector fills the heap before a marking cycle completes and frees space, G1 falls back to a full collection, which stops the application for the whole time it takes to mark and compact every live object.
At hundreds of millions of objects, that is a pause measured in tens of seconds or more, during which the connector reads nothing and emits nothing.
The current layout is exposed to this on both sides, since it fills the heap faster and takes longer to mark.
The packed layout allocates less, retains less, and completes its marking cycles quickly, which makes this fallback far less likely, and far shorter if it does occur.

The large metadata arrays deserve one note.
Once a transaction is large enough, its arrays exceed half of a G1 region and are allocated as humongous objects, which G1 places directly in the old generation and does not copy during normal collections.
This is benign for pause times, and it is the reason the [growth cap](#growth-cap) default favors few growth steps, since each step allocates a new set of these arrays.

### Generalized `PackedTransaction`

Nothing about the storage pattern above is specific to Oracle.
Only three things are: the transaction's immutable attributes, the set of per-event metadata fields, and how one of the connector's events maps onto those fields and onto a payload.
The generalized form extracts these three into connector-supplied types and leaves the mechanics (growth, truncation, payload chunks, batching, and draining) in shared code.

The following classes would exist in the `debezium-common-connector` framework module, so that any future connector could use them.
Throughout, `A` is the connector's transaction attributes type, `R` is the connector's event type, and `L` is the connector's layout type.
`R` is the parsed event that the connector already builds from what it reads, which for Oracle is `LogMinerEvent`, and not the raw row.

#### Columns

```java
/** Growable, truncatable, segmented. The common connector framework drives these; it never reads them. */
abstract class Column {
    abstract void ensureCapacity(int count);
    abstract void truncateTo(int count);
    // segment (de)serialization for persisted variants goes here too
}

final class LongColumn extends Column { long get(int i); void set(int i, long v); }
final class IntColumn extends Column { ... }
final class ShortColumn extends Column { ... }
final class ByteColumn extends Column { ... }
final class RefColumn<T> extends Column { T get(int i); void set(int i, T v); }
```

A `Column` is one of the parallel metadata arrays from the Oracle-centric design, wrapped so that the framework can manage it without knowing what it holds.
The framework only ever asks a column to grow or to truncate, and growth follows the same [growth cap](#growth-cap) policy in every column type.
Reading and writing go through the typed subclasses, and only connector code does that, which keeps the hot path free of boxing and casting.

`RefColumn` covers metadata that is naturally a shared object reference rather than a number.
A reference occupies 4 bytes under compressed oops, the same as an interned `int`, so a connector that already reuses one instance per distinct value can store the reference directly and skip the dictionary.

The base class is also the intended extension point for persistence: a variant that spills to off-heap or disk would serialize column segments here, without any change to the connector's layout.

#### Transaction layout

```java
/** What a connector contributes: attributes, columns, and how an event maps onto them. */
interface TransactionLayout<A, R> {
    List<Column> columns(); // registered once per transaction
    void write(int id, R event); // metadata only
    byte[] encode(R event); // everything else, or null
    R decode(A attributes, int id, PayloadInput payload); // payload is null if encode returned null
}
```

The layout is the entire contract between a connector and the buffer.
It declares the columns, which are registered with the transaction once, when it is created.
It writes an event's metadata into those columns at a given event id.
It encodes whatever else the event carries into a payload, or returns `null` if there is nothing else.
And it reverses the two, rebuilding the event from its columns at a given event id plus the payload.

The framework never looks inside a payload.
It has no notion of old and new column values, or of any other event shape, so a connector is free to model its events however it needs to, as Oracle does with its LOB and XML events.

A layout instance belongs to exactly one transaction, because the columns it declares hold that transaction's data.

#### Packed transaction

```java
/** No longer knows about a connector */
final class PackedTransaction<A, R, L extends TransactionLayout<A, R>> {
    final A attributes; // immutable, connector-defined
    private final L layout;
    private final List<Column> columns; // layout's columns plus the three below
    private final IntColumn payloadChunk, payloadOffset, payloadLength;
    private final List<PayloadChunk> chunks;
    private int count;

    int append(R event) {
        for (Column c : columns) c.ensureCapacity(count + 1);
        final int id = count++;
        layout.write(id, event);
        final byte[] payload = layout.encode(event);
        if (payload != null) { 
            // append to chunks as before
        } 
        else { 
            payloadChunk.set(id, -1); 
        }
        return id;
    }

    void truncateTo(int newCount) { 
        /* chunks as before */ 
        for (Column c : columns) {
            c.truncateTo(newCount);
        } 
        count = newCount; 
    }

    /** Commit-time passes are connector code that reads typed columns and sets skip bits, etc. */
    void drain(BitSet skip, DrainConsumer<A, R> consumer) { 
        /* as before, events decoded lazily with layout.decode */ 
    }
}
```

The generalized `PackedTransaction` has the same shape as the Oracle-centric one, with the connector-specific parts replaced by the type parameters:

* The immutable transaction fields collapse into a single connector-defined `attributes` value.
* The connector's metadata arrays become the layout's columns.
  The transaction owns only the three payload-location columns (`payloadChunk`, `payloadOffset`, and `payloadLength`), because payload storage is common to every connector.
  These are added to the layout's columns so that growth and truncation treat all of them uniformly.
* `append` grows every column, delegates the metadata write and the payload encoding to the layout, and then appends the payload to the chunks exactly as before.
* `truncateTo` trims the chunks exactly as before, and then truncates every column.
* `drain` keeps the skip-bit contract.
  Whatever commit-time analysis a connector needs, such as Oracle's savepoint rollback resolution, remains connector code that reads the typed columns and produces the `BitSet`.

`PayloadChunk` is unchanged, as it has no knowledge of the connector.
The payload codec is the opposite: it is entirely connector code, reached through the layout's `encode` and `decode`.

Carrying the layout as the type parameter `L` is what allows connector code to get its own layout back from a transaction, with its typed columns, without a cast.

#### Packed transaction cache

```java
final class PackedTransactionCache<A, R, L extends TransactionLayout<A, R>> {
    private final Map<String, PackedTransaction<A, R, L>> transactions = new LinkedHashMap<>();
    private final Map<String, Integer> countAtBatchStart = new HashMap<>();
    private final Supplier<L> layoutFactory;

    PackedTransactionCache(Supplier<L> layoutFactory) {
        this.layoutFactory = layoutFactory;
    }

    PackedTransaction<A, R, L> create(String transactionId, A attributes) {
        final PackedTransaction<A, R, L> transaction = new PackedTransaction<>(attributes, layoutFactory.get());
        transactions.put(transactionId, transaction);
        return transaction;
    }

    // beginBatch, append, discardBatch, get, and remove
}
```

The cache is constructed with a layout factory rather than a layout, since each transaction needs its own layout instance and therefore its own columns.
The batch operations are identical to the Oracle-centric version, which makes the read pass rollback capability available to every connector that adopts the buffer.
`commit` is deliberately absent, as how a transaction is resolved and emitted is the connector's decision.

#### Oracle on the generalized classes

Then inside a connector module, e.g. `debezium-connector-oracle`, these can be extended as follows:

```java
// Defines the immutable transaction attributes
record OracleTransactionAttributes(
        String transactionId, 
        BigInteger startScn, 
        long startTime,
        String userName, 
        String clientId, 
        int redoThreadId) {}

// Connector-specific event metadata layout and Serdes 
final class OracleLayout implements TransactionLayout<OracleTransactionAttributes, LogMinerEvent> {
    final ByteColumn type = new ByteColumn();
    final LongColumn scnDelta = new LongColumn();
    final LongColumn changeTime = new LongColumn();
    final LongColumn rsIdBlockSeq = new LongColumn();
    final ShortColumn rsIdOffset = new ShortColumn();
    final LongColumn rowIdHi = new LongColumn();
    final LongColumn rowIdLo = new LongColumn();
    final RefColumn<TableId> table = new RefColumn<>();
    // columns() and write() as in the sketch above; encode() and decode() delegate to OraclePayloadCodec
}

final class OracleTransactionCache extends PackedTransactionCache<OracleTransactionAttributes, LogMinerEvent, OracleLayout> {
    OracleTransactionCache() {
        super(OracleLayout::new);
    }

    void commit(String transactionId, TransactionCommitConsumer consumer) throws InterruptedException {
        // Remove transaction from cache at commit time.
        final var tx = remove(transactionId);
        // Perform savepoint rollback BitSet skip flags
        final BitSet skip = SavepointRollbacks.resolve(tx.layout(), tx.count()); // typed columns, no cast
        // drain the transaction queue 
        // For Oracle LOB processing, this would be handled by the commit consumer like today
        tx.drain(skip, ...);
    }
}
```

The Oracle connector's contribution reduces to three small types:

* `OracleTransactionAttributes` is the immutable transaction metadata, the same fields that headed the Oracle-centric `PackedTransaction`.
* `OracleLayout` declares the same per-event metadata as before, one column per array, so the footprint remains 59 bytes per event.
  Its `encode` and `decode` are the `OraclePayloadCodec` from [Payload encoding](#payload-encoding), with `decode` reading the event's metadata from its own columns and taking `startScn` from the attributes.
  The table is held in a `RefColumn<TableId>` rather than as an interned integer, which removes the need for `TableDictionary` at the same 4 bytes per event.
* `OracleTransactionCache` adds the one operation the framework leaves open, `commit`.
  It follows the same two passes as before: resolve savepoint rollbacks into skip bits by reading the layout's typed columns, and then drain the transaction into the `TransactionCommitConsumer`, which continues to handle LOB processing as it does today.

A second connector, such as Informix, would supply its own attributes record, layout, and commit handling in the same way, and would inherit the storage, compression, truncation, and batching behavior unchanged.

## Alternatives considered

A natural question is why Debezium should build this, rather than adopt an existing open source in-memory store.
The Oracle connector can already buffer transactions in Infinispan or Ehcache, but neither ships in every build of Debezium, and a requirement here is an option that ships in all of them.
That leaves the question of whether some other library could fill the role.

### Key-value caches and data grids

This covers the existing Infinispan and Ehcache buffers, as well as alternatives such as Apache Ignite or Apache Commons JCS.

**The unit of storage is wrong.**
A cache stores entries, so the natural mapping is one entry per event, and the cost of this design's problem is precisely the cost of having one of anything per event.
A cache that holds its values on the heap holds them by reference, so the roughly 27 objects per event remain, and the cache adds an entry, a key, and its own bookkeeping on top.
A cache that holds its values serialized or off-heap pays for serialization, a key, hashing, and entry metadata on every event.
The Ehcache buffer illustrates the point: as noted under [Payload encoding](#payload-encoding), it writes the event's class name and all of its metadata ahead of every event.
The saving in this design comes from the opposite direction, from hundreds of events sharing one array and from metadata that has no objects at all.
A cache could only match that if its values were whole chunks rather than events, at which point the cache is reduced to a `Map<Integer, byte[]>`, and everything described in this document would still need to be built on top of it.

**The workload is not a cache workload.**
A transaction buffer never evicts, because eviction is data loss.
Nothing expires, and no event is ever looked up by key.
What it needs is an ordered append, the ability to drop the tail, and a sequential replay, which is the shape of a log and not of a map.
Forcing that shape onto a map is what creates much of the complexity in the existing buffers:

* Order must be reconstructed with synthetic, counter-based event keys, where here it is the array index.
* The savepoint rollback pass must deserialize whole events to read their type and row id, where here it is a scan over a few primitive arrays.
* Undoing a mining pass means tracking and removing individual keys, where here it is a `truncateTo`.

**The dependency has a cost.**
A new library is a new dependency to ship, to patch, and to support in every build of Debezium.
This design needs only LZ4, which is already on the Kafka Connect classpath as a runtime dependency of `kafka-clients`, and which can be switched off entirely.

### Apache Arrow

Arrow deserves separate consideration, because it is not a cache.
It is a columnar, struct of arrays memory format, which is exactly the shape of the metadata in this design, and its variable-width vectors could hold the payloads.
The following reflects the Arrow Java 19.0.0 documentation.

* **It requires JVM flags.**
  Arrow Java needs `--add-opens=java.base/java.nio=org.apache.arrow.memory.core,ALL-UNNAMED` on the `java` command line, and fails at runtime without it.
  A connector is a plugin in a Kafka Connect worker whose JVM options belong to the operator and not to Debezium, so every deployment would need to change how its workers are launched before the connector could start.
* **It is off-heap only, with manual memory management.**
  An `ArrowBuf` is a region of direct memory, and in Arrow's words, "we use manual reference counting instead of the garbage collector".
  Every vector must be closed explicitly, and an allocator that is closed with memory outstanding throws an exception.
  The reasons Arrow gives for using direct memory are avoiding copies during I/O and sharing memory with native code through JNI, neither of which applies to a buffer that lives and dies inside one JVM.
  The allocator implementations are based on either Netty or `sun.misc.Unsafe`.
  Off-heap storage would also move the buffer out of the heap that users size and monitor today, and into native memory, which is sized and monitored separately.
* **It does not compress in memory.**
  Arrow's LZ4 and ZSTD buffer compression is a feature of serialized record batches, in other words of data being written to a stream or a file, and not of vectors held in memory.
  Sealing a full chunk, and reopening a sealed chunk after a truncation, would still have to be built.
* **It is a great deal of machinery for the need.**
  What this design requires is eleven primitive arrays and a list of byte buffers.
  Arrow brings a type system, schemas, null bitmaps, dictionaries, and an interchange format whose purpose is to move data between processes and languages, none of which the buffer uses.

Arrow is the right tool when columnar data has to cross a process or language boundary.
Here it never leaves the connector.

### What an existing library would provide

This design is bounded by the heap.
The sizing above puts 100 million events at about 15 to 25 GB, and a transaction that outgrows the heap will still fail.
An off-heap or disk tier is the one thing that Infinispan and Ehcache provide today that this design does not.

This is not a reason to choose differently, because the two are complementary rather than alternatives.
Sealed chunks are self-contained byte arrays that are no longer being written to, which makes them a natural unit to spill, and `Column` reserves a place for segment serialization for that purpose.
A future tier below this design, whether built or adopted, would store chunks and column segments rather than events, and would inherit the same savings.

An earlier revision of this document proposed Chronicle Queue, a disk-based persistent commit log, in pursuit of off-heap and disk persistence.
This revision addresses the heap buffer instead, since that is the buffer every build of Debezium ships.
Chronicle also shares the obstacle described for Arrow above, and to a greater degree.
On Java 17 and later, Chronicle's libraries require eleven `--add-exports` and `--add-opens` arguments on the `java` command line, covering `java.lang`, `java.lang.reflect`, `java.io`, `java.util`, `sun.nio.ch`, `sun.misc`, several `jdk.internal` packages, and the `jdk.compiler` module, and the application fails at startup without them.
As with Arrow, those arguments belong to whoever operates the Kafka Connect worker, not to the connector.
