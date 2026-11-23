package com.bjornwz.earbuddy

import android.content.Context
import android.os.Handler
import android.os.Looper
import com.tencent.aai.AAIClient
import com.tencent.aai.audio.data.PcmAudioDataSource
import com.tencent.aai.config.ClientConfiguration
import com.tencent.aai.exception.ClientException
import com.tencent.aai.exception.ServerException
import com.tencent.aai.listener.AudioRecognizeResultListener
import com.tencent.aai.listener.AudioRecognizeStateListener
import com.tencent.aai.model.AudioRecognizeConfiguration
import com.tencent.aai.model.AudioRecognizeRequest
import com.tencent.aai.model.AudioRecognizeResult
import com.tencent.aai.auth.AbsCredentialProvider
import java.util.concurrent.LinkedBlockingQueue
import java.util.concurrent.TimeUnit
import kotlin.concurrent.thread

/**
 * 腾讯云实时语音识别（16k_ja / 16k_ko / 16k_zh）适配器，仿 VolcAsr 对外接口。
 *
 * SDK：官方 Android 实时语音识别 SDK standard 版（asr-realtime-standard-release.aar，
 * v3.1.43），核心类 AAIClient。签名已用 javap 对 AAR 实测核对：
 * - AAIClient(Context, Int appid, Int projectId, String secretId, AbsCredentialProvider)
 * - startAudioRecognize(request, resultListener, stateListener, config) 为阻塞调用，必须放后台线程
 * - stopAudioRecognize() / cancelAudioRecognize() 均返回 Boolean，同样放线程里调用
 * - PcmAudioDataSource.read(short[], length)：填满 length 返回实际长度，-1 表示结束；
 *   每次返回长度须为 20ms（640 采样）的整倍数（最后一个包除外）
 * - sliceType：0 一段话开始 / 1 进行中（非稳态）/ 2 结束（稳态）
 *
 * 音频：SDK 是拉模式，App 是推模式。由 TranslateService 经 startPumpMic/startPumpChunks
 * 喂来的 ByteArray（16k 单声道 16-bit PCM）经 sendPcm 入队，SDK 的 read() 从队列取。
 * 不让 SDK 自己采麦克风（会与现有采集抢麦），SessionRecorder/回声消除/蓝牙 SCO 都不动。
 *
 * 注意：startPumpMic 不写 VoskManager 的环形缓冲（samplePos 不走），所以
 * TranslateService 侧对腾讯路径一律 commitSegment(text, wav=null)，直接进 onSentence。
 */
