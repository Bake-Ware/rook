package systems.bake.rook.core.worker

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent

/** Starts the worker again after the phone boots and after the app is updated, if it was on. */
class BootReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        if (intent.action != Intent.ACTION_BOOT_COMPLETED && intent.action != Intent.ACTION_MY_PACKAGE_REPLACED) return
        if (RookWorker.prefs(context).getBoolean(RookWorker.ON, false)) WorkerService.start(context)
    }
}
