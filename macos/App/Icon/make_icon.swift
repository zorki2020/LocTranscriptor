// Draws the LocTranscriptor app icon with CoreGraphics: original artwork, no SF Symbols,
// no third-party glyphs. Big Sur style: a continuous-corner square on the 1024 grid
// (824 pt body, 100 pt margins), a soft gradient and a drop shadow.
//
// Build and run (from the repository root):
//   swiftc -O macos/App/Icon/make_icon.swift -o "$TMPDIR/make_icon"
//   "$TMPDIR/make_icon" preview a macos/App/Icon/previews/variant-a.png     # 512 px preview
//   "$TMPDIR/make_icon" iconset a "$TMPDIR/AppIcon.iconset"                 # all sizes
//   iconutil -c icns "$TMPDIR/AppIcon.iconset" -o "$TMPDIR/AppIcon.icns"
// scripts/build_app.sh does the last two steps on every build; the .icns is not in git.
//
// Variants: a (waveform turning into text lines), b (speech bubble with a waveform),
// c (transcript sheet with speaker dots).

import CoreGraphics
import Foundation
import ImageIO
import UniformTypeIdentifiers

let canvas: CGFloat = 1024
let body = CGRect(x: 100, y: 100, width: 824, height: 824)

func rgb(_ hex: UInt32, _ alpha: CGFloat = 1) -> CGColor {
    CGColor(
        srgbRed: CGFloat((hex >> 16) & 0xff) / 255, green: CGFloat((hex >> 8) & 0xff) / 255,
        blue: CGFloat(hex & 0xff) / 255, alpha: alpha)
}

/// A superellipse (n = 5) close to Apple's continuous-corner icon shape.
func squircle(_ rect: CGRect, exponent n: CGFloat = 5) -> CGPath {
    let path = CGMutablePath()
    let a = rect.width / 2
    let b = rect.height / 2
    let steps = 720
    for i in 0..<steps {
        let t = CGFloat(i) / CGFloat(steps) * 2 * .pi
        let c = cos(t)
        let s = sin(t)
        let x = rect.midX + a * (c < 0 ? -1 : 1) * pow(abs(c), 2 / n)
        let y = rect.midY + b * (s < 0 ? -1 : 1) * pow(abs(s), 2 / n)
        if i == 0 { path.move(to: CGPoint(x: x, y: y)) } else { path.addLine(to: CGPoint(x: x, y: y)) }
    }
    path.closeSubpath()
    return path
}

func roundedBar(_ ctx: CGContext, x: CGFloat, centerY: CGFloat, width: CGFloat, height: CGFloat) {
    let rect = CGRect(x: x, y: centerY - height / 2, width: width, height: height)
    ctx.addPath(CGPath(roundedRect: rect, cornerWidth: width / 2, cornerHeight: width / 2, transform: nil))
    ctx.fillPath()
}

func line(_ ctx: CGContext, x: CGFloat, y: CGFloat, length: CGFloat, thickness: CGFloat) {
    let rect = CGRect(x: x, y: y - thickness / 2, width: length, height: thickness)
    ctx.addPath(CGPath(roundedRect: rect, cornerWidth: thickness / 2, cornerHeight: thickness / 2, transform: nil))
    ctx.fillPath()
}

func linearGradient(_ ctx: CGContext, _ colors: [CGColor], from: CGPoint, to: CGPoint) {
    let gradient = CGGradient(colorsSpace: CGColorSpace(name: CGColorSpace.sRGB), colors: colors as CFArray, locations: nil)!
    ctx.drawLinearGradient(gradient, start: from, end: to, options: [.drawsBeforeStartLocation, .drawsAfterEndLocation])
}

/// Background: shadow, gradient body, a soft top highlight and a hairline edge.
func drawBody(_ ctx: CGContext, top: UInt32, bottom: UInt32, small: Bool) {
    let shape = squircle(body)
    ctx.saveGState()
    ctx.setShadow(offset: CGSize(width: 0, height: small ? -6 : -12), blur: small ? 10 : 28, color: rgb(0x000000, 0.32))
    ctx.addPath(shape)
    ctx.setFillColor(rgb(bottom))
    ctx.fillPath()
    ctx.restoreGState()

    ctx.saveGState()
    ctx.addPath(shape)
    ctx.clip()
    linearGradient(ctx, [rgb(top), rgb(bottom)], from: CGPoint(x: 512, y: body.maxY), to: CGPoint(x: 512, y: body.minY))
    // Light from above: a faint radial glow in the upper part.
    let glow = CGGradient(
        colorsSpace: CGColorSpace(name: CGColorSpace.sRGB),
        colors: [rgb(0xffffff, 0.22), rgb(0xffffff, 0)] as CFArray, locations: [0, 1])!
    ctx.drawRadialGradient(
        glow, startCenter: CGPoint(x: 512, y: 980), startRadius: 0, endCenter: CGPoint(x: 512, y: 980),
        endRadius: 620, options: [])
    ctx.restoreGState()

    ctx.saveGState()
    ctx.addPath(shape)
    ctx.setStrokeColor(rgb(0x000000, 0.10))
    ctx.setLineWidth(2)
    ctx.strokePath()
    ctx.restoreGState()
}

