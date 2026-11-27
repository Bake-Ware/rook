package systems.bake.rook.core.worker

import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder
import java.io.File

/**
 * What the phone tells the hub by itself (brief AP7): sms.received and notification-android.received in
 * 0.5's shapes, each told once (also after a restart, which reads what was kept), nothing dropped or cut
 * (a long body and a burst of fifty pass whole and in order, also while the worker is not linked), and
 * Rook's own notifications not told.
 */
class EventsTest {
    @get:Rule val folder = TemporaryFolder()

    private fun keys(o: JSONObject) = o.keys().asSequence().toSet()

    // ---- the shapes: 0.5's (origin/castling rook/plugins/sms and notification-android plugin.yaml, emits) ----

    @Test fun smsReceivedIsFromBodyTs() {
        val p = smsReceived(Sms(7, "5551234", "Hi, it is Sam", 1795745581914))
        assertEquals(setOf("from", "body", "ts"), keys(p))
        assertEquals("5551234", p.getString("from"))
        assertEquals("Hi, it is Sam", p.getString("body"))
        assertEquals(1795745581.914, p.getDouble("ts"), 1e-6)
        assertEquals("", smsReceived(Sms(8, null, null, 0)).getString("from"))
    }

    @Test fun notificationReceivedIsKeyPackageTitleTextTs() {
        val p = notificationReceived(Posted("0|com.x|1|null|10", "com.x", "Dinner", "Short", "The long text", 1795743140466))
        assertEquals(setOf("key", "package", "title", "text", "ts"), keys(p))
        assertEquals("The long text", p.getString("text"))       // 0.5: big_text or text
        assertEquals(1795743140.466, p.getDouble("ts"), 1e-6)
        val plain = notificationReceived(Posted("k", "com.x", null, "Short", null, 1))
        assertEquals("", plain.getString("title"))
        assertEquals("Short", plain.getString("text"))
    }

    // ---- told once ----------------------------------------------------------------------------------

    @Test fun theFirstLookAtTheInboxTellsNothingAndNotesTheNewest() {
        val inbox = listOf(Sms(1, "a", "old", 100), Sms(2, "b", "older still", 50), Sms(3, "c", "same ms", 100))
        assertEquals(SmsTold(100, setOf(1, 3)), SmsTold.start(inbox))
        assertEquals(SmsTold(0, emptySet()), SmsTold.start(emptyList()))
    }

    @Test fun eachTextIsToldOnceOldestFirstAndNotAgainAfterARestart() {
        val start = SmsTold(100, setOf(1))
        val inbox = listOf(Sms(1, "a", "told", 100), Sms(4, "b", "third", 300), Sms(2, "c", "first, same ms", 100),
            Sms(3, "d", "second", 200))
        val (news, told) = start.fresh(inbox)
        assertEquals(listOf(2L, 3L, 4L), news.map { it.id })
        assertEquals(SmsTold(300, setOf(4)), told)
        val kept = SmsTold.of(told.json())                       // what a restart or a reboot reads back
        assertEquals(told, kept)
        assertTrue(kept.fresh(inbox).first.isEmpty())
        // The provider gives a deleted newest text's id to the next one: it is still news, at its later date.
        assertEquals(listOf(4L), kept.fresh(listOf(Sms(4, "e", "a new text, a reused id", 400))).first.map { it.id })
    }

    @Test fun aBurstOfFiftyTextsAtOnceIsToldWholeAndInOrder() {
        val burst = (1..50).map { Sms(it.toLong(), "555", "text $it", 1000L + it / 3) }.shuffled()
        val (news, _) = SmsTold(999, emptySet()).fresh(burst)
        assertEquals((1..50).map { "text $it" }, news.map { it.body })
    }

    @Test fun notificationsPostedWhileTheListenerWasNotBoundAreToldOnceAndRooksOwnNever() {
        val told = NoticesTold(100, setOf("a"))
        val own = "systems.bake.rook.core"
        val shade = listOf(
            Posted("a", "com.x", "told", null, null, 100),
            Posted("b", "com.x", "same ms, not told", null, null, 100),
            Posted("c", "com.y", "later", null, null, 150),
            Posted("d", "com.z", "before", null, null, 99),
            Posted("w", own, "Rook worker", null, null, 160),            // the worker's, a card, an ask: Rook's own
        )
        val missed = told.missed(shade, own)
        assertEquals(listOf("b", "c"), missed.map { it.key })
        var after = told
        missed.forEach { after = after.after(it) }
        assertEquals(NoticesTold(150, setOf("c")), after)
        assertTrue(NoticesTold.of(after.json()).missed(shade, own).isEmpty())
        // An update Android posts again under the same key has a new post time: a new post, told again.
        assertEquals(listOf("c"), after.missed(listOf(Posted("c", "com.y", "updated", null, null, 151)), own).map { it.key })
    }

    // ---- kept until sent, nothing dropped or cut --------------------------------------------------------

    @Test fun eventsWaitWhileTheWorkerIsNotLinkedAndGoInOrderWhenItLinks() {
        val dir = folder.newFolder()
        val long = "a body ".repeat(20_000) + "\nwith lines\r\nand ünïcödé,   and \"quotes\""
        Outbox(dir).add("sms.received", JSONObject().put("from", "555").put("body", long).put("ts", 1.5))
        (1..50).forEach { Outbox(dir).add("notification-android.received", JSONObject().put("key", "k$it")) }
        assertEquals(0, Outbox(dir).drain { _, _ -> false })            // not linked: everything stays
        assertEquals(51, Outbox(dir).unsent().length())

        val sent = mutableListOf<Pair<String, JSONObject>>()
        var room = 20                                                    // the link goes after twenty
        Outbox(dir).drain { name, payload -> if (room-- > 0) { sent.add(name to payload); true } else false }
        assertEquals(20, sent.size)
        assertEquals(31, Outbox(dir).unsent().length())
        Outbox(dir).add("notification-android.card", JSONObject().put("id", "q1"))   // made while unlinked

        Outbox(File(dir.path)).drain { name, payload -> sent.add(name to payload); true }   // linked again, after a restart
        assertEquals(52, sent.size)
        assertEquals("sms.received", sent[0].first)
        assertEquals(long, sent[0].second.getString("body"))
        assertEquals((1..50).map { "k$it" }, sent.subList(1, 51).map { it.second.getString("key") })
        assertEquals("q1", sent[51].second.getString("id"))
        assertEquals(0, Outbox(dir).unsent().length())
        assertTrue(dir.listFiles()!!.none { it.length() > 0 })           // all sent: the file starts again empty
        Outbox(dir).add("sms.received", JSONObject().put("body", "next"))
        assertEquals("next", Outbox(dir).unsent().getJSONObject(0).getJSONObject("payload").getString("body"))
    }

    @Test fun aLineCutShortByACrashIsPassedOverAndTheNextOneIsStillWhole() {
        val dir = folder.newFolder()
        Outbox(dir).add("sms.received", JSONObject().put("body", "one"))
        File(dir, "unsent.jsonl").appendText("{\"name\": \"sms.rec")        // the process died mid-write
        Outbox(dir).add("sms.received", JSONObject().put("body", "two"))
        val sent = mutableListOf<String>()
        assertEquals(1, Outbox(dir).drain { _, p -> sent.add(p.getString("body")); true })
        assertEquals(listOf("one", "two"), sent)
    }
}
