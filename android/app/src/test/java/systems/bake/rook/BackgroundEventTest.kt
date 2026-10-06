package systems.bake.rook

import org.json.JSONObject
import org.junit.Assert.*
import org.junit.Test

class BackgroundEventTest {
    private fun ev(json: String) = BackgroundEvent.parse(JSONObject(json))

    @Test fun parsesStepsAndRejectsMalformed() {
        val e = ev("""{"type":"background","turn":4,"seq":9,"ts":1700000000000,"kind":"tool_call","text":"Checking your calendar","tool":"calendar.list","args":{"limit":5},"status":"ok","elapsed_ms":120.7}""")!!
        assertEquals(4, e.turn); assertEquals(9L, e.seq); assertEquals("calendar.list", e.tool)
        assertTrue(e.args!!.contains("\"limit\": 5")); assertEquals(120L, e.elapsedMs); assertFalse(e.failed)
        val r = ev("""{"type":"background","turn":4,"seq":10,"kind":"tool_result","result":"${"x".repeat(2500)}","status":"failed"}""")!!
        assertEquals(BackgroundEvent.MAX_RESULT, r.result!!.length); assertTrue(r.failed); assertEquals("tool result", r.text)
        for (bad in listOf("""{"type":"background","turn":1,"seq":1,"kind":"future"}""", """{"type":"background","turn":1.5,"seq":1,"kind":"start"}""",
                """{"type":"background","turn":1,"kind":"start"}""", """{"type":"activity","turn":1,"seq":1,"kind":"start"}"""))
            assertNull(bad, ev(bad))
    }

    @Test fun groupsByTurnDropsReplaysAndTracksRunning() {
        val t = BackgroundTimeline(maxTurns = 2)
        fun add(conn: Long, turn: Int, seq: Long, kind: String) = t.add(conn, BackgroundEvent(turn, seq, 0, kind, kind, null, null, null, null, null))
        assertTrue(add(1, 1, 1, "start")); assertFalse(add(1, 1, 1, "start"))
        assertTrue(t.running(TurnKey(1, 1)))
        assertTrue(add(1, 1, 2, "done")); assertFalse(t.running(TurnKey(1, 1)))
        assertTrue(add(2, 1, 1, "start"))       // a new connection restarts seq
        assertTrue(add(2, 2, 2, "start"))
        assertEquals(listOf(TurnKey(2, 1), TurnKey(2, 2)), t.turns.keys.toList())
        assertEquals(4L, t.version)
    }

    @Test fun capsStepsPerTurnKeepingTheNewest() {
        val t = BackgroundTimeline(maxTurns = 2, maxSteps = 200)
        fun step(turn: Int, seq: Long, kind: String = "thought") = BackgroundEvent(turn, seq, 0, kind, "s$seq", null, null, null, null, null)
        for (seq in 1L..250L) t.add(1, step(1, seq))
        val steps = t.turns[TurnKey(1, 1)]!!
        assertEquals(200, steps.size)
        assertEquals(51L, steps.first().seq); assertEquals(250L, steps.last().seq)
        assertEquals(50, t.truncated[TurnKey(1, 1)])
        assertTrue(t.running(TurnKey(1, 1)))
        t.add(1, step(1, 251, "done"))
        assertFalse(t.running(TurnKey(1, 1)))                  // the newest step decides, even after truncation
        assertEquals(0L, t.lastFailure)
        t.add(1, step(2, 252, "error")); assertEquals(t.version, t.lastFailure)
        t.add(1, step(3, 253))                                  // turn 1 falls out with its truncation count
        assertEquals(listOf(TurnKey(1, 2), TurnKey(1, 3)), t.turns.keys.toList())
        assertNull(t.truncated[TurnKey(1, 1)])
    }

    @Test fun backgroundStepsStayOutOfTheConversationLog() {
        val before = VoiceBus.background.version
        // No screen attached: the step is kept for the Background tab, the conversation log is untouched.
        VoiceBus.listener = null
        val gen = VoiceBus.connectionGeneration + 1000
        VoiceBus.emitBackground(gen, BackgroundEvent(1, 1, 0, "start", "start", null, null, null, null, null))
        assertEquals(before + 1, VoiceBus.background.version)
        var replayed = 0
        VoiceBus.attach(object : VoiceBus.Listener {
            override fun onState(state: String) {}
            override fun onTranscript(text: String) {}
            override fun onAssistantDelta(text: String) {}
            override fun onAssistantDone() {}
            override fun onInterrupt() {}
            override fun onError(msg: String) {}
            override fun onBackground(event: BackgroundEvent) { replayed++ }
        }, 0)
        VoiceBus.listener = null
        assertEquals(0, replayed)
        assertTrue(VoiceBus.background.turns.containsKey(TurnKey(gen, 1)))
    }
}
