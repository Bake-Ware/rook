package systems.bake.rook

import android.content.Intent
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.provider.Settings
import androidx.appcompat.app.AppCompatActivity
import androidx.activity.result.contract.ActivityResultContracts

/** Only opens after the user taps the installation-permission notification. */
class ApkUpdatePermissionActivity : AppCompatActivity() {
    private val permission = registerForActivityResult(ActivityResultContracts.StartActivityForResult()) {
        if (Build.VERSION.SDK_INT < 26 || packageManager.canRequestPackageInstalls()) ApkUpdater.request(this)
        finish()
    }
    override fun onCreate(state: Bundle?) {
        super.onCreate(state)
        if (state != null) return
        if (Build.VERSION.SDK_INT >= 26 && !packageManager.canRequestPackageInstalls())
            permission.launch(Intent(Settings.ACTION_MANAGE_UNKNOWN_APP_SOURCES, Uri.parse("package:$packageName")))
        else { ApkUpdater.request(this); finish() }
    }
}
