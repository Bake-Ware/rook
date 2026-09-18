package systems.bake.rook

import okhttp3.HttpUrl.Companion.toHttpUrl
import okhttp3.OkHttpClient
import okhttp3.Request
import org.json.JSONObject
import java.util.Locale
import java.util.concurrent.TimeUnit

/** Voice IDs stay opaque on the wire; known Kokoro prefixes are only display labels. */
data class VoiceCatalog(val voices: List<String>, val default: String) {
    fun choices(saved: String?): List<String> = (listOf(saved ?: default) + voices).distinct()
    companion object {
        const val FALLBACK = "af_heart"
        fun parse(json: JSONObject): VoiceCatalog {
            val array = json.getJSONArray("voices")
            val ids = (0 until array.length()).mapNotNull { (array.opt(it) as? String)?.takeIf(String::isNotBlank) }.distinct()
            require(ids.isNotEmpty()) { "No voices available" }
            val default = (json.opt("default") as? String)?.takeIf { it in ids } ?: ids.first()
            return VoiceCatalog(ids, default)
        }
        fun label(id: String): String {
            val languages = mapOf('a' to "US", 'b' to "UK", 'e' to "Spanish", 'f' to "French", 'h' to "Hindi",
                'i' to "Italian", 'j' to "Japanese", 'p' to "Brazilian Portuguese", 'z' to "Mandarin")
            if (id.length < 4 || id[2] != '_') return id
            val language = languages[id[0]] ?: return id
            val gender = when (id[1]) { 'f' -> "female"; 'm' -> "male"; else -> return id }
            val name = id.substring(3).split('_').joinToString(" ") { it.replaceFirstChar { c -> c.titlecase(Locale.ROOT) } }
            return "$name ($language $gender)"
        }
        fun endpoint(voiceUrl: String): String {
            val https = when {
                voiceUrl.startsWith("wss://") -> "https://" + voiceUrl.removePrefix("wss://")
                voiceUrl.startsWith("ws://") -> "https://" + voiceUrl.removePrefix("ws://")
                else -> voiceUrl
            }.toHttpUrl()
            require(https.scheme == "https" && https.username.isEmpty() && https.password.isEmpty()) { "Invalid voice URL" }
            return https.newBuilder().encodedPath("/voices").query(null).fragment(null).build().toString()
        }
        fun fetch(url: String, token: String, insecure: Boolean): VoiceCatalog {
            val builder = OkHttpClient.Builder().callTimeout(15, TimeUnit.SECONDS).followRedirects(false)
            if (insecure) VoiceTls.trustAll(builder)
            val http = builder.build()
            try {
                val request = Request.Builder().url(endpoint(url)).apply {
                    if (token.isNotEmpty()) header("Authorization", "Bearer $token")
                }.build()
                return http.newCall(request).execute().use { response ->
                    check(response.isSuccessful) { "Voice list unavailable (${response.code})" }
                    parse(JSONObject(response.body?.string() ?: ""))
                }
            } finally { http.dispatcher.executorService.shutdown(); http.connectionPool.evictAll() }
        }
    }
}
