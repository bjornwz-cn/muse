package com.bjornwz.earbuddy

import android.app.Application

/** 应用入口：装崩溃日志钩子 + 记启动事件（诊断日志系统）。 */
class EarApp : Application() {
    override fun onCreate() {
        super.onCreate()
        val prev = Thread.getDefaultUncaughtExceptionHandler()
        Thread.setDefaultUncaughtExceptionHandler { t, e ->
            DiagLog.logSync(
                this, "crash",
                mapOf(
                    "thread" to t.name,
                    "error" to e.toString(),
                    "stack" to e.stackTraceToString().take(2000)
                )
            )
            prev?.uncaughtException(t, e)
        }
        DiagLog.log(this, "app_start")
    }
}
