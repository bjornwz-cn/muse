package com.bjornwz.earbuddy

import android.Manifest
import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.content.pm.ServiceInfo
import android.os.Build
import android.os.Handler
import android.os.IBinder
import android.os.Looper
import android.provider.Settings

/**
 * 实时翻译会话服务（前台）：会话的真正宿主。
 * 悬浮按钮/翻译页都只是开关——会话在这里跑，不需要把 App 调到前台：
 * 识别（火山流式 / 离线 Vosk / 云端重识别）→ 流式切分 → 翻译 → 字幕条 + 朗读 + 记录。
 * 内部音频的投影在会话停止后保留（只停采集），下次开始免授权。
 */
class TranslateService : Service() {

    private val handler = Handler(Looper.getMainLooper())
    @Volatile private var running = false
    @Volatile private var pausedByCall = false
    private var volc: VolcAsr? = null
    private var tx: TencentAsr? = null
    private var from = "zh"
    private var to = "en"
    /** 实际喂给识别引擎的源语言：from=auto 且引擎不支持自动检测时回落 zh。 */
    private var effFrom = "zh"
    private var autoCapable = false
    /** 火山流式只识别中英：日语/韩语会话强制改走分段识别路径。
     *  2026-11-20 实测：流式接口（bigmodel_async）传 language=ja-JP 不报错，
     *  但返回的是罗马音（且不准）；官方文档亦注明 language 参数仅一句话识别支持。 */
    private var useVolc = false
    /** 腾讯云实时 ASR（16k_ja/16k_ko/16k_zh）：中日韩都走真流式，替代火山流式＋日韩 nostream 路径。 */
    private var useTx = false
    /** 日韩首选：火山一句话识别（多语种，与流式共用一把 Key）。 */
    private var useNostream = false
    private var sessionChannel: CloudAsr.Channel? = null
    /** 云端识别为准（日韩）：离线分段文本不可信，不上屏、云端失败不拿它兜底。 */
    private var cloudPrimary = false
    private var startedAt = 0L
    private val entries = mutableListOf<TranslateStore.Entry>()

    /** 最近一次「有声音/有识别结果」的时刻，静音看门狗据此自动停（防忘关扣费）。 */
    @Volatile private var lastActivityAt = 0L

    private fun onAudioLevel(pct: Int) {
        TranslateControl.publishLevel(pct)
        if (pct >= 8) {
            lastActivityAt = System.currentTimeMillis()
            if (vadMode) vadLastSpeechSample = VoskManager.samplePos()
        }
    }

    // 流式切分状态（与翻译页时代一致）
    private var committedLen = 0
    private var segStartAt = 0L
    private var lastPartial = ""
    private var lastCommitSample = 0L

    // ——— VAD 切段（日韩 nostream 流式化，v0.8.9）———
    // 不再按 Vosk 垃圾文本标点切：按能量 VAD 跟踪真实语音停顿切段，
    // 有声 ≥2 秒且静音 ≥0.7 秒时切段；单段最长 6 秒强切。
    // 切段边界一律用采样位置（音频时间），与聚合器的音频时间计时对齐。
    private var vadMode = false
    private var vadSegStartSample = -1L
    private var vadLastSpeechSample = 0L
    private val vadHandler = Handler(Looper.getMainLooper())
    private var vadTicking = false

    private val vadTicker = object : Runnable {
        override fun run() {
            if (!running || !vadMode || pausedByCall) { vadTicking = false; return }
            val now = VoskManager.samplePos()
            if (vadSegStartSample < 0) {
                vadSegStartSample = now
                vadLastSpeechSample = now
            }
            // 段内尚无语音（开场静音）：把段起点锚到当前，避免空段攒出前导静音
            if (vadLastSpeechSample <= vadSegStartSample) {
                vadSegStartSample = now
                vadLastSpeechSample = now
            }
            val segLenMs = (now - vadSegStartSample) * 1000 / 16000
            val silenceMs = (now - vadLastSpeechSample) * 1000 / 16000
            when {
                segLenMs >= 6000 -> cutVadSegment(now) // 单段最长 6 秒，强切
                segLenMs >= 2000 && silenceMs >= 700 -> cutVadSegment(vadLastSpeechSample) // 停顿切
            }
            vadHandler.postDelayed(this, 200)
        }
    }

