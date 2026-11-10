package systems.bake.rook

import android.content.Context
import android.os.Handler
import android.os.Looper
import android.os.SystemClock
import android.util.Log
import com.google.android.gms.tasks.Tasks
import com.google.android.gms.wearable.ChannelClient
import com.google.android.gms.wearable.Wearable
import org.json.JSONObject
import systems.bake.rook.wearlink.AudioEncoder
import systems.bake.rook.wearlink.Codecs
import systems.bake.rook.wearlink.FrameAssembler
import systems.bake.rook.wearlink.LinkReader
import systems.bake.rook.wearlink.LinkWriter
import systems.bake.rook.wearlink.Transcript
import systems.bake.rook.wearlink.WearLink
import java.util.concurrent.LinkedBlockingQueue
import java.util.concurrent.TimeUnit
import kotlin.concurrent.thread

/**
 * One conversation from the watch app. Its microphone feeds a voice session here (the phone
 * does all the network work) and the replies go back over the same Data Layer channel to play
 * on the watch, on whatever earbuds or glasses are paired to it. [WatchVoiceListener] hands
 * over the channel the watch opened on [WearLink.PATH].
 *
 * The voice session is a [WatchSpeech] session on the hub's `speech` plugin, through the hub's
 * door with the person's token. The watch gets that session's history; the phone's own chat is
 * left alone.
 */
class WatchBridge private constructor(private val ctx: Context, val channel: ChannelClient.Channel) {
    companion object {
        private const val TAG = "WatchBridge"
        /** Listening with nothing heard for this long ends a watch conversation. */
        const val IDLE_CLOSE_MS = 20_000L
        /** Mic frames kept while the voice server connects (3 s). */
        private const val PENDING_FRAMES = 150
        /** Chat lines the watch gets when a conversation starts. */
        private const val HISTORY_LINES = 20
        @Volatile private var current: WatchBridge? = null

        fun open(ctx: Context, channel: ChannelClient.Channel) {
            current?.close("replaced by a new conversation")
            current = WatchBridge(ctx.applicationContext, channel).also { it.start() }
        }

        fun channelClosed(channel: ChannelClient.Channel) {
            current?.takeIf { it.channel == channel }?.close("watch closed the channel", notifyWatch = false)
        }
    }

    private sealed class Down {
        class Audio(val turn: Int, val sr: Int, val pcm: ByteArray) : Down()
        object Pause : Down()
        object Resume : Down()
        object Flush : Down()
        object Quit : Down()
    }

    private val channels = Wearable.getChannelClient(ctx)
    private val main = Handler(Looper.getMainLooper())
    @Volatile private var closed = false
    @Volatile private var writer: LinkWriter? = null
    @Volatile private var client: WatchVoice? = null
    private var downCodec = WearLink.CODEC_PCM
    private val down = LinkedBlockingQueue<Down>(1024)
    private val frameLock = Any()
    private var opened = false
    private val pending = ArrayDeque<Pair<ByteArray, Boolean>>()
    @Volatile private var lastState = ""
    @Volatile private var lastActivityAt = SystemClock.elapsedRealtime()

    private val sink = object : AudioSink {
        override fun write(turn: Int, sampleRate: Int, pcm: ByteArray) {
            if (!down.offer(Down.Audio(turn, sampleRate, pcm))) Log.w(TAG, "watch audio queue full; dropped a packet")
        }
        override fun pause() { down.offer(Down.Pause) }
        override fun resume() { down.offer(Down.Resume) }
        override fun flush() {
            val it = down.iterator()
            while (it.hasNext()) if (it.next() is Down.Audio) it.remove()
            down.offer(Down.Flush)
        }
    }

    private fun start() = thread(name = "watch-bridge") {
        try { run() } catch (e: Exception) {
            if (!closed) Log.w(TAG, "watch conversation failed", e)
            close("error: ${e.message}")
        }
    }

