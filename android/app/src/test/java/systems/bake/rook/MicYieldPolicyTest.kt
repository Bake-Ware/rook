package systems.bake.rook

import org.junit.Assert.*
import org.junit.Test

class MicYieldPolicyTest {
    private fun policy() = MicYieldPolicy(settleMs = 1_000, maxBackoffMs = 8_000, stableMs = 10_000)

    @Test fun anotherRecorderReleasesOnceAndWaitsUntilItIsGone() {
        val p = policy()
        p.acquired(0)
        p.othersRecording = true
        assertTrue(p.update(30_000))        // release now
        assertFalse(p.update(30_100))       // already released: no second release
        assertEquals(MicYieldPolicy.Reason.OTHER_APP, p.reason)
        assertFalse(p.mayAcquire(60_000))   // never steal back while it is still recording
        assertNull(p.retryAt())
        p.othersRecording = false
        assertFalse(p.update(70_000))
        assertEquals(71_000L, p.retryAt())
        assertFalse(p.mayAcquire(70_500))   // settle delay
        assertTrue(p.mayAcquire(71_000))
        p.acquired(71_000)
        assertFalse(p.paused)
    }

    @Test fun silencedOwnClientYields() {
        val p = policy()
        p.acquired(0)
        p.silenced = true
        assertTrue(p.update(50_000))
        assertEquals(MicYieldPolicy.Reason.OTHER_APP, p.reason)
    }

    @Test fun callOutranksOtherReasonsAndBlocksReacquire() {
        val p = policy()
        p.acquired(0)
        p.focusLost = true
        p.inCall = true
        assertTrue(p.update(50_000))
        assertEquals(MicYieldPolicy.Reason.CALL, p.reason)
        p.focusLost = false
        p.update(50_001)
        assertFalse(p.mayAcquire(100_000))
        p.inCall = false
        p.update(100_000)
        assertTrue(p.mayAcquire(100_000 + 1_000))
    }

    @Test fun transientFocusLossWaitsForGain() {
        val p = policy()
        p.acquired(0)
        assertEquals(true, MicYieldPolicy.focusLoss(-2))
        p.focusLost = true
        assertTrue(p.update(30_000))
        assertFalse(p.mayAcquire(99_000))
        p.focusLost = MicYieldPolicy.focusLoss(1)!!
        p.update(100_000)
        assertTrue(p.mayAcquire(101_000))
    }

    @Test fun duckingIsNotAYield() {
        assertNull(MicYieldPolicy.focusLoss(-3))
        assertEquals(true, MicYieldPolicy.focusLoss(-1))
        assertEquals(false, MicYieldPolicy.focusLoss(3))
    }

    @Test fun flappingBacksOffExponentiallyToTheCap() {
        val p = policy()
        var now = 0L
        p.acquired(now)
        val delays = mutableListOf<Long>()
        repeat(6) {
            p.othersRecording = true; p.update(now + 10)     // pushed off right after acquiring
            p.othersRecording = false; p.update(now + 20)
            val at = p.retryAt()!!
            delays += at - (now + 20)
            now = at
            assertTrue(p.mayAcquire(now))
            p.acquired(now)
        }
        assertEquals(listOf(2_000L, 4_000L, 8_000L, 8_000L, 8_000L, 8_000L), delays)
    }

    @Test fun stableCaptureResetsBackoff() {
        val p = policy()
        p.acquired(0)
        p.silenced = true; p.update(5); p.silenced = false; p.update(6)
        p.acquired(p.retryAt()!!)
        p.othersRecording = true; p.update(1_000_000)        // long after acquiring: not a flap
        assertEquals(0, p.attempts)
        p.othersRecording = false; p.update(1_000_001)
        assertEquals(1_001_001L, p.retryAt())
    }

    @Test fun failedReopenStaysPausedAndBacksOff() {
        val p = policy()
        p.acquired(0)
        p.othersRecording = true; p.update(50_000)
        p.othersRecording = false; p.update(50_001)
        assertTrue(p.mayAcquire(51_001))
        p.acquireFailed(51_001)
        assertTrue(p.paused)
        assertEquals(51_001L + 2_000L, p.retryAt())
        assertFalse(p.mayAcquire(52_000))
    }

    @Test fun notPausedMayAcquireOnlyWithoutReasons() {
        val p = policy()
        assertTrue(p.mayAcquire(0))
        p.inCall = true
        assertFalse(p.mayAcquire(0))     // never open the mic during a call
        p.reset()
        p.inCall = false
        assertTrue(p.mayAcquire(0))
    }

    @Test fun resetClearsPause() {
        val p = policy()
        p.othersRecording = true; p.update(0)
        assertTrue(p.paused)
        p.reset()
        assertFalse(p.paused)
        assertNull(p.reason)
    }

    @Test fun callModes() {
        assertTrue(MicYieldPolicy.callMode(MicYieldPolicy.MODE_IN_CALL, rookOwnsMode = true))
        assertTrue(MicYieldPolicy.callMode(MicYieldPolicy.MODE_RINGTONE, rookOwnsMode = false))
        assertFalse(MicYieldPolicy.callMode(MicYieldPolicy.MODE_IN_COMMUNICATION, rookOwnsMode = true))
        assertTrue(MicYieldPolicy.callMode(MicYieldPolicy.MODE_IN_COMMUNICATION, rookOwnsMode = false))
        assertFalse(MicYieldPolicy.callMode(0, rookOwnsMode = false))
    }
}
