package com.bjornwz.earbuddy

import java.security.SecureRandom
import java.util.Base64
import javax.crypto.Mac
import javax.crypto.spec.SecretKeySpec

/**
 * 腾讯云实时识别裸 WebSocket 签名与 URL（与 tencent_asr_test/tencent_asr_ja.py 的
 * build_signed_url 同一套算法；纯 Kotlin/JVM，无 Android 依赖，可单测）。
 *
 * 协议（已用同一套凭据在云端虚拟机实测通过，2026-11-23 09:46：握手 code 0，
 * 18.17 秒日语 4 句全对）：
 * wss://asr.cloud.tencent.com/asr/v2/{appid}?{排序后的参数}&signature={签名}
 * 签名 = Base64(HmacSHA1(secretKey, "asr.cloud.tencent.com/asr/v2/{appid}?{query}"))
 *
 * 注意：0.9.2 之前走官方 AAR SDK（AAIClient）。字节码实测证明 SDK 的
 * startAudioRecognize 是异步的（建任务、状态置 CONNECTING 后立即返回），而
 * TencentAsr 把它当阻塞调用、返回后立即 release()，等于在握手完成前掐断会话——
 * 所以两次真机会话都 5 秒内静默失败、0 行、无任何回调。0.9.3 起改走裸 WebSocket，
 * 全程可观测、可诊断。
 */
internal object TxWsSign {
    const val HOST = "asr.cloud.tencent.com"
    const val EXPIRED_SECONDS = 86400L

    /** RFC 3986 unreserved 字符不编码，其余按 UTF-8 百分编码（与 Python quote(safe='') 一致）。 */
    fun pctEncode(s: String): String {
        val sb = StringBuilder()
        for (b in s.toByteArray(Charsets.UTF_8)) {
            val c = b.toInt() and 0xFF
            val ch = c.toChar()
            if (ch in 'A'..'Z' || ch in 'a'..'z' || ch in '0'..'9' ||
                ch == '-' || ch == '_' || ch == '.' || ch == '~'
            ) {
                sb.append(ch)
            } else {
                sb.append('%')
                sb.append(String.format("%02X", c))
            }
        }
        return sb.toString()
    }

    fun hmacSha1Base64(secretKey: String, data: String): String {
        val mac = Mac.getInstance("HmacSHA1")
        mac.init(SecretKeySpec(secretKey.toByteArray(Charsets.UTF_8), "HmacSHA1"))
        return Base64.getEncoder().encodeToString(mac.doFinal(data.toByteArray(Charsets.UTF_8)))
    }

    fun randomAlphanumeric(n: Int, rnd: java.util.Random = SecureRandom()): String {
        val chars = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"
        return (1..n).map { chars[rnd.nextInt(chars.length)] }.joinToString("")
    }

    /**
     * 生成带签名的 wss URL。timestamp/nonce/voiceId 可注入固定值（单测与 Python 对照用）；
     * 正常调用留空，内部随机生成。返回值含签名——只用于直连，绝不记入日志。
     */
    fun buildSignedUrl(
        appId: String,
        secretId: String,
        secretKey: String,
        engineModelType: String,
        timestamp: Long = System.currentTimeMillis() / 1000,
        nonce: Int = 100_000_000 + SecureRandom().nextInt(900_000_000),
        voiceId: String = randomAlphanumeric(16)
    ): String {
        val hostPath = "$HOST/asr/v2/$appId"
        val params = sortedMapOf(
            "engine_model_type" to engineModelType,
            "expired" to (timestamp + EXPIRED_SECONDS).toString(),
            "filter_dirty" to "0",
            "filter_modal" to "0",
            "filter_punc" to "0",
            "needvad" to "1",
            "nonce" to nonce.toString(),
            "secretid" to secretId,
            "timestamp" to timestamp.toString(),
            "voice_format" to "1",
            "voice_id" to voiceId,
            "word_info" to "1"
        )
        val query = params.entries.joinToString("&") { (k, v) -> "${pctEncode(k)}=${pctEncode(v)}" }
        val signStr = "$hostPath?$query"
        val signature = pctEncode(hmacSha1Base64(secretKey, signStr))
        return "wss://$signStr&signature=$signature"
    }
}
