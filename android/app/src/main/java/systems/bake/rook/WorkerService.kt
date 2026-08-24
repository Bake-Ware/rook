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
    @Volatile private var wantProjection = false
    private var lastName: String = ""

    override fun onCreate() {
        super.onCreate()
        createChannel()
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        val prefs = getSharedPreferences("rook", MODE_PRIVATE)

        // Screen-capture control. The Activity gets consent, then hands the
        // token to us (the service that owns the mediaProjection FGS type) so we
        // can foreground WITH that type and build the persistent capture session.
        when (intent?.action) {
            ACTION_START_CAPTURE -> {
                wantProjection = true
                if (lastName.isEmpty()) lastName = prefs.getString("name", defaultName()) ?: defaultName()
                startForegroundCompat(NOTIF_ID, buildNotification("on band as $lastName · screen"))
                acquireWakeLock()
                val code = intent.getIntExtra(EXTRA_CODE, android.app.Activity.RESULT_CANCELED)
                val data: Intent? =
                    if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU)
                        intent.getParcelableExtra(EXTRA_DATA, Intent::class.java)
                    else @Suppress("DEPRECATION") intent.getParcelableExtra(EXTRA_DATA)
                if (data != null) {
                    val ok = ScreenCaptureBridge.startSession(this, code, data)
                    Log.i(TAG, "capture session start ok=$ok")
                }
                // Make sure the band worker itself is up too (usually already is).
                startWorker(
                    prefs.getString("hub", BuildConfig.DEFAULT_HUB) ?: BuildConfig.DEFAULT_HUB,
                    prefs.getString("psk", BuildConfig.DEFAULT_PSK) ?: BuildConfig.DEFAULT_PSK,
                    lastName,
                )
                return START_STICKY
            }
            ACTION_STOP_CAPTURE -> {
                ScreenCaptureBridge.stopSession()
                wantProjection = false
                startForegroundCompat(NOTIF_ID, buildNotification("on band as $lastName"))
                return START_STICKY
            }
        }

        // On a START_STICKY restart the intent is null. Fall back to the last
        // SAVED band settings rather than the (empty) BuildConfig defaults, so a
        // killed worker actually reconnects instead of coming back with a blank PSK.
        val hub = intent?.getStringExtra(EXTRA_HUB)
            ?: prefs.getString("hub", BuildConfig.DEFAULT_HUB) ?: BuildConfig.DEFAULT_HUB
        val psk = intent?.getStringExtra(EXTRA_PSK)
            ?: prefs.getString("psk", BuildConfig.DEFAULT_PSK) ?: BuildConfig.DEFAULT_PSK
        val name = intent?.getStringExtra(EXTRA_NAME)
            ?: prefs.getString("name", defaultName()) ?: defaultName()
        lastName = name
        wantProjection = ScreenCaptureBridge.hasSession()

        startForegroundCompat(NOTIF_ID, buildNotification("on band as $name"))
        acquireWakeLock()
        startWorker(hub, psk, name)
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
        ScreenCaptureBridge.stopSession()
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
        // Typed foreground-service starts exist from API 30 (R). We ALWAYS run as
        // dataSync (the band connection). We add mediaProjection ONLY once the
        // user has granted screen capture — claiming that type without an active
        // MediaProjection consent token makes startForeground() throw
        // SecurityException on Android 14+, which crashed the app on every start.
        // After consent is granted the service is re-started (see MainActivity),
        // which re-runs this with the mediaProjection type included.
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.R) {
            var type = ServiceInfo.FOREGROUND_SERVICE_TYPE_DATA_SYNC
            if (wantProjection || ScreenCaptureBridge.hasSession()) {
                type = type or ServiceInfo.FOREGROUND_SERVICE_TYPE_MEDIA_PROJECTION
            }
            startForeground(id, n, type)
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
        const val EXTRA_CODE = "projCode"
        const val EXTRA_DATA = "projData"
        const val ACTION_START_CAPTURE = "systems.bake.rook.START_CAPTURE"
        const val ACTION_STOP_CAPTURE = "systems.bake.rook.STOP_CAPTURE"

        // startForegroundService is API 26+. On Android 6/7 it doesn't exist
        // (calling it crashed the app on Start / screen-share); use startService,
        // which is fine there — the service still calls startForeground itself.
        private fun launch(ctx: Context, i: Intent) {
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) ctx.startForegroundService(i)
            else @Suppress("DEPRECATION") ctx.startService(i)
        }

        fun start(ctx: Context, hub: String, psk: String, name: String) {
            val i = Intent(ctx, WorkerService::class.java)
                .putExtra(EXTRA_HUB, hub)
                .putExtra(EXTRA_PSK, psk)
                .putExtra(EXTRA_NAME, name)
            launch(ctx, i)
        }

        fun stop(ctx: Context) {
            ctx.stopService(Intent(ctx, WorkerService::class.java))
        }

        /** Hand a fresh MediaProjection consent token to the service so it can
         *  foreground with the mediaProjection type and open the capture session. */
        fun startCapture(ctx: Context, code: Int, data: Intent) {
            val i = Intent(ctx, WorkerService::class.java)
                .setAction(ACTION_START_CAPTURE)
                .putExtra(EXTRA_CODE, code)
                .putExtra(EXTRA_DATA, data)
            launch(ctx, i)
        }
    }
}