    private fun cutVadSegment(endSample: Long) {
        val start = vadSegStartSample
        if (start < 0 || endSample <= start) return
        val wav = VoskManager.extractWav(start, endSample)
        vadSegStartSample = endSample
        vadLastSpeechSample = endSample
        if (wav != null) commitSegment("", wav, endSample / 16000.0)
    }

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        try {
            when (intent?.action) {
                ACTION_STOP -> stopSession()
                else -> startSession()
            }
        } catch (t: Throwable) {
            // 最后一道防线：启动/停止路径的任何未捕获异常都不能让服务带崩整个进程
            DiagLog.logSync(this, "svc_cmd_fail", mapOf("error" to t.toString()))
            try { stopForegroundCompat() } catch (e: Exception) { }
            try { stopSelf() } catch (e: Exception) { }
        }
        return START_NOT_STICKY
    }

    private fun volcMode() = TranslateStore.asrEngine(this) == "volc" &&
        TranslateStore.asrKey(this, "volc").isNotEmpty()

    /** 腾讯云实时 ASR 已选且三值（AppId/SecretId/SecretKey）都已填。 */
    private fun txMode() = TranslateStore.asrEngine(this) == "tx" &&
        TranslateStore.txConfigured(this)

    private fun engineDesc(): String {
        if (useTx) return "腾讯·实时识别（${TencentAsr.engineFor(effFrom)}）"
        if (useNostream) return "火山·多语种识别（日韩）"
        if (cloudPrimary) return "云端·${sessionChannel?.name ?: "多语种"}（日韩识别）"
        if (volcMode()) return "火山·流式识别2.0"
        val ch = CloudAsr.activeChannel(this)
        if (ch != null) return "云端·${ch.name}"
        if (TranslateStore.asrEngine(this) != "vosk") return "离线识别（云端 Key 未填）"
        return "离线·${if (TranslateStore.modelTier(this) == "high") "高精度大模型" else "标准小模型"}"
    }

    private fun status(s: String) {
        TranslateControl.publishStatus(s)
        SubtitleOverlay.status(s)
    }

    /** 启动入口（带兜底）：startSessionInner 里任何未捕获的 Throwable 都不能闪退，
     *  只记日志、报状态、干净收尾。 */
    private fun startSession() {
        try {
            startSessionInner()
        } catch (t: Throwable) {
            DiagLog.logSync(
                this, "tr_start_fail",
                mapOf(
                    "error" to t.toString(),
                    "engine" to TranslateStore.asrEngine(this),
                    "from" to from, "to" to to, "tx" to useTx, "volc" to useVolc
                )
            )
            running = false
            TranslateStore.setActive(this, false)
            TranslateControl.active = false
            status("❌ 启动失败：${t.message ?: "未知错误"}，已记入诊断日志")
            stopForegroundCompat()
            stopSelf()
        }
    }

    private fun startSessionInner() {
        if (running) return
        startForegroundCompat()
        from = TranslateStore.dirFrom(this)
        to = TranslateStore.dirTo(this)
        autoCapable = volcMode()
        effFrom = if (from == "auto") (if (autoCapable) "auto" else "zh") else from
        // 日语/韩语：火山流式与离线模型都只认中英（实测日语被识别成英中乱码），
        // 必须走云端多语种识别（SenseVoice/Whisper）。渠道优先用当前引擎选的，
        // 引擎是火山/离线时自动借任意一个已填 Key 的云端渠道。
        val multiLang = effFrom == "ja" || effFrom == "ko"
        sessionChannel = CloudAsr.activeChannel(this)
        cloudPrimary = false
        useNostream = false
        useVolc = volcMode() && !multiLang
        useTx = txMode()
        if (useTx) {
            // 腾讯实时 ASR 中日韩都走真流式：不借云端渠道、不走 nostream、不走 VAD 切段
            sessionChannel = null
        } else if (multiLang) {
            // 首选火山一句话识别（多语种，与流式共用同一把 Key，已实测日语通过）；
            // 没填火山 Key 时才借任意一个已填 Key 的云端渠道（SenseVoice/Whisper）
            useNostream = TranslateStore.asrKey(this, "volc").isNotEmpty()
            if (!useNostream && sessionChannel == null) {
                sessionChannel = listOf("siliconflow", "groq", "openai", "custom")
                    .firstNotNullOfOrNull { id ->
                        CloudAsr.channelOf(this, id)
                            ?.takeIf { TranslateStore.asrKey(this, it.id).isNotEmpty() }
                    }
            }
            cloudPrimary = useNostream || sessionChannel != null
        }
        // 启动前状态快照（同步写盘）：万一随后闪退，诊断日志里能看到当时走的哪条路。
        // 只记“是否已填”布尔值，绝不记密钥本身。
        DiagLog.logSync(
            this, "tr_start_state",
            mapOf(
                "engine" to TranslateStore.asrEngine(this),
                "from" to from, "to" to to, "effFrom" to effFrom,
                "tx" to useTx, "volc" to useVolc, "nostream" to useNostream,
                "txConfigured" to TranslateStore.txConfigured(this),
                "volcKey" to TranslateStore.asrKey(this, "volc").isNotEmpty(),
                "mic" to TranslateStore.micSource(this),
                "provider" to TranslateStore.provider(this),
                "recPerm" to (checkSelfPermission(Manifest.permission.RECORD_AUDIO) ==
                    PackageManager.PERMISSION_GRANTED),
                "overlay" to Settings.canDrawOverlays(this)
            )
        )
        // v0.9.2：选了腾讯引擎但三项密钥未填满时，禁止静默回退到别的引擎启动。
        // 旧行为是悄悄改走火山一句话/离线，界面上还显示「腾讯云实时识别」，用户
        // 以为在用腾讯——这就是「翻译用的模型对不上」。现在明确拦下并提示去填。
        if (TranslateStore.asrEngine(this) == "tx" && !TranslateStore.txConfigured(this)) {
            DiagLog.logSync(
                this, "tr_blocked",
                mapOf("engine" to "tx", "reason" to "tx_keys_missing")
            )
            val msg = "❌ 腾讯引擎未配置：请先在翻译设置 → 识别引擎 → 引擎密钥里填 AppId、SecretId、SecretKey"
            status(msg)
            android.widget.Toast.makeText(this, msg, android.widget.Toast.LENGTH_LONG).show()
            TranslateStore.setActive(this, false)
            TranslateControl.active = false
            TranslateControl.sessionStartedAt = 0L
            stopForegroundCompat()
            stopSelf()
            return
        }
        // 日韩 nostream 流式化（v0.8.9）：流式接口实测不支持日语（见 VolcSegmentAsr 注释），
        // 仍走一句话识别，但切段与聚合都按音频时间走，不再按 Vosk 垃圾文本标点切。
        vadMode = multiLang && useNostream
        vadSegStartSample = -1L
        vadLastSpeechSample = 0L
        synchronized(aggBuf) { aggBuf.setLength(0); aggLastAudioEndSec = -1.0; aggLastFeedWallAt = 0L }
        LlmTranslate.resetSession()
        domainProbe.setLength(0)
        running = true
        pausedByCall = false
        lastActivityAt = System.currentTimeMillis()
        committedLen = 0; segStartAt = 0L; lastPartial = ""; lastCommitSample = 0L
        entries.clear()
        TranslateControl.entriesBuffer.clear()
        if (startedAt == 0L) startedAt = System.currentTimeMillis()
        TranslateControl.sessionStartedAt = startedAt
        // 会话录音存档：与翻译同步开始（写入点挂在 VoskManager 的采集路径上）
        if (TranslateStore.recordAudio(this)) {
            SessionRecorder.start(applicationContext, startedAt)
        }
        TranslateStore.setActive(this, true)
        TranslateControl.active = true
        DiagLog.log(
            this, "tr_start",
            mapOf(
                "from" to from, "to" to to, "engine" to TranslateStore.asrEngine(this),
                "nostream" to useNostream, "volc" to useVolc, "tx" to useTx,
                "src" to TranslateStore.micSource(this), "provider" to TranslateStore.provider(this)
            )
        )
        TranslateControl.stopper = { stopSession() }
        SubtitleOverlay.show(applicationContext)
        status("🎙 正在听（${engineDesc()}）…")
        handler.removeCallbacks(callWatcher)
        handler.postDelayed(callWatcher, 1000)
        if ((effFrom == "ja" || effFrom == "ko") && !cloudPrimary && !useTx) {
            status("❌ 日语/韩语识别需先填 Key：火山、硅基流动、Groq 任一（在「识别引擎」里）")
            handler.postDelayed({ stopSession() }, 3500)
            return
        }
        if (useVolc) { beginEngines(); return }
        if (useTx) { beginEngines(); return } // 腾讯不依赖离线模型，直接进引擎
        val lang = VoskManager.variant(this, effFrom)
        if (VoskManager.isReady(this, lang)) { beginEngines(); return }
        status("正在下载${lang.label}识别模型（${lang.sizeHint}，只下一次）…")
        VoskManager.ensureModel(this, lang) { state, pct ->
            if (!running) return@ensureModel
            when {
                state == "ready" -> beginEngines()
                state == "downloading" -> status("下载模型中… $pct%")
                state.startsWith("error") -> {
                    status("❌ 模型下载失败，可换网络后重试")
                    handler.postDelayed({ stopSession() }, 2500)
                }
            }
        }
    }

    /** 引擎启动入口（带兜底）：任一引擎分支抛异常都不闪退，只记日志、报状态、延时停会话。 */
    private fun beginEngines() {
        try {
            beginEnginesInner()
        } catch (t: Throwable) {
            DiagLog.logSync(
                this, "engine_start_fail",
                mapOf(
                    "error" to t.toString(),
                    "engine" to TranslateStore.asrEngine(this),
                    "tx" to useTx, "volc" to useVolc, "mic" to TranslateStore.micSource(this)
                )
            )
            status("❌ 引擎启动失败：${t.message ?: "未知错误"}，已记入诊断日志")
            handler.postDelayed({ stopSession() }, 2500)
        }
    }

    private fun beginEnginesInner() {
        if (!running || pausedByCall) return
        lastActivityAt = System.currentTimeMillis()
        committedLen = 0; segStartAt = 0L; lastPartial = ""
        status("🎙 正在听（${engineDesc()}）… 边说边翻")
        if (useTx) {
            // 腾讯云实时 ASR：拉模式，阻塞队列桥接现有推模式采集泵（不让 SDK 自己采麦）。
            // startPumpMic 不写 VoskManager 环形缓冲（samplePos 不走），commitSegment 对腾讯
            // 路径一律 wav=null 直接进 onSentence（见 commitSegment 开头）。
            val session = TencentAsr(
                context = this,
                appId = TranslateStore.txAppId(this).toIntOrNull() ?: 0,
                secretId = TranslateStore.txSecretId(this),
                credentialProvider = com.tencent.aai.auth.LocalCredentialProvider(
                    TranslateStore.txSecretKey(this)
                ),
                engineModelType = TencentAsr.engineFor(effFrom),
                onPartial = { p -> onPartialText(p) },
                onFinal = { text -> onFinalText(text) },
                onError = { msg ->
                    DiagLog.log(this, "asr_error", mapOf("engine" to "tx_stream", "msg" to msg.take(120)))
                    status("❌ $msg")
                },
                onReady = { status("🎙 正在听（${engineDesc()}）… 边说边翻") }
            )
            tx = session
            session.start()
            if (TranslateStore.micSource(this) == "internal") {
                if (!InternalCapture.isRunning() && !InternalCapture.resumeCapture()) {
                    status("❌ 内部声音需先在 App 内授权一次")
                    return
                }
                VoskManager.startPumpChunks(
                    chunkSource = { InternalCapture.nextChunk() },
                    onChunk = { bytes -> session.sendPcm(bytes) },
                    onLevel = { pct -> onAudioLevel(pct) }
                )
            } else {
                VoskManager.startPumpMic(this,
                    onChunk = { bytes -> session.sendPcm(bytes) },
                    onLevel = { pct -> onAudioLevel(pct) },
                    onError = { msg -> status("❌ 录音中断：$msg") }
                )
            }
            return
        }
        if (useVolc) {
            val session = VolcAsr(
                apiKey = TranslateStore.asrKey(this, "volc"),
                onPartial = { p -> onPartialText(p) },
                onFinal = { text -> onFinalText(text) },
                onError = { msg ->
                    DiagLog.log(this, "asr_error", mapOf("engine" to "volc_stream", "msg" to msg.take(120)))
                    status("❌ $msg")
                },
                onReady = { status("🎙 正在听（${engineDesc()}）… 边说边翻") }
            )
            volc = session
            session.start()
            if (TranslateStore.micSource(this) == "internal") {
                if (!InternalCapture.isRunning() && !InternalCapture.resumeCapture()) {
                    status("❌ 内部声音需先在 App 内授权一次")
                    return
                }
                VoskManager.startPumpChunks(
                    chunkSource = { InternalCapture.nextChunk() },
                    onChunk = { bytes -> session.sendPcm(bytes) },
                    onLevel = { pct -> onAudioLevel(pct) }
                )
            } else {
                VoskManager.startPumpMic(this,
                    onChunk = { bytes -> session.sendPcm(bytes) },
                    onLevel = { pct -> onAudioLevel(pct) },
                    onError = { msg -> status("❌ 录音中断：$msg") }
                )
            }
            return
        }
        if (TranslateStore.micSource(this) == "internal") {
            if (!InternalCapture.isRunning() && !InternalCapture.resumeCapture()) {
                status("❌ 内部声音需先在 App 内授权一次")
                return
            }
            VoskManager.startListeningPcm(this, VoskManager.variant(this, effFrom),
                chunkSource = { InternalCapture.nextChunk() },
                onPartial = { p -> onPartialText(p) },
                onFinal = { text -> onFinalText(text) },
                onLevel = { pct -> onAudioLevel(pct) },
                onError = { msg -> status("❌ 识别中断：$msg") }
            )
        } else {
            VoskManager.startListening(this, VoskManager.variant(this, effFrom),
                onPartial = { p -> onPartialText(p) },
                onFinal = { text -> onFinalText(text) },
                onLevel = { pct -> onAudioLevel(pct) },
                onError = { msg -> status("❌ 识别中断：$msg") }
            )
        }
        // VAD 切段 ticker：通话暂停/恢复会重进 beginEngines，这里统一负责启停
        if (vadMode && !vadTicking) {
            vadSegStartSample = -1L
            vadLastSpeechSample = 0L
            vadHandler.removeCallbacks(vadTicker)
            vadHandler.postDelayed(vadTicker, 200)
            vadTicking = true
        }
    }

    private fun pauseEngines() {
        volc?.closeNow(); volc = null
        tx?.closeNow(); tx = null
        VoskManager.pauseCapture(this)
    }

    private val callWatcher = object : Runnable {
        override fun run() {
            if (!running) return
            val inCall = Speaker.inVoiceCall(this@TranslateService)
            if (inCall && !pausedByCall) {
                pausedByCall = true
                pauseEngines()
                status("📞 通话中，已暂停（结束后自动继续）")
            } else if (!inCall && pausedByCall) {
                pausedByCall = false
                status("🎙 通话结束，继续翻译")
                beginEngines()
            }
            // 句子聚合的停顿冲刷：碎片停了 1.6 秒还没攒够一句，也先翻出去。
            // VAD 模式按音频时间计时（段到达慢不再误触发）：音频流里距上一段结束 ≥1.6 秒
            // 才冲；内部音频无数据时采样位置不走，另以 4 秒墙钟兜底。
            if (running && !pausedByCall && aggBuf.isNotEmpty()) {
                val nowWall = System.currentTimeMillis()
                val audioDue = aggLastAudioEndSec >= 0 &&
                    (VoskManager.samplePos() / 16000.0 - aggLastAudioEndSec >= 1.6)
                val wallDue = if (aggLastAudioEndSec >= 0) nowWall - aggLastFeedWallAt >= 4000
                    else nowWall - aggLastAt >= 1600
                if (audioDue || wallDue) flushAgg()
            }
            // 静音看门狗：连续无声音/无识别超过阈值 → 自动停（防忘关持续扣费）
            if (running && !pausedByCall) {
                val min = TranslateStore.silenceStopMinutes(this@TranslateService)
                if (min > 0 && lastActivityAt > 0 &&
                    System.currentTimeMillis() - lastActivityAt > min * 60_000L) {
                    android.widget.Toast.makeText(
                        this@TranslateService,
                        "静音超过 $min 分钟，已自动停止翻译",
                        android.widget.Toast.LENGTH_LONG
                    ).show()
                    stopSession()
                    return
                }
            }
            handler.postDelayed(this, 1000)
        }
    }

    // ——— 流式切分：与翻译页时代相同的规则（标点切 / 2.2 秒切 / 42 字强切）———
    // VAD 模式（日韩 nostream）不走这里：切段由 vadTicker 按能量停顿驱动。
    private fun onPartialText(p: String) {
        if (p.isNotBlank()) lastActivityAt = System.currentTimeMillis()
        if (vadMode) return // Vosk 文本（日语音频经中文模型）不可信，不参与切段
        // 日韩云端模式下离线 partial 是乱码：只用于内部分段，不上屏（定稿以云端识别为准）
        if (!cloudPrimary) {
            TranslateControl.publishPartial(p)
            SubtitleOverlay.partial(p)
        }
        if (p.length < committedLen) { committedLen = 0; segStartAt = 0L }
        lastPartial = p
        if (p.length <= committedLen) return
        val cand = p.substring(committedLen)
        if (cand.isBlank()) { committedLen = p.length; return }
        if (segStartAt == 0L) segStartAt = System.currentTimeMillis()
        val punct = charArrayOf('。', '！', '？', '；', '，', '、', '.', '!', '?', ';')
        val idx = cand.indexOfLast { it in punct }
        if (idx >= 3) { commitUpTo(p, committedLen + idx + 1); return }
        val now = System.currentTimeMillis()
        if ((now - segStartAt >= 2200 && cand.length >= 4) || cand.length >= 42) {
            commitUpTo(p, p.length)
        }
    }

    private fun commitUpTo(full: String, end: Int) {
        val text = full.substring(committedLen, end).trim().trimStart('，', '、', ',', ' ')
        committedLen = end
        segStartAt = System.currentTimeMillis()
        if (text.isNotEmpty()) {
            val endSample = VoskManager.samplePos()
            val wav = VoskManager.extractWav(lastCommitSample, endSample)
            lastCommitSample = endSample
            commitSegment(text, wav)
        }
    }

    private fun onFinalText(t: String) {
        if (t.isNotBlank()) lastActivityAt = System.currentTimeMillis()
        if (vadMode) {
            // Vosk 的 final 也是按中文模型乱切的：不提交，尾巴由 VAD ticker / stopSession 处理
            committedLen = 0; segStartAt = 0L; lastPartial = ""
            return
        }
        val rest = if (t.length > committedLen) t.substring(committedLen) else ""
        committedLen = 0; segStartAt = 0L; lastPartial = ""
        TranslateControl.publishPartial("")
        SubtitleOverlay.partial("")
        val endSample = VoskManager.samplePos()
        val r = rest.trim()
        if (r.isNotEmpty()) {
            val wav = VoskManager.extractWav(lastCommitSample, endSample)
            commitSegment(r, wav)
        }
        lastCommitSample = endSample
    }

    /** extractWav 给的是带头 WAV；一句话识别要裸 PCM：定位 data 块后截取。 */
    private fun stripWavHeader(wav: ByteArray): ByteArray {
        if (wav.size <= 44) return wav
        val limit = minOf(wav.size - 8, 512)
        for (i in 0 until limit) {
            if (wav[i] == 'd'.code.toByte() && wav[i + 1] == 'a'.code.toByte() &&
                wav[i + 2] == 't'.code.toByte() && wav[i + 3] == 'a'.code.toByte()
            ) {
                return wav.copyOfRange(i + 8, wav.size)
            }
        }
        return wav.copyOfRange(44, wav.size)
    }

    private fun commitSegment(voskText: String, wav: ByteArray?, audioEndSec: Double? = null) {
        if (useTx) {
            // 腾讯路径：startPumpMic 不写 VoskManager 环形缓冲，wav 无意义，直接进句子聚合
            if (voskText.isNotBlank()) onSentence(voskText)
            return
        }
        if (cloudPrimary && useNostream) {
            if (wav == null) return
            VolcSegmentAsr.transcribe(
                this, TranslateStore.asrKey(this, "volc"), stripWavHeader(wav), effFrom
            ) { text, reason ->
                if (text.isNullOrBlank()) {
                    // v0.9.2：报错必须可见（状态栏 + 翻译页红字 + toast），并把真实原因记进日志。
                    // 旧代码只记秃秃的 asr_error，2026-11-23 当天全是 12 秒无响应却查不出原因。
                    val why = reason ?: "识别失败（原因未知）"
                    DiagLog.log(this, "asr_error", mapOf(
                        "engine" to "volc_nostream", "lang" to effFrom,
                        "reason" to why.take(200)
                    ))
                    status("❌ 识别失败：$why")
                    TranslateControl.publishError("识别失败：$why")
                }
                val srcFinal = text?.takeIf { it.isNotBlank() } ?: ""
                if (srcFinal.isNotBlank()) feedSentence(srcFinal, audioEndSec)
            }
            return
        }
        val ch = sessionChannel
        if (ch == null || wav == null) { onSentence(voskText); return }
        CloudAsr.transcribe(this, ch, wav, effFrom) { text ->
            // 日韩会话：云端失败时宁可丢句，也不用离线乱码充数
            val srcFinal = text?.takeIf { it.isNotBlank() }
                ?: if (cloudPrimary) "" else voskText
            if (srcFinal.isNotBlank()) feedSentence(srcFinal)
        }
    }

    private fun onSentence(src: String) = feedSentence(src)

    // ——— 句子聚合：识别端吐出的碎片先攒成整句再翻译，避免逐词翻译不成句 ———
    // 触发：攒够一句（句末标点且 ≥24 字）/ 攒太长（≥140 字）/ 停顿 1.6 秒（看门狗 tick 检查）。
    private val aggBuf = StringBuilder()
    @Volatile private var aggLastAt = 0L
    /** 最近一次送翻片段的音频结束时间（秒，采样位置换算）；<0 表示无（非 VAD 路径）。 */
    @Volatile private var aggLastAudioEndSec = -1.0
    /** 最近一次 feedSentence 的墙钟时间（VAD 路径下 4 秒兜底的计时起点）。 */
    @Volatile private var aggLastFeedWallAt = 0L

    /** 领域探测：会话开头约 300 字内判定一次领域（供大模型翻译适配风格/术语）。 */
    private val domainProbe = StringBuilder()

    private fun feedSentence(srcRaw: String, audioEndSec: Double? = null) {
        val src = srcRaw.trim()
        if (src.isEmpty()) return
        val merged: String
        synchronized(aggBuf) {
            if (aggBuf.isNotEmpty() && needsSpace(aggBuf[aggBuf.length - 1], src[0])) {
                aggBuf.append(' ')
            }
            aggBuf.append(src)
            aggLastAt = System.currentTimeMillis()
            aggLastFeedWallAt = aggLastAt
            if (audioEndSec != null) aggLastAudioEndSec = audioEndSec
            merged = aggBuf.toString()
        }
        val endsSentence = merged.last() in "。！？!?…"
        if ((endsSentence && merged.length >= 24) || merged.length >= 140) flushAgg()
    }

    private fun needsSpace(prev: Char, next: Char): Boolean {
        val nextLatin = next in 'a'..'z' || next in 'A'..'Z' || next.isDigit()
        if (!nextLatin) return false
        return prev in 'a'..'z' || prev in 'A'..'Z' || prev.isDigit() || prev in ".!?"
    }

    private fun flushAgg() {
        val s = synchronized(aggBuf) {
            val t = aggBuf.toString().trim()
            aggBuf.setLength(0)
            aggLastAudioEndSec = -1.0
            t
        }
        if (s.isNotEmpty()) translateAndPublish(s)
    }

    /** 逐句语种判定（双向/自动模式）：假名→日，谚文→韩，汉字占优→中，否则英。 */
    private fun detectLang(text: String): String {
        var cjk = 0; var latin = 0
        for (c in text) when {
            c in 'ぁ'..'ゖ' || c in 'ァ'..'ヺ' -> return "ja"
            c in '가'..'힣' -> return "ko"
            c in '一'..'鿿' -> cjk++
            c in 'a'..'z' || c in 'A'..'Z' -> latin++
        }
        return if (cjk >= latin) "zh" else "en"
    }

    private fun resolvePair(src: String): Pair<String, String> {
        if (from != "auto" && to != "auto") return from to to
        val d = detectLang(src)
        if (to == "auto") return d to (if (d == "zh") "en" else "zh")
        return d to to
    }

    private fun translateAndPublish(src: String) {
        // 领域自动感知：开头几句判定领域后固定（大模型翻译据此适配术语与风格）
        if (LlmTranslate.domain == "通用" && domainProbe.length < 300) {
            domainProbe.append(src)
            val d = LlmTranslate.detectDomain(domainProbe.toString())
            if (d != "通用") LlmTranslate.domain = d
        }
        val (f, t) = resolvePair(src)
        val t0 = System.currentTimeMillis()
        Translator.translate(this, src, f, t) { dst ->
            val (ck, cv) = DiagLog.contentField(this, src)
            DiagLog.log(
                this, "tr_line",
                mapOf(
                    "mtMs" to (System.currentTimeMillis() - t0),
                    "ok" to (dst != null),
                    "provider" to TranslateStore.provider(this),
                    ck to cv
                )
            )
            val text = dst ?: "（翻译失败，检查网络）"
            if (dst != null) LlmTranslate.notePair(src, dst)
            entries.add(TranslateStore.Entry(src, text))
            TranslateControl.publishLine(src, text)
            SubtitleOverlay.line(src, text)
            if (dst != null && TranslateStore.shouldSpeak(this@TranslateService)) {
                Speaker.say(this@TranslateService, dst, -1)
            }
        }
    }

    fun stopSession() {
        if (!running) {
            stopForegroundCompat()
            stopSelf()
            return
        }
        // 尾巴句先提交翻译（与此前行为一致）
        if (vadMode) {
            vadHandler.removeCallbacks(vadTicker); vadTicking = false
            val end = VoskManager.samplePos()
            if (vadSegStartSample >= 0 && end > vadSegStartSample) {
                val wav = VoskManager.extractWav(vadSegStartSample, end)
                if (wav != null) commitSegment("", wav, end / 16000.0)
                vadSegStartSample = end
            }
        }
        val tail = if (!vadMode && lastPartial.length > committedLen)
            lastPartial.substring(committedLen).trim() else ""
        committedLen = 0; segStartAt = 0L; lastPartial = ""
        if (tail.isNotEmpty()) {
            val endSample = VoskManager.samplePos()
            val wav = VoskManager.extractWav(lastCommitSample, endSample)
            lastCommitSample = endSample
            commitSegment(tail, wav)
        }
        flushAgg() // 聚合缓冲里的尾巴一并送翻
        running = false
        DiagLog.log(
            this, "tr_stop",
            mapOf(
                "durSec" to (if (startedAt > 0) (System.currentTimeMillis() - startedAt) / 1000 else 0),
                "lines" to entries.size
            )
        )
        pausedByCall = false
        handler.removeCallbacks(callWatcher)
        volc?.closeNow(); volc = null
        tx?.closeNow(); tx = null
        // v0.9.2：掐断进行中的一句话识别请求——旧代码停会话后请求还在跑，
        // 12 秒后重试、24 秒后才报 asr_error，日志里看着像停后还在出错。
        VolcSegmentAsr.cancelAll()
        VoskManager.pauseCapture(this)
        // 录音收尾：WAV 落盘 → 后台转 m4a（成功后删 WAV 省空间）
        val sid = startedAt
        val recFile = SessionRecorder.stop()
        TranslateStore.setActive(this, false)
        TranslateControl.active = false
        TranslateControl.stopper = null
        TranslateControl.sessionStartedAt = 0L
        TranslateControl.publishPartial("")
        TranslateControl.publishStatus("已停止")
        SubtitleOverlay.hide()
        // 记录延迟 1.2 秒再存：给聚合冲刷出的最后一句留翻译回程时间（新会话已起则跳过）
        handler.postDelayed({
            if (startedAt == 0L) {
                if (entries.isNotEmpty() && sid > 0) {
                    TranslateStore.saveRecord(
                        this,
                        TranslateStore.Record(
                            sid, sid, from, to, entries.toList(),
                            audioId = if (recFile != null) sid else 0L
                        )
                    )
                }
                entries.clear()
            }
        }, 1200)
        if (recFile != null) {
            val m4a = SessionRecorder.m4aFor(applicationContext, sid)
            AacTranscoder.transcodeAsync(recFile, m4a) { ok ->
                if (ok) try { recFile.delete() } catch (e: Exception) { }
            }
        }
        startedAt = 0L
        val deferred = TranslateStore.takeDeferred(this)
        if (deferred.isNotEmpty()) {
            val imp = deferred.filter { it.second }
            val sb = StringBuilder("翻译结束。期间收到 ${deferred.size} 条通知。")
            imp.forEach { sb.append(it.first).append("。") }
            Speaker.sayGated(this, sb.toString(), 0)
        }
        stopForegroundCompat()
        stopSelf()
    }

    override fun onDestroy() {
        if (running) {
            running = false
            handler.removeCallbacks(callWatcher)
            volc?.closeNow(); volc = null
            tx?.closeNow(); tx = null
            VolcSegmentAsr.cancelAll()
            VoskManager.pauseCapture(this)
            SessionRecorder.stop() // 兜底收尾，文件保留（记录可能未及保存引用）
            TranslateStore.setActive(this, false)
            TranslateControl.active = false
            TranslateControl.stopper = null
            TranslateControl.sessionStartedAt = 0L
            SubtitleOverlay.hide()
        }
        vadHandler.removeCallbacks(vadTicker)
        vadTicking = false
        super.onDestroy()
    }

    private fun startForegroundCompat() {
        val nm = getSystemService(NotificationManager::class.java)
        if (Build.VERSION.SDK_INT >= 26 && nm.getNotificationChannel(CH) == null) {
            nm.createNotificationChannel(
                NotificationChannel(CH, "实时翻译", NotificationManager.IMPORTANCE_LOW)
            )
        }
        val openIntent = PendingIntent.getActivity(
            this, 0,
            Intent(this, TranslateActivity::class.java),
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT
        )
        val stopIntent = PendingIntent.getService(
            this, 1,
            Intent(this, TranslateService::class.java).setAction(ACTION_STOP),
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT
        )
        val notif: Notification = Notification.Builder(this, CH)
            .setContentTitle("耳机助手 · 同传进行中")
            .setContentText(engineDesc() + if (TranslateStore.recordAudio(this)) " · 录音存档中" else "")
            .setSmallIcon(android.R.drawable.ic_btn_speak_now)
            .setContentIntent(openIntent)
            .addAction(android.R.drawable.ic_menu_close_clear_cancel, "停止", stopIntent)
            .build()
        try {
            if (Build.VERSION.SDK_INT >= 29) {
                val type = if (TranslateStore.micSource(this) == "internal")
                    ServiceInfo.FOREGROUND_SERVICE_TYPE_DATA_SYNC
                else ServiceInfo.FOREGROUND_SERVICE_TYPE_MICROPHONE
                startForeground(ID, notif, type)
            } else {
                startForeground(ID, notif)
            }
        } catch (e: Exception) {
            try { startForeground(ID, notif) } catch (e2: Exception) { }
        }
    }

    private fun stopForegroundCompat() {
        try {
            if (Build.VERSION.SDK_INT >= 24) stopForeground(STOP_FOREGROUND_REMOVE)
            else @Suppress("DEPRECATION") stopForeground(true)
        } catch (e: Exception) { }
    }

    companion object {
        private const val CH = "translate"
        private const val ID = 43
        const val ACTION_START = "com.bjornwz.earbuddy.TRANSLATE_START"
        const val ACTION_STOP = "com.bjornwz.earbuddy.TRANSLATE_STOP"

        fun start(ctx: Context) {
            val i = Intent(ctx, TranslateService::class.java).setAction(ACTION_START)
            try {
                if (Build.VERSION.SDK_INT >= 26) ctx.startForegroundService(i)
                else ctx.startService(i)
            } catch (e: Exception) { }
        }

        fun stop(ctx: Context) {
            try {
                ctx.startService(Intent(ctx, TranslateService::class.java).setAction(ACTION_STOP))
            } catch (e: Exception) { }
        }
    }
}
