//
//  NanoMuseFaces.swift
//  nanoMuse
//
//  The agent's face: the five moods, the dragon that every account starts
//  with, and the look the user drew in the avatar studio (kept on disk and
//  shared through the account's profile). Mirrors Android's AgentAvatar /
//  AvatarStore.
//

import SwiftUI
import UIKit
import AVFoundation
import ImageIO
import UniformTypeIdentifiers
import CryptoKit

enum NanoMuseMood: String, CaseIterable, Codable {
    case idle, working, waiting, happy, error
}

/// An emoji on a colour: the look the desktop and the web console can set and the profile
/// carries as `avatar: "emoji"` with `emoji` and `color` (#435). The phone cannot pick one
/// yet; it wears the one another device chose. The fallbacks are the desktop's
/// (`src/client/Avatar.tsx`): a sparkle for no emoji, the action blue for no colour.
struct NanoMuseEmojiLook: Codable, Equatable {
    var emoji: String
    var color: String

    static let defaultEmoji = "✨"
    static let defaultColor = "#0064d4"

    /// What is drawn: the emoji, or the sparkle when the profile carries none.
    var shownEmoji: String {
        let trimmed = emoji.trimmingCharacters(in: .whitespacesAndNewlines)
        return trimmed.isEmpty ? Self.defaultEmoji : trimmed
    }

    /// The disc's colour, parsed from the profile's `#rrggbb`.
    var uiColor: UIColor { Self.color(hex: color) }

    /// `#rrggbb` (the `#` optional, either case) as a colour; anything else is the default blue.
    static func color(hex: String) -> UIColor {
        var digits = hex.trimmingCharacters(in: .whitespacesAndNewlines)
        if digits.hasPrefix("#") { digits.removeFirst() }
        guard digits.count == 6, let value = UInt32(digits, radix: 16) else {
            // #0064d4, the action blue
            return UIColor(red: 0, green: 100.0 / 255.0, blue: 212.0 / 255.0, alpha: 1)
        }
        return UIColor(
            red: CGFloat((value >> 16) & 0xff) / 255,
            green: CGFloat((value >> 8) & 0xff) / 255,
            blue: CGFloat(value & 0xff) / 255,
            alpha: 1
        )
    }
}

/// Where the face comes from, in the order the store settles it: the pictures drawn in the
/// studio or pulled from the profile, else the emoji look another device set, else the
/// bundled dragon. A clip of the person's own (NanoMuseOwnClip, #324) sits above all three
/// where clips play and is the view's business, not the store's.
enum NanoMuseFaceSource: Equatable {
    case drawn, emoji, dragon

    static func of(hasDrawn: Bool, emoji: NanoMuseEmojiLook?) -> NanoMuseFaceSource {
        if hasDrawn { return .drawn }
        if emoji != nil { return .emoji }
        return .dragon
    }
}

/// Which face the header wears: the bundled dragon, the emoji look another
/// device set, or the pictures the user drew. Pictures live under
/// Application Support/nanomuse/avatar as PNG, one per mood, plus a small
/// meta.json (description, style, when); the emoji look sits beside them in
/// emoji.json.
@MainActor
final class NanoMuseFaceStore: ObservableObject {
    static let shared = NanoMuseFaceStore()

    struct Meta: Codable, Equatable {
        var description: String = ""
        var style: String = ""
        var createdAt: Double = 0
        var faceId: String = ""
    }

    @Published private(set) var custom: [NanoMuseMood: UIImage] = [:]
    @Published private(set) var meta = Meta()
    /// The emoji on its colour the profile carries (#435), or nil. Worn only when nothing was
    /// drawn: a drawn face replaces it, a reset to the dragon removes it.
    @Published private(set) var emojiLook: NanoMuseEmojiLook?

    var hasCustomFace: Bool { custom[.idle] != nil }

    /// The drawn face, the emoji look or the dragon, in that order.
    var source: NanoMuseFaceSource { NanoMuseFaceSource.of(hasDrawn: hasCustomFace, emoji: emojiLook) }

