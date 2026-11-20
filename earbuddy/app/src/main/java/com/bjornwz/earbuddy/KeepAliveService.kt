package com.bjornwz.earbuddy

import android.app.AlarmManager
import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.os.Build
import android.os.IBinder

/** 常驻前台服务：保持进程存活（国产系统保活第一道），并挂状态通知。 */
class KeepAliveService : Service() {

    private var btReceiver: android.content.BroadcastReceiver? = null

    override fun onCreate() {
        super.onCreate()
        // 蓝牙门闸：监听耳机连接/断开，维护连接集合；播报耳机连上后补播暂存通知
        try {
            BtGate.seed(applicationContext)
            val r = object : android.content.BroadcastReceiver() {
                override fun onReceive(c: android.content.Context, i: android.content.Intent) {
                    val dev = i.getParcelableExtra<android.bluetooth.BluetoothDevice>(
                        android.bluetooth.BluetoothDevice.EXTRA_DEVICE
                    )
                    val mac = try { dev?.address } catch (e: Exception) { null }
                    when (i.action) {
                        android.bluetooth.BluetoothDevice.ACTION_ACL_CONNECTED -> {
                            BtGate.onAcl(applicationContext, mac, true)
                            BtGate.seed(applicationContext)
                            if (BtGate.isTargetConnected(applicationContext)) {
                                BtGate.flush(applicationContext)
                            }
                        }
                        android.bluetooth.BluetoothDevice.ACTION_ACL_DISCONNECTED -> {
                            BtGate.onAcl(applicationContext, mac, false)
                            BtGate.seed(applicationContext)
                        }
                        android.bluetooth.BluetoothAdapter.ACTION_STATE_CHANGED -> {
                            val st = i.getIntExtra(
                                android.bluetooth.BluetoothAdapter.EXTRA_STATE, -1)
                            // 蓝牙整体关闭时 ACL 断开广播未必送达，直接清空连接集合防陈旧记录外放
                            if (st == android.bluetooth.BluetoothAdapter.STATE_OFF) {
                                BtGate.clearConnections(applicationContext)
                            }
                            if (st == android.bluetooth.BluetoothAdapter.STATE_ON) {
                                BtGate.seed(applicationContext)
                            }
                        }
                    }
                }
            }
            val f = android.content.IntentFilter().apply {
                addAction(android.bluetooth.BluetoothDevice.ACTION_ACL_CONNECTED)
                addAction(android.bluetooth.BluetoothDevice.ACTION_ACL_DISCONNECTED)
                addAction(android.bluetooth.BluetoothAdapter.ACTION_STATE_CHANGED)
            }
            registerReceiver(r, f)
            btReceiver = r
        } catch (e: Exception) { }
        Speaker.init(applicationContext)
        startForegroundWithStatus()
        // 通话结束检测：每 3 秒看一眼，有积压的待播通知就补播
        val handler = android.os.Handler(android.os.Looper.getMainLooper())
        val ticker = object : Runnable {
            override fun run() {
                Speaker.flushDeferred(applicationContext)
                Speaker.keepWarm(applicationContext)
                heartbeat(applicationContext)
                handler.postDelayed(this, 3000)
            }
        }
        handler.postDelayed(ticker, 3000)
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        startForegroundWithStatus()
        return START_STICKY
    }

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onTaskRemoved(rootIntent: Intent?) {
        // 从最近任务划掉本应用时，2 秒后自拉起，避免切到后台就不播报
        try {
            val pi = PendingIntent.getBroadcast(
                this, 42, Intent(this, RestartReceiver::class.java),
                PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE
            )
            getSystemService(AlarmManager::class.java)
                ?.setAndAllowWhileIdle(AlarmManager.RTC_WAKEUP, System.currentTimeMillis() + 2000, pi)
        } catch (e: Exception) { /* 忽略 */ }
        super.onTaskRemoved(rootIntent)
    }

