package systems.bake.rook.core

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent

/** After boot: the worker starts again, with no code, once the phone has joined (brief AP1). */
class BootReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        if (intent.action == Intent.ACTION_BOOT_COMPLETED) Hub.startWorker(context)
    }
}