    private static var dragonCache: [NanoMuseMood: UIImage] = [:]
    /// Rendered emoji stills, keyed by emoji, colour and side.
    private static var emojiCache: [String: UIImage] = [:]

    private init() {
        load()
    }

    // MARK: Paths

    /// The stills' folder; the motion clips sit under `motion/` inside it (NanoMuseAvatarMotion).
    nonisolated static var directory: URL {
        let base = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask).first
            ?? FileManager.default.temporaryDirectory
        return base.appendingPathComponent("nanomuse/avatar", isDirectory: true)
    }

    private static func file(_ mood: NanoMuseMood) -> URL {
        directory.appendingPathComponent("\(mood.rawValue).png")
    }

    private static var metaFile: URL { directory.appendingPathComponent("meta.json") }
    /// The emoji look (#435), beside the stills so an account change (C12) takes it along.
    private static var emojiFile: URL { directory.appendingPathComponent("emoji.json") }

    // MARK: Reading

    /// The picture for a mood: the drawn one, the drawn idle still when that
    /// mood was never drawn, else the emoji look on its colour (the same
    /// still for every mood), else the dragon. Nil only when the bundle lacks
    /// the dragon files as well.
    func image(for mood: NanoMuseMood) -> UIImage? {
        if let own = custom[mood] { return own }
        if let idle = custom[.idle] { return idle }
        if let look = emojiLook { return Self.emojiStill(look, side: 512) }
        return Self.dragon(mood)
    }

    /// The emoji centred on a square of its colour, `side` points at 1x, the glyph about 0.58
    /// of the side as the desktop draws it (a longer run of emoji shrinks to fit). The views
    /// clip it to their disc. Cached per look and side.
    static func emojiStill(_ look: NanoMuseEmojiLook, side: CGFloat) -> UIImage {
        let key = "\(look.shownEmoji)|\(look.color.lowercased())|\(Int(side))"
        if let cached = emojiCache[key] { return cached }
        let format = UIGraphicsImageRendererFormat()
        format.scale = 1
        format.opaque = true
        let renderer = UIGraphicsImageRenderer(size: CGSize(width: side, height: side), format: format)
        let text = look.shownEmoji as NSString
        let image = renderer.image { _ in
            look.uiColor.setFill()
            UIRectFill(CGRect(x: 0, y: 0, width: side, height: side))
            var pointSize = side * 0.58
            var attributes: [NSAttributedString.Key: Any] = [.font: UIFont.systemFont(ofSize: pointSize)]
            var bounds = text.size(withAttributes: attributes)
            let widest = side * 0.86
            if bounds.width > widest, bounds.width > 0 {
                pointSize *= widest / bounds.width
                attributes = [.font: UIFont.systemFont(ofSize: pointSize)]
                bounds = text.size(withAttributes: attributes)
            }
            let origin = CGPoint(x: (side - bounds.width) / 2, y: (side - bounds.height) / 2)
            text.draw(at: origin, withAttributes: attributes)
        }
        emojiCache[key] = image
        return image
    }

    static func dragon(_ mood: NanoMuseMood) -> UIImage? {
        if let cached = dragonCache[mood] { return cached }
        let name = "dragon-\(mood.rawValue)"
        var image: UIImage?
        if let url = Bundle.main.url(forResource: name, withExtension: "webp"),
           let data = try? Data(contentsOf: url) {
            image = UIImage(data: data)
        }
        if image == nil, let named = UIImage(named: name) {
            image = named
        }
        if let image { dragonCache[mood] = image }
        return image
    }

    private func load() {
        var found: [NanoMuseMood: UIImage] = [:]
        for mood in NanoMuseMood.allCases {
            let url = Self.file(mood)
            if let data = try? Data(contentsOf: url), let img = UIImage(data: data) {
                found[mood] = img
            }
        }
        custom = found
        if let data = try? Data(contentsOf: Self.metaFile),
           let m = try? JSONDecoder().decode(Meta.self, from: data) {
            meta = m
        }
        if let data = try? Data(contentsOf: Self.emojiFile),
           let look = try? JSONDecoder().decode(NanoMuseEmojiLook.self, from: data) {
            emojiLook = look
        }
    }

    // MARK: Writing

    /// A new face: `stills` must contain at least the idle picture. Replaces
    /// whatever was worn before.
    func adopt(_ stills: [NanoMuseMood: UIImage], description: String, style: String) {
        guard let idle = stills[.idle] else { return }
        // The old face's clips go with it; the new ones follow the poses (NanoMuseAvatarStudio).
        NanoMuseAvatarMotion.shared.clear()
        let fm = FileManager.default
        try? fm.createDirectory(at: Self.directory, withIntermediateDirectories: true)
        for mood in NanoMuseMood.allCases {
            try? fm.removeItem(at: Self.file(mood))
        }
        var kept: [NanoMuseMood: UIImage] = [:]
        for (mood, image) in stills {
            let squared = Self.square(image, side: 512)
            if let png = squared.pngData() {
                try? png.write(to: Self.file(mood), options: .atomic)
                kept[mood] = squared
            }
        }
        custom = kept
        let idleBytes = Self.encodeStill(Self.square(idle, side: 512)) ?? Data()
        meta = Meta(description: description, style: style,
                    createdAt: Date().timeIntervalSince1970,
                    faceId: Self.faceId(of: idleBytes))
        saveMeta()
        // a drawn face replaces an emoji look; the push below tells the account so
        removeEmojiLook()
        NanoMuseProfileSync.shared.faceChanged()
    }

    /// One more pose for a face that is already worn.
    func put(_ mood: NanoMuseMood, image: UIImage) {
        let squared = Self.square(image, side: 512)
        try? FileManager.default.createDirectory(at: Self.directory, withIntermediateDirectories: true)
        if let png = squared.pngData() {
            try? png.write(to: Self.file(mood), options: .atomic)
        }
        custom[mood] = squared
    }

    /// C12: the folder changed under the store — another account's face is in place (or none).
    /// The motion clips are per face and go; NanoMuseAvatarMotion draws new ones when enabled.
    func reload() {
        NanoMuseAvatarMotion.shared.clear()
        custom = [:]
        meta = Meta()
        emojiLook = nil
        load()
    }

    /// Back to the dragon. The stills, the meta and the emoji look go; a clip of the person's
    /// own (NanoMuseOwnClip, in the same folder) is not the drawn face's and stays.
    func reset(sync: Bool = true) {
        NanoMuseAvatarMotion.shared.clear()
        removeStills()
        custom = [:]
        meta = Meta()
        removeEmojiLook()
        if sync { NanoMuseProfileSync.shared.faceChanged() }
    }

    private func removeStills() {
        let fm = FileManager.default
        for mood in NanoMuseMood.allCases {
            try? fm.removeItem(at: Self.file(mood))
        }
        try? fm.removeItem(at: Self.metaFile)
    }

    private func removeEmojiLook() {
        try? FileManager.default.removeItem(at: Self.emojiFile)
        emojiLook = nil
    }

    /// Wear a face that another device drew (pulled from the profile). Clips are per device:
    /// the old ones go, and the caller asks NanoMuseAvatarMotion for new ones when enabled.
    /// An emoji look goes too: the profile carries one look at a time.
    func wear(remote stills: [NanoMuseMood: UIImage], faceId: String, description: String, style: String) {
        guard stills[.idle] != nil else { return }
        NanoMuseAvatarMotion.shared.clear()
        let fm = FileManager.default
        removeStills()
        try? fm.createDirectory(at: Self.directory, withIntermediateDirectories: true)
        var kept: [NanoMuseMood: UIImage] = [:]
        for (mood, image) in stills {
            if let png = image.pngData() {
                try? png.write(to: Self.file(mood), options: .atomic)
                kept[mood] = image
            }
        }
        custom = kept
        meta = Meta(description: description, style: style,
                    createdAt: Date().timeIntervalSince1970, faceId: faceId)
        saveMeta()
        removeEmojiLook()
    }

    /// Wear the emoji look another device set (pulled from the profile, #435). The drawn
    /// stills and their clips go, as they do when another device goes back to the dragon;
    /// nothing is pushed, the account already has this look.
    func wear(emoji look: NanoMuseEmojiLook) {
        if hasCustomFace {
            NanoMuseAvatarMotion.shared.clear()
            removeStills()
            custom = [:]
            meta = Meta()
        }
        guard look != emojiLook else { return }
        try? FileManager.default.createDirectory(at: Self.directory, withIntermediateDirectories: true)
        if let data = try? JSONEncoder().encode(look) {
            try? data.write(to: Self.emojiFile, options: .atomic)
        }
        emojiLook = look
    }

    private func saveMeta() {
        if let data = try? JSONEncoder().encode(meta) {
            try? data.write(to: Self.metaFile, options: .atomic)
        }
    }

    // MARK: Encoding helpers

    /// The still as the relay wants it: WebP when Image I/O can write it,
    /// else a PNG shrunk until it fits the relay's 200 KB per-still limit.
    static func encodeStill(_ image: UIImage) -> Data? {
        if let webp = encodeWebP(image, quality: 0.86), webp.count <= 190 * 1024 { return webp }
        var side: CGFloat = 512
        while side >= 128 {
            let small = square(image, side: side)
            if let png = small.pngData(), png.count <= 190 * 1024 { return png }
            side /= 2
        }
        return nil
    }

    static func encodeWebP(_ image: UIImage, quality: CGFloat) -> Data? {
        guard let cg = image.cgImage else { return nil }
        let data = NSMutableData()
        guard let dest = CGImageDestinationCreateWithData(data, UTType.webP.identifier as CFString, 1, nil) else { return nil }
        let options: [CFString: Any] = [kCGImageDestinationLossyCompressionQuality: quality]
        CGImageDestinationAddImage(dest, cg, options as CFDictionary)
        guard CGImageDestinationFinalize(dest) else { return nil }
        return data as Data
    }

    static func faceId(of idleBytes: Data) -> String {
        guard !idleBytes.isEmpty else { return "" }
        let digest = Insecure.SHA1.hash(data: idleBytes)
        return digest.map { String(format: "%02x", $0) }.joined().prefix(12).description
    }

    /// Centre-crop to a square and scale to `side` points at 1x.
    static func square(_ image: UIImage, side: CGFloat) -> UIImage {
        let w = image.size.width, h = image.size.height
        guard w > 0, h > 0 else { return image }
        let s = min(w, h)
        let crop = CGRect(x: (w - s) / 2, y: (h - s) / 2, width: s, height: s)
        let format = UIGraphicsImageRendererFormat()
        format.scale = 1
        format.opaque = false
        let renderer = UIGraphicsImageRenderer(size: CGSize(width: side, height: side), format: format)
        return renderer.image { _ in
            let scale = side / s
            let origin = CGPoint(x: -crop.minX * scale, y: -crop.minY * scale)
            image.draw(in: CGRect(origin: origin, size: CGSize(width: w * scale, height: h * scale)))
        }
    }
}

