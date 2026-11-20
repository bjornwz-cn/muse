package com.bjornwz.earbuddy

import android.content.Context
import android.media.AudioDeviceInfo
import android.media.AudioFormat
import android.media.AudioManager
import android.media.AudioRecord
import android.media.MediaRecorder
import android.media.audiofx.AcousticEchoCanceler
import android.media.audiofx.AutomaticGainControl
import android.media.audiofx.NoiseSuppressor
import android.os.Build
import android.os.Handler
import android.os.Looper
import org.json.JSONObject
import org.vosk.Model
import org.vosk.Recognizer
import java.io.File
import java.io.FileOutputStream
import java.net.HttpURLConnection
import java.net.URL
import java.util.zip.ZipInputStream
import kotlin.math.sqrt

/**
 * Vosk 离线流式识别（v0.3.1 自研录音管线）：
 * - 模型：标准小模型 / 高精度大模型，按语言在设置里选，首次使用下载一次
 * - 录音：AudioRecord + VOICE_RECOGNITION 音源 + 系统降噪/自动增益/回声消除
 * - 收音来源可切换：手机麦克风（默认，音质最好）/ 蓝牙耳机麦克风（SCO 路由，离嘴近）
 * - 实时电平回调，供 UI 显示音量条与过低提示
 */
object VoskManager {

    data class Lang(val code: String, val label: String, val urls: List<String>, val dirName: String, val sizeHint: String)

    // 多源下载：小模型优先走 GitHub 镜像（与应用更新同一通道，国内可达），官方源兜底；
    // 大模型体积超 GitHub 单文件上限，只留官方源，另可本地导入 zip 包。
    private const val GH = "https://raw.githubusercontent.com/bjornwz-cn/muse/main/models/"
    val ZH = Lang("zh", "中文", listOf(GH + "vosk-model-small-cn-0.22.zip", "https://alphacephei.com/vosk/models/vosk-model-small-cn-0.22.zip"), "vosk-zh", "约 42MB")
    val EN = Lang("en", "English", listOf(GH + "vosk-model-small-en-us-0.15.zip", "https://alphacephei.com/vosk/models/vosk-model-small-en-us-0.15.zip"), "vosk-en", "约 40MB")
    val ZH_BIG = Lang("zh", "中文·高精度", listOf("https://alphacephei.com/vosk/models/vosk-model-cn-0.22.zip"), "vosk-zh-big", "约 1.36GB")
    val EN_BIG = Lang("en", "English·高精度", listOf("https://alphacephei.com/vosk/models/vosk-model-en-us-0.22-lgraph.zip"), "vosk-en-big", "约 128MB")

    /** 按当前精度设置取实际模型。tier: std / high */
    fun variant(ctx: Context, code: String): Lang {
        val high = TranslateStore.modelTier(ctx) == "high"
        return when {
            code == "en" && high -> EN_BIG
            code == "en" -> EN
            high -> ZH_BIG
            else -> ZH
        }
    }

    private val main = Handler(Looper.getMainLooper())

    private fun baseDir(ctx: Context, lang: Lang) = File(ctx.filesDir, "models/${lang.dirName}")

    fun resolveModelPath(ctx: Context, lang: Lang): File? {
        val base = baseDir(ctx, lang)
        if (!base.exists()) return null
        if (File(base, "conf").exists()) return base
        return base.listFiles()?.firstOrNull { File(it, "conf").exists() }
    }

    fun isReady(ctx: Context, lang: Lang) = resolveModelPath(ctx, lang) != null