    private fun startForegroundWithStatus() {
        val nm = getSystemService(NotificationManager::class.java)
        val channelId = "keepalive"
        if (nm.getNotificationChannel(channelId) == null) {
            nm.createNotificationChannel(
                NotificationChannel(channelId, "运行状态", NotificationManager.IMPORTANCE_LOW)
            )
        }
        val open = PendingIntent.getActivity(
            this, 0,
            Intent(this, MainActivity::class.java),
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT
        )
        val notif = Notification.Builder(this, channelId)
            .setContentTitle("耳机助手运行中")
            .setContentText("通知播报待命中")
            .setSmallIcon(android.R.drawable.ic_btn_speak_now)
            .setContentIntent(open)
            .setOngoing(true)
            .build()
        if (Build.VERSION.SDK_INT >= 34) {
            startForeground(NOTIF_ID, notif, ServiceInfo.FOREGROUND_SERVICE_TYPE_MEDIA_PLAYBACK)
        } else {
            startForeground(NOTIF_ID, notif)
        }
    }

    companion object {
        private const val NOTIF_ID = 1001
        fun start(ctx: Context) {
            val i = Intent(ctx, KeepAliveService::class.java)
            if (Build.VERSION.SDK_INT >= 26) ctx.startForegroundService(i) else ctx.startService(i)
        }

        // ——— 保活自检：心跳 + 后台中断记录 + 60 秒存活实测（真实检测，杜绝假完成）———
        private fun hp(ctx: Context) = ctx.getSharedPreferences("health", Context.MODE_PRIVATE)

        /** 服务每 3 秒调：写心跳、记中断、处理到期的存活测试。 */
        fun heartbeat(ctx: Context) {
            val p = hp(ctx)
            val now = System.currentTimeMillis()
            val last = p.getLong("last_hb", 0)
            val e = p.edit()
            if (last > 0 && now - last > 60_000) {
                e.putLong("last_gap_ms", now - last).putLong("last_gap_at", now)
                DiagLog.log(ctx, "hb_gap", mapOf("gapMs" to (now - last)))
            }
            if (last == 0L || now - last >= 10_000) e.putLong("last_hb", now)
            val due = p.getLong("test_due", 0)
            if (due > 0 && p.getLong("test_done", 0) == 0L && now >= due) {
                e.putLong("test_done", now)
            }
            e.apply()
            DiagUpload.maybeAutoUpload(ctx)
        }

        fun lastHeartbeat(ctx: Context): Long = hp(ctx).getLong("last_hb", 0)
        fun lastGapMs(ctx: Context): Long = hp(ctx).getLong("last_gap_ms", 0)
        fun lastGapAt(ctx: Context): Long = hp(ctx).getLong("last_gap_at", 0)

        fun startTest(ctx: Context) {
            hp(ctx).edit()
                .putLong("test_due", System.currentTimeMillis() + 60_000)
                .putLong("test_done", 0)
                .apply()
        }

        /** 存活测试状态：Pair(状态码, 说明)。0 未测试 1 进行中 2 通过 3 被冻结 4 被杀/未执行 */
        fun testState(ctx: Context): Pair<Int, String> {
            val p = hp(ctx)
            val due = p.getLong("test_due", 0)
            if (due == 0L) return 0 to "尚未测试"
            val now = System.currentTimeMillis()
            if (now < due) return 1 to "测试中，还剩 ${(due - now) / 1000} 秒——请保持停留在其他 App，不要打开本应用"
            val done = p.getLong("test_done", 0)
            if (done == 0L) return 4 to "未通过：到点后进程没能执行——后台期间被杀或深度冻结，请检查第 3 步启动管理"
            val lateSec = (done - due) / 1000
            return if (lateSec <= 30) 2 to "通过：后台期间进程存活（执行延迟 ${lateSec} 秒）"
            else 3 to "未通过：后台被冻结约 ${lateSec} 秒后才恢复执行，请检查第 3 步启动管理"
        }
    }
}
