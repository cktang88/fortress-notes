import AppKit
import SwiftUI

/// Liquid Glass for the app's four floating surfaces (DESIGN a.7: the palette, Quick
/// Capture, the Ask composer, the toast), with the HIG's fallbacks: an opaque window
/// background with a hairline border under Reduce Transparency, and a border on the
/// glass under Increase Contrast.
struct FloatingGlass<S: Shape>: ViewModifier {
    let shape: S
    var interactive = false
    @Environment(\.accessibilityReduceTransparency) private var reduceTransparency
    @Environment(\.colorSchemeContrast) private var contrast

    func body(content: Content) -> some View {
        if reduceTransparency {
            content
                .background(Color(nsColor: .windowBackgroundColor), in: shape)
                .overlay(shape.stroke(Color(nsColor: .separatorColor), lineWidth: 1))
        } else {
            content
                .glassEffect(interactive ? .regular.interactive() : .regular, in: shape)
                .overlay {
                    if contrast == .increased {
                        shape.stroke(Color(nsColor: .separatorColor), lineWidth: 1)
                    }
                }
        }
    }
}

extension View {
    func floatingGlass<S: Shape>(in shape: S, interactive: Bool = false) -> some View {
        modifier(FloatingGlass(shape: shape, interactive: interactive))
    }
}
