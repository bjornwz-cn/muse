import Foundation

/// 翻译会话记录（与 Android 版 Records 对应）：本地 JSON 文件存储，可回看。
struct SubtitleLine: Identifiable, Codable {
    var id = UUID()
    let src: String
    let dst: String
    let time: Date
}

struct SessionRecord: Identifiable, Codable {
    var id = UUID()
    let startedAt: Date
    var lines: [SubtitleLine]
}

enum RecordStore {
    private static var fileURL: URL {
        FileManager.default.urls(for: .documentDirectory, in: .userDomainMask)[0]
            .appendingPathComponent("records.json")
    }

    static func load() -> [SessionRecord] {
        guard let data = try? Data(contentsOf: fileURL),
              let list = try? JSONDecoder().decode([SessionRecord].self, from: data)
        else { return [] }
        return list
    }

    static func save(_ list: [SessionRecord]) {
        if let data = try? JSONEncoder().encode(list) {
            try? data.write(to: fileURL, options: .atomic)
        }
    }

    static func append(record: SessionRecord) {
        var list = load()
        list.insert(record, at: 0)
        if list.count > 100 { list = Array(list.prefix(100)) }
        save(list)
    }
}
