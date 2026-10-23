package systems.bake.rook

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test

class VoiceModelsTest {
    private val raw = """{"ok": true, "front": {"model": "claude-haiku-5-5", "effort": "low"},
        "back": {"model": "opus", "effort": ""},
        "choices": {"front": [{"id": "claude-haiku-5-5", "label": "Haiku 5.5", "note": "Default."},
                              {"id": "local", "label": "Local (kaiju)", "note": "Your own GPU."}],
                    "back": [{"id": "opus", "label": "Opus", "note": "Most capable."},
                             {"id": "sonnet", "label": "Sonnet", "note": "Quicker."}],
                    "efforts": ["", "low", "high"]}}"""

    @Test fun parsesTheHubsAnswer() {
        val s = VoiceModels.parse(raw)!!
        assertEquals("claude-haiku-5-5", s.front); assertEquals("low", s.frontEffort)
        assertEquals("opus", s.back); assertEquals("", s.backEffort)
        assertEquals(listOf("claude-haiku-5-5", "local"), s.fronts.map { it.id })
        assertEquals("Quicker.", VoiceModels.note(s.backs, "sonnet"))
        assertEquals("default", VoiceModels.effortLabel(""))
        assertNull(s.error)
        assertNull(VoiceModels.parse("not json")); assertNull(VoiceModels.parse(null))
        val refused = VoiceModels.parse(raw.replace("\"ok\": true", "\"ok\": false, \"error\": \"only the owner\""))!!
        assertEquals("only the owner", refused.error)
    }

    @Test fun onlyWhatChangedIsSent() {
        val s = VoiceModels.parse(raw)!!
        assertEquals(0, VoiceModels.changes(s, "claude-haiku-5-5", "low", "opus", "").length())
        val c = VoiceModels.changes(s, "local", "low", "sonnet", "high")
        assertEquals("local", c.getString("front")); assertEquals("sonnet", c.getString("back"))
        assertEquals("high", c.getString("back_effort")); assertEquals(false, c.has("front_effort"))
    }
}