    /** 下载+解压模型（多源依次尝试，任一源成功即止）。cb 主线程回调：(状态, 进度 0-100 或 -1)。 */
    fun ensureModel(ctx: Context, lang: Lang, cb: (String, Int) -> Unit) {
        if (isReady(ctx, lang)) { cb("ready", 100); return }
        val appCtx = ctx.applicationContext
        Thread {
            var lastErr = ""
            for ((idx, url) in lang.urls.withIndex()) {
                try {
                    main.post { cb("source", idx + 1) }
                    val zipFile = File(appCtx.cacheDir, "${lang.dirName}.zip")
                    if (zipFile.exists()) zipFile.delete()
                    val conn = URL(url).openConnection() as HttpURLConnection
                    conn.connectTimeout = 15000
                    conn.readTimeout = 45000
                    conn.connect()
                    if (conn.responseCode !in 200..299) throw Exception("HTTP ${conn.responseCode}")
                    val total = conn.contentLengthLong
                    var read = 0L
                    var lastPct = -1
                    conn.inputStream.use { ins ->
                        FileOutputStream(zipFile).use { out ->
                            val buf = ByteArray(128 * 1024)
                            while (true) {
                                val n = ins.read(buf)
                                if (n < 0) break
                                out.write(buf, 0, n)
                                read += n
                                if (total > 0) {
                                    val pct = (read * 100 / total).toInt()
                                    if (pct != lastPct) { lastPct = pct; main.post { cb("downloading", pct) } }
                                }
                            }
                        }
                    }
                    if (total > 0 && read < total) throw Exception("下载不完整 $read/$total")
                    main.post { cb("unzipping", -1) }
                    unzipInto(appCtx, lang, zipFile)
                    zipFile.delete()
                    if (isReady(appCtx, lang)) { main.post { cb("ready", 100) }; return@Thread }
                    throw Exception("模型解压后不完整")
                } catch (e: Exception) {
                    lastErr = e.message ?: e.toString()
                }
            }
            main.post { cb("error:所有下载源都失败（$lastErr）", -1) }
        }.start()
    }

    private fun unzipInto(ctx: Context, lang: Lang, zipFile: File) {
        val dest = baseDir(ctx, lang).apply { mkdirs() }
        ZipInputStream(zipFile.inputStream()).use { zis ->
            while (true) {
                val e = zis.nextEntry ?: break
                val f = File(dest, e.name)
                if (e.isDirectory) f.mkdirs()
                else {
                    f.parentFile?.mkdirs()
                    FileOutputStream(f).use { out -> zis.copyTo(out) }
                }
                zis.closeEntry()
            }
        }
    }

    /** 本地导入：把用户选的模型 zip 解压到该模型目录（大模型走这条：电脑下载后传入手机）。 */
    fun importModel(ctx: Context, lang: Lang, stream: java.io.InputStream, cb: (String, Int) -> Unit) {
        val appCtx = ctx.applicationContext
        Thread {
            try {
                val zipFile = File(appCtx.cacheDir, "${lang.dirName}-import.zip")
                stream.use { ins -> FileOutputStream(zipFile).use { out -> ins.copyTo(out) } }
                main.post { cb("unzipping", -1) }
                unzipInto(appCtx, lang, zipFile)
                zipFile.delete()
                if (isReady(appCtx, lang)) main.post { cb("ready", 100) }
                else main.post { cb("error:包内不是该模型的完整文件", -1) }
            } catch (e: Exception) {
                main.post { cb("error:${e.message}", -1) }
            }
        }.start()
    }

    // ——— 自研录音管线 ———
    private var model: Model? = null
    private var modelKey: String? = null
    private var record: AudioRecord? = null
    private var thread: Thread? = null
    @Volatile private var listening = false
    private var scoOn = false

    // ——— 会话音频留痕（供云端识别切段重识别）：绝对采样位置与录音对齐 ———
    private val audioChunks = ArrayList<ShortArray>()
    private var audioDropped = 0L
    private var audioEnd = 0L

    @Synchronized private fun resetAudioLog() {
        audioChunks.clear(); audioDropped = 0L; audioEnd = 0L
    }

    @Synchronized private fun appendAudio(chunk: ShortArray) {
        if (chunk.isEmpty()) return
        SessionRecorder.write(chunk) // 会话录音存档（未开启时无开销）
        audioChunks.add(chunk)
        audioEnd += chunk.size
        // 最多留 10 分钟（16k*2B*600 ≈ 19MB），更老的从头部丢弃
        while (audioEnd - audioDropped > 16000L * 600 && audioChunks.size > 1) {
            audioDropped += audioChunks.removeAt(0).size
        }
    }

