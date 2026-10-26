import CoreGraphics
import CoreImage
import CoreImage.CIFilterBuiltins
import Foundation
import ImageIO

func fail(_ message: String, _ code: Int32) -> Never {
    FileHandle.standardError.write(Data("\(message)\n".utf8))
    exit(code)
}

func number(_ value: Any?) -> Double? {
    return (value as? NSNumber)?.doubleValue
}

/// The five tone-curve points, as ascending x/y pairs in 0…1.
func curvePoints(_ value: Any?) -> [CGPoint]? {
    guard let pairs = value as? [[Any]], pairs.count == 5 else { return nil }
    let points = pairs.compactMap { pair -> CGPoint? in
        guard pair.count == 2, let x = number(pair[0]), let y = number(pair[1]) else { return nil }
        return CGPoint(x: x, y: y)
    }
    return points.count == 5 ? points : nil
}

/// 64 luminance buckets from the same render the photographer is looking at.
func luminanceHistogram(_ image: CIImage, _ context: CIContext) -> [Double] {
    let buckets = 64
    let filter = CIFilter.areaHistogram()
    filter.inputImage = image
    filter.extent = image.extent
    filter.count = buckets
    filter.scale = 1
    guard let histogram = filter.outputImage else { return [] }
    var pixels = [Float](repeating: 0, count: buckets * 4)
    pixels.withUnsafeMutableBytes { raw in
        context.render(
            histogram,
            toBitmap: raw.baseAddress!,
            rowBytes: buckets * 4 * 4,
            bounds: CGRect(x: 0, y: 0, width: buckets, height: 1),
            format: .RGBAf,
            colorSpace: nil
        )
    }
    return (0..<buckets).map { Double(pixels[$0 * 4]) }
}

let requestData = FileHandle.standardInput.readDataToEndOfFile()
guard let request = try? JSONSerialization.jsonObject(with: requestData) as? [String: Any],
      let source = request["source"] as? String,
      let destination = request["destination"] as? String else {
    fail("expected one JSON request object on stdin", 2)
}

guard let rawFilter = CIRAWFilter(imageURL: URL(fileURLWithPath: source)) else {
    fail("unable to decode \(URL(fileURLWithPath: source).lastPathComponent)", 3)
}

let neutralTemperature = rawFilter.neutralTemperature
let neutralTint = rawFilter.neutralTint
let nativeSize = rawFilter.nativeSize

if let longEdge = number(request["long_edge"]), longEdge > 0 {
    let native = max(nativeSize.width, nativeSize.height)
    if native > 0 {
        rawFilter.scaleFactor = Float(min(1.0, longEdge / native))
    }
}

let settings = request["settings"] as? [String: Any] ?? [:]
if let value = number(settings["exposure_ev"]) { rawFilter.exposure = Float(value) }
if let value = number(settings["temperature_k"]) { rawFilter.neutralTemperature = Float(value) }
if let value = number(settings["tint"]) { rawFilter.neutralTint = Float(value) }
if let value = number(settings["contrast"]) { rawFilter.contrastAmount = Float(value) }
if let value = number(settings["sharpness"]) { rawFilter.sharpnessAmount = Float(value) }
if let value = number(settings["luminance_nr"]) { rawFilter.luminanceNoiseReductionAmount = Float(value) }
if let value = number(settings["color_nr"]) { rawFilter.colorNoiseReductionAmount = Float(value) }

guard var image = rawFilter.outputImage else {
    fail("unable to develop \(URL(fileURLWithPath: source).lastPathComponent)", 4)
}

if let points = curvePoints(settings["curve"]) {
    let curve = CIFilter.toneCurve()
    curve.inputImage = image
    curve.point0 = points[0]
    curve.point1 = points[1]
    curve.point2 = points[2]
    curve.point3 = points[3]
    curve.point4 = points[4]
    guard let curved = curve.outputImage else {
        fail("unable to apply the tone curve", 5)
    }
    image = curved
}

let context = CIContext()
// The export pipeline hard-fails without an ICC profile, so always tag sRGB.
guard let colorSpace = CGColorSpace(name: CGColorSpace.sRGB) else {
    fail("sRGB colour space unavailable", 6)
}
let destinationURL = URL(fileURLWithPath: destination)
do {
    if (request["format"] as? String) == "jpeg" {
        try context.writeJPEGRepresentation(
            of: image,
            to: destinationURL,
            colorSpace: colorSpace,
            options: [kCGImageDestinationLossyCompressionQuality as CIImageRepresentationOption: 0.92]
        )
    } else {
        try context.writeTIFFRepresentation(
            of: image,
            to: destinationURL,
            format: .RGBA16,
            colorSpace: colorSpace
        )
    }
} catch {
    fail("unable to write the developed image", 7)
}

var response: [String: Any] = [
    "width": Int(image.extent.width.rounded()),
    "height": Int(image.extent.height.rounded()),
    "neutral_temperature": Double(neutralTemperature),
    "neutral_tint": Double(neutralTint),
]
if request["histogram"] as? Bool == true {
    response["histogram"] = luminanceHistogram(image, context)
}
guard let responseData = try? JSONSerialization.data(withJSONObject: response, options: [.sortedKeys]) else {
    fail("unable to encode the response", 8)
}
print(String(decoding: responseData, as: UTF8.self))
