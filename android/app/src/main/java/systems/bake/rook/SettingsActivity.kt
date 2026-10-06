package systems.bake.rook

import android.Manifest
import android.content.ComponentName
import android.content.Intent
import android.content.pm.PackageManager
import android.graphics.Color
import android.media.projection.MediaProjectionManager
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.os.PowerManager
import android.provider.Settings
import android.view.Gravity
import android.view.View
import android.widget.ArrayAdapter
import android.widget.LinearLayout
import android.widget.TextView
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AlertDialog
import androidx.appcompat.app.AppCompatActivity
import androidx.core.app.NotificationManagerCompat
import androidx.lifecycle.lifecycleScope
import com.google.android.material.tabs.TabLayout
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import org.json.JSONArray
import systems.bake.rook.databinding.ActivitySettingsBinding

/**
 * Everything that isn't the conversation, in four tabs (see [SettingsTabs]):
 *  - Voice: conversation mode + prompt, voice picker, fast voice, show thinking,
 *    wake word, default assistant.
 *  - Band: account sign-in / pairing / saved bands, hub + PSK + name, worker start/stop.
 *  - Permissions: one row per grant with its live state; tap to grant.
 *  - App: version, updates, and the advanced voice connection (server, key, TLS).
 * The status footer is shared by every tab.
 */
class SettingsActivity : AppCompatActivity() {

    private var voiceFetch: Job? = null
    private var voiceChoices = emptyList<String>()
    private var modeShown = VoiceModes.byId(null)
    private lateinit var b: ActivitySettingsBinding
    private val prefs by lazy { getSharedPreferences("rook", MODE_PRIVATE) }
    private val log = StatusLog()
    private lateinit var pages: List<View>

    private val projectionLauncher =
        registerForActivityResult(ActivityResultContracts.StartActivityForResult()) { res ->
            if (res.resultCode == RESULT_OK && res.data != null) {
                WorkerService.startCapture(this, res.resultCode, res.data!!)
                status("screen capture: granted")
            } else status("screen capture: denied")
            // The session starts in the service; re-read once it has had a moment.
            b.permList.postDelayed({ renderPermissions() }, 800)
        }

