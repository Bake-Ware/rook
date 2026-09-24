package systems.bake.rook

import android.content.Intent
import android.media.projection.MediaProjectionManager
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.provider.Settings
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AppCompatActivity
import androidx.appcompat.app.AlertDialog
import androidx.lifecycle.lifecycleScope
import kotlinx.coroutines.launch
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import kotlinx.coroutines.Job
import android.widget.ArrayAdapter
import kotlinx.coroutines.CancellationException
import org.json.JSONArray
import systems.bake.rook.databinding.ActivitySettingsBinding

/**
 * Everything that isn't the conversation: band settings + worker start/stop,
 * the permission grants only a foreground app can request, and the voice
 * server / wake-word configuration.
 */
class SettingsActivity : AppCompatActivity() {

    private var voiceFetch: Job? = null
    private var voiceChoices = emptyList<String>()
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
        b.versionInfo.text = "APK ${BuildConfig.VERSION_NAME} · ${BuildConfig.VERSION_CODE}"
        b.autoUpdates.isChecked = getSharedPreferences("rook", MODE_PRIVATE).getBoolean("apk_auto_update", true)
        b.autoUpdates.setOnCheckedChangeListener { _, enabled ->
            getSharedPreferences("rook", MODE_PRIVATE).edit().putBoolean("apk_auto_update", enabled).apply()
        }
        b.btnAllowUpdates.setOnClickListener { startActivity(Intent(this, ApkUpdatePermissionActivity::class.java)) }
        b.btnUpdate.setOnClickListener {
            startActivity(Intent(Intent.ACTION_VIEW, Uri.parse(BuildConfig.ROOK_SERVER.trimEnd('/') + "/apk")))
        }
        b.btnBack.setOnClickListener { finish() }
        b.btnStopFind.setOnClickListener { FindDeviceBridge.stop(); status("find-device ring stopped") }
        b.btnLocation.setOnClickListener {
            if (checkSelfPermission(android.Manifest.permission.ACCESS_COARSE_LOCATION) != android.content.pm.PackageManager.PERMISSION_GRANTED &&
                checkSelfPermission(android.Manifest.permission.ACCESS_FINE_LOCATION) != android.content.pm.PackageManager.PERMISSION_GRANTED) {
                permsLauncher.launch(arrayOf(android.Manifest.permission.ACCESS_FINE_LOCATION, android.Manifest.permission.ACCESS_COARSE_LOCATION))
                status("Grant location first, then tap Background location again.")
            } else if (Build.VERSION.SDK_INT >= 30) {
                startActivity(Intent(Settings.ACTION_APPLICATION_DETAILS_SETTINGS, Uri.parse("package:$packageName")))
                status("Permissions → Location → Allow all the time. Then stop and start the worker.")
            } else if (Build.VERSION.SDK_INT >= 29) {
                permsLauncher.launch(arrayOf(android.Manifest.permission.ACCESS_BACKGROUND_LOCATION))
            } else status("Location granted. No separate background permission is needed on this Android version.")
        }
        ScreenCaptureBridge.init(this)