    /** 已喂给识别器的采样数（绝对位置）。 */
    fun samplePos(): Long = audioEnd

    /** 截取 [from, to) 采样段并编码为 16k 单声道 WAV；越界自动收敛，无数据返回 null。 */
    @Synchronized fun extractWav(from: Long, to: Long): ByteArray? {
        val f = maxOf(from, audioDropped)
        val t = minOf(to, audioEnd)
        if (t - f < 1600) return null   // 短于 0.1 秒不送
        val need = (t - f).toInt()
        val pcm = ShortArray(need)
        var written = 0
        var pos = audioDropped
        for (c in audioChunks) {
            val cStart = pos
            val cEnd = pos + c.size
            pos = cEnd
            if (cEnd <= f) continue
            if (cStart >= t) break
            val s0 = maxOf(f, cStart).toInt()
            val s1 = minOf(t, cEnd).toInt()
            val count = s1 - s0
            System.arraycopy(c, (s0 - cStart).toInt(), pcm, written, count)
            written += count
            if (written >= need) break
        }
        if (written == 0) return null
        return wavEncode(pcm.copyOf(written))
    }

    private fun wavEncode(pcm: ShortArray): ByteArray {
        val dataSize = pcm.size * 2
        val out = java.io.ByteArrayOutputStream(44 + dataSize)
        fun w32(v: Int) { out.write(v and 0xFF); out.write(v shr 8 and 0xFF); out.write(v shr 16 and 0xFF); out.write(v shr 24 and 0xFF) }
        fun w16(v: Int) { out.write(v and 0xFF); out.write(v shr 8 and 0xFF) }
        out.write("RIFF".toByteArray()); w32(36 + dataSize)
        out.write("WAVE".toByteArray()); out.write("fmt ".toByteArray())
        w32(16); w16(1); w16(1); w32(16000); w32(32000); w16(2); w16(16)
        out.write("data".toByteArray()); w32(dataSize)
        for (v in pcm) w16(v.toInt())
        return out.toByteArray()
    }
    private var prevAudioMode = AudioManager.MODE_NORMAL

    // ——— 防回授（v0.8.3）：外放朗读时麦克风会把译文当原文再翻，形成死循环 ———
    // 朗读期间及结束后 400ms（回声尾巴）内，麦克风帧一律丢弃；声音被关在耳机里时不外泄、不丢帧，保持全双工。
    // 内部音频路径已在 InternalCapture 处丢帧，不经过这里。
    @Volatile private var lastSpeakEndAt = 0L

    private fun echoGuard(ctx: Context): Boolean {
        val am = ctx.getSystemService(AudioManager::class.java)
        val contained = try {
            am.getDevices(AudioManager.GET_DEVICES_OUTPUTS).any {
                it.type == AudioDeviceInfo.TYPE_BLUETOOTH_A2DP ||
                    it.type == AudioDeviceInfo.TYPE_BLUETOOTH_SCO ||
                    it.type == AudioDeviceInfo.TYPE_BLE_HEADSET ||
                    it.type == AudioDeviceInfo.TYPE_BLE_BROADCAST ||
                    it.type == AudioDeviceInfo.TYPE_WIRED_HEADSET ||
                    it.type == AudioDeviceInfo.TYPE_WIRED_HEADPHONES
            }
        } catch (e: Exception) { false }
        if (contained) return false
        if (Speaker.isSpeakingNow()) {
            lastSpeakEndAt = System.currentTimeMillis()
            return true
        }
        return System.currentTimeMillis() - lastSpeakEndAt < 400
    }