    private val permsLauncher =
        registerForActivityResult(ActivityResultContracts.RequestMultiplePermissions()) { res ->
            val granted = res.filterValues { it }.keys.map { it.substringAfterLast('.') }
            status("granted: " + (if (granted.isEmpty()) "none" else granted.joinToString(", ")))
            renderPermissions()
        }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        b = ActivitySettingsBinding.inflate(layoutInflater)
        setContentView(b.root)
        supportActionBar?.setDisplayHomeAsUpEnabled(true)
        val version = "APK ${BuildConfig.VERSION_NAME} · ${BuildConfig.VERSION_CODE}"
        b.versionInfo.text = version
        b.appVersion.text = version
        b.btnBack.setOnClickListener { finish() }
        ScreenCaptureBridge.init(this)
        setupTabs()
        setupVoice()
        setupBand()
        setupApp()
        b.btnGrantPerms.setOnClickListener { request(*BASIC_PERMISSIONS) }
    }

    // ---- tabs -------------------------------------------------------------

    private fun setupTabs() {
        pages = listOf(b.pageVoice, b.pageBand, b.pagePermissions, b.pageApp)
        val tabs = b.settingsTabs
        tabs.setTabTextColors(getColor(R.color.rook_dim), getColor(R.color.rook_accent))
        tabs.setSelectedTabIndicatorColor(getColor(R.color.rook_accent))
        SettingsTabs.ALL.forEach { tabs.addTab(tabs.newTab().setText(it.label)) }
        fun sentenceCase(view: View) {
            if (view is TextView) view.isAllCaps = false
            if (view is android.view.ViewGroup) for (i in 0 until view.childCount) sentenceCase(view.getChildAt(i))
        }
        sentenceCase(tabs)
        val start = SettingsTabs.indexOf(prefs.getString(SettingsTabs.PREF, null))
        showPage(start)
        tabs.getTabAt(start)?.select()
        tabs.addOnTabSelectedListener(object : TabLayout.OnTabSelectedListener {
            override fun onTabSelected(tab: TabLayout.Tab) {
                showPage(tab.position)
                b.settingsScroll.scrollTo(0, 0)
                prefs.edit().putString(SettingsTabs.PREF, SettingsTabs.idAt(tab.position)).apply()
            }
            override fun onTabUnselected(tab: TabLayout.Tab) {}
            override fun onTabReselected(tab: TabLayout.Tab) { b.settingsScroll.smoothScrollTo(0, 0) }
        })
    }

    private fun showPage(index: Int) {
        pages.forEachIndexed { i, page -> page.visibility = if (i == index) View.VISIBLE else View.GONE }
        if (pages[index] === b.pagePermissions) renderPermissions()
    }

    // ---- voice ------------------------------------------------------------

    private fun setupVoice() {
        showVoices(VoiceCatalog(emptyList(), VoiceCatalog.FALLBACK))
        b.voicePicker.setOnClickListener { b.voicePicker.showDropDown() }
        b.voicePicker.setOnItemClickListener { _, _, position, _ ->
            val voice = voiceChoices.getOrNull(position) ?: return@setOnItemClickListener
            prefs.edit().putString("voice_choice", voice).apply()
            VoiceService.inst?.voiceChanged(voice)
            status("voice: ${VoiceCatalog.label(voice)}")
        }
        b.btnRetryVoices.setOnClickListener { fetchVoices() }
        fetchVoices()
        b.voiceMode.setAdapter(ArrayAdapter(this, android.R.layout.simple_dropdown_item_1line, VoiceModes.ALL.map { it.label }))
        showMode(VoiceModes.byId(prefs.getString(VoiceModes.PREF_MODE, VoiceModes.DEFAULT)))
        b.voiceMode.setOnClickListener { b.voiceMode.showDropDown() }
        b.voiceMode.setOnItemClickListener { _, _, position, _ ->
            val mode = VoiceModes.ALL.getOrNull(position) ?: return@setOnItemClickListener
            saveModePrompt()
            prefs.edit().putString(VoiceModes.PREF_MODE, mode.id).apply()
            showMode(mode)
            status("mode: ${mode.label} (takes effect on next Voice on)")
        }
        b.btnModeDefault.setOnClickListener { b.voiceModePrompt.setText(modeShown.defaultPrompt) }
        b.fastVoice.isChecked = prefs.getBoolean(PREF_FAST_VOICE, false)
        b.fastVoice.setOnCheckedChangeListener { _, enabled ->
            prefs.edit().putBoolean(PREF_FAST_VOICE, enabled).apply()
            status("fast voice ${if (enabled) "on" else "off"} (takes effect on next Voice on)")
        }
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
        b.btnSaveVoice.setOnClickListener { saveVoiceSettings() }
    }

    /** Voice tab's Save and App tab's Save write the same set, exactly as the single Save button did. */
    private fun saveVoiceSettings() {
        prefs.edit()
            .putString("voice_url", b.voiceUrl.text.toString().trim())
            .putString("voice_token", b.voiceToken.text.toString().trim())
            .putBoolean("voice_insecure", b.voiceInsecure.isChecked)
            .putBoolean("wake_enabled", b.wakeEnabled.isChecked)
            .apply()
        saveModePrompt()
        val service = VoiceService.inst
        if (service != null) service.wakeSettingChanged()
        else if (b.wakeEnabled.isChecked && granted(Manifest.permission.RECORD_AUDIO)) {
            VoiceService.standby(this, b.voiceUrl.text.toString().trim(), b.voiceInsecure.isChecked)
        }
        fetchVoices()
        status("voice settings saved (takes effect on next Voice on)")
    }

    private fun showMode(mode: VoiceModes.Mode) {
        modeShown = mode
        b.voiceMode.setText(mode.label, false)
        val stored = prefs.getString(VoiceModes.promptKey(mode.id), "")
        b.voiceModePrompt.setText(VoiceModes.shownPrompt(mode, stored))
        val dictate = mode.id == "dictate"
        b.voiceModePrompt.isEnabled = !dictate
        b.btnModeDefault.isEnabled = !dictate
        b.voiceModePrompt.hint = if (mode.id == VoiceModes.DEFAULT) "Extra instructions (optional)" else getString(R.string.voice_mode_prompt)
        b.voiceModeHelp.text = when (mode.id) {
            "assistant" -> "The full agent: answers, looks things up and runs tasks."
            "dictate" -> "Transcribes only, no replies. Say “read it back”, “scratch that”, “start over” or “I’m done”."
            else -> "Talk only: no tools or device access. Each mode keeps its own conversation."
        }
    }

    private fun saveModePrompt() {
        val mode = modeShown
        if (mode.id == "dictate") return
        prefs.edit().putString(VoiceModes.promptKey(mode.id), VoiceModes.storedPrompt(mode, b.voiceModePrompt.text.toString())).apply()
    }

    private fun showVoices(catalog: VoiceCatalog) {
        val saved = prefs.getString("voice_choice", null)
        voiceChoices = catalog.choices(saved)
        b.voicePicker.setAdapter(ArrayAdapter(this, android.R.layout.simple_dropdown_item_1line, voiceChoices.map(VoiceCatalog::label)))
        b.voicePicker.setText(VoiceCatalog.label(saved ?: catalog.default), false)
    }

    private fun fetchVoices() {
        voiceFetch?.cancel()
        voiceFetch = lifecycleScope.launch {
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

    private fun renderAssistantState() {
        val held = if (Build.VERSION.SDK_INT >= 29) try {
            getSystemService(android.app.role.RoleManager::class.java).isRoleHeld(android.app.role.RoleManager.ROLE_ASSISTANT)
        } catch (_: Exception) { null } else null
        b.assistantState.text = when (held) {
            true -> "Rook is the default assistant."
            false -> "Rook is not the default assistant."
            null -> ""
        }
        b.assistantState.visibility = if (held == null) View.GONE else View.VISIBLE
    }

    // ---- band -------------------------------------------------------------

    private fun setupBand() {
        b.accountServer.setText(prefs.getString("account_server", BuildConfig.ROOK_SERVER))
        b.btnGoogle.setOnClickListener { fetchBands(google = true) }
        if (BuildConfig.GOOGLE_WEB_CLIENT_ID.isEmpty()) b.btnGoogle.visibility = View.GONE   // not configured in this build
        b.btnPair.setOnClickListener { fetchBands(google = false) }
        b.btnSavedBands.setOnClickListener { chooseBand(JSONArray(prefs.getString("band_configurations", "[]"))) }
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
        b.btnStopFind.setOnClickListener { FindDeviceBridge.stop(); status("find-device ring stopped") }
    }

    private fun fetchBands(google: Boolean) {
        b.btnGoogle.isEnabled = false
        b.btnPair.isEnabled = false
        lifecycleScope.launch {
            try {
                val server = b.accountServer.text.toString().trim()
                val bands = if (google) GoogleEnrollment.signIn(this@SettingsActivity, server)
                    else GoogleEnrollment.pair(server, b.pairingCode.text.toString())
                prefs.edit()
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
                prefs.edit()
                    .putString("hub", band.getString("hub"))
                    .putString("psk", band.getString("psk"))
                    .putString("band_id", band.getString("id"))
                    .putInt("band_epoch", band.getInt("epoch")).apply()
                status("Selected ${names[index]}. Tap Start to connect.")
            }.setNegativeButton("Cancel", null).show()
    }

    // ---- permissions ------------------------------------------------------

    /** One grant: [state] is true/false, or null when this Android version doesn't need it. */
    private class Grant(val title: String, val why: String, val state: () -> Boolean?, val action: () -> Unit)

    private fun granted(p: String) = checkSelfPermission(p) == PackageManager.PERMISSION_GRANTED
    private fun request(vararg p: String) = permsLauncher.launch(arrayOf(*p))
    private fun appDetails() = startActivity(Intent(Settings.ACTION_APPLICATION_DETAILS_SETTINGS, Uri.parse("package:$packageName")))

    private fun grants(): List<Grant> = listOf(
        Grant("Microphone", "Talking to Rook and the wake word", { granted(Manifest.permission.RECORD_AUDIO) }) {
            request(Manifest.permission.RECORD_AUDIO) },
        Grant("Notifications", "Worker status, timers and replies", { NotificationManagerCompat.from(this).areNotificationsEnabled() }) {
            if (Build.VERSION.SDK_INT >= 33 && !granted(Manifest.permission.POST_NOTIFICATIONS)) request(Manifest.permission.POST_NOTIFICATIONS)
            else if (Build.VERSION.SDK_INT >= 26) startActivity(Intent(Settings.ACTION_APP_NOTIFICATION_SETTINGS).putExtra(Settings.EXTRA_APP_PACKAGE, packageName))
            else appDetails()
        },
        Grant("SMS", "Read and send texts for the band", { granted(Manifest.permission.READ_SMS) && granted(Manifest.permission.SEND_SMS) }) {
            request(Manifest.permission.READ_SMS, Manifest.permission.SEND_SMS) },
        Grant("Contacts", "Look up who's who", { granted(Manifest.permission.READ_CONTACTS) }) {
            request(Manifest.permission.READ_CONTACTS) },
        Grant("Call log", "Recent calls", { granted(Manifest.permission.READ_CALL_LOG) }) {
            request(Manifest.permission.READ_CALL_LOG) },
        Grant("Calendar", "Read your events", { granted(Manifest.permission.READ_CALENDAR) }) {
            request(Manifest.permission.READ_CALENDAR) },
        Grant("Location", "Where the phone is, while the app is open", {
            granted(Manifest.permission.ACCESS_FINE_LOCATION) || granted(Manifest.permission.ACCESS_COARSE_LOCATION) }) {
            request(Manifest.permission.ACCESS_FINE_LOCATION, Manifest.permission.ACCESS_COARSE_LOCATION) },
        Grant("Background location", "Remote location needs “Allow all the time”; then stop and start the worker", {
            if (Build.VERSION.SDK_INT >= 29) granted(Manifest.permission.ACCESS_BACKGROUND_LOCATION) else null }) { backgroundLocation() },
        Grant("Screen capture", "Screenshots for the band; asked again after the worker restarts", { ScreenCaptureBridge.hasSession() }) {
            val mgr = getSystemService(MEDIA_PROJECTION_SERVICE) as MediaProjectionManager
            projectionLauncher.launch(mgr.createScreenCaptureIntent())
        },
        Grant("Accessibility (HID)", "Remote taps, swipes and typing", { accessibilityEnabled() }) {
            startActivity(Intent(Settings.ACTION_ACCESSIBILITY_SETTINGS)); status("enable 'Rook Worker' under Accessibility") },
        Grant("Notification access", "Read notifications for the band", {
            NotificationManagerCompat.getEnabledListenerPackages(this).contains(packageName) }) {
            startActivity(Intent(Settings.ACTION_NOTIFICATION_LISTENER_SETTINGS)); status("enable 'Rook Worker' under Notification access") },
        Grant("Battery optimisation off", "Keeps the worker connected in the background", {
            (getSystemService(POWER_SERVICE) as PowerManager).isIgnoringBatteryOptimizations(packageName) }) {
            @Suppress("BatteryLife")
            startActivity(Intent(Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS, Uri.parse("package:$packageName")))
        },
        Grant("Display over other apps", "Lets device.wake turn the screen on", { Settings.canDrawOverlays(this) }) {
            startActivity(Intent(Settings.ACTION_MANAGE_OVERLAY_PERMISSION, Uri.parse("package:$packageName")))
            status("enable 'Display over other apps' → lets device.wake turn the screen on")
        },
        Grant("Exact alarms", "Timers ring on time", { if (Build.VERSION.SDK_INT >= 31) Timers.canExact(this) else null }) {
            if (Build.VERSION.SDK_INT >= 31) startActivity(Intent(Settings.ACTION_REQUEST_SCHEDULE_EXACT_ALARM, Uri.parse("package:$packageName")))
        },
    )

    private fun backgroundLocation() {
        if (!granted(Manifest.permission.ACCESS_COARSE_LOCATION) && !granted(Manifest.permission.ACCESS_FINE_LOCATION)) {
            request(Manifest.permission.ACCESS_FINE_LOCATION, Manifest.permission.ACCESS_COARSE_LOCATION)
            status("Grant location first, then tap Background location again.")
        } else if (Build.VERSION.SDK_INT >= 30) {
            appDetails()
            status("Permissions → Location → Allow all the time. Then stop and start the worker.")
        } else if (Build.VERSION.SDK_INT >= 29) {
            request(Manifest.permission.ACCESS_BACKGROUND_LOCATION)
        } else status("Location granted. No separate background permission is needed on this Android version.")
    }

    private fun accessibilityEnabled(): Boolean {
        val enabled = Settings.Secure.getString(contentResolver, Settings.Secure.ENABLED_ACCESSIBILITY_SERVICES) ?: return false
        val component = ComponentName(this, HidAccessibilityService::class.java)
        return enabled.split(':').any {
            it.equals(component.flattenToString(), true) || it.equals(component.flattenToShortString(), true)
        }
    }

    private fun dp(n: Int) = (n * resources.displayMetrics.density).toInt()

    private fun renderPermissions() {
        if (!::b.isInitialized) return
        val list = b.permList
        list.removeAllViews()
        grants().forEachIndexed { i, g ->
            val state = try { g.state() } catch (_: Exception) { false }
            if (i > 0) list.addView(View(this).apply { setBackgroundColor(getColor(R.color.rook_line)) },
                LinearLayout.LayoutParams(-1, 1).apply { marginStart = dp(16); marginEnd = dp(16) })
            val label = when (state) { true -> "Granted"; false -> "Not granted"; null -> "Not needed" }
            val row = LinearLayout(this).apply {
                orientation = LinearLayout.HORIZONTAL; gravity = Gravity.CENTER_VERTICAL
                minimumHeight = dp(56); setPadding(dp(16), dp(10), dp(16), dp(10))
                isClickable = true; isFocusable = true
                val ripple = android.util.TypedValue()
                theme.resolveAttribute(android.R.attr.selectableItemBackground, ripple, true)
                setBackgroundResource(ripple.resourceId)
                contentDescription = "${g.title}: $label. ${g.why}"
                setOnClickListener { g.action() }
            }
            val text = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL }
            text.addView(TextView(this).apply { this.text = g.title; textSize = 15f; setTextColor(getColor(R.color.rook_fg)) })
            text.addView(TextView(this).apply { this.text = g.why; textSize = 12f; setTextColor(getColor(R.color.rook_dim)) })
            row.addView(text, LinearLayout.LayoutParams(0, -2, 1f))
            row.addView(TextView(this).apply {
                this.text = label; textSize = 12f; typeface = android.graphics.Typeface.MONOSPACE
                setTextColor(when (state) { true -> GRANTED; false -> getColor(R.color.rook_accent); null -> getColor(R.color.rook_dim) })
                setPadding(dp(12), 0, 0, 0)
            })
            list.addView(row)
        }
    }

    // ---- app --------------------------------------------------------------

    private fun setupApp() {
        b.autoUpdates.isChecked = prefs.getBoolean("apk_auto_update", true)
        b.autoUpdates.setOnCheckedChangeListener { _, enabled -> prefs.edit().putBoolean("apk_auto_update", enabled).apply() }
        b.btnAllowUpdates.setOnClickListener { startActivity(Intent(this, ApkUpdatePermissionActivity::class.java)) }
        b.btnUpdate.setOnClickListener {
            startActivity(Intent(Intent.ACTION_VIEW, Uri.parse(BuildConfig.ROOK_SERVER.trimEnd('/') + "/apk")))
        }
        b.voiceUrl.setText(prefs.getString("voice_url", BuildConfig.DEFAULT_VOICE_URL))
        b.voiceToken.setText(prefs.getString("voice_token", BuildConfig.DEFAULT_VOICE_TOKEN))
        b.voiceInsecure.isChecked = prefs.getBoolean("voice_insecure", false)
        b.btnSaveConnection.setOnClickListener { saveVoiceSettings() }
    }

    private fun renderApp() {
        val update = org.json.JSONObject(ApkUpdater.status(this))
        b.updateStatus.text = "${update.optString("state").replace('_', ' ')} · ${update.optString("message")}".trim(' ', '·')
        val canInstall = Build.VERSION.SDK_INT < 26 || packageManager.canRequestPackageInstalls()
        b.btnAllowUpdates.text = if (canInstall) "Installation from Rook allowed · check now" else "Allow installation from Rook"
    }

    // ---- lifecycle --------------------------------------------------------

    override fun onSupportNavigateUp(): Boolean { finish(); return true }

    override fun onPause() {
        super.onPause()
        saveModePrompt()   // keep prompt edits even when leaving without tapping Save
    }

    override fun onResume() {
        super.onResume()
        renderApp()
        renderAssistantState()
        renderPermissions()
    }

    private fun status(msg: String) { b.status.text = log.add(msg) }

    companion object {
        private val GRANTED = Color.rgb(164, 188, 146)
        /** The old one-tap "grant SMS / contacts / calendar / location" set. */
        private val BASIC_PERMISSIONS = arrayOf(
            Manifest.permission.READ_SMS, Manifest.permission.SEND_SMS,
            Manifest.permission.READ_CONTACTS, Manifest.permission.READ_CALL_LOG,
            Manifest.permission.READ_CALENDAR,
            Manifest.permission.ACCESS_FINE_LOCATION, Manifest.permission.ACCESS_COARSE_LOCATION, Manifest.permission.RECORD_AUDIO,
        )
    }
}