        val prefs = getSharedPreferences("rook", MODE_PRIVATE)
        b.accountServer.setText(prefs.getString("account_server", BuildConfig.ROOK_SERVER))
        b.btnGoogle.setOnClickListener { fetchBands(google = true) }
        if (BuildConfig.GOOGLE_WEB_CLIENT_ID.isEmpty()) b.btnGoogle.visibility = android.view.View.GONE   // not configured in this build
        b.btnPair.setOnClickListener { fetchBands(google = false) }
        b.btnSavedBands.setOnClickListener {
            chooseBand(JSONArray(prefs.getString("band_configurations", "[]")))
        }
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
                android.Manifest.permission.ACCESS_FINE_LOCATION, android.Manifest.permission.ACCESS_COARSE_LOCATION, android.Manifest.permission.RECORD_AUDIO,
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
        showVoices(VoiceCatalog(emptyList(), VoiceCatalog.FALLBACK))
        b.voicePicker.setOnClickListener { b.voicePicker.showDropDown() }
        b.voicePicker.setOnItemClickListener { _, _, position, _ ->
            val voice = voiceChoices.getOrNull(position) ?: return@setOnItemClickListener
            prefs.edit().putString("voice_choice", voice).apply()
            VoiceService.inst?.voiceChanged(voice)
        }
        b.btnRetryVoices.setOnClickListener { fetchVoices() }
        fetchVoices()
        b.showThinking.isChecked = prefs.getBoolean("show_thinking", false)
        b.showThinking.setOnCheckedChangeListener { _, enabled ->
            prefs.edit().putBoolean("show_thinking", enabled).apply()
            VoiceService.inst?.thinkingChanged()
        }
        b.wakeEnabled.isChecked = prefs.getBoolean("wake_enabled", true)
        b.btnDefaultAssistant.setOnClickListener {
            try {
                if (Build.VERSION.SDK_INT >= 29) {
                    val roles = getSystemService(android.app.role.RoleManager::class.java)
                    if (roles.isRoleAvailable(android.app.role.RoleManager.ROLE_ASSISTANT)) {
                        startActivity(roles.createRequestRoleIntent(android.app.role.RoleManager.ROLE_ASSISTANT))
                    } else startActivity(Intent(Settings.ACTION_VOICE_INPUT_SETTINGS))
                } else startActivity(Intent(Settings.ACTION_VOICE_INPUT_SETTINGS))
            } catch (_: Exception) { startActivity(Intent(Settings.ACTION_SETTINGS)) }
        }
        b.btnSaveVoice.setOnClickListener {
            prefs.edit()
                .putString("voice_url", b.voiceUrl.text.toString().trim())
                .putString("voice_token", b.voiceToken.text.toString().trim())
                .putBoolean("voice_insecure", b.voiceInsecure.isChecked)
                .putBoolean("wake_enabled", b.wakeEnabled.isChecked)
                .apply()
            val service = VoiceService.inst
            if (service != null) service.wakeSettingChanged()
            else if (b.wakeEnabled.isChecked &&
                checkSelfPermission(android.Manifest.permission.RECORD_AUDIO) == android.content.pm.PackageManager.PERMISSION_GRANTED) {
                VoiceService.standby(this, b.voiceUrl.text.toString().trim(), b.voiceInsecure.isChecked)
            }
            fetchVoices()
            status("voice settings saved (takes effect on next Voice on)")
        }
    }

    private fun showVoices(catalog: VoiceCatalog) {
        val prefs = getSharedPreferences("rook", MODE_PRIVATE)
        val saved = prefs.getString("voice_choice", null)
        voiceChoices = catalog.choices(saved)
        b.voicePicker.setAdapter(ArrayAdapter(this, android.R.layout.simple_dropdown_item_1line, voiceChoices.map(VoiceCatalog::label)))
        b.voicePicker.setText(VoiceCatalog.label(saved ?: catalog.default), false)
    }

    private fun fetchVoices() {
        voiceFetch?.cancel()
        voiceFetch = lifecycleScope.launch {
            val prefs = getSharedPreferences("rook", MODE_PRIVATE)
            val url = prefs.getString("voice_url", BuildConfig.DEFAULT_VOICE_URL) ?: BuildConfig.DEFAULT_VOICE_URL
            val token = prefs.getString("voice_token", "") ?: ""
            val insecure = prefs.getBoolean("voice_insecure", false)
            b.voiceListStatus.text = "Loading voices…"
            b.btnRetryVoices.isEnabled = false
            try {
                val catalog = withContext(Dispatchers.IO) { VoiceCatalog.fetch(url, token, insecure) }
                if (!prefs.contains("voice_choice")) {
                    prefs.edit().putString("voice_choice", catalog.default).apply()
                    VoiceService.inst?.voiceChanged(catalog.default)
                }
                showVoices(catalog)
                b.voiceListStatus.text = "Voice changes apply immediately."
            } catch (cancelled: CancellationException) { throw cancelled
            } catch (_: Exception) {
                showVoices(VoiceCatalog(emptyList(), VoiceCatalog.FALLBACK))
                b.voiceListStatus.text = "Couldn’t load voices. Showing saved/default voice. Retry below."
            } finally { b.btnRetryVoices.isEnabled = true }
        }
    }

    override fun onSupportNavigateUp(): Boolean { finish(); return true }

    private fun fetchBands(google: Boolean) {
        b.btnGoogle.isEnabled = false
        b.btnPair.isEnabled = false
        lifecycleScope.launch {
            try {
                val server = b.accountServer.text.toString().trim()
                val bands = if (google) GoogleEnrollment.signIn(this@SettingsActivity, server)
                    else GoogleEnrollment.pair(server, b.pairingCode.text.toString())
                getSharedPreferences("rook", MODE_PRIVATE).edit()
                    .putString("account_server", server)
                    .putString("band_configurations", bands.toString()).apply()
                b.pairingCode.text.clear()
                chooseBand(bands)
            } catch (cancelled: CancellationException) {
                throw cancelled
            } catch (error: Exception) {
                status(error.message ?: "Enrollment failed. Try Google again or use a pairing code.")
            } finally {
                b.btnGoogle.isEnabled = true
                b.btnPair.isEnabled = true
            }
        }
    }

    private fun chooseBand(bands: JSONArray) {
        if (bands.length() == 0) {
            status("No bands yet. Create a band or accept an invitation in your Rook account.")
            return
        }
        val names = Array(bands.length()) { bands.getJSONObject(it).getString("name") }
        AlertDialog.Builder(this).setTitle("Choose your active band")
            .setItems(names) { _, index ->
                val band = bands.getJSONObject(index)
                b.hub.setText(band.getString("hub"))
                b.psk.setText(band.getString("psk"))
                getSharedPreferences("rook", MODE_PRIVATE).edit()
                    .putString("hub", band.getString("hub"))
                    .putString("psk", band.getString("psk"))
                    .putString("band_id", band.getString("id"))
                    .putInt("band_epoch", band.getInt("epoch")).apply()
                status("Selected ${names[index]}. Tap Start to connect.")
            }.setNegativeButton("Cancel", null).show()
    }

    override fun onResume() {
        super.onResume()
        val update = org.json.JSONObject(ApkUpdater.status(this))
        b.updateStatus.text = "${update.optString("state").replace('_', ' ')} · ${update.optString("message")}".trim(' ', '·')
    }

    private fun status(msg: String) {
        b.status.text = buildString { append(b.status.text); append('\n'); append(msg) }
    }
}
