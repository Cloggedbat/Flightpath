package dev.flightpath.app

import android.media.MediaCodec
import android.media.MediaCodecInfo
import android.media.MediaExtractor
import android.media.MediaFormat

/**
 * Luma-only clip decoder on the phone's own hardware video decoder.
 *
 * Why this class exists. The OpenCV that ships inside the APK is 4.5.1.48,
 * the only build Chaquopy has an Android wheel for. Its Video I/O section is
 * empty except for one backend, ANDROID_MEDIANDK, and that backend turns a
 * decoded frame into a Mat only when the decoder hands it colour format 19
 * (YUV420Planar) or 21 (YUV420SemiPlanar). Qualcomm and Samsung decoders
 * return a vendor format instead, so retrieveFrame logs "Unsupported video
 * format" and cv2.VideoCapture.read() returns false. The file opens, then
 * every frame fails. That happens for H.264 and HEVC alike, so changing the
 * camera's Video Compression setting cannot fix it.
 *
 * MediaCodec.getOutputImage() avoids the whole problem. It normalises any
 * vendor layout into planes with honest row strides, and carries a crop rect,
 * which matters at 1080p because decoders pad the height to 1088.
 *
 * Only the Y plane is returned. Every consumer in the engine converts to
 * grayscale as its first act, so colour is decoded and thrown away otherwise.
 *
 * Usage from Python (Chaquopy):
 *     d = jclass("dev.flightpath.app.ClipDecoder")()
 *     d.open(path); w = d.getWidth(); buf = d.nextLuma()
 */
class ClipDecoder {

    private var extractor: MediaExtractor? = null
    private var codec: MediaCodec? = null
    private val info = MediaCodec.BufferInfo()

    private var inputDone = false
    private var outputDone = false

    /** Frame size in pixels, from the decoder's crop rect. Valid after the first frame. */
    var width: Int = 0
        private set
    var height: Int = 0
        private set

    /** Nominal rate from the container. 0 when the container does not say. */
    var frameRate: Double = 0.0
        private set

    /** Track mime, for diagnostics: "video/avc" is H.264, "video/hevc" is HEVC. */
    var mime: String = ""
        private set

    /** Name of the decoder the platform chose, for diagnostics. */
    var decoderName: String = ""
        private set

    /**
     * Presentation timestamps, which give a true frame rate without trusting
     * the container. A GoPro writes 239.76 fps as often as 240.
     */
    var firstPtsUs: Long = -1
        private set
    var lastPtsUs: Long = -1
        private set
    var framesDecoded: Int = 0
        private set

    /** Empty unless something failed, in which case it says what. */
    var error: String = ""
        private set

    /** Opens the clip. Returns false and sets [error] if it cannot. */
    fun open(path: String): Boolean {
        close()
        try {
            val ex = MediaExtractor()
            ex.setDataSource(path)
            var track = -1
            var format: MediaFormat? = null
            for (i in 0 until ex.trackCount) {
                val f = ex.getTrackFormat(i)
                val m = f.getString(MediaFormat.KEY_MIME) ?: continue
                if (m.startsWith("video/")) {
                    track = i
                    format = f
                    mime = m
                    break
                }
            }
            if (track < 0 || format == null) {
                ex.release()
                error = "no video track in the file"
                return false
            }
            ex.selectTrack(track)

            if (format.containsKey(MediaFormat.KEY_FRAME_RATE)) {
                // Written as an int by most muxers, as a float by some.
                frameRate = try {
                    format.getInteger(MediaFormat.KEY_FRAME_RATE).toDouble()
                } catch (_: ClassCastException) {
                    format.getFloat(MediaFormat.KEY_FRAME_RATE).toDouble()
                }
            }
            if (format.containsKey(MediaFormat.KEY_WIDTH)) width = format.getInteger(MediaFormat.KEY_WIDTH)
            if (format.containsKey(MediaFormat.KEY_HEIGHT)) height = format.getInteger(MediaFormat.KEY_HEIGHT)

            val c = MediaCodec.createDecoderByType(mime)
            // Ask for the flexible layout. This is what makes getOutputImage
            // give back sane planes whatever the vendor's native format is.
            format.setInteger(
                MediaFormat.KEY_COLOR_FORMAT,
                MediaCodecInfo.CodecCapabilities.COLOR_FormatYUV420Flexible
            )
            // No output Surface: we want the bytes, not a preview.
            c.configure(format, null, null, 0)
            c.start()
            decoderName = try { c.name } catch (_: Throwable) { "" }

            extractor = ex
            codec = c
            inputDone = false
            outputDone = false
            framesDecoded = 0
            firstPtsUs = -1
            lastPtsUs = -1
            error = ""
            return true
        } catch (t: Throwable) {
            error = "${t.javaClass.simpleName}: ${t.message}"
            close()
            return false
        }
    }

