package systems.bake.rook

import org.json.JSONObject
import org.junit.Assert.*
import org.junit.Test

class ActivityEventTest {
    private fun event(phase: String = "planning", turn: Int = 1, seq: Long = 1, elapsed: Long? = null) =
        ActivityEvent(turn, seq, 1700000000000, phase, phase, null, null, "Bakephone", "device.info", elapsed, 30000, null)
    @Test fun parsesOptionalFieldsAndRejectsUnknownOrMalformedEvents() {
        val parsed = ActivityEvent.parse(JSONObject("""{"type":"activity","turn":3,"seq":7,"ts":1234,"phase":"tool_result","label":"Finished","detail":"offline","worker":"phone","cap":"device.info","status":"failed","elapsed_ms":23.5,"timeout_ms":1000}"""))!!
        assertTrue(parsed.failed); assertEquals(23L, parsed.elapsedMs); assertEquals("offline", parsed.detail)
        for (json in listOf("""{"type":"future"}""", """{"type":"activity","turn":1,"seq":1,"ts":1,"phase":"future"}""", """{"type":"activity","turn":1.2,"seq":1,"ts":1,"phase":"planning"}""")) assertNull(ActivityEvent.parse(JSONObject(json)))
    }
    @Test fun groupsTurnsAndIsolatesConnectionsAndIgnoresRepeatedSequence() {
        val timeline = ActivityTimeline()
        assertTrue(timeline.add(1, event(seq=1)))
        assertFalse(timeline.add(1, event(seq=1)))
        assertTrue(timeline.add(1, event(turn=2, seq=2)))
        assertTrue(timeline.add(1, event(turn=1, seq=3)))
        assertTrue(timeline.add(2, event(seq=1)))
        assertEquals(3, timeline.turns.size)
        assertEquals(2, timeline.turns[TurnKey(1,1)]!!.size)
        assertEquals(1, timeline.turns[TurnKey(2,1)]!!.size)
    }
    @Test fun stallsAt15And45SecondsButHeartbeatsResetSilence() {
        val s = TurnStatus(); s.event(event(), 1000)
        assertEquals(0, s.display(15999)!!.severity)
        assertEquals(1, s.display(16000)!!.severity)
        assertEquals(2, s.display(46000)!!.severity)
        s.event(event("tool_wait", seq=2, elapsed=44000), 46000)
        assertEquals(0, s.display(47000)!!.severity)
        assertEquals("Waiting on Bakephone - 45s", s.display(47000)!!.text)
        s.event(event("done", seq=3), 48000)
        assertNull(s.display(100000))
    }
    @Test fun oldServerNeverShowsStallAndResetClearsSupport() {
        val s = TurnStatus(); s.legacyState("thinking", 0)
        assertEquals(0, s.display(90000)!!.severity)
        s.finishLegacy(); assertNull(s.display(90000))
        s.event(event(), 0); s.reset(); s.begin(0,"Listening")
        assertEquals("Listening", s.display(90000)!!.text)
    }
    @Test fun lateOldDoneAndLateMetadataDoNotChangeCurrentTurn() {
        val s = TurnStatus(); s.event(event(turn=2), 0)
        s.event(event("done", turn=1), 10)
        assertNotNull(s.display(20))
        s.event(event("done", turn=2), 30)
        s.event(event("planned", turn=2), 40)
        assertNull(s.display(50))
    }
    @Test fun unseenFailurePersistsUntilViewed() {
        val badge=UnseenItems(); badge.add(true,false); badge.add(false,false)
        assertEquals(2,badge.count); assertTrue(badge.failed)
        badge.seen(); badge.add(true,true)
        assertEquals(0,badge.count); assertFalse(badge.failed)
    }
    @Test fun engineStatusesAndNewFieldsRenderWithoutAnswersWhenNotOk() {
        for ((status,label) in mapOf("ok" to "ok", "disabled" to "engine off", "skipped" to "skipped", "timeout" to "timed out", "error" to "error")) {
            val d=Decision.parse(JSONObject("""{"type":"decision","turn":1,"engine_status":"$status","status":"ok","elapsed_ms":5.5,"model":"router","adapter":"a","reason":"reason","mode":"shadow","answers":[{"id":"intent","type":"choice","choice":"chat","confidence":0.8}]}"""))!!
            assertEquals(label,d.engineLabel); assertEquals(5.5,d.latencyMs!!,0.001)
            assertEquals("router",d.model); assertEquals("a",d.adapter); assertEquals("reason",d.error)
            assertEquals(if(status=="ok") 1 else 0,d.answers.size)
            assertEquals(status in listOf("error","timeout"),d.failed)
        }
    }
}