class TencentAsr(
    private val context: Context,
    private val appId: Int,
    private val secretId: String,
    private val credentialProvider: AbsCredentialProvider,
    private val engineModelType: String,
    private val onPartial: (String) -> Unit,
    private val onFinal: (String) -> Unit,
    private val onError: (String) -> Unit,
    private val onReady: () -> Unit
) {
    companion object {
        private const val PROJECT_ID = 0
        private const val MAX_RECONNECT = 5

        /** 按源语言选引擎：ja→16k_ja，ko→16k_ko，其余（zh/en/auto 回落 zh）→16k_zh。 */
        fun engineFor(lang: String): String = when (lang) {
            "ja" -> "16k_ja"
            "ko" -> "16k_ko"
            else -> "16k_zh"
        }
    }

    private val main = Handler(Looper.getMainLooper())
    @Volatile private var wantRunning = false
    @Volatile private var finished = false
    @Volatile private var lastError: String? = null
    private var worker: Thread? = null
    private var client: AAIClient? = null
    private var dataSource: TxQueueAudioDataSource? = null
    private var reconnectAttempts = 0
    /** 定稿去重：同一句可能同时经 onSliceSuccess(sliceType=2) 与 onSegmentSuccess 到达。 */
    private var lastFinalText: String? = null

    fun start() {
        wantRunning = true
        finished = false
        reconnectAttempts = 0
        lastError = null
        lastFinalText = null
        worker = thread(name = "tx-asr") { runLoop() }
    }

    private fun runLoop() {
        while (wantRunning && !finished) {
            lastError = null
            val ds = TxQueueAudioDataSource()
            dataSource = ds
            // AAIClient 构造可能抛 Error（如 native 库加载失败 UnsatisfiedLinkError，
            // 不是 Exception）：必须抓 Throwable，否则工作线程一死整个进程跟着闪退。
            val c: AAIClient? = try {
                AAIClient(context.applicationContext, appId, PROJECT_ID, secretId, credentialProvider)
            } catch (t: Throwable) {
                lastError = "腾讯客户端创建失败：${t.message}"
                DiagLog.logSync(
                    context, "tx_client_fail",
                    mapOf("error" to t.toString(), "appId" to appId, "engine" to engineModelType)
                )
                null
            }
            if (c == null) {
                dataSource = null
                // 构造失败不会靠 1 秒重试自愈（缺库/坏参数每次都一样）：直接报错，不空转重试。
                main.post { onError(lastError ?: "腾讯客户端创建失败") }
                break
            }
            client = c
            // build() 可能抛 IllegalArgumentException 等：必须包住，否则同上闪退。
            val reqCfg: Pair<AudioRecognizeRequest, AudioRecognizeConfiguration>? = try {
                val request = AudioRecognizeRequest.Builder()
                    .pcmAudioDataSource(ds)
                    .setEngineModelType(engineModelType)
                    .setNeedvad(1)
                    .setWordInfo(1)
                    .setFilterDirty(0)
                    .setFilterModal(0)
                    .setFilterPunc(0)
                    .setConvert_num_mode(1)
                    // 注意：setVadSilenceTime / setMaxSpeakTime 在 16k_ja 上不可用（API 文档限定支持引擎不含 16k_ja），不要设。
                    .build()
                val config = AudioRecognizeConfiguration.Builder()
                    // App 自己有静音看门狗（silenceStopMinutes），不要 SDK 的双重停
                    .setSilentDetectTimeOut(false)
                    .setSilentDetectTimeOutAutoStop(false)
                    .audioFlowSilenceTimeOut(5000)
                    .minVolumeCallbackTime(80)
                    .isCompress(true)
                    .build()
                request to config
            } catch (t: Throwable) {
                lastError = "腾讯请求构建失败：${t.message}"
                DiagLog.logSync(
                    context, "tx_build_fail",
                    mapOf("error" to t.toString(), "engine" to engineModelType)
                )
                null
            }
            if (reqCfg == null) {
                if (client === c) client = null
                try { c.release() } catch (t: Throwable) { }
                dataSource = null
                if (!handleFailureAndMaybeRetry()) break
                continue
            }
            val (request, config) = reqCfg
            try {
                ClientConfiguration.setAudioRecognizeConnectTimeout(3000)
                ClientConfiguration.setAudioRecognizeWriteTimeout(5000)
            } catch (t: Throwable) {
            }
            try {
                // 阻塞调用：会话结束（正常 stop / 失败 / 取消）后才返回
                c.startAudioRecognize(request, resultListener, stateListener, config)
            } catch (t: Throwable) {
                if (lastError == null) lastError = t.message ?: "启动失败"
                DiagLog.logSync(context, "tx_start_fail", mapOf("error" to t.toString()))
            }
            // 本轮 client 只由 worker 线程释放，避免双重 release；
            // 只清仍是本轮的字段（closeNow 超时后新会话已起时不能把新 client 清掉）。
            if (client === c) client = null
            try { c.release() } catch (t: Throwable) { }
            dataSource = null
            if (!wantRunning || finished) break
            // 非正常结束：按错误重连（无错误但会话提前结束也视为连接中断）
            if (lastError == null) lastError = "连接中断"
            if (!handleFailureAndMaybeRetry()) break
        }
    }

    /** 失败后 1 秒重试，最多 MAX_RECONNECT 次；超限返回 false（调用方停止并报错）。 */
    private fun handleFailureAndMaybeRetry(): Boolean {
        reconnectAttempts++
        if (reconnectAttempts > MAX_RECONNECT) {
            val msg = lastError ?: "连接中断"
            main.post { onError("腾讯识别多次中断，已停止（$msg）") }
            return false
        }
        main.post { onReady() } // 借 onReady 刷新状态为“正在听”，不弹错误
        try { Thread.sleep(1000) } catch (e: Exception) { }
        return wantRunning && !finished
    }

    /** 喂入 16k/16bit/单声道 PCM（只入队，实际取用由 SDK 的 read() 拉取）。 */
    fun sendPcm(pcm: ByteArray) {
        if (!wantRunning || finished) return
        val ds = dataSource ?: return
        ds.offerPcm(bytesToShorts(pcm))
    }

    /** 正常结束：stopAudioRecognize 是阻塞调用，放线程里；worker 线程随后释放 client。 */
    fun finish() {
        if (finished) return
        finished = true
        wantRunning = false
        val c = client
        thread {
            try { c?.stopAudioRecognize() } catch (t: Throwable) { }
        }
    }

    fun closeNow() {
        finished = true
        wantRunning = false
        val c = client
        thread {
            try { c?.cancelAudioRecognize() } catch (t: Throwable) { }
        }
        dataSource?.stop()
        try { worker?.join(1500) } catch (t: Throwable) { }
        worker = null
    }

    private val resultListener = object : AudioRecognizeResultListener {
        override fun onSliceSuccess(
            request: AudioRecognizeRequest,
            result: AudioRecognizeResult,
            seq: Int
        ) {
            val text = result.text ?: return
            if (text.isBlank()) return
            if (result.sliceType == 2) {
                deliverFinal(text)
            } else {
                main.post { onPartial(text) }
            }
        }

        override fun onSegmentSuccess(
            request: AudioRecognizeRequest,
            result: AudioRecognizeResult,
            seq: Int
        ) {
            val text = result.text ?: return
            if (text.isBlank()) return
            deliverFinal(text)
        }

        override fun onSuccess(request: AudioRecognizeRequest, result: String) {
            // 全文：流式已逐句提交，这里仅作会话正常结束的信号，不再重复送。
        }

        override fun onFailure(
            request: AudioRecognizeRequest,
            clientException: ClientException?,
            serverException: ServerException?,
            response: String?
        ) {
            val msg = when {
                clientException != null -> "客户端错误 ${clientException.code}：${clientException.message}"
                serverException != null -> "服务端错误：${serverException.message}"
                !response.isNullOrBlank() -> "识别失败：$response"
                else -> "识别失败"
            }
            lastError = msg
            // 不在这里直接 onError：worker 线程在 startAudioRecognize 返回后统一处理（重连或报错）。
        }
    }

    private val stateListener = object : AudioRecognizeStateListener {
        override fun onStartRecord(request: AudioRecognizeRequest) {
            main.post { onReady() }
        }

        override fun onStopRecord(request: AudioRecognizeRequest) {}

        @Deprecated("SDK 已废弃，建议用 onVoiceDb")
        override fun onVoiceVolume(request: AudioRecognizeRequest, volume: Int) {}

        override fun onVoiceDb(volumeDb: Float) {
            // 音量：服务已由采集泵的 onAudioLevel 回调（音量条＋静音看门狗），这里不重复处理。
        }

        override fun onNextAudioData(audioDatas: ShortArray, readBufferLength: Int) {}

        override fun onSilentDetectTimeOut() {}
    }

    /** 定稿去重：同一句经 onSliceSuccess(sliceType=2) 与 onSegmentSuccess 各到一次时只送一次。 */
    private fun deliverFinal(text: String) {
        synchronized(this) {
            if (text == lastFinalText) return
            lastFinalText = text
        }
        main.post { onFinal(text) }
    }

    private fun bytesToShorts(bytes: ByteArray): ShortArray {
        val n = bytes.size / 2
        val out = ShortArray(n)
        for (i in 0 until n) {
            val lo = bytes[i * 2].toInt() and 0xFF
            val hi = bytes[i * 2 + 1].toInt() and 0xFF
            out[i] = ((hi shl 8) or lo).toShort()
        }
        return out
    }
}

