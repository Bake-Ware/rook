package systems.bake.rook.core.worker

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.Manifest
import android.content.pm.PackageManager
import android.content.pm.ServiceInfo
import android.net.wifi.WifiManager
import android.os.Build
import android.os.Handler
import android.os.IBinder
import android.os.Looper
import android.os.PowerManager
import android.provider.Settings
import android.util.Log
import com.chaquo.python.Python
import com.chaquo.python.android.AndroidPlatform
import kotlin.concurrent.thread

/**
 * The foreground service the phone's worker runs in, for as long as it is on: with the screen off,
 * through Doze, after the app is swiped away (a foreground service outlives its task) and, being
 * START_STICKY, after Android stops the process, when it starts the service again with no intent.
 *
 * What keeps it linked is what kept the 0.5 app's worker linked: a foreground service, whose process
 * keeps its network and its wake locks in Doze, a partial wake lock, so the worker's loop runs with
 * the screen off, and a Wi-Fi lock, so the phone stays on Wi-Fi rather than dropping to mobile data
 * when the screen goes off.
 *
 * Its foreground service type is specialUse (Android 14 and later need one). The types Android names
 * do not fit a link that lasts for good: dataSync is for transfers, and from Android 15 it is stopped
 * after six hours in a day; connectedDevice and remoteMessaging describe other things. specialUse
 * says what this is in its manifest property and has no such stop. It is what an app outside the
 * Play Store may use without a review.
 */
class WorkerService : Service() {
    @Volatile private var stopping = false
    @Volatile private var worker: Thread? = null
    private var shown = "Starting"                          // what the notification says
    private var running: Pair<String, String?>? = null      // the hub and join code the worker was started with
    private var wakeLock: PowerManager.WakeLock? = null
    private var wifiLock: WifiManager.WifiLock? = null

    override fun onCreate() {
        super.onCreate()
        Bridge.attach(this)
        current = this
        val channel = NotificationChannel(CHANNEL, "Rook worker", NotificationManager.IMPORTANCE_LOW)
        channel.description = "Shows that this phone is a worker on its hub"
        getSystemService(NotificationManager::class.java).createNotificationChannel(channel)
        NotificationListener.rebind(this)       // brief AP4: bound again if Android had let it go
        SmsWatch.start(this)                    // brief AP7: once READ_SMS is granted
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        val prefs = RookWorker.prefs(this)
        val hub = prefs.getString(RookWorker.HUB, null)
        if (hub == null || !prefs.getBoolean(RookWorker.ON, false)) {
            stopSelf()
            return START_NOT_STICKY
        }
        foreground()
        hold()
        ensure(hub, prefs.getString(RookWorker.CODE, null))
        return START_STICKY
    }

    /**
     * Have the worker run for `hub` with `code`. One already running for that hub goes on, unless a new
     * code came; one running for anything else (RookWorker.start with another address or a new join
     * code) is stopped first, and the new one starts once it has. A fresh identity due (RookWorker.FRESH) is made by dropping the old
     * state before the new worker starts.
     */
    private fun ensure(hub: String, code: String?) {
        val now = running
        // Running for this hub: a code adds nothing unless it is a new one (a running worker has joined).
        if (worker?.isAlive == true && now != null && now.first == hub && (code == null || code == now.second)) return
        val previous = worker
        running = hub to code
        stopping = false
        worker = thread(name = "rook-worker", isDaemon = true) {
            try {
                if (previous != null) end(previous)
                val prefs = RookWorker.prefs(this)
                if (prefs.getBoolean(RookWorker.FRESH, false)) {
                    RookWorker.state(this).deleteRecursively()       // the old identity, its keys and its plugins' data
                    prefs.edit().remove(RookWorker.FRESH).remove(RookWorker.JOINED).apply()
                }
                if (!Python.isStarted()) Python.start(AndroidPlatform(Storage.python(this)))     // brief AP8: before the first unlock too
                val entry = Python.getInstance().getModule("rook_android")
                if (!stopping && worker === Thread.currentThread()) {
                    entry.callAttr("run", RookWorker.state(this).absolutePath, hub, deviceName(), code, Told())
                }
            } catch (t: Throwable) {
                Log.e(TAG, "the worker stopped", t)
                told("failed", "${t.javaClass.simpleName}: ${t.message}")
            }
            // It stopped on its own (refused, or an error it says), not for a newer worker or onDestroy.
            if (!stopping && worker === Thread.currentThread()) stopSelf()
        }
    }

    /** Stop the worker `thread` runs and wait until it has ended. A stop asked before the worker is
     *  running reaches nothing, so it is asked again until the thread ends. */
    private fun end(thread: Thread) {
        while (thread.isAlive) {
            if (Python.isStarted()) {
                try { Python.getInstance().getModule("rook_android").callAttr("stop") } catch (t: Throwable) { Log.w(TAG, "stop", t) }
            }
            thread.join(100)
        }
    }

