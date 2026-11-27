package systems.bake.rook.core.worker

import android.app.Activity
import android.app.KeyguardManager
import android.os.Build
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.view.WindowManager

/**
 * A see-through screen that turns the screen on, over the lock screen (brief AP5; 0.5's WakeActivity),
 * for screen.on, phone.unlock and the screen-driving caps that wake the phone first. It takes no
 * touches: whatever is under it, the lock screen included, gets them.
 *
 * With [DISMISS] it also asks Android to dismiss the lock screen: with no secure lock that unlocks; on
 * a secure lock it brings up the PIN pad (Android never bypasses the credential) and stays until the
 * lock screen goes or the pad is closed, or until Bridge.wakeEnd. With [HOLD_MS] it stays that long;
 * without, it goes as soon as it is up. Bridge.wake waits for it to be up.
 */
class WakeActivity : Activity() {
    private val main = Handler(Looper.getMainLooper())

    override fun onCreate(saved: Bundle?) {
        super.onCreate(saved)
        if (Build.VERSION.SDK_INT >= 27) {
            setShowWhenLocked(true)
            setTurnScreenOn(true)
        } else {
            @Suppress("DEPRECATION")
            window.addFlags(WindowManager.LayoutParams.FLAG_SHOW_WHEN_LOCKED or WindowManager.LayoutParams.FLAG_TURN_SCREEN_ON)
        }
        val dismiss = intent.getBooleanExtra(DISMISS, false)
        // Touches go through to what is under it; asking to dismiss the lock screen takes the focus, which Android wants.
        window.addFlags(WindowManager.LayoutParams.FLAG_NOT_TOUCHABLE)
        if (!dismiss) window.addFlags(WindowManager.LayoutParams.FLAG_NOT_FOCUSABLE)
        current = this
        if (dismiss && Build.VERSION.SDK_INT >= 26) {
            getSystemService(KeyguardManager::class.java).requestDismissKeyguard(this, object : KeyguardManager.KeyguardDismissCallback() {
                override fun onDismissSucceeded() = finish()
                override fun onDismissCancelled() = finish()
                override fun onDismissError() = finish()
            })
        }
        val hold = intent.getLongExtra(HOLD_MS, 0L)
        if (hold > 0) main.postDelayed({ finish() }, hold)
        goWhenUp = !dismiss && hold <= 0
    }

    private var goWhenUp = false

    override fun onResume() {
        super.onResume()
        window.decorView.post {
            Bridge.woke()
            if (goWhenUp) finish()
        }
    }

    override fun onDestroy() {
        if (current === this) current = null
        main.removeCallbacksAndMessages(null)
        Bridge.woke()       // never leaves a caller waiting
        super.onDestroy()
    }

    companion object {
        const val DISMISS = "dismiss"
        const val HOLD_MS = "hold_ms"
        @Volatile internal var current: WakeActivity? = null
    }
}
