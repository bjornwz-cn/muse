package com.bjornwz.earbuddy

import android.content.Context
import android.graphics.PixelFormat
import android.graphics.drawable.GradientDrawable
import android.os.Build
import android.os.Handler
import android.os.Looper
import android.provider.Settings
import android.view.Gravity
import android.view.View
import android.view.WindowManager
import android.widget.LinearLayout
import android.widget.TextView

/** 进程内桥：悬浮按钮/字幕条/翻译页 ↔ 正在运行的翻译会话（由 TranslateService 注册）。 */
object TranslateControl {
    @Volatile var stopper: (() -> Unit)? = null
    @Volatile var active: Boolean = false
    @Volatile var statusText: String = ""
    @Volatile var partialText: String = ""

    /** 已完成的双语条目（服务维护，翻译页打开时回放）。 */
    val entriesBuffer: MutableList<TranslateStore.Entry> =
        java.util.Collections.synchronizedList(mutableListOf())

    @Volatile var lineListener: ((src: String, dst: String) -> Unit)? = null
    @Volatile var partialListener: ((String) -> Unit)? = null
    @Volatile var statusListener: ((String) -> Unit)? = null
    @Volatile var levelListener: ((Int) -> Unit)? = null
    @Volatile var levelValue: Int = 0
    /** 本场开始时间（服务维护，翻译页据此显示「已进行时长」）；0 表示未在会话中。 */
    @Volatile var sessionStartedAt: Long = 0L
    /** 最近一次识别/引擎错误的可读原因（翻译页红字显示 + toast）；服务只在会话内发布。 */
    @Volatile var errorListener: ((String) -> Unit)? = null

    fun publishError(msg: String) {
        errorListener?.invoke(msg)
    }

    fun publishLevel(pct: Int) {
        levelValue = pct
        levelListener?.invoke(pct)
    }

    fun publishStatus(s: String) {
        statusText = s
        statusListener?.invoke(s)
    }

    fun publishPartial(p: String) {
        partialText = p
        partialListener?.invoke(p)
    }

    fun publishLine(src: String, dst: String) {
        entriesBuffer.add(TranslateStore.Entry(src, dst))
        lineListener?.invoke(src, dst)
    }
}

/**
 * 屏幕底部悬浮字幕条：翻译进行中且 App 不在前台时显示，
 * 上行原文（识别中实时刷新）、下行最新译文，右上角 ✕ 直接停。
 */
object SubtitleOverlay {

    private val main = Handler(Looper.getMainLooper())
    private var wm: WindowManager? = null
    private var root: View? = null
    private var params: WindowManager.LayoutParams? = null
    private var srcView: TextView? = null
    private var dstView: TextView? = null
    private var appCtx: Context? = null

    val isShowing: Boolean get() = root != null

    fun show(ctx: Context) {
        main.post {
            if (root != null) return@post
            val c = ctx.applicationContext
            if (!Settings.canDrawOverlays(c)) return@post
            appCtx = c
            val mgr = c.getSystemService(WindowManager::class.java)
            wm = mgr
            val box = LinearLayout(c).apply {
                orientation = LinearLayout.HORIZONTAL
                background = GradientDrawable().apply {
                    shape = GradientDrawable.RECTANGLE
                    cornerRadius = Ui.dp(c, 12).toFloat()
                    setColor(0xC2131318.toInt())
                }
                setPadding(Ui.dp(c, 14), Ui.dp(c, 9), Ui.dp(c, 6), Ui.dp(c, 10))
                gravity = Gravity.CENTER_VERTICAL
            }
            val texts = LinearLayout(c).apply {
                orientation = LinearLayout.VERTICAL
                layoutParams = LinearLayout.LayoutParams(0, LinearLayout.LayoutParams.WRAP_CONTENT, 1f)
            }
            srcView = TextView(c).apply {
                text = "🎙 正在听…"
                textSize = 13f
                setTextColor(0xFFB9C0CC.toInt())
                maxLines = 2
            }
            dstView = TextView(c).apply {
                text = ""
                textSize = 17f
                setTextColor(0xFFFFFFFF.toInt())
                maxLines = 3
            }
            texts.addView(srcView); texts.addView(dstView)
            box.addView(texts)
            val close = TextView(c).apply {
                text = "✕"
                textSize = 15f
                setTextColor(0xFFB9C0CC.toInt())
                gravity = Gravity.CENTER
                layoutParams = LinearLayout.LayoutParams(Ui.dp(c, 34), Ui.dp(c, 34))
                setOnClickListener { TranslateControl.stopper?.invoke() }
            }
            box.addView(close)
            // 拖拽调位：按住字幕条上下拖动，松手记住位置（✕ 是子控件，自行消费点击不受影响）
            var downRawY = 0f
            var startY = 0
            box.setOnTouchListener { v, ev ->
                val p = params ?: return@setOnTouchListener false
                when (ev.action) {
                    android.view.MotionEvent.ACTION_DOWN -> {
                        downRawY = ev.rawY
                        startY = p.y
                        true
                    }
                    android.view.MotionEvent.ACTION_MOVE -> {
                        val maxY = (c.resources.displayMetrics.heightPixels * 0.8f).toInt()
                        p.y = (startY + (downRawY - ev.rawY).toInt()).coerceIn(0, maxY)
                        try { wm?.updateViewLayout(v, p) } catch (e: Exception) {}
                        true
                    }
                    android.view.MotionEvent.ACTION_UP, android.view.MotionEvent.ACTION_CANCEL -> {
                        c.getSharedPreferences("translate", Context.MODE_PRIVATE)
                            .edit().putInt("subtitle_y", p.y).apply()
                        true
                    }
                    else -> false
                }
            }
            val savedY = c.getSharedPreferences("translate", Context.MODE_PRIVATE)
                .getInt("subtitle_y", -1)
            val p = WindowManager.LayoutParams(
                WindowManager.LayoutParams.MATCH_PARENT,
                WindowManager.LayoutParams.WRAP_CONTENT,
                if (Build.VERSION.SDK_INT >= 26) WindowManager.LayoutParams.TYPE_APPLICATION_OVERLAY
                else @Suppress("DEPRECATION") WindowManager.LayoutParams.TYPE_PHONE,
                WindowManager.LayoutParams.FLAG_NOT_FOCUSABLE,
                PixelFormat.TRANSLUCENT
            ).apply {
                gravity = Gravity.BOTTOM
                y = if (savedY >= 0) savedY else Ui.dp(c, 26)
            }
            params = p
            try {
                mgr.addView(box, p)
                root = box
            } catch (e: Exception) {
                root = null
                params = null
            }
        }
    }

    /** 状态提示（如下载模型、通话暂停）：显示在原文行。 */
    fun status(text: String) {
        main.post {
            if (root == null) return@post
            srcView?.text = text
        }
    }

    /** 识别中的实时原文（未定稿）。 */
    fun partial(text: String) {
        main.post {
            if (root == null) return@post
            srcView?.text = if (text.isBlank()) "🎙 正在听…" else "… $text"
        }
    }

    /** 一句已提交：原文定格，译文随后由 lineDone 补齐（或直接带译文）。 */
    fun line(src: String, dst: String) {
        main.post {
            if (root == null) return@post
            if (src.isNotBlank()) srcView?.text = src
            dstView?.text = dst
        }
    }

    fun hide() {
        main.post {
            val v = root ?: return@post
            try { wm?.removeView(v) } catch (e: Exception) {}
            root = null; srcView = null; dstView = null; params = null
        }
    }
}
