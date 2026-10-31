package systems.bake.rook

import okio.ByteString.Companion.decodeBase64
import okio.ByteString.Companion.toByteString
import org.json.JSONObject
import systems.bake.rook.wearlink.Transcript
import java.io.IOException
import java.util.concurrent.LinkedBlockingQueue
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicInteger
import kotlin.concurrent.thread

/** What [WatchBridge] needs from a voice session: the 0.4 [VoiceClient], or 0.5's [WatchSpeech]. */
interface WatchVoice {
    fun connect()
    fun pushFrame(frame: ByteArray, speech: Boolean)
    /** The person tapped stop on the watch. */
    fun interrupt()
    fun reportPlayback(turn: Int, frames: Long)
    fun close()
}

/**
 * A watch conversation on Rook 0.5: the hub's `speech` plugin (rook/plugins/speech) instead
 * of the 0.4 voice server's WebSocket. The phone keeps one voice session (a `voice` stream on
 * the hub) for the watch and comes back to it, so a reply that lands while the watch is
 * away shows up in the next conversation's history.
 *
 * - The watch's mic frames go through the phone's VAD into a [Segmenter]; each utterance is
 *   one `speech.hear` (L16, 16 kHz). Its answer has what was heard and System One's quick
 *   reply with its audio.
 * - `speech.read` long-polls the session for the agent's own reply (System Two) and its
 *   `status` events (thinking, idle, error); a reply's audio is fetched on its own
 *   (`speech.read {after: seq-1, limit: 1, audio: true}`) so System One's clips aren't made twice.
 * - Half duplex: while a reply plays the mic is ignored (the watch speaker would talk over
 *   itself). A tap on the watch stops the reply and keeps quiet until the person speaks again.
 *
 * The [listener] gets the 0.4 client's callbacks (states listening / thinking / speaking), so
 * the bridge and the watch app are the same for both. Clips go to [sink] paced about
 * [AHEAD_MS] ahead of real time, at most 200 ms each.
 */
