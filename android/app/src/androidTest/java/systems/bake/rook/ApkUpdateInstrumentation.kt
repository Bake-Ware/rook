package systems.bake.rook

import android.app.Instrumentation
import android.content.Context
import android.os.Bundle
import java.io.File

/** Test-only entry point: private-file fixtures, never exposed in the shipped app. */
object ApkUpdateSmokeTest {
    fun run(ctx: Context, mode: String): Bundle {
        val result = Bundle()
        try {
            val apk = File(ctx.filesDir, "test-update.apk")
            if (mode == "verify") {
                ApkUpdater.verifyApk(ctx, apk, 6)
                var rejected = false
                try { ApkUpdater.verifyApk(ctx, apk, 5) } catch (_: IllegalArgumentException) { rejected = true }
                check(rejected) { "metadata version mismatch was accepted" }
                rejected = false
                try { ApkUpdater.verifyApk(ctx, File(ctx.filesDir, "test-wrong-signer.apk"), 6) }
                catch (_: IllegalArgumentException) { rejected = true }
                check(rejected) { "wrong signer was accepted" }
                result.putString("stream", "PASS: newer same-signer APK accepted; wrong version and wrong signer rejected\n")
            } else if (mode == "install") {
                ctx.getSharedPreferences("rook", Context.MODE_PRIVATE).edit()
                    .putBoolean("autostart", true).putBoolean("apk_auto_update", false)
                    .putString("hub", "10.0.2.2:17474").putString("psk", "rook-isolated-emulator-test")
                    .putString("name", "rook-ota-test").commit()
                WorkerService.start(ctx, "10.0.2.2:17474", "rook-isolated-emulator-test", "rook-ota-test")
                ApkUpdater.installVerified(ctx, apk, 6)
                result.putString("stream", "COMMITTED: inspect package version, update status, and WorkerService after replacement\n")
            }
            return result
        } catch (error: Throwable) {
            result.putString("stream", "FAIL: ${error.stackTraceToString()}\n"); return result
        }
    }
}
