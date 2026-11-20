package com.bjornwz.earbuddy

import android.app.Notification
import android.content.ComponentName
import android.content.pm.PackageManager
import android.service.notification.NotificationListenerService
import android.service.notification.StatusBarNotification

/**
 * 通知监听：取标题/正文 → 规则引擎判定 → 组装「应用名，标题：正文」→ 交给 Speaker 播报。
 * 过滤：本应用通知、常驻通知、分组汇总、空内容。
 */
class NotifyListener : NotificationListenerService() {

    override fun onListenerConnected() {
        super.onListenerConnected()
        Speaker.init(applicationContext)
    }

    override fun onListenerDisconnected() {
        super.onListenerDisconnected()
        // 被系统解绑时主动请求重连，减少后台失联时间
        try { requestRebind(ComponentName(this, NotifyListener::class.java)) } catch (e: Exception) { }
    }

    override fun onNotificationPosted(sbn: StatusBarNotification) {
        val pkg = sbn.packageName ?: return
        if (pkg == packageName) return
        val n = sbn.notification ?: return
        if (n.flags and Notification.FLAG_ONGOING_EVENT != 0) return
        if (n.flags and Notification.FLAG_GROUP_SUMMARY != 0) return

        val extras = n.extras
        val title = extras.getCharSequence(Notification.EXTRA_TITLE)?.toString()?.trim().orEmpty()
        val big = extras.getCharSequence(Notification.EXTRA_BIG_TEXT)?.toString()
        val rawText = (big ?: extras.getCharSequence(Notification.EXTRA_TEXT)?.toString())
            ?.trim().orEmpty()
        // 剥掉微信等应用的未读计数前缀（[13条]），播报不念计数，判重也不受计数干扰
        val text = AnnGuard.stripLeadCount(rawText)
        val body = listOf(title, text).filter { it.isNotEmpty() }.distinct().joinToString("：")
        if (body.isEmpty()) return

        // 陈旧通知不播：监听重连时系统会重播通知栏存量，发布时间过老的直接丢弃
        if (System.currentTimeMillis() - sbn.postTime > 10 * 60 * 1000) return
        // 去重：同一通知（key+发布时间+内容）只播一次，防重连重播与重复投递
        val dedupId = Dedup.idOf(pkg, sbn.key, sbn.postTime, "$title|$text")
        if (Dedup.seen(applicationContext, dedupId)) return
        Dedup.mark(applicationContext, dedupId)

        val ctx = applicationContext
        if (!Rules.shouldAnnounce(ctx, pkg, title, text)) {
            DiagLog.log(ctx, "notify", mapOf("pkg" to pkg, "result" to "blocked"))
            return
        }

        val appName = try {
            val pm = packageManager
            pm.getApplicationLabel(pm.getApplicationInfo(pkg, 0)).toString()
        } catch (e: PackageManager.NameNotFoundException) { pkg }

        val prio = priorityOf(ctx, pkg, title, text)
        // 冲突仲裁：翻译进行中，通知只存不播，结束后统一汇总
        if (TranslateStore.isActive(ctx)) {
            TranslateStore.defer(ctx, "$appName，$body", prio == 0)
            DiagLog.log(ctx, "notify", mapOf("pkg" to pkg, "result" to "deferred", "prio" to prio))
            return
        }
        // 门闸：未连接播报耳机不自动播报，先暂存，耳机连上后汇总补播
        if (!BtGate.isTargetConnected(ctx)) {
            BtGate.hold(ctx, appName, body, prio == 0)
            DiagLog.log(ctx, "notify", mapOf("pkg" to pkg, "result" to "held", "prio" to prio))
            return
        }
        // 降噪守卫：剥离计数后的内容判重＋同会话连发合并，通过后才真正播报
        val convKey = pkg + "|" + title.ifEmpty { appName }
        AnnGuard.offer(ctx, pkg, convKey, "$appName，$body", body, prio, AnnGuard.normOf("$title|$text"))
    }

    /** 播报优先级：0 通讯类应用 ＞ 1 其他。同时多条到达时按此排序逐条播。 */
    private fun priorityOf(ctx: android.content.Context, pkg: String, title: String, text: String): Int {
        if (pkg in MESSAGING_PKGS) return 0
        return 1
    }

    companion object {
        private val MESSAGING_PKGS = setOf(
            "com.tencent.mm", "com.tencent.mobileqq", "com.tencent.wework",
            "com.alibaba.android.rimet", "com.ss.android.lark", "com.larksuite.suite",
            "com.android.mms", "com.hihonor.mms", "com.google.android.apps.messaging",
            "com.android.incallui", "com.android.dialer", "com.hihonor.dialer",
            "com.whatsapp", "org.telegram.messenger"
        )
    }
}
