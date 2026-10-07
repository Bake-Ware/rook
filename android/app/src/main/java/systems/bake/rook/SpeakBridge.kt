package systems.bake.rook

import android.content.Context
import android.content.Intent
import android.media.AudioAttributes
import android.media.AudioFocusRequest
import android.media.AudioManager
import android.media.MediaPlayer
import android.os.Build
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.speech.tts.TextToSpeech
import android.speech.tts.UtteranceProgressListener
import android.speech.tts.Voice
import android.util.Log
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONArray
import org.json.JSONObject
import java.io.File
import java.util.Locale
import java.util.concurrent.Executors
import java.util.concurrent.TimeUnit

/**
 * Speech for the `voice.speak` worker cap (rook_android/plugins/speak_android.py).
 *
 * When the app has a voice server configured, each line is synthesized there
 * (POST /api/voice) in the app's selected voice, so agents sound like the
 * assistant, and played here one at a time. If the server can't be reached or
 * refuses, that line falls back to the device voice and the job notes why.
 *
 * On-device speech: one [TextToSpeech] engine per process, created lazily on the main thread and kept
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
    private var speechHold = false
    private var pumpScheduled = false
    private var counter = 0L

    // Voice-server route: one utterance at a time (fetching or playing).
    private val fetcher = Executors.newSingleThreadExecutor()
    private var current: SpeakQueue.Job? = null
    private var player: MediaPlayer? = null
    private var playerFile: File? = null
    private val deviceWaiting = ArrayDeque<SpeakQueue.Job>()   // fell back while the engine was starting

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
        speechVolume(ctx)?.let { (stream, level) ->
            val (v, max) = level
            out.put("volume", "$v/$max").put("volume_stream", stream)
            when {
                v == 0 -> out.put("warning", "$stream volume is 0: speech will be inaudible")
                v * 5 <= max -> out.put("warning", "$stream volume is low ($v/$max): speech may be hard to hear")
            }
            Unit
        }
        return out.toString()
    }

    @JvmStatic fun status(id: String): String {
        val j = queue.get(id) ?: return JSONObject().put("ok", false).put("error", "unknown speech id $id").toString()
        val out = JSONObject().put("ok", true).put("id", j.id).put("state", j.state).put("done", j.done)
        j.error?.let { out.put("error", it) }
        j.note?.let { out.put("note", it) }
        j.via?.let { out.put("via", it) }
        if (j.state == SpeakQueue.QUEUED) {
            out.put("ahead", queue.ahead(id)).put("engine", init)
            if (replyPlaying()) out.put("waiting_for", "voice reply")
            else if (init != "ready") out.put("waiting_for", "tts engine")
        }
        return out.toString()
    }

    @JvmStatic fun stop(): String {
        val n = queue.stopAll()
        main.post {
            try { tts?.stop() } catch (_: Exception) {}
            stopPlayer(); current = null; deviceWaiting.clear()
            abandonFocus()
        }
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
        while (deviceWaiting.isNotEmpty()) deviceSpeak(deviceWaiting.removeFirst())
        pump()
    }

    private fun failInit(engine: TextToSpeech, error: String) {
        Log.w(TAG, error)
        init = "failed"; initError = error
        if (tts === engine) tts = null
        try { engine.shutdown() } catch (_: Exception) {}
        deviceWaiting.clear(); current = null
        queue.failAll(error)
        abandonFocus()
    }

    private fun pump() {
        pumpScheduled = false
        if (queue.idle()) return
        val srv = server()
        if (srv != null) { pumpServer(srv); return }
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
            main.post {
                if (queue.finished(id, state, error)) abandonFocus()
                if (current?.id == id) { current = null; pump() }
            }
        }
    }

    // ---- voice server (main thread unless noted) ------------------------------

    private class Server(val url: String, val token: String, val insecure: Boolean, val voice: String)

    /** The app's voice server, or null when none is configured. */
    private fun server(): Server? {
        val ctx = app ?: return null
        val p = ctx.getSharedPreferences("rook", Context.MODE_PRIVATE)
        val base = p.getString("voice_url", BuildConfig.DEFAULT_VOICE_URL) ?: ""
        if (base.isBlank()) return null
        val url = try { VoiceCatalog.speechEndpoint(base) } catch (_: Exception) { return null }
        return Server(url, p.getString("voice_token", "") ?: "", p.getBoolean("voice_insecure", false),
                      p.getString("voice_choice", "") ?: "")
    }

    private fun pumpServer(srv: Server) {
        val playingReply = replyPlaying()
        if (current != null) {
            if (!queue.hasPendingInterrupt()) return          // one at a time; finishing pumps again
            stopPlayer(); try { tts?.stop() } catch (_: Exception) {}
            current = null                                    // queue.next marks it superseded
        }
        val job = queue.next(playingReply)
        if (job == null) {
            if (queue.hasPending() && !pumpScheduled) { pumpScheduled = true; main.postDelayed({ pump() }, DEFER_POLL_MS) }
            return
        }
        if (job.interrupt && playingReply) try { app?.let { VoiceService.interrupt(it) } } catch (e: Exception) {
            Log.w(TAG, "voice interrupt failed: $e")
        }
        current = job; job.via = "server"
        requestFocus()
        val cache = app?.cacheDir
        fetcher.execute {
            val result = try { fetch(srv, job, cache) } catch (e: Exception) { Result.failure<File>(e) }
            main.post { onFetched(job, result) }
        }
    }

    /** Worker thread: synthesize one line on the voice server into a temp WAV file. */
    private fun fetch(srv: Server, job: SpeakQueue.Job, cache: File?): Result<File> {
        val b = OkHttpClient.Builder().callTimeout(30, TimeUnit.SECONDS).followRedirects(false)
        if (srv.insecure) VoiceTls.trustAll(b)
        val http = b.build()
        try {
            val body = JSONObject().put("text", job.text)
            // A voice named by the caller wins; otherwise the voice picked in the app.
            val voice = job.voice.ifEmpty { srv.voice }
            if (voice.isNotEmpty()) body.put("voice", voice)
            val req = Request.Builder().url(srv.url).header("User-Agent", "rook-worker")
                .post(body.toString().toRequestBody("application/json".toMediaType()))
                .apply { if (srv.token.isNotEmpty()) header("Authorization", "Bearer ${srv.token}") }.build()
            http.newCall(req).execute().use { r ->
                if (!r.isSuccessful) return Result.failure(RuntimeException("voice server ${r.code}"))
                val file = File(cache ?: return Result.failure(RuntimeException("no cache dir")), "speak-${job.id}.wav")
                file.outputStream().use { out -> r.body?.byteStream()?.copyTo(out) }
                return Result.success(file)
            }
        } finally { http.dispatcher.executorService.shutdown(); http.connectionPool.evictAll() }
    }

    private fun onFetched(job: SpeakQueue.Job, result: Result<File>) {
        val file = result.getOrNull()
        if (current !== job || job.done) { file?.delete(); return }   // stopped or superseded meanwhile
        if (file == null) { fallBack(job, result.exceptionOrNull()?.message ?: "failed"); return }
        try {
            val mp = MediaPlayer()
            player = mp; playerFile = file
            mp.setAudioAttributes(attrs)
            mp.setDataSource(file.path)
            mp.setOnCompletionListener { finishServer(job, SpeakQueue.DONE, null) }
            mp.setOnErrorListener { _, what, extra -> finishServer(job, SpeakQueue.ERROR, "playback error $what/$extra"); true }
            mp.prepare(); mp.start()
            queue.started(job.id)
        } catch (e: Exception) { stopPlayer(); fallBack(job, "playback: $e") }
    }

    private fun finishServer(job: SpeakQueue.Job, state: String, error: String?) {
        stopPlayer()
        if (current === job) current = null
        if (queue.finished(job.id, state, error)) abandonFocus()
        pump()
    }

    /** The server route failed for this line: say it with the device voice instead. */
    private fun fallBack(job: SpeakQueue.Job, reason: String) {
        Log.w(TAG, "voice server: $reason; using the device voice")
        job.via = "device"; job.note = "voice server unavailable ($reason); used the device voice"
        // Device TTS reads job.voice as a device voice name or locale; a server voice id
        // (e.g. "sojourn") is not one, so leave it out and use the device default.
        deviceSpeak(job)
    }

    private fun deviceSpeak(job: SpeakQueue.Job) {
        val engine = tts
        if (init != "ready" || engine == null) { deviceWaiting.addLast(job); ensureInit(); return }
        engine.setSpeechRate(job.rate); engine.setPitch(job.pitch)
        engine.defaultVoice?.let { try { engine.voice = it } catch (_: Exception) {} }
        requestFocus()
        val params = Bundle().apply { putFloat(TextToSpeech.Engine.KEY_PARAM_VOLUME, 1f) }
        if (engine.speak(job.text, TextToSpeech.QUEUE_ADD, params, job.id) != TextToSpeech.SUCCESS) {
            if (current === job) current = null
            if (queue.finished(job.id, SpeakQueue.ERROR, "voice server and device voice both failed")) abandonFocus()
            pump()
        }
    }

    private fun stopPlayer() {
        player?.let { try { it.stop() } catch (_: Exception) {}; try { it.release() } catch (_: Exception) {} }
        player = null
        playerFile?.delete(); playerFile = null
    }

    // ---- audio focus -------------------------------------------------------

    private fun audio(): AudioManager? = app?.getSystemService(Context.AUDIO_SERVICE) as? AudioManager

    private fun requestFocus() {
        // Step wake standby out of call audio mode so the line is actually audible.
        if (!speechHold) speechHold = try { VoiceService.inst?.holdForSpeech() == true } catch (e: Exception) { Log.w(TAG, "speech hold: $e"); false }
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
        if (speechHold) {
            speechHold = false
            try { VoiceService.inst?.releaseSpeechHold() } catch (e: Exception) { Log.w(TAG, "speech hold release: $e") }
        }
        if (!focusHeld) return
        focusHeld = false
        val am = audio() ?: return
        try {
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) focusRequest?.let { am.abandonAudioFocusRequest(it) }
            else @Suppress("DEPRECATION") am.abandonAudioFocus(null)
        } catch (_: Exception) {}
        focusRequest = null
    }

    /**
     * The volume speech actually plays at: the stream that controls [attrs]
     * (USAGE_ASSISTANT). On some phones that is not the media volume; Samsung's
     * separate assistant slider once left speech near-silent while media read 9/15.
     */
    private fun speechVolume(ctx: Context): Pair<String, Pair<Int, Int>>? = try {
        val am = ctx.getSystemService(Context.AUDIO_SERVICE) as AudioManager
        val stream = attrs.volumeControlStream.takeIf { it != AudioManager.USE_DEFAULT_STREAM_TYPE }
            ?: AudioManager.STREAM_MUSIC
        streamName(stream) to (am.getStreamVolume(stream) to am.getStreamMaxVolume(stream))
    } catch (_: Exception) { null }

    private fun streamName(stream: Int) = when (stream) {
        AudioManager.STREAM_MUSIC -> "media"
        AudioManager.STREAM_ALARM -> "alarm"
        AudioManager.STREAM_NOTIFICATION -> "notification"
        AudioManager.STREAM_RING -> "ring"
        AudioManager.STREAM_SYSTEM -> "system"
        AudioManager.STREAM_VOICE_CALL -> "call"
        AudioManager.STREAM_ACCESSIBILITY -> "accessibility"
        11 -> "assistant"   // AudioSystem.STREAM_ASSISTANT (hidden); Samsung's assistant slider
        else -> "stream $stream"
    }
}
