// 그림 이름표 확인용 글자 인식 (ADR-009). macOS 내장 Vision으로 한 줄에 하나씩 출력한다
// 사용: swift ocr.swift <그림 경로>
import Foundation
import Vision

let url = URL(fileURLWithPath: CommandLine.arguments[1])
let request = VNRecognizeTextRequest()
request.recognitionLanguages = ["ko-KR", "en-US"]
request.recognitionLevel = .accurate
try VNImageRequestHandler(url: url).perform([request])
for observation in request.results ?? [] {
    if let text = observation.topCandidates(1).first {
        print(text.string)
    }
}
