package systems.bake.rook

import android.app.PendingIntent
import android.appwidget.AppWidgetManager
import android.appwidget.AppWidgetProvider
import android.content.ComponentName
import android.content.Context
import android.content.Intent
import android.content.res.Configuration
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.util.Log
import android.view.View
import android.widget.RemoteViews
import android.widget.RemoteViewsService

/**
 * Home-screen widgets: Talk (push-to-talk to the house agent's voice), Waiting on you (the
 * cards in [AttentionStore], each opening its card screen) and Chat (push-to-talk under the
 * recent conversation, as the app's Chat tab shows it). All read only what is on the phone,
 * so they work offline; the store calls [refresh], [VoiceService] calls [voiceState] and
 * [VoiceBus] calls [conversationChanged] when what they show changes. What they show is decided
 * in [WidgetModel] and [WidgetTranscript].
 */
class TalkWidget : AppWidgetProvider() {
    override fun onUpdate(ctx: Context, mgr: AppWidgetManager, ids: IntArray) = RookWidgets.renderTalk(ctx, mgr, ids, VoiceBus.state)
}

class WaitingWidget : AppWidgetProvider() {
    override fun onUpdate(ctx: Context, mgr: AppWidgetManager, ids: IntArray) = RookWidgets.renderWaiting(ctx, mgr, ids)
    override fun onAppWidgetOptionsChanged(ctx: Context, mgr: AppWidgetManager, id: Int, options: Bundle?) =
        RookWidgets.renderWaiting(ctx, mgr, intArrayOf(id))
}

class ChatWidget : AppWidgetProvider() {
    override fun onUpdate(ctx: Context, mgr: AppWidgetManager, ids: IntArray) = RookWidgets.renderChat(ctx, mgr, ids)
}

/** The Chat widget's bubbles: a list the launcher scrolls, filled from [RookWidgets.transcript]. */
class ChatWidgetService : RemoteViewsService() {
    override fun onGetViewFactory(intent: Intent): RemoteViewsFactory = Bubbles(applicationContext)

    private class Bubbles(private val ctx: Context) : RemoteViewsFactory {
        private var lines = emptyList<WidgetTranscript.Line>()
        override fun onCreate() {}
        override fun onDataSetChanged() { lines = RookWidgets.transcript.snapshot() }
        override fun onDestroy() {}
        override fun getCount() = lines.size
        override fun getViewAt(position: Int): RemoteViews {
            val line = lines.getOrNull(position) ?: return RemoteViews(ctx.packageName, R.layout.widget_chat_agent)
            val v = RemoteViews(ctx.packageName, if (line.user) R.layout.widget_chat_user else R.layout.widget_chat_agent)
            v.setTextViewText(R.id.widget_bubble, line.text)
            // The app fades a reply that was cut off.
            v.setTextColor(R.id.widget_bubble, ctx.getColor(if (line.faded) R.color.rook_dim else R.color.rook_fg))
            v.setOnClickFillInIntent(R.id.widget_bubble, Intent())
            return v
        }
        override fun getLoadingView(): RemoteViews? = null
        override fun getViewTypeCount() = 2
        override fun getItemId(position: Int) = lines.getOrNull(position)?.id ?: position.toLong()
        override fun hasStableIds() = true
    }
}

object RookWidgets {
    private const val TAG = "RookWidgets"
    @Volatile private var lastTalk: WidgetModel.Talk? = null
    /** The Chat widget's conversation, rebuilt from [VoiceBus]'s log; lives as long as the log does. */
    val transcript = WidgetTranscript()
    @Volatile private var app: Context? = null
    private val main by lazy { Handler(Looper.getMainLooper()) }
    @Volatile private var chatPending = false
    /** Reply deltas stream in word by word: redraw the bubbles at most this often. */
    private const val CHAT_REDRAW_MS = 300L
    private val chatRedraw = Runnable {
        chatPending = false
        val ctx = app ?: return@Runnable
        runCatching {
            catchUp()
            val mgr = AppWidgetManager.getInstance(ctx) ?: return@runCatching
            val ids = ids(ctx, mgr, ChatWidget::class.java)
            @Suppress("DEPRECATION")
            if (ids.isNotEmpty()) mgr.notifyAppWidgetViewDataChanged(ids, R.id.widget_chat_list)
        }.onFailure { Log.w(TAG, "chat widget refresh failed", it) }
    }

    private fun ids(ctx: Context, mgr: AppWidgetManager, cls: Class<*>) = mgr.getAppWidgetIds(ComponentName(ctx, cls))

    /**
     * Remembers the app so [VoiceBus] events can redraw the Chat widget (there is no Application
     * class): the worker service and the app's screen call it when the process starts.
     */
    fun bind(ctx: Context) {
        if (app != null) return
        app = ctx.applicationContext
        VoiceBus.onLogged = ::conversationChanged
    }

    /** Main thread: feed the conversation events the transcript has not seen yet. */
    private fun catchUp() = VoiceBus.replay(transcript, transcript.seen)

    /** A conversation event was logged (any thread): redraw the Chat widgets' bubbles shortly. */
    fun conversationChanged() {
        if (chatPending || app == null) return
        chatPending = true
        main.postDelayed(chatRedraw, CHAT_REDRAW_MS)
    }

