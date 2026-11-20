package com.bjornwz.earbuddy

import android.content.Context
import java.io.File
import java.io.RandomAccessFile

/**
 * 会话录音存档：翻译进行时把采集到的 16k 单声道 PCM 同步写成 WAV（filesDir/recordings/rec-<会话id>.wav），
 * 停后由 AacTranscoder 转成 m4a 方便分享（给 YoooClaw/微信等）。写入点在 VoskManager 的
 * appendAudio 与两个 PCM 泵里统一挂接，未激活时 write() 直接返回、无开销。
 * 内部音频源时，采集层已丢弃本机 TTS 朗读期间的帧，录音里不会混入译文朗读。
 */
object SessionRecorder {

    private var raf: RandomAccessFile? = null
    private var dataBytes = 0L
    private var current: File? = null

    @Volatile var active: Boolean = false
        private set

    fun dir(ctx: Context): File = File(ctx.filesDir, "recordings").apply { mkdirs() }

    fun wavFor(ctx: Context, id: Long) = File(dir(ctx), "rec-$id.wav")
    fun m4aFor(ctx: Context, id: Long) = File(dir(ctx), "rec-$id.m4a")

    /** 记录里引用了录音时，按 id 找实际文件（转码完成优先 m4a）。 */
    fun audioFileFor(ctx: Context, id: Long): File? {
        if (id == 0L) return null
        val m4a = m4aFor(ctx, id)
        if (m4a.exists() && m4a.length() > 0) return m4a
        val wav = wavFor(ctx, id)
        if (wav.exists() && wav.length() > 44) return wav
        return null
    }

    @Synchronized fun start(ctx: Context, id: Long): File? {
        stop()
        return try {
            val f = wavFor(ctx, id)
            val r = RandomAccessFile(f, "rw")
            r.setLength(0)
            r.write(ByteArray(44)) // 占位头，stop 时回填尺寸
            raf = r
            current = f
            dataBytes = 0
            active = true
            f
        } catch (e: Exception) {
            active = false
            null
        }
    }

    @Synchronized fun write(chunk: ShortArray) {
        if (!active) return
        val r = raf ?: return
        try {
            val bytes = ByteArray(chunk.size * 2)
            for (i in chunk.indices) {
                val v = chunk[i].toInt()
                bytes[i * 2] = (v and 0xFF).toByte()
                bytes[i * 2 + 1] = ((v shr 8) and 0xFF).toByte()
            }
            r.write(bytes)
            dataBytes += bytes.size
        } catch (e: Exception) {
            // 写盘异常不打断翻译主流程
        }
    }

    /** 收尾：回填 WAV 头并关闭。无有效数据返回 null。 */
    @Synchronized fun stop(): File? {
        val r = raf ?: return null
        val f = current
        active = false
        raf = null
        current = null
        return try {
            r.seek(0)
            r.write(wavHeader(dataBytes))
            r.close()
            if (f != null && dataBytes > 0) f else null
        } catch (e: Exception) {
            null
        }
    }

    private fun wavHeader(dataSize: Long): ByteArray {
        val h = ByteArray(44)
        fun putStr(off: Int, s: String) { for (i in s.indices) h[off + i] = s[i].code.toByte() }
        fun put32(off: Int, v: Long) {
            h[off] = (v and 0xFF).toByte(); h[off + 1] = ((v shr 8) and 0xFF).toByte()
            h[off + 2] = ((v shr 16) and 0xFF).toByte(); h[off + 3] = ((v shr 24) and 0xFF).toByte()
        }
        fun put16(off: Int, v: Int) {
            h[off] = (v and 0xFF).toByte(); h[off + 1] = ((v shr 8) and 0xFF).toByte()
        }
        putStr(0, "RIFF"); put32(4, 36 + dataSize); putStr(8, "WAVE")
        putStr(12, "fmt "); put32(16, 16); put16(20, 1); put16(22, 1)
        put32(24, 16000); put32(28, 32000); put16(32, 2); put16(34, 16)
        putStr(36, "data"); put32(40, dataSize)
        return h
    }
}
