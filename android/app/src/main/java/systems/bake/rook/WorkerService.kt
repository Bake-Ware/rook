package systems.bake.rook

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.os.Build
import android.os.IBinder
import android.os.PowerManager
import android.util.Log
import com.chaquo.python.PyObject
import kotlin.concurrent.thread

/**
 * Foreground service that runs the Python band worker for the life of the app.
 *
 * The actual worker is `rook.worker` (staged into the Chaquopy python source by
 * stage_worker.py). We call `worker_entry.start(hub, psk, name)` on a dedicated
 * thread; it owns its own asyncio loop and blocks until [stopWorker] is called.
 */
class WorkerService : Service() {

    private var workerThread: Thread? = null
    private var entry: PyObject? = null
    private var wakeLock: PowerManager.WakeLock? = null

    override fun onCreate() {
        super.onCreate()
        createChannel()
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        val hub = intent?.getStringExtra(EXTRA_HUB) ?: BuildConfig.DEFAULT_HUB
        val psk = intent?.getStringExtra(EXTRA_PSK) ?: BuildConfig.DEFAULT_PSK
        val name = intent?.getStringExtra(EXTRA_NAME) ?: defaultName()

        startForegroundCompat(NOTIF_ID, buildNotification("on band as $name"))
        acquireWakeLock()
        startWorker(hub, psk, name)
        // START_STICKY: Android restarts us (with a null intent) if killed; the
        // null branch above falls back to BuildConfig defaults + saved prefs.
        return START_STICKY
    }

    private fun startWorker(hub: String, psk: String, name: String) {
        if (workerThread?.isAlive == true) return
        workerThread = thread(name = "rook-worker", isDaemon = true) {
            try {
                val py = PythonHost.ensureStarted(applicationContext)
                entry = py.getModule("worker_entry")
                // Blocks until the worker stops (entry installs its own SIGTERM-
                // equivalent stop Event, triggered from stopWorker()).
                entry?.callAttr("start", hub, psk, name)
            } catch (t: Throwable) {
                Log.e(TAG, "worker crashed", t)
            }
        }
    }

    private fun stopWorker() {
        try {
            entry?.callAttr("stop")
        } catch (t: Throwable) {
            Log.w(TAG, "stop() failed", t)
        }
        workerThread = null
        entry = null
    }

    override fun onDestroy() {
        stopWorker()
        wakeLock?.let { if (it.isHeld) it.release() }
        super.onDestroy()
    }

    override fun onBind(intent: Intent?): IBinder? = null

    // ---- plumbing ----------------------------------------------------------

    private fun acquireWakeLock() {
        val pm = getSystemService(Context.POWER_SERVICE) as PowerManager
        wakeLock = pm.newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "rook:worker").apply {
            setReferenceCounted(false)
            acquire()
        }
    }

    private fun defaultName(): String =
        (Build.MODEL ?: "android").replace(' ', '-')

    private fun createChannel() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            val ch = NotificationChannel(
                CHANNEL_ID, "Rook Worker", NotificationManager.IMPORTANCE_LOW
            ).apply { description = "Keeps the band worker alive" }
            (getSystemService(NotificationManager::class.java)).createNotificationChannel(ch)
        }
    }

    private fun buildNotification(text: String): Notification {
        val builder = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O)
            Notification.Builder(this, CHANNEL_ID) else
            @Suppress("DEPRECATION") Notification.Builder(this)
        return builder
            .setContentTitle("Rook Worker")
            .setContentText(text)
            .setSmallIcon(android.R.drawable.stat_sys_data_bluetooth)
            .setOngoing(true)
            .build()
    }

    private fun startForegroundCompat(id: Int, n: Notification) {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.UPSIDE_DOWN_CAKE) {
            startForeground(
                id, n,
                ServiceInfo.FOREGROUND_SERVICE_TYPE_DATA_SYNC or
                    ServiceInfo.FOREGROUND_SERVICE_TYPE_MEDIA_PROJECTION
            )
        } else {
            startForeground(id, n)
        }
    }

    companion object {
        private const val TAG = "RookWorkerService"
        private const val CHANNEL_ID = "rook_worker"
        private const val NOTIF_ID = 1001
        const val EXTRA_HUB = "hub"
        const val EXTRA_PSK = "psk"
        const val EXTRA_NAME = "name"

        fun start(ctx: Context, hub: String, psk: String, name: String) {
            val i = Intent(ctx, WorkerService::class.java)
                .putExtra(EXTRA_HUB, hub)
                .putExtra(EXTRA_PSK, psk)
                .putExtra(EXTRA_NAME, name)
            ctx.startForegroundService(i)
        }

        fun stop(ctx: Context) {
            ctx.stopService(Intent(ctx, WorkerService::class.java))
        }
    }
}
