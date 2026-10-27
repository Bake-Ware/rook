package systems.bake.rook.wear

import android.Manifest
import android.annotation.SuppressLint
import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.content.pm.ServiceInfo
import android.media.AudioFormat
import android.media.AudioRecord
import android.media.MediaRecorder
import android.os.Handler
import android.os.IBinder
import android.os.Looper
import android.os.PowerManager
import android.os.VibrationEffect
import android.os.Vibrator
import android.util.Log
import com.google.android.gms.tasks.Tasks
import com.google.android.gms.wearable.CapabilityClient
import com.google.android.gms.wearable.ChannelClient
import com.google.android.gms.wearable.Node
import com.google.android.gms.wearable.Wearable
import org.json.JSONArray
import org.json.JSONObject
import systems.bake.rook.wearlink.AudioEncoder
import systems.bake.rook.wearlink.Codecs
import systems.bake.rook.wearlink.LinkMessage
import systems.bake.rook.wearlink.LinkReader
import systems.bake.rook.wearlink.LinkWriter
import systems.bake.rook.wearlink.Pcm
import systems.bake.rook.wearlink.Transcript
import systems.bake.rook.wearlink.WearLink
import java.io.IOException
import java.util.concurrent.TimeUnit
import kotlin.concurrent.thread

/**
 * One conversation: a microphone foreground service that streams the watch's mic to the Rook
 * app on the phone over a Data Layer channel and plays the replies that come back. The phone
 * ends it when nothing is said for a while; a tap ends it sooner, or stops a reply.
 */
class TalkService : Service() {
    companion object {
        private const val TAG = "TalkService"
        private const val ACTION_START = "systems.bake.rook.wear.START"
        private const val ACTION_TAP = "systems.bake.rook.wear.TAP"
        private const val ACTION_END = "systems.bake.rook.wear.END"
        private const val CHANNEL = "rook_talk"
        private const val NOTIF_ID = 1
        /** A conversation never holds the mic longer than this. */
        private const val MAX_SESSION_MS = 10 * 60_000L
        /** Mic frames kept until the phone is ready (2 s). */
        private const val PRE_READY_FRAMES = 100

        fun start(ctx: Context) = ctx.startForegroundService(Intent(ctx, TalkService::class.java).setAction(ACTION_START))
        fun tap(ctx: Context) = ctx.startService(Intent(ctx, TalkService::class.java).setAction(ACTION_TAP))
        fun end(ctx: Context) = ctx.startService(Intent(ctx, TalkService::class.java).setAction(ACTION_END))
    }

