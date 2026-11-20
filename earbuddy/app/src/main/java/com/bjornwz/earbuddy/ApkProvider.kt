package com.bjornwz.earbuddy

import android.content.ContentProvider
import android.content.ContentValues
import android.database.Cursor
import android.net.Uri
import android.os.ParcelFileDescriptor
import java.io.File
import java.io.FileNotFoundException

/**
 * 极简文件提供者：只把 cacheDir/updates 下的 APK 以 content:// 递给系统安装器，
 * 免去引入 androidx FileProvider（本项目手工构建、零三方依赖）。
 */
class ApkProvider : ContentProvider() {

    override fun onCreate(): Boolean = true

    private fun fileFor(uri: Uri): File {
        val name = uri.lastPathSegment ?: throw FileNotFoundException("no name")
        if (name.contains('/') || name.contains("..")) throw FileNotFoundException("bad name")
        val dir = File(context!!.cacheDir, "updates")
        val f = File(dir, name)
        if (!f.exists()) throw FileNotFoundException(name)
        return f
    }

    override fun openFile(uri: Uri, mode: String): ParcelFileDescriptor =
        ParcelFileDescriptor.open(fileFor(uri), ParcelFileDescriptor.MODE_READ_ONLY)

    override fun getType(uri: Uri): String = "application/vnd.android.package-archive"

    override fun query(uri: Uri, p: Array<String>?, s: String?, a: Array<String>?, o: String?): Cursor? = null
    override fun insert(uri: Uri, values: ContentValues?): Uri? = null
    override fun delete(uri: Uri, s: String?, a: Array<String>?): Int = 0
    override fun update(uri: Uri, v: ContentValues?, s: String?, a: Array<String>?): Int = 0

    companion object {
        const val AUTHORITY = "com.bjornwz.earbuddy.apkprovider"
        fun uriFor(file: File): Uri = Uri.parse("content://$AUTHORITY/${file.name}")
    }
}
