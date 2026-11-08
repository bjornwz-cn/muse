import Foundation

/// 翻译引擎（与 Android 版同接口）：默认谷歌免费 gtx 接口；DeepL 需填 Key 后切换。
enum Translator {
    static var provider: String {
        get { UserDefaults.standard.string(forKey: "provider") ?? "google" }
        set { UserDefaults.standard.set(newValue, forKey: "provider") }
    }
    static var deeplKey: String {
        get { UserDefaults.standard.string(forKey: "deepl_key") ?? "" }
        set { UserDefaults.standard.set(newValue, forKey: "deepl_key") }
    }

    static func translate(_ text: String, from: String, to: String) async throws -> String {
        if provider == "deepl", !deeplKey.isEmpty {
            return try await deepl(text, from: from, to: to)
        }
        return try await google(text, from: from, to: to)
    }

    private static func google(_ text: String, from: String, to: String) async throws -> String {
        var comps = URLComponents(string: "https://translate.googleapis.com/translate_a/single")!
        comps.queryItems = [
            URLQueryItem(name: "client", value: "gtx"),
            URLQueryItem(name: "sl", value: from),
            URLQueryItem(name: "tl", value: to),
            URLQueryItem(name: "dt", value: "t"),
            URLQueryItem(name: "q", value: text),
        ]
        let (data, _) = try await URLSession.shared.data(from: comps.url!)
        guard let root = try JSONSerialization.jsonObject(with: data) as? [Any],
              let segs = root.first as? [Any] else { throw err("翻译返回异常") }
        return segs.compactMap { ($0 as? [Any])?.first as? String }.joined()
    }

    private static func deepl(_ text: String, from: String, to: String) async throws -> String {
        var req = URLRequest(url: URL(string: "https://api-free.deepl.com/v2/translate")!)
        req.httpMethod = "POST"
        req.setValue("DeepL-Auth-Key \(deeplKey)", forHTTPHeaderField: "Authorization")
        req.setValue("application/x-www-form-urlencoded", forHTTPHeaderField: "Content-Type")
        let body = "text=\(enc(text))&source_lang=\(from.uppercased())&target_lang=\(to.uppercased())"
        req.httpBody = body.data(using: .utf8)
        let (data, _) = try await URLSession.shared.data(for: req)
        guard let root = try JSONSerialization.jsonObject(with: data) as? [String: Any],
              let arr = root["translations"] as? [[String: Any]],
              let first = arr.first?["text"] as? String else { throw err("DeepL 返回异常") }
        return first
    }

    private static func enc(_ s: String) -> String {
        s.addingPercentEncoding(withAllowedCharacters: .urlQueryAllowed) ?? s
    }
    private static func err(_ m: String) -> NSError {
        NSError(domain: "Translator", code: 1, userInfo: [NSLocalizedDescriptionKey: m])
    }
}
