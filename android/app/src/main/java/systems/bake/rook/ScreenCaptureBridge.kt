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
import android.view.WindowManager
import java.io.ByteArrayOutputStream

/**
 * Real screen capture via MediaProjection — the native replacement for the
 * Termux `termux-camera-photo` hack. Python's `screenshot.capture` calls
 * [captureJpeg]; it returns a JPEG byte[] or null if consent hasn't been granted.
 *
 * Consent is a one-time per-grant Activity flow handled in MainActivity, which
 * calls [setProjection] with the result. The MediaProjection token survives until
 * the app is killed (or [release] is called).
 */
object ScreenCaptureBridge {

    @Volatile private var projection: MediaProjection? = null
    @Volatile private var resultCode: Int = 0
    @Volatile private var resultData: Intent? = null
    private var appContext: Context? = null

    fun init(ctx: Context) { appContext = ctx.applicationContext }

    /** Called from MainActivity after the user approves the capture dialog. */
    fun setProjection(ctx: Context, code: Int, data: Intent) {
        appContext = ctx.applicationContext
        resultCode = code
        resultData = data
        // Defer building the MediaProjection until first capture so it's created
        // while the foreground service (mediaProjection type) is already running.
        projection = null
    }

    fun hasConsent(): Boolean = resultData != null

    fun release() {
        projection?.stop()
        projection = null
        resultData = null
        resultCode = 0
    }

    /** @return JPEG bytes, or null if no consent yet. Safe to call from Python. */
    @JvmStatic
    fun captureJpeg(quality: Int): ByteArray? {
        val ctx = appContext ?: return null
        val data = resultData ?: return null
        ensureProjection(ctx, resultCode, data) ?: return null

        val metrics = DisplayMetrics()
        @Suppress("DEPRECATION")
        (ctx.getSystemService(Context.WINDOW_SERVICE) as WindowManager)
            .defaultDisplay.getRealMetrics(metrics)
        val w = metrics.widthPixels
        val h = metrics.heightPixels
        val dpi = metrics.densityDpi

        val reader = ImageReader.newInstance(w, h, PixelFormat.RGBA_8888, 2)
        val handlerThread = Looper.getMainLooper() // VirtualDisplay needs a handler
        val display: VirtualDisplay = projection!!.createVirtualDisplay(
            "rook-capture", w, h, dpi,
            DisplayManager.VIRTUAL_DISPLAY_FLAG_AUTO_MIRROR,
            reader.surface, null, Handler(handlerThread)
        )

        return try {
            // Grab a single frame. Poll briefly for the first available image.
            var image = reader.acquireLatestImage()
            var tries = 0
            while (image == null && tries < 50) {
                Thread.sleep(20)
                image = reader.acquireLatestImage()
                tries++
            }
            if (image == null) return null

            val plane = image.planes[0]
            val rowStride = plane.rowStride
            val pixelStride = plane.pixelStride
            val rowPadding = rowStride - pixelStride * w
            val bmp = Bitmap.createBitmap(
                w + rowPadding / pixelStride, h, Bitmap.Config.ARGB_8888
            )
            bmp.copyPixelsFromBuffer(plane.buffer)
            image.close()

            val cropped = Bitmap.createBitmap(bmp, 0, 0, w, h)
            val out = ByteArrayOutputStream()
            cropped.compress(Bitmap.CompressFormat.JPEG, quality.coerceIn(1, 100), out)
            out.toByteArray()
        } finally {
            display.release()
            reader.close()
        }
    }

    private fun ensureProjection(ctx: Context, code: Int, data: Intent): MediaProjection? {
        projection?.let { return it }
        val mgr = ctx.getSystemService(Context.MEDIA_PROJECTION_SERVICE)
            as MediaProjectionManager
        val mp = mgr.getMediaProjection(code, data) ?: return null
        // Android 14+ (API 34) requires a registered callback before
        // createVirtualDisplay(), else IllegalStateException.
        mp.registerCallback(object : MediaProjection.Callback() {
            override fun onStop() { projection = null }
        }, Handler(Looper.getMainLooper()))
        projection = mp
        return mp
    }
}
