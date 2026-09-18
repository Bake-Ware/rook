package systems.bake.rook

import okhttp3.OkHttpClient
import java.security.SecureRandom
import java.security.cert.X509Certificate
import javax.net.ssl.*

/** Explicit user-selected development TLS override shared by HTTP and WebSocket. */
object VoiceTls {
    fun trustAll(b: OkHttpClient.Builder) {
        val tm = object : X509TrustManager {
            override fun checkClientTrusted(c: Array<X509Certificate>, a: String) {}
            override fun checkServerTrusted(c: Array<X509Certificate>, a: String) {}
            override fun getAcceptedIssuers() = arrayOf<X509Certificate>()
        }
        val ssl=SSLContext.getInstance("TLS"); ssl.init(null,arrayOf<TrustManager>(tm),SecureRandom())
        b.sslSocketFactory(ssl.socketFactory,tm).hostnameVerifier { _,_ -> true }
    }
}
