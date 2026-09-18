package dev.flightpath.app

import android.content.Context
import android.net.ConnectivityManager
import android.net.Network
import android.net.NetworkCapabilities
import android.net.NetworkRequest
import android.net.wifi.WifiNetworkSpecifier
import android.os.PatternMatcher
import android.util.Log

/**
 * Joins the GoPro's access point for THIS APP ONLY.
 *
 * This is the thing Termux cannot do and the single biggest reason for a native
 * app. `WifiNetworkSpecifier` asks Android for a local-only connection to the
 * camera, and `bindProcessToNetwork` routes this process (Kotlin, the WebView,
 * and the Python worker, which all share it) over that link. The rest of the
 * phone stays on 5G. No "no internet" warnings, no silent fallback to cellular,
 * no Samsung "Switch to mobile data" kicking us off.
 *
 * Android shows one system dialog listing matching networks the first time;
 * after the user picks the camera, later requests for the same SSID connect
 * without asking again.
 */
class CameraWifi(private val context: Context) {

    interface Listener {
        fun onConnected(ssid: String)
        fun onUnavailable(reason: String)
        fun onLost()
    }

    private val cm = context.getSystemService(Context.CONNECTIVITY_SERVICE) as ConnectivityManager
    private var callback: ConnectivityManager.NetworkCallback? = null
    private var boundNetwork: Network? = null

    /**
     * @param ssid      exact camera SSID (e.g. "GP12345678"), or null to match any GP* network
     * @param password  the camera's WiFi password, shown on its Connections screen
     */
    fun connect(ssid: String?, password: String, listener: Listener) {
        disconnect()

        val specBuilder = WifiNetworkSpecifier.Builder()
        if (!ssid.isNullOrBlank()) {
            specBuilder.setSsid(ssid)
        } else {
            // GoPro access points are named GP followed by eight digits.
            specBuilder.setSsidPattern(PatternMatcher("GP", PatternMatcher.PATTERN_PREFIX))
        }
        specBuilder.setWpa2Passphrase(password)

        val request = NetworkRequest.Builder()
            .addTransportType(NetworkCapabilities.TRANSPORT_WIFI)
            // The camera has no internet. Saying so up front is what stops
            // Android from treating the link as broken and dropping it.
            .removeCapability(NetworkCapabilities.NET_CAPABILITY_INTERNET)
            .setNetworkSpecifier(specBuilder.build())
            .build()

        val cb = object : ConnectivityManager.NetworkCallback() {
            override fun onAvailable(network: Network) {
                boundNetwork = network
                val ok = cm.bindProcessToNetwork(network)
                Log.i(TAG, "camera network available, bound=$ok")
                listener.onConnected(ssid ?: "GoPro")
            }

            override fun onUnavailable() {
                Log.w(TAG, "camera network unavailable")
                listener.onUnavailable(
                    "Could not join the camera's WiFi. Is the phone's WiFi switched on, " +
                    "and the camera's (Preferences > Connections > Wireless Connections)? " +
                    "If the camera is not named GP..., type its exact name from Camera Info."
                )
            }

            override fun onLost(network: Network) {
                Log.w(TAG, "camera network lost")
                if (boundNetwork == network) {
                    cm.bindProcessToNetwork(null)
                    boundNetwork = null
                }
                listener.onLost()
            }
        }
        callback = cb
        // 30 s is long enough for the user to read and tap the system dialog.
        cm.requestNetwork(request, cb, 30_000)
    }

    /**
     * Fallback: do not join anything, just bind this app to whatever WiFi the
     * phone is already on (the user joined the camera from Android settings).
     * Android still owns that connection and may drop it, but at least our
     * traffic stops being routed around it to cellular.
     */
    fun bindCurrentWifi(listener: Listener) {
        disconnect()
        val request = NetworkRequest.Builder()
            .addTransportType(NetworkCapabilities.TRANSPORT_WIFI)
            .removeCapability(NetworkCapabilities.NET_CAPABILITY_INTERNET)
            .build()
        val cb = object : ConnectivityManager.NetworkCallback() {
            override fun onAvailable(network: Network) {
                boundNetwork = network
                val ok = cm.bindProcessToNetwork(network)
                Log.i(TAG, "bound to current wifi, ok=$ok")
                listener.onConnected("current WiFi")
            }
            override fun onUnavailable() {
                listener.onUnavailable("The phone is not on any WiFi. Join the camera's network in Android WiFi settings first.")
            }
            override fun onLost(network: Network) {
                if (boundNetwork == network) { cm.bindProcessToNetwork(null); boundNetwork = null }
                listener.onLost()
            }
        }
        callback = cb
        cm.requestNetwork(request, cb, 10_000)
    }

    fun disconnect() {
        callback?.let {
            try { cm.unregisterNetworkCallback(it) } catch (_: IllegalArgumentException) {}
        }
        callback = null
        if (boundNetwork != null) {
            cm.bindProcessToNetwork(null)
            boundNetwork = null
        }
    }

    val isBound: Boolean get() = boundNetwork != null

    companion object {
        private const val TAG = "FlightPath.Wifi"
    }
}
