package systems.bake.rook

import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import android.content.pm.PackageInfo
import android.content.pm.PackageInstaller
import android.content.pm.PackageManager
import android.os.Build
import androidx.core.app.NotificationCompat
import okhttp3.OkHttpClient
import okhttp3.Request
import org.json.JSONObject
import java.io.File
import java.security.MessageDigest
import java.util.concurrent.Executors
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicBoolean

/** App-signed, monotonic self updates. The OS remains the authority on consent. */
object ApkUpdater {
    private const val ORIGIN = "https://rook.bakeforge.com"
    private const val INTERVAL = 6 * 60 * 60 * 1000L
    private const val MAX_APK = 256 * 1024 * 1024L
    private const val CHANNEL = "rook_updates"
    private const val NOTIFICATION = 1040
    private val busy = AtomicBoolean(false)
    private val executor = Executors.newSingleThreadExecutor()
    private val http = OkHttpClient.Builder().connectTimeout(20, TimeUnit.SECONDS)
        .readTimeout(45, TimeUnit.SECONDS).callTimeout(5, TimeUnit.MINUTES)
        .followRedirects(false).followSslRedirects(false).build()
    private fun prefs(ctx: Context) = ctx.getSharedPreferences("rook_apk_updates", Context.MODE_PRIVATE)
    private fun settings(ctx: Context) = ctx.getSharedPreferences("rook", Context.MODE_PRIVATE)

    @JvmStatic fun status(ctx: Context): String {
        val p = prefs(ctx)
        return JSONObject().put("ok", true).put("state", p.getString("state", "idle"))
            .put("message", p.getString("message", ""))
            .put("installed_version", BuildConfig.VERSION_NAME).put("installed_code", BuildConfig.VERSION_CODE)
            .put("target_code", p.getLong("target_code", 0)).put("last_check_ms", p.getLong("last_check", 0))
            .put("automatic", settings(ctx).getBoolean("apk_auto_update", true))
            .put("install_permission", Build.VERSION.SDK_INT < 26 || ctx.packageManager.canRequestPackageInstalls())
            .put("busy", busy.get()).toString()
    }

    internal fun state(ctx: Context, value: String, message: String = "") {
        prefs(ctx).edit().putString("state", value).putString("message", message.take(400)).commit()
    }

    /** A fast capability response; downloading and committing never block band RPC. */
    @JvmStatic fun request(ctx: Context, automatic: Boolean = false): String {
        val app = ctx.applicationContext
        if (automatic && (!settings(app).getBoolean("apk_auto_update", true) ||
            System.currentTimeMillis() - prefs(app).getLong("last_check", 0) < INTERVAL)) return status(app)
        if (!busy.compareAndSet(false, true)) return status(app)
        executor.execute {
            try { checkAndInstall(app) }
            catch (error: Exception) { state(app, "failed", error.message ?: error.javaClass.simpleName) }
            finally { busy.set(false) }
        }
        return JSONObject(status(app)).put("queued", true).toString()
    }

