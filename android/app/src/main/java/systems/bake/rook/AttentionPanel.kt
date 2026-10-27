package systems.bake.rook

import android.Manifest
import android.app.NotificationManager
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.graphics.Color
import android.net.Uri
import android.os.Build
import android.os.PowerManager
import android.provider.Settings
import android.view.View
import android.widget.LinearLayout
import android.widget.TextView
import androidx.appcompat.app.AlertDialog
import androidx.appcompat.app.AppCompatActivity
import androidx.appcompat.widget.SwitchCompat
import androidx.core.app.NotificationManagerCompat
import systems.bake.rook.databinding.ActivityMainBinding
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

/**
 * The main screen's Attention tab: the setup grants (only while one is missing), pending
 * decision cards (tap to open), and the quiet feed of classified notifications (a run of one
 * app's lines folds after two; tapping a line sorts that app: ring me, tell me, just list it, hide it).
 * Until the tab has been opened once, a one-line link above the chat points at missing grants.
 */
class AttentionPanel(private val act: AppCompatActivity, private val b: ActivityMainBinding, private val askNotifications: () -> Unit,
                     private val askMic: () -> Unit = {}) {
    private val prefs = act.getSharedPreferences("rook", Context.MODE_PRIVATE)
    private var shown = false
    /** Called after each render, so the tab badge follows pending cards. */
    var onChanged: (() -> Unit)? = null
    private val refresh = Runnable { render() }
    /** Runs of one app's lines the person unfolded ("+N more"), by their first line's id. */
    private val openRuns = mutableSetOf<String>()
    private val red = Color.rgb(239, 120, 120)
    private val amber = Color.rgb(216, 173, 109)
    private fun dp(n: Int) = (n * act.resources.displayMetrics.density).toInt()

    class Grant(val title: String, val why: String, val granted: Boolean?, val action: () -> Unit)

    private fun settings(action: String, pkg: Boolean = false) {
        try { act.startActivity(if (pkg) Intent(action, Uri.parse("package:${act.packageName}")) else Intent(action)) }
        catch (_: Exception) { act.startActivity(Intent(Settings.ACTION_SETTINGS)) }
    }

    /** Each grant attention needs; granted is null where this Android version doesn't need it. */
    fun grants(): List<Grant> {
        val nm = act.getSystemService(NotificationManager::class.java)
        return listOf(
            Grant("Notification access", "Sort other apps' notifications on the phone",
                NotificationManagerCompat.getEnabledListenerPackages(act).contains(act.packageName)) {
                settings(Settings.ACTION_NOTIFICATION_LISTENER_SETTINGS) },
            Grant("Post notifications", "Show decisions and alerts",
                if (Build.VERSION.SDK_INT >= 33) act.checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) == PackageManager.PERMISSION_GRANTED else null) {
                askNotifications() },
            Grant("Do Not Disturb access", "Urgent alerts ring through Do Not Disturb", nm?.isNotificationPolicyAccessGranted ?: false) {
                settings(Settings.ACTION_NOTIFICATION_POLICY_ACCESS_SETTINGS) },
            Grant("Full-screen alerts", "Urgent alerts take over the screen",
                if (Build.VERSION.SDK_INT >= 34) nm?.canUseFullScreenIntent() ?: false else null) {
                settings(Settings.ACTION_MANAGE_APP_USE_FULL_SCREEN_INTENT, pkg = true) },
            Grant("Display over other apps", "Open a waiting decision when you unlock", Settings.canDrawOverlays(act)) {
                settings(Settings.ACTION_MANAGE_OVERLAY_PERMISSION, pkg = true) },
            Grant("Battery optimisation off", "Keeps alerts arriving in the background",
                (act.getSystemService(Context.POWER_SERVICE) as PowerManager).isIgnoringBatteryOptimizations(act.packageName)) {
                @Suppress("BatteryLife") settings(Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS, pkg = true) },
        )
    }

    fun missing() = try { grants().filter { it.granted == false } } catch (_: Exception) { emptyList() }
    fun pendingCount() = AttentionStore.showing(act).count { it.state == "pending" }

    fun resume() {
        AttentionStore.addListener(refresh)
        // Re-submitting lets a fresh Do Not Disturb grant reach the urgent channel.
        runCatching { AttentionChannels.ensure(act) }
        render()
    }
    fun pause() = AttentionStore.removeListener(refresh)
    fun shown(visible: Boolean) {
        shown = visible
        if (visible) prefs.edit().putBoolean(PREF_SEEN, true).apply()
        render()
    }

    fun render() {
        val missing = missing()
        b.attentionSetup.visibility = if (missing.isNotEmpty() && !prefs.getBoolean(PREF_SEEN, false)) View.VISIBLE else View.GONE
        b.attentionSetup.text = "Attention setup: ${missing.size} to allow ›"
        onChanged?.invoke()
        if (!shown) return
        val list = b.attentionList
        list.removeAllViews()
        if (missing.isNotEmpty()) {
            val c = card(list)
            text(c, "ATTENTION SETUP", amber, 12f)
            missing.forEach { g -> grantRow(c, g) }
        }
        val cards = AttentionStore.showing(act)
        if (cards.isNotEmpty()) {
            val c = card(list)
            cards.forEach { k -> cardRow(c, k) }
        }
        val (feed, ignored) = AttentionStore.recentFeed(act, 100)
        val f = card(list)
        if (feed.isEmpty()) text(f, "Nothing yet.", act.getColor(R.color.rook_dim))
        else text(f, "Tap a line to choose how this phone sorts that app.", act.getColor(R.color.rook_dim), 12f)
        val fmt = SimpleDateFormat("EEE HH:mm", Locale.getDefault())
        AttentionModel.runs(feed.map { it.optString("app") }).forEach { (start, end) ->
            val run = feed.subList(start, end)
            val key = run.first().optString("id")
            val shown = if (key in openRuns) run else run.take(RUN_SHOWN)
            shown.forEach { e ->
                val color = when (e.optString("priority")) { "urgent" -> red; "family" -> amber; else -> act.getColor(R.color.rook_fg) }
                text(f, "${fmt.format(Date((e.optDouble("ts") * 1000).toLong()))}  ${e.optString("summary")}", color).apply {
                    val app = e.optString("app")
                    if (app.isNotBlank()) {
                        isClickable = true; isFocusable = true
                        contentDescription = "${e.optString("summary")}. Tap to sort $app"
                        setOnClickListener { sortDialog(app, e.optString("pkg")) }
                    }
                }
            }
            if (run.size > shown.size) text(f, "+${run.size - shown.size} more from ${run.first().optString("app")} ›",
                act.getColor(R.color.rook_dim), 12f).apply {
                isClickable = true; isFocusable = true
                setOnClickListener { openRuns.add(key); render() }
            }
        }
        if (ignored > 0) text(f, "$ignored hidden this week", act.getColor(R.color.rook_dim), 12f)
        spokenReminders(list)
    }

    /** The "Spoken reminders" switch (attention.remind) and the microphone grant its quiet check needs. */
    private fun spokenReminders(list: LinearLayout) {
        val c = card(list)
        val on = SpokenReminder.enabled(act)
        c.addView(SwitchCompat(act).apply {
            text = "Spoken reminders"; textSize = 15f; setTextColor(act.getColor(R.color.rook_fg))
            isChecked = on
            setOnCheckedChangeListener { _, checked -> SpokenReminder.setEnabled(act, checked); render() }
        })
        text(c, "Before it speaks a reminder, the app listens for a few seconds to check nobody is talking. " +
            "The sound stays on the phone: nothing is recorded, kept or sent.", act.getColor(R.color.rook_dim), 12f)
        if (!on) return
        if (SpokenReminder.micGranted(act)) text(c, "Microphone: allowed", act.getColor(R.color.rook_dim), 12f)
        else grantRow(c, Grant("Microphone", "Needed to check the room is quiet", false) { askMic() })
    }

    /** Ring me / Tell me / Just list it / Hide it for one app: applies here at once and the hub keeps it. */
    private fun sortDialog(app: String, pkg: String) {
        val now = AttentionStore.choice(act, app, pkg)
        val keys = AttentionRules.CHOICES.keys.toList() + listOfNotNull(if (now != null) "default" else null)
        val labels = keys.map { k -> (AttentionRules.CHOICES[k] ?: "The phone's own rules") + if (k == now) "  ✓" else "" }
        AlertDialog.Builder(act).setTitle("Notifications from $app")
            .setItems(labels.toTypedArray()) { _, i -> AttentionStore.sort(act, app, pkg, keys[i]); render() }
            .setNegativeButton("Cancel", null).show()
    }

    private fun grantRow(parent: LinearLayout, g: Grant) {
        val row = LinearLayout(act).apply {
            orientation = LinearLayout.VERTICAL; setPadding(0, dp(6), 0, dp(6)); isClickable = true; isFocusable = true
            contentDescription = "${g.title}: not allowed. ${g.why}. Tap to allow"
            setOnClickListener { g.action() }
        }
        row.addView(TextView(act).apply { text = "${g.title} ›"; textSize = 15f; setTextColor(act.getColor(R.color.rook_fg)) })
        row.addView(TextView(act).apply { text = g.why; textSize = 12f; setTextColor(act.getColor(R.color.rook_dim)) })
        parent.addView(row)
    }

    private fun cardRow(parent: LinearLayout, k: AttentionCard) {
        val row = LinearLayout(act).apply {
            orientation = LinearLayout.VERTICAL; setPadding(0, dp(6), 0, dp(6)); isClickable = true; isFocusable = true
            setOnClickListener {
                act.startActivity(Intent(act, AttentionCardActivity::class.java).putExtra(AttentionCardActivity.EXTRA_ID, k.id)
                    .putExtra(AttentionCardActivity.EXTRA_OPENED, true))
            }
        }
        row.addView(TextView(act).apply { text = k.line; textSize = 15f; setTextColor(if (k.urgent) red else act.getColor(R.color.rook_fg)) })
        val state = when (k.state) { "sent" -> "sent…"; "failed" -> "didn’t go through"; else -> "" }
        row.addView(TextView(act).apply {
            text = listOf(k.source.ifBlank { "Rook" }, state).filter { it.isNotEmpty() }.joinToString(" · ")
            textSize = 12f; setTextColor(act.getColor(R.color.rook_dim))
        })
        parent.addView(row)
    }

    private fun text(parent: LinearLayout, value: String, color: Int, size: Float = 13f): TextView =
        TextView(act).apply { text = value; textSize = size; setTextColor(color); setPadding(0, dp(3), 0, dp(3)) }
            .also { parent.addView(it) }
    private fun card(parent: LinearLayout): LinearLayout = LinearLayout(act).apply {
        orientation = LinearLayout.VERTICAL; setPadding(dp(12), dp(8), dp(12), dp(8)); setBackgroundResource(R.drawable.rook_panel)
        parent.addView(this, LinearLayout.LayoutParams(-1, -2).apply { topMargin = dp(8) })
    }

    companion object {
        const val PREF_SEEN = "attention_tab_seen"
        /** A run of one app's lines shows this many before "+N more". */
        const val RUN_SHOWN = 2
    }
}
