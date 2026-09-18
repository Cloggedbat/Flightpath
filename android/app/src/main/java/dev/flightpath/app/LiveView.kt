package dev.flightpath.app

import android.app.Activity
import android.util.Log
import android.view.View
import android.widget.TextView
import androidx.media3.common.MediaItem
import androidx.media3.common.PlaybackException
import androidx.media3.common.Player
import androidx.media3.common.util.UnstableApi
import androidx.media3.datasource.DataSource
import androidx.media3.exoplayer.DefaultLoadControl
import androidx.media3.exoplayer.ExoPlayer
import androidx.media3.exoplayer.source.ProgressiveMediaSource
import androidx.media3.extractor.DefaultExtractorsFactory
import androidx.media3.extractor.ts.DefaultTsPayloadReaderFactory
import androidx.media3.extractor.ts.TsExtractor
import androidx.media3.ui.PlayerView

/**
 * Plays the GoPro's preview stream. The camera pushes MPEG-TS over UDP to
 * port 8554 of whoever asked for it (the Python side asks, over the bound
 * camera network, so the packets land on this process). Media3 decodes with
 * the phone's hardware H.264 decoder. Low resolution by design: this is for
 * aiming the tripod, not for measuring anything.
 */
@UnstableApi
class LiveView(private val activity: Activity, private val onEvent: (String, String) -> Unit) {

    private val overlay: View = activity.findViewById(R.id.liveOverlay)
    private val playerView: PlayerView = activity.findViewById(R.id.player)
    private val guides: GuideView = activity.findViewById(R.id.guides)
    private val status: TextView = activity.findViewById(R.id.liveStatus)
    private var player: ExoPlayer? = null

    init {
        activity.findViewById<View>(R.id.liveClose).setOnClickListener { hide("closed") }
    }

    val isOpen: Boolean get() = overlay.visibility == View.VISIBLE

    fun show(port: Int, leftHanded: Boolean) {
        hide(null)
        guides.leftHanded = leftHanded
        status.text = "Waiting for the camera’s picture…"
        overlay.visibility = View.VISIBLE
        activity.window.addFlags(android.view.WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)

        // Small buffers: we want the picture now, not smooth.
        val loadControl = DefaultLoadControl.Builder()
            .setBufferDurationsMs(250, 2_000, 150, 250)
            .setPrioritizeTimeOverSizeThresholds(true)
            .build()
        // Not UdpDataSource: the HERO9 puts a 12 byte header in front of the
        // TS packets in every datagram, and TsExtractor never syncs past it.
        val udp = DataSource.Factory { TsUdpDataSource(SOCKET_TIMEOUT_MS) }
        val extractors = DefaultExtractorsFactory()
            .setTsExtractorMode(TsExtractor.MODE_SINGLE_PMT)
            .setTsExtractorFlags(
                DefaultTsPayloadReaderFactory.FLAG_ALLOW_NON_IDR_KEYFRAMES or
                DefaultTsPayloadReaderFactory.FLAG_DETECT_ACCESS_UNITS or
                DefaultTsPayloadReaderFactory.FLAG_IGNORE_AAC_STREAM
            )
        val source = ProgressiveMediaSource.Factory(udp, extractors)
            .createMediaSource(MediaItem.fromUri("udp://0.0.0.0:$port"))

        val p = ExoPlayer.Builder(activity).setLoadControl(loadControl).build()
        p.addListener(object : Player.Listener {
            override fun onRenderedFirstFrame() {
                status.text = "Live · square the lens to the target line"
                onEvent("playing", "")
            }
            override fun onPlayerError(error: PlaybackException) {
                Log.w(TAG, "live view error", error)
                val why = if (error.cause is java.net.SocketTimeoutException)
                    "No picture arrived. Is the camera idle (not recording) and on the same WiFi?"
                else "Could not play the stream: ${error.errorCodeName}"
                status.text = why
                onEvent("error", why)
            }
            override fun onPlaybackStateChanged(state: Int) {
                if (state == Player.STATE_ENDED) hide("ended")
            }
        })
        playerView.player = p
        p.setMediaSource(source)
        p.playWhenReady = true
        p.prepare()
        player = p
        onEvent("open", "")
    }

    fun hide(reason: String?) {
        player?.let { it.stop(); it.release() }
        player = null
        playerView.player = null
        if (overlay.visibility == View.VISIBLE) {
            overlay.visibility = View.GONE
            activity.window.clearFlags(android.view.WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
            if (reason != null) onEvent(reason, "")
        }
    }

    companion object {
        private const val TAG = "FlightPath.Live"
        private const val SOCKET_TIMEOUT_MS = 8_000
    }
}
