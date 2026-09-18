package systems.bake.rook

import android.content.res.ColorStateList
import android.graphics.Color
import android.widget.LinearLayout
import android.widget.ProgressBar
import android.widget.TextView
import kotlin.math.roundToInt

/** Shared card and inline rendering keeps engine status and answer presentation consistent. */
object DecisionContent {
    fun fill(parent: LinearLayout, d: Decision) {
        val ctx = parent.context
        val accent = ctx.getColor(R.color.rook_accent)
        fun text(value: String, size: Float = 12f, color: Int = ctx.getColor(R.color.rook_fg)) {
            parent.addView(TextView(ctx).apply { this.text = value; textSize = size; setTextColor(color); setPadding(0, 6, 0, 6) })
        }
        text(d.engineLabel, 16f, if (d.failed) Color.rgb(239,120,120) else accent)
        parent.addView(TextView(ctx).apply {
            text = d.mode.ifBlank { "shadow" }; textSize = 10f; setTextColor(accent)
            setPadding(12, 4, 12, 4); setBackgroundResource(R.drawable.bubble_user)
        }, LinearLayout.LayoutParams(-2, -2))
        d.error?.let { text(it) }
        text("${d.latencyMs?.let { "$it ms" } ?: "Elapsed: —"} · model: ${d.model ?: "—"} · adapter: ${d.adapter ?: "—"}")
        if (d.engineStatus == "ok") for (a in d.answers) {
            val value = a.choice ?: a.p?.let { "${if (it >= 0.5) "yes" else "no"} (p=${(it * 100).roundToInt()}%)" }
                ?: listOfNotNull(a.level?.let { "level $it" }, a.expected?.let { "expected $it" }).joinToString(" · ")
            text("${a.id}: $value", 13f)
            text("Confidence: ${a.confidence?.let { "${(it * 100).roundToInt()}%" } ?: "not supplied"}")
            parent.addView(ProgressBar(ctx, null, android.R.attr.progressBarStyleHorizontal).apply {
                max = 100; progress = ((a.confidence ?: 0.0) * 100).roundToInt(); progressTintList = ColorStateList.valueOf(accent)
                contentDescription = "${a.id} confidence ${a.confidence?.let { "${(it * 100).roundToInt()} percent" } ?: "not supplied"}"
            }, LinearLayout.LayoutParams(-1, (6 * ctx.resources.displayMetrics.density).toInt()))
            if (a.probabilities.isNotEmpty()) text(a.probabilities.entries.joinToString(" · ") { "${it.key}: ${(it.value * 100).roundToInt()}%" })
        }
    }
}
