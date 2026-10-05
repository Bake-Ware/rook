package systems.bake.rook

import org.junit.Assert.*
import org.junit.Test

class SpeakQueueTest {
    private fun job(id: String, interrupt: Boolean = false) = SpeakQueue.Job(id, "text $id", "", 1f, 1f, interrupt)

    @Test fun fifoAndCompletion() {
        val q = SpeakQueue()
        q.add(job("a")); q.add(job("b"))
        assertEquals("a", q.next(false)?.id)
        assertEquals("b", q.next(false)?.id)
        assertNull(q.next(false))
        q.started("a")
        assertEquals(SpeakQueue.SPEAKING, q.get("a")?.state)
        assertFalse(q.finished("a", SpeakQueue.DONE))
        assertTrue(q.finished("b", SpeakQueue.DONE))
        assertTrue(q.get("a")!!.done && q.idle())
    }

    @Test fun waitsBehindVoiceReplyUnlessInterrupting() {
        val q = SpeakQueue()
        q.add(job("a"))
        assertNull(q.next(replyPlaying = true))
        assertEquals(SpeakQueue.QUEUED, q.get("a")?.state)
        assertEquals("a", q.next(replyPlaying = false)?.id)
    }

    @Test fun interruptSupersedesOlderSpeechButNotNewer() {
        val q = SpeakQueue()
        q.add(job("playing")); q.next(false); q.started("playing")
        q.add(job("held")); q.add(job("now", interrupt = true)); q.add(job("after"))
        assertEquals(1, q.ahead("held"))
        assertEquals("now", q.next(replyPlaying = true)?.id)
        assertEquals(SpeakQueue.STOPPED, q.get("playing")?.state)
        assertEquals(SpeakQueue.STOPPED, q.get("held")?.state)
        assertNotNull(q.get("held")?.error)
        // The engine's late onStop for the flushed utterance doesn't resurrect anything.
        assertFalse(q.finished("playing", SpeakQueue.STOPPED))
        assertNull(q.next(replyPlaying = true))
        assertEquals("after", q.next(false)?.id)
    }

    @Test fun failAllAndStopAll() {
        val q = SpeakQueue()
        q.add(job("a")); q.add(job("b")); q.next(false)
        q.failAll("init failed")
        assertEquals("init failed", q.get("a")?.error)
        assertEquals(SpeakQueue.ERROR, q.get("b")?.state)
        q.add(job("c"))
        assertEquals(1, q.stopAll())
        assertEquals(SpeakQueue.STOPPED, q.get("c")?.state)
        assertTrue(q.idle())
    }

    @Test fun historyIsBoundedToFinishedJobs() {
        val q = SpeakQueue(keep = 2)
        q.add(job("a")); q.next(false); q.finished("a", SpeakQueue.DONE)
        q.add(job("b")); q.add(job("c"))
        assertNull(q.get("a"))
        assertNotNull(q.get("b")); assertNotNull(q.get("c"))
    }
}
