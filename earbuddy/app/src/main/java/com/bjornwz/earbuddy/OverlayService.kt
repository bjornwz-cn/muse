package com.bjornwz.earbuddy

import android.app.Service
import android.content.Context
import android.content.Intent
import android.graphics.PixelFormat
import android.graphics.drawable.GradientDrawable
import android.os.Build
import android.os.IBinder
import android.provider.Settings
import android.view.Gravity
import android.view.MotionEvent
import android.view.View
import android.view.WindowManager
import android.widget.TextView

/** 悬浮翻译按钮：可拖动；点击切换——未翻译时自动开始翻译并出底部字幕条，翻译中再点直接停。 */
class OverlayService : Service() {

    private var wm: WindowManager? = null
    private var view: TextView? = null
    private var params: WindowManager.LayoutParams? = null
    private val mainHandler = android.os.Handler(android.os.Looper.getMainLooper())
    private var lastActive: Boolean? = null

    /** 每秒对齐一次按钮状态：翻译中=红底「停」，空闲=绿底「译」——状态来自会话本身，
     * 无论从字幕条 ✕、通知栏还是 App 里停的，按钮都会跟着变。 */
    private val statePoll = object : Runnable {
        override fun run() {
            val active = TranslateControl.active ||
                (try { TranslateStore.isActive(this@OverlayService) } catch (e: Exception) { false })
            if (active != lastActive) {
                lastActive = active
                view?.let { tv ->
                    tv.text = if (active) "停" else "译"
                    (tv.background as? GradientDrawable)?.setColor(
                        if (active) 0xFFE5484D.toInt() else Ui.ACCENT
                    )
                }
            }
            mainHandler.postDelayed(this, 1000)
        }
    }

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onCreate() {
        super.onCreate()
        if (!Settings.canDrawOverlays(this)) { stopSelf(); return }
        wm = getSystemService(WindowManager::class.java)
        val size = Ui.dp(this, 54)
        val tv = TextView(this).apply {
            text = "译"
            textSize = 20f
            setTextColor(0xFFFFFFFF.toInt())
            gravity = Gravity.CENTER
            background = GradientDrawable().apply {
                shape = GradientDrawable.OVAL
                setColor(Ui.ACCENT)
            }
            elevation = Ui.dp(this@OverlayService, 6).toFloat()
        }
        val p = WindowManager.LayoutParams(
            size, size,
            if (Build.VERSION.SDK_INT >= 26) WindowManager.LayoutParams.TYPE_APPLICATION_OVERLAY
            else @Suppress("DEPRECATION") WindowManager.LayoutParams.TYPE_PHONE,
            WindowManager.LayoutParams.FLAG_NOT_FOCUSABLE,
            PixelFormat.TRANSLUCENT
        ).apply {
            gravity = Gravity.END or Gravity.CENTER_VERTICAL
            x = Ui.dp(this@OverlayService, 10)
            y = 0
        }
        params = p
        var downX = 0f; var downY = 0f; var startX = 0; var startY = 0; var moved = false
        tv.setOnTouchListener { v, ev ->
            when (ev.action) {
                MotionEvent.ACTION_DOWN -> {
                    downX = ev.rawX; downY = ev.rawY
                    startX = p.x; startY = p.y; moved = false
                    true
                }
                MotionEvent.ACTION_MOVE -> {
                    val dx = (ev.rawX - downX).toInt()
                    val dy = (ev.rawY - downY).toInt()
                    if (kotlin.math.abs(dx) + kotlin.math.abs(dy) > 12) moved = true
                    // gravity END：x 向左为正，拖动方向取反
                    p.x = startX - dx
                    p.y = startY + dy
                    try { wm?.updateViewLayout(v, p) } catch (e: Exception) {}
                    true
                }
                MotionEvent.ACTION_UP -> {
                    if (!moved) {
                        val active = TranslateStore.isActive(this@OverlayService) ||
                            TranslateControl.active
                        val stopper = TranslateControl.stopper
                        if (active && stopper != null) {
                            // 正在翻译 → 直接停，全程不打开 App
                            stopper.invoke()
                            android.widget.Toast.makeText(
                                this@OverlayService, "已停止翻译",
                                android.widget.Toast.LENGTH_SHORT
                            ).show()
                        } else if (active) {
                            TranslateService.stop(this@OverlayService)
                        } else {
                            startFromOverlay()
                        }
                    }
                    true
                }
                else -> false
            }
        }
        view = tv
        try { wm?.addView(tv, p) } catch (e: Exception) { stopSelf() }
        mainHandler.post(statePoll)
    }

    /**
     * 悬浮按钮启动翻译：默认全程后台（服务直启，App 不弹前台）。
     * 只有两种情况必须借道翻译页：录音权限未给、内部声音尚未授权过（系统强制弹窗）。
     */
    private fun startFromOverlay() {
        val ctx = this@OverlayService
        val needMicPerm = TranslateStore.micSource(ctx) != "internal" &&
            checkSelfPermission(android.Manifest.permission.RECORD_AUDIO) !=
            android.content.pm.PackageManager.PERMISSION_GRANTED
        val needConsent = TranslateStore.micSource(ctx) == "internal" &&
            !InternalCapture.isRunning() && !InternalCapture.hasProjection()
        if (needMicPerm || needConsent) {
            // 借道翻译页拿权限/授权：会话起来后退回后台（background_start），
            // 用户本来就在别的 App 里，不打扰。主界面点「开始翻译」不带这个标记，留在前台。
            startActivity(
                Intent(ctx, TranslateActivity::class.java)
                    .putExtra("auto_start", true)
                    .putExtra("background_start", true)
                    .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
            )
        } else {
            TranslateService.start(ctx)
            android.widget.Toast.makeText(
                ctx, "翻译已开始（字幕在屏幕下方）",
                android.widget.Toast.LENGTH_SHORT
            ).show()
        }
    }

    override fun onDestroy() {
        mainHandler.removeCallbacks(statePoll)
        view?.let { try { wm?.removeView(it) } catch (e: Exception) {} }
        view = null
        super.onDestroy()
    }

    companion object {
        fun start(ctx: Context) {
            try { ctx.startService(Intent(ctx, OverlayService::class.java)) } catch (e: Exception) {}
        }
        fun stop(ctx: Context) {
            try { ctx.stopService(Intent(ctx, OverlayService::class.java)) } catch (e: Exception) {}
        }
    }
}
