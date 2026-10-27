package systems.bake.rook

import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class WidgetTranscriptTest {
    @After fun detach() { VoiceBus.listener = null; VoiceBus.onLogged = null }

    private fun WidgetTranscript.lines() = snapshot().map { (if (it.user) "you: " else "agent: ") + it.text + if (it.faded) " (cut)" else "" }

    /** A transcript that starts from "now", like a widget placed on a running app. */
    private fun fresh(capacity: Int = 30) = WidgetTranscript(capacity).also { VoiceBus.replay(it, VoiceBus.record { } - 1) }

    @Test fun bubblesFollowTheAppsRules() {
        val t = fresh()
        VoiceBus.emit(5) { it.onTranscript("what's on tonight", 1) }
        VoiceBus.emit(5) { it.onAssistantDelta("Dinner at seven,", 1) }
        VoiceBus.emit(5) { it.onAssistantDelta("and the bins go out.", 1) }
        VoiceBus.emit(5) { it.onAssistantDone() }
        VoiceBus.record { it.onUserText("typed note") }
        VoiceBus.emit { it.onSpoken("Timer done") }
        VoiceBus.replay(t, t.seen)
        assertEquals(listOf("you: what's on tonight", "agent: Dinner at seven, and the bins go out.",
            "you: typed note", "agent: Timer done"), t.lines())
    }

    @Test fun catchUpReplaysOnlyWhatIsNewAndLeavesTheScreenAlone() {
        val t = fresh()
        VoiceBus.emit(6) { it.onTranscript("one", 1) }
        VoiceBus.replay(t, t.seen)
        VoiceBus.replay(t, t.seen)                        // nothing new: no duplicates
        assertEquals(listOf("you: one"), t.lines())
        assertNull(VoiceBus.listener)
    }

    @Test fun deltasJoinTheirTurnEvenWhenTurnsInterleave() {
        val t = fresh()
        VoiceBus.emit(7) { it.onTranscript("first", 1) }
        VoiceBus.emit(7) { it.onAssistantDelta("one", 1) }
        VoiceBus.emit(7) { it.onTranscript("second", 2) }
        VoiceBus.emit(7) { it.onAssistantDelta("two", 2) }
        VoiceBus.emit(7) { it.onAssistantDelta("more", 1) }
        VoiceBus.replay(t, t.seen)
        assertEquals(listOf("you: first", "agent: one more", "you: second", "agent: two"), t.lines())
    }

    @Test fun aNewConnectionStartsItsTurnsAfresh() {
        val t = fresh()
        VoiceBus.emit(8) { it.onAssistantDelta("old", 1) }
        VoiceBus.emit(9) { it.onAssistantDelta("new", 1) }    // turn 1 again, but a new session
        VoiceBus.replay(t, t.seen)
        assertEquals(listOf("agent: old", "agent: new"), t.lines())
    }

    @Test fun anInterruptedReplyFadesAndAnEmptyReplyIsHidden() {
        val t = fresh()
        VoiceBus.emit(10) { it.onAssistantDelta("I was say", 1) }
        VoiceBus.emit(10) { it.onInterrupt() }
        VoiceBus.emit(10) { it.onAssistantDelta("", 2) }
        VoiceBus.replay(t, t.seen)
        assertEquals(listOf("agent: I was say (cut)"), t.lines())
    }

    @Test fun keepsOnlyTheNewestBubbles() {
        val t = fresh(capacity = 3)
        for (i in 1..5) VoiceBus.emit(11) { it.onTranscript("line $i", i) }
        VoiceBus.replay(t, t.seen)
        assertEquals(listOf("you: line 3", "you: line 4", "you: line 5"), t.lines())
        assertTrue(t.snapshot().map { it.id }.zipWithNext().all { (a, b) -> a < b })
    }

    @Test fun everyLoggedEventIsAnnounced() {
        var told = 0
        VoiceBus.onLogged = { told++ }
        VoiceBus.emit { it.onSpoken("hi") }
        VoiceBus.record { it.onUserText("typed") }
        assertEquals(2, told)
    }
}
