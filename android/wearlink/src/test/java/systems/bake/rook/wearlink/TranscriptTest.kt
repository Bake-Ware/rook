package systems.bake.rook.wearlink

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test
import systems.bake.rook.wearlink.Transcript.Line
import systems.bake.rook.wearlink.Transcript.Who

class TranscriptTest {
    @Test fun replyPiecesJoinOneLineUntilDone() {
        val t = Transcript().heard("what time is it").reply("It is").reply("half past three.").done().reply("Anything else?")
        assertEquals(listOf(
            Line(Who.USER, "what time is it"),
            Line(Who.ROOK, "It is half past three."),
            Line(Who.ROOK, "Anything else?"),
        ), t.lines)
        assertTrue(t.open)
    }

    @Test fun heardClosesTheReply() {
        val t = Transcript().reply("Sure.").heard("thanks").reply("Welcome.")
        assertEquals(listOf("Sure.", "thanks", "Welcome."), t.lines.map { it.text })
    }

    @Test fun noteStandsAloneAndBlankIsIgnored() {
        val t = Transcript().reply("Working").note("Lost the phone").reply("on it").heard("  ").reply(" ")
        assertEquals(listOf(Who.ROOK, Who.NOTE, Who.ROOK), t.lines.map { it.who })
        assertEquals("on it", t.lines.last().text)
    }

    @Test fun aRepeatedNoteIsNotStacked() {
        val t = Transcript().note("Can't reach the phone").note("Can't reach the phone")
        assertEquals(1, t.lines.size)
        val u = t.heard("hi").note("Can't reach the phone").note("Lost the phone").note("Lost the phone")
        assertEquals(listOf("Can't reach the phone", "hi", "Can't reach the phone", "Lost the phone"), u.lines.map { it.text })
    }

    @Test fun keepsTheNewestLines() {
        var t = Transcript(capacity = 3)
        for (i in 1..5) t = t.heard("line $i")
        assertEquals(listOf("line 3", "line 4", "line 5"), t.lines.map { it.text })
    }

    @Test fun historyRoundTripsWithoutNotes() {
        val t = Transcript().heard("hi").reply("Hello.").note("error").heard("bye")
        val back = Transcript.fromJson(org.json.JSONObject(t.toJson().toString()))
        assertEquals(listOf(Line(Who.USER, "hi"), Line(Who.ROOK, "Hello."), Line(Who.USER, "bye")), back.lines)
        assertFalse(back.open)
    }

    @Test fun historyFitsTheBudgetKeepingTheNewest() {
        var t = Transcript(capacity = 100)
        for (i in 1..100) t = t.heard("x".repeat(1000) + i)
        val json = t.toJson(maxBytes = 10_000)
        val back = Transcript.fromJson(json, capacity = 100)
        assertTrue(json.toString().toByteArray().size <= 10_000)
        assertTrue(back.lines.size in 5..9)
        assertTrue(back.lines.last().text.endsWith("100"))
    }

    @Test fun longLinesKeepTheirEnd() {
        val t = Transcript().reply("a".repeat(5000) + "END")
        val line = Transcript.fromJson(t.toJson()).lines.single().text
        assertEquals(Transcript.MAX_LINE, line.length)
        assertTrue(line.startsWith("…") && line.endsWith("END"))
    }
}
