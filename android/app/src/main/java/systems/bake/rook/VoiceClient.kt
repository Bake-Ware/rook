package systems.bake.rook

import android.annotation.SuppressLint
import android.content.Context
import android.media.AudioAttributes
import android.media.AudioFormat
import android.media.AudioManager
import android.media.AudioRecord
import android.media.AudioTrack
import android.media.MediaRecorder
import android.media.audiofx.AcousticEchoCanceler
import android.media.audiofx.NoiseSuppressor
import android.util.Log
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.Response
import okhttp3.WebSocket
import okhttp3.WebSocketListener
import okio.ByteString
import okio.ByteString.Companion.toByteString
import org.json.JSONObject
import java.security.SecureRandom
import java.security.cert.X509Certificate
import java.util.concurrent.LinkedBlockingQueue
import java.util.concurrent.TimeUnit
import javax.net.ssl.SSLContext
import javax.net.ssl.TrustManager
import javax.net.ssl.X509TrustManager
import kotlin.concurrent.thread

/**
 * Client for the kaiju voice-agent WebSocket (`/ws`).
 *
 * Wire protocol (mirrors static/index.html on the server):
 *   client -> server : binary frames, exactly 640 bytes = 20 ms of 16 kHz mono PCM16 LE
 *                      text JSON {"type":"stop"} = interrupt current turn
 *                      text JSON {"type":"voice","voice":...} = pick TTS voice
 *   server -> client : text JSON {"type": state|stt|assistant_delta|assistant_done|
 *                                 thought|tool|interrupt|error|audio_sr, ...}
 *                      binary frames = PCM16 LE at the last announced audio_sr
 *
 * Endpointing (VAD) lives on the server, so we just stream the mic continuously
 * while connected. Mic uses VOICE_COMMUNICATION so the platform AEC kills our
 * own playback before it reaches the server.
 */
