package systems.bake.rook

import android.Manifest
import android.content.Context
import android.content.pm.PackageManager
import android.location.Location
import android.location.LocationListener
import android.location.LocationManager
import android.os.Build
import android.os.Bundle
import android.os.Looper
import android.os.SystemClock
import org.json.JSONObject
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit

/** One bounded request, made on the worker thread; never depends on another app. */
object LocationBridge {
    @JvmStatic fun get(ctx: Context, timeoutSeconds: Double): String {
        fun error(message: String) = JSONObject().put("ok", false).put("error", message).toString()
        if (!timeoutSeconds.isFinite()) return error("timeout must be finite")
        if (ctx.checkSelfPermission(Manifest.permission.ACCESS_FINE_LOCATION) != PackageManager.PERMISSION_GRANTED &&
            ctx.checkSelfPermission(Manifest.permission.ACCESS_COARSE_LOCATION) != PackageManager.PERMISSION_GRANTED)
            return error("Grant location in Rook Settings first.")
        if (Build.VERSION.SDK_INT >= 29 && ctx.checkSelfPermission(Manifest.permission.ACCESS_BACKGROUND_LOCATION) != PackageManager.PERMISSION_GRANTED)
            return error("For remote location, open Rook Settings → Background location and choose Allow all the time, then restart the worker.")
        val manager = ctx.getSystemService(Context.LOCATION_SERVICE) as LocationManager
        val providers = manager.getProviders(true).filter { it != LocationManager.PASSIVE_PROVIDER }
        if (providers.isEmpty()) return error("Location is turned off. Enable device Location services.")
        var best: Location? = null
        fun age(loc: Location) = ((SystemClock.elapsedRealtimeNanos() - loc.elapsedRealtimeNanos) / 1e9).coerceAtLeast(0.0)
        for (provider in providers) {
            try { manager.getLastKnownLocation(provider)?.let { if (best == null || age(it) < age(best!!)) best = it } }
            catch (_: SecurityException) { /* precise provider may be unavailable with approximate permission */ }
        }
        val latch = CountDownLatch(1)
        val result = java.util.concurrent.atomic.AtomicReference<Location>()
        val listener = object : LocationListener {
            override fun onLocationChanged(location: Location) { result.set(location); latch.countDown() }
            override fun onProviderEnabled(provider: String) {}
            override fun onProviderDisabled(provider: String) {}
            @Deprecated("Legacy callback") override fun onStatusChanged(provider: String?, status: Int, extras: Bundle?) {}
        }
        var registered = false
        var failure: String? = null
        if (best == null || age(best!!) > 15) {
            try {
                for (provider in providers) {
                    try {
                        manager.requestLocationUpdates(provider, 0L, 0f, listener, Looper.getMainLooper())
                        registered = true
                    } catch (e: Exception) { failure = e.message }
                }
                if (registered) latch.await(timeoutSeconds.coerceIn(1.0, 25.0).times(1000).toLong(), TimeUnit.MILLISECONDS)
                result.get()?.let { best = it }
            } finally { manager.removeUpdates(listener) }
        }
        val fix = best ?: return error(if (!registered) failure ?: "No accessible location provider." else "No fix before timeout. Try outdoors or increase timeout (up to 25 seconds).")
        return JSONObject().put("ok", true).put("lat", fix.latitude).put("lon", fix.longitude)
            .put("accuracy_m", if (fix.hasAccuracy()) fix.accuracy.toDouble() else JSONObject.NULL)
            .put("provider", fix.provider).put("age_s", age(fix)).put("stale", age(fix) > 120)
            .put("maps_url", "https://www.google.com/maps/search/?api=1&query=${fix.latitude},${fix.longitude}").toString()
    }
}
