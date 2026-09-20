package dev.flightpath.app

import android.Manifest
import android.annotation.SuppressLint
import android.bluetooth.BluetoothAdapter
import android.bluetooth.BluetoothDevice
import android.bluetooth.BluetoothGatt
import android.bluetooth.BluetoothGattCallback
import android.bluetooth.BluetoothGattCharacteristic
import android.bluetooth.BluetoothGattDescriptor
import android.bluetooth.BluetoothManager
import android.bluetooth.le.ScanCallback
import android.bluetooth.le.ScanResult
import android.bluetooth.le.ScanSettings
import android.content.Context
import android.content.pm.PackageManager
import android.os.Build
import android.os.Handler
import android.os.Looper
import android.util.Log
import androidx.core.content.ContextCompat
import java.util.UUID

/**
 * Wakes the camera's WiFi over Bluetooth.
 *
 * A HERO9 does not broadcast WiFi on its own. GoPro's own documentation is
 * explicit: "camera WiFi must be enabled upon each connection via BLE." The
 * Wireless Connections switch and the Connect Device screen only turn on
 * Bluetooth advertising. Until this class existed, the only thing that could
 * send that request was GoPro's Quik app, so every session started by opening
 * a second app, which goal 1 (one APK, no other tools) does not allow.
 *
 * The exchange, from GoPro's published tutorial code:
 *   1. Find a device advertising service FEA6.
 *   2. Connect, discover services.
 *   3. Read the WiFi SSID and password characteristics.
 *   4. Subscribe to the command response characteristic.
 *   5. Write 03:17:01:01 to the command request characteristic.
 *      (length 3, command 0x17 "AP control", 1 byte of payload, value 1 = on)
 *   6. A response whose third byte is 0x00 means the WiFi is coming up.
 *
 * Android allows one GATT operation at a time, so those steps are a state
 * machine driven by the callbacks rather than straight-line code.
 *
 * The camera must be bonded to this phone once. That happens by itself on the
 * first read of a protected characteristic, with the camera on Preferences,
 * Connections, Connect Device, GoPro App. After that it reconnects silently.
 */
class CameraBle(private val context: Context) {

    interface Listener {
        /** One line for the wizard, so a slow wake does not look like a hang. */
        fun onProgress(message: String)
        fun onWoken(ssid: String, password: String)
        fun onFailed(reason: String)
    }

    private val main = Handler(Looper.getMainLooper())
    private val manager by lazy {
        context.getSystemService(Context.BLUETOOTH_SERVICE) as BluetoothManager?
    }
    private val adapter: BluetoothAdapter? get() = manager?.adapter

    private var gatt: BluetoothGatt? = null
    private var listener: Listener? = null
    private var scanning = false
    private var finished = false
    private var ssid = ""
    private var password = ""

    private val scanCallback = object : ScanCallback() {
        override fun onScanResult(callbackType: Int, result: ScanResult) {
            val rec = result.scanRecord
            val byService = rec?.serviceUuids?.any { it.uuid == SERVICE } == true
            val name = try { rec?.deviceName ?: result.device.name } catch (_: SecurityException) { null }
            val byName = name != null &&
                (name.startsWith("GoPro", true) || GP_NAME.matches(name))
            if (byService || byName) {
                stopScan()
                report("Found ${name ?: "the camera"}. Connecting.")
                connect(result.device)
            }
        }

        override fun onScanFailed(errorCode: Int) {
            fail("Bluetooth scan failed (code $errorCode). Turn Bluetooth off and on again.")
        }
    }

    private val gattCallback = object : BluetoothGattCallback() {

        override fun onConnectionStateChange(g: BluetoothGatt, status: Int, newState: Int) {
            if (newState == BluetoothGatt.STATE_CONNECTED) {
                report("Connected. Reading the camera's WiFi details.")
                main.postDelayed({ discover(g) }, 600)   // settle before discovery
            } else if (newState == BluetoothGatt.STATE_DISCONNECTED && !finished) {
                fail(if (status == 133)
                    "Bluetooth dropped the connection. Put the camera on Preferences, " +
                        "Connections, Connect Device, GoPro App and try once more."
                else "Bluetooth disconnected (status $status).")
            }
        }

        override fun onServicesDiscovered(g: BluetoothGatt, status: Int) {
            if (status != BluetoothGatt.GATT_SUCCESS) {
                fail("Could not read the camera's Bluetooth services (status $status).")
                return
            }
            if (!readChar(g, WIFI_SSID)) {
                fail("This device does not look like a GoPro: no WiFi name characteristic.")
            }
        }

        // API 33 and later call this one; older devices call the deprecated
        // overload below. Neither chains to the other, so each is handled once.
        override fun onCharacteristicRead(
            g: BluetoothGatt, c: BluetoothGattCharacteristic, value: ByteArray, status: Int
        ) = handleRead(g, c, value, status)

        @Deprecated("Called on API 32 and below", ReplaceWith(""))
        @Suppress("DEPRECATION")
        override fun onCharacteristicRead(
            g: BluetoothGatt, c: BluetoothGattCharacteristic, status: Int
        ) = handleRead(g, c, c.value ?: ByteArray(0), status)

        override fun onDescriptorWrite(
            g: BluetoothGatt, d: BluetoothGattDescriptor, status: Int
        ) {
            // Subscribed to responses. Now ask for the WiFi.
            report("Asking the camera to switch its WiFi on.")
            if (!writeCommand(g)) fail("Could not send the WiFi command to the camera.")
        }

        override fun onCharacteristicChanged(
            g: BluetoothGatt, c: BluetoothGattCharacteristic, value: ByteArray
        ) = handleResponse(value)

        @Deprecated("Called on API 32 and below", ReplaceWith(""))
        @Suppress("DEPRECATION")
        override fun onCharacteristicChanged(g: BluetoothGatt, c: BluetoothGattCharacteristic) =
            handleResponse(c.value ?: ByteArray(0))
    }

