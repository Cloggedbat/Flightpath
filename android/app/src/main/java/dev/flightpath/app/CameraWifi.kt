package dev.flightpath.app

import android.content.Context
import android.net.ConnectivityManager
import android.net.Network
import android.net.NetworkCapabilities
import android.net.NetworkRequest
import android.net.wifi.WifiNetworkSpecifier
import android.os.Build
import android.os.Handler
import android.os.Looper
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
    private var bound = false
    private val handler = Handler(Looper.getMainLooper())

    /**
     * Route this process down the camera link, retrying until it takes.
     *
     * bindProcessToNetwork can return false while the link is still being
     * set up, and a single silent false is fatal in a way that looks like a
     * missing camera: Android sends every unbound socket to the default
     * network, so the engine's requests go out over cellular and the camera
     * is never contacted, even though the phone holds an address on its
     * network. Measured on the S22: phone 10.5.5.100, camera 10.5.5.9, link
     * good, bound false. So keep asking.
     */
    private fun bindWithRetries(network: Network, ssid: String, listener: Listener) {
        var attempt = 0
        fun tryBind() {
            attempt++
            val ok = try { cm.bindProcessToNetwork(network) } catch (_: Throwable) { false }
            if (ok) {
                bound = true
                Log.i(TAG, "bound to the camera network after $attempt attempt(s)")
                listener.onConnected(ssid)
                return
            }
            if (attempt >= BIND_ATTEMPTS || boundNetwork != network) {
                bound = false
                Log.w(TAG, "could not bind to the camera network after $attempt attempts")
                // Still report connected: the link exists, and the wizard's
                // diagnostic line says the bind failed rather than hiding it.
                listener.onConnected(ssid)
                return
            }
            handler.postDelayed({ tryBind() }, BIND_RETRY_MS)
        }
        tryBind()
    }

    /** Re-run the bind on demand, for a retry button or a later reconnect. */
    fun rebind(): Boolean {
        val net = boundNetwork ?: return false
        val ok = try { cm.bindProcessToNetwork(net) } catch (_: Throwable) { false }
        bound = ok
        return ok
    }

    /**
     * Which side is at fault when the camera does not answer.
     *
     * If this says the phone holds a 10.5.5.x address on the camera's
     * interface, the link is good and the camera is not serving, which is a
     * camera-side problem (it is usually sitting in a menu). If there is no
     * such address, or the process is not bound, the fault is here.
     */
    fun linkSummary(): String {
        val net = boundNetwork ?: return "not joined to any camera network"
        val parts = mutableListOf("process bound: $bound")
        try {
            val lp = cm.getLinkProperties(net)
            parts.add("interface: " + (lp?.interfaceName ?: "unknown"))
            val addrs = lp?.linkAddresses?.joinToString(", ") { it.address.hostAddress ?: "?" }
            parts.add("phone address: " + (if (addrs.isNullOrBlank()) "none" else addrs))
            parts.add("camera address: " + (cameraHost() ?: "unknown"))
            val onCameraSubnet = lp?.linkAddresses?.any {
                it.address.hostAddress?.startsWith("10.5.5.") == true
            } == true
            parts.add(if (onCameraSubnet)
                "on the camera's 10.5.5.x network, so the link is good"
            else
                "NOT on the usual 10.5.5.x network")
        } catch (t: Throwable) {
            parts.add("link details unavailable: ${t.javaClass.simpleName}")
        }
        return parts.joinToString("; ")
    }

    /**
     * The camera's own address on this link, or null.
     *
     * An access point is the DHCP server and the gateway for the network it
     * serves, so this is the camera, wherever it decided to put itself. It is
     * worth asking rather than hardcoding 10.5.5.9: that address is a
     * convention, and a camera that answers on another one would otherwise
     * look like a camera that is not there at all.
     */
    fun cameraHost(): String? {
        val net = boundNetwork ?: return null
        val lp = try { cm.getLinkProperties(net) } catch (_: Throwable) { null } ?: return null
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.R) {
            lp.dhcpServerAddress?.hostAddress?.let { return it }
        }
        for (r in lp.routes) {
            val gw = r.gateway?.hostAddress ?: continue
            if (gw != "0.0.0.0" && gw != "::" && !gw.contains(":")) return gw
        }
        return null
    }

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
                bindWithRetries(network, ssid ?: "GoPro", listener)
            }

            override fun onCapabilitiesChanged(network: Network, caps: NetworkCapabilities) {
                // The bind can be refused while the link is still being set
                // up. Capabilities arriving is the signal that it is ready.
                if (!bound && network == boundNetwork) {
                    if (cm.bindProcessToNetwork(network)) {
                        bound = true
                        Log.i(TAG, "bound on capabilities change")
                    }
                }
            }

            override fun onUnavailable() {
                Log.w(TAG, "camera network unavailable")
                listener.onUnavailable(
                    "Could not join the camera's WiFi. Is the phone's WiFi switched on, and " +
                    "did Quik wake the camera's WiFi first (a HERO9 only broadcasts after an " +
                    "app asks over Bluetooth)? If the camera is not named GP..., type its " +
                    "exact name from Camera Info."
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
                bindWithRetries(network, "current WiFi", listener)
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
        bound = false
        if (boundNetwork != null) {
            cm.bindProcessToNetwork(null)
            boundNetwork = null
        }
    }

    val isBound: Boolean get() = boundNetwork != null

    companion object {
        private const val TAG = "FlightPath.Wifi"
        // Five seconds of asking. The bind has been seen to fail outright on
        // the first call while the link was still coming up.
        private const val BIND_ATTEMPTS = 20
        private const val BIND_RETRY_MS = 250L
    }
}
