package systems.bake.rook

import android.content.Context
import android.content.Intent
import android.media.AudioManager
import android.media.session.MediaSession
import android.media.session.PlaybackState
import android.os.Build
import android.os.Handler
import android.os.SystemClock
import android.util.Log
import android.view.KeyEvent

/**
 * Pure decision logic for headset / assistant keys reaching Rook's media session.
 *
 *  - VOICE_ASSIST, ASSIST, SEARCH: press = talk (or interrupt while Rook speaks).
 *  - MEDIA_PLAY_PAUSE: long press = talk.
 *  - HEADSETHOOK, MEDIA_PLAY_PAUSE: a short press interrupts while Rook is
 *    thinking/speaking, otherwise it is passed on to the next media app as a normal
 *    click so music controls keep working.
 *  - HEADSETHOOK long press normally never reaches a media session: the system
 *    handles it itself (unlocked: ACTION_WEB_SEARCH, which Rook deliberately does
 *    not register for; locked / screen off: VOICE_SEARCH_HANDS_FREE, which
 *    VoiceCommandActivity handles). The long-press branch below only covers devices
 *    that do deliver it.
 *  - Every other key is passed on untouched.
 */
internal class HeadsetKeyDecoder(private val longPressMs: Long = 600L) {
    enum class Action { TALK, INTERRUPT, CONSUME, PASS, CLICK }

    private var longFired = false

    fun decide(keyCode: Int, down: Boolean, repeatCount: Int, longPress: Boolean, heldMs: Long, speaking: Boolean): Action =
        when (keyCode) {
            KEY_VOICE_ASSIST, KEY_ASSIST, KEY_SEARCH ->
                if (down && repeatCount == 0) (if (speaking) Action.INTERRUPT else Action.TALK) else Action.CONSUME
            KEY_HEADSETHOOK, KEY_MEDIA_PLAY_PAUSE -> when {
                down && repeatCount == 0 && !longPress -> { longFired = false; Action.CONSUME }
                down -> if (longFired) Action.CONSUME else { longFired = true; Action.TALK }
                longFired -> { longFired = false; Action.CONSUME }
                heldMs >= longPressMs -> Action.TALK
                speaking -> Action.INTERRUPT
                else -> Action.CLICK
            }
            else -> Action.PASS
        }

    companion object {
        // KeyEvent.KEYCODE_* (stable platform constants), literal so this stays pure.
        const val KEY_HEADSETHOOK = 79
        const val KEY_SEARCH = 84
        const val KEY_MEDIA_PLAY_PAUSE = 85
        const val KEY_ASSIST = 219
        const val KEY_VOICE_ASSIST = 231
    }
}

/**
 * MediaSession owned by VoiceService while it runs, so Bluetooth/wired headset
 * buttons routed to Rook start or interrupt a voice turn. Keys Rook does not want
 * are re-dispatched with the session briefly inactive, so they reach whichever
 * media app would otherwise have received them.
 */
internal class HeadsetButtons(
    ctx: Context,
    private val main: Handler,
    private val speaking: () -> Boolean,
    private val onAction: (HeadsetKeyDecoder.Action) -> Unit,
) {
    private val audio = ctx.getSystemService(Context.AUDIO_SERVICE) as AudioManager
    private val decoder = HeadsetKeyDecoder()
    private val session = MediaSession(ctx, "rook-voice")
    private var released = false
    private val reactivate = Runnable { if (!released) session.isActive = true }

    init {
        session.setCallback(object : MediaSession.Callback() {
            override fun onMediaButtonEvent(mediaButtonIntent: Intent): Boolean {
                val ev = keyEvent(mediaButtonIntent) ?: return false
                val down = ev.action == KeyEvent.ACTION_DOWN
                val action = decoder.decide(ev.keyCode, down, ev.repeatCount, ev.isLongPress,
                    ev.eventTime - ev.downTime, speaking())
                Log.i(TAG, "key=${ev.keyCode} down=$down -> $action")
                when (action) {
                    HeadsetKeyDecoder.Action.TALK, HeadsetKeyDecoder.Action.INTERRUPT -> onAction(action)
                    HeadsetKeyDecoder.Action.CONSUME -> {}
                    HeadsetKeyDecoder.Action.PASS -> passOn(listOf(ev))
                    HeadsetKeyDecoder.Action.CLICK -> {
                        val now = SystemClock.uptimeMillis()
                        passOn(listOf(KeyEvent(now, now, KeyEvent.ACTION_DOWN, ev.keyCode, 0),
                            KeyEvent(now, now, KeyEvent.ACTION_UP, ev.keyCode, 0)))
                    }
                }
                return true
            }
        }, main)
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.O) {
            @Suppress("DEPRECATION")
            session.setFlags(MediaSession.FLAG_HANDLES_MEDIA_BUTTONS or MediaSession.FLAG_HANDLES_TRANSPORT_CONTROLS)
        }
        session.setPlaybackState(PlaybackState.Builder()
            .setActions(PlaybackState.ACTION_PLAY_PAUSE)
            .setState(PlaybackState.STATE_STOPPED, 0L, 1f)
            .build())
        session.isActive = true
    }

    private fun passOn(events: List<KeyEvent>) {
        main.removeCallbacks(reactivate)
        session.isActive = false
        for (e in events) try { audio.dispatchMediaKeyEvent(e) } catch (_: Exception) {}
        main.postDelayed(reactivate, 1_000L)
    }

    fun release() {
        released = true
        main.removeCallbacks(reactivate)
        try { session.isActive = false; session.release() } catch (_: Exception) {}
    }

    @Suppress("DEPRECATION")
    private fun keyEvent(i: Intent): KeyEvent? =
        if (Build.VERSION.SDK_INT >= 33) i.getParcelableExtra(Intent.EXTRA_KEY_EVENT, KeyEvent::class.java)
        else i.getParcelableExtra(Intent.EXTRA_KEY_EVENT)

    companion object { private const val TAG = "HeadsetButtons" }
}
