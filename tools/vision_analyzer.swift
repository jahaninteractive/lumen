import AppKit
import Foundation
import Vision

func unit(_ value: CGFloat) -> Double {
    return Double(max(0, min(1, value)))
}

let paths = Array(CommandLine.arguments.dropFirst())
guard !paths.isEmpty else {
    FileHandle.standardError.write(Data("usage: vision_analyzer IMAGE [IMAGE...]\n".utf8))
    exit(2)
}

enum AnalysisError: Error {
    case decode
    case vision(Error)
}

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

func featurePrint(_ observation: VNFeaturePrintObservation?) -> [Float]? {
    guard let observation, observation.elementType == .float, observation.elementCount > 0 else { return nil }
    return observation.data.withUnsafeBytes { Array($0.bindMemory(to: Float.self).prefix(observation.elementCount)) }
}

func analyze(_ path: String) throws -> [String: Any] {
    guard let image = NSImage(contentsOf: URL(fileURLWithPath: path)),
          let cgImage = image.cgImage(forProposedRect: nil, context: nil, hints: nil) else {
        throw AnalysisError.decode
    }
    let landmarks = VNDetectFaceLandmarksRequest()
    let quality = VNDetectFaceCaptureQualityRequest()
    let saliency = VNGenerateAttentionBasedSaliencyImageRequest()
    let printRequest = VNGenerateImageFeaturePrintRequest()
    let handler = VNImageRequestHandler(cgImage: cgImage, options: [:])
    do {
        try handler.perform([landmarks, quality, saliency, printRequest])
    } catch {
        throw AnalysisError.vision(error)
    }
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
    var result: [String: Any] = [
        "schema_version": 3,
        "coordinate_system": "normalized_bottom_left",
        "image_width": cgImage.width,
        "image_height": cgImage.height,
        "face_count": faces.count,
        "faces": faceRectangles,
        "saliency_objects": saliencyObjects,
        "face_quality": qualities.isEmpty ? NSNull() : qualities.reduce(0, +) / Double(qualities.count),
        "eye_quality": eyeScores.isEmpty ? NSNull() : eyeScores.reduce(0, +) / Double(eyeScores.count)
    ]
    if let vector = featurePrint(printRequest.results?.first) {
        result["feature_print"] = vector
    }
    return result
}

func line(_ object: [String: Any]) -> String {
    guard let data = try? JSONSerialization.data(withJSONObject: object, options: [.sortedKeys]) else {
        return "{\"error\":\"unable to encode result\"}"
    }
    return String(decoding: data, as: UTF8.self)
}

if paths.count == 1 {
    do {
        print(line(try analyze(paths[0])))
    } catch AnalysisError.decode {
        FileHandle.standardError.write(Data("unable to decode image\n".utf8))
        exit(3)
    } catch {
        FileHandle.standardError.write(Data("Vision failed: \(error)\n".utf8))
        exit(4)
    }
} else {
    for path in paths {
        autoreleasepool {
            do {
                print(line(try analyze(path)))
            } catch AnalysisError.decode {
                print(line(["error": "unable to decode image"]))
            } catch {
                print(line(["error": "Vision failed: \(error)"]))
            }
        }
    }
}
