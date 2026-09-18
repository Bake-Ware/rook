package systems.bake.rook
import org.json.JSONObject
import org.junit.Assert.*
import org.junit.Test

class BubbleDecisionsTest {
    private val key=TurnKey(1,7)
    private val d=Decision.parse(JSONObject("""{"type":"decision","turn":7,"engine_status":"ok","answers":[]}"""))!!
    @Test fun noDecisionMeansNoMarkerAndNoExpansion() {
        val a=BubbleDecisions<String>(); a.register(key,"bot",false)
        assertNull(a.decision(key,"bot")); a.toggle(key,"bot"); assertFalse(a.isExpanded("bot"))
    }
    @Test fun earlyAndLateArrivalAttachOnlyToMatchingTurn() {
        for (early in listOf(true,false)) {
            val a=BubbleDecisions<String>()
            if(early) a.receive(key,d)
            a.register(key,"bot",false); a.register(TurnKey(1,8),"other",false)
            if(!early) a.receive(key,d)
            assertEquals(d,a.decision(key,"bot")); assertNull(a.decision(TurnKey(1,8),"other"))
        }
    }
    @Test fun quietTurnUsesUserThenMigratesToLateAssistant() {
        val a=BubbleDecisions<String>(); a.receive(key,d); a.register(key,"user",true)
        assertEquals(d,a.decision(key,"user")); a.toggle(key,"user")
        a.register(key,"bot",false)
        assertNull(a.decision(key,"user")); assertEquals(d,a.decision(key,"bot"))
        assertTrue(a.isExpanded("bot")); assertFalse(a.isExpanded("user"))
    }
    @Test fun expansionSurvivesRebindingAndDoesNotLeakAcrossTurnsOrConnections() {
        val a=BubbleDecisions<String>(); a.register(key,"bot",false); a.receive(key,d); a.toggle(key,"bot")
        repeat(3) { a.register(key,"bot",false); a.receive(key,d) }
        a.register(TurnKey(2,7),"next-session",false)
        assertTrue(a.isExpanded("bot")); assertFalse(a.isExpanded("next-session"))
        assertNull(a.decision(TurnKey(2,7),"next-session"))
        a.toggle(key,"bot"); assertFalse(a.isExpanded("bot"))
    }
    @Test fun cardJumpExpandsMatchingBubbleEvenIfItArrivesLater() {
        val a=BubbleDecisions<String>(); a.receive(key,d)
        assertNull(a.expand(key)); a.register(key,"user",true)
        assertTrue(a.isExpanded("user")); assertEquals("user",a.expand(key))
    }
}
