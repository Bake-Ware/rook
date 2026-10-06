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
        // "In 10 minutes" with no server clock time is still a valid set.
        assertEquals(600L, TimerEvent.parse(JSONObject("""{"type":"timer","action":"set","id":"t1","duration_s":600}"""))!!.durationS)
        for (bad in listOf("""{"type":"timer","action":"set","id":"t1"}""", """{"type":"timer","action":"set","id":"t1","duration_s":0}""", """{"type":"timer","action":"snooze","id":"t1","fires_at":5}""",
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
        repeat(TimerBook.MAX_FINISHED + 10) { b.apply(set("x$it", 1000)); b.cancel("x$it") }
        assertFalse(b.isFinished("x0"))
        assertTrue(b.isFinished("x${TimerBook.MAX_FINISHED + 9}"))
    }

    @Test fun reusedIdWithNewFireTimeStillArms() {
        val b = book()
        val first = TimerEvent("set", "t1", "tea", now + 60_000, 0)
        b.apply(first)
        now += 60_000
        assertNotNull(b.fired("t1"))
        assertEquals(TimerBook.Change.None, b.apply(first))                 // resend of the one that rang
        val again = TimerEvent("set", "t1", "tea", now + 300_000, 0)          // same id, a new timer
        assertTrue(b.apply(again) is TimerBook.Change.Arm)
        assertTrue(b.isFinished("t1", first.firesAt)); assertFalse(b.isFinished("t1", again.firesAt))
        b.cancel("t1")
        assertEquals(TimerBook.Change.None, b.apply(again))                 // resend after cancel
        // Survives a restart.
        val restored = book(b.toJson())
        assertEquals(TimerBook.Change.None, restored.apply(first))
        assertEquals(TimerBook.Change.None, restored.apply(again))
        assertTrue(restored.apply(TimerEvent("set", "t1", "tea", now + 900_000, 0)) is TimerBook.Change.Arm)
    }

    @Test fun durationUsesPhoneArrivalTimeNotServerClock() {
        val b = book()
        // Server clock 2 minutes fast: its fires_at is 2 min later than the phone's now + 10 min.
        val e = TimerEvent("set", "pasta", "pasta", now + 120_000 + 600_000, 600)
        val armed = (b.apply(e) as TimerBook.Change.Arm).timer
        assertEquals(now + 600_000, armed.firesAt)
        assertEquals(e.firesAt, armed.serverFiresAt)
        // Reconnect resend 30 s later: unchanged, keeps the original phone fire time.
        val armedAt = now; now += 30_000
        assertEquals(TimerBook.Change.None, b.apply(e))
        assertEquals(armedAt + 600_000, b.get("pasta")!!.firesAt)
        assertEquals(armedAt + 600_000, book(b.toJson()).get("pasta")!!.firesAt)
        // Server clock far behind: a duration timer still rings on time instead of being dropped as stale.
        val behind = TimerEvent("set", "eggs", "", now - 2 * TimerBook.LATE_LIMIT_MS, 300)
        assertEquals(now + 300_000, (b.apply(behind) as TimerBook.Change.Arm).timer.firesAt)
        // No duration ("at 7pm"): fires_at is used as is.
        val at = TimerEvent("set", "seven", "", now + 3_600_000, 0)
        assertEquals(at.firesAt, (b.apply(at) as TimerBook.Change.Arm).timer.firesAt)
    }

    @Test fun userCancelIsQueuedForTheServerAndPersisted() {
        val b = book()
        b.apply(set("t1", 60_000)); b.apply(set("t2", 60_000))
        assertEquals(TimerBook.Change.Disarm("t1"), b.userCancel("t1"))
        assertEquals(TimerBook.Change.None, b.userCancel("nope"))
        b.apply(TimerEvent("cancel", "t2", "", 0, 0))                        // server cancel: nothing to send back
        assertEquals(listOf("t1"), b.pendingCancels())
        val restored = book(b.toJson())
        assertEquals(listOf("t1"), restored.pendingCancels())
        restored.sent("t1")
        assertTrue(restored.pendingCancels().isEmpty())
        assertTrue(book(restored.toJson()).pendingCancels().isEmpty())
    }

    @Test fun spokenTextAndCountdown() {
        assertEquals("Pasta timer is done", ActiveTimer("a", "pasta", 0, 0).doneText)
        assertEquals("Your timer is done", ActiveTimer("a", " ", 0, 0).doneText)
        assertEquals("4:05", timerRemaining(245_000, 0))
        assertEquals("1:00:01", timerRemaining(3_600_500, 0))
        assertEquals("0:00", timerRemaining(0, 10))
    }
}
