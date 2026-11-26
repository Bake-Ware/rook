package systems.bake.rook.core.worker

import android.app.Activity
import android.os.Bundle

/** Debug builds only: RookWorker.start (or stop) from adb, as the app's shell will call it. Not in release builds. */
class DebugStart : Activity() {
    override fun onCreate(saved: Bundle?) {
        super.onCreate(saved)
        if (intent.getBooleanExtra("stop", false)) {
            RookWorker.stop(this)
        } else {
            val hub = intent.getStringExtra("hub")
            if (hub != null) RookWorker.start(this, hub, intent.getStringExtra("code"))
        }
        finish()
    }
}
