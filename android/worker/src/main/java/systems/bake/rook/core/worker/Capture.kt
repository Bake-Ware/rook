package systems.bake.rook.core.worker

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.graphics.Bitmap
import android.graphics.PixelFormat
import android.hardware.display.DisplayManager
import android.hardware.display.VirtualDisplay
import android.media.Image
import android.media.ImageReader
import android.media.projection.MediaProjection
import android.media.projection.MediaProjectionManager
import android.os.Build
import android.os.Handler
import android.os.HandlerThread
import android.os.IBinder
import android.util.DisplayMetrics
import android.util.Log
import android.view.WindowManager
import java.io.ByteArrayOutputStream
import java.util.concurrent.CountDownLatch

/**
 * screen.capture where the accessibility service is off (brief AP5): Android's own screen capture,
 * MediaProjection. The cap brings up Android's prompt itself (Bridge.capture, through AskActivity);
 * once the person allows it, AskActivity starts [CaptureService], a foreground service of type
 * mediaProjection, and only once that service is in the foreground does it take the projection. That
 * order is what Android 14 requires and what 0.5 got wrong: it took the projection first and crashed.
 *
 * From Android 14 a grant is single-use: one getMediaProjection and one virtual display per "Start".
 * So the session made from a grant is kept: one projection, one virtual display mirroring the screen
 * into one ImageReader, whose newest frame is held, and every later capture is that frame, with no
 * new prompt. It lasts until the person stops it (the status bar's chip or the notification) or
 * Android ends it; the next capture then asks again. A screen that does not change sends no new
 * frame, so the newest frame held is always what is on screen now.
 */
object Capture {
    private const val TAG = "RookCapture"
    private val lock = Object()
    private var projection: MediaProjection? = null
    private var display: VirtualDisplay? = null
    private var reader: ImageReader? = null
    private var thread: HandlerThread? = null
    private var newest: Image? = null
    private var first = CountDownLatch(1)

    @JvmStatic fun live(): Boolean = synchronized(lock) { reader != null }

    /** Start the session from the person's grant. Called by CaptureService once it is in the foreground. Null, or why not. */
    internal fun begin(context: Context, code: Int, data: Intent): String? = synchronized(lock) {
        release()
        try {
            val manager = context.getSystemService(MediaProjectionManager::class.java)
            val mp = manager.getMediaProjection(code, data) ?: return "Android gave no screen capture for that answer"
            val handlerThread = HandlerThread("rook-capture").apply { start() }
            val handler = Handler(handlerThread.looper)
            mp.registerCallback(object : MediaProjection.Callback() {     // Android 14 needs one before the display
                override fun onStop() { synchronized(lock) { if (projection === mp) end() } }
            }, handler)
            val metrics = DisplayMetrics()
            @Suppress("DEPRECATION")
            context.getSystemService(WindowManager::class.java).defaultDisplay.getRealMetrics(metrics)
            val r = ImageReader.newInstance(metrics.widthPixels, metrics.heightPixels, PixelFormat.RGBA_8888, 3)
            val gate = CountDownLatch(1)
            first = gate
            r.setOnImageAvailableListener({ source ->
                val image = try { source.acquireLatestImage() } catch (e: IllegalStateException) { null } ?: return@setOnImageAvailableListener
                synchronized(lock) {
                    newest?.close()
                    newest = image
                }
                gate.countDown()
            }, handler)
            display = mp.createVirtualDisplay("rook-capture", metrics.widthPixels, metrics.heightPixels, metrics.densityDpi,
                DisplayManager.VIRTUAL_DISPLAY_FLAG_AUTO_MIRROR, r.surface, null, handler)
            projection = mp
            reader = r
            thread = handlerThread
            Log.i(TAG, "screen capture session up (${metrics.widthPixels}x${metrics.heightPixels})")
            null
        } catch (t: Throwable) {
            Log.e(TAG, "the screen capture session did not start", t)
            release()
            "the screen capture did not start: ${t.javaClass.simpleName}: ${t.message}"
        }
    }

    /** End the session (the person stopped it, or Android did), and its service. */
    internal fun end() {
        release()
        CaptureService.stop()
    }

    private fun release() = synchronized(lock) {
        try { display?.release() } catch (_: Throwable) {}
        try { newest?.close() } catch (_: Throwable) {}
        try { reader?.close() } catch (_: Throwable) {}
        val mp = projection
        projection = null; display = null; reader = null; newest = null
        try { mp?.stop() } catch (_: Throwable) {}
        thread?.quitSafely(); thread = null
    }

    /** The newest frame as a JPEG at [quality]; waits for the session's first frame. Null when there is no session. */
    @JvmStatic fun jpeg(quality: Int): ByteArray? {
        val gate = synchronized(lock) { if (reader == null) return null; first }
        gate.await()
        val bitmap = synchronized(lock) {
            val image = newest ?: return null
            val plane = image.planes[0]
            val padded = plane.rowStride / plane.pixelStride
            val bmp = Bitmap.createBitmap(padded, image.height, Bitmap.Config.ARGB_8888)
            plane.buffer.rewind()
            bmp.copyPixelsFromBuffer(plane.buffer)
            if (padded == image.width) bmp else Bitmap.createBitmap(bmp, 0, 0, image.width, image.height)
        }
        return ByteArrayOutputStream().also { bitmap.compress(Bitmap.CompressFormat.JPEG, quality, it) }.toByteArray()
    }
}

/**
 * The foreground service of type mediaProjection that a screen capture session runs in (Capture). Started
 * by AskActivity right after the person allows the capture, with Android's answer; it goes into the
 * foreground first and then starts the session, and it stops when the session ends.
 */
class CaptureService : Service() {
    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        val code = intent?.getIntExtra(CODE, 0) ?: 0
        @Suppress("DEPRECATION")
        val data = intent?.getParcelableExtra<Intent>(DATA)
        if (data == null) {
            stopSelf()
            return START_NOT_STICKY
        }
        current = this
        val notifications = getSystemService(NotificationManager::class.java)
        notifications.createNotificationChannel(NotificationChannel(CHANNEL, "Rook screen capture", NotificationManager.IMPORTANCE_LOW))
        val shown = Notification.Builder(this, CHANNEL)
            .setContentTitle("Rook can capture the screen")
            .setContentText("For screen.capture, until you stop it")
            .setSmallIcon(android.R.drawable.ic_menu_camera)
            .setOngoing(true)
            .build()
        if (Build.VERSION.SDK_INT >= 29) startForeground(NOTIFICATION, shown, ServiceInfo.FOREGROUND_SERVICE_TYPE_MEDIA_PROJECTION)
        else startForeground(NOTIFICATION, shown)
        Bridge.answered(Bridge.CAPTURE, Capture.begin(this, code, data))
        return START_NOT_STICKY
    }

    override fun onDestroy() {
        if (current === this) current = null
        Capture.end()
        super.onDestroy()
    }

    override fun onBind(intent: Intent?): IBinder? = null

    companion object {
        const val CODE = "code"
        const val DATA = "data"
        private const val CHANNEL = "rook-capture"
        private const val NOTIFICATION = 2
        @Volatile private var current: CaptureService? = null

        internal fun stop() {
            current?.let { it.stopForeground(Service.STOP_FOREGROUND_REMOVE); it.stopSelf() }
        }
    }
}
