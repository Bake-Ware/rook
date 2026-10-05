package systems.bake.rook

import android.app.Activity
import android.content.Intent
import android.os.Bundle

/**
 * Invisible entry point for legacy voice/assist triggers that Bluetooth headsets and
 * older Android builds still send: VOICE_COMMAND (HFP voice-recognition button),
 * SEARCH_LONG_PRESS, VOICE_ASSIST and the hands-free voice search
 * (VOICE_SEARCH_HANDS_FREE) the system sends for a HEADSETHOOK long press while the
 * device is locked. (Unlocked, that long press becomes ACTION_WEB_SEARCH, which Rook
 * does not claim.) Each is forwarded to MainActivity's existing
 * ACTION_ASSIST path, which owns permission requests and opens a voice session.
 */
class VoiceCommandActivity : Activity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        startActivity(Intent(this, MainActivity::class.java)
            .setAction(Intent.ACTION_ASSIST)
            .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_CLEAR_TOP or Intent.FLAG_ACTIVITY_SINGLE_TOP))
        finish()
    }
}