/// The face at a given size and mood. When the mood has a motion clip — a
/// drawn face's `avatar/motion/<mood>.mp4`, or the bundled dragon's
/// `dragon-<mood>.mp4` — it plays muted in a loop, clipped to the disc.
/// Otherwise the still, with the small life the Android header gives it: a
/// slow breath when idle, a quicker bob while working, a tilt while
/// waiting, a pop when happy and a shake on error. Android: AgentAvatar.kt.
/// A clip of the person's own (NanoMuseOwnClip, #324) plays instead for every mood.
struct NanoMuseFaceView: View {
    var mood: NanoMuseMood
    var size: CGFloat
    var showsRing: Bool = true

    @ObservedObject private var faces = NanoMuseFaceStore.shared
    @ObservedObject private var motion = NanoMuseAvatarMotion.shared
    @ObservedObject private var own = NanoMuseOwnClip.shared
    @Environment(\.scenePhase) private var scenePhase
    /// Reduce Motion on: the still keeps still (no breath, bob, tilt, pop or shake); the mood
    /// still changes with its crossfade, and a clip the person chose still plays.
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @State private var breath = false
    @State private var popped = false
    @State private var shake: CGFloat = 0

    /// The clip for this mood: the person's own, when one is set (#324), stands in for every
    /// mood; else the drawn face's clip for this mood, or the dragon's. An emoji look (#435)
    /// has no clip: its still moves as the dragon's does.
    private var clip: URL? {
        if let state = own.moods[mood] { return state }
        if let ownClip = own.clip { return ownClip }
        switch faces.source {
        case .drawn: return motion.clips[mood]
        case .emoji: return nil
        case .dragon: return NanoMuseAvatarMotion.dragonClip(mood)
        }
    }

