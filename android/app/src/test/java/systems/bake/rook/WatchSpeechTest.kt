package systems.bake.rook

import okio.ByteString.Companion.decodeBase64
import okio.ByteString.Companion.toByteString
import org.json.JSONArray
import org.json.JSONObject
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test
import systems.bake.rook.wearlink.Transcript
import java.io.IOException
import java.util.Collections
import java.util.concurrent.LinkedBlockingQueue
import java.util.concurrent.TimeUnit

/** [WatchSpeech] against a scripted hub: the calls it makes and what reaches the watch. */
class WatchSpeechTest {
    private class Call(val cap: String, val args: JSONObject, val why: String)

    /** speech.read with `wait` takes from [polls]; every other call answers from [answer]. */
    private inner class FakeHub : HubCaps {
        val calls: MutableList<Call> = Collections.synchronizedList(ArrayList())
        val polls = LinkedBlockingQueue<() -> JSONObject>()
        @Volatile var answer: (String, JSONObject) -> JSONObject = { cap, _ -> error("unexpected $cap") }

        override fun call(cap: String, args: JSONObject, why: String, timeoutMs: Long): JSONObject {
            calls += Call(cap, args, why)
            assertTrue("every call has a why", why.isNotBlank())
            if (cap == "speech.read" && args.has("wait")) {
                val next = polls.poll(100, TimeUnit.MILLISECONDS)
                return next?.invoke() ?: JSONObject().put("events", JSONArray()).put("next", args.optInt("after"))
            }
            return answer(cap, args)
        }
        fun count(cap: String, pred: (JSONObject) -> Boolean = { true }) = synchronized(calls) { calls.count { it.cap == cap && pred(it.args) } }
    }

    private class Sink : AudioSink {
        val written = LinkedBlockingQueue<Triple<Int, Int, ByteArray>>()
        @Volatile var flushes = 0
        override fun write(turn: Int, sampleRate: Int, pcm: ByteArray) { written += Triple(turn, sampleRate, pcm) }
        override fun pause() {}
        override fun resume() {}
        override fun flush() { flushes++ }
    }

    private class Heard : VoiceClient.Listener {
        val events = LinkedBlockingQueue<String>()
        override fun onState(state: String) { events += "state:$state" }
        override fun onTranscript(text: String) { events += "heard:$text" }
        override fun onAssistantDelta(text: String) { events += "reply:$text" }
        override fun onAssistantDone() { events += "done" }
        override fun onInterrupt() { events += "interrupt" }
        override fun onError(msg: String) { events += "error:$msg" }
        override fun onClosed() { events += "closed" }
        override fun onOpen() { events += "open" }

        /** Waits for [want]; the events seen up to it. */
        fun until(want: String, ms: Long = 5_000): List<String> {
            val seen = ArrayList<String>()
            val end = System.currentTimeMillis() + ms
            while (System.currentTimeMillis() < end) {
                val e = events.poll(50, TimeUnit.MILLISECONDS) ?: continue
                seen += e
                if (e == want) return seen
            }
            throw AssertionError("no $want; saw $seen")
        }
    }

    private class Store(override var stream: String = "", override var seq: Int = 0) : WatchSpeech.Store

    private val hub = FakeHub()
    private val sink = Sink()
    private val heard = Heard()
    private val histories = LinkedBlockingQueue<List<Transcript.Line>>()
    private var speech: WatchSpeech? = null

    private fun start(store: Store): WatchSpeech =
        WatchSpeech(hub, "bakephone", heard, sink, store, history = { histories += it }).also { speech = it; it.connect() }

    @After fun stop() { speech?.close() }

    private fun clip(ms: Int, rate: Int = 24000) =
        JSONObject().put("mime", "audio/wav").put("data", SpeechAudioTest.wav(ByteArray(rate * 2 * ms / 1000) { 1 }, rate).toByteString().base64())

    private fun talk(s: WatchSpeech, frames: Int = 20) {
        repeat(frames) { s.pushFrame(ByteArray(640) { 5 }, true) }
        repeat(Segmenter.END_SILENCE) { s.pushFrame(ByteArray(640), false) }
    }

    private fun event(seq: Int, type: String, data: JSONObject) =
        JSONObject().put("seq", seq).put("type", type).put("data", data)

    private fun page(next: Int, vararg events: JSONObject) =
        JSONObject().put("events", JSONArray(events.toList())).put("next", next).put("closed", false)

