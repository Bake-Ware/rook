package systems.bake.rook

import android.content.Context
import com.chaquo.python.Python
import com.chaquo.python.android.AndroidPlatform

/** Single place that guarantees Chaquopy is started before any Python call. */
object PythonHost {
    @Synchronized
    fun ensureStarted(context: Context): Python {
        if (!Python.isStarted()) {
            Python.start(AndroidPlatform(context.applicationContext))
        }
        return Python.getInstance()
    }
}
