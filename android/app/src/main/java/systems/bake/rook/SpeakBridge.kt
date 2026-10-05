package systems.bake.rook

import android.content.Context
import android.content.Intent
import android.media.AudioAttributes
import android.media.AudioFocusRequest
import android.media.AudioManager
import android.os.Build
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.speech.tts.TextToSpeech
import android.speech.tts.UtteranceProgressListener
import android.speech.tts.Voice
import android.util.Log
import org.json.JSONArray
import org.json.JSONObject
import java.util.Locale

/**
 * On-device text-to-speech for the `voice.speak` worker cap (rook_android/plugins/speak_android.py).
 *
 * One [TextToSpeech] engine per process, created lazily on the main thread and kept
 * alive (its init is asynchronous: jobs queue until it reports ready). Speech plays as
 * USAGE_ASSISTANT with transient, may-duck audio focus, so other audio dips rather than
 * stops; the worker's foreground service keeps the process alive with the screen off.
 *
 * All engine and queue work runs on the main looper. The Python side calls [speak],
 * then polls [status] (it never blocks a Chaquopy thread on the engine).
 */
object SpeakBridge {
    private const val TAG = "SpeakBridge"
    private const val INIT_TIMEOUT_MS = 15_000L
    private const val DEFER_POLL_MS = 250L

    private val main = Handler(Looper.getMainLooper())
    private val queue = SpeakQueue()
    private var app: Context? = null
    private var tts: TextToSpeech? = null
    @Volatile private var init = "none"           // none | pending | ready | failed
    @Volatile private var initError: String? = null
    private var focusRequest: AudioFocusRequest? = null
    private var focusHeld = false
    private var pumpScheduled = false
    private var counter = 0L

