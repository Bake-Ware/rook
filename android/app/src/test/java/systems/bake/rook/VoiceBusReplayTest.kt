package systems.bake.rook

import org.junit.Assert.*
import org.junit.Test

class VoiceBusReplayTest {
    private class Screen : VoiceBus.Listener {
        val lines = mutableListOf<String>()
        var seen = 0L
        override fun onState(state: String) {}
        override fun onTranscript(text: String) { lines += "you: $text@${VoiceBus.eventGeneration}" }
        override fun onAssistantDelta(text: String) { lines += "rook: $text" }
        override fun onAssistantDone() {}
        override fun onInterrupt() {}
        override fun onError(msg: String) {}
        override fun onDelivered(seq: Long) { seen = seq }
    }

    @Test fun eventsWhileAwayAreReplayedOnceOnResume() {
        val screen = Screen()
        VoiceBus.attach(screen, VoiceBus.record { })     // start from "now"
        VoiceBus.emit(7) { it.onTranscript("hello") }    // live
        assertEquals(listOf("you: hello@7"), screen.lines)

        VoiceBus.listener = null                          // paused / backgrounded
        VoiceBus.emit(8) { it.onTranscript("while away") }
        VoiceBus.emit(8) { it.onAssistantDelta("answer") }
        assertEquals(1, screen.lines.size)

        VoiceBus.attach(screen, screen.seen)              // resumed
        assertEquals(listOf("you: hello@7", "you: while away@8", "rook: answer"), screen.lines)
        assertFalse(VoiceBus.replaying)

        VoiceBus.listener = null
        VoiceBus.attach(screen, screen.seen)              // resumed again: nothing new, no duplicates
        assertEquals(3, screen.lines.size)
        VoiceBus.listener = null
    }

    @Test fun recreatedScreenRebuildsHistory() {
        val first = Screen()
        val start = VoiceBus.record { }
        VoiceBus.attach(first, start)
        VoiceBus.emit(3) { it.onTranscript("one") }
        VoiceBus.listener = null                          // activity destroyed
        VoiceBus.emit(3) { it.onTranscript("two") }
        val second = Screen()
        VoiceBus.attach(second, start)                    // a new instance replays from the log
        assertEquals(listOf("you: one@3", "you: two@3"), second.lines)
        VoiceBus.listener = null
    }
}
