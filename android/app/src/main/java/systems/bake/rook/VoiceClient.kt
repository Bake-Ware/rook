package systems.bake.rook

import android.content.Context
import android.media.AudioAttributes
import android.media.AudioFormat
import android.media.AudioTrack
import android.os.SystemClock
import okhttp3.*
import okio.ByteString
import okio.ByteString.Companion.toByteString
import org.json.JSONObject
import java.nio.ByteBuffer
import java.security.MessageDigest
import java.security.SecureRandom
import java.security.cert.X509Certificate
import java.util.UUID
import java.util.concurrent.LinkedBlockingQueue
import java.util.concurrent.TimeUnit
import javax.net.ssl.*
import kotlin.concurrent.thread

/** Versioned voice transport. Audio has response IDs; the shared service owns the mic. */
class VoiceClient(
    private val ctx: Context, private val url: String, private val insecureTls: Boolean,
    private val listener: Listener, private val ownMic: Boolean = false, private val token: String = "",
) {
    interface Listener {
        fun onState(state: String)
        fun onTranscript(text: String)
        fun onAssistantDelta(text: String)
        fun onAssistantDone()
        fun onInterrupt()
        fun onError(msg: String)
        fun onClosed()
        fun onBye(mode: String, afterMs: Long) {}
        fun onTool(title: String, status: String) {}
    }
    companion object { const val SR_IN = 16000; const val FRAME_BYTES = 640; const val PLAY_TAIL_MS = 200L }
    private data class Packet(val turn: Int, val sr: Int, val bytes: ByteArray)
    @Volatile private var ws: WebSocket? = null
    @Volatile private var running = false
    @Volatile private var connected = false
    @Volatile private var aec = false
    @Volatile private var protocol = 1
    @Volatile private var outputTurn = 0
    @Volatile private var minimumTurn = 0
    @Volatile private var waitingInterrupt = false
    @Volatile private var outSr = 24000
    @Volatile private var playingUntil = 0L
    private val queue = LinkedBlockingQueue<Packet>(128)
    private val outbox = LinkedBlockingQueue<String>(16)
    private val audioLock = Any()
    private var track: AudioTrack? = null
    private var trackSr = 0
    private var trackTurn = -1
    private var playThread: Thread? = null
    private var http: OkHttpClient? = null
    private val preroll = ArrayDeque<ByteArray>()
    private var speechFrames = 0
    private var quietFrames = 0
    @Volatile private var paused = false
    private var pausedAt = 0L
    val isRunning get() = running
    val isPlaying get() = SystemClock.elapsedRealtime() < playingUntil + PLAY_TAIL_MS

    fun setAecAvailable(value: Boolean) {
        aec = value
        if (connected) ws?.send(JSONObject().put("type", "audio_config").put("aec", value).toString())
    }
    fun connect() {
        check(!ownMic) { "VoiceService must own the microphone" }
        val builder = OkHttpClient.Builder().readTimeout(0, TimeUnit.MILLISECONDS).pingInterval(20, TimeUnit.SECONDS)
        if (insecureTls) trustAll(builder)
        val client = builder.build(); http = client
        val prefs = ctx.getSharedPreferences("rook", Context.MODE_PRIVATE)
        val scope = MessageDigest.getInstance("SHA-256").digest((url + "\u0000" + token).toByteArray()).joinToString("") { "%02x".format(it) }
        val key = "voice_conversation_$scope"
        val conversation = prefs.getString(key, null) ?: UUID.randomUUID().toString().also { prefs.edit().putString(key, it).apply() }
        val request = Request.Builder().url(url).apply { if (token.isNotEmpty()) header("Authorization", "Bearer $token") }.build()
        running = true
        startPlayer()
        ws = client.newWebSocket(request, object : WebSocketListener() {
            override fun onOpen(webSocket: WebSocket, response: Response) {
                if (!running) { webSocket.close(1000, "closed"); return }
                ws = webSocket
                webSocket.send(JSONObject().put("type", "hello").put("protocol", 2).put("client", "rook-android")
                    .put("conversation", conversation).put("aec", aec).toString())
                connected = true
                while (true) webSocket.send(outbox.poll() ?: break)
            }
            override fun onMessage(webSocket: WebSocket, text: String) {
                if (!running) return
                val m = try { JSONObject(text) } catch (_: Exception) { return }
                when (m.optString("type")) {
                    "session" -> protocol = m.optInt("protocol", 1)
                    "state" -> { if (m.optInt("turn", minimumTurn) >= minimumTurn) listener.onState(m.optString("state")) }
                    "stt" -> listener.onTranscript(m.optString("text"))
                    "assistant_delta", "assistant" -> if (!waitingInterrupt) listener.onAssistantDelta(m.optString("text"))
                    "assistant_done" -> if (!waitingInterrupt) listener.onAssistantDone()
                    "audio_sr" -> { outSr = m.optInt("sr", 24000).coerceIn(8000,48000); outputTurn = m.optInt("turn", 0) }
                    "interrupt" -> { minimumTurn = m.optInt("turn", minimumTurn); flush(); waitingInterrupt = false; listener.onInterrupt() }
                    "bye" -> listener.onBye(m.optString("mode", "sleep"), m.optLong("after_ms", 0))
                    "tool" -> listener.onTool(m.optString("title"), m.optString("status"))
                    "error" -> listener.onError(m.optString("msg"))
                }
            }
            override fun onMessage(webSocket: WebSocket, bytes: ByteString) {
                if (!running || waitingInterrupt) return
                val raw = bytes.toByteArray()
                val framed = protocol >= 2 && raw.size >= 8 && raw.copyOfRange(0,4).contentEquals(byteArrayOf(82,75,50,65))
                if (protocol >= 2 && !framed) return
                val turn = if (framed) ByteBuffer.wrap(raw,4,4).int else outputTurn
                if (turn < minimumTurn) return
                val pcm = if (framed) raw.copyOfRange(8,raw.size) else raw
                if (pcm.size > 19200 || pcm.size % 2 != 0) return
                playingUntil = maxOf(playingUntil, SystemClock.elapsedRealtime()) + pcm.size * 1000L / (2*outSr)
                if (!queue.offer(Packet(turn,outSr,pcm))) { listener.onError("Voice playback fell behind; reconnecting"); close() }
            }
            override fun onFailure(webSocket: WebSocket, error: Throwable, response: Response?) {
                if (running) listener.onError("Voice connection lost; reconnecting")
                shutdown()
            }
            override fun onClosed(webSocket: WebSocket, code: Int, reason: String) = shutdown()
        })
    }

    /** Speech gate runs locally so a real barge-in can pause output before network round-trip. */
    fun pushFrame(buf: ByteArray, len: Int = buf.size, speech: Boolean = false) {
        if (!running || !connected || len != FRAME_BYTES) return
        preroll.addLast(buf.copyOf(len)); while (preroll.size > 25) preroll.removeFirst()
        if (isPlaying || paused) {
            if (!aec || protocol < 2) return
            if (speech) { speechFrames++; quietFrames = 0 } else { quietFrames++; speechFrames = 0 }
            if (speechFrames >= 8 && !paused) synchronized(audioLock) { paused = true; pausedAt = SystemClock.elapsedRealtime(); track?.pause() }
            if (speechFrames >= 18) {
                waitingInterrupt = true
                flush()
                ws?.send(JSONObject().put("type", "speech_start").toString())
                while (preroll.isNotEmpty()) ws?.send(preroll.removeFirst().toByteString())
                speechFrames = 0; quietFrames = 0
                return
            }
            if (paused && quietFrames >= 10) synchronized(audioLock) {
                playingUntil += SystemClock.elapsedRealtime() - pausedAt
                paused = false; track?.play()
            }
            // Send no echo-bearing audio until the local speech gate confirms barge-in.
            return
        }
        speechFrames = 0; quietFrames = 0
        val socket = ws ?: return
        if (socket.queueSize() > 640 * 100) { close(); return }
        socket.send(buf.toByteString(0,len))
    }
    private fun enqueue(m: JSONObject) {
        if (connected) ws?.send(m.toString()) else if (!outbox.offer(m.toString())) listener.onError("Too many pending voice messages")
    }
    fun sendText(text: String, speak: Boolean) = enqueue(JSONObject().put("type", "text").put("text", text).put("speak", speak))
    fun sendImage(b64: String, caption: String, speak: Boolean) = enqueue(JSONObject().put("type", "image").put("data", b64).put("text", caption).put("speak", speak))
    fun setVoice(voice: String) = enqueue(JSONObject().put("type", "voice").put("voice", voice))
    fun interrupt() { waitingInterrupt = protocol >= 2; flush(); enqueue(JSONObject().put("type", "stop")) }
    fun close() { ws?.close(1000, "bye"); shutdown() }
    @Synchronized private fun shutdown() {
        if (!running) return
        running = false; connected = false
        ws?.cancel(); ws = null
        flush(); outbox.clear()
        playThread?.interrupt()
        http?.dispatcher?.executorService?.shutdown(); http?.connectionPool?.evictAll(); http = null
        listener.onClosed()
    }
    private fun flush() = synchronized(audioLock) {
        queue.clear(); playingUntil = 0; paused = false
        try { track?.pause(); track?.flush(); track?.play() } catch (_: Exception) {}
        trackTurn = -1
    }
    private fun startPlayer() {
        playThread = thread(name="voice-play") {
            try {
                while (running) {
                    val packet = queue.poll(200,TimeUnit.MILLISECONDS) ?: continue
                    var offset = 0
                    while (running && offset < packet.bytes.size) {
                        val written = synchronized(audioLock) {
                            if (waitingInterrupt || packet.turn < minimumTurn) -1
                            else if (paused) 0
                            else {
                                var t = track
                                if (t == null || trackSr != packet.sr) {
                                    t?.release()
                                    t = AudioTrack.Builder().setAudioAttributes(AudioAttributes.Builder()
                                        .setUsage(AudioAttributes.USAGE_VOICE_COMMUNICATION).setContentType(AudioAttributes.CONTENT_TYPE_SPEECH).build())
                                        .setAudioFormat(AudioFormat.Builder().setSampleRate(packet.sr).setEncoding(AudioFormat.ENCODING_PCM_16BIT)
                                            .setChannelMask(AudioFormat.CHANNEL_OUT_MONO).build())
                                        .setBufferSizeInBytes(maxOf(AudioTrack.getMinBufferSize(packet.sr,AudioFormat.CHANNEL_OUT_MONO,AudioFormat.ENCODING_PCM_16BIT),packet.sr/5))
                                        .setTransferMode(AudioTrack.MODE_STREAM).build()
                                    track=t; trackSr=packet.sr; t.play()
                                }
                                if (trackTurn != packet.turn) { t.pause(); t.flush(); t.play(); trackTurn=packet.turn }
                                val n=t.write(packet.bytes,offset,packet.bytes.size-offset,AudioTrack.WRITE_NON_BLOCKING)
                                ws?.send(JSONObject().put("type","playback").put("turn",packet.turn)
                                    .put("frames",t.playbackHeadPosition.toLong() and 0xffffffffL).toString())
                                n
                            }
                        }
                        if (written < 0) break
                        if (written == 0) Thread.sleep(5) else offset += written
                    }
                }
            } catch (_: InterruptedException) {
            } catch (_: Exception) { if (running) { listener.onError("Voice playback failed"); shutdown() } }
            finally { synchronized(audioLock) { try { track?.stop(); track?.release() } catch (_: Exception) {}; track=null } }
        }
    }
    private fun trustAll(b: OkHttpClient.Builder) {
        val tm = object : X509TrustManager {
            override fun checkClientTrusted(c: Array<X509Certificate>, a: String) {}
            override fun checkServerTrusted(c: Array<X509Certificate>, a: String) {}
            override fun getAcceptedIssuers() = arrayOf<X509Certificate>()
        }
        val ssl=SSLContext.getInstance("TLS"); ssl.init(null,arrayOf<TrustManager>(tm),SecureRandom())
        b.sslSocketFactory(ssl.socketFactory,tm).hostnameVerifier { _,_ -> true }
    }
}
