import SwiftUI

/// 同传主界面（与 Android TranslateActivity 对应）：双语字幕、方向切换、
/// 收音来源（手机麦/耳机麦）、译文朗读开关、会话结束自动存记录。
struct TranslateView: View {
    @StateObject private var vm = TranslateVM()
    @State private var showRecords = false

    var body: some View {
        NavigationStack {
            VStack(spacing: 12) {
                HStack {
                    Button(vm.directionLabel) { vm.toggleDirection() }
                        .disabled(vm.running)
                    Spacer()
                    Button("记录") { showRecords = true }
                }
                .font(.subheadline)

                HStack(spacing: 16) {
                    Toggle("耳机麦", isOn: $vm.useBtMic).disabled(vm.running)
                    Toggle("朗读译文", isOn: $vm.speakOn)
                }
                .font(.subheadline)

                // 收音电平条
                ProgressView(value: vm.level)
                    .progressViewStyle(.linear)
                    .animation(.linear(duration: 0.1), value: vm.level)

                Text(vm.status).font(.footnote).foregroundStyle(.secondary)
                    .frame(maxWidth: .infinity, alignment: .leading)

                ScrollViewReader { proxy in
                    ScrollView {
                        LazyVStack(alignment: .leading, spacing: 10) {
                            ForEach(vm.lines) { line in
                                VStack(alignment: .leading, spacing: 2) {
                                    Text(line.src)
                                    Text(line.dst).foregroundStyle(.blue)
                                }
                                .id(line.id)
                            }
                            if !vm.partial.isEmpty {
                                Text(vm.partial).foregroundStyle(.secondary)
                            }
                        }
                        .frame(maxWidth: .infinity, alignment: .leading)
                    }
                    .onChange(of: vm.lines.count) { _ in
                        if let last = vm.lines.last { proxy.scrollTo(last.id, anchor: .bottom) }
                    }
                }

                Button(vm.running ? "停止翻译" : "开始翻译") {
                    vm.toggle()
                }
                .buttonStyle(.borderedProminent)
                .controlSize(.large)
            }
            .padding()
            .navigationTitle("耳机助手 · 同传")
            .navigationBarTitleDisplayMode(.inline)
            .sheet(isPresented: $showRecords) { RecordsView() }
        }
    }
}

@MainActor
final class TranslateVM: ObservableObject {
    @Published var running = false
    @Published var from = "zh"
    @Published var to = "en"
    @Published var useBtMic = false
    @Published var speakOn = true
    @Published var level: Float = 0
    @Published var status = "点「开始翻译」，对着麦克风说话即可（识别在本机离线进行）"
    @Published var partial = ""
    @Published var lines: [SubtitleLine] = []

    private let engine = SpeechEngine()
    private var sessionStart: Date?

    var directionLabel: String {
        "\(from == "zh" ? "中文" : "English") → \(to == "zh" ? "中文" : "English")（点击切换）"
    }

    func toggleDirection() { swap(&from, &to) }

    func toggle() { running ? stop() : start() }

    func start() {
        SpeechEngine.requestPermissions { [weak self] ok in
            guard let self else { return }
            guard ok else { self.status = "❌ 请在系统设置里允许麦克风与语音识别权限"; return }
            self.lines = []
            self.partial = ""
            self.sessionStart = Date()
            self.engine.onPartial = { [weak self] t in self?.partial = t }
            self.engine.onLevel = { [weak self] v in self?.level = v }
            self.engine.onError = { [weak self] m in self?.status = "❌ \(m)" }
            self.engine.onFinal = { [weak self] text in
                guard let self else { return }
                self.partial = ""
                Task { await self.translateAndAppend(text) }
            }
            self.engine.start(localeCode: self.from, useBtMic: self.useBtMic)
            self.running = true
            self.status = "识别中…（停顿自动断句翻译）"
        }
    }

    private func translateAndAppend(_ text: String) async {
        do {
            let dst = try await Translator.translate(text, from: from, to: to)
            lines.append(SubtitleLine(src: text, dst: dst, time: Date()))
            if speakOn { Speaker.shared.say(dst, lang: to) }
        } catch {
            status = "翻译失败：\(error.localizedDescription)"
        }
    }

    func stop() {
        engine.stop()
        running = false
        level = 0
        status = "已停止"
        if let start = sessionStart, !lines.isEmpty {
            RecordStore.append(record: SessionRecord(startedAt: start, lines: lines))
        }
        sessionStart = nil
    }
}

struct RecordsView: View {
    @State private var records = RecordStore.load()
    var body: some View {
        NavigationStack {
            List(records) { rec in
                Section(header: Text(rec.startedAt, style: .date)) {
                    ForEach(rec.lines) { line in
                        VStack(alignment: .leading, spacing: 2) {
                            Text(line.src)
                            Text(line.dst).foregroundStyle(.blue)
                        }
                    }
                }
            }
            .navigationTitle("翻译记录")
            .navigationBarTitleDisplayMode(.inline)
        }
    }
}
