package com.bjornwz.earbuddy

import android.content.ContentProvider
import android.content.ContentValues
import android.database.Cursor
import android.net.Uri
import android.os.ParcelFileDescriptor
import java.io.File
import java.io.FileNotFoundException

/**
 * 极简录音文件提供者：只把 filesDir/recordings 下的会话录音以 content:// 递给分享目标
 * （微信/YoooClaw 等），模式同 ApkProvider，免引入 androidx。
 */
class AudioProvider : ContentProvider() {

    override fun onCreate(): Boolean = true

    private fun fileFor(uri: Uri): File {
        val name = uri.lastPathSegment ?: throw FileNotFoundException("no name")
        if (name.contains('/') || name.contains("..")) throw FileNotFoundException("bad name")
        val dir = File(context!!.filesDir, "recordings")
        val f = File(dir, name)
        if (!f.exists()) throw FileNotFoundException(name)
        return f
    }

    override fun openFile(uri: Uri, mode: String): ParcelFileDescriptor =
        ParcelFileDescriptor.open(fileFor(uri), ParcelFileDescriptor.MODE_READ_ONLY)

    override fun getType(uri: Uri): String =
        if (uri.lastPathSegment?.endsWith(".m4a") == true) "audio/mp4" else "audio/wav"

    override fun query(uri: Uri, p: Array<String>?, s: String?, a: Array<String>?, o: String?): Cursor? = null
    override fun insert(uri: Uri, values: ContentValues?): Uri? = null
    override fun delete(uri: Uri, s: String?, a: Array<String>?): Int = 0
    override fun update(uri: Uri, v: ContentValues?, s: String?, a: Array<String>?): Int = 0

    companion object {
        const val AUTHORITY = "com.bjornwz.earbuddy.audioprovider"
        fun uriFor(file: File): Uri = Uri.parse("content://$AUTHORITY/${file.name}")
    }
}