    private val attrs: AudioAttributes by lazy {
        AudioAttributes.Builder()
            .setUsage(if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) AudioAttributes.USAGE_ASSISTANT
                      else AudioAttributes.USAGE_ASSISTANCE_NAVIGATION_GUIDANCE)
            .setContentType(AudioAttributes.CONTENT_TYPE_SPEECH)
            .build()
    }

    // ---- API for Python (any thread) ------------------------------------

    /** True if some TTS engine is installed (Android 11+ needs the manifest <queries> entry). */
    @JvmStatic fun engineAvailable(ctx: Context): Boolean = try {
        ctx.packageManager.queryIntentServices(Intent(TextToSpeech.Engine.INTENT_ACTION_TTS_SERVICE), 0).isNotEmpty()
    } catch (e: Exception) { false }

    /** Start the engine now so the first voice.speak doesn't pay for init. */
    @JvmStatic fun warmUp(ctx: Context) { app = ctx.applicationContext; main.post { ensureInit() } }

    @JvmStatic fun speak(ctx: Context, text: String, voice: String, rate: Float, pitch: Float,
                         interrupt: Boolean): String {
        app = ctx.applicationContext
        val id = synchronized(this) { "say-${System.currentTimeMillis().toString(36)}-${++counter}" }
        val job = queue.add(SpeakQueue.Job(id, text, voice.trim(), rate, pitch, interrupt))
        main.post { pump() }
        val out = JSONObject().put("ok", true).put("id", job.id).put("state", job.state)
            .put("engine", init).put("reply_playing", replyPlaying())
        mediaVolume(ctx)?.let { (v, max) ->
            out.put("volume", "$v/$max")
            if (v == 0) out.put("warning", "media volume is 0: speech will be inaudible")
        }
        return out.toString()
    }

    @JvmStatic fun status(id: String): String {
        val j = queue.get(id) ?: return JSONObject().put("ok", false).put("error", "unknown speech id $id").toString()
        val out = JSONObject().put("ok", true).put("id", j.id).put("state", j.state).put("done", j.done)
        j.error?.let { out.put("error", it) }
        j.note?.let { out.put("note", it) }
        if (j.state == SpeakQueue.QUEUED) {
            out.put("ahead", queue.ahead(id)).put("engine", init)
            if (replyPlaying()) out.put("waiting_for", "voice reply")
            else if (init != "ready") out.put("waiting_for", "tts engine")
        }
        return out.toString()
    }

    @JvmStatic fun stop(): String {
        val n = queue.stopAll()
        main.post { try { tts?.stop() } catch (_: Exception) {}; abandonFocus() }
        return JSONObject().put("ok", true).put("stopped", n).toString()
    }

    /** Installed voices; null when the engine isn't ready yet (the caller retries). */
    @JvmStatic fun voices(ctx: Context): String {
        app = ctx.applicationContext
        val engine = tts
        if (init != "ready" || engine == null) {
            main.post { ensureInit() }
            return JSONObject().put("ok", false).put("engine", init).put("error", initError ?: "tts engine starting").toString()
        }
        return try {
            val list = JSONArray()
            (engine.voices ?: emptySet<Voice>()).sortedBy { it.name }.forEach { v ->
                list.put(JSONObject().put("name", v.name).put("locale", v.locale.toLanguageTag())
                    .put("quality", v.quality).put("network", v.isNetworkConnectionRequired)
                    .put("installed", TextToSpeech.Engine.KEY_FEATURE_NOT_INSTALLED !in (v.features ?: emptySet())))
            }
            JSONObject().put("ok", true).put("engine", engine.defaultEngine)
                .put("default", engine.defaultVoice?.name ?: JSONObject.NULL)
                .put("count", list.length()).put("voices", list).toString()
        } catch (e: Exception) { JSONObject().put("ok", false).put("error", e.toString()).toString() }
    }

    // ---- chat mirror -----------------------------------------------------

    /** Show a spoken line in the app's chat as an assistant message (any thread). */
    @JvmStatic fun chat(text: String) { main.post { postChat(text) } }

    // Goes through VoiceBus's conversation log, so lines said while the screen is away
    // are replayed once when it comes back (same path as heard/spoken voice turns).
    private fun postChat(text: String) { VoiceBus.emit { it.onSpoken(text) } }

    // ---- engine (main thread) --------------------------------------------

    private fun replyPlaying() = VoiceBus.state == "speaking"

    private fun ensureInit() {
        if (init == "ready" || init == "pending") return
        val ctx = app ?: return
        init = "pending"; initError = null
        val started = System.nanoTime()
        var engine: TextToSpeech? = null
        engine = TextToSpeech(ctx) { status -> main.post { onInit(engine, status, started) } }
        tts = engine
        main.postDelayed({
            if (tts === engine && init == "pending") failInit(engine, "tts engine did not start within ${INIT_TIMEOUT_MS / 1000}s")
        }, INIT_TIMEOUT_MS)
    }

    private fun onInit(engine: TextToSpeech?, status: Int, started: Long) {
        if (engine == null || tts !== engine || init != "pending") return
        if (status != TextToSpeech.SUCCESS) { failInit(engine, "tts engine init failed (status $status)"); return }
        engine.setAudioAttributes(attrs)
        engine.setOnUtteranceProgressListener(progress)
        init = "ready"
        Log.i(TAG, "tts ready engine=${engine.defaultEngine} in ${(System.nanoTime() - started) / 1_000_000}ms")
        pump()
    }

    private fun failInit(engine: TextToSpeech, error: String) {
        Log.w(TAG, error)
        init = "failed"; initError = error
        if (tts === engine) tts = null
        try { engine.shutdown() } catch (_: Exception) {}
        queue.failAll(error)
        abandonFocus()
    }

    private fun pump() {
        pumpScheduled = false
        if (queue.idle()) return
        if (init != "ready") { if (queue.hasPending()) ensureInit(); return }
        val engine = tts ?: return
        while (true) {
            val playing = replyPlaying()
            val job = queue.next(playing) ?: break
            // Stop the assistant's reply first (the voice client flushes its audio on interrupt).
            if (job.interrupt && playing) try { app?.let { VoiceService.interrupt(it) } } catch (e: Exception) {
                Log.w(TAG, "voice interrupt failed: $e")
            }
            applyParams(engine, job)
            requestFocus()
            val params = Bundle().apply { putFloat(TextToSpeech.Engine.KEY_PARAM_VOLUME, 1f) }
            val mode = if (job.interrupt) TextToSpeech.QUEUE_FLUSH else TextToSpeech.QUEUE_ADD
            if (engine.speak(job.text, mode, params, job.id) != TextToSpeech.SUCCESS) {
                // The engine service can die under us. Everything already handed to it is
                // lost too: fail all in-flight jobs (not just this one) so waiters return and
                // the ducking focus is released; still-pending jobs rebuild the engine.
                queue.engineLost("tts engine rejected the utterance")
                abandonFocus()
                init = "none"; tts = null
                try { engine.shutdown() } catch (_: Exception) {}
                break
            }
        }
        // Held back behind a voice reply: look again shortly.
        if (queue.hasPending() && !pumpScheduled) { pumpScheduled = true; main.postDelayed({ pump() }, DEFER_POLL_MS) }
    }

    private fun applyParams(engine: TextToSpeech, job: SpeakQueue.Job) {
        engine.setSpeechRate(job.rate)
        engine.setPitch(job.pitch)
        val want = job.voice
        try {
            if (want.isEmpty()) { engine.defaultVoice?.let { engine.voice = it }; return }
            val byName = engine.voices?.firstOrNull { it.name.equals(want, ignoreCase = true) }
            if (byName != null) { engine.voice = byName; return }
            val r = engine.setLanguage(Locale.forLanguageTag(want.replace('_', '-')))
            if (r < TextToSpeech.LANG_AVAILABLE) {
                job.note = "voice '$want' not found; used the default voice"
                engine.defaultVoice?.let { engine.voice = it }
            }
        } catch (e: Exception) { job.note = "voice selection failed: $e" }
    }

    private val progress = object : UtteranceProgressListener() {
        override fun onStart(id: String) { main.post { queue.started(id) } }
        override fun onDone(id: String) = end(id, SpeakQueue.DONE, null)
        @Deprecated("Deprecated in Java") override fun onError(id: String) = end(id, SpeakQueue.ERROR, "tts error")
        override fun onError(id: String, code: Int) = end(id, SpeakQueue.ERROR, "tts error $code")
        override fun onStop(id: String, interrupted: Boolean) = end(id, SpeakQueue.STOPPED, null)
        private fun end(id: String, state: String, error: String?) {
            main.post { if (queue.finished(id, state, error)) abandonFocus() }
        }
    }

    // ---- audio focus -------------------------------------------------------

    private fun audio(): AudioManager? = app?.getSystemService(Context.AUDIO_SERVICE) as? AudioManager

    private fun requestFocus() {
        if (focusHeld) return
        val am = audio() ?: return
        focusHeld = try {
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                val req = AudioFocusRequest.Builder(AudioManager.AUDIOFOCUS_GAIN_TRANSIENT_MAY_DUCK)
                    .setAudioAttributes(attrs).setOnAudioFocusChangeListener { }.build()
                focusRequest = req
                am.requestAudioFocus(req) == AudioManager.AUDIOFOCUS_REQUEST_GRANTED
            } else @Suppress("DEPRECATION") {
                am.requestAudioFocus(null, AudioManager.STREAM_MUSIC, AudioManager.AUDIOFOCUS_GAIN_TRANSIENT_MAY_DUCK) ==
                    AudioManager.AUDIOFOCUS_REQUEST_GRANTED
            }
        } catch (e: Exception) { Log.w(TAG, "audio focus: $e"); false }
        // Focus denied (e.g. during a phone call) is not fatal: speak anyway, unducked.
    }

    private fun abandonFocus() {
        if (!focusHeld) return
        focusHeld = false
        val am = audio() ?: return
        try {
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) focusRequest?.let { am.abandonAudioFocusRequest(it) }
            else @Suppress("DEPRECATION") am.abandonAudioFocus(null)
        } catch (_: Exception) {}
        focusRequest = null
    }

    private fun mediaVolume(ctx: Context): Pair<Int, Int>? = try {
        val am = ctx.getSystemService(Context.AUDIO_SERVICE) as AudioManager
        am.getStreamVolume(AudioManager.STREAM_MUSIC) to am.getStreamMaxVolume(AudioManager.STREAM_MUSIC)
    } catch (_: Exception) { null }
}