    // ---------- the sequence ----------

    /** Scan, connect, read the credentials, turn the WiFi on. */
    @SuppressLint("MissingPermission")     // checked in missingPermission()
    fun wake(listener: Listener) {
        cancel()
        this.listener = listener
        finished = false
        ssid = ""
        password = ""

        val missing = missingPermission()
        if (missing != null) {
            fail(missing)
            return
        }
        val ad = adapter
        if (ad == null || !ad.isEnabled) {
            fail("The phone's Bluetooth is off. Turn it on and try again. " +
                "A GoPro only switches its WiFi on when asked over Bluetooth.")
            return
        }
        val scanner = ad.bluetoothLeScanner
        if (scanner == null) {
            fail("This phone has no Bluetooth LE scanner.")
            return
        }

        report("Looking for the camera over Bluetooth.")
        scanning = true
        // Unfiltered, matching on either the GoPro service or the name: some
        // firmware advertises the service UUID only in the scan response.
        val settings = ScanSettings.Builder()
            .setScanMode(ScanSettings.SCAN_MODE_LOW_LATENCY)
            .build()
        scanner.startScan(null, settings, scanCallback)

        main.postDelayed({
            if (scanning) {
                stopScan()
                fail("No GoPro answered over Bluetooth in 15 seconds. Turn the camera on, " +
                    "then put it on Preferences, Connections, Connect Device, GoPro App " +
                    "and try again.")
            }
        }, SCAN_MS)
        main.postDelayed({ if (!finished) fail("The camera did not finish waking up in time.") },
            OVERALL_MS)
    }

    fun cancel() {
        stopScan()
        closeGatt()
        listener = null
        finished = false
    }

    @SuppressLint("MissingPermission")
    private fun connect(device: BluetoothDevice) {
        closeGatt()
        gatt = device.connectGatt(context, false, gattCallback, BluetoothDevice.TRANSPORT_LE)
    }

    @SuppressLint("MissingPermission")
    private fun discover(g: BluetoothGatt) {
        if (!g.discoverServices()) fail("Could not start Bluetooth service discovery.")
    }

    @SuppressLint("MissingPermission")
    private fun readChar(g: BluetoothGatt, uuid: UUID): Boolean {
        val c = findChar(g, uuid) ?: return false
        return g.readCharacteristic(c)
    }

    private fun handleRead(
        g: BluetoothGatt, c: BluetoothGattCharacteristic, value: ByteArray, status: Int
    ) {
        if (status != BluetoothGatt.GATT_SUCCESS) {
            fail(if (status == BluetoothGatt.GATT_INSUFFICIENT_AUTHENTICATION ||
                     status == BluetoothGatt.GATT_INSUFFICIENT_ENCRYPTION)
                "The camera refused until it is paired with this phone. On the camera: " +
                    "Preferences, Connections, Connect Device, GoPro App, then try again " +
                    "and accept the pairing request."
            else "Could not read the camera's WiFi details (status $status).")
            return
        }
        when (c.uuid) {
            WIFI_SSID -> {
                ssid = value.decodeToString().trim()
                if (!readChar(g, WIFI_PASSWORD)) fail("No WiFi password characteristic.")
            }
            WIFI_PASSWORD -> {
                password = value.decodeToString().trim()
                subscribe(g)
            }
        }
    }

