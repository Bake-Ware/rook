package systems.bake.rook.wear

import org.junit.Assert.assertEquals
import org.junit.Test
import systems.bake.rook.wear.Talk.Phase
import systems.bake.rook.wear.Talk.State

class TalkPhaseTest {
    private val open = State(running = true, ready = true)

    @Test fun idleUntilAConversationRuns() {
        assertEquals(Phase.IDLE, State().phase)
        // A stale server state from the last conversation never colors the idle Rook.
        assertEquals(Phase.IDLE, State(server = "listening", playing = false).phase)
    }

    @Test fun connectingUntilThePhoneAnswers() {
        assertEquals(Phase.CONNECTING, State(running = true).phase)
        assertEquals(Phase.CONNECTING, State(running = true, server = "listening").phase)
        assertEquals(Phase.CONNECTING, open.phase)   // ready, but the voice server hasn't said yet
    }

    @Test fun followsTheVoiceServer() {
        assertEquals(Phase.LISTENING, open.copy(server = "listening").phase)
        assertEquals(Phase.THINKING, open.copy(server = "thinking").phase)
        assertEquals(Phase.SPEAKING, open.copy(server = "speaking").phase)
        assertEquals(Phase.CONNECTING, open.copy(server = "reconnecting").phase)
    }

    @Test fun speakingWhileTheWatchPlaysEvenAfterTheServerListens() {
        assertEquals(Phase.SPEAKING, open.copy(server = "listening", playing = true).phase)
        assertEquals(Phase.LISTENING, open.copy(server = "listening", playing = false).phase)
    }

    @Test fun aFailureIsRedUntilTheNextConversation() {
        val failed = open.copy(server = "speaking", playing = true).ended("Lost the phone")
        assertEquals(Phase.ERROR, failed.phase)
        assertEquals("Lost the phone", failed.transcript.lines.last().text)
        assertEquals(Phase.IDLE, open.ended(null).phase)
        assertEquals(Phase.CONNECTING, State(running = true, transcript = failed.transcript).phase)
    }

    @Test fun everyPhaseHasItsOwnColor() {
        assertEquals(Phase.values().size, Phase.values().map { it.color }.toSet().size)
    }
}