    private val main = Handler(Looper.getMainLooper())
    private val channels by lazy { Wearable.getChannelClient(this) }
    @Volatile private var running = false
    @Volatile private var ready = false
    @Volatile private var writer: LinkWriter? = null
    @Volatile private var channel: ChannelClient.Channel? = null
    @Volatile private var uplink: AudioEncoder? = null
    @Volatile private var player: ReplyPlayer? = null
    private var wakeLock: PowerManager.WakeLock? = null
    @Volatile private var buzzedListening = false

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        when (intent?.action) {
            ACTION_START -> begin()
            ACTION_TAP -> tapped()
            ACTION_END -> finish(null)
            else -> if (!running) stopSelf()
        }
        return START_NOT_STICKY
    }

    private fun begin() {
        if (running) return
        if (checkSelfPermission(Manifest.permission.RECORD_AUDIO) != PackageManager.PERMISSION_GRANTED) {
            Talk.update { it.ended("Rook needs the microphone") }
            stopSelf(); return
        }
        running = true; ready = false; buzzedListening = false
        startForeground(NOTIF_ID, notification(), ServiceInfo.FOREGROUND_SERVICE_TYPE_MICROPHONE)
        wakeLock = (getSystemService(POWER_SERVICE) as PowerManager)
            .newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "rook:talk").apply { acquire(MAX_SESSION_MS + 5_000) }
        Talk.update { Talk.State(running = true, transcript = it.transcript.done()) }
        TalkTile.follow(this)
        main.postDelayed(tooLong, MAX_SESSION_MS)
        thread(name = "talk-link") { link() }
    }

    private val tooLong = Runnable { finish(null) }

    /** Tap while it talks or thinks: stop the reply. Otherwise: end the conversation. */
    private fun tapped() {
        if (!running) return
        when (Talk.state.value.phase) {
            Talk.Phase.SPEAKING, Talk.Phase.THINKING -> send(WearLink.STOP)
            else -> finish(null)
        }
    }

    private fun link() {
        var failure: String? = null
        try {
            val node = findPhone() ?: throw IOException("Can't reach the phone. Is it nearby, with the new Rook app?")
            val ch = Tasks.await(channels.openChannel(node.id, WearLink.PATH), 10, TimeUnit.SECONDS)
            channel = ch
            val out = Tasks.await(channels.getOutputStream(ch), 10, TimeUnit.SECONDS)
            val input = Tasks.await(channels.getInputStream(ch), 10, TimeUnit.SECONDS)
            val w = LinkWriter(out).also { writer = it }
            // Opus when this watch can encode it; the phone answers with what it can decode.
            val enc = Codecs.encoder(WearLink.CODEC_OPUS, WearLink.SR_IN).also { uplink = it }
            w.send(WearLink.HELLO, JSONObject().put("v", WearLink.VERSION).put("codec", enc.codec).put("sr", WearLink.SR_IN)
                .put("accepts", JSONArray().put(WearLink.CODEC_OPUS).put(WearLink.CODEC_PCM)))
            player = ReplyPlayer(
                played = { turn, frames -> send(WearLink.PLAYED, JSONObject().put("turn", turn).put("frames", frames)) },
                playing = { on -> changed { it.copy(playing = on) } },
            )
            thread(name = "talk-mic") { mic() }
            val reader = LinkReader(input)
            while (running) {
                val m = reader.next() ?: break
                if (!handle(m)) break
            }
        } catch (e: Exception) {
            if (running) { Log.w(TAG, "conversation failed", e); failure = e.message ?: "Lost the phone" }
        }
        main.post { finish(failure) }
    }

    private fun findPhone(): Node? {
        val info = Tasks.await(Wearable.getCapabilityClient(this)
            .getCapability(WearLink.CAPABILITY, CapabilityClient.FILTER_REACHABLE), 10, TimeUnit.SECONDS)
        return info.nodes.firstOrNull { it.isNearby } ?: info.nodes.firstOrNull()
    }

    /** Link thread. False: the conversation is over. */
    private fun handle(m: LinkMessage): Boolean {
        when (m.type) {
            WearLink.READY -> {
                if (m.json().optString("uplink") != uplink?.codec) { uplink?.close(); uplink = Pcm(WearLink.SR_IN) }
                ready = true
                changed { it.copy(ready = true) }
            }
            WearLink.STATE -> { val st = m.json().optString("state"); changed { it.copy(server = st) } }
            WearLink.HISTORY -> { val h = Transcript.fromJson(m.json()); Talk.update { it.copy(transcript = h) } }
            WearLink.HEARD -> { val t = m.json().optString("text"); Talk.update { it.copy(transcript = it.transcript.heard(t)) } }
            WearLink.REPLY -> { val t = m.json().optString("text"); Talk.update { it.copy(transcript = it.transcript.reply(t)) } }
            WearLink.REPLY_DONE -> Talk.update { it.copy(transcript = it.transcript.done()) }
            WearLink.OUT_FORMAT -> m.json().let { player?.format(it.optString("codec", WearLink.CODEC_PCM), it.optInt("sr", 24000), it.optInt("turn")) }
            WearLink.OUT_AUDIO -> player?.audio(m.payload)
            WearLink.FLUSH -> player?.flush()
            WearLink.PAUSE -> player?.pause()
            WearLink.RESUME -> player?.resume()
            WearLink.ERROR -> { val msg = m.json().optString("msg"); Talk.update { it.copy(transcript = it.transcript.note(msg)) } }
            WearLink.BYE -> return false
        }
        return true
    }

    /** Any thread: change a fact; the first time Rook listens, the watch taps the wrist. */
    private fun changed(change: (Talk.State) -> Talk.State) {
        Talk.update(change)
        if (!buzzedListening && Talk.state.value.phase == Talk.Phase.LISTENING) { buzzedListening = true; buzz() }
    }

    @SuppressLint("MissingPermission")
    private fun mic() {
        var rec: AudioRecord? = null
        try {
            val minBuf = AudioRecord.getMinBufferSize(WearLink.SR_IN, AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT)
            rec = AudioRecord(MediaRecorder.AudioSource.VOICE_RECOGNITION, WearLink.SR_IN, AudioFormat.CHANNEL_IN_MONO,
                AudioFormat.ENCODING_PCM_16BIT, maxOf(minBuf, WearLink.FRAME_BYTES * 8))
            check(rec.state == AudioRecord.STATE_INITIALIZED) { "microphone unavailable" }
            rec.startRecording()
            val early = ArrayDeque<ByteArray>()
            val buf = ByteArray(WearLink.FRAME_BYTES)
            while (running) {
                var off = 0
                while (off < buf.size && running) {
                    val n = rec.read(buf, off, buf.size - off)
                    check(n > 0) { "microphone read failed" }
                    off += n
                }
                if (!running) break
                if (!ready) {
                    early.addLast(buf.copyOf()); while (early.size > PRE_READY_FRAMES) early.removeFirst()
                    continue
                }
                while (early.isNotEmpty()) sendMic(early.removeFirst())
                sendMic(buf)
            }
        } catch (e: Exception) {
            if (running) { Log.w(TAG, "mic failed", e); main.post { finish("Microphone: ${e.message}") } }
        } finally {
            try { rec?.stop() } catch (_: Exception) {}
            rec?.release()
        }
    }

    private fun sendMic(frame: ByteArray) {
        val enc = uplink ?: return
        for (packet in enc.encode(frame)) send(WearLink.MIC, packet)
    }

    private fun send(type: Int, json: JSONObject? = null, payload: ByteArray? = null) {
        val w = writer ?: return
        try {
            when {
                json != null -> w.send(type, json)
                payload != null -> w.send(type, payload)
                else -> w.send(type)
            }
        } catch (e: Exception) {
            if (running) { Log.w(TAG, "link write failed", e); main.post { finish("Lost the phone") } }
        }
    }
    private fun send(type: Int, payload: ByteArray) = send(type, null, payload)

    /** Main thread. Ends the conversation; [failure] stays on screen. */
    private fun finish(failure: String?) {
        if (!running) { stopSelf(); return }
        running = false
        main.removeCallbacks(tooLong)
        val w = writer; writer = null
        if (failure == null) try { w?.send(WearLink.END) } catch (_: Exception) {}
        channel?.let { ch -> try { channels.close(ch) } catch (_: Exception) {} }
        channel = null
        player?.close(); player = null
        thread { uplink?.close(); uplink = null }
        try { wakeLock?.release() } catch (_: Exception) {}
        wakeLock = null
        Talk.update { it.ended(failure) }
        stopForeground(STOP_FOREGROUND_REMOVE)
        stopSelf()
    }

    private fun buzz() {
        try { (getSystemService(VIBRATOR_SERVICE) as Vibrator).vibrate(VibrationEffect.createPredefined(VibrationEffect.EFFECT_CLICK)) }
        catch (_: Exception) {}
    }

    private fun notification(): Notification {
        val nm = getSystemService(NotificationManager::class.java)
        nm.createNotificationChannel(NotificationChannel(CHANNEL, getString(R.string.channel_name), NotificationManager.IMPORTANCE_LOW))
        val open = PendingIntent.getActivity(this, 0, Intent(this, TalkActivity::class.java),
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE)
        val end = PendingIntent.getService(this, 1, Intent(this, TalkService::class.java).setAction(ACTION_END),
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE)
        return Notification.Builder(this, CHANNEL)
            .setSmallIcon(android.R.drawable.ic_btn_speak_now)
            .setContentTitle(getString(R.string.app_name))
            .setContentText(getString(R.string.notif_text))
            .setOngoing(true)
            .setContentIntent(open)
            .addAction(Notification.Action.Builder(null, "End", end).build())
            .build()
    }

    override fun onDestroy() {
        if (running) finish(null)
        super.onDestroy()
    }
}
