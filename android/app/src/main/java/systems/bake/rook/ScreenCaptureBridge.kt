package systems.bake.rook

import android.content.Context
import android.content.Intent
import android.graphics.Bitmap
import android.graphics.PixelFormat
import android.hardware.display.DisplayManager
import android.hardware.display.VirtualDisplay
import android.media.ImageReader
import android.media.projection.MediaProjection
import android.media.projection.MediaProjectionManager
import android.os.Handler
import android.os.Looper
import android.util.DisplayMetrics
import android.util.Log
import android.view.WindowManager
import java.io.ByteArrayOutputStream

/**
 * Screen capture via MediaProjection, Android-14-correct.
 *
 * Android 14 makes a MediaProjection consent token SINGLE USE: you may call
 * getMediaProjection() once and createVirtualDisplay() once per grant. The old
 * code built a fresh projection + virtual display on every screenshot, which
 * threw "Don't re-use the resultData... Don't take multiple captures by invoking
 * createVirtualDisplay multiple times on the same instance."
 *
 * So we build ONE persistent session right after consent (inside WorkerService,
 * which owns the mediaProjection foreground-service type): one projection, one
 * VirtualDisplay mirroring into one long-lived ImageReader. Each screenshot just
 * pulls the latest mirrored frame from that reader — no new projection, no new
 * display. [startSession] is called by the service; Python calls [captureJpeg].
 */
object ScreenCaptureBridge {

    private const val TAG = "RookScreenCapture"

    @Volatile private var projection: MediaProjection? = null
    @Volatile private var reader: ImageReader? = null
    @Volatile private var display: VirtualDisplay? = null
    @Volatile private var capW: Int = 0
    @Volatile private var capH: Int = 0
    @Volatile private var lastJpeg: ByteArray? = null
    private var appContext: Context? = null

    fun init(ctx: Context) { appContext = ctx.applicationContext }

    fun hasSession(): Boolean = reader != null
    /** legacy name — some callers ask "do we have capture" this way */
    fun hasConsent(): Boolean = hasSession()

    /**
     * Build the persistent capture session from a fresh consent token. MUST be
     * called from a context where the mediaProjection foreground service is
     * already running (i.e. from WorkerService). Returns true on success.
     */
    @Synchronized
    fun startSession(ctx: Context, code: Int, data: Intent): Boolean {
        appContext = ctx.applicationContext
        stopSession()
        return try {
            val mgr = ctx.getSystemService(Context.MEDIA_PROJECTION_SERVICE)
                as MediaProjectionManager
            val mp = mgr.getMediaProjection(code, data) ?: return false
            // Android 14+ requires a registered callback before createVirtualDisplay.
            mp.registerCallback(object : MediaProjection.Callback() {
                override fun onStop() { stopSession() }
            }, Handler(Looper.getMainLooper()))

            val metrics = DisplayMetrics()
            @Suppress("DEPRECATION")
            (ctx.getSystemService(Context.WINDOW_SERVICE) as WindowManager)
                .defaultDisplay.getRealMetrics(metrics)
            val w = metrics.widthPixels
            val h = metrics.heightPixels
            val dpi = metrics.densityDpi

            val r = ImageReader.newInstance(w, h, PixelFormat.RGBA_8888, 2)
            val vd = mp.createVirtualDisplay(
                "rook-capture", w, h, dpi,
                DisplayManager.VIRTUAL_DISPLAY_FLAG_AUTO_MIRROR,
                r.surface, null, Handler(Looper.getMainLooper())
            )
            projection = mp
            reader = r
            display = vd
            capW = w
            capH = h
            lastJpeg = null
            Log.i(TAG, "capture session up (${w}x$h @ ${dpi}dpi)")
            true
        } catch (t: Throwable) {
            Log.e(TAG, "startSession failed", t)
            stopSession()
            false
        }
    }

    @Synchronized
    fun stopSession() {
        try { display?.release() } catch (_: Throwable) {}
        try { reader?.close() } catch (_: Throwable) {}
        try { projection?.stop() } catch (_: Throwable) {}
        display = null
        reader = null
        projection = null
    }

    /**
     * @return JPEG bytes of the latest screen frame, or null if there's no
     *   active capture session. Safe to call from Python. Reuses the persistent
     *   ImageReader — no new projection/display, so it's Android-14-safe and
     *   fast on repeat.
     */
    @JvmStatic
    fun captureJpeg(quality: Int): ByteArray? {
        val r = reader ?: return null
        // The virtual display mirrors continuously, but a fully static screen
        // stops producing new frames; poll briefly, then fall back to the last
        // frame we successfully encoded so a screenshot never comes back empty.
        var image = r.acquireLatestImage()
        var tries = 0
        while (image == null && tries < 40) {
            Thread.sleep(15)
            image = r.acquireLatestImage()
            tries++
        }
        if (image == null) return lastJpeg
        return try {
            val plane = image.planes[0]
            val rowStride = plane.rowStride
            val pixelStride = plane.pixelStride
            val w = image.width
            val h = image.height
            val rowPadding = rowStride - pixelStride * w
            val bmp = Bitmap.createBitmap(
                w + rowPadding / pixelStride, h, Bitmap.Config.ARGB_8888
            )
            bmp.copyPixelsFromBuffer(plane.buffer)
            val cropped = if (rowPadding == 0) bmp else Bitmap.createBitmap(bmp, 0, 0, w, h)
            val out = ByteArrayOutputStream()
            cropped.compress(Bitmap.CompressFormat.JPEG, quality.coerceIn(1, 100), out)
            val bytes = out.toByteArray()
            lastJpeg = bytes
            bytes
        } catch (t: Throwable) {
            Log.e(TAG, "encode failed", t)
            lastJpeg
        } finally {
            try { image.close() } catch (_: Throwable) {}
        }
    }
}
