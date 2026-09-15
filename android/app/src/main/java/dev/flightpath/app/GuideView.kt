package dev.flightpath.app

import android.content.Context
import android.graphics.Canvas
import android.graphics.Color
import android.graphics.Paint
import android.util.AttributeSet
import android.view.View

/**
 * Aiming guides drawn over the live view: a lens-height line, and the box
 * where the ball should sit so it has 6 to 8 ft of frame to fly through.
 * Right-handed default: ball on the left, flight to the right. The page can
 * flip it with setLeftHanded().
 */
class GuideView @JvmOverloads constructor(
    context: Context, attrs: AttributeSet? = null
) : View(context, attrs) {

    private val line = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        color = Color.argb(180, 56, 193, 63); strokeWidth = 3f; style = Paint.Style.STROKE
    }
    private val faint = Paint(line).apply { color = Color.argb(90, 242, 247, 243); strokeWidth = 2f }
    private val text = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        color = Color.argb(220, 242, 247, 243); textSize = 34f
    }
    var leftHanded = false
        set(v) { field = v; invalidate() }

    override fun onDraw(c: Canvas) {
        val w = width.toFloat(); val h = height.toFloat()
        // The stream is 16:9 letterboxed inside this view; compute that box.
        val vh = minOf(h, w * 9f / 16f); val vw = vh * 16f / 9f
        val x0 = (w - vw) / 2f; val y0 = (h - vh) / 2f
        // Lens-height line: keep the ball a little below it.
        val ly = y0 + vh * 0.62f
        c.drawLine(x0, ly, x0 + vw, ly, faint)
        // Ball box: first fifth of the frame on the swing side.
        val bx = if (leftHanded) x0 + vw * 0.72f else x0 + vw * 0.10f
        c.drawRect(bx, ly - vh * 0.10f, bx + vw * 0.18f, ly + vh * 0.10f, line)
        c.drawText("ball here", bx, ly - vh * 0.12f, text)
        // Flight arrow across the rest of the frame.
        val ax0 = if (leftHanded) bx else bx + vw * 0.18f
        val ax1 = if (leftHanded) x0 + vw * 0.08f else x0 + vw * 0.92f
        c.drawLine(ax0, ly, ax1, ly, line)
        val d = if (leftHanded) -1f else 1f
        c.drawLine(ax1, ly, ax1 - d * 22f, ly - 14f, line)
        c.drawLine(ax1, ly, ax1 - d * 22f, ly + 14f, line)
        c.drawText("keep this space clear", ax0 + d * 30f - (if (leftHanded) 340f else 0f), ly + 50f, text)
    }
}
