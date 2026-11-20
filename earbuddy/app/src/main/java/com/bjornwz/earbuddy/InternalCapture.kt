package com.bjornwz.earbuddy

import android.media.AudioFormat
import android.media.AudioPlaybackCaptureConfiguration
import android.media.AudioRecord
import android.media.projection.MediaProjection
import android.os.Build
import java.util.concurrent.LinkedBlockingQueue
import java.util.concurrent.TimeUnit

/**
 * 手机内部音频采集（M4）：MediaProjection + AudioPlaybackCapture，
 * 抓其他 App 播放的声音（视频、会议、播客），重采样到 16k 单声道喂给识别管线。
 * 注意：目标 App 可通过 AudioPolicy 禁止被录（如部分 DRM/通话），那种情况只能拿到静音。
 * 防回声啃食：本机 TTS 朗读译文期间丢弃采集帧，避免把译文当原文再翻译。
 */
object InternalCapture {
    private const val SRC_RATE = 48000
    private const val DST_RATE = 16000
    private const val FACTOR = SRC_RATE / DST_RATE   // 3
    private const val CHUNK = 2048                    // 16k 下 128ms

    private val queue = LinkedBlockingQueue<ShortArray>()
    @Volatile private var running = false
    private var projection: MediaProjection? = null
    private var record: AudioRecord? = null
    private var thread: Thread? = null

    fun isRunning() = running

    fun start(mp: MediaProjection): Boolean {
        stopCaptureOnly()
        if (projection != null && projection !== mp) {
            try { projection?.stop() } catch (e: Exception) { }
        }
        projection = mp
        val config = AudioPlaybackCaptureConfiguration.Builder(mp)
            .addMatchingUsage(android.media.AudioAttributes.USAGE_MEDIA)
            .addMatchingUsage(android.media.AudioAttributes.USAGE_GAME)
            .addMatchingUsage(android.media.AudioAttributes.USAGE_UNKNOWN)
            .build()
        val format = AudioFormat.Builder()
            .setSampleRate(SRC_RATE)
            .setChannelMask(AudioFormat.CHANNEL_IN_STEREO)
            .setEncoding(AudioFormat.ENCODING_PCM_16BIT)
            .build()
        val rec = try {
            AudioRecord.Builder()
                .setAudioFormat(format)
                .setBufferSizeInBytes(SRC_RATE * 2 * 2 * 2)
                .setAudioPlaybackCaptureConfig(config)
                .build()
        } catch (e: Exception) { return false }
        if (rec.state != AudioRecord.STATE_INITIALIZED) { rec.release(); return false }
        record = rec
        running = true
        rec.startRecording()
        thread = Thread {
            val buf = ShortArray(8192)            // 立体声交错
            val accum = ArrayList<Short>(CHUNK * 2)
            var decimSum = 0
            var decimCnt = 0
            while (running) {
                val n = try { rec.read(buf, 0, buf.size) } catch (e: Exception) { -1 }
                if (n <= 0) continue
                if (Speaker.isSpeakingNow()) {   // 丢弃本机朗读期间的帧，防译文被当原文
                    accum.clear(); decimSum = 0; decimCnt = 0
                    continue
                }
                // 立体声→单声道 + 3 倍抽取（均值抗混叠），抽取计数跨帧连续
                var i = 0
                while (i + 1 < n) {
                    val mono = (buf[i].toInt() + buf[i + 1].toInt()) / 2
                    i += 2
                    decimSum += mono; decimCnt++
                    if (decimCnt == FACTOR) {
                        accum.add((decimSum / FACTOR).toShort())
                        decimSum = 0; decimCnt = 0
                        if (accum.size >= CHUNK) {
                            queue.offer(ShortArray(CHUNK) { accum[it] })
                            val rest = accum.drop(CHUNK)
                            accum.clear(); accum.addAll(rest)
                        }
                    }
                }
            }
        }.apply { start() }
        return true
    }

    /** 识别管线取帧：最多等 300ms，无数据返回 null（调用方继续轮询）。 */
    fun nextChunk(): ShortArray? =
        try { queue.poll(300, TimeUnit.MILLISECONDS) } catch (e: Exception) { null }

    fun stop() {
        stopCaptureOnly()
        try { projection?.stop() } catch (e: Exception) { }
        projection = null
    }

    /**
     * 只停采集（录音线程/队列），保留 MediaProjection 本体：
     * 翻译会话停止后投影不释放，下次开始可免授权直接 resumeCapture，
     * 悬浮按钮才能全程后台启停（系统授权弹窗只在首次出现）。
     */
    fun stopCaptureOnly() {
        running = false
        try { record?.stop() } catch (e: Exception) { }
        try { thread?.join(400) } catch (e: Exception) { }
        thread = null
        try { record?.release() } catch (e: Exception) { }
        record = null
        queue.clear()
    }

    /** 投影尚在时直接恢复采集（免授权）。投影已失效返回 false。 */
    fun resumeCapture(): Boolean {
        val mp = projection ?: return false
        if (running) return true
        return start(mp)
    }

    /** 投影本体是否还持有（不等于正在采集）。 */
    fun hasProjection() = projection != null
}
