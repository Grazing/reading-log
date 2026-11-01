import AppKit
import Foundation

let size: CGFloat = 1024
let scale: CGFloat = 1
let px = Int(size * scale)
let img = NSImage(size: NSSize(width: size, height: size))
img.lockFocus()

let bg = NSColor(srgbRed: 158 / 255, green: 142 / 255, blue: 127 / 255, alpha: 1)
bg.setFill()
NSBezierPath(roundedRect: NSRect(x: 0, y: 0, width: size, height: size), xRadius: 180, yRadius: 180).fill()

NSColor.white.setStroke()
NSColor.white.setFill()

func strokeRounded(_ r: NSRect, radius: CGFloat, width: CGFloat) {
    let p = NSBezierPath(roundedRect: r, xRadius: radius, yRadius: radius)
    p.lineWidth = width
    p.lineJoinStyle = .miter
    p.stroke()
}

func strokeLine(_ x1: CGFloat, _ y1: CGFloat, _ x2: CGFloat, _ y2: CGFloat, width: CGFloat) {
    let p = NSBezierPath()
    p.move(to: NSPoint(x: x1, y: y1))
    p.line(to: NSPoint(x: x2, y: y2))
    p.lineWidth = width
    p.lineCapStyle = .square
    p.stroke()
}

// AppKit y-up. SVG y-down. Convert: y' = 1024 - y
func Y(_ svgY: CGFloat) -> CGFloat { size - svgY }

strokeRounded(NSRect(x: 196, y: Y(214 + 640), width: 632, height: 640), radius: 56, width: 40)
strokeLine(196, Y(362), 828, Y(362), width: 32)

func ring(cx: CGFloat, cy: CGFloat) {
    let r: CGFloat = 40
    let rect = NSRect(x: cx - r, y: Y(cy) - r, width: r * 2, height: r * 2)
    bg.setFill()
    NSBezierPath(ovalIn: rect).fill()
    NSColor.white.setStroke()
    let p = NSBezierPath(ovalIn: rect)
    p.lineWidth = 32
    p.stroke()
}
ring(cx: 372, cy: 214)
ring(cx: 652, cy: 214)

let gridW: CGFloat = 20
for x in [276, 356, 436, 516, 596, 676, 748] as [CGFloat] {
    strokeLine(x, Y(420), x, Y(790), width: gridW)
}
for y in [494, 586, 678, 770] as [CGFloat] {
    strokeLine(196, Y(y), 828, Y(y), width: gridW)
}

img.unlockFocus()

guard let tiff = img.tiffRepresentation,
      let rep = NSBitmapImageRep(data: tiff),
      let png = rep.representation(using: .png, properties: [:]) else {
    fputs("png failed\n", stderr)
    exit(1)
}
let dest = CommandLine.arguments.count > 1 ? CommandLine.arguments[1] : "AppIcon.png"
try png.write(to: URL(fileURLWithPath: dest))
print("wrote \(dest)")
