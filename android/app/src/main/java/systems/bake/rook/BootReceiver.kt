package systems.bake.rook

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent

/** Restart the worker after reboot, using the last-saved band settings. */
class BootReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        val action = intent.action
        if (action == Intent.ACTION_MY_PACKAGE_REPLACED) ApkUpdater.replaced(context)
        // Alarms are cleared by a reboot and an app update; voice timers are armed again
        // (whether or not the worker autostarts), and re-armed exact once that is allowed.
        if (action == Intent.ACTION_MY_PACKAGE_REPLACED || action == Intent.ACTION_BOOT_COMPLETED ||
            action == "android.app.action.SCHEDULE_EXACT_ALARM_PERMISSION_STATE_CHANGED") {
            try { Timers.rearm(context) } catch (t: Throwable) { android.util.Log.w("RookBootReceiver", "timer re-arm failed", t) }
        }
        if (action != Intent.ACTION_MY_PACKAGE_REPLACED && action != Intent.ACTION_BOOT_COMPLETED &&
            action != Intent.ACTION_LOCKED_BOOT_COMPLETED) return

        val prefs = context.getSharedPreferences("rook", Context.MODE_PRIVATE)
        // Only autostart if the user has started it at least once before.
        if (!prefs.getBoolean("autostart", false)) return

        val hub = prefs.getString("hub", BuildConfig.DEFAULT_HUB)!!
        val psk = prefs.getString("psk", BuildConfig.DEFAULT_PSK)!!
        val fallback = (android.os.Build.MODEL ?: "android").replace(' ', '-')
        val name = prefs.getString("name", fallback)!!
        // Android 14+ forbids starting a dataSync foreground service straight from
        // BOOT_COMPLETED (ForegroundServiceStartNotAllowedException). Don't let
        // that crash the boot broadcast — the worker is START_STICKY and the user
        // can also start it from the app. Best-effort autostart only.
        try {
            WorkerService.start(context, hub, psk, name)
        } catch (t: Throwable) {
            android.util.Log.w("RookBootReceiver", "autostart on boot not permitted", t)
        }
    }
}