    /** The cards changed: redraw every Waiting widget. */
    fun refresh(ctx: Context) {
        bind(ctx)
        runCatching {
            val mgr = AppWidgetManager.getInstance(ctx) ?: return
            val ids = ids(ctx, mgr, WaitingWidget::class.java)
            if (ids.isNotEmpty()) renderWaiting(ctx, mgr, ids)
        }.onFailure { Log.w(TAG, "waiting widget refresh failed", it) }
    }

    /** The voice session's state: redraw the mics only when what they show changes. */
    fun voiceState(ctx: Context, state: String) {
        val t = WidgetModel.talk(state)
        bind(ctx)
        if (t == lastTalk) return
        lastTalk = t
        runCatching {
            val mgr = AppWidgetManager.getInstance(ctx) ?: return
            ids(ctx, mgr, TalkWidget::class.java).takeIf { it.isNotEmpty() }?.let { renderTalk(ctx, mgr, it, state) }
            ids(ctx, mgr, WaitingWidget::class.java).takeIf { it.isNotEmpty() }?.let { renderWaiting(ctx, mgr, it, state) }
            ids(ctx, mgr, ChatWidget::class.java).takeIf { it.isNotEmpty() }?.let { renderChat(ctx, mgr, it, state) }
        }.onFailure { Log.w(TAG, "talk widget refresh failed", it) }
    }

    fun renderTalk(ctx: Context, mgr: AppWidgetManager, ids: IntArray, state: String) {
        bind(ctx)
        val t = WidgetModel.talk(state)
        val v = RemoteViews(ctx.packageName, R.layout.widget_talk)
        v.setImageViewResource(R.id.widget_mic, if (t.active) R.drawable.ic_mic_active else R.drawable.ic_mic)
        v.setTextViewText(R.id.widget_talk_label, t.label)
        v.setOnClickPendingIntent(R.id.widget_talk, talkIntent(ctx))
        ids.forEach { mgr.updateAppWidget(it, v) }
    }

    fun renderWaiting(ctx: Context, mgr: AppWidgetManager, ids: IntArray, state: String = VoiceBus.state) {
        bind(ctx)
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

    /**
     * The Chat widget: the agent's name and what it is doing, the recent conversation as bubbles
     * (newest at the bottom, scrollable), and a Talk bar that works like the Talk widget.
     */
    fun renderChat(ctx: Context, mgr: AppWidgetManager, ids: IntArray, state: String = VoiceBus.state) {
        bind(ctx)
        catchUp()
        val t = WidgetModel.talk(state)
        for (id in ids) {
            val v = RemoteViews(ctx.packageName, R.layout.widget_chat)
            v.setTextViewText(R.id.widget_chat_state, if (t.active) t.label else "")
            v.setViewVisibility(R.id.widget_chat_state, if (t.active) View.VISIBLE else View.GONE)
            v.setOnClickPendingIntent(R.id.widget_chat_header, NotificationNavigation.mainActivity(ctx))
            // The list's adapter is this app's ChatWidgetService; the data URI keeps one per widget.
            val adapter = Intent(ctx, ChatWidgetService::class.java).putExtra(AppWidgetManager.EXTRA_APPWIDGET_ID, id)
            adapter.data = Uri.parse(adapter.toUri(Intent.URI_INTENT_SCHEME))
            @Suppress("DEPRECATION")
            v.setRemoteAdapter(R.id.widget_chat_list, adapter)
            v.setEmptyView(R.id.widget_chat_list, R.id.widget_chat_empty)
            v.setPendingIntentTemplate(R.id.widget_chat_list, openAppTemplate(ctx))
            v.setOnClickPendingIntent(R.id.widget_chat_empty, talkIntent(ctx))
            v.setImageViewResource(R.id.widget_chat_mic, if (t.active) R.drawable.ic_mic_active else R.drawable.ic_mic)
            v.setTextViewText(R.id.widget_chat_talk_label, t.label)
            v.setOnClickPendingIntent(R.id.widget_chat_talk, talkIntent(ctx))
            mgr.updateAppWidget(id, v)
        }
        @Suppress("DEPRECATION")
        mgr.notifyAppWidgetViewDataChanged(ids, R.id.widget_chat_list)
    }

    /** A tap on a bubble opens the app. List items fill in a template, which must be mutable on 12+. */
    private fun openAppTemplate(ctx: Context): PendingIntent = PendingIntent.getActivity(ctx, 201,
        Intent(ctx, MainActivity::class.java).addFlags(
            Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_CLEAR_TOP or Intent.FLAG_ACTIVITY_SINGLE_TOP),
        PendingIntent.FLAG_UPDATE_CURRENT or if (Build.VERSION.SDK_INT >= 31) PendingIntent.FLAG_MUTABLE else 0)

    private fun flags() = PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE

    private fun talkIntent(ctx: Context): PendingIntent = PendingIntent.getActivity(ctx, 200,
        Intent(ctx, WidgetTalkActivity::class.java).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_NO_ANIMATION), flags())

    /** The card screen on this card; tapping counts as opening it, like its notification. */
    private fun cardIntent(ctx: Context, id: String): PendingIntent = PendingIntent.getActivity(ctx, "widget:$id".hashCode(),
        Intent(ctx, AttentionCardActivity::class.java).putExtra(AttentionCardActivity.EXTRA_ID, id)
            .putExtra(AttentionCardActivity.EXTRA_OPENED, true)
            .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_SINGLE_TOP), flags())
}
