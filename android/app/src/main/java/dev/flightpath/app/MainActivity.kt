package dev.flightpath.app

import android.annotation.SuppressLint
import android.content.Context
import android.os.Build
import android.os.Bundle
import android.util.Log
import android.view.View
import android.view.WindowManager
import android.webkit.JavascriptInterface
import android.webkit.WebChromeClient
import android.webkit.WebView
import android.webkit.WebViewClient
import android.widget.Toast
import androidx.activity.OnBackPressedCallback
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AppCompatActivity
import com.chaquo.python.Python
import com.chaquo.python.android.AndroidPlatform
import kotlin.concurrent.thread

/**
 * The whole app is: start the Python server, show its UI in a WebView, and
 * hand the WebView a tiny bridge so the "Connect camera" button can drive the
 * native WiFi binding. Everything else already exists in Python.
 */
@androidx.media3.common.util.UnstableApi
class MainActivity : AppCompatActivity() {

    private lateinit var web: WebView
    private lateinit var wifi: CameraWifi
    private lateinit var ble: CameraBle
    private lateinit var scanner: WifiScanner
    private lateinit var updater: Updater
    private lateinit var live: LiveView
    private val prefs by lazy { getSharedPreferences("flightpath", Context.MODE_PRIVATE) }

    /** Nearby devices, for waking the camera's WiFi over Bluetooth. */
    private val blePermission =
        registerForActivityResult(ActivityResultContracts.RequestMultiplePermissions()) { grants ->
            if (grants.values.all { it }) {
                startBleWake()
            } else {
                js("window.__nativeBle && window.__nativeBle('failed', " +
                    q("Without the Nearby devices permission the app cannot wake the camera's " +
                      "WiFi. You can still turn it on with the GoPro Quik app instead.") + ")")
            }
        }

