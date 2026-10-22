import AppKit
import Foundation
import Vision

func unit(_ value: CGFloat) -> Double {
    return Double(max(0, min(1, value)))
}

guard CommandLine.arguments.count == 2 else {
    FileHandle.standardError.write(Data("usage: vision_analyzer IMAGE\n".utf8))
    exit(2)
}

let imageURL = URL(fileURLWithPath: CommandLine.arguments[1])
guard let image = NSImage(contentsOf: imageURL),
      let cgImage = image.cgImage(forProposedRect: nil, context: nil, hints: nil) else {
    FileHandle.standardError.write(Data("unable to decode image\n".utf8))
    exit(3)
}

let landmarks = VNDetectFaceLandmarksRequest()
let quality = VNDetectFaceCaptureQualityRequest()
let saliency = VNGenerateAttentionBasedSaliencyImageRequest()
let handler = VNImageRequestHandler(cgImage: cgImage, options: [:])

func rectangle(_ bounds: CGRect, confidence: Double? = nil) -> [String: Any] {
    var result: [String: Any] = [
        "x": Double(bounds.origin.x),
        "y": Double(bounds.origin.y),
        "width": Double(bounds.width),
        "height": Double(bounds.height)
    ]
    if let confidence {
        result["confidence"] = confidence
    }
    return result
}

do {
    try handler.perform([landmarks, quality, saliency])
    let faces = landmarks.results ?? []
    let qualities = (quality.results ?? []).compactMap { $0.faceCaptureQuality.map(Double.init) }
    let faceRectangles = faces.map { rectangle($0.boundingBox) }
    let saliencyObjects = (saliency.results?.first?.salientObjects ?? []).map {
        rectangle($0.boundingBox, confidence: Double($0.confidence))
    }
    var eyeScores: [Double] = []
    for face in faces {
        for region in [face.landmarks?.leftEye, face.landmarks?.rightEye].compactMap({ $0 }) {
            let points = region.normalizedPoints
            guard let minX = points.map({ $0.x }).min(), let maxX = points.map({ $0.x }).max(),
                  let minY = points.map({ $0.y }).min(), let maxY = points.map({ $0.y }).max() else { continue }
            let aspect = (maxY - minY) / max(0.001, maxX - minX)
            eyeScores.append(unit((aspect - 0.04) / 0.26))
        }
    }
    let result: [String: Any] = [
        "schema_version": 2,
        "coordinate_system": "normalized_bottom_left",
        "image_width": cgImage.width,
        "image_height": cgImage.height,
        "face_count": faces.count,
        "faces": faceRectangles,
        "saliency_objects": saliencyObjects,
        "face_quality": qualities.isEmpty ? NSNull() : qualities.reduce(0, +) / Double(qualities.count),
        "eye_quality": eyeScores.isEmpty ? NSNull() : eyeScores.reduce(0, +) / Double(eyeScores.count)
    ]
    let data = try JSONSerialization.data(withJSONObject: result, options: [.sortedKeys])
    print(String(decoding: data, as: UTF8.self))
} catch {
    FileHandle.standardError.write(Data("Vision failed: \(error)\n".utf8))
    exit(4)
}