    /// Tells one still from the next so the crossfade runs: the mood, and which face it is.
    private var stillID: String {
        switch faces.source {
        case .drawn: return mood.rawValue + "c"
        case .emoji: return mood.rawValue + "e" + (faces.emojiLook?.shownEmoji ?? "") + (faces.emojiLook?.color ?? "")
        case .dragon: return mood.rawValue + "d"
        }
    }

    private var clipVersion: Int { own.moods[mood] != nil || own.clip != nil ? own.version : motion.clipVersion }

    /// The still under the clip, and the face itself when there is no clip: the drawn one for
    /// this mood, or the own clip's first frame when nothing was drawn, else the emoji look,
    /// else the dragon.
    private var still: UIImage? {
        if !faces.hasCustomFace, let poster = own.moodPosters[mood] ?? (own.clip != nil ? own.poster : nil) { return poster }
        return faces.image(for: mood)
    }

    var body: some View {
        ZStack {
            if showsRing {
                Circle().fill(NanoMuseTones.disc)
            }
            if let clip {
                ZStack {
                    // The still underneath covers the first frames while the player warms up.
                    if let img = still {
                        Image(uiImage: img).resizable().scaledToFill()
                    }
                    NanoMuseLoopingClipView(url: clip, version: clipVersion)
                }
                .frame(width: size, height: size)
                .clipShape(Circle())
                .transition(.opacity)
            } else if let img = still {
                Image(uiImage: img)
                    .resizable()
                    .scaledToFill()
                    .frame(width: size, height: size)
                    .clipShape(Circle())
                    .transition(.opacity)
                    .id(stillID)
            } else {
                Image(systemName: "face.smiling")
                    .resizable()
                    .scaledToFit()
                    .padding(size * 0.18)
                    .foregroundStyle(NanoMuseTones.action)
            }
        }
        .frame(width: size, height: size)
        .scaleEffect(scale)
        .rotationEffect(.degrees(tilt))
        .offset(x: shake, y: bob)
        .animation(.easeInOut(duration: 0.35), value: mood)
        .onAppear {
            if !reduceMotion { startBreathing() }
            NanoMuseClipPlayers.shared.setSuspended(scenePhase != .active)
        }
        .nmOnChange(of: scenePhase) { phase in
            NanoMuseClipPlayers.shared.setSuspended(phase != .active)
        }
        .nmOnChange(of: mood) { newMood in
            guard !reduceMotion else { return }
            if newMood == .happy {
                popped = true
                withAnimation(.spring(response: 0.35, dampingFraction: 0.45)) { popped = false }
            } else if newMood == .error {
                withAnimation(.interpolatingSpring(stiffness: 600, damping: 6)) { shake = 4 }
                DispatchQueue.main.asyncAfter(deadline: .now() + 0.25) {
                    withAnimation(.interpolatingSpring(stiffness: 600, damping: 10)) { shake = 0 }
                }
            }
        }
        .accessibilityLabel(Text(AppLocalized("Agent face")))
    }

