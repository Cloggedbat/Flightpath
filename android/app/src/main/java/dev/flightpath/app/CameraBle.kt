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
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicInteger
import java.util.concurrent.atomic.AtomicReference

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
    /** Whether the GATT link is actually up. `gatt` stays non-null after a
     *  drop, and its cached services still answer, so without this isReady()
     *  would happily report a dead link as usable. */
    @Volatile private var linkUp = false
    private var ssid = ""
    private var password = ""

    // Replies to a command we sent and are waiting on, by command id.
    private var lastDevice: BluetoothDevice? = null
    /** Set while reconnect() is waiting for the link to become usable. */
    private val readyLatch = AtomicReference<CountDownLatch?>(null)
    private val cmdLatch = AtomicReference<CountDownLatch?>(null)
    private val cmdId = AtomicInteger(-1)
    private val cmdStatus = AtomicInteger(-1)

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
                linkUp = true
                report("Connected. Reading the camera's WiFi details.")
                main.postDelayed({ discover(g) }, 600)   // settle before discovery
            } else if (newState == BluetoothGatt.STATE_DISCONNECTED) {
                linkUp = false
                // A drop after the wake finished is not an error to report,
                // but it does mean the shutter can no longer go this way.
                if (!finished) fail(if (status == 133)
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
            if (readyLatch.get() != null) {
                subscribeOnly(g)               // reconnect: commands only
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
            readyLatch.get()?.let {
                it.countDown()                 // reconnect finished, link usable
                return
            }
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
        lastDevice = device
        gatt = device.connectGatt(context, false, gattCallback, BluetoothDevice.TRANSPORT_LE)
    }

    /**
     * Bring the command link back without redoing the whole wake.
     *
     * A GATT link does not last forever: the camera or Android can drop it
     * between one use and the next. When that happened, the shutter fell
     * back to the WiFi path, which does nothing on this camera, so the user
     * saw a timeout and heard no beep. Reconnecting is cheap, so do it
     * rather than fail. Blocking, because the engine's worker thread wants
     * an answer before it fires the shutter.
     */
    @SuppressLint("MissingPermission")
    fun reconnect(timeoutMs: Long = 12_000): Boolean {
        if (isReady()) return true
        val dev = lastDevice ?: return false
        val latch = CountDownLatch(1)
        readyLatch.set(latch)
        // Stop the wake state machine reporting anything for this pass.
        finished = true
        main.post {
            closeGatt()
            gatt = dev.connectGatt(context, false, gattCallback, BluetoothDevice.TRANSPORT_LE)
        }
        val signalled = try {
            latch.await(timeoutMs, TimeUnit.MILLISECONDS)
        } catch (_: InterruptedException) {
            false
        } finally {
            readyLatch.set(null)
        }
        return signalled && isReady()
    }

    /** Subscribe to command responses and nothing else: a reconnect does not
     *  need the WiFi credentials again. */
    @SuppressLint("MissingPermission")
    private fun subscribeOnly(g: BluetoothGatt) {
        val rsp = findChar(g, COMMAND_RSP)
        if (rsp == null) {
            readyLatch.get()?.countDown()
            return
        }
        g.setCharacteristicNotification(rsp, true)
        val cccd = rsp.getDescriptor(CCCD)
        if (cccd == null) {
            readyLatch.get()?.countDown()
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
        // Anything another thread is blocked waiting for.
        val waiting = cmdId.get()
        if (waiting >= 0 && value[1] == waiting.toByte()) {
            cmdStatus.set(value[2].toInt())
            cmdLatch.get()?.countDown()
            return
        }
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

    // ---------- commands over the open link ----------

    /** Is the Bluetooth link still up and usable for commands? */
    fun isReady(): Boolean {
        if (!linkUp) return false
        val g = gatt ?: return false
        return findChar(g, COMMAND_REQ) != null
    }

    /** Start recording over Bluetooth. Empty string on success, else why not. */
    fun shutterStart(): String = sendCommand(0x01, byteArrayOf(0x03, 0x01, 0x01, 0x01))

    /** Stop recording over Bluetooth. Empty string on success, else why not. */
    fun shutterStop(): String = sendCommand(0x01, byteArrayOf(0x03, 0x01, 0x01, 0x00))

    /**
     * Send one command and wait for the camera's reply.
     *
     * The HERO9's WiFi shutter is a dead end. GoPro deprecated those control
     * commands from this model on, every HTTP attempt has timed out, and what
     * the camera leaves behind is a 27,639 byte stub, the same size whether
     * the battery is at 18 percent or 62. Bluetooth is the supported path and
     * the link is already open from waking the WiFi.
     *
     * Blocking on purpose: the engine calls this from its worker thread and
     * wants to know whether the camera accepted the command.
     */
    @SuppressLint("MissingPermission")
    private fun sendCommand(id: Int, bytes: ByteArray, timeoutMs: Long = 6000): String {
        val g = gatt ?: return "no Bluetooth connection to the camera"
        val req = findChar(g, COMMAND_REQ) ?: return "no command characteristic on the camera"
        synchronized(this) {
            val latch = CountDownLatch(1)
            cmdId.set(id)
            cmdStatus.set(-1)
            cmdLatch.set(latch)
            try {
                val wrote = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
                    g.writeCharacteristic(req, bytes,
                        BluetoothGattCharacteristic.WRITE_TYPE_DEFAULT) == BluetoothGatt.GATT_SUCCESS
                } else {
                    @Suppress("DEPRECATION")
                    req.value = bytes
                    @Suppress("DEPRECATION")
                    req.writeType = BluetoothGattCharacteristic.WRITE_TYPE_DEFAULT
                    @Suppress("DEPRECATION")
                    g.writeCharacteristic(req)
                }
                if (!wrote) return "could not write the command over Bluetooth"
                if (!latch.await(timeoutMs, TimeUnit.MILLISECONDS)) {
                    return "the camera did not reply over Bluetooth in ${timeoutMs / 1000} s"
                }
                val st = cmdStatus.get()
                return if (st == 0) "" else "the camera refused the command (status $st)"
            } catch (t: Throwable) {
                return "${t.javaClass.simpleName}: ${t.message}"
            } finally {
                cmdId.set(-1)
                cmdLatch.set(null)
            }
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
        linkUp = false
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
        // phone can join it. The GATT connection stays OPEN: the shutter goes
        // over Bluetooth, because this camera's WiFi shutter does not work.
        main.postDelayed({ l?.onWoken(s, p) }, 2500)
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