func withShadow(_ ctx: CGContext, small: Bool, alpha: CGFloat = 0.22, _ draw: () -> Void) {
    ctx.saveGState()
    ctx.setShadow(offset: CGSize(width: 0, height: small ? -3 : -8), blur: small ? 6 : 18, color: rgb(0x000000, alpha))
    ctx.beginTransparencyLayer(auxiliaryInfo: nil)
    draw()
    ctx.endTransparencyLayer()
    ctx.restoreGState()
}

// MARK: - Variant A: a waveform that turns into lines of text

func variantA(_ ctx: CGContext, small: Bool) {
    drawBody(ctx, top: 0x5A9BFF, bottom: 0x3441D6, small: small)
    withShadow(ctx, small: small) {
        ctx.setFillColor(rgb(0xffffff))
        let mid: CGFloat = 512
        if small {
            for (i, h) in [CGFloat(200), 360, 240].enumerated() {
                roundedBar(ctx, x: 236 + CGFloat(i) * 96, centerY: mid, width: 64, height: h)
            }
            line(ctx, x: 548, y: mid + 80, length: 240, thickness: 64)
            line(ctx, x: 548, y: mid - 80, length: 170, thickness: 64)
            return
        }
        let heights: [CGFloat] = [150, 290, 420, 330, 210]
        for (i, h) in heights.enumerated() {
            roundedBar(ctx, x: 222 + CGFloat(i) * 64, centerY: mid, width: 40, height: h)
        }
        // Text lines, slightly translucent toward the end, as if still being written.
        ctx.setFillColor(rgb(0xffffff, 0.96))
        line(ctx, x: 560, y: mid + 110, length: 250, thickness: 40)
        ctx.setFillColor(rgb(0xffffff, 0.86))
        line(ctx, x: 560, y: mid, length: 200, thickness: 40)
        ctx.setFillColor(rgb(0xffffff, 0.72))
        line(ctx, x: 560, y: mid - 110, length: 140, thickness: 40)
    }
}

// MARK: - Variant B: a speech bubble with a waveform

func bubblePath(_ rect: CGRect, radius: CGFloat) -> CGPath {
    let path = CGMutablePath()
    path.addRoundedRect(in: rect, cornerWidth: radius, cornerHeight: radius)
    // Tail at the lower left.
    let tail = CGMutablePath()
    tail.move(to: CGPoint(x: rect.minX + radius * 0.9, y: rect.minY + 10))
    tail.addQuadCurve(
        to: CGPoint(x: rect.minX + radius * 0.35, y: rect.minY - 105),
        control: CGPoint(x: rect.minX + radius * 0.95, y: rect.minY - 60))
    tail.addQuadCurve(
        to: CGPoint(x: rect.minX + radius * 2.0, y: rect.minY + 10),
        control: CGPoint(x: rect.minX + radius * 1.35, y: rect.minY - 50))
    tail.closeSubpath()
    path.addPath(tail)
    return path
}

func variantB(_ ctx: CGContext, small: Bool) {
    drawBody(ctx, top: 0x474B57, bottom: 0x1B1D23, small: small)
    let bubble = small ? CGRect(x: 196, y: 300, width: 632, height: 480) : CGRect(x: 214, y: 318, width: 596, height: 456)
    withShadow(ctx, small: small, alpha: 0.35) {
        ctx.addPath(bubblePath(bubble, radius: small ? 130 : 118))
        ctx.setFillColor(rgb(0xffffff))
        ctx.fillPath()
    }
    // Waveform in the upper part of the bubble, blue like the app's accent.
    ctx.saveGState()
    let bars: [CGFloat] = small ? [120, 230, 300, 190, 110] : [70, 140, 210, 150, 240, 170, 100, 60]
    let barWidth: CGFloat = small ? 62 : 36
    let gap: CGFloat = small ? 34 : 26
    let total = CGFloat(bars.count) * barWidth + CGFloat(bars.count - 1) * gap
    let startX = bubble.midX - total / 2
    let waveY = small ? bubble.midY : bubble.midY + 70
    let waveRect = CGRect(x: startX, y: waveY - 130, width: total, height: 260)
    let clip = CGMutablePath()
    for (i, h) in bars.enumerated() {
        let x = startX + CGFloat(i) * (barWidth + gap)
        clip.addRoundedRect(
            in: CGRect(x: x, y: waveY - h / 2, width: barWidth, height: h), cornerWidth: barWidth / 2,
            cornerHeight: barWidth / 2)
    }
    ctx.addPath(clip)
    ctx.clip()
    linearGradient(
        ctx, [rgb(0x4C8DFF), rgb(0x3A3FD9)], from: CGPoint(x: waveRect.minX, y: waveRect.maxY),
        to: CGPoint(x: waveRect.maxX, y: waveRect.minY))
    ctx.restoreGState()
    if !small {
        ctx.setFillColor(rgb(0xB9BFCB))
        line(ctx, x: startX, y: bubble.minY + 120, length: total, thickness: 30)
        line(ctx, x: startX, y: bubble.minY + 64, length: total * 0.62, thickness: 30)
    }
}

