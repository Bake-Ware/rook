package systems.bake.rook

import org.json.JSONObject

/**
 * The band's workers as this phone hears them: parsed from the embedded
 * worker's `rook_android.roster.snapshot()` (band announces, no hub call).
 */
data class RosterWorker(
    val id: String, val name: String, val description: String, val caps: Int,
    val version: String, val build: Int?, val appVersion: String?, val appPlatform: String?,
    val batteryPercent: Int?, val charging: Boolean?, val ageSecs: Double, val self: Boolean,
) {
    /** One missed 30 s announce still counts as online (matches the hub's roster view). */
    val online get() = ageSecs <= WorkerRoster.ONLINE_SECS
    val buildLabel get() = listOfNotNull(build?.let { "build $it" } ?: version.takeIf { it.isNotEmpty() },
        appVersion?.let { "app $it" }).joinToString(" · ")
    val batteryLabel get() = batteryPercent?.let { "$it%" + if (charging == true) " ⚡" else "" }
}

data class WorkerRoster(val running: Boolean, val workers: List<RosterWorker>) {
    val onlineCount get() = workers.count { it.online }

    companion object {
        const val ONLINE_SECS = 65.0

        fun parse(json: String): WorkerRoster {
            val root = try { JSONObject(json) } catch (_: Exception) { return WorkerRoster(false, emptyList()) }
            val list = root.optJSONArray("workers")
            val out = mutableListOf<RosterWorker>()
            for (i in 0 until (list?.length() ?: 0)) {
                val w = list!!.optJSONObject(i) ?: continue
                val id = w.optString("worker_id").takeIf { it.isNotEmpty() } ?: continue
                val app = w.optJSONObject("app_release")
                val battery = w.optJSONObject("hb")?.optJSONObject("battery")
                val pct = (battery?.opt("percent") as? Number)?.toInt()?.takeIf { it in 0..100 }
                out += RosterWorker(
                    id = id,
                    name = w.optString("name").ifEmpty { id.take(12) },
                    description = w.optString("description"),
                    caps = w.optInt("caps", 0).coerceAtLeast(0),
                    version = w.optString("version"),
                    build = (w.opt("build") as? Number)?.toInt(),
                    appVersion = app?.optString("version")?.takeIf { it.isNotEmpty() },
                    appPlatform = app?.optString("platform")?.takeIf { it.isNotEmpty() },
                    batteryPercent = pct,
                    charging = if (battery?.has("charging") == true) battery.optBoolean("charging") else null,
                    ageSecs = w.optDouble("last_seen_age_secs", Double.MAX_VALUE).let { if (it.isNaN() || it < 0) Double.MAX_VALUE else it },
                    self = w.optBoolean("self", false),
                )
            }
            return WorkerRoster(root.optBoolean("running", false), sort(out))
        }

        /** This phone first, then online before offline, then by name (case-insensitive), then id. */
        fun sort(workers: List<RosterWorker>): List<RosterWorker> = workers.sortedWith(
            compareBy<RosterWorker>({ !it.self }, { !it.online }, { it.name.lowercase() }, { it.id }))

        /** "online", or "offline · seen 5m ago". */
        fun presence(w: RosterWorker): String = if (w.online) "online" else "offline · seen ${ago(w.ageSecs)}"

        fun ago(secs: Double): String {
            if (secs == Double.MAX_VALUE) return "never"
            val s = secs.toLong()
            return when {
                s < 60 -> "${s}s ago"
                s < 3600 -> "${s / 60}m ago"
                s < 86400 -> "${s / 3600}h ago"
                else -> "${s / 86400}d ago"
            }
        }
    }
}
