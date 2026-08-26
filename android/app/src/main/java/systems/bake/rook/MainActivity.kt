package systems.bake.rook

import android.content.Intent
import android.media.projection.MediaProjectionManager
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.provider.Settings
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AppCompatActivity
import systems.bake.rook.databinding.ActivityMainBinding

/**
 * Thin control panel: edit band settings, start/stop the worker, and grant the
 * three things only a foreground app can grant — screen capture (MediaProjection),
 * HID (AccessibilityService), and battery-optimization exemption.
 */
class MainActivity : AppCompatActivity() {

    private lateinit var b: ActivityMainBinding

    private val projectionLauncher =
        registerForActivityResult(ActivityResultContracts.StartActivityForResult()) { res ->
            if (res.resultCode == RESULT_OK && res.data != null) {
                // Hand the consent token to the service (which owns the
                // mediaProjection FGS type) to open ONE persistent capture
                // session. Android 14 makes the token single-use, so we must
                // build the projection now, promptly, inside that service.
                WorkerService.startCapture(this, res.resultCode, res.data!!)
                status("screen capture: granted")
            } else {
                status("screen capture: denied")
            }
        }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        b = ActivityMainBinding.inflate(layoutInflater)
        setContentView(b.root)
        ScreenCaptureBridge.init(this)

        val prefs = getSharedPreferences("rook", MODE_PRIVATE)
        b.hub.setText(prefs.getString("hub", BuildConfig.DEFAULT_HUB))
        b.psk.setText(prefs.getString("psk", BuildConfig.DEFAULT_PSK))
        b.name.setText(prefs.getString("name", (Build.MODEL ?: "android").replace(' ', '-')))

        b.btnStart.setOnClickListener {
            val hub = b.hub.text.toString().trim()
            val psk = b.psk.text.toString().trim()
            val name = b.name.text.toString().trim().ifEmpty { (Build.MODEL ?: "android").replace(' ', '-') }
            prefs.edit()
                .putString("hub", hub).putString("psk", psk).putString("name", name)
                .putBoolean("autostart", true)
                .apply()
            WorkerService.start(this, hub, psk, name)
            status("worker starting as $name -> $hub")
        }

        b.btnStop.setOnClickListener {
            prefs.edit().putBoolean("autostart", false).apply()
            WorkerService.stop(this)
            status("worker stopped")
        }

        b.btnGrantScreen.setOnClickListener {
            val mgr = getSystemService(MEDIA_PROJECTION_SERVICE) as MediaProjectionManager
            projectionLauncher.launch(mgr.createScreenCaptureIntent())
        }

        b.btnGrantA11y.setOnClickListener {
            startActivity(Intent(Settings.ACTION_ACCESSIBILITY_SETTINGS))
            status("enable 'Rook Worker' under Accessibility")
        }

        b.btnGrantBattery.setOnClickListener {
            @Suppress("BatteryLife")
            startActivity(Intent(
                Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS,
                Uri.parse("package:$packageName")
            ))
        }

        b.btnGrantNotif.setOnClickListener {
            startActivity(Intent(Settings.ACTION_NOTIFICATION_LISTENER_SETTINGS))
            status("enable 'Rook Worker' under Notification access")
        }

        b.btnGrantPerms.setOnClickListener {
            permsLauncher.launch(arrayOf(
                android.Manifest.permission.READ_SMS,
                android.Manifest.permission.SEND_SMS,
                android.Manifest.permission.READ_CONTACTS,
                android.Manifest.permission.READ_CALL_LOG,
                android.Manifest.permission.ACCESS_FINE_LOCATION,
            ))
        }

        b.btnGrantOverlay.setOnClickListener {
            startActivity(Intent(
                Settings.ACTION_MANAGE_OVERLAY_PERMISSION,
                Uri.parse("package:$packageName")
            ))
            status("enable 'Display over other apps' → lets device.wake turn the screen on")
        }

