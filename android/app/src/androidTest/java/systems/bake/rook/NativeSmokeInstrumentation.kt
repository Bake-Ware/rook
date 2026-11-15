package systems.bake.rook

import android.app.Instrumentation
import android.content.Context
import android.media.AudioManager
import android.os.Bundle
import org.json.JSONObject

/** Run on an isolated emulator. */
class NativeSmokeInstrumentation : Instrumentation() {
    private var mode: String? = null
    override fun onCreate(arguments: Bundle?) { super.onCreate(arguments); mode = arguments?.getString("mode"); start() }
    override fun onStart() {
        if (mode == "voice") {
            try { finish(-1, VoiceSmokeTest.run(targetContext, context)) }
            catch (error: Throwable) { finish(0, Bundle().apply { putString("stream", "FAIL: ${error.stackTraceToString()}") }) }
            return
        }
        mode?.let { finish(-1, ApkUpdateSmokeTest.run(targetContext, it)); return }
        val report = Bundle()
        try {
            val audio = targetContext.getSystemService(Context.AUDIO_SERVICE) as AudioManager
            val initial = audio.getStreamVolume(AudioManager.STREAM_ALARM)
            val ring = JSONObject(FindDeviceBridge.ring(targetContext, 1))
            check(ring.getBoolean("ok")) { ring.toString() }
            check(audio.getStreamVolume(AudioManager.STREAM_ALARM) == audio.getStreamMaxVolume(AudioManager.STREAM_ALARM))
            Thread.sleep(1500)
            check(audio.getStreamVolume(AudioManager.STREAM_ALARM) == initial) { "timer must restore volume" }
            check(JSONObject(FindDeviceBridge.ring(targetContext, 10)).getBoolean("ok"))
            FindDeviceBridge.stop()
            check(audio.getStreamVolume(AudioManager.STREAM_ALARM) == initial) { "stop must restore volume" }
            report.putString("stream", "PASS: maximum alarm volume, timed/manual restore\n")
            finish(-1, report)
        } catch (error: Throwable) {
            FindDeviceBridge.stop()
            report.putString("stream", "FAIL: ${error.stackTraceToString()}\n")
            finish(0, report)
        }
    }
}
