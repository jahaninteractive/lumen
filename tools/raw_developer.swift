import CoreGraphics
import CoreImage
import CoreImage.CIFilterBuiltins
import Foundation
import ImageIO

let invalidRequest: Int32 = 2

func fail(_ message: String, _ code: Int32) -> Never {
    FileHandle.standardError.write(Data("\(message)\n".utf8))
    exit(code)
}

/// JSON booleans bridge to NSNumber, so without this a `true` would pass as a number.
func isBoolean(_ value: Any) -> Bool {
    return CFGetTypeID(value as CFTypeRef) == CFBooleanGetTypeID()
}

/// nil when the key is absent, which leaves Core Image's per-file default alone.
/// A key that is present but not a finite number is a hard error: an upstream
/// validation gap must not reach the photographer as a slider that does nothing.
func optionalNumber(_ container: [String: Any], _ key: String) -> Double? {
    guard let value = container[key] else { return nil }
    guard !isBoolean(value), let candidate = (value as? NSNumber)?.doubleValue, candidate.isFinite else {
        fail("\(key) must be a number", invalidRequest)
    }
    return candidate
}

/// A key that is present but not a boolean is a hard error, for the same reason as
/// optionalNumber: a validation gap must not reach the photographer as a dead checkbox.
func optionalFlag(_ container: [String: Any], _ key: String) -> Bool? {
    guard let value = container[key] else { return nil }
    guard isBoolean(value), let flag = value as? Bool else {
        fail("\(key) must be true or false", invalidRequest)
    }
    return flag
}

func unitCoordinate(_ entry: Any) -> Double? {
    guard !isBoolean(entry), let value = (entry as? NSNumber)?.doubleValue else { return nil }
    guard value.isFinite, value >= 0, value <= 1 else { return nil }
    return value
}

/// The five tone-curve points, as x-ascending pairs in 0…1. Absent means no curve;
/// present but malformed is an error rather than a silently skipped filter.
func curvePoints(_ container: [String: Any]) -> [CGPoint]? {
    guard let value = container["curve"] else { return nil }
    guard let pairs = value as? [[Any]], pairs.count == 5 else {
        fail("curve must be five [x, y] pairs", invalidRequest)
    }
    var points: [CGPoint] = []
    for pair in pairs {
        guard pair.count == 2, let x = unitCoordinate(pair[0]), let y = unitCoordinate(pair[1]) else {
            fail("curve points must be two numbers in 0...1", invalidRequest)
        }
        points.append(CGPoint(x: x, y: y))
    }
    guard zip(points, points.dropFirst()).allSatisfy({ $0.x < $1.x }) else {
        fail("curve points must ascend in x", invalidRequest)
    }
    return points
}

/// 256 buckets for each of Rec. 709 luma and the three display channels, from the same render.
func channelHistograms(_ image: CIImage, _ context: CIContext, _ displaySpace: CGColorSpace) -> [String: [Double]] {
    let buckets = 256  // one per 8-bit display value
    // Bin the pixels actually seen: the histogram is drawn under the tone curve, so it
    // must describe sRGB display values, not Core Image's linear working-space ones.
    let converted = image.matchedFromWorkingSpace(to: displaySpace) ?? image
    // areaHistogram silently drops samples outside 0...1, and converting to sRGB pushes
    // out-of-gamut channels past 1. Unclamped, a frame with blown reds lost a fifth of its
    // red pixels from the chart. Clamping piles them into the end buckets, which is where a
    // photographer expects clipping to show.
    let clamp = CIFilter.colorClamp()
    clamp.inputImage = converted
    clamp.minComponents = CIVector(x: 0, y: 0, z: 0, w: 0)
    clamp.maxComponents = CIVector(x: 1, y: 1, z: 1, w: 1)
    let display = clamp.outputImage ?? converted

    // One areaHistogram pass, returned as the raw RGBAf samples: buckets * 4 floats.
    func bins(_ source: CIImage) -> [Float] {
        let filter = CIFilter.areaHistogram()
        filter.inputImage = source
        filter.extent = display.extent
        filter.count = buckets
        filter.scale = 1
        guard let histogram = filter.outputImage else { return [] }
        var pixels = [Float](repeating: 0, count: buckets * 4)
        pixels.withUnsafeMutableBytes { raw in
            // colorSpace nil: these samples are bucket counts, not colours. Converting them
            // would apply a transfer function to the counts and flatten the shadow buckets.
            context.render(
                histogram,
                toBitmap: raw.baseAddress!,
                rowBytes: buckets * 4 * 4,
                bounds: CGRect(x: 0, y: 0, width: buckets, height: 1),
                format: .RGBAf,
                colorSpace: nil
            )
        }
        return pixels
    }

    // areaHistogram bins every channel independently, so one pass over the colour image
    // already holds R, G and B in sample offsets 0, 1 and 2.
    let colour = bins(display)

    // Luma needs its own pass. Flatten to Rec. 709 per pixel *before* binning: binning each
    // channel and then weighting the three counts averages three distributions, which is a
    // different curve once a subject is saturated — and a sunset is exactly that.
    let weights = CIVector(x: 0.2126, y: 0.7152, z: 0.0722, w: 0)
    let greyscale = CIFilter.colorMatrix()
    greyscale.inputImage = display
    greyscale.rVector = weights
    greyscale.gVector = weights
    greyscale.bVector = weights
    greyscale.aVector = CIVector(x: 0, y: 0, z: 0, w: 1)
    greyscale.biasVector = CIVector(x: 0, y: 0, z: 0, w: 0)
    guard let luma = greyscale.outputImage else { return [:] }
    let grey = bins(luma)

    guard colour.count == buckets * 4, grey.count == buckets * 4 else { return [:] }
    func channel(_ pixels: [Float], _ offset: Int) -> [Double] {
        (0..<buckets).map { (index: Int) -> Double in Double(pixels[index * 4 + offset]) }
    }
    // Every channel of the greyscale pass holds the same luma distribution, so offset 0 is it.
    return ["y": channel(grey, 0), "r": channel(colour, 0),
            "g": channel(colour, 1), "b": channel(colour, 2)]
}

