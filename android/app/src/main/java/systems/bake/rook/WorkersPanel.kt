package systems.bake.rook

import android.content.Context
import android.graphics.Color
import android.graphics.Typeface
import android.os.Handler
import android.os.Looper
import android.util.Log
import android.view.View
import android.widget.LinearLayout
import android.widget.TextView
import androidx.lifecycle.LifecycleCoroutineScope
import com.google.android.material.bottomsheet.BottomSheetDialog
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import systems.bake.rook.databinding.ActivityMainBinding
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

/**
 * The main screen's Workers tab, grouped by band (this phone's band first): the
 * workers its own band worker hears (`rook_android.roster`, fed by band
 * announces) plus, for a phone enrolled through an account, the hub's rosters
 * of that account's other bands (fetched by the worker's device-config
 * refresh; the app makes no hub call and holds no credentials). Refreshes on
 * pull and every [REFRESH_MS] while visible.
 */
class WorkersPanel(private val ctx: Context, private val b: ActivityMainBinding, private val scope: LifecycleCoroutineScope) {
    private val main = Handler(Looper.getMainLooper())
    private val periodic = object : Runnable { override fun run() { refresh(); main.postDelayed(this, REFRESH_MS) } }
    private var visible = false
    private var resumed = false
    private var loading: Job? = null
    private var roster: WorkerRoster? = null
    private var updatedAt = 0L
    private var error: String? = null
    private val green = Color.rgb(164, 188, 146)
    private fun dp(n: Int) = (n * ctx.resources.displayMetrics.density).toInt()
    private fun color(id: Int) = ctx.getColor(id)

    init {
        b.workersRefresh.setColorSchemeColors(color(R.color.rook_accent))
        b.workersRefresh.setProgressBackgroundColorSchemeColor(color(R.color.rook_panel))
        b.workersRefresh.setOnRefreshListener { refresh() }
        render()
    }

    fun shown(isVisible: Boolean) { visible = isVisible; schedule() }
    fun resume() { resumed = true; schedule() }
    fun pause() { resumed = false; schedule() }

    private fun schedule() {
        main.removeCallbacks(periodic)
        if (visible && resumed) main.post(periodic)
        else { loading?.cancel(); b.workersRefresh.isRefreshing = false }
    }

    private fun refresh() {
        if (loading?.isActive == true) return
        loading = scope.launch {
            try {
                val json = withContext(Dispatchers.IO) {
                    PythonHost.ensureStarted(ctx).getModule("rook_android.roster").callAttr("snapshot").toString()
                }
                roster = WorkerRoster.parse(json); error = null; updatedAt = System.currentTimeMillis()
            } catch (cancelled: CancellationException) { throw cancelled
            } catch (t: Throwable) {
                Log.w("RookWorkers", "roster snapshot failed", t)
                error = t.message ?: t.javaClass.simpleName
            } finally {
                b.workersRefresh.isRefreshing = false
                render()
            }
        }
    }

    private fun text(parent: LinearLayout, value: String, color: Int = color(R.color.rook_fg), size: Float = 13f, mono: Boolean = false) =
        TextView(ctx).apply {
            text = value; textSize = size; setTextColor(color); setPadding(0, dp(2), 0, dp(2))
            if (mono) typeface = Typeface.MONOSPACE
            parent.addView(this)
        }

    private fun card(parent: LinearLayout): LinearLayout = LinearLayout(ctx).apply {
        orientation = LinearLayout.VERTICAL; setPadding(dp(12), dp(10), dp(12), dp(10)); setBackgroundResource(R.drawable.rook_panel)
        parent.addView(this, LinearLayout.LayoutParams(-1, -2).apply { topMargin = dp(8) })
    }