    private fun run() {
        val out = Tasks.await(channels.getOutputStream(channel), 10, TimeUnit.SECONDS)
        val input = Tasks.await(channels.getInputStream(channel), 10, TimeUnit.SECONDS)
        val w = LinkWriter(out).also { writer = it }
        val reader = LinkReader(input)
        val hello = reader.next() ?: return close("watch left", notifyWatch = false)
        if (hello.type != WearLink.HELLO) return close("watch sent no hello")
        val h = hello.json()
        val decoder = Codecs.decoder(h.optString("codec", WearLink.CODEC_PCM), h.optInt("sr", WearLink.SR_IN))
        val accepts = h.optJSONArray("accepts")
        downCodec = if (accepts != null && (0 until accepts.length()).any { accepts.optString(it) == WearLink.CODEC_OPUS })
            WearLink.CODEC_OPUS else WearLink.CODEC_PCM
        w.send(WearLink.READY, JSONObject().put("uplink", decoder.codec).put("downlink", downCodec))
        Log.i(TAG, "watch conversation: uplink=${decoder.codec} downlink=$downCodec")
        thread(name = "watch-bridge-down") { sendDown() }
        main.post { connectVoice() }
        main.postDelayed(idleCheck, 2000)
        val vad = SpeechDetector(ctx)
        val frames = FrameAssembler()
        try {
            while (!closed) {
                val m = reader.next() ?: break
                when (m.type) {
                    WearLink.MIC -> for (pcm in decoder.decode(m.payload)) {
                        frames.push(pcm, decoder.outputRate) { frame -> feed(frame, vad.feed(frame)) }
                    }
                    WearLink.STOP -> client?.interrupt()
                    WearLink.PLAYED -> m.json().let { client?.reportPlayback(it.optInt("turn"), it.optLong("frames")) }
                    WearLink.END -> break
                }
            }
        } finally {
            vad.close(); decoder.close()
            close("watch ended the conversation", notifyWatch = false)
        }
    }

    /** Reader thread. Frames said before the voice server answers are kept, then sent. */
    private fun feed(frame: ByteArray, speech: Boolean) = synchronized(frameLock) {
        if (!opened) {
            pending.addLast(frame to speech)
            while (pending.size > PENDING_FRAMES) pending.removeFirst()
        } else client?.pushFrame(frame, speech)
    }

    /** Main thread. */
    private fun connectVoice() {
        if (closed) return
        thread(name = "watch-speech-setup") { connectSpeech() }
    }

    /** Off the main thread (the sign-in status is Python's): the hub's speech plugin. */
    private fun connectSpeech() {
        val prefs = ctx.getSharedPreferences("rook", Context.MODE_PRIVATE)
        val status = try { Castling.status(ctx) } catch (e: Exception) { null }
        val url = status?.takeIf { it.optBoolean("enrolled") }?.optString("url").orEmpty()
        val token = prefs.getString(Castling.TOKEN_PREF, "").orEmpty()
        val door = try {
            require(url.isNotEmpty()) { "Sign this phone in to your Rook 0.5 hub first (Rook app: Settings > Band)" }
            require(token.isNotEmpty()) { "Add your hub token in the Rook app (Settings > Band) to talk on Rook 0.5" }
            HubDoor(url, token)
        } catch (e: IllegalArgumentException) {
            tell(WearLink.ERROR, e.message ?: "Rook 0.5 voice isn't set up")
            main.post { close("Rook 0.5 voice isn't set up") }
            return
        }
        val device = status?.optString("name").orEmpty().ifEmpty { prefs.getString(Castling.NAME_PREF, "").orEmpty() }
        val speech = WatchSpeech(door, device, listener, sink, SpeechStore(prefs, url),
            history = { lines -> if (lines.isNotEmpty()) tell(WearLink.HISTORY, Transcript.of(lines, HISTORY_LINES).toJson()) },
            warn = { msg, e -> Log.w(TAG, msg, e) })
        main.post {
            if (closed) return@post
            client = speech      // before connect: its onOpen hands over the frames kept meanwhile
            speech.connect()
        }
    }

    /** The watch's 0.5 voice session, kept per hub address. */
    private class SpeechStore(private val prefs: android.content.SharedPreferences, private val hub: String) : WatchSpeech.Store {
        override var stream: String
            get() = if (prefs.getString("speech05_hub", "") == hub) prefs.getString("speech05_stream", "").orEmpty() else ""
            set(v) { prefs.edit().putString("speech05_hub", hub).putString("speech05_stream", v).apply() }
        override var seq: Int
            get() = prefs.getInt("speech05_seq", 0)
            set(v) { prefs.edit().putInt("speech05_seq", v).apply() }
    }

