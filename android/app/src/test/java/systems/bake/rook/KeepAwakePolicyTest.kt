package systems.bake.rook

import org.junit.Assert.*
import org.junit.Test

class KeepAwakePolicyTest {
    private val min = 60_000L

    @Test fun explicitHoldLastsTheGivenMinutesThenLapses() {
        val p = KeepAwakePolicy()
        assertFalse(p.held(0))
        p.engage(0, 30)
        assertTrue(p.held(30 * min - 1))
        assertEquals("manual", p.mode(1))
        assertFalse(p.held(30 * min))
        assertNull(p.mode(30 * min))
    }

    @Test fun passiveCallsRestartTheIdleTimer() {
        val p = KeepAwakePolicy()
        assertTrue(p.activity(0, 10))            // first call starts the hold
        assertFalse(p.activity(9 * min, 10))     // later calls only extend it
        assertTrue(p.held(18 * min))
        assertEquals("auto", p.mode(18 * min))
        assertFalse(p.held(19 * min))
        assertTrue(p.activity(20 * min, 10))     // idle lapsed: a new hold starts
    }

    @Test fun laterDeadlineWinsAndOffClearsBoth() {
        val p = KeepAwakePolicy()
        p.engage(0, 5)
        assertFalse(p.activity(min, 10))         // already held by the explicit hold
        assertEquals("auto", p.mode(2 * min))
        assertEquals(11 * min, p.until())
        p.clearAuto()
        assertEquals(5 * min, p.until())
        p.activity(min, 10)
        p.release()
        assertFalse(p.held(min))
    }

    @Test fun minutesAreClamped() {
        assertEquals(1, KeepAwakePolicy.clampMinutes(0))
        assertEquals(1, KeepAwakePolicy.clampMinutes(-5))
        assertEquals(KeepAwakePolicy.MAX_MINUTES, KeepAwakePolicy.clampMinutes(100_000))
        val p = KeepAwakePolicy()
        p.engage(0, 0)
        assertEquals(min, p.until())
    }
}
