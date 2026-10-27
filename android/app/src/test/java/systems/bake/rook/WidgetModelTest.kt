package systems.bake.rook

import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class WidgetModelTest {
    private fun card(id: String, created: Double, extra: String = "", local: Boolean = false) =
        AttentionCard.fromPost(JSONObject("""{"id":"$id","line":"Line $id"$extra}"""), created, local = local)

    @Test fun talkFollowsTheVoiceState() {
        assertEquals(WidgetModel.Talk("Talk", false), WidgetModel.talk("idle"))
        assertEquals(WidgetModel.Talk("Talk", false), WidgetModel.talk("standby"))
        assertEquals(WidgetModel.Talk("Listening", true), WidgetModel.talk("listening"))
        assertEquals(WidgetModel.Talk("Thinking", true), WidgetModel.talk("thinking"))
        assertEquals(WidgetModel.Talk("Tap to stop", true), WidgetModel.talk("speaking"))
        assertEquals(WidgetModel.Talk("Connecting", true), WidgetModel.talk("reconnecting"))
        assertEquals(WidgetModel.Talk("Talk", false), WidgetModel.talk("something new"))
    }

    @Test fun urgentAndFailedFirstThenOldest() {
        val failed = card("f", 30.0).apply { state = "failed" }
        val w = WidgetModel.waiting(listOf(card("new", 20.0), card("old", 10.0), card("u", 40.0, ""","channel":"urgent""""), failed), 10)
        assertEquals(listOf("f", "u", "old", "new"), w.rows.map { it.id })
        assertEquals(listOf(true, true, false, false), w.rows.map { it.loud })
        assertEquals(4, w.total); assertEquals(0, w.more)
    }

    @Test fun localAlertsAreLeftOut() {
        val alert = card("n1", 5.0, ""","kind":"alert","channel":"urgent","actions":["ack"]""", local = true)
        val w = WidgetModel.waiting(listOf(alert, card("a", 10.0)), 10)
        assertEquals(listOf("a"), w.rows.map { it.id })
        assertEquals(1, w.total)
    }

    @Test fun overflowCountsWhatDoesNotFit() {
        val w = WidgetModel.waiting((1..5).map { card("c$it", it.toDouble()) }, 2)
        assertEquals(listOf("c1", "c2"), w.rows.map { it.id })
        assertEquals(5, w.total); assertEquals(3, w.more)
        val none = WidgetModel.waiting(emptyList(), 3)
        assertTrue(none.rows.isEmpty()); assertEquals(0, none.total); assertEquals(0, none.more)
    }

    @Test fun rowTextNamesTheKind() {
        assertEquals("Line a", WidgetModel.rowText(card("a", 0.0)))
        assertEquals("Question · Line q", WidgetModel.rowText(card("q", 0.0, ""","kind":"ask"""")))
        assertEquals("Approval · Line h", WidgetModel.rowText(card("h", 0.0, ""","kind":"hold"""")))
        assertEquals("Job · Line j", WidgetModel.rowText(card("j", 0.0, ""","kind":"job"""")))
        assertEquals("Alert · Line l", WidgetModel.rowText(card("l", 0.0, ""","kind":"alert"""")))
        val failed = card("x", 0.0, ""","kind":"hold"""").apply { state = "failed" }
        assertEquals("Didn’t go through: Line x", WidgetModel.rowText(failed))
        assertFalse(WidgetModel.rowText(card("y", 0.0)).contains("·"))
    }

    @Test fun rowsFitTheHeight() {
        assertEquals(1, WidgetModel.rowsFor(40))
        assertEquals(1, WidgetModel.rowsFor(70))
        assertEquals(2, WidgetModel.rowsFor(110))
        assertEquals(4, WidgetModel.rowsFor(160))
        assertEquals(6, WidgetModel.rowsFor(800))
    }
}