/**
 * 阻塞队列桥接的 PcmAudioDataSource：App 推模式喂入，SDK 拉模式取用。
 * read() 阻塞直到凑满 length 采样（与 DemoAudioRecordDataSource 的 AudioRecord.read 语义一致），
 * 停止时返回 -1。队列满时丢最旧（保实时性，仿 VolcAsr 的 MAX_PENDING 做法）。
 */
class TxQueueAudioDataSource : PcmAudioDataSource {
    companion object {
        private const val MAX_CHUNKS = 40 // 每块 4096 采样，约 10 秒缓冲
    }

    private val queue = LinkedBlockingQueue<ShortArray>()
    @Volatile private var stopped = false
    private var pending: ShortArray? = null
    private var pendingPos = 0

    fun offerPcm(shorts: ShortArray) {
        if (stopped || shorts.isEmpty()) return
        while (queue.size >= MAX_CHUNKS) queue.poll()
        queue.offer(shorts)
    }

    override fun read(audioPcmData: ShortArray, length: Int): Int {
        if (length <= 0) return 0
        var filled = 0
        while (filled < length) {
            val p = pending
            if (p != null && pendingPos < p.size) {
                val take = minOf(p.size - pendingPos, length - filled)
                System.arraycopy(p, pendingPos, audioPcmData, filled, take)
                pendingPos += take
                filled += take
                if (pendingPos >= p.size) {
                    pending = null
                    pendingPos = 0
                }
                continue
            }
            val chunk = try {
                queue.poll(200, TimeUnit.MILLISECONDS)
            } catch (e: InterruptedException) {
                null
            }
            if (chunk == null) {
                if (stopped) return if (filled > 0) filled else -1
                continue
            }
            pending = chunk
            pendingPos = 0
        }
        return filled
    }

    override fun start() {
        stopped = false
    }

    override fun stop() {
        stopped = true
    }

    override fun isSetSaveAudioRecordFiles(): Boolean = false
}
