package systems.bake.rook

import android.annotation.SuppressLint
import android.content.Context
import android.content.pm.PackageManager
import android.media.AudioFormat
import android.media.AudioManager
import android.media.AudioRecord
import android.media.MediaRecorder
import android.media.ToneGenerator
import android.os.Build
import java.io.ByteArrayOutputStream
import kotlin.math.sqrt

/**
 * Records one voice.speak reply (docs/design/voice-replies.md): a short beep, then
 * 16 kHz mono PCM until [ReplyEndpointer] says the reply is over. Blocking: call it
 * off the main thread. The caller makes sure wake standby has let go of the mic.
 */
object ReplyCapture {
    const val SR = 16_000
    private const val FRAME = SR / 50 * 2            // 20 ms of 16-bit samples

    class Outcome(val pcm: ByteArray?, val state: String, val error: String? = null)

    @SuppressLint("MissingPermission")
    fun record(ctx: Context, startTimeoutMs: Int, cancelled: () -> Boolean): Outcome {
        if (ctx.checkSelfPermission(android.Manifest.permission.RECORD_AUDIO) != PackageManager.PERMISSION_GRANTED)
            return Outcome(null, "error", "microphone permission not granted")
        beep()
        val minBuf = AudioRecord.getMinBufferSize(SR, AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT)
        val size = maxOf(minBuf, FRAME * 8)
        val rec = try {
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.R) AudioRecord.Builder()
                .setAudioSource(MediaRecorder.AudioSource.VOICE_RECOGNITION)
                .setAudioFormat(AudioFormat.Builder().setSampleRate(SR).setChannelMask(AudioFormat.CHANNEL_IN_MONO)
                    .setEncoding(AudioFormat.ENCODING_PCM_16BIT).build())
                .setBufferSizeInBytes(size).setPrivacySensitive(false).build()
            else AudioRecord(MediaRecorder.AudioSource.VOICE_RECOGNITION, SR, AudioFormat.CHANNEL_IN_MONO,
                             AudioFormat.ENCODING_PCM_16BIT, size)
        } catch (e: Exception) { return Outcome(null, "error", "microphone unavailable: ${e.message}") }
        try {
            if (rec.state != AudioRecord.STATE_INITIALIZED) return Outcome(null, "error", "microphone unavailable")
            rec.startRecording()
            if (rec.recordingState != AudioRecord.RECORDSTATE_RECORDING) return Outcome(null, "error", "microphone busy")
            val end = ReplyEndpointer(startTimeoutMs)
            val out = ByteArrayOutputStream()
            val buf = ByteArray(FRAME)
            while (true) {
                if (cancelled()) return Outcome(null, "cancelled")
                var off = 0
                while (off < buf.size) {
                    val n = rec.read(buf, off, buf.size - off)
                    if (n <= 0) return Outcome(null, "error", "microphone read failed")
                    off += n
                }
                out.write(buf)
                when (end.feed(level(buf))) {
                    ReplyEndpointer.State.LISTENING -> continue
                    ReplyEndpointer.State.NO_SPEECH -> return Outcome(null, "none")
                    else -> return Outcome(out.toByteArray(), "speech")
                }
            }
        } finally {
            try { rec.stop() } catch (_: Exception) {}
            rec.release()
        }
    }

    private fun level(frame: ByteArray): Float {
        var sum = 0.0
        var i = 0
        while (i + 1 < frame.size) {
            val s = ((frame[i + 1].toInt() shl 8) or (frame[i].toInt() and 0xff)).toShort() / 32768.0
            sum += s * s; i += 2
        }
        return sqrt(sum / (frame.size / 2)).toFloat()
    }

    /** "Your turn": a short tone, then a pause so the recording doesn't start on it. */
    private fun beep() {
        try {
            val tone = ToneGenerator(AudioManager.STREAM_MUSIC, 70)
            tone.startTone(ToneGenerator.TONE_PROP_BEEP2, 150)
            Thread.sleep(260)
            tone.release()
        } catch (_: Throwable) {}
    }
}
