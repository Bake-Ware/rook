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
                ScreenCaptureBridge.setProjection(this, res.resultCode, res.data!!)
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
    }

    private fun status(msg: String) {
        b.status.text = buildString { append(b.status.text); append('\n'); append(msg) }
    }
}
