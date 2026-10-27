package systems.bake.rook
import org.json.JSONObject
import org.junit.Assert.*
import org.junit.Test

class SpeakerDebugTest {
    @Test fun confidentMatch() {
        val m = JSONObject("""{"type":"speaker","label":"Bake","heard":"Bake","score":0.5523,"threshold":0.45,
            "basis":"voice","clip":{"id":3,"pinned":true,"setting":"enrollment"}}""")
        assertEquals("speaker: Bake · score 0.55 / 0.45 · closest clip: enrollment", SpeakerDebug.line(m))
    }
    @Test fun weakMatchOnHisPhoneIsHimAndLearned() {
        val m = JSONObject("""{"label":"Bake","heard":"guest","score":0.3349,"threshold":0.45,"basis":"owner phone",
            "learned":12,"skipped":null,"norm":null}""")
        assertEquals("speaker: Bake · score 0.33 / 0.45 · heard as unknown · yours by default (your phone) · learned (clip 12)",
            SpeakerDebug.line(m))
    }
    @Test fun someoneElseAndSkippedAndDropped() {
        val m = JSONObject("""{"label":"guest","heard":"guest","score":0.05,"threshold":0.45,"basis":"voice",
            "skipped":"quality","dropped":"quiet"}""")
        assertEquals("speaker: someone else · score 0.05 / 0.45 · not learned: quality · ignored: quiet", SpeakerDebug.line(m))
        assertNull(SpeakerDebug.line(JSONObject("""{"type":"speaker"}""")))
    }
}