// MARK: - Request

let requestData = FileHandle.standardInput.readDataToEndOfFile()
guard let request = try? JSONSerialization.jsonObject(with: requestData) as? [String: Any],
      let source = request["source"] as? String,
      let destination = request["destination"] as? String else {
    fail("expected one JSON request object on stdin", invalidRequest)
}

var imageFormat = "tiff16"
if let value = request["format"] {
    guard let name = value as? String, name == "tiff16" || name == "jpeg" else {
        fail("format must be tiff16 or jpeg", invalidRequest)
    }
    imageFormat = name
}

var wantsHistogram = false
if let value = request["histogram"] {
    guard isBoolean(value), let flag = value as? Bool else {
        fail("histogram must be true or false", invalidRequest)
    }
    wantsHistogram = flag
}

let longEdge = optionalNumber(request, "long_edge") ?? 0
guard longEdge >= 0 else {
    fail("long_edge must not be negative", invalidRequest)
}

var settings: [String: Any] = [:]
if let value = request["settings"] {
    guard let dictionary = value as? [String: Any] else {
        fail("settings must be an object", invalidRequest)
    }
    settings = dictionary
}
// detailAmount is deliberately absent. Core Image exposes it, but measured on this camera's
// reduced-size lossless raws it is inert: 0.0 and 1.0 differ by 0.0001 mean on the noisiest
// frame in the library. A slider whose position changes nothing is the fault this panel just
// finished fixing, so it stays out until a file is found where it bites.
let knownSettings = ["exposure_ev", "temperature_k", "tint", "contrast",
                     "sharpness", "luminance_nr", "color_nr",
                     "clarity", "lens_correction", "curve"]
for key in settings.keys where !knownSettings.contains(key) {
    fail("unknown develop setting \(key)", invalidRequest)
}
let curve = curvePoints(settings)

// MARK: - Develop

let sourceURL = URL(fileURLWithPath: source)
guard let rawFilter = CIRAWFilter(imageURL: sourceURL) else {
    fail("unable to decode \(sourceURL.lastPathComponent)", 3)
}

let neutralTemperature = rawFilter.neutralTemperature
let neutralTint = rawFilter.neutralTint
let nativeSize = rawFilter.nativeSize
// What this file decodes to with nothing asked of it. These are not mid-range: on a Sony ARW
// sharpness already sits at 1.0, so a panel that parks its handle at 0.5 tells the
// photographer they can sharpen further when the only direction available is down.
let decodedDefaults: [String: Double] = [
    "contrast": Double(rawFilter.contrastAmount),
    "sharpness": Double(rawFilter.sharpnessAmount),
    "luminance_nr": Double(rawFilter.luminanceNoiseReductionAmount),
    "color_nr": Double(rawFilter.colorNoiseReductionAmount),
]
// Whether this particular file carries a profile Core Image can correct against. Not every
// body and lens pair does, and a checkbox that silently does nothing is worse than none.
let lensCorrectionSupported = rawFilter.isLensCorrectionSupported

// Decode at native scale and resample afterwards, rather than asking the raw filter for a
// small image. Core Image skips raw sharpening entirely at scaleFactor <= 0.5 and applies
// noise reduction in proportion to the scale, so a preview decoded small showed none of the
// sharpening and about 1/500th of the noise reduction that the full-size export would bake
// in — the photographer judged those two sliders on a render that did not use them.
// Measured on a 3504x2336 raw: decoding native costs 0.41s against 0.35s for a 700px decode,
// because the decode dominates and the resample is nearly free.
let native = max(nativeSize.width, nativeSize.height)
let previewScale = longEdge > 0 && native > 0 ? min(1.0, longEdge / native) : 1.0