    private val locationPermission =
        registerForActivityResult(ActivityResultContracts.RequestPermission()) { granted ->
            if (granted) {
                scanner.start()
                js("window.__nativeScan && window.__nativeScan('started')")
            } else {
                js("window.__nativeScan && window.__nativeScan('denied')")
            }
        }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_main)
        // The worker lives in this process. If the screen sleeps between
        // shots, Android freezes the app and the range session dies with it.
        window.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
        web = findViewById(R.id.web)
        wifi = CameraWifi(this)
        ble = CameraBle(this)
        scanner = WifiScanner(this)
        updater = Updater(this)
        live = LiveView(this) { state, msg ->
            js("window.__nativeLive && window.__nativeLive(${q(state)}, ${q(msg)})")
        }

        setupWebView()
        startPythonThenLoad()

        onBackPressedDispatcher.addCallback(this, object : OnBackPressedCallback(true) {
            override fun handleOnBackPressed() {
                if (live.isOpen) live.hide("closed")
                else if (web.canGoBack()) web.goBack()
                else finish()
            }
        })
    }

    @SuppressLint("SetJavaScriptEnabled")
    private fun setupWebView() {
        web.settings.apply {
            javaScriptEnabled = true
            domStorageEnabled = true
            cacheMode = android.webkit.WebSettings.LOAD_NO_CACHE
            mediaPlaybackRequiresUserGesture = false
        }
        web.webViewClient = WebViewClient()
        web.webChromeClient = WebChromeClient()
        web.addJavascriptInterface(Bridge(), "FlightPathNative")
        web.setBackgroundColor(0xFF12241A.toInt())
        web.visibility = View.INVISIBLE
    }

    private fun startPythonThenLoad() {
        thread(name = "flightpath-boot") {
            try {
                if (!Python.isStarted()) Python.start(AndroidPlatform(this))
                val py = Python.getInstance()
                val url = py.getModule("android_main")
                    .callAttr("start", filesDir.absolutePath)
                    .toString()
                runOnUiThread {
                    web.loadUrl(url)
                    web.visibility = View.VISIBLE
                }
            } catch (t: Throwable) {
                Log.e(TAG, "python failed to start", t)
                runOnUiThread {
                    Toast.makeText(this, "Engine failed to start: ${t.message}", Toast.LENGTH_LONG).show()
                }
            }
        }
    }

    /** What the page can call via window.FlightPathNative.* */
    inner class Bridge {
        @JavascriptInterface
        fun isNative(): Boolean = true

        @JavascriptInterface
        fun savedSsid(): String = prefs.getString("ssid", "") ?: ""

        /** Join the camera's WiFi for this app only. Empty ssid = any GP* network. */
        @JavascriptInterface
        fun connectCamera(ssid: String, password: String) {
            joinCameraWifi(ssid, password)
        }

        /**
         * Turn the camera's WiFi on over Bluetooth, then join it. A HERO9 does
         * not broadcast WiFi until an app asks; this is that request, so the
         * GoPro Quik app is not needed at all.
         */
        /** Which side is at fault when the camera does not answer. */
        @JavascriptInterface
        fun networkInfo(): String = wifi.linkSummary()

        @JavascriptInterface
        fun wakeCamera() {
            runOnUiThread {
                if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S &&
                    ble.missingPermission() != null) {
                    blePermission.launch(arrayOf(
                        android.Manifest.permission.BLUETOOTH_SCAN,
                        android.Manifest.permission.BLUETOOTH_CONNECT))
                } else {
                    startBleWake()
                }
            }
        }

        /** Reconnect with whatever was saved last time, no dialog if Android remembers it. */
        @JavascriptInterface
        fun reconnectSaved(): Boolean {
            val pw = prefs.getString("pw", "") ?: ""
            if (pw.isBlank()) return false
            connectCamera(prefs.getString("ssid", "") ?: "", pw)
            return true
        }

        /** Bind to whatever WiFi the phone already has, without joining anything. */
        @JavascriptInterface
        fun useCurrentWifi() {
            runOnUiThread {
                wifi.bindCurrentWifi(object : CameraWifi.Listener {
                    override fun onConnected(ssid: String) {
                        pyReconnect()
                        js("window.__nativeWifi && window.__nativeWifi('connected', ${q(ssid)})")
                    }
                    override fun onUnavailable(reason: String) {
                        js("window.__nativeWifi && window.__nativeWifi('unavailable', ${q(reason)})")
                    }
                    override fun onLost() {
                        js("window.__nativeWifi && window.__nativeWifi('lost', '')")
                    }
                })
            }
        }

        /** Start a WiFi scan. Asks for the location permission the first time. */
        @JavascriptInterface
        fun scanNetworks() {
            runOnUiThread {
                if (!scanner.wifiEnabled()) {
                    js("window.__nativeScan && window.__nativeScan('wifi_off')")
                } else if (scanner.hasPermission()) {
                    scanner.start()
                    js("window.__nativeScan && window.__nativeScan('started')")
                } else {
                    locationPermission.launch(android.Manifest.permission.ACCESS_FINE_LOCATION)
                }
            }
        }

        /** JSON array of {ssid, level, gopro}. Poll this a few seconds after scanNetworks(). */
        @JavascriptInterface
        fun scanResults(): String = scanner.results()

        @JavascriptInterface
        fun appVersion(): String = BuildConfig.VERSION_NAME + " (" + BuildConfig.VERSION_CODE + ")"

        @JavascriptInterface
        fun updateServer(): String = prefs.getString("update_url", BuildConfig.UPDATE_URL) ?: BuildConfig.UPDATE_URL

        @JavascriptInterface
        fun setUpdateServer(url: String) { prefs.edit().putString("update_url", url.trim()).apply() }

        /** Result arrives via window.__nativeUpdate(json). */
        @JavascriptInterface
        fun checkForUpdate() {
            updater.check(updateServer(), BuildConfig.VERSION_CODE) { r ->
                val j = org.json.JSONObject()
                j.put("ok", r.ok); j.put("newer", r.newer); j.put("versionName", r.versionName)
                j.put("versionCode", r.versionCode); j.put("message", r.message); j.put("apkUrl", r.apkUrl)
                js("window.__nativeUpdate && window.__nativeUpdate(" + j.toString() + ")")
            }
        }

        @JavascriptInterface
        fun downloadUpdate(url: String) { runOnUiThread { updater.download(url) } }

        /** Show the camera's live picture full screen. Events via window.__nativeLive(state, msg). */
        @JavascriptInterface
        fun showLiveView(port: Int, leftHanded: Boolean) {
            runOnUiThread { live.show(port, leftHanded) }
        }

        @JavascriptInterface
        fun hideLiveView() { runOnUiThread { live.hide("closed") } }

        @JavascriptInterface
        fun isLiveOpen(): Boolean = live.isOpen

        @JavascriptInterface
        fun disconnectCamera() {
            runOnUiThread { wifi.disconnect() }
        }

        @JavascriptInterface
        fun isBound(): Boolean = wifi.isBound
    }

    /** Join the camera's access point for this app only. */
    private fun joinCameraWifi(ssid: String, password: String) {
        prefs.edit().putString("ssid", ssid).putString("pw", password).apply()
        runOnUiThread {
            wifi.connect(ssid.ifBlank { null }, password, object : CameraWifi.Listener {
                override fun onConnected(ssid: String) {
                    pyReconnect()
                    js("window.__nativeWifi && window.__nativeWifi('connected', ${q(ssid)})")
                }
                override fun onUnavailable(reason: String) {
                    js("window.__nativeWifi && window.__nativeWifi('unavailable', ${q(reason)})")
                }
                override fun onLost() {
                    js("window.__nativeWifi && window.__nativeWifi('lost', '')")
                }
            })
        }
    }

    /**
     * The Bluetooth wake, then straight into the WiFi join: one tap from a
     * camera sitting there doing nothing to a connected engine. The password
     * never reaches the page; it comes off the camera and goes to the WiFi
     * layer inside this process.
     */
    private fun startBleWake() {
        ble.wake(object : CameraBle.Listener {
            override fun onProgress(message: String) {
                js("window.__nativeBle && window.__nativeBle('progress', ${q(message)})")
            }

            override fun onWoken(ssid: String, password: String) {
                js("window.__nativeBle && window.__nativeBle('woken', ${q(ssid)})")
                joinCameraWifi(ssid, password)
            }

            override fun onFailed(reason: String) {
                js("window.__nativeBle && window.__nativeBle('failed', ${q(reason)})")
            }
        })
    }

    private fun pyReconnect() {
        // Tell the engine where the camera actually is before it looks. The
        // access point is the gateway, so this is the camera's own address,
        // which beats assuming 10.5.5.9.
        val host = runCatching { wifi.cameraHost() }.getOrNull()
        thread {
            runCatching {
                val py = Python.getInstance().getModule("android_main")
                if (!host.isNullOrBlank()) py.callAttr("set_camera_host", host)
                py.callAttr("reconnect")
            }
        }
    }

    private fun js(code: String) = runOnUiThread { web.evaluateJavascript(code, null) }

    private fun q(s: String): String =
        "'" + s.replace("\\", "\\\\").replace("'", "\\'").replace("\n", " ") + "'"

    override fun onPause() {
        super.onPause()
        // Never leave a decoder running in the background.
        if (live.isOpen) live.hide("closed")
    }

    override fun onDestroy() {
        scanner.stop()
        ble.cancel()
        wifi.disconnect()
        runCatching { Python.getInstance().getModule("android_main").callAttr("stop") }
        super.onDestroy()
    }

    companion object {
        private const val TAG = "FlightPath"
    }
}
