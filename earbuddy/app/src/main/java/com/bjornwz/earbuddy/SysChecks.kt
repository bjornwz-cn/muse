package com.bjornwz.earbuddy

import android.content.ComponentName
import android.content.Context
import android.os.PowerManager
import android.provider.Settings

/** 系统状态直读（主页状态区与设置页自检共用，避免两处复制实现）。 */
object SysChecks {

    fun isListenerEnabled(ctx: Context): Boolean {
        val flat = Settings.Secure.getString(
            ctx.contentResolver, "enabled_notification_listeners"
        ) ?: return false
        val mine = ComponentName(ctx, NotifyListener::class.java).flattenToString()
        return flat.split(':').any { it.equals(mine, ignoreCase = true) }
    }

    fun isBatteryWhitelisted(ctx: Context): Boolean =
        ctx.getSystemService(PowerManager::class.java)
            .isIgnoringBatteryOptimizations(ctx.packageName)
}
