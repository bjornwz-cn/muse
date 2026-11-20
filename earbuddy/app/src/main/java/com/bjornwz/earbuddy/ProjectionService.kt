package com.bjornwz.earbuddy

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.Service
import android.content.Intent
import android.content.pm.ServiceInfo
import android.media.projection.MediaProjection
import android.media.projection.MediaProjectionManager
import android.os.Build
import android.os.IBinder

/**
 * 内部音频翻译的投屏授权宿主服务（Android 14+ 要求：先启动 mediaProjection 型前台服务，
 * 才能用授权结果创建 MediaProjection）。收到授权 → 启动 InternalCapture 采集。
 */
class ProjectionService : Service() {
    override fun onBind(intent: Intent?): IBinder? = null

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        startForegroundCompat()
        if (intent?.action == ACTION_BEGIN) {
            val code = intent.getIntExtra(EXTRA_CODE, 0)
            @Suppress("DEPRECATION")
            val data: Intent? = intent.getParcelableExtra(EXTRA_DATA)
            if (data != null) {
                try {
                    val mpm = getSystemService(MediaProjectionManager::class.java)
                    val mp = mpm.getMediaProjection(code, data)
                    if (mp != null) {
                        mp.registerCallback(object : MediaProjection.Callback() {
                            override fun onStop() { InternalCapture.stop() }
                        }, null)
                        InternalCapture.start(mp)
                    }
                } catch (e: Exception) { }
            }
        }
        return START_NOT_STICKY
    }

    private fun startForegroundCompat() {
        val nm = getSystemService(NotificationManager::class.java)
        if (Build.VERSION.SDK_INT >= 26 && nm.getNotificationChannel(CH) == null) {
            nm.createNotificationChannel(
                NotificationChannel(CH, "内部音频翻译", NotificationManager.IMPORTANCE_LOW)
            )
        }
        val notif: Notification = Notification.Builder(this, CH)
            .setContentTitle("耳机助手")
            .setContentText("正在采集手机内部声音用于翻译")
            .setSmallIcon(android.R.drawable.ic_btn_speak_now)
            .build()
        if (Build.VERSION.SDK_INT >= 29) {
            startForeground(ID, notif, ServiceInfo.FOREGROUND_SERVICE_TYPE_MEDIA_PROJECTION)
        } else {
            startForeground(ID, notif)
        }
    }

    override fun onDestroy() {
        InternalCapture.stop()
        super.onDestroy()
    }

    companion object {
        private const val CH = "projection"
        private const val ID = 42
        const val ACTION_BEGIN = "com.bjornwz.earbuddy.PROJ_BEGIN"
        const val EXTRA_CODE = "code"
        const val EXTRA_DATA = "data"
    }
}
