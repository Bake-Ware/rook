package systems.bake.rook

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import androidx.core.app.RemoteInput

/** The Reply action on a voice.speak reply notification: hands typed text to [SpeakBridge]. */
class ReplyReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        if (intent.action != ACTION) return
        val id = intent.getStringExtra(EXTRA_ID) ?: return
        val text = RemoteInput.getResultsFromIntent(intent)?.getCharSequence(KEY_TEXT)?.toString() ?: return
        SpeakBridge.textReply(context.applicationContext, id, text)
    }

    companion object {
        const val ACTION = "systems.bake.rook.VOICE_REPLY"
        const val EXTRA_ID = "speech_id"
        const val KEY_TEXT = "reply_text"
    }
}