    private val listener = object : VoiceClient.Listener {
        override fun onOpen() {
            synchronized(frameLock) {
                opened = true
                while (pending.isNotEmpty()) pending.removeFirst().let { (f, s) -> client?.pushFrame(f, speech = s) }
            }
        }
        override fun onState(state: String) {
            lastState = state; lastActivityAt = SystemClock.elapsedRealtime()
            tell(WearLink.STATE, JSONObject().put("state", state))
        }
        override fun onTranscript(text: String) {
            lastActivityAt = SystemClock.elapsedRealtime()
            tell(WearLink.HEARD, JSONObject().put("text", text))
        }
        override fun onAssistantDelta(text: String) {
            tell(WearLink.REPLY, JSONObject().put("text", text))
        }
        override fun onAssistantDone() { tell(WearLink.REPLY_DONE) }
        override fun onInterrupt() {}
        override fun onError(msg: String) { tell(WearLink.ERROR, msg) }
        override fun onBye(mode: String, afterMs: Long) {
            main.postDelayed({ close("the assistant said goodbye") }, afterMs.coerceIn(0L, 15_000L))
        }
        override fun onClosed() {
            main.post {
                synchronized(frameLock) { opened = false }
                client = null
                if (closed) return@post
                close("voice connection closed")
            }
        }
    }

    private val idleCheck = object : Runnable {
        override fun run() {
            if (closed) return
            if (lastState == "listening" && SystemClock.elapsedRealtime() - lastActivityAt >= IDLE_CLOSE_MS) close("nothing said")
            else main.postDelayed(this, 2000)
        }
    }

    /** Encodes reply audio for the watch, in order, on its own thread. */
    private fun sendDown() {
        var enc: AudioEncoder? = null
        var encSr = 0
        var sentTurn = -1
        try {
            while (!closed) {
                when (val d = down.poll(500, TimeUnit.MILLISECONDS) ?: continue) {
                    is Down.Audio -> {
                        if (enc == null || d.sr != encSr) {
                            enc?.close(); enc = Codecs.encoder(downCodec, d.sr); encSr = d.sr; sentTurn = -1
                        }
                        if (d.turn != sentTurn) {
                            tell(WearLink.OUT_FORMAT, JSONObject().put("codec", enc.codec).put("sr", d.sr).put("turn", d.turn))
                            sentTurn = d.turn
                        }
                        for (packet in enc.encode(d.pcm)) writer?.send(WearLink.OUT_AUDIO, packet)
                    }
                    Down.Pause -> tell(WearLink.PAUSE)
                    Down.Resume -> tell(WearLink.RESUME)
                    // A fresh encoder: no half frame of the old reply leaks into the next one.
                    Down.Flush -> { enc?.close(); enc = null; encSr = 0; tell(WearLink.FLUSH) }
                    Down.Quit -> break
                }
            }
        } catch (e: Exception) {
            if (!closed) Log.w(TAG, "sending reply audio to the watch failed", e)
            close("lost the watch", notifyWatch = false)
        } finally { enc?.close() }
    }

    private fun tell(type: Int, json: JSONObject? = null) {
        val w = writer ?: return
        try { if (json != null) w.send(type, json) else w.send(type) }
        catch (e: Exception) { if (!closed) { Log.w(TAG, "watch link write failed", e); main.post { close("lost the watch", notifyWatch = false) } } }
    }
    private fun tell(type: Int, msg: String) = tell(type, JSONObject().put("msg", msg))

    fun close(reason: String, notifyWatch: Boolean = true) {
        synchronized(this) { if (closed) return; closed = true }
        Log.i(TAG, "watch conversation closed: $reason")
        if (current === this) current = null
        main.removeCallbacks(idleCheck)
        val c = client; client = null
        try { c?.close() } catch (_: Exception) {}
        down.clear(); down.offer(Down.Quit)
        if (notifyWatch) {
            val w = writer
            if (w != null) try { w.send(WearLink.BYE, JSONObject().put("reason", reason)) } catch (_: Exception) {}
        }
        writer = null
        try { channels.close(channel) } catch (_: Exception) {}
    }
}