class VoiceClient(
    private val ctx: Context,
    private val url: String,
    private val insecureTls: Boolean,
    private val listener: Listener,
    /** false = caller pushes mic frames via [pushFrame] (shared AudioRecord); true = own the mic. */
    private val ownMic: Boolean = true,
) {
    interface Listener {
        fun onState(state: String)
        fun onTranscript(text: String)
        fun onAssistantDelta(text: String)
        fun onAssistantDone()
        fun onInterrupt()
        fun onError(msg: String)
        fun onClosed()
    }

    companion object {
        private const val TAG = "VoiceClient"
        const val SR_IN = 16000
        const val FRAME_BYTES = 640 // 20 ms @ 16 kHz mono PCM16
    }

    @Volatile private var ws: WebSocket? = null
    @Volatile private var running = false
    private var recThread: Thread? = null
    private var playThread: Thread? = null
    private val playQueue = LinkedBlockingQueue<ByteArray>()
    @Volatile private var outSr = 24000
    @Volatile private var track: AudioTrack? = null
    @Volatile private var trackSr = 0
    private var audioChunks = 0L
    private var audioBytes = 0L

    fun connect() {
        val builder = OkHttpClient.Builder()
            .readTimeout(0, TimeUnit.MILLISECONDS)
            .pingInterval(20, TimeUnit.SECONDS)
        if (insecureTls) trustAll(builder)
        val client = builder.build()
        val req = Request.Builder().url(url).build()
        running = true
        startPlayer()
        ws = client.newWebSocket(req, object : WebSocketListener() {
            override fun onOpen(webSocket: WebSocket, response: Response) {
                Log.i(TAG, "ws open $url")
                // Advance notice for the server: this client has hardware AEC.
                // (Ignored by the server today; step-4 barge-in will key off it.)
                webSocket.send(JSONObject().put("type", "hello")
                    .put("client", "rook-android").put("aec", true).toString())
                if (ownMic) startMic()
                listener.onState("listening")
            }

            override fun onMessage(webSocket: WebSocket, text: String) {
                val m = try { JSONObject(text) } catch (_: Throwable) { return }
                when (m.optString("type")) {
                    "state" -> listener.onState(m.optString("state"))
                    "stt" -> listener.onTranscript(m.optString("text"))
                    "assistant_delta" -> listener.onAssistantDelta(m.optString("text"))
                    "assistant" -> listener.onAssistantDelta(m.optString("text"))
                    "assistant_done" -> listener.onAssistantDone()
                    "audio_sr" -> outSr = m.optInt("sr", 24000)
                    "interrupt" -> { flushPlayback(); listener.onInterrupt() }
                    "error" -> listener.onError(m.optString("msg"))
                }
            }

            override fun onMessage(webSocket: WebSocket, bytes: ByteString) {
                if (running) { audioBytes += bytes.size; if ((++audioChunks % 25) == 1L) Log.i(TAG, "audio chunk #$audioChunks total=${audioBytes}B sr=$outSr"); playQueue.offer(bytes.toByteArray()) }
            }

            override fun onFailure(webSocket: WebSocket, t: Throwable, response: Response?) {
                Log.w(TAG, "ws failure", t)
                listener.onError(t.message ?: t.javaClass.simpleName)
                shutdown()
            }

            override fun onClosed(webSocket: WebSocket, code: Int, reason: String) {
                shutdown()
            }
        })
    }

    /** External mic path: exactly one 20 ms frame (640 bytes) per call. */
    fun pushFrame(buf: ByteArray, len: Int = buf.size) {
        if (running && len == FRAME_BYTES) ws?.send(buf.toByteString(0, len))
    }

    val isRunning: Boolean get() = running

    /** Ask the server to cut the current turn, and drop any queued audio locally. */
    fun interrupt() {
        flushPlayback()
        ws?.send(JSONObject().put("type", "stop").toString())
    }

    fun setVoice(voice: String) {
        ws?.send(JSONObject().put("type", "voice").put("voice", voice).toString())
    }

    fun close() {
        try { ws?.close(1000, "bye") } catch (_: Throwable) {}
        shutdown()
    }

    private fun shutdown() {
        if (!running) return
        running = false
        ws = null
        playQueue.clear()
        try { recThread?.join(500) } catch (_: Throwable) {}
        playQueue.offer(ByteArray(0)) // poison
        try { playThread?.join(500) } catch (_: Throwable) {}
        try { track?.stop() } catch (_: Throwable) {}
        try { track?.release() } catch (_: Throwable) {}
        track = null
        listener.onClosed()
    }

    // ---- mic ------------------------------------------------------------

    @SuppressLint("MissingPermission") // RECORD_AUDIO checked by the caller
    private fun startMic() {
        recThread = thread(name = "voice-mic") {
            val minBuf = AudioRecord.getMinBufferSize(
                SR_IN, AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT)
            val rec = AudioRecord(
                MediaRecorder.AudioSource.VOICE_COMMUNICATION, SR_IN,
                AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT,
                maxOf(minBuf, FRAME_BYTES * 8))
            if (rec.state != AudioRecord.STATE_INITIALIZED) {
                listener.onError("mic init failed"); return@thread
            }
            try { if (AcousticEchoCanceler.isAvailable()) AcousticEchoCanceler.create(rec.audioSessionId)?.enabled = true } catch (_: Throwable) {}
            try { if (NoiseSuppressor.isAvailable()) NoiseSuppressor.create(rec.audioSessionId)?.enabled = true } catch (_: Throwable) {}
            rec.startRecording()
            val buf = ByteArray(FRAME_BYTES)
            try {
                while (running) {
                    var off = 0
                    while (off < FRAME_BYTES && running) {
                        val n = rec.read(buf, off, FRAME_BYTES - off)
                        if (n <= 0) break
                        off += n
                    }
                    if (off == FRAME_BYTES) ws?.send(buf.toByteString(0, FRAME_BYTES))
                }
            } finally {
                try { rec.stop() } catch (_: Throwable) {}
                rec.release()
            }
        }
    }

    // ---- speaker --------------------------------------------------------

    private fun startPlayer() {
        playThread = thread(name = "voice-play") {
            while (running) {
                val chunk = playQueue.take()
                if (chunk.isEmpty()) break
                val t = ensureTrack(outSr) ?: continue
                t.write(chunk, 0, chunk.size)
            }
        }
    }

    private fun ensureTrack(sr: Int): AudioTrack? {
        var t = track
        if (t != null && trackSr == sr) return t
        try { t?.stop(); t?.release() } catch (_: Throwable) {}
        val minBuf = AudioTrack.getMinBufferSize(sr, AudioFormat.CHANNEL_OUT_MONO, AudioFormat.ENCODING_PCM_16BIT)
        t = AudioTrack.Builder()
            .setAudioAttributes(AudioAttributes.Builder()
                .setUsage(AudioAttributes.USAGE_MEDIA)
                .setContentType(AudioAttributes.CONTENT_TYPE_SPEECH).build())
            .setAudioFormat(AudioFormat.Builder().setSampleRate(sr)
                .setEncoding(AudioFormat.ENCODING_PCM_16BIT)
                .setChannelMask(AudioFormat.CHANNEL_OUT_MONO).build())
            .setBufferSizeInBytes(maxOf(minBuf, sr * 2 / 2)) // ~0.5 s
            .setTransferMode(AudioTrack.MODE_STREAM)
            .build()
        t.play()
        Log.i(TAG, "AudioTrack ready sr=$sr state=${t.state} vol=${(ctx.getSystemService(Context.AUDIO_SERVICE) as AudioManager).getStreamVolume(AudioManager.STREAM_MUSIC)}")
        track = t; trackSr = sr
        return t
    }

    private fun flushPlayback() {
        playQueue.clear()
        val t = track ?: return
        try { t.pause(); t.flush(); t.play() } catch (_: Throwable) {}
    }

    // ---- TLS ------------------------------------------------------------

    /** kaiju serves a self-signed cert; opt-in trust-all for that case. */
    private fun trustAll(b: OkHttpClient.Builder) {
        val tm = object : X509TrustManager {
            override fun checkClientTrusted(chain: Array<X509Certificate>, authType: String) {}
            override fun checkServerTrusted(chain: Array<X509Certificate>, authType: String) {}
            override fun getAcceptedIssuers(): Array<X509Certificate> = arrayOf()
        }
        val sc = SSLContext.getInstance("TLS")
        sc.init(null, arrayOf<TrustManager>(tm), SecureRandom())
        b.sslSocketFactory(sc.socketFactory, tm)
        b.hostnameVerifier { _, _ -> true }
    }
}