// MARK: - Variant C: a transcript sheet with speakers

func variantC(_ ctx: CGContext, small: Bool) {
    drawBody(ctx, top: 0xFFB259, bottom: 0xEE4F5C, small: small)
    let sheet = small ? CGRect(x: 236, y: 196, width: 552, height: 632) : CGRect(x: 268, y: 196, width: 488, height: 632)
    withShadow(ctx, small: small, alpha: 0.28) {
        ctx.addPath(CGPath(roundedRect: sheet, cornerWidth: 44, cornerHeight: 44, transform: nil))
        ctx.setFillColor(rgb(0xffffff))
        ctx.fillPath()
    }
    let left = sheet.minX + (small ? 70 : 56)
    if small {
        ctx.setFillColor(rgb(0x3E6FE8))
        ctx.fillEllipse(in: CGRect(x: left, y: 560, width: 90, height: 90))
        ctx.setFillColor(rgb(0x2FA866))
        ctx.fillEllipse(in: CGRect(x: left, y: 340, width: 90, height: 90))
        ctx.setFillColor(rgb(0x9AA1AE))
        line(ctx, x: left + 130, y: 605, length: 260, thickness: 56)
        line(ctx, x: left + 130, y: 385, length: 200, thickness: 56)
        return
    }
    // Waveform header.
    let bars: [CGFloat] = [40, 84, 120, 70, 104, 56, 92, 36, 64]
    ctx.setFillColor(rgb(0x2B2D33))
    for (i, h) in bars.enumerated() {
        roundedBar(ctx, x: left + CGFloat(i) * 42, centerY: 724, width: 22, height: h)
    }
    // Speakers: a colored dot and text lines each.
    let rows: [(UInt32, [CGFloat], CGFloat)] = [
        (0x3E6FE8, [300, 220], 580),
        (0x2FA866, [330, 180], 410),
    ]
    for (color, lengths, top) in rows {
        ctx.setFillColor(rgb(color))
        ctx.fillEllipse(in: CGRect(x: left, y: top - 22, width: 44, height: 44))
        ctx.setFillColor(rgb(0x9AA1AE))
        for (j, length) in lengths.enumerated() {
            line(ctx, x: left + 72, y: top - CGFloat(j) * 64, length: length, thickness: 26)
        }
    }
    ctx.setFillColor(rgb(0x3E6FE8))
    ctx.fillEllipse(in: CGRect(x: left, y: 260 - 22, width: 44, height: 44))
    ctx.setFillColor(rgb(0x9AA1AE))
    line(ctx, x: left + 72, y: 260, length: 250, thickness: 26)
}

// MARK: - Rendering

func render(variant: String, pixels: Int) -> CGImage {
    let ctx = CGContext(
        data: nil, width: pixels, height: pixels, bitsPerComponent: 8, bytesPerRow: 0,
        space: CGColorSpace(name: CGColorSpace.sRGB)!, bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue)!
    ctx.interpolationQuality = .high
    ctx.setShouldAntialias(true)
    let scale = CGFloat(pixels) / canvas
    ctx.scaleBy(x: scale, y: scale)
    let small = pixels <= 32
    switch variant {
    case "a": variantA(ctx, small: small)
    case "b": variantB(ctx, small: small)
    case "c": variantC(ctx, small: small)
    default:
        FileHandle.standardError.write(Data("unknown variant: \(variant)\n".utf8))
        exit(2)
    }
    return ctx.makeImage()!
}

func writePNG(_ image: CGImage, to path: String) {
    let url = URL(fileURLWithPath: path)
    guard let destination = CGImageDestinationCreateWithURL(url as CFURL, UTType.png.identifier as CFString, 1, nil)
    else { fatalError("can't write \(path)") }
    CGImageDestinationAddImage(destination, image, nil)
    guard CGImageDestinationFinalize(destination) else { fatalError("can't write \(path)") }
}

let args = CommandLine.arguments
guard args.count >= 4 else {
    print("usage: make_icon preview <a|b|c> <out.png> [pixels] | make_icon iconset <a|b|c> <dir.iconset>")
    exit(2)
}
let mode = args[1]
let variant = args[2]
let out = args[3]
switch mode {
case "preview":
    let pixels = args.count > 4 ? Int(args[4]) ?? 512 : 512
    writePNG(render(variant: variant, pixels: pixels), to: out)
case "iconset":
    try FileManager.default.createDirectory(atPath: out, withIntermediateDirectories: true)
    for points in [16, 32, 128, 256, 512] {
        writePNG(render(variant: variant, pixels: points), to: "\(out)/icon_\(points)x\(points).png")
        writePNG(render(variant: variant, pixels: points * 2), to: "\(out)/icon_\(points)x\(points)@2x.png")
    }
default:
    print("unknown mode \(mode)")
    exit(2)
}