    private fun checkAndInstall(ctx: Context) {
        val p = prefs(ctx)
        val installer = ctx.packageManager.packageInstaller
        val previous = p.getInt("session", -1)
        if (previous >= 0) {
            val session = installer.getSessionInfo(previous)
            if (session != null && p.getString("state", "") in setOf("installing", "confirmation_required") &&
                System.currentTimeMillis() - p.getLong("session_started", 0) < 24 * 60 * 60 * 1000L) return
            if (session != null) installer.abandonSession(previous)
            p.edit().remove("session").commit()
        }
        state(ctx, "checking")
        p.edit().putLong("last_check", System.currentTimeMillis()).commit()
        val manifest = http.newCall(Request.Builder().url("$ORIGIN/apk.json").header("User-Agent", "rook-worker").build()).execute().use { response ->
            check(response.isSuccessful) { "Update manifest HTTP ${response.code}" }
            val body = response.body ?: error("Empty update manifest")
            require(body.contentLength() <= 16384) { "Update manifest too large" }
            val bytes = body.byteStream().use { it.readBytesBounded(16384) }
            JSONObject(String(bytes, Charsets.UTF_8))
        }
        val code = manifest.getLong("version_code")
        p.edit().putLong("target_code", code).commit()
        if (code <= BuildConfig.VERSION_CODE) { state(ctx, "current"); cancelNotification(ctx); return }
        require(manifest.getString("package") == ctx.packageName) { "Wrong update package" }
        val hash = manifest.getString("sha256")
        require(hash.matches(Regex("[0-9a-f]{64}"))) { "Invalid APK digest" }
        val size = manifest.getLong("size")
        require(size in 1..MAX_APK) { "Invalid APK size" }
        if (Build.VERSION.SDK_INT >= 26 && !ctx.packageManager.canRequestPackageInstalls()) {
            state(ctx, "permission_required", "Allow Rook to install updates, then it can request unattended installation.")
            val intent = Intent(ctx, ApkUpdatePermissionActivity::class.java)
            notify(ctx, "Allow Rook updates", "Tap to allow installation from Rook.", intent)
            return
        }
        val apk = File(ctx.filesDir, "rook-update.apk")
        try {
            state(ctx, "downloading", "Downloading ${manifest.getString("version_name")}")
            http.newCall(Request.Builder().url("$ORIGIN/apk").header("User-Agent", "rook-worker").build()).execute().use { response ->
                check(response.isSuccessful) { "APK download HTTP ${response.code}" }
                val body = response.body ?: error("Empty APK response")
                val digest = MessageDigest.getInstance("SHA-256")
                var total = 0L
                body.byteStream().use { input -> apk.outputStream().use { output ->
                    val buffer = ByteArray(64 * 1024)
                    while (true) {
                        val count = input.read(buffer)
                        if (count < 0) break
                        total += count
                        require(total <= size) { "APK exceeds declared size" }
                        digest.update(buffer, 0, count); output.write(buffer, 0, count)
                    }
                    output.fd.sync()
                } }
                require(total == size && digest.digest().hex() == hash) { "APK digest or size mismatch" }
            }
            verifyApk(ctx, apk, code)
            installVerified(ctx, apk, code)
        } finally { apk.delete() }
    }

    internal fun verifyApk(ctx: Context, apk: File, code: Long) {
        val flags = if (Build.VERSION.SDK_INT >= 28) PackageManager.GET_SIGNING_CERTIFICATES else PackageManager.GET_SIGNATURES
        val candidate = ctx.packageManager.getPackageArchiveInfo(apk.path, flags) ?: error("Invalid APK")
        val installed = ctx.packageManager.getPackageInfo(ctx.packageName, flags)
        fun version(info: PackageInfo) = if (Build.VERSION.SDK_INT >= 28) info.longVersionCode else info.versionCode.toLong()
        require(candidate.packageName == ctx.packageName) { "Wrong APK package" }
        require(version(candidate) == code && code > version(installed)) { "APK version mismatch or downgrade" }
        fun signers(info: PackageInfo): Set<String> {
            val values = if (Build.VERSION.SDK_INT >= 28) info.signingInfo?.apkContentsSigners else info.signatures
            return values?.map { MessageDigest.getInstance("SHA-256").digest(it.toByteArray()).hex() }?.toSet() ?: emptySet()
        }
        val expected = signers(installed)
        require(expected.isNotEmpty() && signers(candidate) == expected) { "APK signing certificate does not match Rook" }
        // PackageInstaller also verifies the complete APK signature at commit.
    }