if let value = optionalNumber(settings, "exposure_ev") { rawFilter.exposure = Float(value) }
if let value = optionalNumber(settings, "temperature_k") { rawFilter.neutralTemperature = Float(value) }
if let value = optionalNumber(settings, "tint") { rawFilter.neutralTint = Float(value) }
if let value = optionalNumber(settings, "contrast") { rawFilter.contrastAmount = Float(value) }
if let value = optionalNumber(settings, "sharpness") { rawFilter.sharpnessAmount = Float(value) }
if let value = optionalNumber(settings, "luminance_nr") { rawFilter.luminanceNoiseReductionAmount = Float(value) }
if let value = optionalNumber(settings, "color_nr") { rawFilter.colorNoiseReductionAmount = Float(value) }
if let wanted = optionalFlag(settings, "lens_correction") {
    // Asking for it on a file that does not support it is the request's mistake, not a
    // silent no-op: the panel only offers the box when the render says it is supported.
    guard !wanted || lensCorrectionSupported else {
        fail("this file has no lens correction profile", invalidRequest)
    }
    rawFilter.isLensCorrectionEnabled = wanted
}

guard var image = rawFilter.outputImage else {
    fail("unable to develop \(sourceURL.lastPathComponent)", 4)
}

// Clarity is a wide, gentle unsharp mask, the way most editors build it. Its radius is in
// pixels, so it runs here at native scale and the resample comes last: applied after a
// preview had been shrunk, the same setting would mean a quarter of the radius and the
// photographer would be judging an effect the export does not produce.
if let value = optionalNumber(settings, "clarity"), value > 0 {
    let clarity = CIFilter.unsharpMask()
    clarity.inputImage = image
    // A share of the frame rather than a fixed count, so clarity means the same thing on a
    // 24MP body and a 61MP one.
    clarity.radius = Float(native * 0.008)
    // The slider's full travel maps to 0.5 intensity, not 1.0. Past about 0.5 an unsharp
    // mask this wide rings visibly around a blown highlight — on a sunset it drew a green
    // halo around the sun — and a control whose top third is unusable is mis-scaled.
    clarity.intensity = Float(value * 0.5)
    guard let clarified = clarity.outputImage else {
        fail("unable to apply clarity", 5)
    }
    image = clarified.cropped(to: image.extent)  // unsharpMask grows the extent by its radius
}

if let points = curve {
    let toneCurve = CIFilter.toneCurve()
    toneCurve.inputImage = image
    toneCurve.point0 = points[0]
    toneCurve.point1 = points[1]
    toneCurve.point2 = points[2]
    toneCurve.point3 = points[3]
    toneCurve.point4 = points[4]
    guard let curved = toneCurve.outputImage else {
        fail("unable to apply the tone curve", 5)
    }
    image = curved
}

if previewScale < 1.0 {
    let resample = CIFilter.lanczosScaleTransform()
    resample.inputImage = image
    resample.scale = Float(previewScale)
    resample.aspectRatio = 1
    guard let scaled = resample.outputImage else {
        fail("unable to resize \(sourceURL.lastPathComponent)", 4)
    }
    image = scaled
}

// MARK: - Write

let context = CIContext()
// The export pipeline hard-fails without an ICC profile, so always tag sRGB.
guard let colorSpace = CGColorSpace(name: CGColorSpace.sRGB) else {
    fail("sRGB colour space unavailable", 6)
}

// Render to a sibling temp file and rename only once everything has succeeded, so a
// failure never leaves a truncated image where a later step would read it as a render.
let destinationURL = URL(fileURLWithPath: destination)
let partialURL = destinationURL.deletingLastPathComponent()
    .appendingPathComponent(".\(destinationURL.lastPathComponent).partial")

func discardPartial() {
    try? FileManager.default.removeItem(at: partialURL)
}

do {
    if imageFormat == "jpeg" {
        try context.writeJPEGRepresentation(
            of: image,
            to: partialURL,
            colorSpace: colorSpace,
            options: [kCGImageDestinationLossyCompressionQuality as CIImageRepresentationOption: 0.92]
        )
    } else {
        try context.writeTIFFRepresentation(
            of: image,
            to: partialURL,
            format: .RGBA16,
            colorSpace: colorSpace
        )
    }
} catch {
    discardPartial()
    fail("unable to write the developed image", 7)
}

var response: [String: Any] = [
    "width": Int(image.extent.width.rounded()),
    "height": Int(image.extent.height.rounded()),
    "neutral_temperature": Double(neutralTemperature),
    "neutral_tint": Double(neutralTint),
    "defaults": decodedDefaults,
    "lens_correction_supported": lensCorrectionSupported,
]
if wantsHistogram {
    response["histogram"] = channelHistograms(image, context, colorSpace)
}
guard let responseData = try? JSONSerialization.data(withJSONObject: response, options: [.sortedKeys]) else {
    discardPartial()
    fail("unable to encode the response", 8)
}

// rename(2) replaces the destination atomically, so no reader ever sees a partial file.
guard rename(partialURL.path, destinationURL.path) == 0 else {
    discardPartial()
    fail("unable to place the developed image", 9)
}
print(String(decoding: responseData, as: UTF8.self))
