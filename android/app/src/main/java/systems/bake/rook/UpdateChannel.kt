package systems.bake.rook

/**
 * APK update channel. "stable" (the default for everyone) or "beta"; a beta phone asks the
 * hub with ?channel=beta and the hub answers with an approved newer beta, else the stable build,
 * so the updater's checks are the same either way.
 */
object UpdateChannel {
    const val PREF = "apk_channel"
    const val STABLE = "stable"
    const val BETA = "beta"

    /** A known channel name, or null. */
    fun normalize(value: String?): String? = when (value?.trim()?.lowercase()) { STABLE -> STABLE; BETA -> BETA; else -> null }

    /** Feed URL for [path] ("apk.json" or "apk") on [channel]; stable keeps the plain URL. */
    fun url(origin: String, path: String, channel: String?): String =
        "${origin.trimEnd('/')}/$path" + if (normalize(channel) == BETA) "?channel=beta" else ""
}
