import Foundation
import AVFoundation

/// 译文朗读（与 Android Speaker 对应）：AVSpeechSynthesizer 队列播报。
final class Speaker {
    static let shared = Speaker()
    private let synth = AVSpeechSynthesizer()

    func say(_ text: String, lang: String) {
        let u = AVSpeechUtterance(string: text)
        u.voice = AVSpeechSynthesisVoice(language: lang == "en" ? "en-US" : "zh-CN")
        u.rate = AVSpeechUtteranceDefaultSpeechRate
        synth.speak(u)
    }

    func stop() { synth.stopSpeaking(at: .immediate) }
    var speaking: Bool { synth.isSpeaking }
}
