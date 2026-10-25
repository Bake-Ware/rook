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
import systems.bake.rook.wearlink.WearLink
import java.util.concurrent.LinkedBlockingQueue
import java.util.concurrent.TimeUnit
import kotlin.concurrent.thread

/**
 * One conversation from the watch app. Its microphone feeds a [VoiceClient] session here (the
 * phone does all the network work) and the replies go back over the same Data Layer channel to
 * play on the watch, on whatever earbuds or glasses are paired to it. [WatchVoiceListener]
 * hands over the channel the watch opened on [WearLink.PATH].
 *
 * Same voice server, account and conversation as the phone's own voice; a phone voice session
 * that is open is closed first (the server allows one connection per conversation).
 */
class WatchBridge private constructor(private val ctx: Context, val channel: ChannelClient.Channel) {
    companion object {
        private const val TAG = "WatchBridge"
        /** Listening with nothing heard for this long ends a watch conversation. */
        const val IDLE_CLOSE_MS = 20_000L
        /** Mic frames kept while the voice server connects (3 s). */
        private const val PENDING_FRAMES = 150
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
    @Volatile private var client: VoiceClient? = null
    private var downCodec = WearLink.CODEC_PCM
    private val down = LinkedBlockingQueue<Down>(1024)
    private val frameLock = Any()
    private var opened = false
    private var everOpened = false
    private var attempts = 0
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
        } else client?.pushFrame(frame, speech = speech)
    }

    /** Main thread. */
    private fun connectVoice() {
        if (closed) return
        val prefs = ctx.getSharedPreferences("rook", Context.MODE_PRIVATE)
        val url = prefs.getString("voice_url", "") ?: ""
        if (url.isEmpty()) { tell(WearLink.ERROR, "Set up voice in the Rook app on the phone first"); close("no voice server"); return }
        // One connection per conversation: the phone's own open session gives way to the watch.
        if (attempts == 0 && VoiceService.inst?.sessionOpen() == true) {
            try { VoiceService.endSession(ctx) } catch (e: Exception) { Log.w(TAG, "could not end the phone's voice session", e) }
            attempts++
            main.postDelayed({ connectVoice() }, 800)
            return
        }
        attempts++
        client = VoiceClient(ctx, url, prefs.getBoolean("voice_insecure", false), listener,
            ownMic = false, token = prefs.getString("voice_token", "") ?: "", sink = sink).also { it.connect() }
    }

    private val listener = object : VoiceClient.Listener {
        override fun onOpen() {
            synchronized(frameLock) {
                opened = true; everOpened = true
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
        override fun onAssistantDelta(text: String) { tell(WearLink.REPLY, JSONObject().put("text", text)) }
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
                // Refused while the phone's session let go of the conversation: try again shortly.
                if (!everOpened && attempts < 4) main.postDelayed({ connectVoice() }, 1500)
                else close("voice connection closed")
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
