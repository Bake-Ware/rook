package systems.bake.rook

import android.content.Context
import android.media.AudioAttributes
import android.media.AudioManager
import android.media.MediaPlayer
import android.media.RingtoneManager
import android.os.Handler
import android.os.Looper
import org.json.JSONObject

/** Alarm stream avoids changing the device's ringer mode. DND policy still applies. */
object FindDeviceBridge {
    private var player: MediaPlayer? = null
    private var audio: AudioManager? = null
    private var previousVolume = 0
    private val handler = Handler(Looper.getMainLooper())
    private val stopTask = Runnable { stop() }

    @JvmStatic @Synchronized fun ring(ctx: Context, seconds: Int): String {
        stop()
        val duration = seconds.coerceIn(1, 120)
        return try {
            val manager = ctx.getSystemService(Context.AUDIO_SERVICE) as AudioManager
            audio = manager
            previousVolume = manager.getStreamVolume(AudioManager.STREAM_ALARM)
            manager.setStreamVolume(AudioManager.STREAM_ALARM, manager.getStreamMaxVolume(AudioManager.STREAM_ALARM), 0)
            val uri = RingtoneManager.getDefaultUri(RingtoneManager.TYPE_ALARM)
                ?: RingtoneManager.getDefaultUri(RingtoneManager.TYPE_NOTIFICATION)
            val sound = MediaPlayer()
            player = sound
            sound.setAudioAttributes(AudioAttributes.Builder().setUsage(AudioAttributes.USAGE_ALARM)
                .setContentType(AudioAttributes.CONTENT_TYPE_SONIFICATION).build())
            sound.setDataSource(ctx, uri)
            sound.isLooping = true
            sound.setOnErrorListener { _, _, _ -> stop(); true }
            sound.prepare()
            sound.start()
            handler.postDelayed(stopTask, duration * 1000L)
            JSONObject().put("ok", true).put("ringing", true).put("seconds", duration)
                .put("volume", manager.getStreamVolume(AudioManager.STREAM_ALARM))
                .put("note", "Alarm volume raised to maximum; device Do Not Disturb policy applies. Volume restores when stopped.").toString()
        } catch (e: Exception) {
            stop()
            JSONObject().put("ok", false).put("error", e.message).toString()
        }
    }

    @JvmStatic @Synchronized fun stop(): String {
        handler.removeCallbacks(stopTask)
        try { player?.release() } finally { player = null }
        try { audio?.setStreamVolume(AudioManager.STREAM_ALARM, previousVolume, 0) } catch (_: Exception) {}
        audio = null
        return JSONObject().put("ok", true).put("ringing", false).toString()
    }
}