    internal fun installVerified(ctx: Context, apk: File, code: Long) {
        verifyApk(ctx, apk, code)
        val installer = ctx.packageManager.packageInstaller
        val params = PackageInstaller.SessionParams(PackageInstaller.SessionParams.MODE_FULL_INSTALL).apply {
            setAppPackageName(ctx.packageName); setSize(apk.length())
            if (Build.VERSION.SDK_INT >= 31) setRequireUserAction(PackageInstaller.SessionParams.USER_ACTION_NOT_REQUIRED)
        }
        val id = installer.createSession(params)
        prefs(ctx).edit().putInt("session", id).putLong("session_started", System.currentTimeMillis()).putLong("target_code", code).commit()
        try {
            installer.openSession(id).use { session ->
                session.openWrite("base.apk", 0, apk.length()).use { output ->
                    apk.inputStream().use { it.copyTo(output) }; session.fsync(output)
                }
                val intent = Intent(ctx, ApkUpdateReceiver::class.java).setAction("systems.bake.rook.APK_RESULT")
                val flags = PendingIntent.FLAG_UPDATE_CURRENT or if (Build.VERSION.SDK_INT >= 31) PendingIntent.FLAG_MUTABLE else 0
                state(ctx, "installing")
                session.commit(PendingIntent.getBroadcast(ctx, id, intent, flags).intentSender)
            }
        } catch (error: Exception) {
            installer.abandonSession(id); prefs(ctx).edit().remove("session").commit(); throw error
        }
    }

    internal fun result(ctx: Context, intent: Intent) {
        val p = prefs(ctx)
        if (intent.getIntExtra(PackageInstaller.EXTRA_SESSION_ID, -2) != p.getInt("session", -1)) return
        val code = intent.getIntExtra(PackageInstaller.EXTRA_STATUS, PackageInstaller.STATUS_FAILURE)
        if (code == PackageInstaller.STATUS_PENDING_USER_ACTION) {
            val confirmation = if (Build.VERSION.SDK_INT >= 33) intent.getParcelableExtra(Intent.EXTRA_INTENT, Intent::class.java)
                else @Suppress("DEPRECATION") intent.getParcelableExtra(Intent.EXTRA_INTENT)
            state(ctx, "confirmation_required", "Android requires confirmation. Tap the Rook update notification to install.")
            if (confirmation != null) notify(ctx, "Install Rook update", "Tap to confirm installation.", confirmation)
        } else {
            p.edit().remove("session").commit()
            state(ctx, if (code == PackageInstaller.STATUS_SUCCESS) "installed" else "failed",
                if (code == PackageInstaller.STATUS_SUCCESS) "" else intent.getStringExtra(PackageInstaller.EXTRA_STATUS_MESSAGE) ?: "Installation failed ($code)")
            cancelNotification(ctx)
        }
    }

    internal fun replaced(ctx: Context) {
        prefs(ctx).edit().remove("session").putString("state", "installed").putString("message", "Updated to ${BuildConfig.VERSION_NAME}").commit()
        File(ctx.filesDir, "rook-update.apk").delete(); cancelNotification(ctx)
    }

    private fun notify(ctx: Context, title: String, message: String, intent: Intent) {
        val manager = ctx.getSystemService(NotificationManager::class.java)
        if (Build.VERSION.SDK_INT >= 26) manager.createNotificationChannel(NotificationChannel(CHANNEL, "Rook updates", NotificationManager.IMPORTANCE_DEFAULT))
        val action = PendingIntent.getActivity(ctx, NOTIFICATION, intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK), PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE)
        try { manager.notify(NOTIFICATION, NotificationCompat.Builder(ctx, CHANNEL).setSmallIcon(android.R.drawable.stat_sys_download_done)
            .setContentTitle(title).setContentText(message).setContentIntent(action).setAutoCancel(true).build()) }
        catch (_: SecurityException) { /* Settings and device.update_status still show the required action. */ }
    }
    private fun cancelNotification(ctx: Context) { ctx.getSystemService(NotificationManager::class.java).cancel(NOTIFICATION) }
    private fun ByteArray.hex() = joinToString("") { "%02x".format(it) }
    private fun java.io.InputStream.readBytesBounded(max: Int): ByteArray {
        val output = java.io.ByteArrayOutputStream()
        val buffer = ByteArray(4096)
        while (true) { val n = read(buffer); if (n < 0) break; require(output.size() + n <= max) { "Manifest too large" }; output.write(buffer, 0, n) }
        return output.toByteArray()
    }
}
