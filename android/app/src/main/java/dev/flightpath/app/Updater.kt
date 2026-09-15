package dev.flightpath.app

import android.content.Context
import android.content.Intent
import android.net.ConnectivityManager
import android.net.Network
import android.net.NetworkCapabilities
import android.net.NetworkRequest
import android.net.Uri
import android.util.Log
import org.json.JSONObject
import java.io.BufferedReader
import java.net.HttpURLConnection
import java.net.URL
import kotlin.concurrent.thread

/**
 * Asks the update server for the newest version and hands the APK download to
 * the system browser. The process may be bound to the camera's WiFi, which has
 * no internet, so the check deliberately grabs an internet-capable network
 * (cellular or home WiFi) and opens the connection over THAT.
 */
class Updater(private val context: Context) {

    data class Result(val ok: Boolean, val newer: Boolean, val versionName: String,
                      val versionCode: Int, val message: String, val apkUrl: String)

    private val cm = context.getSystemService(Context.CONNECTIVITY_SERVICE) as ConnectivityManager

    fun check(baseUrl: String, currentCode: Int, cb: (Result) -> Unit) {
        val base = baseUrl.trimEnd('/')
        if (base.isBlank()) {
            cb(Result(false, false, "", 0, "No update server set.", ""))
            return
        }
        withInternet { net ->
            thread {
                try {
                    val url = URL("$base/version.json")
                    val conn = (net?.openConnection(url) ?: url.openConnection()) as HttpURLConnection
                    conn.connectTimeout = 8000; conn.readTimeout = 8000
                    val body = conn.inputStream.bufferedReader().use(BufferedReader::readText)
                    val j = JSONObject(body)
                    val code = j.optInt("versionCode", 0)
                    val name = j.optString("versionName", "?")
                    val apk = base + j.optString("apk", "/FlightPath.apk")
                    cb(Result(true, code > currentCode, name, code,
                        if (code > currentCode) "Version $name is available." else "You have the latest ($name).",
                        apk))
                } catch (t: Throwable) {
                    Log.w(TAG, "update check failed", t)
                    cb(Result(false, false, "", 0, "Could not reach the update server: ${t.javaClass.simpleName}", ""))
                }
            }
        }
    }

    /** Opens the APK in the browser. Chrome downloads it over whatever has internet. */
    fun download(apkUrl: String) {
        val i = Intent(Intent.ACTION_VIEW, Uri.parse(apkUrl)).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
        context.startActivity(i)
    }

    /** Find a network with internet, even if this process is bound to one without. */
    private fun withInternet(block: (Network?) -> Unit) {
        val req = NetworkRequest.Builder()
            .addCapability(NetworkCapabilities.NET_CAPABILITY_INTERNET)
            .addCapability(NetworkCapabilities.NET_CAPABILITY_VALIDATED)
            .build()
        var done = false
        val cb = object : ConnectivityManager.NetworkCallback() {
            override fun onAvailable(network: Network) {
                if (done) return
                done = true
                try { cm.unregisterNetworkCallback(this) } catch (_: Exception) {}
                block(network)
            }
            override fun onUnavailable() {
                if (done) return
                done = true
                block(null)
            }
        }
        try {
            cm.requestNetwork(req, cb, 5000)
        } catch (t: Throwable) {
            block(null)
        }
    }

    companion object { private const val TAG = "FlightPath.Update" }
}
