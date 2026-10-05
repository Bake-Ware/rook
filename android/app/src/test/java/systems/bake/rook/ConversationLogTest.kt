package systems.bake.rook

import org.junit.Assert.*
import org.junit.Test

class ConversationLogTest {
    @Test fun replaysOnlyWhatTheObserverMissed() {
        val log = ConversationLog<String>()
        log.append(1, "heard: hello")
        log.append(1, "said: hi")
        var seen = log.lastSeq                       // screen rendered both live
        log.append(1, "heard: what time is it")      // screen paused
        log.append(1, "said: noon")
        val missed = log.after(seen)
        assertEquals(listOf("heard: what time is it", "said: noon"), missed.map { it.event })
        seen = missed.last().seq
        assertTrue(log.after(seen).isEmpty())        // resuming again adds no duplicates
    }

    @Test fun recreatedScreenGetsWholeHistoryInOrderWithGenerations() {
        val log = ConversationLog<String>()
        log.append(1, "a"); log.append(2, "b"); log.append(2, "c")
        val all = log.after(0)
        assertEquals(listOf("a", "b", "c"), all.map { it.event })
        assertEquals(listOf(1L, 2L, 2L), all.map { it.generation })
        assertEquals(listOf(1L, 2L, 3L), all.map { it.seq })
    }

    @Test fun boundedKeepsNewestAndSequenceKeepsGrowing() {
        val log = ConversationLog<Int>(capacity = 3)
        repeat(5) { log.append(0, it) }
        assertEquals(3, log.size())
        assertEquals(listOf(2, 3, 4), log.after(0).map { it.event })
        assertEquals(5L, log.lastSeq)
        assertEquals(listOf(4), log.after(4).map { it.event })
    }

    @Test fun recordedLocalEventIsNotReplayedToItsAuthor() {
        val log = ConversationLog<String>()
        log.append(1, "heard: x")
        val seen = log.append(1, "typed: y").seq   // screen drew it itself
        log.append(1, "said: z")
        assertEquals(listOf("said: z"), log.after(seen).map { it.event })
    }
}