        // ---- voice ----
        b.voiceUrl.setText(prefs.getString("voice_url", "wss://192.168.1.64:8900/ws"))
        b.voiceInsecure.isChecked = prefs.getBoolean("voice_insecure", true)
        b.btnVoiceStart.setOnClickListener {
            val url = b.voiceUrl.text.toString().trim()
            val insecure = b.voiceInsecure.isChecked
            prefs.edit().putString("voice_url", url).putBoolean("voice_insecure", insecure).apply()
            if (checkSelfPermission(android.Manifest.permission.RECORD_AUDIO) !=
                android.content.pm.PackageManager.PERMISSION_GRANTED) {
                micPermLauncher.launch(android.Manifest.permission.RECORD_AUDIO)
            } else {
                b.voiceLog.text = ""
                VoiceService.start(this, url, insecure)
            }
        }
        b.btnVoiceInterrupt.setOnClickListener { VoiceService.interrupt(this) }
        b.btnVoiceEnd.setOnClickListener { VoiceService.endSession(this) }
        b.wakeEnabled.isChecked = prefs.getBoolean("wake_enabled", true)
        b.wakeEnabled.setOnCheckedChangeListener { _, on -> prefs.edit().putBoolean("wake_enabled", on).apply() }
        b.btnVoiceStandby.setOnClickListener {
            val url = b.voiceUrl.text.toString().trim()
            val insecure = b.voiceInsecure.isChecked
            prefs.edit().putString("voice_url", url).putBoolean("voice_insecure", insecure).apply()
            if (checkSelfPermission(android.Manifest.permission.RECORD_AUDIO) !=
                android.content.pm.PackageManager.PERMISSION_GRANTED) {
                micPermLauncher.launch(android.Manifest.permission.RECORD_AUDIO)
            } else VoiceService.standby(this, url, insecure)
        }
        b.btnVoiceOff.setOnClickListener { VoiceService.stop(this) }

        maybeRequestNotifications()
    }

    private val micPermLauncher =
        registerForActivityResult(ActivityResultContracts.RequestPermission()) { ok ->
            if (ok) b.btnVoiceStart.performClick() else status("microphone permission denied")
        }

    private var curBot: StringBuilder? = null

    private val voiceListener = object : VoiceBus.Listener {
        override fun onState(state: String) { b.voiceState.text = state }
        override fun onTranscript(text: String) { curBot = null; vlog("you: $text") }
        override fun onAssistantDelta(text: String) {
            val sb = curBot ?: StringBuilder().also { curBot = it; vlog("bot: ") }
            sb.append(text)
            // rewrite the last line in place
            val lines = b.voiceLog.text.toString().split('\n').toMutableList()
            if (lines.isNotEmpty()) lines[lines.size - 1] = "bot: $sb"
            b.voiceLog.text = lines.joinToString("\n")
        }
        override fun onAssistantDone() { curBot = null }
        override fun onInterrupt() { curBot = null; vlog("[interrupted]") }
        override fun onError(msg: String) { vlog("error: $msg") }
        override fun onWake() { vlog("[wake word]") }
    }

    private fun vlog(line: String) {
        val cur = b.voiceLog.text.toString()
        b.voiceLog.text = if (cur.isEmpty()) line else cur + "\n" + line
    }

    override fun onResume() {
        super.onResume()
        VoiceBus.listener = voiceListener
        b.voiceState.text = VoiceBus.state
    }

    override fun onPause() {
        VoiceBus.listener = null
        super.onPause()
    }

    private val permsLauncher =
        registerForActivityResult(ActivityResultContracts.RequestMultiplePermissions()) { res ->
            val granted = res.filterValues { it }.keys.map { it.substringAfterLast('.') }
            status("granted: " + (if (granted.isEmpty()) "none" else granted.joinToString(", ")))
        }

    private val notifPermLauncher =
        registerForActivityResult(ActivityResultContracts.RequestPermission()) { /* best-effort */ }

    private fun maybeRequestNotifications() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU &&
            checkSelfPermission(android.Manifest.permission.POST_NOTIFICATIONS) !=
                android.content.pm.PackageManager.PERMISSION_GRANTED) {
            notifPermLauncher.launch(android.Manifest.permission.POST_NOTIFICATIONS)
        }
    }

    private fun status(msg: String) {
        b.status.text = buildString { append(b.status.text); append('\n'); append(msg) }
    }
}
