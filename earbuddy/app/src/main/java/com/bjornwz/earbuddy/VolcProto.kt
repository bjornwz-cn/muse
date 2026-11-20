package com.bjornwz.earbuddy

import java.io.ByteArrayInputStream
import java.io.ByteArrayOutputStream
import java.util.zip.GZIPInputStream
import java.util.zip.GZIPOutputStream

/**
 * 火山语音二进制帧协议的公共编解码（VolcAsr 流式与 VolcSegmentAsr 一句话共用）：
 * 4 字节头 [0x11, type<<4|flags, serial<<4|comp, 0] + 可选 sequence + 载荷长度 + 载荷。
 */
internal object VolcProto {

    fun frameHeader(type: Int, flags: Int, serial: Int, comp: Int): ByteArray =
        byteArrayOf(
            ((0x1 shl 4) or 0x1).toByte(),
            ((type shl 4) or flags).toByte(),
            ((serial shl 4) or comp).toByte(),
            0
        )

    fun be32(v: Int): ByteArray = byteArrayOf(
        (v shr 24).toByte(), (v shr 16).toByte(), (v shr 8).toByte(), v.toByte()
    )

    fun gzip(data: ByteArray): ByteArray {
        val out = ByteArrayOutputStream()
        GZIPOutputStream(out).use { it.write(data) }
        return out.toByteArray()
    }

    fun gunzip(data: ByteArray): ByteArray =
        GZIPInputStream(ByteArrayInputStream(data)).readBytes()
}