    @Test(timeout = 15_000) fun aNewSessionThenAnUtteranceAndSystemOnesReply() {
        hub.answer = { cap, args ->
            when (cap) {
                "speech.open" -> JSONObject().put("stream", "s1").put("agent", "house")
                "speech.hear" -> JSONObject().put("heard", "hello there").put("complete", true).put("handoff", false)
                    .put("say", JSONObject().put("text", "Hi Bake!").put("seq", 2).put("audio", JSONArray().put(clip(300))))
                else -> error("unexpected $cap")
            }
        }
        val store = Store()
        val s = start(store)
        heard.until("state:listening")
        assertEquals("s1", store.stream)
        assertEquals("bakephone", hub.calls.first { it.cap == "speech.open" }.args.getString("device"))
        assertEquals(emptyList<Transcript.Line>(), histories.poll(1, TimeUnit.SECONDS))

        talk(s)
        // The reply may start playing before hear's thread says it's done thinking: speaking, then listening.
        val seen = heard.until("state:speaking")
        assertEquals(listOf("state:thinking", "heard:hello there", "reply:Hi Bake!", "done"), seen.take(4))
        heard.until("state:listening")
        val hear = hub.calls.first { it.cap == "speech.hear" }.args
        assertEquals("s1", hear.getString("stream"))
        assertEquals("audio/L16;rate=16000", hear.getJSONObject("audio").getString("mime"))
        val sent = hear.getJSONObject("audio").getString("data").decodeBase64()!!.size
        assertEquals((20 + Segmenter.END_SILENCE) * 640, sent)
        // 300 ms of 24 kHz audio in 200 ms pieces, all one turn.
        val pieces = generateSequence { sink.written.poll() }.toList()
        assertEquals(listOf(9600, 4800), pieces.map { it.third.size })
        assertTrue(pieces.all { it.first == pieces[0].first && it.second == 24000 })
    }

    @Test(timeout = 15_000) fun aKeptSessionGivesTheWatchItsHistory() {
        hub.answer = { cap, args ->
            check(cap == "speech.read" && args.getString("stream") == "s7" && args.getInt("after") == 40) { "unexpected $cap $args" }
            page(103,
                event(101, "turn", JSONObject().put("role", "prompt").put("text", "what's on today")),
                event(102, "status", JSONObject().put("state", "thinking")),
                event(103, "turn", JSONObject().put("role", "reply").put("system", "two").put("text", "Two meetings.")))
        }
        val store = Store("s7", 100)
        start(store)
        assertEquals(listOf(Transcript.Line(Transcript.Who.USER, "what's on today"), Transcript.Line(Transcript.Who.ROOK, "Two meetings.")),
            histories.poll(5, TimeUnit.SECONDS))
        heard.until("state:listening")
        assertEquals(0, hub.count("speech.open"))
        // The long poll carries on from the backlog's end.
        val end = System.currentTimeMillis() + 2_000
        while (hub.count("speech.read") { it.has("wait") } == 0 && System.currentTimeMillis() < end) Thread.sleep(20)
        assertEquals(103, hub.calls.first { it.args.has("wait") }.args.getInt("after"))
    }

    @Test(timeout = 15_000) fun aClosedSessionIsReplaced() {
        hub.answer = { cap, _ ->
            when (cap) {
                "speech.read" -> throw HubDoor.Refused("voice session s7 is closed")
                "speech.open" -> JSONObject().put("stream", "s8")
                else -> error(cap)
            }
        }
        val store = Store("s7", 5)
        start(store)
        heard.until("state:listening")
        assertEquals("s8", store.stream)
        assertEquals(emptyList<Transcript.Line>(), histories.poll(1, TimeUnit.SECONDS))
    }

