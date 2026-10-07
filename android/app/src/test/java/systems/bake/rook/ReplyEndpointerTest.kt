package systems.bake.rook
import org.junit.Assert.*
import org.junit.Test
import systems.bake.rook.ReplyEndpointer.State

class ReplyEndpointerTest {
    private fun run(e: ReplyEndpointer, levels: List<Float>): State {
        var s = State.LISTENING
        for (l in levels) { s = e.feed(l); if (s != State.LISTENING) return s }
        return s
    }
    private fun frames(ms: Int, level: Float) = List(ms / 20) { level }

    @Test fun replyEndsAfterSilence() {
        val e = ReplyEndpointer(startTimeoutMs = 8000)
        val s = run(e, frames(500, 0.002f) + frames(1500, 0.1f) + frames(2000, 0.002f))
        assertEquals(State.SPEECH_ENDED, s); assertTrue(e.heardSpeech)
    }
    @Test fun silenceMeansNoReply() {
        assertEquals(State.NO_SPEECH, run(ReplyEndpointer(startTimeoutMs = 3000), frames(5000, 0.003f)))
    }
    @Test fun aShortBlipDoesNotOpenTheReply() {
        val e = ReplyEndpointer(startTimeoutMs = 3000)
        assertEquals(State.NO_SPEECH, run(e, frames(400, 0.002f) + frames(100, 0.2f) + frames(4000, 0.002f)))
        assertFalse(e.heardSpeech)
    }
    @Test fun speechStartedNearTheTimeoutIsKept() {
        val e = ReplyEndpointer(startTimeoutMs = 1000)
        assertEquals(State.SPEECH_ENDED, run(e, frames(940, 0.002f) + frames(800, 0.1f) + frames(1400, 0.002f)))
    }
    @Test fun longSpeechIsCapped() {
        assertEquals(State.TOO_LONG, run(ReplyEndpointer(startTimeoutMs = 8000, maxMs = 3000), frames(5000, 0.1f)))
    }
    @Test fun talkingStraightAwayStillCounts() {
        val e = ReplyEndpointer(startTimeoutMs = 3000)
        assertEquals(State.SPEECH_ENDED, run(e, frames(1500, 0.12f) + frames(1400, 0.004f)))
    }
    @Test fun noisyRoomRaisesTheBar() {
        val e = ReplyEndpointer(startTimeoutMs = 2000)
        // Floor 0.02 -> threshold 0.06: steady 0.04 hum is not speech.
        assertEquals(State.NO_SPEECH, run(e, frames(300, 0.02f) + frames(3000, 0.04f)))
    }
}
