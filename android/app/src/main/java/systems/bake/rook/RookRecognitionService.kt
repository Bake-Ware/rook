package systems.bake.rook

import android.content.Intent
import android.speech.RecognitionService
import android.speech.SpeechRecognizer

/** Required metadata on Android 12+, where this component is unused by the
 * assistant role. Rook is not a system-wide dictation provider. Older Android
 * selects the ACTION_ASSIST activity; its VoiceInteractionService stays disabled.
 */
class RookRecognitionService : RecognitionService() {
    override fun onStartListening(intent: Intent?, callback: Callback) = callback.error(SpeechRecognizer.ERROR_CLIENT)
    override fun onStopListening(callback: Callback) = callback.error(SpeechRecognizer.ERROR_CLIENT)
    override fun onCancel(callback: Callback) {}
}
