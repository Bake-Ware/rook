package systems.bake.rook

import android.Manifest
import android.app.NotificationManager
import android.annotation.SuppressLint
import android.content.Context
import android.content.pm.PackageManager
import android.media.AudioFormat
import android.media.AudioManager
import android.media.AudioRecord
import android.media.MediaRecorder
import android.os.Build
import android.os.SystemClock
import android.telephony.TelephonyManager
import android.util.Log
import org.json.JSONObject
import java.util.concurrent.atomic.AtomicBoolean
import kotlin.concurrent.thread

/**
 * attention.remind (rook_android/plugins/attention_android.py): speak a short reminder only
 * when the room is quiet. The hub decides when and what; the phone checks and speaks.
 *
 * [start] returns at once; one reminder runs at a time on its own thread (never a Chaquopy
 * thread): the pre-checks ([ReminderGate.precheck]), a quiet sample of a few seconds
 * ([QuietCheck]), then the line through [SpeakBridge] with the app's selected voice. Every
 * accepted reminder ends with one `reminded` event in the attention log.
 *
 * Privacy: the sample is read into a small buffer that is reduced to one level per 30 ms frame
 * and overwritten by the next read; audio is never written, kept, logged or sent. Only the
 * outcome and two rounded numbers (share of active frames, peak level) reach the event.
 *
 * The microphone: when wake-word standby is already capturing, the check reads levels from the
 * frames standby reads anyway ([VoiceService.levelTap]); no second recorder is opened. Otherwise
 * it opens a short AudioRecord (16 kHz mono, VOICE_RECOGNITION, not privacy-sensitive, so a
 * foreground app that wants the mic gets it and Rook is silenced) and releases it on every path.
 * Another recorder, a call or Rook's own voice session ends the check at once.
 */
object SpokenReminder {
    private const val TAG = "RookRemind"
    /** SharedPreferences("rook") key of the Attention tab's "Spoken reminders" switch (default on). */
    const val PREF = "attention_spoken_reminders"
    private const val CHECK_EVERY_MS = 100L
    private const val SPEAK_TIMEOUT_MS = 120_000L
    private const val SPEAK_POLL_MS = 200L

    private val running = AtomicBoolean(false)

    fun enabled(ctx: Context): Boolean =
        ctx.getSharedPreferences("rook", Context.MODE_PRIVATE).getBoolean(PREF, true)

    fun setEnabled(ctx: Context, on: Boolean) =
        ctx.getSharedPreferences("rook", Context.MODE_PRIVATE).edit().putBoolean(PREF, on).apply()

    fun micGranted(ctx: Context): Boolean = Build.VERSION.SDK_INT < Build.VERSION_CODES.M ||
        ctx.checkSelfPermission(Manifest.permission.RECORD_AUDIO) == PackageManager.PERMISSION_GRANTED

    // ---- attention.remind (Python, any thread; returns at once) ------------------------------

    @JvmStatic fun start(ctx: Context, id: String, text: String, sampleMs: Int, speakUnchecked: Boolean): String {
        val app = ctx.applicationContext
        val cid = id.trim()
        val line = text.split(Regex("\\s+")).filter { it.isNotEmpty() }.joinToString(" ")
        if (cid.isEmpty() || cid.length > ReminderGate.ID_MAX) return err("id must be 1-${ReminderGate.ID_MAX} characters")
        if (line.isEmpty() || line.length > ReminderGate.TEXT_MAX) return err("text must be 1-${ReminderGate.TEXT_MAX} characters")
        val out = JSONObject().put("ok", true).put("id", cid)
        if (!enabled(app)) {
            AttentionStore.reminded(app, cid, ReminderGate.OFF, null, null, null)
            return out.put("accepted", false).put("reason", "off").toString()
        }
        if (!running.compareAndSet(false, true)) return out.put("accepted", false).put("busy", "another reminder is running").toString()
        try {
            thread(name = "rook-remind", isDaemon = true) {
                try { run(app, cid, line, ReminderGate.clampSample(sampleMs), speakUnchecked) }
                finally { running.set(false) }
            }
        } catch (e: Throwable) {
            running.set(false)
            return err("could not start: ${e.message}")
        }
        return out.put("accepted", true).toString()
    }

    private fun err(message: String) = JSONObject().put("ok", false).put("error", message).toString()

    // ---- the reminder (its own thread) -------------------------------------------------------

    private class Sample(val result: QuietCheck.Result?, val abort: String? = null)