    /// The still's own motion; a clip brings its own and stays at rest, and so does the still
    /// under Reduce Motion.
    private var animatesStill: Bool { clip == nil && !reduceMotion }

    private var scale: CGFloat {
        if popped { return 1.12 }
        guard animatesStill else { return 1.0 }
        switch mood {
        case .working: return breath ? 1.045 : 0.985
        case .idle, .waiting, .happy, .error: return breath ? 1.02 : 1.0
        }
    }

    private var bob: CGFloat {
        animatesStill && mood == .working ? (breath ? -1.5 : 1.5) : 0
    }

    private var tilt: Double {
        animatesStill && mood == .waiting ? (breath ? 6 : -6) : 0
    }

    private func startBreathing() {
        withAnimation(.easeInOut(duration: 1.6).repeatForever(autoreverses: true)) {
            breath = true
        }
    }
}

// MARK: - Clip playback

/// One muted looping player per clip, shared by every face view that shows it (the header,
/// the agent page, the studio thumbnails all point their layers at the same player). A
/// player goes when its last view does; all of them pause while the app is not in front.
@MainActor
final class NanoMuseClipPlayers {
    static let shared = NanoMuseClipPlayers()

    private struct Entry {
        let player: AVQueuePlayer
        let looper: AVPlayerLooper
        var users: Int
    }

