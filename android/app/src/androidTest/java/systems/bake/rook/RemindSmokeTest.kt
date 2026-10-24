package systems.bake.rook

import android.content.Context
import android.media.AudioManager
import android.os.Bundle
import android.os.SystemClock
import org.json.JSONObject

/**
 * attention.remind on an isolated emulator (`-e mode remind`), through the real Python cap.
 * Never on a person's phone: it mutes the speech streams, then may speak a line at volume 0.
 */
object RemindSmokeTest {
    fun run(target: Context): Bundle {
        val out = StringBuilder()
        val audio = target.getSystemService(Context.AUDIO_SERVICE) as AudioManager
        val streams = listOf(AudioManager.STREAM_MUSIC, AudioManager.STREAM_SYSTEM, AudioManager.STREAM_NOTIFICATION)
        val before = streams.associateWith { audio.getStreamVolume(it) }
        val wasOn = SpokenReminder.enabled(target)
        try {
            // In Do Not Disturb an app without policy access may not change volumes; nothing speaks then anyway.
            streams.forEach { try { audio.setStreamVolume(it, 0, 0) } catch (_: SecurityException) {} }
            val py = PythonHost.ensureStarted(target)
            val json = py.getModule("json")
            val plugin = py.getModule("rook_android.plugins.attention_android").callAttr("AndroidAttentionPlugin")
            fun remind(vararg args: Pair<String, Any>): JSONObject {
                val kw = args.map { (k, v) -> com.chaquo.python.Kwarg(k, v) }.toTypedArray()
                return JSONObject(json.callAttr("dumps", plugin.callAttr("_remind", *kw)).toString())
            }
            val since = JSONObject(AttentionStore.poll(target, 0)).getLong("last")
            fun event(id: String, timeoutMs: Long): JSONObject? {
                val end = SystemClock.elapsedRealtime() + timeoutMs
                while (SystemClock.elapsedRealtime() < end) {
                    val ev = JSONObject(AttentionStore.poll(target, since)).getJSONArray("events")
                    for (i in 0 until ev.length()) ev.getJSONObject(i).let { if (it.optString("type") == "reminded" && it.optString("id") == id) return it }
                    Thread.sleep(200)
                }
                return null
            }

            SpokenReminder.setEnabled(target, true)
            check(!remind("id" to "", "text" to "x").getBoolean("ok")) { "empty id must be refused" }
            check(!remind("id" to "a", "text" to "x", "speak_unchecked" to "perhaps").getBoolean("ok"))

            val a = remind("id" to "smoke-quiet", "text" to "Need your input: deploy hub update?", "sample_ms" to 1000,
                           "speak_unchecked" to "false")
            check(a.getBoolean("accepted")) { a.toString() }
            val busy = remind("id" to "smoke-busy", "text" to "second")
            check(!busy.getBoolean("accepted") && busy.has("busy")) { "a second reminder must be refused: $busy" }
            val e1 = event("smoke-quiet", 20_000) ?: error("no reminded event for smoke-quiet")
            out.append("quiet check, speak_unchecked=false: ").append(e1).append('\n')
            check(e1.optString("outcome") in setOf("spoken", "dnd", "busy", "unchecked", "media", "mic_in_use", "call", "voice_session", "speaking")) { e1.toString() }
            check(e1.optString("outcome") != "spoken" || e1.optString("via") in setOf("server", "device"))

            val b = remind("id" to "smoke-unchecked", "text" to "Hey boss, you have 6 decisions pending.", "sample_ms" to 1000,
                           "speak_unchecked" to true)
            check(b.getBoolean("accepted")) { b.toString() }
            val e2 = event("smoke-unchecked", 150_000) ?: error("no reminded event for smoke-unchecked")
            out.append("speak_unchecked=true: ").append(e2).append('\n')

            SpokenReminder.setEnabled(target, false)
            val off = remind("id" to "smoke-off", "text" to "x")
            check(!off.getBoolean("accepted") && off.optString("reason") == "off") { off.toString() }
            val e3 = event("smoke-off", 2_000) ?: error("no off event")
            check(e3.optString("outcome") == "off")
            check(e1.keys().asSequence().all { it in setOf("seq", "ts", "type", "id", "outcome", "via", "level", "checked") }) { "unexpected event fields: $e1" }
            out.insert(0, "PASS: attention.remind validation, busy, off, events\n")
            return Bundle().apply { putString("stream", out.toString()) }
        } finally {
            SpokenReminder.setEnabled(target, wasOn)
            before.forEach { (s, v) -> try { audio.setStreamVolume(s, v, 0) } catch (_: Exception) {} }
        }
    }
}
