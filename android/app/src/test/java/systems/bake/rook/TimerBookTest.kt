package systems.bake.rook

import org.json.JSONObject
import org.junit.Assert.*
import org.junit.Test

class TimerBookTest {
    private var now = 1_000_000_000L
    private fun book(json: String? = null) = TimerBook.load(json) { now }
    private fun set(id: String, inMs: Long, label: String = "pasta") = TimerEvent("set", id, label, now + inMs, inMs / 1000)

    @Test fun parsesSetAndCancelAndRejectsMalformed() {
        val e = TimerEvent.parse(JSONObject("""{"type":"timer","action":"set","id":"t1","label":" Pasta ","fires_at":1700000000000,"duration_s":600}"""))!!
        assertEquals(TimerEvent("set", "t1", "Pasta", 1700000000000, 600), e)
        assertEquals("cancel", TimerEvent.parse(JSONObject("""{"type":"timer","action":"cancel","id":"t1"}"""))!!.action)
        for (bad in listOf("""{"type":"timer","action":"set","id":"t1"}""", """{"type":"timer","action":"snooze","id":"t1","fires_at":5}""",
                """{"type":"timer","action":"set","id":"","fires_at":5}""", """{"type":"timer","action":"set","id":"t1","fires_at":-1}""",
                """{"type":"background","action":"set","id":"t1","fires_at":5}"""))
            assertNull(bad, TimerEvent.parse(JSONObject(bad)))
    }

    @Test fun setIsIdempotentAndCancelRemoves() {
        val b = book()
        val e = set("t1", 60_000)
        assertTrue(b.apply(e) is TimerBook.Change.Arm)
        assertEquals(TimerBook.Change.None, b.apply(e))
        // Re-sent with a new time (the server adjusted it): armed again, still one timer.
        assertTrue(b.apply(e.copy(firesAt = e.firesAt + 1000)) is TimerBook.Change.Arm)
        assertEquals(1, b.timers().size)
        assertEquals(TimerBook.Change.Disarm("t1"), b.apply(TimerEvent("cancel", "t1", "", 0, 0)))
        assertTrue(b.timers().isEmpty())
        assertEquals(TimerBook.Change.None, b.apply(TimerEvent("cancel", "t1", "", 0, 0)))
    }

    @Test fun resendAfterRingingOrCancelDoesNotRingAgain() {
        val b = book()
        val e = set("t1", 1000)
        b.apply(e)
        now += 1000
        assertEquals("t1", b.fired("t1")!!.id)
        assertNull(b.fired("t1"))
        assertEquals(TimerBook.Change.None, b.apply(e))   // reconnect resend
        b.apply(set("t2", 5000)); b.cancel("t2")
        assertEquals(TimerBook.Change.None, b.apply(set("t2", 5000)))
        assertTrue(b.timers().isEmpty())
    }

    @Test fun pastDueRingsNowButStaleIsDropped() {
        val b = book()
        assertTrue(b.apply(set("late", -5000)) is TimerBook.Change.RingNow)
        assertEquals(TimerBook.Change.None, b.apply(set("stale", -TimerBook.LATE_LIMIT_MS - 1)))
        assertTrue(b.isFinished("stale"))
    }

    @Test fun persistsAndRearmsAfterRestart() {
        val b = book()
        b.apply(set("soon", 60_000, "Eggs")); b.apply(set("later", 3_600_000, ""))
        b.apply(set("done", 1000)); now += 2000; b.fired("done")
        val restored = book(b.toJson())
        assertEquals(listOf("soon", "later"), restored.timers().map { it.id })
        assertEquals("Eggs", restored.get("soon")!!.label)
        assertTrue(restored.isFinished("done"))
        now += 120_000   // "soon" was missed while the phone was off
        val changes = restored.rearm()
        assertEquals(listOf("soon"), changes.filterIsInstance<TimerBook.Change.RingNow>().map { it.timer.id })
        assertEquals(listOf("later"), changes.filterIsInstance<TimerBook.Change.Arm>().map { it.timer.id })
        now += 2 * TimerBook.LATE_LIMIT_MS
        val dropped = book(restored.toJson())
        assertTrue(dropped.rearm().isEmpty())
        assertTrue(dropped.timers().isEmpty())
    }

    @Test fun corruptStoreStartsEmptyAndFinishedIsBounded() {
        assertTrue(book("{not json").timers().isEmpty())
        val b = book()
        repeat(TimerBook.MAX_FINISHED + 10) { b.cancel("x$it") }
        assertFalse(b.isFinished("x0"))
        assertTrue(b.isFinished("x${TimerBook.MAX_FINISHED + 9}"))
    }

    @Test fun spokenTextAndCountdown() {
        assertEquals("Pasta timer is done", ActiveTimer("a", "pasta", 0, 0).doneText)
        assertEquals("Your timer is done", ActiveTimer("a", " ", 0, 0).doneText)
        assertEquals("4:05", timerRemaining(245_000, 0))
        assertEquals("1:00:01", timerRemaining(3_600_500, 0))
        assertEquals("0:00", timerRemaining(0, 10))
    }
}