    fun startListening(
        ctx: Context, lang: Lang,
        onPartial: (String) -> Unit,
        onFinal: (String) -> Unit,
        onLevel: (Int) -> Unit,
        onError: (String) -> Unit
    ) {
        stopListening()
        val path = resolveModelPath(ctx, lang)
        if (path == null) { onError("模型未就绪"); return }
        val appCtx = ctx.applicationContext
        try {
            val key = lang.dirName
            if (model == null || modelKey != key) {
                model = Model(path.absolutePath)
                modelKey = key
            }
            val recognizer = Recognizer(model, 16000.0f)
            try { recognizer.setWords(false) } catch (e: Exception) {}
            resetAudioLog()

            val useBt = TranslateStore.micSource(appCtx) == "bt"
            val am = appCtx.getSystemService(AudioManager::class.java)
            if (useBt) startBtSco(appCtx, am)

            val minBuf = AudioRecord.getMinBufferSize(
                16000, AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT
            )
            val ar = AudioRecord(
                MediaRecorder.AudioSource.VOICE_RECOGNITION,
                16000, AudioFormat.CHANNEL_IN_MONO,
                AudioFormat.ENCODING_PCM_16BIT, maxOf(minBuf, 8192)
            )
            if (ar.state != AudioRecord.STATE_INITIALIZED) {
                ar.release()
                onError("录音初始化失败（麦克风被占用？）")
                return
            }
            // 指定输入设备
            try {
                val wantType = if (useBt) AudioDeviceInfo.TYPE_BLUETOOTH_SCO else AudioDeviceInfo.TYPE_BUILTIN_MIC
                val dev = am.getDevices(AudioManager.GET_DEVICES_INPUTS).firstOrNull { it.type == wantType }
                if (dev != null) ar.preferredDevice = dev
            } catch (e: Exception) {}
            // 系统音频效果：降噪 / 自动增益 / 回声消除（设备支持才生效）
            try { if (NoiseSuppressor.isAvailable()) NoiseSuppressor.create(ar.audioSessionId)?.enabled = true } catch (e: Exception) {}
            try { if (AutomaticGainControl.isAvailable()) AutomaticGainControl.create(ar.audioSessionId)?.enabled = true } catch (e: Exception) {}
            try { if (AcousticEchoCanceler.isAvailable()) AcousticEchoCanceler.create(ar.audioSessionId)?.enabled = true } catch (e: Exception) {}

            record = ar
            listening = true
            ar.startRecording()
            thread = Thread {
                val buf = ShortArray(4096)
                var lastLevelAt = 0L
                while (listening) {
                    val n = try { ar.read(buf, 0, buf.size) } catch (e: Exception) { -1 }
                    if (n <= 0) continue
                    if (echoGuard(appCtx)) continue   // 防回授：外放朗读期间不收音
                    appendAudio(buf.copyOf(n))
                    // 电平（RMS 归一化），约每 120ms 回调一次
                    val now = System.currentTimeMillis()
                    if (now - lastLevelAt > 120) {
                        lastLevelAt = now
                        var sum = 0.0
                        for (i in 0 until n) sum += (buf[i] * buf[i]).toDouble()
                        val rms = sqrt(sum / n) / 32768.0
                        val pct = (rms * 300).toInt().coerceIn(0, 100)
                        main.post { onLevel(pct) }
                    }
                    try {
                        if (recognizer.acceptWaveForm(buf, n)) {
                            val t = parse(recognizer.result, "text")
                            if (t.isNotEmpty()) main.post { onFinal(t) }
                        } else {
                            val p = parse(recognizer.partialResult, "partial")
                            main.post { onPartial(p) }
                        }
                    } catch (e: Exception) {
                        main.post { onError("识别出错：${e.message}") }
                        break
                    }
                }
                try {
                    val t = parse(recognizer.finalResult, "text")
                    if (t.isNotEmpty()) main.post { onFinal(t) }
                } catch (e: Exception) {}
                try { recognizer.close() } catch (e: Exception) {}
            }.apply { start() }
        } catch (e: Exception) {
            onError("启动识别失败：${e.message}")
        }
    }

    private fun startBtSco(ctx: Context, am: AudioManager) {
        try {
            prevAudioMode = am.mode
            if (Build.VERSION.SDK_INT >= 31) {
                val dev = am.availableCommunicationDevices.firstOrNull { it.type == AudioDeviceInfo.TYPE_BLUETOOTH_SCO }
                if (dev != null && am.setCommunicationDevice(dev)) { scoOn = true; return }
            }
            @Suppress("DEPRECATION")
            am.startBluetoothSco()
            @Suppress("DEPRECATION")
            am.isBluetoothScoOn = true
            am.mode = AudioManager.MODE_IN_COMMUNICATION
            scoOn = true
        } catch (e: Exception) {}
    }

