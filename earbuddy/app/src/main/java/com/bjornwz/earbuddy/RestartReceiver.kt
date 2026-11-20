package com.bjornwz.earbuddy

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent

/** 常驻服务被划掉任务后自拉起（配合 KeepAliveService.onTaskRemoved 的闹钟）。 */
class RestartReceiver : BroadcastReceiver() {
    override fun onReceive(ctx: Context, intent: Intent?) {
        try {
            ctx.startForegroundService(Intent(ctx, KeepAliveService::class.java))
        } catch (e: Exception) {
            // 后台启动受限时静默失败，用户下次打开 App 即恢复
        }
    }
}
