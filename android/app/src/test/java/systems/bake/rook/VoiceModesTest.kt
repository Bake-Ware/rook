package systems.bake.rook
import org.junit.Assert.*
import org.junit.Test

class VoiceModesTest {
    @Test fun unknownModeFallsBackToAssistant() {
        assertEquals("assistant", VoiceModes.byId(null).id)
        assertEquals("assistant", VoiceModes.byId("bogus").id)
        assertEquals("listen", VoiceModes.byId("listen").id)
        assertEquals(VoiceModes.ALL.map { it.id }.distinct().size, VoiceModes.ALL.size)
    }
    @Test fun defaultPromptIsStoredBlankAndEditsAreCapped() {
        val talk = VoiceModes.byId("conversation")
        assertEquals("", VoiceModes.storedPrompt(talk, "  " + talk.defaultPrompt + "\n"))
        assertEquals("Talk like a pirate.", VoiceModes.storedPrompt(talk, " Talk like a pirate. "))
        assertEquals(VoiceModes.MAX_PROMPT, VoiceModes.storedPrompt(talk, "x".repeat(5000)).length)
        assertEquals(talk.defaultPrompt, VoiceModes.shownPrompt(talk, ""))
        assertEquals("custom", VoiceModes.shownPrompt(talk, "custom"))
    }
    @Test fun sessionModeMustMatchWhatWasAsked() {
        assertTrue(VoiceModes.sessionModeMatches("conversation", "conversation"))
        assertTrue(VoiceModes.sessionModeMatches("assistant", "assistant"))
        // An old server sends no mode and runs the assistant: fine only if that was asked for.
        assertTrue(VoiceModes.sessionModeMatches("assistant", null))
        assertFalse(VoiceModes.sessionModeMatches("conversation", null))
        assertFalse(VoiceModes.sessionModeMatches("conversation", ""))
        assertFalse(VoiceModes.sessionModeMatches("conversation", "assistant"))
        assertFalse(VoiceModes.sessionModeMatches("dictate", "listen"))
        assertTrue(VoiceModes.mismatchMessage("conversation", null).contains("\"assistant\" instead of \"conversation\""))
    }
    @Test fun assistantKeepsHistoricalConversationScope() {
        assertEquals("wss://v/ws\u0000tok", VoiceModes.conversationScope("wss://v/ws", "tok", "assistant"))
        assertEquals("wss://v/ws\u0000tok", VoiceModes.conversationScope("wss://v/ws", "tok", "unknown"))
        assertNotEquals(VoiceModes.conversationScope("wss://v/ws", "tok", "conversation"),
            VoiceModes.conversationScope("wss://v/ws", "tok", "roleplay"))
    }
}