    private fun stopBtSco(ctx: Context) {
        if (!scoOn) return
        scoOn = false
        try {
            val am = ctx.getSystemService(AudioManager::class.java)
            if (Build.VERSION.SDK_INT >= 31) am.clearCommunicationDevice()
            @Suppress("DEPRECATION")
            am.stopBluetoothSco()
            am.mode = prevAudioMode
        } catch (e: Exception) {}
    }

    fun stopListening() {
        listening = false
        try { record?.stop() } catch (e: Exception) {}
        try { thread?.join(600) } catch (e: Exception) {}
        thread = null
        try { record?.release() } catch (e: Exception) {}
        record = null
    }

    /**
     * 暂停采集（后台服务用）：停识别/录音泵与蓝牙路由，
     * 但内部音频只停采集、保留投影本体，下次可免授权恢复。
     */
    fun pauseCapture(ctx: Context) {
        stopListening()
        stopBtSco(ctx.applicationContext)
        InternalCapture.stopCaptureOnly()
    }

    /**
     * PCM 帧驱动的识别（内部音频翻译用）：chunkSource 阻塞取帧，返回 null 表示暂无数据。
     * 与 startListening 共用同一识别/回调语义，只是音源不同。
     */
    fun startListeningPcm(
        ctx: Context, lang: Lang,
        chunkSource: () -> ShortArray?,
        onPartial: (String) -> Unit,
        onFinal: (String) -> Unit,
        onLevel: (Int) -> Unit,
        onError: (String) -> Unit
    ) {
        stopListening()
        val path = resolveModelPath(ctx, lang)
        if (path == null) { onError("模型未就绪"); return }
        try {
            val key = lang.dirName
            if (model == null || modelKey != key) {
                model = Model(path.absolutePath)
                modelKey = key
            }
            val recognizer = Recognizer(model, 16000.0f)
            try { recognizer.setWords(false) } catch (e: Exception) { }
            resetAudioLog()
            listening = true
            thread = Thread {
                var lastLevelAt = 0L
                while (listening) {
                    val chunk = try { chunkSource() } catch (e: Exception) { null }
                    if (chunk == null || chunk.isEmpty()) continue
                    appendAudio(chunk)
                    val now = System.currentTimeMillis()
                    if (now - lastLevelAt > 120) {
                        lastLevelAt = now
                        var sum = 0.0
                        for (v in chunk) sum += (v * v).toDouble()
                        val rms = kotlin.math.sqrt(sum / chunk.size) / 32768.0
                        val pct = (rms * 300).toInt().coerceIn(0, 100)
                        main.post { onLevel(pct) }
                    }
                    try {
                        if (recognizer.acceptWaveForm(chunk, chunk.size)) {
                            val t = parse(recognizer.result, "text")
                            if (t.isNotEmpty()) main.post { onFinal(t) }
                        } else {
                            val p = parse(recognizer.partialResult, "partial")
                            main.post { onPartial(p) }
                        }
                    } catch (e: Exception) {
                        main.post { onError("识别出错：${e.message}") }
                        break
                    }
                }
            }.apply { start() }
        } catch (e: Exception) {
            onError("识别启动失败：${e.message}")
        }
    }

    private fun parse(json: String?, key: String): String {
        if (json.isNullOrBlank()) return ""
        return try { JSONObject(json).optString(key, "").trim() } catch (e: Exception) { "" }
    }

    private fun shortsToBytes(chunk: ShortArray): ByteArray {
        val out = ByteArray(chunk.size * 2)
        for (i in chunk.indices) {
            val v = chunk[i].toInt()
            out[i * 2] = (v and 0xFF).toByte()
            out[i * 2 + 1] = ((v shr 8) and 0xFF).toByte()
        }
        return out
    }