    private fun render() {
        val list = b.workersList
        list.removeAllViews()
        val r = roster
        val updated = if (updatedAt == 0L) "" else " · updated " + SimpleDateFormat("HH:mm:ss", Locale.getDefault()).format(Date(updatedAt))
        when {
            error != null -> text(card(list), "Couldn't read the band roster: $error", Color.rgb(239, 120, 120))
            r == null -> text(card(list), "Loading workers…", color(R.color.rook_dim))
            !r.running -> text(card(list), "The band worker isn't running on this phone. Start it in Settings → Band to see the band's workers.", color(R.color.rook_dim))
            else -> {
                val bandCount = if (r.bands.size > 1) " · ${r.bands.size} bands" else ""
                text(list, "${r.onlineCount} online · ${r.workers.size} seen$bandCount$updated", color(R.color.rook_dim), 12f, mono = true)
                    .setPadding(dp(4), dp(10), 0, dp(2))
                r.notice?.let { text(card(list), it, color(R.color.rook_dim), 12f) }
                for (band in r.bands) {
                    if (r.bands.size > 1 || r.source == RosterSource.ACCOUNT) bandHeader(list, band)
                    for (w in band.workers) row(list, w)
                    if (band.current && band.workers.size <= 1) text(card(list),
                        "Listening for other workers. Each one announces about every 30 seconds; pull down to refresh.", color(R.color.rook_dim), 12f)
                    if (!band.current && band.workers.isEmpty()) text(card(list), "No workers on this band right now.", color(R.color.rook_dim), 12f)
                }
            }
        }
    }

    private fun bandHeader(list: LinearLayout, band: RosterBand) {
        val label = band.name + (if (band.current) "  · this phone's band" else "") + "   ${band.onlineCount}/${band.workers.size} online"
        text(list, label, color(R.color.rook_accent), 13f).apply {
            setPadding(dp(4), dp(16), 0, dp(0))
            typeface = Typeface.DEFAULT_BOLD
            contentDescription = "Band ${band.name}" + (if (band.current) ", this phone's band" else "") +
                ", ${band.onlineCount} of ${band.workers.size} online"
        }
    }

    private fun row(list: LinearLayout, w: RosterWorker) {
        val c = card(list)
        c.isClickable = true; c.isFocusable = true
        val head = LinearLayout(ctx).apply { orientation = LinearLayout.HORIZONTAL }
        head.addView(TextView(ctx).apply {
            text = "● "; textSize = 13f; setTextColor(if (w.online) green else color(R.color.rook_dim))
        })
        head.addView(TextView(ctx).apply {
            text = w.name + if (w.self) "  (this phone)" else ""; textSize = 15f; setTextColor(color(R.color.rook_fg))
            maxLines = 1; ellipsize = android.text.TextUtils.TruncateAt.END
        }, LinearLayout.LayoutParams(0, -2, 1f))
        w.batteryLabel?.let { battery ->
            head.addView(TextView(ctx).apply { text = battery; textSize = 12f; setTextColor(color(R.color.rook_dim)); typeface = Typeface.MONOSPACE })
        }
        c.addView(head)
        val line = listOf(WorkerRoster.presence(w), w.buildLabel).filter { it.isNotEmpty() }.joinToString(" · ")
        text(c, line, if (w.online) color(R.color.rook_dim) else color(R.color.rook_accent), 12f)
        c.contentDescription = listOfNotNull(w.name, if (w.self) "this phone" else null, WorkerRoster.presence(w), w.buildLabel.ifEmpty { null },
            w.batteryLabel?.let { "battery $it" }).joinToString(", ") + ". Tap for details"
        c.setOnClickListener { details(w) }
    }

    private fun details(w: RosterWorker) {
        val sheet = BottomSheetDialog(ctx)
        val body = LinearLayout(ctx).apply { orientation = LinearLayout.VERTICAL; setPadding(dp(20), dp(16), dp(20), dp(28)); setBackgroundColor(color(R.color.rook_panel)) }
        text(body, w.name, color(R.color.rook_accent), 18f)
        text(body, WorkerRoster.presence(w), if (w.online) green else color(R.color.rook_accent), 12f)
        text(body, w.description.ifEmpty { "No description set." }, if (w.description.isEmpty()) color(R.color.rook_dim) else color(R.color.rook_fg), 14f)
            .setPadding(0, dp(10), 0, dp(10))
        fun field(label: String, value: String?) { if (!value.isNullOrEmpty()) text(body, "$label  $value", color(R.color.rook_dim), 12f, mono = true) }
        field("band   ", w.band)
        field("caps   ", w.caps.toString())
        field("build  ", w.build?.toString())
        field("version", w.version)
        field("app    ", listOfNotNull(w.appPlatform, w.appVersion).joinToString(" ").ifEmpty { null })
        field("battery", w.batteryLabel)
        field("id     ", w.id)
        (body.getChildAt(body.childCount - 1) as? TextView)?.setTextIsSelectable(true)   // the id, for rook_call worker=
        sheet.setContentView(body)
        sheet.show()
    }

    companion object { const val REFRESH_MS = 30_000L }
}
