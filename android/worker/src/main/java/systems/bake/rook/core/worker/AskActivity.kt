package systems.bake.rook.core.worker

import android.Manifest
import android.app.Activity
import android.content.ClipboardManager
import android.content.Intent
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.provider.Settings

/**
 * A see-through screen that is in front only for as long as an answer takes (Bridge): Android's own
 * prompt for a runtime permission; Android's settings page for displaying over other apps, for Do Not
 * Disturb access or for notification access, the answer read when the person comes back; or a read of the clipboard, which Android
 * shows only to the app in front. Whatever ends it, the request is answered once.
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
            else -> requestPermissions(arrayOf(permission), 1)
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
    override fun onActivityResult(requestCode: Int, resultCode: Int, data: Intent?) = answer()

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
    }
}