    /** Listen for command responses, which is a descriptor write. */
    @SuppressLint("MissingPermission")
    private fun subscribe(g: BluetoothGatt) {
        val rsp = findChar(g, COMMAND_RSP)
        if (rsp == null) {
            fail("No command response characteristic on this device.")
            return
        }
        g.setCharacteristicNotification(rsp, true)
        val cccd = rsp.getDescriptor(CCCD)
        if (cccd == null) {
            // Unusual, but the command alone is worth trying.
            report("Asking the camera to switch its WiFi on.")
            if (!writeCommand(g)) fail("Could not send the WiFi command to the camera.")
            return
        }
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            g.writeDescriptor(cccd, BluetoothGattDescriptor.ENABLE_NOTIFICATION_VALUE)
        } else {
            @Suppress("DEPRECATION")
            cccd.value = BluetoothGattDescriptor.ENABLE_NOTIFICATION_VALUE
            @Suppress("DEPRECATION")
            g.writeDescriptor(cccd)
        }
    }

    /** 03:17:01:01 = length 3, command 0x17 (AP control), 1 byte payload, on. */
    @SuppressLint("MissingPermission")
    private fun writeCommand(g: BluetoothGatt): Boolean {
        val req = findChar(g, COMMAND_REQ) ?: return false
        val bytes = byteArrayOf(0x03, 0x17, 0x01, 0x01)
        return if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            g.writeCharacteristic(req, bytes, BluetoothGattCharacteristic.WRITE_TYPE_DEFAULT) ==
                BluetoothGatt.GATT_SUCCESS
        } else {
            @Suppress("DEPRECATION")
            req.value = bytes
            @Suppress("DEPRECATION")
            req.writeType = BluetoothGattCharacteristic.WRITE_TYPE_DEFAULT
            @Suppress("DEPRECATION")
            g.writeCharacteristic(req)
        }
    }

    /** Response format: [length, command id, status, ...]. Status 0 is success. */
    private fun handleResponse(value: ByteArray) {
        if (value.size < 3) return
        if (value[1] != 0x17.toByte()) return          // some other command's reply
        if (value[2] == 0x00.toByte()) {
            if (ssid.isBlank()) {
                fail("The camera turned its WiFi on but did not give its network name.")
                return
            }
            succeed()
        } else {
            fail("The camera refused to turn its WiFi on (status ${value[2].toInt()}).")
        }
    }

    // ---------- plumbing ----------

    private fun findChar(g: BluetoothGatt, uuid: UUID): BluetoothGattCharacteristic? {
        for (s in g.services) {
            s.getCharacteristic(uuid)?.let { return it }
        }
        return null
    }

    /** Null when everything needed is granted, otherwise what to tell the user. */
    fun missingPermission(): String? {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
            val needed = listOf(Manifest.permission.BLUETOOTH_SCAN,
                                Manifest.permission.BLUETOOTH_CONNECT)
            if (needed.any {
                    ContextCompat.checkSelfPermission(context, it) !=
                        PackageManager.PERMISSION_GRANTED
                }) {
                return "FlightPath needs the Nearby devices permission to wake the camera's " +
                    "WiFi over Bluetooth. It is used for nothing else."
            }
        }
        return null
    }

    @SuppressLint("MissingPermission")
    private fun stopScan() {
        if (!scanning) return
        scanning = false
        try { adapter?.bluetoothLeScanner?.stopScan(scanCallback) } catch (_: Exception) {}
    }

    @SuppressLint("MissingPermission")
    private fun closeGatt() {
        try { gatt?.disconnect() } catch (_: Exception) {}
        try { gatt?.close() } catch (_: Exception) {}
        gatt = null
    }

    private fun report(msg: String) {
        val l = listener ?: return
        main.post { l.onProgress(msg) }
    }

    private fun succeed() {
        if (finished) return
        finished = true
        stopScan()
        val l = listener
        val s = ssid
        val p = password
        // The camera needs a moment to bring the access point up before the
        // phone can join it.
        main.postDelayed({
            closeGatt()
            l?.onWoken(s, p)
        }, 2500)
    }

    private fun fail(reason: String) {
        if (finished) return
        finished = true
        Log.w(TAG, "ble wake failed: $reason")
        stopScan()
        closeGatt()
        val l = listener
        main.post { l?.onFailed(reason) }
    }

    private companion object {
        const val TAG = "FlightPathBle"
        const val SCAN_MS = 15_000L
        const val OVERALL_MS = 45_000L

        // From GoPro's published Open GoPro tutorial source.
        val SERVICE: UUID = UUID.fromString("0000fea6-0000-1000-8000-00805f9b34fb")
        val COMMAND_REQ: UUID = UUID.fromString("b5f90072-aa8d-11e3-9046-0002a5d5c51b")
        val COMMAND_RSP: UUID = UUID.fromString("b5f90073-aa8d-11e3-9046-0002a5d5c51b")
        val WIFI_SSID: UUID = UUID.fromString("b5f90002-aa8d-11e3-9046-0002a5d5c51b")
        val WIFI_PASSWORD: UUID = UUID.fromString("b5f90003-aa8d-11e3-9046-0002a5d5c51b")
        val CCCD: UUID = UUID.fromString("00002902-0000-1000-8000-00805f9b34fb")

        val GP_NAME = Regex("^GP\\d{4,}$", RegexOption.IGNORE_CASE)
    }
}
