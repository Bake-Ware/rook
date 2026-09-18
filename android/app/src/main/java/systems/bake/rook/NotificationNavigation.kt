package systems.bake.rook

import android.app.PendingIntent
import android.content.Context
import android.content.Intent

object NotificationNavigation {
    @JvmStatic fun mainActivity(ctx: Context): PendingIntent = PendingIntent.getActivity(
        ctx, 100, Intent(ctx, MainActivity::class.java).addFlags(
            Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_CLEAR_TOP or Intent.FLAG_ACTIVITY_SINGLE_TOP),
        PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE)
}
