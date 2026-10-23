package systems.bake.rook
import org.junit.Assert.*
import org.junit.Test
import systems.bake.rook.BargeGate.Frame

class BargeGateTest {
    /** Minimal playback model applying the gate's answers the way VoiceClient does. */
    private class Player(val gate: BargeGate, var playingUntil: Long) {
        var paused = false; var pausedAt = 0L; var flushed = false; var now = 0L
        val sent = mutableListOf<Frame>()
        fun playing() = now < playingUntil
        fun flush() { playingUntil = 0; paused = false; flushed = true; gate.clear() }
        fun unpause() { if (paused) { playingUntil = BargeGate.resumedUntil(playingUntil, pausedAt, now); paused = false } }
        fun push(speech: Boolean, n: Int = 1): Frame {
            var last = Frame.DROP
            repeat(n) {
                now += 20
                if (gate.expired(now)) flush()
                last = gate.frame(speech, playing(), paused, gated = true, now = now)
                when (last) {
                    Frame.PAUSE -> { paused = true; pausedAt = now }
                    Frame.UNPAUSE -> unpause()
                    Frame.HOLD -> if (!paused) { paused = true; pausedAt = now }
                    Frame.BARGE -> flush()
                    else -> {}
                }
                sent += last
            }
            return last
        }
        fun resume() { if (gate.resume()) unpause() }
    }

    @Test fun legacyServerFlushesOnConfirmedSpeech() {
        val p = Player(BargeGate(), playingUntil = 60_000)
        assertEquals(Frame.DROP, p.push(true, 7))
        assertEquals(Frame.PAUSE, p.push(true)); assertTrue(p.paused)
        assertEquals(Frame.DROP, p.push(true, 9))
        assertEquals(Frame.BARGE, p.push(true))
        assertTrue(p.flushed); assertFalse(p.gate.held)
        // Not playing any more: everything goes to the server.
        assertEquals(Frame.SEND, p.push(false))
    }
    @Test fun shortPauseResumesAfterQuietOnBothServers() {
        for (confirm in listOf(false, true)) {
            val p = Player(BargeGate().also { it.confirm = confirm }, playingUntil = 10_000)
            p.push(true, 8); assertTrue(p.paused)
            assertEquals(Frame.DROP, p.push(false, 9))
            assertEquals(Frame.UNPAUSE, p.push(false)); assertFalse(p.paused)
            // 10 quiet frames (200 ms) were paused: the reply ends that much later.
            assertEquals(10_200L, p.playingUntil)
            assertFalse(p.flushed); assertFalse(p.gate.held)
        }
    }
    @Test fun confirmServerHoldsThenResumeRestoresTiming() {
        val p = Player(BargeGate().also { it.confirm = true }, playingUntil = 5_000)
        p.push(true, 8); val pausedAt = p.pausedAt
        assertEquals(Frame.HOLD, p.push(true, 10))
        assertTrue(p.gate.held); assertTrue(p.paused); assertFalse(p.flushed)
        // Held: every mic frame is streamed, speech or not.
        assertEquals(List(30) { Frame.SEND }, List(30) { p.push(it % 2 == 0) })
        p.now += 2_000
        p.resume()
        assertFalse(p.gate.held); assertFalse(p.paused); assertFalse(p.flushed)
        assertEquals(5_000L + (p.now - pausedAt), p.playingUntil)
        // Gated again while the reply plays.
        assertEquals(Frame.DROP, p.push(false))
        assertTrue(p.playing())
    }
    @Test fun holdThenInterruptFlushes() {
        val p = Player(BargeGate().also { it.confirm = true }, playingUntil = 5_000)
        assertEquals(Frame.HOLD, p.push(true, 18))
        p.flush()   // server interrupt
        assertFalse(p.gate.held); assertFalse(p.gate.resume())
        assertFalse(p.gate.expired(p.now + 60_000))
        assertEquals(Frame.SEND, p.push(false))
    }
    @Test fun holdTimesOutAfterTenSeconds() {
        val g = BargeGate().also { it.confirm = true }
        val p = Player(g, playingUntil = 60_000)
        p.push(true, 18); val since = g.heldSince
        assertFalse(g.expired(since + 10_000)); assertTrue(g.held)
        assertTrue(g.expired(since + 10_001)); assertFalse(g.held)
        assertFalse(g.expired(since + 20_000))
        // Driven by mic frames: the first frame after 10 s flushes and is then sent.
        val q = Player(BargeGate().also { it.confirm = true }, playingUntil = 60_000)
        q.push(true, 18); q.push(false, 500)
        assertFalse(q.flushed); assertTrue(q.gate.held)
        q.push(false, 1)
        assertTrue(q.flushed); assertFalse(q.gate.held); assertEquals(Frame.SEND, q.sent.last())
    }
    @Test fun framesForwardedOnlyWhileHeldOrNotPlaying() {
        val g = BargeGate().also { it.confirm = true }
        assertEquals(Frame.SEND, g.frame(true, playing = false, paused = false, gated = true, now = 0))
        assertEquals(Frame.DROP, g.frame(false, playing = true, paused = false, gated = true, now = 0))
        // No AEC (or protocol 1): playback never sends and never counts speech.
        repeat(40) { assertEquals(Frame.DROP, g.frame(true, playing = true, paused = false, gated = false, now = 0)) }
        assertFalse(g.held)
        val p = Player(g, playingUntil = 60_000)
        p.push(true, 18)
        assertTrue(p.sent.dropLast(1).none { it == Frame.SEND })
        assertEquals(Frame.SEND, p.push(true)); assertEquals(Frame.SEND, p.push(false))
    }
    @Test fun audioQueuedWhilePausedDoesNotDoubleCountThePause() {
        // 300 ms left at the pause, 200 ms more arrive while paused: 500 ms left after resuming.
        val until = BargeGate.extendedUntil(1_300, now = 1_500, pausedAt = 1_000, ms = 200)
        assertEquals(1_500L, until)
        assertEquals(4_500L, BargeGate.resumedUntil(until, pausedAt = 1_000, now = 4_000))
        assertEquals(2_200L, BargeGate.extendedUntil(1_300, now = 2_000, pausedAt = null, ms = 200))
    }
}
