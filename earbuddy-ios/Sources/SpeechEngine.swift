import Foundation
import Speech
import AVFoundation

/// 实时语音识别：AVAudioEngine 收音 → SFSpeechRecognizer 流式识别。
/// 优先端上识别（requiresOnDeviceRecognition），逻辑与 Android 版 Vosk 流式识别对应：
/// partial 实时回显、final 按停顿断句上屏。
final class SpeechEngine: NSObject {
    var onPartial: ((String) -> Void)?
    var onFinal: ((String) -> Void)?
    var onLevel: ((Float) -> Void)?
    var onError: ((String) -> Void)?

    private let audioEngine = AVAudioEngine()
    private var recognizer: SFSpeechRecognizer?
    private var request: SFSpeechAudioBufferRecognitionRequest?
    private var task: SFSpeechRecognitionTask?
    private(set) var running = false

    static func requestPermissions(_ done: @escaping (Bool) -> Void) {
        SFSpeechRecognizer.requestAuthorization { s1 in
            AVAudioSession.sharedInstance().requestRecordPermission { s2 in
                DispatchQueue.main.async { done(s1 == .authorized && s2) }
            }
        }
    }

    /// useBtMic=true 时优先蓝牙 HFP 麦克风（耳机麦），否则手机麦。
    func start(localeCode: String, useBtMic: Bool) {
        stop()
        let locale = Locale(identifier: localeCode == "en" ? "en-US" : "zh-CN")
        guard let rec = SFSpeechRecognizer(locale: locale), rec.isAvailable else {
            onError?("识别器不可用（\(localeCode)）"); return
        }
        recognizer = rec

        let session = AVAudioSession.sharedInstance()
        do {
            var options: AVAudioSession.CategoryOptions = [.duckOthers]
            if useBtMic { options.insert(.allowBluetooth) }
            try session.setCategory(.playAndRecord, mode: .measurement, options: options)
            try session.setActive(true, options: .notifyOthersOnDeactivation)
            if useBtMic, let bt = session.availableInputs?.first(where: { $0.portType == .bluetoothHFP }) {
                try session.setPreferredInput(bt)
            }
        } catch { onError?("音频会话失败：\(error.localizedDescription)"); return }

        let req = SFSpeechAudioBufferRecognitionRequest()
        req.shouldReportPartialResults = true
        if rec.supportsOnDeviceRecognition { req.requiresOnDeviceRecognition = true }
        request = req

        let input = audioEngine.inputNode
        let format = input.outputFormat(forBus: 0)
        input.removeTap(onBus: 0)
        input.installTap(onBus: 0, bufferSize: 1024, format: format) { [weak self] buffer, _ in
            self?.request?.append(buffer)
            // 简易电平：取通道均方根
            if let data = buffer.floatChannelData?[0] {
                let n = Int(buffer.frameLength)
                var sum: Float = 0
                for i in 0..<n { sum += data[i] * data[i] }
                let rms = n > 0 ? sqrt(sum / Float(n)) : 0
                DispatchQueue.main.async { self?.onLevel?(min(1, rms * 6)) }
            }
        }
        audioEngine.prepare()
        do { try audioEngine.start() } catch {
            onError?("录音启动失败：\(error.localizedDescription)"); return
        }
        running = true
        task = rec.recognitionTask(with: req) { [weak self] result, error in
            guard let self else { return }
            if let result {
                let text = result.bestTranscription.formattedString
                if result.isFinal {
                    if !text.isEmpty { self.onFinal?(text) }
                    // 断句后自动续上下一段（与 Android 版停顿断句一致）
                    if self.running { self.restartTask(localeCode: localeCode) }
                } else {
                    self.onPartial?(text)
                }
            }
            if let error, self.running {
                self.onError?("识别中断：\(error.localizedDescription)")
                self.stop()
            }
        }
    }

    private func restartTask(localeCode: String) {
        task?.cancel(); task = nil
        guard let rec = recognizer else { return }
        let req = SFSpeechAudioBufferRecognitionRequest()
        req.shouldReportPartialResults = true
        if rec.supportsOnDeviceRecognition { req.requiresOnDeviceRecognition = true }
        request = req
        // 新 request 需要重新挂 tap 的 append 目标：旧 tap 引用旧 request，这里换成新 request 后
        // 通过 request 变量间接引用（installTap 闭包读取 self.request，已是新值需重装 tap）
        let input = audioEngine.inputNode
        input.removeTap(onBus: 0)
        let format = input.outputFormat(forBus: 0)
        input.installTap(onBus: 0, bufferSize: 1024, format: format) { [weak self] buffer, _ in
            self?.request?.append(buffer)
        }
        task = rec.recognitionTask(with: req) { [weak self] result, error in
            guard let self else { return }
            if let result {
                let text = result.bestTranscription.formattedString
                if result.isFinal {
                    if !text.isEmpty { self.onFinal?(text) }
                    if self.running { self.restartTask(localeCode: localeCode) }
                } else { self.onPartial?(text) }
            }
            if error != nil, self.running { self.stop() }
        }
    }

    func stop() {
        running = false
        if audioEngine.isRunning {
            audioEngine.stop()
            audioEngine.inputNode.removeTap(onBus: 0)
        }
        request?.endAudio()
        task?.cancel()
        request = nil; task = nil
        try? AVAudioSession.sharedInstance().setActive(false, options: .notifyOthersOnDeactivation)
    }
}
