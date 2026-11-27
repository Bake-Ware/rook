package systems.bake.rook.core.worker

import android.Manifest
import android.app.Activity
import android.content.ClipboardManager
import android.content.ComponentName
import android.content.Intent
import android.media.projection.MediaProjectionConfig
import android.media.projection.MediaProjectionManager
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.provider.Settings

/**
 * A see-through screen that is in front only for as long as an answer takes (Bridge): Android's own
 * prompt for a runtime permission; Android's settings page for displaying over other apps, for Do Not
 * Disturb access or for notification access, the answer read when the person comes back; or a read of the clipboard, which Android
 * shows only to the app in front. For brief AP5 also: Android's page for Rook's accessibility service, and
 * Android's own screen capture prompt, whose "Start" starts the capture service from here, while this
 * screen is in front (Capture). Whatever ends it, the request is answered once.
 */
class AskActivity : Activity() {
    private var key: String? = null

    override fun onCreate(saved: Bundle?) {
        super.onCreate(saved)
        Bridge.attach(this)
        if (intent.getBooleanExtra(CLIPBOARD, false)) {
            key = Bridge.CLIPBOARD          // read once the window has focus
            return
        }
        if (intent.getBooleanExtra(CAPTURE, false)) {
            key = Bridge.CAPTURE
            if (saved != null) return
            val projections = getSystemService(MediaProjectionManager::class.java)
            // The whole screen, as screen.capture's picture is (Android 14 would otherwise offer one app).
            val ask = if (Build.VERSION.SDK_INT >= 34) projections.createScreenCaptureIntent(MediaProjectionConfig.createConfigForDefaultDisplay())
                      else projections.createScreenCaptureIntent()
            startActivityForResult(ask, CAPTURE_ASK)
            return
        }
        val permission = intent.getStringExtra(PERMISSION) ?: return finish()
        key = permission
        if (saved != null) return           // turned, say: the prompt is still up
        when (permission) {
            Manifest.permission.SYSTEM_ALERT_WINDOW ->
                startActivityForResult(Intent(Settings.ACTION_MANAGE_OVERLAY_PERMISSION, Uri.parse("package:$packageName")), 1)
            Manifest.permission.ACCESS_NOTIFICATION_POLICY ->
                startActivityForResult(Intent(Settings.ACTION_NOTIFICATION_POLICY_ACCESS_SETTINGS), 1)
            Bridge.NOTIFICATION_ACCESS ->       // Rook's own switch where Android has one (11 and later), else the list
                startActivityForResult(if (Build.VERSION.SDK_INT >= 30) {
                    Intent(Settings.ACTION_NOTIFICATION_LISTENER_DETAIL_SETTINGS).putExtra(
                        Settings.EXTRA_NOTIFICATION_LISTENER_COMPONENT_NAME, NotificationListener.component(this).flattenToString())
                } else {
                    Intent(Settings.ACTION_NOTIFICATION_LISTENER_SETTINGS)
                }, 1)
            // Android's list of accessibility services, Rook's row picked out (its own page needs a system permission).
            Bridge.ACCESSIBILITY -> startActivityForResult(Intent(Settings.ACTION_ACCESSIBILITY_SETTINGS)
                .putExtra(":settings:fragment_args_key", ComponentName(this, Hands::class.java).flattenToString()), 1)
            else -> requestPermissions(permission.split(',').toTypedArray(), 1)     // askAll's several, comma-joined
        }
    }

    override fun onWindowFocusChanged(hasFocus: Boolean) {
        super.onWindowFocusChanged(hasFocus)
        if (hasFocus && key == Bridge.CLIPBOARD) {
            val clip = getSystemService(ClipboardManager::class.java).primaryClip
            val text = if (clip != null && clip.itemCount > 0) clip.getItemAt(0).coerceToText(this)?.toString() else null
            answer(text)
        }
    }

    override fun onRequestPermissionsResult(requestCode: Int, permissions: Array<out String>, grantResults: IntArray) = answer()

    @Deprecated("startActivityForResult is all a settings page answers")
    override fun onActivityResult(requestCode: Int, resultCode: Int, data: Intent?) {
        if (requestCode != CAPTURE_ASK || resultCode != RESULT_OK || data == null) return answer()
        // Allowed: the capture service goes into the foreground (type mediaProjection) and only then takes the
        // projection, and it answers the request once the session is up (Capture). Started from here, while
        // this screen is in front, as Android requires of a foreground service started for a capture.
        key = null
        try {
            startForegroundService(Intent(this, CaptureService::class.java)
                .putExtra(CaptureService.CODE, resultCode).putExtra(CaptureService.DATA, data))
        } catch (e: Exception) {
            Bridge.answered(Bridge.CAPTURE, "Android did not let Rook start its screen capture service: ${e.message}")
        }
        finish()
    }

    override fun onDestroy() {
        if (!isChangingConfigurations) key?.let { Bridge.answered(it) }     // dismissed: answered with what is granted now
        super.onDestroy()
    }

    private fun answer(text: String? = null) {
        key?.let { Bridge.answered(it, text) }
        key = null
        finish()
    }

    companion object {
        const val PERMISSION = "permission"
        const val CLIPBOARD = "clipboard"
        const val CAPTURE = "capture"
        private const val CAPTURE_ASK = 2
    }
}
