package com.bjornwz.earbuddy

import android.content.Context
import android.content.SharedPreferences
import java.util.Calendar

/**
 * 播报规则引擎（M1）。
 * 维度：总开关 × 应用开关 × 静默时段（时段内一律不播）。
 * 规则由王震 2026-11-07 定案 Q4/Q9：按应用列表、时间自由组合；带报头读全文。
 * （重点白名单 2026-11-08 王震令去掉。）
 */
object Rules {
    private const val PREF = "rules"
    private const val K_MASTER = "master_on"
    private const val K_DISABLED_APPS = "disabled_apps"   // 用户在列表里关掉的应用（默认全部播报）
    private const val K_QUIET_START = "quiet_start_min"   // 分钟数，默认 23:00
    private const val K_QUIET_END = "quiet_end_min"       // 默认 07:00
    private const val K_QUIET_ON = "quiet_enabled"        // 静默时段开关，默认开

    private fun prefs(ctx: Context): SharedPreferences =
        ctx.getSharedPreferences(PREF, Context.MODE_PRIVATE)

    fun isMasterOn(ctx: Context): Boolean = prefs(ctx).getBoolean(K_MASTER, true)
    fun setMasterOn(ctx: Context, on: Boolean) =
        prefs(ctx).edit().putBoolean(K_MASTER, on).apply()

    fun isAppEnabled(ctx: Context, pkg: String): Boolean =
        pkg !in disabledApps(ctx)

    fun disabledApps(ctx: Context): Set<String> =
        prefs(ctx).getStringSet(K_DISABLED_APPS, emptySet()) ?: emptySet()

    fun setAppEnabled(ctx: Context, pkg: String, enabled: Boolean) {
        val set = disabledApps(ctx).toMutableSet()
        if (enabled) set.remove(pkg) else set.add(pkg)
        prefs(ctx).edit().putStringSet(K_DISABLED_APPS, set).apply()
    }

    fun quietStart(ctx: Context): Int = prefs(ctx).getInt(K_QUIET_START, 23 * 60)
    fun quietEnd(ctx: Context): Int = prefs(ctx).getInt(K_QUIET_END, 7 * 60)
    fun setQuiet(ctx: Context, startMin: Int, endMin: Int) =
        prefs(ctx).edit().putInt(K_QUIET_START, startMin).putInt(K_QUIET_END, endMin).apply()

    fun quietEnabled(ctx: Context): Boolean = prefs(ctx).getBoolean(K_QUIET_ON, true)
    fun setQuietEnabled(ctx: Context, on: Boolean) =
        prefs(ctx).edit().putBoolean(K_QUIET_ON, on).apply()

    fun inQuietHours(ctx: Context, now: Calendar = Calendar.getInstance()): Boolean {
        if (!quietEnabled(ctx)) return false
        val start = quietStart(ctx); val end = quietEnd(ctx)
        if (start == end) return false
        val cur = now.get(Calendar.HOUR_OF_DAY) * 60 + now.get(Calendar.MINUTE)
        return if (start < end) cur in start until end else cur >= start || cur < end
    }

    /** 返回 true 表示应当播报。 */
    fun shouldAnnounce(ctx: Context, pkg: String, title: String, text: String): Boolean {
        if (!isMasterOn(ctx)) return false
        if (!isAppEnabled(ctx, pkg)) return false
        if (inQuietHours(ctx)) return false
        return true
    }

    fun fmtMinutes(min: Int): String = "%02d:%02d".format(min / 60, min % 60)
}
