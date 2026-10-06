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
    }
}
