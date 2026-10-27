package systems.bake.rook

import android.Manifest
import android.app.Activity
import android.content.Intent
import android.content.pm.PackageManager

/**
 * The Talk widget's tap: an invisible screen for a moment, so the voice session starts while the
 * app is in the foreground (Android only lets a visible app open the microphone from the
 * background). Does what the app's Talk button does: thinking or speaking -> interrupt, otherwise
 * talk now. With no mic grant or no voice server yet, the app's own assist path asks for them.
 */
class WidgetTalkActivity : Activity() {
    override fun onResume() {
        super.onResume()
        val prefs = getSharedPreferences("rook", MODE_PRIVATE)
        val url = prefs.getString("voice_url", BuildConfig.DEFAULT_VOICE_URL) ?: ""
        val mic = checkSelfPermission(Manifest.permission.RECORD_AUDIO) == PackageManager.PERMISSION_GRANTED
        when {
            VoiceBus.state == "thinking" || VoiceBus.state == "speaking" -> VoiceService.interrupt(this)
            !mic || url.isEmpty() -> startActivity(Intent(this, MainActivity::class.java)
                .setAction(Intent.ACTION_ASSIST)
                .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_CLEAR_TOP or Intent.FLAG_ACTIVITY_SINGLE_TOP))
            else -> VoiceService.start(this, url, prefs.getBoolean("voice_insecure", false))
        }
        finish()
    }
}
