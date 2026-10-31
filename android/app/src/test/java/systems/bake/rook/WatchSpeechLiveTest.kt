package systems.bake.rook

import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Assume.assumeTrue
import org.junit.Test
import systems.bake.rook.wearlink.Transcript
import java.util.concurrent.LinkedBlockingQueue
import java.util.concurrent.TimeUnit

/**
 * [WatchSpeech] through a real 0.5 hub's door, with the `speech` plugin and a fake engine whose
 * STT reads the "audio" bytes as text and whose TTS answers 24 kHz WAV. Skipped unless
 * ROOK_LIVE_DOOR (http://127.0.0.1:port) and ROOK_LIVE_TOKEN (a person's token) are set:
 * docs/castling-watch.md says how to run it.
 */
class WatchSpeechLiveTest {
    private class Store(override var stream: String = "", override var seq: Int = 0) : WatchSpeech.Store

    private class Sink : AudioSink {
        val rates = LinkedBlockingQueue<Int>()
        override fun write(turn: Int, sampleRate: Int, pcm: ByteArray) { rates += sampleRate }
        override fun pause() {}
        override fun resume() {}
        override fun flush() {}
    }

    private class Heard : VoiceClient.Listener {
        val events = LinkedBlockingQueue<String>()
        override fun onState(state: String) { events += "state:$state" }
        override fun onTranscript(text: String) { events += "heard:$text" }
        override fun onAssistantDelta(text: String) { events += "reply:$text" }
        override fun onAssistantDone() {}
        override fun onInterrupt() {}
        override fun onError(msg: String) { events += "error:$msg" }
        override fun onClosed() { events += "closed" }

        fun until(pred: (String) -> Boolean, ms: Long = 30_000): String {
            val end = System.currentTimeMillis() + ms
            val seen = ArrayList<String>()
            while (System.currentTimeMillis() < end) {
                val e = events.poll(100, TimeUnit.MILLISECONDS) ?: continue
                seen += e
                if (e.startsWith("error:")) throw AssertionError("$e (saw $seen)")
                if (pred(e)) return e
            }
            throw AssertionError("timed out; saw $seen")
        }
    }

    /** Frames the fake STT reads as [text] (silence is spaces, which it strips). */
    private fun say(s: WatchSpeech, text: String) {
        val speech = text.padEnd(640, ' ').toByteArray().copyOf(640)
        val quiet = ByteArray(640) { ' '.code.toByte() }
        s.pushFrame(speech, true)
        repeat(Segmenter.MIN_SPEECH) { s.pushFrame(quiet, true) }
        repeat(Segmenter.END_SILENCE) { s.pushFrame(quiet, false) }
    }

    @Test(timeout = 90_000) fun aConversationThroughARealHub() {
        val url = System.getenv("ROOK_LIVE_DOOR").orEmpty()
        val token = System.getenv("ROOK_LIVE_TOKEN").orEmpty()
        assumeTrue("ROOK_LIVE_DOOR and ROOK_LIVE_TOKEN not set", url.isNotEmpty() && token.isNotEmpty())
        val store = Store()
        val sink = Sink()
        val heard = Heard()
        val histories = LinkedBlockingQueue<List<Transcript.Line>>()
        val s = WatchSpeech(HubDoor(url, token), "live-phone", heard, sink, store, history = { histories += it })
        s.connect()
        heard.until({ it == "state:listening" })
        assertTrue(store.stream.isNotEmpty())

        say(s, "hello there")                       // System One answers itself, with its audio
        assertEquals("heard:hello there", heard.until({ it.startsWith("heard:") }))
        assertEquals("reply:Hi Bake! (sounds like bake)", heard.until({ it.startsWith("reply:") }))
        assertEquals(24000, sink.rates.poll(10, TimeUnit.SECONDS))
        heard.until({ it == "state:listening" })
        Thread.sleep(WatchSpeech.TAIL_MS + 200)        // the mic waits out the reply's last words

        say(s, "is the build green")               // System Two: the agent's reply comes through speech.read
        heard.until({ it == "reply:Let me check." })
        val two = heard.until({ it.startsWith("reply:brain:") })
        assertTrue(two, two.endsWith("is the build green"))
        sink.rates.clear()
        assertEquals(24000, sink.rates.poll(20, TimeUnit.SECONDS))
        s.close()

        // The next conversation comes back to the same session, with its history.
        val again = WatchSpeech(HubDoor(url, token), "live-phone", Heard(), Sink(), store, history = { histories += it })
        histories.clear()
        val kept = store.stream
        again.connect()
        val lines = histories.poll(20, TimeUnit.SECONDS)!!
        again.close()
        assertEquals(kept, store.stream)
        assertEquals(Transcript.Line(Transcript.Who.USER, "hello there"), lines.first())
        assertTrue(lines.toString(), lines.last().who == Transcript.Who.ROOK && lines.last().text.startsWith("brain:"))
    }
}
