package systems.bake.rook

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.net.ConnectivityManager
import android.net.Network
import android.net.wifi.WifiManager
import android.os.Build
import android.os.IBinder
import android.os.PowerManager
import android.util.Log
import com.chaquo.python.PyObject
import kotlin.concurrent.thread

/**
 * Foreground service that runs the Python worker for the life of the app.
 *
 * We call `castling_entry.start(files dir)` on a dedicated thread; it owns its
 * own asyncio loop and blocks until [stopWorker] is called. The hub, keys and
 * name come from the phone's enrollment ([Castling]).
 */
class WorkerService : Service() {

    private var workerThread: Thread? = null
    private var entry: PyObject? = null
    private var wakeLock: PowerManager.WakeLock? = null
    private var wifiLock: WifiManager.WifiLock? = null
    private var lastNetwork: Network? = null
    /** The phone hops between Wi-Fi and cellular; the band link on the old
     *  network then dies slowly (or its connect hangs). Reconnect at once on
     *  the new default network instead (bakephone drop-offs, 2026-10-22). */
    private val networkCallback = object : ConnectivityManager.NetworkCallback() {
        override fun onAvailable(network: Network) {
            val previous = lastNetwork
            lastNetwork = network
            if (previous != null && previous != network) kick("default network changed")
        }
        override fun onLost(network: Network) { Log.i(TAG, "default network lost") }
    }
    @Volatile private var wantProjection = false
    private var lastName: String = ""
    private val updateHandler = android.os.Handler(android.os.Looper.getMainLooper())
    private val updateTick = object : Runnable {
        override fun run() {
            ApkUpdater.request(applicationContext, automatic = true)
            updateHandler.postDelayed(this, 15 * 60 * 1000L)
        }
    }

    /** ACTION_USER_PRESENT is not a manifest broadcast: attention hears unlocks while the worker runs. */
    private val unlockReceiver = object : android.content.BroadcastReceiver() {
        override fun onReceive(ctx: Context, intent: Intent) {
            if (intent.action != Intent.ACTION_USER_PRESENT) return
            try { AttentionStore.unlocked(ctx) } catch (t: Throwable) { Log.w(TAG, "attention unlock failed", t) }
        }
    }

    override fun onCreate() {
        super.onCreate()
        createChannel()
        runCatching { AttentionChannels.ensure(this) }
        RookWidgets.bind(this)
        // EXPORTED on purpose: SystemUI (its own app uid, not the system uid) sends USER_PRESENT,
        // and a NOT_EXPORTED receiver never gets it on Android 13+. It is a protected broadcast,
        // so no ordinary app can send it to us.
        androidx.core.content.ContextCompat.registerReceiver(this, unlockReceiver,
            android.content.IntentFilter(Intent.ACTION_USER_PRESENT), androidx.core.content.ContextCompat.RECEIVER_EXPORTED)
        updateHandler.postDelayed(updateTick, 30_000)
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.N) {
            try { getSystemService(ConnectivityManager::class.java).registerDefaultNetworkCallback(networkCallback) }
            catch (t: Throwable) { Log.w(TAG, "network callback unavailable", t) }
        }
    }

    private fun kick(reason: String) {
        Log.i(TAG, "kick: $reason")
        thread(name = "rook-kick", isDaemon = true) {
            try { entry?.callAttr("kick", reason) } catch (t: Throwable) { Log.w(TAG, "kick failed", t) }
        }
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        val prefs = getSharedPreferences("rook", MODE_PRIVATE)

        // Screen-capture control. The Activity gets consent, then hands the
        // token to us (the service that owns the mediaProjection FGS type) so we
        // can foreground WITH that type and build the persistent capture session.
        when (intent?.action) {
            ACTION_START_CAPTURE -> {
                wantProjection = true
                if (lastName.isEmpty()) lastName = prefs.getString(Castling.NAME_PREF, null) ?: defaultName()
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
                startWorker()
                return START_STICKY
            }
            ACTION_STOP_CAPTURE -> {
                ScreenCaptureBridge.stopSession()
                wantProjection = false
                startForegroundCompat(NOTIF_ID, buildNotification("on band as $lastName"))
                return START_STICKY
            }
        }

        val name = prefs.getString(Castling.NAME_PREF, null) ?: defaultName()
        lastName = name
        wantProjection = ScreenCaptureBridge.hasSession()

        startForegroundCompat(NOTIF_ID, buildNotification("on band as $name"))
        acquireWakeLock()
        startWorker()
        return START_STICKY
    }

    private fun startWorker() {
        if (workerThread?.isAlive == true) return
        workerThread = thread(name = "rook-worker", isDaemon = true) {
            try {
                val py = PythonHost.ensureStarted(applicationContext)
                // Blocks until the worker stops (stopWorker() calls the entry's stop()).
                entry = py.getModule("castling_entry")
                entry?.callAttr("start", applicationContext.filesDir.absolutePath)
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
        updateHandler.removeCallbacks(updateTick)
        runCatching { unregisterReceiver(unlockReceiver) }
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.N) {
            try { getSystemService(ConnectivityManager::class.java).unregisterNetworkCallback(networkCallback) }
            catch (_: Throwable) {}
        }
        ScreenCaptureBridge.stopSession()
        FindDeviceBridge.stop()
        KeepAwake.shutdown()
        stopWorker()
        wakeLock?.let { if (it.isHeld) it.release() }
        wifiLock?.let { if (it.isHeld) it.release() }
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
        // Keep Wi-Fi up while the screen is off, so the phone stays on the home
        // network instead of falling back to cellular and dropping the band link.
        if (wifiLock == null) try {
            @Suppress("DEPRECATION")
            wifiLock = (applicationContext.getSystemService(Context.WIFI_SERVICE) as WifiManager)
                .createWifiLock(WifiManager.WIFI_MODE_FULL_HIGH_PERF, "rook:band").apply {
                    setReferenceCounted(false); acquire()
                }
        } catch (t: Throwable) { Log.w(TAG, "wifi lock unavailable", t) }
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
            .setContentIntent(NotificationNavigation.mainActivity(this))
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
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
            var type = ServiceInfo.FOREGROUND_SERVICE_TYPE_DATA_SYNC
            if (wantProjection || ScreenCaptureBridge.hasSession()) {
                type = type or ServiceInfo.FOREGROUND_SERVICE_TYPE_MEDIA_PROJECTION
            }
            if (checkSelfPermission(android.Manifest.permission.ACCESS_BACKGROUND_LOCATION) == android.content.pm.PackageManager.PERMISSION_GRANTED &&
                (checkSelfPermission(android.Manifest.permission.ACCESS_COARSE_LOCATION) == android.content.pm.PackageManager.PERMISSION_GRANTED ||
                 checkSelfPermission(android.Manifest.permission.ACCESS_FINE_LOCATION) == android.content.pm.PackageManager.PERMISSION_GRANTED)) {
                type = type or ServiceInfo.FOREGROUND_SERVICE_TYPE_LOCATION
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

        fun start(ctx: Context) {
            launch(ctx, Intent(ctx, WorkerService::class.java))
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