    private fun run(app: Context, id: String, text: String, sampleMs: Int, speakUnchecked: Boolean) {
        var outcome = ReminderGate.ERROR
        var via: String? = null
        var level: QuietCheck.Result? = null
        var checked = true
        try {
            val pre = ReminderGate.precheck(signals(app))
            if (pre != null) { outcome = pre; return }
            val sample = sample(app, sampleMs)
            level = sample.result?.takeIf { it.frames > 0 }
            if (sample.abort != null) { outcome = sample.abort; return }
            val stop = ReminderGate.afterSample(sample.result?.outcome, speakUnchecked)
            if (stop != null) { outcome = stop; return }
            checked = sample.result?.outcome == QuietCheck.Outcome.QUIET
            // The sample took a few seconds: look again right before speaking.
            ReminderGate.precheck(signals(app))?.let { outcome = it; return }
            val spoke = speak(app, text)
            if (spoke != null) { outcome = ReminderGate.SPOKEN; via = spoke }
        } catch (e: Throwable) {
            Log.w(TAG, "reminder failed: ${e.javaClass.simpleName}")
            outcome = ReminderGate.ERROR
        } finally {
            Log.i(TAG, "reminder $id: $outcome${via?.let { " via $it" } ?: ""}")
            AttentionStore.reminded(app, id, outcome, via, level, if (outcome == ReminderGate.SPOKEN && !checked) false else null)
        }
    }

    /** Speak through the app's voice.speak path; the route ("server"/"device") once finished, null on failure. */
    private fun speak(app: Context, text: String): String? {
        val r = JSONObject(SpeakBridge.speak(app, text, "", 1f, 1f, false))
        if (!r.optBoolean("ok")) return null
        val sid = r.optString("id")
        val deadline = SystemClock.elapsedRealtime() + SPEAK_TIMEOUT_MS
        while (SystemClock.elapsedRealtime() < deadline) {
            Thread.sleep(SPEAK_POLL_MS)
            val st = JSONObject(SpeakBridge.status(sid))
            if (!st.optBoolean("ok")) return null
            if (st.optBoolean("done")) {
                return if (st.optString("state") == SpeakQueue.DONE) st.optString("via").ifEmpty { "device" } else null
            }
        }
        Log.w(TAG, "reminder speech did not finish in ${SPEAK_TIMEOUT_MS / 1000}s")
        return null
    }

    // ---- the quiet sample -----------------------------------------------------------------------

    private fun sample(app: Context, sampleMs: Int): Sample {
        val voice = VoiceService.inst
        return if (voice != null && voice.standbyCapturing()) sampleFromStandby(app, sampleMs)
        else sampleOwn(app, sampleMs)
    }

    /** Levels from the frames wake standby already reads; no recorder of our own. */
    private fun sampleFromStandby(app: Context, sampleMs: Int): Sample {
        val qc = QuietCheck()
        VoiceService.levelTap = { buf -> synchronized(qc) { qc.feedBytes(buf) } }
        try {
            val end = SystemClock.elapsedRealtime() + sampleMs
            while (SystemClock.elapsedRealtime() < end) {
                Thread.sleep(CHECK_EVERY_MS)
                ReminderGate.precheck(signals(app))?.let { return Sample(synchronized(qc) { qc.result() }, it) }
            }
        } finally { VoiceService.levelTap = null }
        val (measured, result) = synchronized(qc) { qc.measuredMs to qc.result() }
        if (ReminderGate.shortSample(measured, sampleMs)) {
            // Standby let go of the mic meanwhile (a call, another app, Rook speaking).
            ReminderGate.precheck(signals(app))?.let { return Sample(result, it) }
            return Sample(null)
        }
        return Sample(result)
    }