    /// Keyed by file and version, so a redone clip (same path, new bytes) gets a fresh player
    /// while views still on the old one keep theirs until they move over.
    private var entries: [String: Entry] = [:]
    private var suspended = false

    private init() {}

    private static func key(_ url: URL, _ version: Int) -> String { "\(version):\(url.path)" }

    func acquire(_ url: URL, version: Int) -> AVQueuePlayer {
        let key = Self.key(url, version)
        if var entry = entries[key] {
            entry.users += 1
            entries[key] = entry
            if !suspended { start(entry.player) }
            return entry.player
        }
        let player = AVQueuePlayer()
        player.isMuted = true
        player.preventsDisplaySleepDuringVideoPlayback = false
        player.allowsExternalPlayback = false
        let looper = AVPlayerLooper(player: player, templateItem: AVPlayerItem(url: url))
        entries[key] = Entry(player: player, looper: looper, users: 1)
        if !suspended { start(player) }
        return player
    }

    /// Muted is not silent to the system: a playing player starts the app's audio session, and
    /// in the default category that stops whatever music the person has on. Make the session
    /// mixable first, then play, unless the clip was let go or the app left the front meanwhile.
    private func start(_ player: AVQueuePlayer) {
        AudioSessionCoordinator.shared.whenIdleMixable { [weak self] in
            guard let self, !self.suspended,
                  self.entries.values.contains(where: { $0.player === player }) else { return }
            player.play()
        }
    }

    func release(_ url: URL, version: Int) {
        let key = Self.key(url, version)
        guard var entry = entries[key] else { return }
        entry.users -= 1
        if entry.users <= 0 {
            entry.player.pause()
            entries[key] = nil
        } else {
            entries[key] = entry
        }
    }

    /// Background → pause every loop; foreground → resume.
    func setSuspended(_ suspend: Bool) {
        guard suspend != suspended else { return }
        suspended = suspend
        for entry in entries.values {
            if suspend { entry.player.pause() } else { start(entry.player) }
        }
    }
}

/// A `UIView` whose layer is the player layer, so the clip fills the disc without a video player's chrome.
final class NanoMusePlayerLayerView: UIView {
    override class var layerClass: AnyClass { AVPlayerLayer.self }
    var playerLayer: AVPlayerLayer? { layer as? AVPlayerLayer }
}

/// The muted looping clip behind a face. `version` changes when the file at `url` was rewritten
/// (a redone clip), so the player is rebuilt instead of showing the old frames.
struct NanoMuseLoopingClipView: UIViewRepresentable {
    var url: URL
    var version: Int

    final class Coordinator {
        var url: URL?
        var version = -1
    }

    func makeCoordinator() -> Coordinator { Coordinator() }

    func makeUIView(context: Context) -> NanoMusePlayerLayerView {
        let view = NanoMusePlayerLayerView()
        view.backgroundColor = .clear
        view.isUserInteractionEnabled = false
        view.playerLayer?.videoGravity = .resizeAspectFill
        return view
    }