class WatchSpeech(
    private val hub: HubCaps,
    private val device: String,
    private val listener: VoiceClient.Listener,
    private val sink: AudioSink,
    private val store: Store,
    private val history: (List<Transcript.Line>) -> Unit,
    private val warn: (String, Throwable?) -> Unit = { _, _ -> },
    private val now: () -> Long = { System.nanoTime() / 1_000_000 },
) : WatchVoice {
    /** Where the phone keeps the watch's voice session between conversations. */
    interface Store {
        var stream: String
        var seq: Int
    }

    companion object {
        const val HISTORY_LINES = 20
        const val AHEAD_MS = 2_000L
        /** After a reply the mic waits this long (the last words still ringing out). */
        const val TAIL_MS = 400L
        const val CHUNK_MS = 200
        const val WAIT_S = 25
        private const val BACKLOG = 60
        private const val PAGE = 200
        private const val FAILS = 3
        private const val WHY_OPEN = "Watch voice: start a voice session for the watch"
        private const val WHY_HEAR = "Watch voice: what the person just said on the watch"
        private const val WHY_READ = "Watch voice: wait for the agent's reply to the watch"
        private const val WHY_AUDIO = "Watch voice: the agent's spoken reply for the watch"
    }

    /** A reply to speak: its clips (System One's, from hear), or the stream seq to fetch them for. */
    private class Reply(val clips: List<JSONObject>?, val seq: Int)

    @Volatile private var open = true
    @Volatile private var stream = ""
    @Volatile private var muted = false
    @Volatile private var generation = 0
    @Volatile private var speakingUntil = 0L
    @Volatile private var hearing = 0
    @Volatile private var agentThinking = false
    /** Replies queued or in the play thread's hands (fetching their audio, or playing). */
    private val pending = AtomicInteger()
    private var lastState = ""
    private var turn = 0
    private val segmenter = Segmenter()
    private val utterances = LinkedBlockingQueue<ByteArray>(8)
    private val replies = LinkedBlockingQueue<Reply>(32)

    override fun connect() {
        thread(name = "watch-speech-read", isDaemon = true) {
            val cursor = try { setUp() } catch (e: Exception) { return@thread fail(problem(e), e) }
            if (!open) return@thread
            thread(name = "watch-speech-hear", isDaemon = true) { hearLoop() }
            thread(name = "watch-speech-play", isDaemon = true) { playLoop() }
            readLoop(cursor)
        }
    }

    /** The session (the kept one if the hub still has it open), its history; the read cursor. */
    private fun setUp(): Int {
        val lines = ArrayList<Transcript.Line>()
        var cursor = 0
        var sid = store.stream
        if (sid.isNotEmpty()) {
            try { cursor = backlog(sid, maxOf(0, store.seq - BACKLOG), lines) }
            catch (e: HubDoor.Refused) { sid = ""; lines.clear(); agentThinking = false }   // closed, or gone with an old hub
        }
        if (sid.isEmpty()) {
            sid = hub.call("speech.open", JSONObject().put("device", device), WHY_OPEN, 20_000).getString("stream")
            store.stream = sid; store.seq = 0; cursor = 0
        }
        stream = sid
        if (!open) return cursor
        history(lines.takeLast(HISTORY_LINES))
        listener.onOpen()
        state()
        return cursor
    }

    private fun backlog(sid: String, from: Int, lines: MutableList<Transcript.Line>): Int {
        var after = from
        repeat(20) {
            val r = hub.call("speech.read", JSONObject().put("stream", sid).put("after", after).put("limit", PAGE), WHY_READ, 20_000)
            if (r.optBoolean("closed")) throw HubDoor.Refused("voice session $sid is closed")
            val events = r.optJSONArray("events")
            val n = events?.length() ?: 0
            for (i in 0 until n) {
                val e = events!!.getJSONObject(i)
                val d = e.optJSONObject("data") ?: continue
                when (e.optString("type")) {
                    "turn" -> d.optString("text").takeIf { it.isNotBlank() }?.let {
                        lines += Transcript.Line(if (d.optString("role") == "prompt") Transcript.Who.USER else Transcript.Who.ROOK, it)
                        if (d.optString("role") == "reply" && d.optString("system") != "one") agentThinking = false
                    }
                    "status" -> agentThinking = d.optString("state") == "thinking"
                }
            }
            after = r.optInt("next", after)
            if (n < PAGE) return after
        }
        return after
    }

    // -- the mic (the bridge's reader thread) ---------------------------------------------------

    override fun pushFrame(frame: ByteArray, speech: Boolean) {
        if (!open || stream.isEmpty()) return
        if (now() < speakingUntil + TAIL_MS) { segmenter.reset(); return }
        when (val out = segmenter.feed(frame, speech)) {
            Segmenter.Out.Started -> muted = false
            is Segmenter.Out.Utterance -> if (!utterances.offer(out.pcm)) warn("watch speech: utterances backed up; dropped one", null)
            null -> {}
        }
    }

    private fun hearLoop() {
        while (open) {
            val pcm = utterances.poll(500, TimeUnit.MILLISECONDS) ?: continue
            hearing++; state()
            try {
                val audio = JSONObject().put("data", pcm.toByteString().base64()).put("mime", "audio/L16;rate=16000")
                val r = hub.call("speech.hear", JSONObject().put("stream", stream).put("audio", audio), WHY_HEAR, 130_000)
                if (!open) return
                val heard = r.optString("heard")
                if (r.optBoolean("complete", true) && heard.isNotBlank()) listener.onTranscript(heard)
                if (r.optBoolean("handoff")) agentThinking = true
                r.optJSONObject("say")?.let { say ->
                    val text = say.optString("text")
                    if (text.isNotBlank()) { listener.onAssistantDelta(text); listener.onAssistantDone() }
                    val clips = say.optJSONArray("audio")
                    if (!muted && clips != null) enqueue(Reply((0 until clips.length()).map { clips.getJSONObject(it) }, say.optInt("seq")))
                }
            } catch (e: Exception) {
                if (open) { warn("watch speech: hear failed", e); listener.onError(problem(e)) }
            } finally { hearing--; state() }
        }
    }

    // -- the agent's replies -----------------------------------------------------------------------

    private fun readLoop(start: Int) {
        var cursor = start
        var fails = 0
        while (open) {
            try {
                val r = hub.call("speech.read", JSONObject().put("stream", stream).put("after", cursor)
                    .put("limit", 50).put("wait", WAIT_S), WHY_READ, (WAIT_S + 15) * 1000L)
                if (!open) return
                val events = r.optJSONArray("events")
                for (i in 0 until (events?.length() ?: 0)) event(events!!.getJSONObject(i))
                cursor = r.optInt("next", cursor)
                store.seq = cursor
                fails = 0
                if (r.optBoolean("closed")) {
                    store.stream = ""
                    return fail("The voice session was closed on the hub", null)
                }
            } catch (e: Exception) {
                if (!open) return
                warn("watch speech: read failed", e)
                if (++fails >= FAILS) return fail(problem(e), e)
                Thread.sleep(2_000)
            }
        }
    }

    private fun event(e: JSONObject) {
        val d = e.optJSONObject("data") ?: return
        when (e.optString("type")) {
            // The person's words and System One's came back with speech.hear.
            "turn" -> if (d.optString("role") == "reply" && d.optString("system") != "one") {
                agentThinking = false
                val text = d.optString("text")
                if (text.isNotBlank()) {
                    listener.onAssistantDelta(text); listener.onAssistantDone()
                    if (!muted) enqueue(Reply(null, e.optInt("seq")))
                }
            }
            "status" -> when (d.optString("state")) {
                "thinking" -> agentThinking = true
                "idle" -> agentThinking = false
                "error" -> { agentThinking = false; listener.onError(d.optString("error", "the agent couldn't answer")) }
            }
        }
        state()
    }

    // -- playback ------------------------------------------------------------------------------------

    private fun enqueue(r: Reply) {
        pending.incrementAndGet()
        if (!replies.offer(r)) { pending.decrementAndGet(); warn("watch speech: replies backed up; dropped one", null) }
    }

    /** Drops the replies not started yet. */
    private fun dropQueued() { pending.addAndGet(-ArrayList<Reply>().also { replies.drainTo(it) }.size) }

    private fun playLoop() {
        while (open) {
            val left = speakingUntil - now()
            val r = replies.poll(left.coerceIn(50, 200), TimeUnit.MILLISECONDS)
            if (r == null) { state(); continue }
            try { play(r) } finally { pending.decrementAndGet(); state() }
        }
    }

    private fun play(r: Reply) {
        val gen = generation
        val clips = r.clips ?: try { fetch(r.seq) } catch (e: Exception) {
            if (open) { warn("watch speech: reply audio failed", e); listener.onError("No audio for that reply: ${problem(e)}") }
            return
        }
        if (gen != generation || muted || !open) return
        turn++
        for (clip in clips) {
            val wav = clip.optString("data").decodeBase64()?.toByteArray()?.let { Wav.pcm(it) }
            if (wav == null) { warn("watch speech: a reply clip isn't 16-bit WAV", null); continue }
            if (!speak(wav.first, wav.second, gen)) return
        }
    }

    private fun fetch(seq: Int): List<JSONObject> {
        val r = hub.call("speech.read", JSONObject().put("stream", stream).put("after", maxOf(0, seq - 1)).put("limit", 1)
            .put("audio", true), WHY_AUDIO, 130_000)
        val e = r.optJSONArray("events")?.optJSONObject(0)
        if (e == null || e.optInt("seq") != seq) throw IOException("the hub didn't send reply $seq")
        e.optString("audio_error").takeIf { it.isNotEmpty() }?.let { throw HubDoor.Refused(it) }
        val a = e.optJSONArray("audio") ?: return emptyList()
        return (0 until a.length()).map { a.getJSONObject(it) }
    }

    /** One clip to the watch, paced; false: stopped meanwhile. */
    private fun speak(rate: Int, pcm: ByteArray, gen: Int): Boolean {
        val step = rate * 2 * CHUNK_MS / 1000
        var off = 0
        while (off < pcm.size) {
            while (open && gen == generation && speakingUntil - now() > AHEAD_MS) Thread.sleep(20)
            if (!open || gen != generation) return false
            val n = minOf(step, pcm.size - off)
            sink.write(turn, rate, pcm.copyOfRange(off, off + n))
            speakingUntil = maxOf(speakingUntil, now()) + n / 2 * 1000L / rate
            off += n
            state()
        }
        return true
    }

    override fun interrupt() {
        muted = true
        generation++
        dropQueued()
        speakingUntil = 0
        sink.flush()
        listener.onInterrupt()
        state()
    }

    override fun reportPlayback(turn: Int, frames: Long) {}     // the hub doesn't track playback

    /** What the watch's Rook shows: speaking while a reply plays, thinking while one is coming. */
    @Synchronized private fun state() {
        if (!open) return
        val s = when {
            now() < speakingUntil -> "speaking"
            // A reply waiting to play (or for its audio) is still coming: no "listening" flicker before it.
            hearing > 0 || agentThinking || pending.get() > 0 -> "thinking"
            else -> "listening"
        }
        if (s != lastState) { lastState = s; listener.onState(s) }
    }

    private fun fail(msg: String, e: Throwable?) {
        if (!open) return
        if (e != null) warn("watch speech: $msg", e)
        listener.onError(msg)
        close()
        listener.onClosed()
    }

    private fun problem(e: Exception): String = when (e) {
        is HubDoor.Refused -> e.message ?: "the hub said no"
        is IOException -> "Can't reach the hub: ${e.message ?: e.javaClass.simpleName}"
        else -> "${e.javaClass.simpleName}: ${e.message}"
    }

    override fun close() {
        open = false
        generation++
        utterances.clear(); dropQueued()
    }
}
