package dev.flightpath.app

import android.Manifest
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.content.pm.PackageManager
import android.net.wifi.WifiManager
import androidx.core.content.ContextCompat
import org.json.JSONArray
import org.json.JSONObject

/**
 * Lists nearby WiFi networks so the user can tap the camera instead of typing
 * its name. Android requires the location permission for this; the app uses it
 * for nothing else.
 */
class WifiScanner(private val context: Context) {

    private val wifi = context.applicationContext.getSystemService(Context.WIFI_SERVICE) as WifiManager
    @Volatile private var lastResults: String = "[]"
    @Volatile private var scanning = false

    private val receiver = object : BroadcastReceiver() {
        override fun onReceive(c: Context?, intent: Intent?) {
            scanning = false
            lastResults = collect()
        }
    }

    fun hasPermission(): Boolean =
        ContextCompat.checkSelfPermission(context, Manifest.permission.ACCESS_FINE_LOCATION) ==
            PackageManager.PERMISSION_GRANTED

    /** The phone's own WiFi radio. Off means no scan results and nothing to
     *  join, whatever the camera is doing; the app must say so rather than
     *  blame the camera. */
    fun wifiEnabled(): Boolean = wifi.isWifiEnabled

    fun start(): Boolean {
        if (!hasPermission()) return false
        try {
            context.applicationContext.registerReceiver(
                receiver, IntentFilter(WifiManager.SCAN_RESULTS_AVAILABLE_ACTION)
            )
        } catch (_: Exception) { /* already registered */ }
        scanning = true
        // startScan is throttled by Android (a few per 2 min). Cached results are
        // still readable, so a throttled scan is not a failure.
        @Suppress("DEPRECATION")
        val kicked = wifi.startScan()
        lastResults = collect()
        return kicked
    }

    fun results(): String = if (hasPermission()) collect() else "[]"
    fun isScanning(): Boolean = scanning

    private fun collect(): String {
        val arr = JSONArray()
        try {
            @Suppress("DEPRECATION")
            val seen = HashSet<String>()
            for (r in wifi.scanResults) {
                val ssid = r.SSID ?: continue
                if (ssid.isBlank() || !seen.add(ssid)) continue
                val o = JSONObject()
                o.put("ssid", ssid)
                o.put("level", r.level)
                o.put("gopro", ssid.startsWith("GP", ignoreCase = true) || ssid.contains("gopro", ignoreCase = true)
                        || ssid.contains("hero", ignoreCase = true))
                arr.put(o)
            }
        } catch (_: SecurityException) { }
        return arr.toString()
    }

    fun stop() {
        try { context.applicationContext.unregisterReceiver(receiver) } catch (_: Exception) {}
    }
}
