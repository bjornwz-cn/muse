package com.bjornwz.earbuddy

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent

/** 开机自启前台服务（需用户已授予通知读取等权限，权限本身由系统持久化）。 */
class BootReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        if (intent.action == Intent.ACTION_BOOT_COMPLETED) {
            KeepAliveService.start(context)
        }
    }
}