    /** A short recorder of our own, released on every path. */
    @SuppressLint("MissingPermission")
    private fun sampleOwn(app: Context, sampleMs: Int): Sample {
        if (!micGranted(app)) return Sample(null)
        val sr = QuietCheck.SAMPLE_RATE
        val minBuf = AudioRecord.getMinBufferSize(sr, AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT)
        if (minBuf <= 0) return Sample(null)
        val frameBytes = sr * QuietCheck.FRAME_MS / 1000 * 2
        var rec: AudioRecord? = null
        val qc = QuietCheck()
        val buf = ByteArray(frameBytes)
        try {
            rec = try {
                if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.R) {
                    AudioRecord.Builder()
                        .setAudioSource(MediaRecorder.AudioSource.VOICE_RECOGNITION)
                        .setAudioFormat(AudioFormat.Builder().setSampleRate(sr)
                            .setChannelMask(AudioFormat.CHANNEL_IN_MONO).setEncoding(AudioFormat.ENCODING_PCM_16BIT).build())
                        .setBufferSizeInBytes(maxOf(minBuf, frameBytes * 8))
                        .setPrivacySensitive(false)
                        .build()
                } else AudioRecord(MediaRecorder.AudioSource.VOICE_RECOGNITION, sr, AudioFormat.CHANNEL_IN_MONO,
                    AudioFormat.ENCODING_PCM_16BIT, maxOf(minBuf, frameBytes * 8))
            } catch (e: Exception) { Log.i(TAG, "quiet check: no recorder (${e.javaClass.simpleName})"); null }
            val r = rec ?: return Sample(null)
            if (r.state != AudioRecord.STATE_INITIALIZED) return Sample(null)
            val mine = r.audioSessionId
            try { r.startRecording() } catch (e: IllegalStateException) { return Sample(null) }
            if (r.recordingState != AudioRecord.RECORDSTATE_RECORDING) return Sample(null)
            val end = SystemClock.elapsedRealtime() + sampleMs
            var nextCheck = SystemClock.elapsedRealtime() + CHECK_EVERY_MS
            while (SystemClock.elapsedRealtime() < end) {
                val n = r.read(buf, 0, buf.size)
                if (n <= 0) break
                qc.feedBytes(buf, n)
                if (SystemClock.elapsedRealtime() >= nextCheck) {
                    nextCheck = SystemClock.elapsedRealtime() + CHECK_EVERY_MS
                    val s = signals(app, mine)
                    // Another app wants the mic, or a call or Rook's voice started: let go now.
                    ReminderGate.precheck(s)?.let { return Sample(qc.result(), it) }
                    if (ownSilenced(app, mine)) return Sample(qc.result().copy(outcome = QuietCheck.Outcome.SILENCED))
                }
            }
            return Sample(qc.result())
        } finally {
            java.util.Arrays.fill(buf, 0)   // the last 30 ms read: not even that outlives the check
            rec?.let { r ->
                try { if (r.recordingState == AudioRecord.RECORDSTATE_RECORDING) r.stop() } catch (_: Exception) {}
                try { r.release() } catch (_: Exception) {}
            }
        }
    }

    // ---- platform signals ---------------------------------------------------------------------

    private fun audio(app: Context) = app.getSystemService(Context.AUDIO_SERVICE) as AudioManager

    private fun recorders(app: Context, mine: Int): ReminderGate.Recorders {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.N) return ReminderGate.Recorders(false, false)
        val list = try {
            audio(app).activeRecordingConfigurations.map {
                ReminderGate.Recorder(it.clientAudioSessionId, it.clientAudioSource,
                    Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q && it.isClientSilenced)
            }
        } catch (_: Exception) { return ReminderGate.Recorders(false, false) }
        val own = setOf(VoiceService.inst?.ownMicSession() ?: 0, mine).filter { it != 0 }.toSet()
        return ReminderGate.recorders(list, own, mine)
    }

    private fun ownSilenced(app: Context, mine: Int) = recorders(app, mine).mineSilenced

    private fun signals(app: Context, mine: Int = 0): ReminderGate.Signals {
        val am = audio(app)
        val voice = VoiceService.inst
        val yielded = try { voice?.micYieldReason() } catch (_: Exception) { null }
        val telephony = if (Build.VERSION.SDK_INT < Build.VERSION_CODES.S) try {
            // Below Android 12 the call state needs no permission (12+ asks for READ_PHONE_STATE).
            @Suppress("DEPRECATION")
            (app.getSystemService(Context.TELEPHONY_SERVICE) as? TelephonyManager)?.callState
                ?.let { it != TelephonyManager.CALL_STATE_IDLE } ?: false
        } catch (_: Exception) { false } else false
        // Do Not Disturb / bedtime keeps reminders out of a sleeping room (no permission needed to read it).
        val dnd = try {
            (app.getSystemService(Context.NOTIFICATION_SERVICE) as? NotificationManager)?.currentInterruptionFilter
                ?.let { it != NotificationManager.INTERRUPTION_FILTER_ALL } ?: false
        } catch (_: Exception) { false }
        return ReminderGate.Signals(
            dnd = dnd,
            audioMode = try { am.mode } catch (_: Exception) { AudioManager.MODE_NORMAL },
            rookOwnsCallMode = try { voice?.ownsCallMode() == true } catch (_: Exception) { false },
            telephonyCall = telephony || yielded == MicYieldPolicy.Reason.CALL,
            voiceSession = try { voice != null && (voice.inConversation() || voice.sessionOpen()) } catch (_: Exception) { false },
            voiceState = VoiceBus.state,
            othersRecording = recorders(app, mine).others || yielded == MicYieldPolicy.Reason.OTHER_APP,
            musicActive = try { am.isMusicActive } catch (_: Exception) { false },
            speaking = SpeakBridge.busy(),
        )
    }
}
