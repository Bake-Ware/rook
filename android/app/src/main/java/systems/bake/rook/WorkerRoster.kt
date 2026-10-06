package systems.bake.rook

import org.json.JSONArray
import org.json.JSONObject

/**
 * The workers this phone can show, parsed from the embedded worker's
 * `rook_android.roster.snapshot()`: the band announces it hears itself (its
 * own band) plus, when the phone runs as an enrolled device of an account, the
 * hub's rosters of every band that account can see (fetched with the worker's
 * 30 s device-config refresh, not by the app).
 */
data class RosterWorker(
    val id: String, val name: String, val description: String, val caps: Int,
    val version: String, val build: Int?, val appVersion: String?, val appPlatform: String?,
    val batteryPercent: Int?, val charging: Boolean?, val ageSecs: Double, val self: Boolean,
    val band: String = "",
) {
    /** One missed 30 s announce still counts as online (matches the hub's roster view). */
    val online get() = ageSecs <= WorkerRoster.ONLINE_SECS
    val buildLabel get() = listOfNotNull(build?.let { "build $it" } ?: version.takeIf { it.isNotEmpty() },
        appVersion?.let { "app $it" }).joinToString(" · ")
    val batteryLabel get() = batteryPercent?.let { "$it%" + if (charging == true) " ⚡" else "" }
}

/** One band's workers; [current] is the band this phone's worker is on. */
data class RosterBand(val id: String, val name: String, val current: Boolean, val workers: List<RosterWorker>) {
    val onlineCount get() = workers.count { it.online }
}

/** Where the list came from, and so whether other bands could be shown. */
enum class RosterSource {
    /** Hub rosters for every band of the account that enrolled this phone. */
    ACCOUNT,
    /** Hub roster of this band only: a pairing code or band migration vouches for one band. */
    HUB_BAND_ONLY,
    /** Heard locally; the phone joined with a band key, not as an enrolled device. */
    LOCAL_NOT_ENROLLED,
    /** Heard locally; the phone is enrolled but the hub's rosters aren't available. */
    LOCAL_HUB_UNAVAILABLE,
    /** Heard locally; the hub answers the phone's config refresh but has no roster feature. */
    LOCAL_HUB_UNSUPPORTED,
}

data class WorkerRoster(
    val running: Boolean,
    /** Every band's workers in display order (also the current band's, flat). */
    val workers: List<RosterWorker>,
    val bands: List<RosterBand> = emptyList(),
    val source: RosterSource = RosterSource.LOCAL_NOT_ENROLLED,
) {
    val onlineCount get() = workers.count { it.online }

    /** Why only this band is listed, or null when the account's bands are all here. */
    val notice: String? get() = when (source) {
        RosterSource.ACCOUNT -> null
        RosterSource.HUB_BAND_ONLY -> "Only this band is shown: this phone was paired with a code or moved by a band migration, which doesn't open your other bands. Enroll it from your account to see them."
        RosterSource.LOCAL_NOT_ENROLLED -> "Only this band is shown: this phone joined with a band key, not as an enrolled device of your account."
        RosterSource.LOCAL_HUB_UNAVAILABLE -> "Only this band is shown: your other bands couldn't be loaded from the hub just now."
        RosterSource.LOCAL_HUB_UNSUPPORTED -> "Only this band is shown: your hub doesn't provide other bands."
    }

    companion object {
        const val ONLINE_SECS = 65.0

        private fun parseWorkers(list: JSONArray?, selfId: String, band: String): List<RosterWorker> {
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
                    self = w.optBoolean("self", false) || (selfId.isNotEmpty() && id == selfId),
                    band = band,
                )
            }
            return out
        }

        /** Local and hub rows of the same band: one per worker, the freshest sighting. */
        fun merge(local: List<RosterWorker>, hub: List<RosterWorker>): List<RosterWorker> {
            val byId = LinkedHashMap<String, RosterWorker>()
            for (w in hub + local) {
                val seen = byId[w.id]
                byId[w.id] = when {
                    seen == null -> w
                    w.ageSecs < seen.ageSecs -> w.copy(self = w.self || seen.self)
                    else -> seen.copy(self = w.self || seen.self)
                }
            }
            return byId.values.toList()
        }

        fun parse(json: String): WorkerRoster {
            val root = try { JSONObject(json) } catch (_: Exception) { return WorkerRoster(false, emptyList()) }
            val running = root.optBoolean("running", false)
            val selfId = root.optString("self_id")
            val identity = root.optBoolean("identity", false)
            val hub = if (identity) root.optJSONObject("hub") else null
            val unsupported = identity && root.optBoolean("hub_unsupported", false)
            val currentId = root.optString("band_id")
            val currentName = root.optString("band_name").ifEmpty { "This band" }
            if (!running) return WorkerRoster(false, emptyList(), emptyList(),
                if (identity) RosterSource.LOCAL_HUB_UNAVAILABLE else RosterSource.LOCAL_NOT_ENROLLED)
            val local = parseWorkers(root.optJSONArray("workers"), selfId, currentName)

            val bands = mutableListOf<RosterBand>()
            val hubBands = hub?.optJSONArray("bands")
            for (i in 0 until (hubBands?.length() ?: 0)) {
                val b = hubBands!!.optJSONObject(i) ?: continue
                val id = b.optString("id").takeIf { it.isNotEmpty() } ?: continue
                val name = b.optString("name").ifEmpty { id.take(8) }
                val current = b.optBoolean("current", false) || (currentId.isNotEmpty() && id == currentId)
                bands += RosterBand(id, name, current, parseWorkers(b.optJSONArray("workers"), selfId, name))
            }
            val source = when {
                !identity -> RosterSource.LOCAL_NOT_ENROLLED
                (hub == null || bands.isEmpty()) && unsupported -> RosterSource.LOCAL_HUB_UNSUPPORTED
                hub == null || bands.isEmpty() -> RosterSource.LOCAL_HUB_UNAVAILABLE
                hub.optString("scope") == "account" -> RosterSource.ACCOUNT
                else -> RosterSource.HUB_BAND_ONLY
            }
            val currentIndex = bands.indexOfFirst { it.current }
            if (currentIndex >= 0) {
                val c = bands[currentIndex]
                bands[currentIndex] = c.copy(workers = merge(local.map { it.copy(band = c.name) }, c.workers))
            } else {
                // No hub copy of our own band (or none at all): it is what we hear.
                bands.add(0, RosterBand(currentId, currentName, true, local))
            }
            val ordered = bands.sortedWith(compareBy<RosterBand>({ !it.current }, { it.name.lowercase() }, { it.id }))
                .map { it.copy(workers = sort(it.workers)) }
            return WorkerRoster(true, ordered.flatMap { it.workers }, ordered, source)
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