    /** What the worker says, as the state the app sees and the notification's text. */
    inner class Told {
        fun state(kind: String, text: String) = told(kind, text)
    }

    private fun told(kind: String, text: String) {
        val state = when (kind) {
            "linked" -> WorkerState.Linked(text)
            "joining" -> WorkerState.Joining
            "failed" -> WorkerState.Failed(text)
            else -> WorkerState.Stopped
        }
        Log.i(TAG, "$kind: $text")
        if (state is WorkerState.Linked) RookWorker.joined(this)
        if (state is WorkerState.Linked) Events.flush(this)         // briefs AP4, AP7: events kept while unlinked go now
        if (state is WorkerState.Linked) SmsWatch.start(this)       // brief AP7: READ_SMS may have been given in Settings
        RookWorker.told(state)
        shown = when (state) {
            is WorkerState.Linked -> "Linked as ${state.moniker}"
            is WorkerState.Failed -> "Stopped: ${state.message}"
            WorkerState.Joining -> text.replaceFirstChar { it.uppercase() }
            WorkerState.Stopped -> "Stopped"
        }
        if (!stopping) getSystemService(NotificationManager::class.java).notify(NOTIFICATION, notification(shown))
    }

    /**
     * Be a foreground service (again, when what it may do changed): type specialUse, and location too once
     * the person has given Rook location all the time (brief AP3). Android lets an app in the background
     * read where the phone is only now and then; a foreground service of type location reads it as often
     * as an app in front. Without background location it is not added, so a start after boot never asks
     * Android for what it would refuse.
     */
    private fun foreground() {
        if (Build.VERSION.SDK_INT < 34) return startForeground(NOTIFICATION, notification(shown))
        val located = checkSelfPermission(Manifest.permission.ACCESS_BACKGROUND_LOCATION) == PackageManager.PERMISSION_GRANTED
        try {
            startForeground(NOTIFICATION, notification(shown), ServiceInfo.FOREGROUND_SERVICE_TYPE_SPECIAL_USE or
                (if (located) ServiceInfo.FOREGROUND_SERVICE_TYPE_LOCATION else 0))
        } catch (e: SecurityException) {            // Android would not have location now: specialUse alone
            Log.w(TAG, "location type refused", e)
            startForeground(NOTIFICATION, notification(shown), ServiceInfo.FOREGROUND_SERVICE_TYPE_SPECIAL_USE)
        }
    }

    private fun hold() {
        if (wakeLock == null) {
            wakeLock = getSystemService(PowerManager::class.java)
                .newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "rook:worker").apply { setReferenceCounted(false); acquire() }
        }
        if (wifiLock == null) {
            @Suppress("DEPRECATION")
            wifiLock = applicationContext.getSystemService(WifiManager::class.java)
                .createWifiLock(WifiManager.WIFI_MODE_FULL_HIGH_PERF, "rook:worker").apply { setReferenceCounted(false); acquire() }
        }
    }

    override fun onDestroy() {
        stopping = true
        worker?.let { thread(isDaemon = true) { end(it) } }
        wakeLock?.release()
        wifiLock?.release()
        RookWorker.told(WorkerState.Stopped)
        if (current === this) current = null
        super.onDestroy()
    }

    override fun onBind(intent: Intent?): IBinder? = null

    /** The phone's own name, as Settings > About phone shows it, else its model. */
    private fun deviceName(): String =
        Settings.Global.getString(contentResolver, Settings.Global.DEVICE_NAME)?.takeIf { it.isNotBlank() } ?: Build.MODEL

    private fun notification(text: String): Notification {
        val open = packageManager.getLaunchIntentForPackage(packageName)
        return Notification.Builder(this, CHANNEL)
            .setContentTitle("Rook worker")
            .setContentText(text)
            .setStyle(Notification.BigTextStyle().bigText(text))
            .setSmallIcon(R.drawable.ic_stat_rook)
            .setOngoing(true)
            .apply { if (open != null) setContentIntent(PendingIntent.getActivity(this@WorkerService, 0, open, PendingIntent.FLAG_IMMUTABLE)) }
            .build()
    }

    companion object {
        private const val TAG = "RookWorker"
        private const val CHANNEL = "rook-worker"
        private const val NOTIFICATION = 1

        @Volatile private var current: WorkerService? = null

        /** What Rook may do changed (a permission was given): be the foreground service again, with the types that allows,
         *  and watch the inbox if READ_SMS is now given (brief AP7). */
        internal fun permitted() {
            current?.let { service -> Handler(Looper.getMainLooper()).post { service.foreground(); SmsWatch.start(service) } }
        }

        fun start(context: Context) {
            context.startForegroundService(Intent(context, WorkerService::class.java))
        }

        fun stop(context: Context) {
            context.stopService(Intent(context, WorkerService::class.java))
        }
    }
}
