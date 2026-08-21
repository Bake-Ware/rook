package systems.bake.rook

import android.app.Activity
import android.app.KeyguardManager
import android.content.Context
import android.os.Build
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.view.WindowManager

/**
 * Invisible activity whose only job is to turn the screen ON (and show over the
 * keyguard) so the accessibility service can then interact with the lock screen.
 * Launched by the `device.wake` cap. Needs "Display over other apps"
 * (SYSTEM_ALERT_WINDOW) so the background start isn't blocked on Android 10+.
 *
 * With extra "dismiss" it also asks the keyguard to dismiss — on a PIN-secured
 * device that just surfaces the PIN pad (Android won't bypass the credential),
 * which is what the unlock flow then taps.
 */
class WakeActivity : Activity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O_MR1) {
            setShowWhenLocked(true)
            setTurnScreenOn(true)
        } else {
            @Suppress("DEPRECATION")
            window.addFlags(
                WindowManager.LayoutParams.FLAG_SHOW_WHEN_LOCKED or
                    WindowManager.LayoutParams.FLAG_TURN_SCREEN_ON or
                    WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON
            )
        }

        if (intent?.getBooleanExtra("dismiss", false) == true) {
            try {
                val km = getSystemService(Context.KEYGUARD_SERVICE) as KeyguardManager
                km.requestDismissKeyguard(this, null)
            } catch (_: Throwable) {}
        }

        // Stay just long enough to guarantee the screen is on + interactive,
        // then get out of the way (back to the lock screen, screen still on).
        val hold = intent?.getLongExtra("hold_ms", 1200L) ?: 1200L
        Handler(Looper.getMainLooper()).postDelayed({ finish() }, hold.coerceIn(200L, 15000L))
    }
}
