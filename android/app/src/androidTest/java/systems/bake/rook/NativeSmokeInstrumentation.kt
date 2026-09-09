package systems.bake.rook

import android.app.Instrumentation
import android.content.Context
import android.media.AudioManager
import android.os.Bundle
import org.json.JSONObject

/** Run on an isolated emulator, with Location permissions granted and a geo fix injected. */
class NativeSmokeInstrumentation : Instrumentation() {
    override fun onCreate(arguments: Bundle?) { super.onCreate(arguments); start() }
    override fun onStart() {
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
            val python = PythonHost.ensureStarted(targetContext)
            val device = python.getModule("rook_android.plugins.device_android").callAttr("AndroidDevicePlugin")
            val nativeRing = device.callAttr("_find", 1)
            check(JSONObject(python.getModule("json").callAttr("dumps", nativeRing).toString()).getBoolean("ok")) { nativeRing.toString() }
            check(JSONObject(python.getModule("json").callAttr("dumps", device.callAttr("_find_stop")).toString()).getBoolean("ok"))
            val plugin = python.getModule("rook_android.plugins.location_android").callAttr("AndroidLocationPlugin")
            val fix = plugin.callAttr("_get", 15.0)
            val location = JSONObject(python.getModule("json").callAttr("dumps", fix).toString())
            check(location.getBoolean("ok")) { location.toString() }
            check(kotlin.math.abs(location.getDouble("lat") - 41.88) < .01) { "unexpected emulator position" }
            check(!location.getBoolean("stale"))
            check(location.getString("maps_url").startsWith("https://www.google.com/maps/"))
            report.putString("stream", "PASS: maximum alarm volume, timed/manual restore, location with Maps closed, Maps URL\n")
            finish(-1, report)
        } catch (error: Throwable) {
            FindDeviceBridge.stop()
            report.putString("stream", "FAIL: ${error.stackTraceToString()}\n")
            finish(0, report)
        }
    }
}
