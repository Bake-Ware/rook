package systems.bake.rook

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import android.media.AudioAttributes
import android.media.RingtoneManager
import android.os.Build
import androidx.core.app.NotificationCompat

/**
 * Notification channels for attention (stable ids; Android keeps the user's per-channel choices).
 *  - rook_urgent: alarm sound, vibration, bypasses Do Not Disturb, full-screen card.
 *  - rook_family / rook_approvals: heads-up. rook_waiting: a normal notification (status bar and
 *    shade, one sound, no pop-up) for decisions the hub queued. rook_digest: low. rook_jobs: silent.
 * Do Not Disturb bypass only sticks when the app has Notification Policy access; [ensure]
 * re-submits the urgent channel so a later grant takes effect (Android 10+ lets an app with
 * that access update bypass on an existing channel).
 */
object AttentionChannels {
    const val URGENT = "rook_urgent"
    const val FAMILY = "rook_family"
    const val APPROVALS = "rook_approvals"
    const val WAITING = "rook_waiting"
    const val DIGEST = "rook_digest"
    const val JOBS = "rook_jobs"
    private val IDS = mapOf("urgent" to URGENT, "family" to FAMILY, "approvals" to APPROVALS, "digest" to DIGEST, "jobs" to JOBS)

    /** Channel id for a contract name (urgent/family/approvals/digest/jobs), or "" when unknown. */
    @JvmStatic fun idFor(name: String?): String = IDS[name?.trim()?.lowercase()] ?: ""

    @JvmStatic fun ensure(ctx: Context) {
        if (Build.VERSION.SDK_INT < 26) return
        val nm = ctx.getSystemService(NotificationManager::class.java) ?: return
        val alarm = RingtoneManager.getDefaultUri(RingtoneManager.TYPE_ALARM) ?: RingtoneManager.getDefaultUri(RingtoneManager.TYPE_NOTIFICATION)
        val alarmAudio = AudioAttributes.Builder().setUsage(AudioAttributes.USAGE_ALARM)
            .setContentType(AudioAttributes.CONTENT_TYPE_SONIFICATION).build()
        nm.createNotificationChannels(listOf(
            NotificationChannel(URGENT, "Urgent", NotificationManager.IMPORTANCE_HIGH).apply {
                description = "Emergencies and urgent decisions: rings like an alarm, even in Do Not Disturb"
                setSound(alarm, alarmAudio); enableVibration(true); vibrationPattern = longArrayOf(0, 600, 300, 600, 300, 600)
                setBypassDnd(true); lockscreenVisibility = Notification.VISIBILITY_PUBLIC
            },
            NotificationChannel(FAMILY, "Family", NotificationManager.IMPORTANCE_HIGH).apply {
                description = "Family, school and health notifications"; enableVibration(true)
            },
            NotificationChannel(APPROVALS, "Decisions", NotificationManager.IMPORTANCE_HIGH).apply {
                description = "Yes/No decisions from Rook"; enableVibration(true)
            },
            NotificationChannel(WAITING, "Waiting decisions", NotificationManager.IMPORTANCE_DEFAULT).apply {
                description = "Decisions that can wait: shown in the shade without popping up"
            },
            NotificationChannel(DIGEST, "Digest", NotificationManager.IMPORTANCE_LOW).apply { description = "Summaries" },
            NotificationChannel(JOBS, "Jobs", NotificationManager.IMPORTANCE_MIN).apply {
                description = "Background job updates"; setSound(null, null); enableVibration(false)
            },
        ))
    }

    /** Whether a channel is still enabled by the user (true below Android 8). */
    fun enabled(ctx: Context, id: String): Boolean {
        if (Build.VERSION.SDK_INT < 26) return true
        val ch = ctx.getSystemService(NotificationManager::class.java)?.getNotificationChannel(id) ?: return true
        return ch.importance != NotificationManager.IMPORTANCE_NONE
    }

    fun notificationId(cardId: String) = 0x2A000 + (cardId.hashCode() and 0xFFFF)

    private fun flags() = PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE

    /** The channel [card] is posted on. */
    fun channelFor(card: AttentionCard) = when {
        card.urgent -> URGENT
        card.waiting -> WAITING
        else -> idFor(card.channel).ifEmpty { APPROVALS }
    }

