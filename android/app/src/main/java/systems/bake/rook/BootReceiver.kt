package systems.bake.rook

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent

/** Restart the worker after reboot, using the last-saved band settings. */
class BootReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        val action = intent.action
        if (action != Intent.ACTION_BOOT_COMPLETED &&
            action != Intent.ACTION_LOCKED_BOOT_COMPLETED) return

        val prefs = context.getSharedPreferences("rook", Context.MODE_PRIVATE)
        // Only autostart if the user has started it at least once before.
        if (!prefs.getBoolean("autostart", false)) return

        val hub = prefs.getString("hub", BuildConfig.DEFAULT_HUB)!!
        val psk = prefs.getString("psk", BuildConfig.DEFAULT_PSK)!!
        val fallback = (android.os.Build.MODEL ?: "android").replace(' ', '-')
        val name = prefs.getString("name", fallback)!!
        WorkerService.start(context, hub, psk, name)
    }
}
