package systems.bake.rook

import android.app.PendingIntent
import android.appwidget.AppWidgetManager
import android.appwidget.AppWidgetProvider
import android.content.ComponentName
import android.content.Context
import android.content.Intent
import android.content.res.Configuration
import android.os.Bundle
import android.util.Log
import android.view.View
import android.widget.RemoteViews

/**
 * Home-screen widgets: Talk (push-to-talk to the house agent's voice) and Waiting on you (the
 * cards in [AttentionStore], each opening its card screen). Both read only what is on the phone,
 * so they work offline; the store calls [refresh] and [VoiceService] calls [voiceState] when what
 * they show changes. What they show is decided in [WidgetModel].
 */
class TalkWidget : AppWidgetProvider() {
    override fun onUpdate(ctx: Context, mgr: AppWidgetManager, ids: IntArray) = RookWidgets.renderTalk(ctx, mgr, ids, VoiceBus.state)
}

class WaitingWidget : AppWidgetProvider() {
    override fun onUpdate(ctx: Context, mgr: AppWidgetManager, ids: IntArray) = RookWidgets.renderWaiting(ctx, mgr, ids)
    override fun onAppWidgetOptionsChanged(ctx: Context, mgr: AppWidgetManager, id: Int, options: Bundle?) =
        RookWidgets.renderWaiting(ctx, mgr, intArrayOf(id))
}

object RookWidgets {
    private const val TAG = "RookWidgets"
    @Volatile private var lastTalk: WidgetModel.Talk? = null

    private fun ids(ctx: Context, mgr: AppWidgetManager, cls: Class<*>) = mgr.getAppWidgetIds(ComponentName(ctx, cls))

    /** The cards changed: redraw every Waiting widget. */
    fun refresh(ctx: Context) {
        runCatching {
            val mgr = AppWidgetManager.getInstance(ctx) ?: return
            val ids = ids(ctx, mgr, WaitingWidget::class.java)
            if (ids.isNotEmpty()) renderWaiting(ctx, mgr, ids)
        }.onFailure { Log.w(TAG, "waiting widget refresh failed", it) }
    }

    /** The voice session's state: redraw the mics only when what they show changes. */
    fun voiceState(ctx: Context, state: String) {
        val t = WidgetModel.talk(state)
        if (t == lastTalk) return
        lastTalk = t
        runCatching {
            val mgr = AppWidgetManager.getInstance(ctx) ?: return
            ids(ctx, mgr, TalkWidget::class.java).takeIf { it.isNotEmpty() }?.let { renderTalk(ctx, mgr, it, state) }
            ids(ctx, mgr, WaitingWidget::class.java).takeIf { it.isNotEmpty() }?.let { renderWaiting(ctx, mgr, it, state) }
        }.onFailure { Log.w(TAG, "talk widget refresh failed", it) }
    }

    fun renderTalk(ctx: Context, mgr: AppWidgetManager, ids: IntArray, state: String) {
        val t = WidgetModel.talk(state)
        val v = RemoteViews(ctx.packageName, R.layout.widget_talk)
        v.setImageViewResource(R.id.widget_mic, if (t.active) R.drawable.ic_mic_active else R.drawable.ic_mic)
        v.setTextViewText(R.id.widget_talk_label, t.label)
        v.setOnClickPendingIntent(R.id.widget_talk, talkIntent(ctx))
        ids.forEach { mgr.updateAppWidget(it, v) }
    }

    fun renderWaiting(ctx: Context, mgr: AppWidgetManager, ids: IntArray, state: String = VoiceBus.state) {
        val stack = AttentionStore.stack(ctx)
        val talk = WidgetModel.talk(state)
        val accent = ctx.getColor(R.color.rook_accent)
        val fg = ctx.getColor(R.color.rook_fg)
        val dim = ctx.getColor(R.color.rook_dim)
        // Launchers report a widget's portrait height as its MAX_HEIGHT and its landscape height as MIN_HEIGHT.
        val portrait = ctx.resources.configuration.orientation != Configuration.ORIENTATION_LANDSCAPE
        val heightKey = if (portrait) AppWidgetManager.OPTION_APPWIDGET_MAX_HEIGHT else AppWidgetManager.OPTION_APPWIDGET_MIN_HEIGHT
        for (id in ids) {
            val height = mgr.getAppWidgetOptions(id)?.getInt(heightKey, 0)?.takeIf { it > 0 } ?: 110
            val w = WidgetModel.waiting(stack, WidgetModel.rowsFor(height))
            val v = RemoteViews(ctx.packageName, R.layout.widget_waiting)
            v.setTextViewText(R.id.widget_waiting_title, if (w.total == 0) "Nothing waiting on you" else "Waiting on you")
            v.setTextViewText(R.id.widget_waiting_count, if (w.total == 0) "" else w.total.toString())
            v.setViewVisibility(R.id.widget_waiting_count, if (w.total == 0) View.GONE else View.VISIBLE)
            v.setImageViewResource(R.id.widget_waiting_mic, if (talk.active) R.drawable.ic_mic_active else R.drawable.ic_mic)
            v.setOnClickPendingIntent(R.id.widget_waiting_mic, talkIntent(ctx))
            v.setOnClickPendingIntent(R.id.widget_waiting_header, NotificationNavigation.mainActivity(ctx))
            v.removeAllViews(R.id.widget_rows)
            for (row in w.rows) {
                val r = RemoteViews(ctx.packageName, R.layout.widget_waiting_row)
                r.setTextViewText(R.id.widget_row_text, row.text)
                r.setTextColor(R.id.widget_row_text, if (row.loud) accent else fg)
                r.setOnClickPendingIntent(R.id.widget_row_text, cardIntent(ctx, row.id))
                v.addView(R.id.widget_rows, r)
            }
            if (w.more > 0) {
                val r = RemoteViews(ctx.packageName, R.layout.widget_waiting_row)
                r.setTextViewText(R.id.widget_row_text, "+${w.more} more")
                r.setTextColor(R.id.widget_row_text, dim)
                r.setOnClickPendingIntent(R.id.widget_row_text, cardIntent(ctx, w.rows.first().id))
                v.addView(R.id.widget_rows, r)
            }
            mgr.updateAppWidget(id, v)
        }
    }

    private fun flags() = PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE

    private fun talkIntent(ctx: Context): PendingIntent = PendingIntent.getActivity(ctx, 200,
        Intent(ctx, WidgetTalkActivity::class.java).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_NO_ANIMATION), flags())

    /** The card screen on this card; tapping counts as opening it, like its notification. */
    private fun cardIntent(ctx: Context, id: String): PendingIntent = PendingIntent.getActivity(ctx, "widget:$id".hashCode(),
        Intent(ctx, AttentionCardActivity::class.java).putExtra(AttentionCardActivity.EXTRA_ID, id)
            .putExtra(AttentionCardActivity.EXTRA_OPENED, true)
            .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_SINGLE_TOP), flags())
}
