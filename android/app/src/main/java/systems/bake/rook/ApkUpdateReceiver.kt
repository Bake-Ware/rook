package systems.bake.rook

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent

/** Explicit PackageInstaller callback delivered through our mutable PendingIntent. */
class ApkUpdateReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) { ApkUpdater.result(context, intent) }
}