    private fun levelOf(chunk: ShortArray): Int {
        var sum = 0.0
        for (v in chunk) sum += (v * v).toDouble()
        val rms = kotlin.math.sqrt(sum / chunk.size) / 32768.0
        return (rms * 300).toInt().coerceIn(0, 100)
    }

    /**
     * PCM 泵（火山流式识别用·麦克风）：收音配置与 startListening 一致（SCO/指定设备/降噪三件套），
     * 只把 PCM 字节交出去，不跑本地识别。停止走 stopListening。
     */
    fun startPumpMic(
        ctx: Context,
        onChunk: (ByteArray) -> Unit,
        onLevel: (Int) -> Unit,
        onError: (String) -> Unit
    ) {
        stopListening()
        val appCtx = ctx.applicationContext
        try {
            val useBt = TranslateStore.micSource(appCtx) == "bt"
            val am = appCtx.getSystemService(AudioManager::class.java)
            if (useBt) startBtSco(appCtx, am)
            val minBuf = AudioRecord.getMinBufferSize(
                16000, AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT
            )
            val ar = AudioRecord(
                MediaRecorder.AudioSource.VOICE_RECOGNITION,
                16000, AudioFormat.CHANNEL_IN_MONO,
                AudioFormat.ENCODING_PCM_16BIT, maxOf(minBuf, 8192)
            )
            if (ar.state != AudioRecord.STATE_INITIALIZED) {
                ar.release()
                onError("录音初始化失败（麦克风被占用？）")
                return
            }
            try {
                val wantType = if (useBt) AudioDeviceInfo.TYPE_BLUETOOTH_SCO else AudioDeviceInfo.TYPE_BUILTIN_MIC
                val dev = am.getDevices(AudioManager.GET_DEVICES_INPUTS).firstOrNull { it.type == wantType }
                if (dev != null) ar.preferredDevice = dev
            } catch (e: Exception) {}
            try { if (NoiseSuppressor.isAvailable()) NoiseSuppressor.create(ar.audioSessionId)?.enabled = true } catch (e: Exception) {}
            try { if (AutomaticGainControl.isAvailable()) AutomaticGainControl.create(ar.audioSessionId)?.enabled = true } catch (e: Exception) {}
            try { if (AcousticEchoCanceler.isAvailable()) AcousticEchoCanceler.create(ar.audioSessionId)?.enabled = true } catch (e: Exception) {}
            record = ar
            listening = true
            ar.startRecording()
            thread = Thread {
                val buf = ShortArray(4096)
                var lastLevelAt = 0L
                while (listening) {
                    val n = try { ar.read(buf, 0, buf.size) } catch (e: Exception) { -1 }
                    if (n <= 0) continue
                    if (echoGuard(appCtx)) continue   // 防回授：外放朗读期间不喂识别（火山侧有静音补包保活）
                    val chunk = buf.copyOf(n)
                    SessionRecorder.write(chunk)
                    val now = System.currentTimeMillis()
                    if (now - lastLevelAt > 120) {
                        lastLevelAt = now
                        main.post { onLevel(levelOf(chunk)) }
                    }
                    onChunk(shortsToBytes(chunk))
                }
            }.apply { start() }
        } catch (e: Exception) {
            onError("启动录音失败：${e.message}")
        }
    }

    /** PCM 泵（火山流式识别用·内部音频）：从 chunkSource 取帧转字节交出去。 */
    fun startPumpChunks(
        chunkSource: () -> ShortArray?,
        onChunk: (ByteArray) -> Unit,
        onLevel: (Int) -> Unit
    ) {
        stopListening()
        listening = true
        thread = Thread {
            var lastLevelAt = 0L
            while (listening) {
                val chunk = try { chunkSource() } catch (e: Exception) { null }
                if (chunk == null || chunk.isEmpty()) continue
                SessionRecorder.write(chunk)
                val now = System.currentTimeMillis()
                if (now - lastLevelAt > 120) {
                    lastLevelAt = now
                    main.post { onLevel(levelOf(chunk)) }
                }
                onChunk(shortsToBytes(chunk))
            }
        }.apply { start() }
    }
}
