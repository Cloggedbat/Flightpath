package dev.flightpath.app

import android.net.Uri
import androidx.media3.common.C
import androidx.media3.common.util.UnstableApi
import androidx.media3.datasource.BaseDataSource
import androidx.media3.datasource.DataSpec
import java.io.IOException
import java.net.DatagramPacket
import java.net.DatagramSocket
import java.net.InetSocketAddress
import java.net.SocketTimeoutException

/**
 * UDP source that hands Media3 only the MPEG-TS bytes of each datagram.
 *
 * Measured on the HERO9 with the wizard's camera test: every preview datagram
 * is 1328 bytes, which is a 12 byte header followed by seven 188 byte TS
 * packets. Media3's UdpDataSource passes the header through, so TsExtractor
 * sees 12 bytes of junk every 1316 bytes, loses sync, and the view stays
 * black. This finds the first 0x47 that repeats 188 bytes later and serves
 * from there, per datagram, so it does not depend on what the header is or
 * on its length staying the same.
 */
@UnstableApi
class TsUdpDataSource(private val timeoutMs: Int) : BaseDataSource(true) {

    private var socket: DatagramSocket? = null
    private var uri: Uri? = null
    private var opened = false
    private val buf = ByteArray(65_536)
    private val packet = DatagramPacket(buf, buf.size)
    private var pos = 0
    private var end = 0

    override fun open(dataSpec: DataSpec): Long {
        uri = dataSpec.uri
        transferInitializing(dataSpec)
        val port = dataSpec.uri.port
        if (port <= 0) throw IOException("live view uri has no port: ${dataSpec.uri}")
        val s = DatagramSocket(null)
        s.reuseAddress = true
        s.soTimeout = timeoutMs
        s.bind(InetSocketAddress(port))
        socket = s
        pos = 0
        end = 0
        opened = true
        transferStarted(dataSpec)
        return C.LENGTH_UNSET.toLong()
    }

    override fun read(target: ByteArray, offset: Int, length: Int): Int {
        if (length == 0) return 0
        val s = socket ?: throw IOException("live view source is not open")
        while (pos >= end) {
            packet.length = buf.size
            try {
                s.receive(packet)
            } catch (e: SocketTimeoutException) {
                throw IOException("no live view data for $timeoutMs ms", e)
            }
            val n = packet.length
            val start = tsOffset(buf, n)
            if (start < 0) continue                    // nothing TS-shaped here, skip it
            pos = start
            // Whole packets only; a ragged tail would desync the extractor.
            end = start + 188 * ((n - start) / 188)
        }
        val count = minOf(length, end - pos)
        System.arraycopy(buf, pos, target, offset, count)
        pos += count
        bytesTransferred(count)
        return count
    }

    override fun getUri(): Uri? = uri

    override fun close() {
        uri = null
        socket?.close()
        socket = null
        if (opened) {
            opened = false
            transferEnded()
        }
    }

    companion object {
        private const val SYNC = 0x47.toByte()

        /**
         * Offset of the first TS packet in a datagram of [n] bytes, or -1.
         * Same rule as Worker._ts_offset in the engine: a 0x47 within the
         * first 64 bytes that repeats 188 later, and 376 later when there is
         * room to check.
         */
        fun tsOffset(d: ByteArray, n: Int): Int {
            val limit = minOf(64, n - 188)
            for (o in 0 until limit) {
                if (d[o] != SYNC || d[o + 188] != SYNC) continue
                if (o + 376 < n && d[o + 376] != SYNC) continue
                return o
            }
            return -1
        }
    }
}
