package com.bjornwz.earbuddy

import android.media.MediaCodec
import android.media.MediaCodecInfo
import android.media.MediaFormat
import android.media.MediaMuxer
import java.io.File
import java.io.RandomAccessFile
import kotlin.concurrent.thread

/**
 * WAV(16k 单声道 PCM) → M4A(AAC 48kbps) 转码：会议录音从约 115MB/小时压到约 21MB/小时，
 * 分享给 YoooClaw/微信更现实。全程防御式：任何异常都回调失败，调用方保留原 WAV。
 */
object AacTranscoder {

    private const val SAMPLE_RATE = 16000
    private const val BIT_RATE = 48000
    private const val CHUNK_SAMPLES = 4096

    fun transcodeAsync(wav: File, m4a: File, done: (Boolean) -> Unit) {
        thread {
            var ok = false
            try {
                ok = transcode(wav, m4a)
            } catch (e: Exception) {
                ok = false
            }
            if (!ok) try { m4a.delete() } catch (e: Exception) { }
            done(ok)
        }
    }

    private fun transcode(wav: File, m4a: File): Boolean {
        val format = MediaFormat.createAudioFormat(MediaFormat.MIMETYPE_AUDIO_AAC, SAMPLE_RATE, 1).apply {
            setInteger(MediaFormat.KEY_AAC_PROFILE, MediaCodecInfo.CodecProfileLevel.AACObjectLC)
            setInteger(MediaFormat.KEY_BIT_RATE, BIT_RATE)
            setInteger(MediaFormat.KEY_MAX_INPUT_SIZE, CHUNK_SAMPLES * 2)
        }
        val codec = MediaCodec.createEncoderByType(MediaFormat.MIMETYPE_AUDIO_AAC)
        codec.configure(format, null, null, MediaCodec.CONFIGURE_FLAG_ENCODE)
        val muxer = MediaMuxer(m4a.absolutePath, MediaMuxer.OutputFormat.MUXER_OUTPUT_MPEG_4)
        var muxerStarted = false
        var trackIndex = -1
        var wroteAny = false
        val raf = RandomAccessFile(wav, "r")
        try {
            codec.start()
            raf.seek(44) // 跳过 WAV 头
            val totalSamples = (raf.length() - 44) / 2
            if (totalSamples <= 0) return false
            val bufferInfo = MediaCodec.BufferInfo()
            var fedSamples = 0L
            var inputDone = false
            var outputDone = false
            val pcmBuf = ByteArray(CHUNK_SAMPLES * 2)
            while (!outputDone) {
                // 喂输入
                if (!inputDone) {
                    val inIdx = codec.dequeueInputBuffer(10_000)
                    if (inIdx >= 0) {
                        val inBuf = codec.getInputBuffer(inIdx)!!
                        inBuf.clear()
                        val remaining = totalSamples - fedSamples
                        if (remaining <= 0) {
                            codec.queueInputBuffer(
                                inIdx, 0, 0,
                                totalSamples * 1_000_000 / SAMPLE_RATE,
                                MediaCodec.BUFFER_FLAG_END_OF_STREAM
                            )
                            inputDone = true
                        } else {
                            val toRead = minOf(CHUNK_SAMPLES.toLong(), remaining).toInt()
                            val n = raf.read(pcmBuf, 0, toRead * 2)
                            if (n <= 0) {
                                codec.queueInputBuffer(
                                    inIdx, 0, 0,
                                    fedSamples * 1_000_000 / SAMPLE_RATE,
                                    MediaCodec.BUFFER_FLAG_END_OF_STREAM
                                )
                                inputDone = true
                            } else {
                                inBuf.put(pcmBuf, 0, n)
                                val pts = fedSamples * 1_000_000 / SAMPLE_RATE
                                codec.queueInputBuffer(inIdx, 0, n, pts, 0)
                                fedSamples += n / 2
                            }
                        }
                    }
                }
                // 收输出
                val outIdx = codec.dequeueOutputBuffer(bufferInfo, 10_000)
                when {
                    outIdx == MediaCodec.INFO_OUTPUT_FORMAT_CHANGED -> {
                        if (!muxerStarted) {
                            trackIndex = muxer.addTrack(codec.outputFormat)
                            muxer.start()
                            muxerStarted = true
                        }
                    }
                    outIdx >= 0 -> {
                        val outBuf = codec.getOutputBuffer(outIdx)!!
                        if (bufferInfo.size > 0 && muxerStarted) {
                            outBuf.position(bufferInfo.offset)
                            outBuf.limit(bufferInfo.offset + bufferInfo.size)
                            muxer.writeSampleData(trackIndex, outBuf, bufferInfo)
                            wroteAny = true
                        }
                        codec.releaseOutputBuffer(outIdx, false)
                        if (bufferInfo.flags and MediaCodec.BUFFER_FLAG_END_OF_STREAM != 0) {
                            outputDone = true
                        }
                    }
                }
            }
            return wroteAny
        } finally {
            try { raf.close() } catch (e: Exception) { }
            try { codec.stop() } catch (e: Exception) { }
            try { codec.release() } catch (e: Exception) { }
            try { if (muxerStarted) muxer.stop() } catch (e: Exception) { }
            try { muxer.release() } catch (e: Exception) { }
        }
    }
}