    /** Ids of this app's notifications in the shade now (null when Android can't say). */
    fun active(ctx: Context): Set<Int>? = try {
        ctx.getSystemService(NotificationManager::class.java)?.activeNotifications?.map { it.id }?.toSet()
    } catch (_: Exception) { null }

    /** Post or refresh the notification for [card]; a known card refreshes silently, [silent] never alerts. */
    fun postCard(ctx: Context, card: AttentionCard, ring: Boolean, silent: Boolean = false) {
        ensure(ctx)
        val nid = notificationId(card.id)
        val open = Intent(ctx, AttentionCardActivity::class.java).putExtra(AttentionCardActivity.EXTRA_ID, card.id)
            .putExtra(AttentionCardActivity.EXTRA_OPENED, true)
            .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_SINGLE_TOP)
        val content = PendingIntent.getActivity(ctx, nid, open, flags())
        val gone = PendingIntent.getBroadcast(ctx, nid, Intent(ctx, AttentionReceiver::class.java)
            .setAction(AttentionReceiver.ACTION_DISMISSED).putExtra(AttentionCardActivity.EXTRA_ID, card.id), flags())
        val channel = channelFor(card)
        val text = when (card.state) { "sent" -> "Sent… ${card.line}"; "failed" -> "Didn’t go through: ${card.message.ifBlank { card.line }}"; else -> card.line }
        val b = NotificationCompat.Builder(ctx, channel)
            .setSmallIcon(android.R.drawable.ic_dialog_info)
            .setContentTitle(card.source.ifBlank { "Rook" }).setContentText(text)
            .setStyle(if (card.state == "pending" && card.body.isNotBlank())
                // Expanded: the title on top, the body's few plain lines under it.
                NotificationCompat.BigTextStyle().setBigContentTitle(card.line).bigText(card.body)
                else NotificationCompat.BigTextStyle().bigText(text))
            .setContentIntent(content).setDeleteIntent(gone).setAutoCancel(false).setOnlyAlertOnce(!ring).setSilent(silent)
            .setPriority(when (channel) { URGENT -> NotificationCompat.PRIORITY_MAX; FAMILY, APPROVALS -> NotificationCompat.PRIORITY_HIGH
                WAITING -> NotificationCompat.PRIORITY_DEFAULT
                DIGEST -> NotificationCompat.PRIORITY_LOW; else -> NotificationCompat.PRIORITY_MIN })
            .setCategory(if (card.urgent) NotificationCompat.CATEGORY_ALARM else NotificationCompat.CATEGORY_REMINDER)
            .setVisibility(if (card.urgent) NotificationCompat.VISIBILITY_PUBLIC else NotificationCompat.VISIBILITY_PRIVATE)
        if (card.expiresAt > 0) b.setTimeoutAfter(((card.expiresAt - System.currentTimeMillis() / 1000.0) * 1000).toLong().coerceAtLeast(1000L))
        if (card.urgent) b.setFullScreenIntent(PendingIntent.getActivity(ctx, nid + 1,
            Intent(open).putExtra(AttentionCardActivity.EXTRA_OPENED, false), flags()), true)
        notify(ctx, nid, b.build())
    }

    /** A heads-up for a family-priority notification from another app; tapping opens that app's own notification target. */
    fun postFamily(ctx: Context, pkg: String, title: String, text: String, target: PendingIntent?) {
        ensure(ctx)
        val b = NotificationCompat.Builder(ctx, FAMILY).setSmallIcon(android.R.drawable.stat_notify_chat)
            .setContentTitle(title).setContentText(text).setStyle(NotificationCompat.BigTextStyle().bigText(text))
            .setContentIntent(target ?: NotificationNavigation.mainActivity(ctx)).setAutoCancel(true)
            .setPriority(NotificationCompat.PRIORITY_HIGH).setCategory(NotificationCompat.CATEGORY_MESSAGE)
            .setVisibility(NotificationCompat.VISIBILITY_PRIVATE)
        notify(ctx, 0x2B000 + (pkg.hashCode() and 0xFFFF), b.build())
    }

    fun cancel(ctx: Context, cardId: String) {
        ctx.getSystemService(NotificationManager::class.java)?.cancel(notificationId(cardId))
    }

    private fun notify(ctx: Context, id: Int, n: Notification) {
        try { ctx.getSystemService(NotificationManager::class.java)?.notify(id, n) }
        catch (_: SecurityException) { /* POST_NOTIFICATIONS not granted: the card still shows in the app. */ }
    }
}
