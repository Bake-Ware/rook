package systems.bake.rook

import org.json.JSONObject
import org.junit.Assert.*
import org.junit.Test

class DecisionTest {
    private fun event(turn: Int = 7, source: String = "text") = Decision.parse(JSONObject("""
        {"type":"decision","turn":$turn,"source":"$source","mode":"shadow","status":"ok",
         "latency_ms":45.2,"engine":{"model":"router","adapter":"v1","calibration":null},
         "answers":[{"id":"intent","type":"choice","choice":"device_control","probabilities":{"device_control":0.94,"chat":0.06},"confidence":0.9},
         {"id":"needs_confirmation","type":"noul","p":0.02,"confidence":0.98},
         {"id":"urgency","type":"score","level":1,"expected":1.3,"probabilities":{"1":0.7,"2":0.3}},
         {"id":"future","type":"choice","choice":"ignored"}]}
    """))!!

    @Test fun parsesContractAndIgnoresUnknownAnswers() {
        val d = event()
        assertEquals(3, d.answers.size)
        assertEquals("thinking: device_control 94% · confirm no · 45 ms", d.summary())
        assertEquals("router", d.model)
        assertEquals("v1", d.adapter)
        assertNull(d.calibration)
        assertTrue(d.details().contains("chat: 6%"))
        assertTrue(d.details().contains("expected=1.3"))
        assertTrue(d.details().contains("confidence: 90%"))
    }
    @Test fun toleratesNonOkAndNullFields() {
        for (status in listOf("timeout", "error", "disabled")) {
            val d = Decision.parse(JSONObject("""{"type":"decision","turn":2,"status":"$status","latency_ms":null,"engine":null,"answers":[],"error":"reason"}"""))!!
            assertEquals("thinking: $status", d.summary())
            assertTrue(d.answers.isEmpty())
            assertTrue(d.details().contains("error: reason"))
        }
    }
    @Test fun malformedAndUnknownEventsAreHarmless() {
        assertNull(Decision.parse(JSONObject("""{"type":"future","turn":7}""")))
        for (turn in listOf("null", "-1", "1.5", "\"oops\"", "2147483648"))
            assertNull(Decision.parse(JSONObject("""{"type":"decision","turn":$turn}""")))
        val d = Decision.parse(JSONObject("""{"type":"decision","turn":0,"answers":[null,3,{"id":"intent","type":"future"},{"id":"is_correction","type":"noul","p":4}]}"""))!!
        assertEquals(1, d.answers.size)
        assertNull(d.answers.single().p)
    }
    @Test fun attachesBeforeAndAfterReplyOnlyToMatchingTurn() {
        val got = mutableMapOf<String, Decision>()
        val index = DecisionAttachments<String> { message, d -> got[message] = d }
        index.decision(event(7))
        index.message(8, "eight")
        assertTrue(got.isEmpty())
        index.message(7, "seven")
        assertEquals(7, got["seven"]?.turn)
        index.decision(event(8))
        assertEquals(8, got["eight"]?.turn)
    }
    @Test fun voiceUsesUtteranceAndTextUsesAssistant() {
        val got = mutableListOf<String>()
        val index = DecisionAttachments<String> { message, _ -> got += message }
        index.message(7, "user", voice = true)
        index.message(7, "assistant")
        index.decision(event(source = "voice"))
        assertEquals(listOf("user"), got)
        index.decision(event())
        assertEquals(listOf("user", "assistant"), got)
    }
    @Test fun earlyVoiceDecisionWaitsForUtteranceWithoutDuplicatingRow() {
        val got = mutableListOf<String>()
        val index = DecisionAttachments<String> { message, _ -> got += message }
        index.decision(event(source = "voice"))
        index.message(7, "assistant")
        assertTrue(got.isEmpty())
        index.message(7, "user", voice = true)
        assertEquals(listOf("user"), got)
    }
    @Test fun allKnownAnswerTypesAndProbabilitiesAreRetained() {
        val d = Decision.parse(JSONObject("""{"type":"decision","turn":1,"source":"voice","answers":[
          {"id":"needs_response","type":"noul","p":0.97},
          {"id":"context_source","type":"choice","choice":"none","probabilities":{"none":0.8,"history":0.2,"invalid":2}},
          {"id":"is_correction","type":"noul","p":0.03,"confidence":0.95}]}"""))!!
        assertEquals(3, d.answers.size)
        assertEquals(0.97, d.answers[0].p!!, 0.001)
        assertEquals(mapOf("none" to 0.8, "history" to 0.2), d.answers[1].probabilities)
        assertEquals(0.03, d.answers[2].p!!, 0.001)
    }
    @Test fun connectionResetPreventsCrossSessionAttachment() {
        val got = mutableListOf<String>()
        val index = DecisionAttachments<String> { message, _ -> got += message }
        index.message(7, "old")
        index.clear()
        index.decision(event())
        assertTrue(got.isEmpty())
        index.message(7, "new")
        assertEquals(listOf("new"), got)
    }
}
