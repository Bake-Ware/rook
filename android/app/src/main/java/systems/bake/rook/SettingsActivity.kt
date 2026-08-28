package systems.bake.rook

import android.content.Intent
import android.media.projection.MediaProjectionManager
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.provider.Settings
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AppCompatActivity
import systems.bake.rook.databinding.ActivitySettingsBinding

/**
 * Everything that isn't the conversation: band settings + worker start/stop,
 * the permission grants only a foreground app can request, and the voice
 * server / wake-word configuration.
 */
class SettingsActivity : AppCompatActivity() {

    private lateinit var b: ActivitySettingsBinding

    private val projectionLauncher =
        registerForActivityResult(ActivityResultContracts.StartActivityForResult()) { res ->
            if (res.resultCode == RESULT_OK && res.data != null) {
                WorkerService.startCapture(this, res.resultCode, res.data!!)
                status("screen capture: granted")
            } else status("screen capture: denied")
        }

    private val permsLauncher =
        registerForActivityResult(ActivityResultContracts.RequestMultiplePermissions()) { res ->
            val granted = res.filterValues { it }.keys.map { it.substringAfterLast('.') }
            status("granted: " + (if (granted.isEmpty()) "none" else granted.joinToString(", ")))
        }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        b = ActivitySettingsBinding.inflate(layoutInflater)
        setContentView(b.root)
        supportActionBar?.setDisplayHomeAsUpEnabled(true)
        ScreenCaptureBridge.init(this)

        val prefs = getSharedPreferences("rook", MODE_PRIVATE)
        b.hub.setText(prefs.getString("hub", BuildConfig.DEFAULT_HUB))
        b.psk.setText(prefs.getString("psk", BuildConfig.DEFAULT_PSK))
        b.name.setText(prefs.getString("name", (Build.MODEL ?: "android").replace(' ', '-')))

        b.btnStart.setOnClickListener {
            val hub = b.hub.text.toString().trim()
            val psk = b.psk.text.toString().trim()
            val name = b.name.text.toString().trim().ifEmpty { (Build.MODEL ?: "android").replace(' ', '-') }
            prefs.edit().putString("hub", hub).putString("psk", psk).putString("name", name)
                .putBoolean("autostart", true).apply()
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
            startActivity(Intent(Settings.ACTION_ACCESSIBILITY_SETTINGS)); status("enable 'Rook Worker' under Accessibility")
        }
        b.btnGrantBattery.setOnClickListener {
            @Suppress("BatteryLife")
            startActivity(Intent(Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS, Uri.parse("package:$packageName")))
        }
        b.btnGrantNotif.setOnClickListener {
            startActivity(Intent(Settings.ACTION_NOTIFICATION_LISTENER_SETTINGS)); status("enable 'Rook Worker' under Notification access")
        }
        b.btnGrantPerms.setOnClickListener {
            permsLauncher.launch(arrayOf(
                android.Manifest.permission.READ_SMS, android.Manifest.permission.SEND_SMS,
                android.Manifest.permission.READ_CONTACTS, android.Manifest.permission.READ_CALL_LOG,
                android.Manifest.permission.ACCESS_FINE_LOCATION, android.Manifest.permission.RECORD_AUDIO,
            ))
        }
        b.btnGrantOverlay.setOnClickListener {
            startActivity(Intent(Settings.ACTION_MANAGE_OVERLAY_PERMISSION, Uri.parse("package:$packageName")))
            status("enable 'Display over other apps' → lets device.wake turn the screen on")
        }

        // ---- voice ----
        b.voiceUrl.setText(prefs.getString("voice_url", BuildConfig.DEFAULT_VOICE_URL))
        b.voiceToken.setText(prefs.getString("voice_token", BuildConfig.DEFAULT_VOICE_TOKEN))
        b.voiceInsecure.isChecked = prefs.getBoolean("voice_insecure", false)
        b.wakeEnabled.isChecked = prefs.getBoolean("wake_enabled", true)
        b.btnSaveVoice.setOnClickListener {
            prefs.edit()
                .putString("voice_url", b.voiceUrl.text.toString().trim())
                .putString("voice_token", b.voiceToken.text.toString().trim())
                .putBoolean("voice_insecure", b.voiceInsecure.isChecked)
                .putBoolean("wake_enabled", b.wakeEnabled.isChecked)
                .apply()
            status("voice settings saved (takes effect on next Voice on)")
        }
    }

    override fun onSupportNavigateUp(): Boolean { finish(); return true }

    private fun status(msg: String) {
        b.status.text = buildString { append(b.status.text); append('\n'); append(msg) }
    }
}
