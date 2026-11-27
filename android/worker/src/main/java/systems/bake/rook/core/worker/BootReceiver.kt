package systems.bake.rook.core.worker

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent

/**
 * Starts the worker again after the phone boots and after the app is updated, if it was on. After a boot
 * it starts before anyone unlocks the phone (LOCKED_BOOT_COMPLETED, brief AP8: what it needs is in
 * [Storage]); BOOT_COMPLETED, once the phone is unlocked, starts it too for an install whose state was
 * still in ordinary storage, which [Storage] moves then. A worker already running goes on.
 */
class BootReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        if (intent.action !in STARTS) return
        if (RookWorker.prefs(context).getBoolean(RookWorker.ON, false)) WorkerService.start(context)
    }

    private companion object {
        val STARTS = setOf(Intent.ACTION_LOCKED_BOOT_COMPLETED, Intent.ACTION_BOOT_COMPLETED, Intent.ACTION_MY_PACKAGE_REPLACED)
    }
}
