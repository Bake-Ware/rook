package systems.bake.rook

import org.json.JSONArray
import org.json.JSONObject
import org.junit.Assert.*
import org.junit.Test

class AttentionModelTest {
    private fun card(id: String, now: Double = 100.0, extra: String = "") =
        AttentionCard.fromPost(JSONObject("""{"id":"$id","line":"Deploy it?"$extra}"""), now)

    @Test fun postDefaultsAndValidation() {
        val c = card("a")
        assertEquals(listOf("yes", "no", "explain"), c.actions)
        assertEquals("approvals", c.channel)
        assertTrue(c.takeover); assertFalse(c.urgent)
        assertEquals(listOf("ack"), card("b", extra = ""","kind":"alert"""").actions)
        assertTrue(card("c", extra = ""","channel":"urgent"""").urgent)
        assertFalse(card("d", extra = ""","channel":"digest"""").takeover)
        assertFalse(card("e", extra = ""","actions":["ack"]""").takeover)
        // A decision the hub queued is a normal notification (rook_waiting), not a takeover.
        assertTrue(card("w", extra = ""","channel":"digest"""").waiting)
        assertFalse(card("w2", extra = ""","channel":"digest","actions":["ack"]""").waiting)
        assertFalse(card("w3").waiting)
        assertFalse(card("w4", extra = ""","channel":"digest","urgency":"urgent"""").waiting)
        assertEquals(140, AttentionCard.fromPost(JSONObject().put("id", "f").put("line", "x".repeat(200)), 0.0).line.length)
        for (bad in listOf("""{"id":"","line":"l"}""", """{"id":"a"}""", """{"id":"a","line":"l","kind":"nope"}""",
                """{"id":"a","line":"l","channel":"sms"}""", """{"id":"a","line":"l","actions":["maybe"]}""",
                """{"id":"${"i".repeat(65)}","line":"l"}""")) {
            try { AttentionCard.fromPost(JSONObject(bad), 0.0); fail(bad) } catch (_: IllegalArgumentException) {}
        }
    }

    @Test fun bodyKeepsShortLines() {
        assertEquals("", card("a").body)
        val c = AttentionCard.fromPost(JSONObject().put("id", "b").put("line", "Read hub approvals database")
            .put("body", "For: Approval cards plain (task)\nWho:   Claude web\n\n  I want to: run a query \nRisk: Low\n"), 0.0)
        assertEquals("For: Approval cards plain (task)\nWho: Claude web\nI want to: run a query\nRisk: Low", c.body)
        val many = AttentionCard.cleanBody((0 until 9).joinToString("\n") { "line $it" })
        assertEquals((0 until 5).map { "line $it" } + "line 5…", many.split("\n"))
        val long = AttentionCard.cleanBody("w ".repeat(400))
        assertEquals(AttentionCard.BODY_MAX, long.length); assertTrue(long.endsWith("…"))
        // Persisted with the card; cards stored before body existed load with an empty one.
        assertEquals(c.body, AttentionCard.fromJson(c.toJson()).body)
        assertEquals("", AttentionCard.fromJson(card("old").toJson().apply { remove("body") }).body)
        val book = CardBook(); book.post(card("r"))
        book.post(AttentionCard.fromPost(JSONObject().put("id", "r").put("line", "Deploy it?").put("body", "Risk: Low"), 1.0))
        assertEquals("Risk: Low", book["r"]!!.body)
        assertEquals("Risk: Low", CardBook().apply { load(book.toJson()) }["r"]!!.body)
    }

        @Test fun repostUpdatesWithoutRinging() {
        val book = CardBook()
        assertTrue(book.post(card("a", 1.0)))
        book["a"]!!.seen = true
        assertFalse(book.post(AttentionCard.fromPost(JSONObject("""{"id":"a","line":"Deploy 857?"}"""), 9.0)))
        assertEquals("Deploy 857?", book["a"]!!.line)
        assertTrue(book["a"]!!.seen)
        assertEquals(1.0, book["a"]!!.created, 0.0)
    }

    @Test fun updatesAndShowingOrder() {
        val book = CardBook()
        book.post(card("late", 5.0)); book.post(card("early", 1.0)); book.post(card("mid", 3.0, ""","expires_at":50"""))
        assertEquals(listOf("early", "mid", "late"), book.showing(10.0).map { it.id })
        assertEquals("failed", book.update("early", "failed", "hub said no")!!.state)
        assertEquals("hub said no", book["early"]!!.message)
        assertEquals(listOf("mid", "late"), book.takeovers(10.0).map { it.id })
        book["mid"]!!.dismissed = true
        assertEquals(listOf("late"), book.takeovers(10.0).map { it.id })
        assertEquals("done", book.update("late", "done", "")!!.state)
        assertNull(book["late"])
        assertNull(book.update("nope", "gone", ""))
        assertEquals(listOf("mid"), book.expire(60.0).map { it.id })
        assertEquals(listOf("early"), book.showing(60.0).map { it.id })
        val copy = CardBook().apply { load(book.toJson()) }
        assertEquals("hub said no", copy["early"]!!.message)
    }

    @Test fun bookIsCapped() {
        val book = CardBook(cap = 3)
        (1..5).forEach { book.post(card("c$it", it.toDouble())) }
        assertEquals(3, book.size)
        assertNull(book["c1"])
    }

    @Test fun eventLogPollsBySeq() {
        val log = EventLog(cap = 3)
        log.add("unlocked", now = 1.0)
        log.add("decision", JSONObject().put("id", "a").put("decision", "approve"), 2.0)
        var r = log.poll(0)
        assertEquals(2, r.getJSONArray("events").length())
        assertEquals(2L, r.getLong("last"))
        assertEquals("approve", r.getJSONArray("events").getJSONObject(1).getString("decision"))
        r = log.poll(2)
        assertEquals(0, r.getJSONArray("events").length()); assertEquals(2L, r.getLong("last"))
        (3..6).forEach { log.add("seen", JSONObject().put("id", "x$it"), it.toDouble()) }
        r = log.poll(2)
        assertTrue(r.optBoolean("gap"))
        assertEquals(listOf(4L, 5L, 6L), (0 until 3).map { r.getJSONArray("events").getJSONObject(it).getLong("seq") })
        // A hub ahead of the phone (data cleared): start over.
        r = log.poll(99)
        assertTrue(r.optBoolean("reset")); assertEquals(6L, r.getLong("last"))
        val page = EventLog().apply { (1..5).forEach { add("seen", now = 0.0) } }.poll(0, limit = 2)
        assertTrue(page.optBoolean("more")); assertEquals(2L, page.getLong("last"))
        // seq survives a reload and keeps counting.
        val copy = EventLog(cap = 3).apply { load(log.toJson()) }
        assertEquals(7L, copy.add("unlocked", now = 7.0).getLong("seq"))
    }

    @Test fun feedCapsAgesAndCountsIgnored() {
        val day = 86400.0
        val feed = QuietFeed(cap = 3)
        feed.add(JSONObject().put("summary", "old").put("ts", 0.0), 0.0)
        (1..3).forEach { feed.add(JSONObject().put("summary", "s$it").put("ts", 7 * day + it), 7 * day + it) }
        assertEquals(listOf("s3", "s2", "s1"), feed.recent(10).map { it.getString("summary") })
        feed.ignore(0.0); feed.ignore(8 * day); feed.ignore(8 * day)
        assertEquals(2, feed.ignoredCount())
        feed.prune(20 * day)
        assertEquals(0, feed.size); assertEquals(0, feed.ignoredCount())
        val copy = QuietFeed().apply { load(JSONObject().put("entries", JSONArray().put(JSONObject().put("summary", "k").put("ts", 1.0))), 2.0) }
        assertEquals(1, copy.size)
    }

    @Test fun answeredCardsLeaveTheStackAndComeBackWhenRefused() {
        val book = CardBook()
        listOf("a" to 1.0, "b" to 2.0, "c" to 3.0).forEach { (id, t) -> book.post(card(id, now = t)) }
        assertEquals(listOf("a", "b", "c"), book.stack(10.0).map { it.id })
        // Yes on "a": out of the card screen's stack at once, still in the book (and the app list).
        book["a"]!!.apply { state = "sent"; decision = "approve"; sentAt = 10.0 }
        assertEquals(listOf("b", "c"), book.stack(11.0).map { it.id })
        assertEquals(listOf("a", "b", "c"), book.showing(11.0).map { it.id })
        // The hub refused it: back, in its place, with the message.
        book.update("a", "failed", "Not done: approvals are unavailable")
        assertEquals(listOf("a", "b", "c"), book.stack(12.0).map { it.id })
        // Confirmed: gone for good.
        book["b"]!!.apply { state = "sent"; sentAt = 12.0 }
        book.update("b", "done", "Approved")
        assertEquals(listOf("a", "c"), book.stack(13.0).map { it.id })
        // Never confirmed: offered again after a few minutes, and the sent time survives a restart.
        book["c"]!!.apply { state = "sent"; sentAt = 20.0 }
        assertFalse(book["c"]!!.unconfirmed(20.0 + AttentionCard.SENT_STALE_SECS - 1))
        assertEquals(listOf("a"), book.stack(20.0 + AttentionCard.SENT_STALE_SECS - 1).map { it.id })
        assertEquals(listOf("a", "c"), book.stack(20.0 + AttentionCard.SENT_STALE_SECS).map { it.id })
        assertEquals(20.0, CardBook().apply { load(book.toJson()) }["c"]!!.sentAt, 0.0)
    }

    @Test fun detailsKeepTheirLinesUpToTheLongerLimit() {
        val text = "Why: the cards fix\nUndo: roll back to 1038\nLinks: https://example.com/pull/211"
        val c = AttentionCard.fromPost(JSONObject().put("id", "d").put("line", "Deploy hub build 1038").put("explain", text), 0.0)
        assertEquals(text, c.explain)
        val long = AttentionCard.fromPost(JSONObject().put("id", "e").put("line", "l").put("explain", "w ".repeat(1500)), 0.0)
        assertEquals(AttentionCard.EXPLAIN_MAX, long.explain.length)
    }

    @Test fun updateChannelUrls() {
        assertEquals("https://r.example/apk.json", UpdateChannel.url("https://r.example/", "apk.json", null))
        assertEquals("https://r.example/apk.json", UpdateChannel.url("https://r.example", "apk.json", "stable"))
        assertEquals("https://r.example/apk?channel=beta", UpdateChannel.url("https://r.example", "apk", "BETA"))
        assertEquals("https://r.example/apk", UpdateChannel.url("https://r.example", "apk", "nightly"))
        assertNull(UpdateChannel.normalize("nightly"))
        assertEquals(UpdateChannel.BETA, UpdateChannel.normalize(" Beta "))
    }
}