    func updateUIView(_ view: NanoMusePlayerLayerView, context: Context) {
        let coordinator = context.coordinator
        guard coordinator.url != url || coordinator.version != version else { return }
        if let old = coordinator.url { NanoMuseClipPlayers.shared.release(old, version: coordinator.version) }
        view.playerLayer?.player = NanoMuseClipPlayers.shared.acquire(url, version: version)
        coordinator.url = url
        coordinator.version = version
    }

    static func dismantleUIView(_ view: NanoMusePlayerLayerView, coordinator: Coordinator) {
        if let old = coordinator.url { NanoMuseClipPlayers.shared.release(old, version: coordinator.version) }
        coordinator.url = nil
        view.playerLayer?.player = nil
    }
}

/// Android's MuseTones: a quiet grey palette for the shell.
enum NanoMuseTones {
    static let surface = Color(uiColor: UIColor { $0.userInterfaceStyle == .dark ? UIColor(red: 0.11, green: 0.11, blue: 0.118, alpha: 1) : .white })
    static let fill = Color(uiColor: UIColor { $0.userInterfaceStyle == .dark ? UIColor(red: 0.165, green: 0.165, blue: 0.18, alpha: 1) : UIColor(red: 0.945, green: 0.945, blue: 0.957, alpha: 1) })
    static let bubble = Color(uiColor: UIColor { $0.userInterfaceStyle == .dark ? UIColor(red: 0.149, green: 0.149, blue: 0.165, alpha: 1) : UIColor(red: 0.914, green: 0.918, blue: 0.925, alpha: 1) })
    static let disc = Color(uiColor: UIColor { $0.userInterfaceStyle == .dark ? UIColor(red: 0.165, green: 0.165, blue: 0.18, alpha: 1) : UIColor(red: 0.945, green: 0.937, blue: 0.922, alpha: 1) })
    static let hairline = Color(uiColor: UIColor { $0.userInterfaceStyle == .dark ? UIColor(red: 0.227, green: 0.227, blue: 0.235, alpha: 1) : UIColor(red: 0.898, green: 0.898, blue: 0.918, alpha: 1) })
    static let canvas = Color(uiColor: UIColor { $0.userInterfaceStyle == .dark ? .black : UIColor(red: 0.953, green: 0.953, blue: 0.961, alpha: 1) })
    static let action = Color(red: 0.039, green: 0.4, blue: 0.894)
    /// The chat's ink (Android's `ChatColors.userBubble` and `sendButton` since #331): your
    /// message pill, the send disc and the scroll-to-latest disc. Black on the white canvas in
    /// light, white on the black canvas in dark; `onInk` is what sits on it, the canvas colour.
    static let ink = Color(uiColor: UIColor { $0.userInterfaceStyle == .dark ? .white : .black })
    static let onInk = Color(uiColor: UIColor { $0.userInterfaceStyle == .dark ? .black : .white })
    /// The agent's message pill, a shade off the canvas (Android's `ChatColors.assistantBubble`:
    /// #F2F2F7 in light, #1C1C1E in dark). The composer pill keeps `bubble`.
    static let agentPill = Color(uiColor: UIColor { $0.userInterfaceStyle == .dark ? UIColor(red: 0.11, green: 0.11, blue: 0.118, alpha: 1) : UIColor(red: 0.949, green: 0.949, blue: 0.969, alpha: 1) })
    /// The pill bar's indicator disc and the glyph on it: the same ink as the chat, black with
    /// a white glyph in light and white with a black glyph in dark, as Android's MuseBottomBar.
    static let tabIndicator = ink
    static let tabIndicatorGlyph = onInk
}

/// Whether the UI runs in Chinese right now (the in-app override wins).
enum NanoMuseLocale {
    /// The BCP-47 tag of the language the screens show right now (`zh-Hans`, `en`, `ja`): the
    /// in-app override wins, else the system's first preferred language.
    static var tag: String {
        AppBundle.current.preferredLocalizations.first
            ?? Locale.preferredLanguages.first ?? "en"
    }

    static var isChinese: Bool {
        tag.lowercased().hasPrefix("zh")
    }
}