    @Test(timeout = 15_000) fun theAgentsReplyIsFetchedWithItsAudioAndSpoken() {
        hub.answer = { cap, args ->
            when (cap) {
                "speech.open" -> JSONObject().put("stream", "s1")
                "speech.read" -> {
                    assertEquals(4, args.getInt("after")); assertEquals(1, args.getInt("limit")); assertTrue(args.getBoolean("audio"))
                    page(5, event(5, "turn", JSONObject().put("role", "reply").put("system", "two").put("text", "Done."))
                        .put("audio", JSONArray().put(clip(100, 16000))))
                }
                else -> error(cap)
            }
        }
        start(Store())
        heard.until("state:listening")
        hub.polls += { page(3, event(3, "status", JSONObject().put("state", "thinking"))) }
        heard.until("state:thinking")
        hub.polls += {
            page(5,
                event(4, "turn", JSONObject().put("role", "reply").put("system", "one").put("text", "Let me check.")),
                event(5, "turn", JSONObject().put("role", "reply").put("system", "two").put("text", "Done.")))
        }
        val seen = heard.until("state:speaking")
        heard.until("state:listening")
        assertTrue(seen.toString(), "reply:Done." in seen)
        assertTrue("System One's reply came with hear, not again here", "reply:Let me check." !in seen)
        assertEquals(16000, sink.written.poll(1, TimeUnit.SECONDS)!!.second)
    }

    @Test(timeout = 15_000) fun aTapStopsTheReplyAndKeepsItQuietUntilThePersonSpeaks() {
        hub.answer = { cap, args ->
            when (cap) {
                "speech.open" -> JSONObject().put("stream", "s1")
                "speech.read" -> args.getInt("after").let { seq ->
                    page(seq + 1, event(seq + 1, "turn", JSONObject().put("role", "reply").put("system", "two").put("text", "Now."))
                        .put("audio", JSONArray().put(clip(100))))
                }
                "speech.hear" -> JSONObject().put("heard", "and now").put("complete", true).put("handoff", true)
                else -> error(cap)
            }
        }
        val s = start(Store())
        heard.until("state:listening")
        s.interrupt()
        heard.until("interrupt")
        assertEquals(1, sink.flushes)
        hub.polls += { page(9, event(9, "turn", JSONObject().put("role", "reply").put("system", "two").put("text", "Later."))) }
        heard.until("reply:Later.")
        Thread.sleep(300)
        assertEquals("muted: no audio fetched", 0, hub.count("speech.read") { it.optBoolean("audio") })
        assertEquals(null, sink.written.poll())
        // The person speaks again: the next reply is spoken.
        talk(s)
        heard.until("heard:and now")
        hub.polls += { page(10, event(10, "turn", JSONObject().put("role", "reply").put("system", "two").put("text", "Now."))) }
        heard.until("reply:Now.")
        val end = System.currentTimeMillis() + 3_000
        while (hub.count("speech.read") { it.optBoolean("audio") } == 0 && System.currentTimeMillis() < end) Thread.sleep(20)
        assertEquals(1, hub.count("speech.read") { it.optBoolean("audio") })
        assertEquals(24000, sink.written.poll(2, TimeUnit.SECONDS)!!.second)
    }

    @Test(timeout = 15_000) fun theMicIsIgnoredWhileAReplyPlays() {
        hub.answer = { cap, _ ->
            when (cap) {
                "speech.open" -> JSONObject().put("stream", "s1")
                "speech.hear" -> JSONObject().put("heard", "hi").put("complete", true)
                    .put("say", JSONObject().put("text", "Hello.").put("seq", 2).put("audio", JSONArray().put(clip(1500))))
                else -> error(cap)
            }
        }
        val s = start(Store())
        heard.until("state:listening")
        talk(s)
        heard.until("state:speaking")
        talk(s)                 // the watch speaker, heard by its own mic
        heard.until("state:listening")
        Thread.sleep(200)
        assertEquals(1, hub.count("speech.hear"))
    }

    @Test(timeout = 20_000) fun losingTheHubEndsTheConversation() {
        var reads = 0
        val failing = object : HubCaps {
            override fun call(cap: String, args: JSONObject, why: String, timeoutMs: Long): JSONObject = when (cap) {
                "speech.open" -> JSONObject().put("stream", "s1")
                else -> { reads++; throw IOException("connection reset") }
            }
        }
        speech = WatchSpeech(failing, "p", heard, sink, Store(), history = {}).also { it.connect() }
        val seen = heard.until("closed", 12_000)
        assertTrue(seen.toString(), "error:Can't reach the hub: connection reset" in seen)
        assertEquals(3, reads)
    }

    @Test(timeout = 10_000) fun noSessionNoConversation() {
        hub.answer = { _, _ -> throw HubDoor.Refused("denied: speech.open") }
        start(Store())
        val seen = heard.until("closed")
        assertEquals(listOf("error:denied: speech.open", "closed"), seen)
    }
}