    /**
     * Decodes the next frame and returns its luma plane, width * height bytes,
     * row major, no padding. Returns null at end of stream or on error.
     */
    fun nextLuma(): ByteArray? {
        val c = codec ?: return null
        val ex = extractor ?: return null
        try {
            while (!outputDone) {
                if (!inputDone) {
                    val inIndex = c.dequeueInputBuffer(TIMEOUT_US)
                    if (inIndex >= 0) {
                        val buf = c.getInputBuffer(inIndex)
                        val size = if (buf == null) -1 else ex.readSampleData(buf, 0)
                        if (size < 0) {
                            c.queueInputBuffer(
                                inIndex, 0, 0, 0, MediaCodec.BUFFER_FLAG_END_OF_STREAM
                            )
                            inputDone = true
                        } else {
                            c.queueInputBuffer(inIndex, 0, size, ex.sampleTime, 0)
                            ex.advance()
                        }
                    }
                }

                val outIndex = c.dequeueOutputBuffer(info, TIMEOUT_US)
                when {
                    outIndex >= 0 -> {
                        if (info.flags and MediaCodec.BUFFER_FLAG_END_OF_STREAM != 0) {
                            outputDone = true
                        }
                        // A codec-config buffer carries no picture.
                        val isConfig = info.flags and MediaCodec.BUFFER_FLAG_CODEC_CONFIG != 0
                        if (info.size > 0 && !isConfig) {
                            val luma = copyLuma(c, outIndex)
                            c.releaseOutputBuffer(outIndex, false)
                            if (luma != null) {
                                if (firstPtsUs < 0) firstPtsUs = info.presentationTimeUs
                                lastPtsUs = info.presentationTimeUs
                                framesDecoded++
                                return luma
                            }
                        } else {
                            c.releaseOutputBuffer(outIndex, false)
                        }
                    }
                    outIndex == MediaCodec.INFO_OUTPUT_FORMAT_CHANGED -> {
                        val f = c.outputFormat
                        if (f.containsKey(MediaFormat.KEY_WIDTH)) width = f.getInteger(MediaFormat.KEY_WIDTH)
                        if (f.containsKey(MediaFormat.KEY_HEIGHT)) height = f.getInteger(MediaFormat.KEY_HEIGHT)
                    }
                    // INFO_TRY_AGAIN_LATER and the deprecated buffers-changed
                    // code both just mean "come round again".
                }
            }
            return null
        } catch (t: Throwable) {
            error = "${t.javaClass.simpleName}: ${t.message}"
            return null
        }
    }

    /**
     * Copies the Y plane out of the decoded image, honouring the row stride
     * and the crop rect. The crop rect is the reason this is not a straight
     * memcpy: a 1920x1080 clip decodes into a 1920x1088 buffer, because 1080
     * is not a multiple of 16.
     */
    private fun copyLuma(c: MediaCodec, index: Int): ByteArray? {
        val image = c.getOutputImage(index) ?: return null
        try {
            val crop = image.cropRect
            val w = crop.width()
            val h = crop.height()
            if (w <= 0 || h <= 0) return null
            val plane = image.planes[0]
            val rowStride = plane.rowStride
            val pixelStride = plane.pixelStride
            val src = plane.buffer
            val out = ByteArray(w * h)
            if (pixelStride == 1) {
                val row = ByteArray(w)
                for (y in 0 until h) {
                    src.position((crop.top + y) * rowStride + crop.left)
                    src.get(row, 0, w)
                    System.arraycopy(row, 0, out, y * w, w)
                }
            } else {
                // Rare, but a plane may be interleaved. Walk it pixel by pixel.
                for (y in 0 until h) {
                    var p = (crop.top + y) * rowStride + crop.left * pixelStride
                    val base = y * w
                    for (x in 0 until w) {
                        out[base + x] = src.get(p)
                        p += pixelStride
                    }
                }
            }
            width = w
            height = h
            return out
        } catch (t: Throwable) {
            error = "${t.javaClass.simpleName}: ${t.message}"
            return null
        } finally {
            image.close()
        }
    }

    /** True frame rate from the decoded timestamps, or 0 with too few frames. */
    fun measuredFps(): Double {
        if (framesDecoded < 2 || lastPtsUs <= firstPtsUs) return 0.0
        return (framesDecoded - 1) * 1_000_000.0 / (lastPtsUs - firstPtsUs)
    }

    fun close() {
        try { codec?.stop() } catch (_: Throwable) {}
        try { codec?.release() } catch (_: Throwable) {}
        try { extractor?.release() } catch (_: Throwable) {}
        codec = null
        extractor = null
        inputDone = false
        outputDone = true
    }

    private companion object {
        const val TIMEOUT_US = 10_000L
    }
}
