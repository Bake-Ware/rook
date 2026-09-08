package systems.bake.rook

import android.app.Activity
import android.content.MutableContextWrapper
import androidx.credentials.CredentialManager
import androidx.credentials.CustomCredential
import androidx.credentials.GetCredentialRequest
import com.google.android.libraries.identity.googleid.GetSignInWithGoogleOption
import com.google.android.libraries.identity.googleid.GoogleIdTokenCredential
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONArray
import org.json.JSONObject
import java.net.URI
import java.util.concurrent.TimeUnit

/** Google proves account ownership; only the Rook backend decides band access. */
object GoogleEnrollment {
    private val http = OkHttpClient.Builder().callTimeout(20, TimeUnit.SECONDS)
        .followRedirects(false).followSslRedirects(false).build()

    private fun origin(server: String): String {
        val uri = URI(server.trim())
        require(uri.scheme == "https" && !uri.host.isNullOrBlank() && uri.userInfo == null &&
            uri.query == null && uri.fragment == null && (uri.path.isNullOrEmpty() || uri.path == "/")) {
            "Enter the HTTPS address of your Rook server."
        }
        return server.trim().trimEnd('/')
    }

    private suspend fun post(server: String, path: String, data: JSONObject): JSONObject =
        withContext(Dispatchers.IO) {
            val request = Request.Builder().url(origin(server) + path)
                .post(data.toString().toRequestBody("application/json".toMediaType())).build()
            http.newCall(request).execute().use { response ->
                require(response.isSuccessful) { "Rook could not complete enrollment (${response.code})." }
                val body = response.body ?: error("Empty enrollment response.")
                require(body.contentLength() <= 2 * 1024 * 1024) { "Enrollment response too large." }
                val source = body.source()
                require(!source.request(2L * 1024 * 1024 + 1)) { "Enrollment response too large." }
                JSONObject(source.readUtf8())
            }
        }

    suspend fun signIn(activity: Activity, server: String): JSONArray {
        val challenge = post(server, "/auth/google/challenge", JSONObject())
        require(challenge.getString("client_id") == BuildConfig.GOOGLE_WEB_CLIENT_ID) {
            "This server uses a different Google client. Use its pairing code instead."
        }
        val option = GetSignInWithGoogleOption.Builder(BuildConfig.GOOGLE_WEB_CLIENT_ID)
            .setNonce(challenge.getString("nonce")).build()
        val request = GetCredentialRequest.Builder().addCredentialOption(option).build()
        val credential = CredentialManager.create(activity).getCredential(
            context = MutableContextWrapper(activity), request = request).credential
        require(credential is CustomCredential &&
            credential.type == GoogleIdTokenCredential.TYPE_GOOGLE_ID_TOKEN_CREDENTIAL) {
            "Google did not return a supported sign-in credential."
        }
        val token = GoogleIdTokenCredential.createFrom(credential.data).idToken
        val result = post(server, "/auth/google/native", JSONObject()
            .put("challenge", challenge.getString("challenge")).put("id_token", token))
        return result.getJSONArray("bands")
    }

    suspend fun pair(server: String, code: String): JSONArray {
        val clean = code.trim().lowercase()
        require(clean.matches(Regex("[a-z0-9]{6}"))) { "Enter the six-character pairing code." }
        val band = post(server, "/enroll", JSONObject().put("code", clean))
        band.put("id", band.getString("band_id"))
        return JSONArray().put(band)
    }
}
