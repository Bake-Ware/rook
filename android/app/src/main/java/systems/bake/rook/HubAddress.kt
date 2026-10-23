package systems.bake.rook

import java.net.URI
import okhttp3.HttpUrl.Companion.toHttpUrlOrNull
import org.json.JSONObject

/**
 * SharedPreferences key: the saved "hub" came from the Rook server (pairing code,
 * Google sign-in), so the worker dials it over the WebSocket bridge whatever the
 * port. Read by the Python worker runtime (rook_android/worker_runtime.py).
 */
const val PREF_HUB_WS = "hub_ws"

/**
 * Which servers the app may reach over plain HTTP / ws://, and how a band's hub
 * address is dialed. Pure JVM (no Android types) so it is unit tested.
 *
 * Plain HTTP is allowed only to a host on the local network, the same rule the
 * desktop worker's enrollment uses (rook/worker/enroll.py): a LAN hub from the
 * one-command installer serves http://<lan-ip>:7005, and its pairing code is
 * fetched over that same connection. Everything else stays HTTPS-only.
 *
 * Android's network security config can't list IP ranges, so the manifest
 * permits cleartext and every HTTP client in the app checks its own URL here
 * (or only builds https:// URLs).
 */
object HubAddress {
    /** Hostnames that only resolve on a local network (RFC 6762, RFC 8375, ICANN .internal). */
    private val LOCAL_SUFFIXES = listOf(".local", ".lan", ".home.arpa", ".internal")
    private val IPV4 = Regex("""(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})""")

    /**
     * True for a host that is only reachable on a local network: loopback,
     * RFC 1918, link-local, CGNAT / tailnet (100.64/10), IPv6 loopback, ULA
     * (fc00::/7) and link-local (fe80::/10), `localhost`, and names under
     * .local / .lan / .home.arpa / .internal. Never does a DNS lookup.
     */
    fun isLocalHost(rawHost: String?): Boolean {
        val host = rawHost?.trim()?.trimEnd('.')?.lowercase() ?: return false
        if (host.isEmpty()) return false
        if (host.startsWith("[") || host.contains(':')) return isLocalIpv6(host.removePrefix("[").removeSuffix("]"))
        IPV4.matchEntire(host)?.let { m ->
            val o = m.groupValues.drop(1).map { it.toInt() }
            if (o.any { it > 255 }) return false
            return isLocalIpv4(o)
        }
        if (host.all { it.isDigit() || it == '.' }) return false   // "10.1", "167772161": odd IP forms, not names
        return host == "localhost" || host.endsWith(".localhost") || LOCAL_SUFFIXES.any { host.endsWith(it) }
    }

    private fun isLocalIpv4(o: List<Int>): Boolean = when {
        o[0] == 10 || o[0] == 127 -> true
        o[0] == 172 && o[1] in 16..31 -> true
        o[0] == 192 && o[1] == 168 -> true
        o[0] == 169 && o[1] == 254 -> true
        o[0] == 100 && o[1] in 64..127 -> true          // CGNAT / tailnet (Tailscale)
        else -> false
    }

    private fun isLocalIpv6(text: String): Boolean {
        val g = parseIpv6(text.substringBefore('%')) ?: return false    // drop a zone id (fe80::1%wlan0)
        if (g.subList(0, 5).all { it == 0 } && g[5] == 0xffff)           // ::ffff:a.b.c.d (IPv4-mapped)
            return isLocalIpv4(listOf(g[6] shr 8, g[6] and 0xff, g[7] shr 8, g[7] and 0xff))
        return g.subList(0, 7).all { it == 0 } && g[7] == 1 ||          // ::1
            (g[0] and 0xfe00) == 0xfc00 ||                                // fc00::/7 (unique local)
            (g[0] and 0xffc0) == 0xfe80                                   // fe80::/10 (link-local)
    }

    /** The eight 16-bit groups of an IPv6 literal, or null. Parsed here, never resolved. */
    private fun parseIpv6(text: String): List<Int>? {
        if (text.isEmpty() || text.count { it == ':' } < 2) return null
        val halves = text.split("::")
        if (halves.size > 2) return null
        fun groups(part: String): List<Int>? {
            if (part.isEmpty()) return emptyList()
            val out = mutableListOf<Int>()
            val pieces = part.split(':')
            for ((i, p) in pieces.withIndex()) {
                if (i == pieces.lastIndex && p.contains('.')) {           // trailing dotted IPv4
                    val m = IPV4.matchEntire(p) ?: return null
                    val o = m.groupValues.drop(1).map { it.toInt() }
                    if (o.any { it > 255 }) return null
                    out += (o[0] shl 8) or o[1]; out += (o[2] shl 8) or o[3]
                } else {
                    if (p.isEmpty() || p.length > 4 || !p.all { it in '0'..'9' || it in 'a'..'f' }) return null
                    out += p.toIntOrNull(16) ?: return null
                }
            }
            return out
        }
        val head = groups(halves[0]) ?: return null
        if (halves.size == 1) return head.takeIf { it.size == 8 }
        val tail = groups(halves[1]) ?: return null
        if (head.size + tail.size > 7) return null
        return head + List(8 - head.size - tail.size) { 0 } + tail
    }

    /**
     * The origin of a Rook server the user typed (no trailing slash). HTTPS, or
     * http:// to a local-network host; no userinfo, path, query or fragment.
     */
    fun serverOrigin(server: String): String {
        val text = server.trim()
        val uri = try { URI(text) } catch (_: Exception) { null }
        val scheme = uri?.scheme?.lowercase()
        val shapeOk = uri != null && !uri.host.isNullOrBlank() && uri.userInfo == null &&
            uri.query == null && uri.fragment == null && (uri.path.isNullOrEmpty() || uri.path == "/")
        require(shapeOk && (scheme == "https" || scheme == "http")) { "Enter the address of your Rook server, like https://rook.example.com." }
        require(scheme == "https" || isLocalHost(uri!!.host) && text.toHttpUrlOrNull() != null && cleartextAllowed(text)) {
            "Use https:// for this server. Plain http:// works only for a server on your local network."
        }
        return text.trimEnd('/')
    }

    /**
     * False when [url] is cleartext (http:// or ws://) to a host off the local
     * network. Parsed the way OkHttp dials it, so the check sees the host the
     * request really goes to; a URL OkHttp can't parse is left for it to reject.
     */
    fun cleartextAllowed(url: String): Boolean {
        val text = url.trim()
        val mapped = when {
            text.regionMatches(0, "ws:", 0, 3, ignoreCase = true) -> "http:" + text.substring(3)
            text.regionMatches(0, "wss:", 0, 4, ignoreCase = true) -> "https:" + text.substring(4)
            else -> text
        }
        val parsed = mapped.toHttpUrlOrNull() ?: return true
        return parsed.scheme == "https" || isLocalHost(parsed.host)
    }

    /**
     * Whether a band from the Rook server (pairing code or Google sign-in) is
     * dialed over the hub's WebSocket bridge. The server's band "hub" is the
     * address of its /band bridge (the desktop installer joins it with --ws),
     * whatever the port, so yes unless the server says "transport": "udp".
     */
    fun bandUsesWebSocket(band: JSONObject): Boolean = band.optString("transport", "ws") != "udp"
}
