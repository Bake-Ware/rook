package systems.bake.rook

/**
 * Bounded, sequence-numbered history of conversation events, kept for the life of
 * the process (owned by [VoiceBus], fed by VoiceService). An observer remembers the
 * last sequence number it handled and asks for everything [after] it, so events
 * that arrived while the screen was paused, backgrounded or destroyed are replayed
 * exactly once. Pure Kotlin for JVM tests.
 */
internal class ConversationLog<T>(private val capacity: Int = 2_000) {
    class Entry<T>(val seq: Long, val generation: Long, val event: T)

    private val entries = ArrayDeque<Entry<T>>()
    private var nextSeq = 1L

    /** Sequence number of the newest entry (0 when nothing was ever recorded). */
    val lastSeq: Long @Synchronized get() = nextSeq - 1

    @Synchronized fun append(generation: Long, event: T): Entry<T> {
        val e = Entry(nextSeq++, generation, event)
        entries.addLast(e)
        while (entries.size > capacity) entries.removeFirst()
        return e
    }

    /** Entries newer than [seq], oldest first. */
    @Synchronized fun after(seq: Long): List<Entry<T>> = entries.filter { it.seq > seq }

    @Synchronized fun size() = entries.size
}
