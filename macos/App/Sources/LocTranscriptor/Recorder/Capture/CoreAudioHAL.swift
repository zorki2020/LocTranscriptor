// Thin, failure-tolerant wrappers over Core Audio object properties. Every getter returns
// nil or an empty list instead of throwing: callers decide what a missing value means.

import CoreAudio
import Foundation

enum HAL {
    static let system = AudioObjectID(kAudioObjectSystemObject)

    static func address(
        _ selector: AudioObjectPropertySelector,
        _ scope: AudioObjectPropertyScope = kAudioObjectPropertyScopeGlobal
    ) -> AudioObjectPropertyAddress {
        AudioObjectPropertyAddress(
            mSelector: selector, mScope: scope, mElement: kAudioObjectPropertyElementMain)
    }

    static func value<T>(
        _ object: AudioObjectID, _ selector: AudioObjectPropertySelector,
        scope: AudioObjectPropertyScope = kAudioObjectPropertyScopeGlobal, initial: T
    ) -> T? {
        var address = address(selector, scope)
        var value = initial
        var size = UInt32(MemoryLayout<T>.size)
        let status = withUnsafeMutablePointer(to: &value) {
            AudioObjectGetPropertyData(object, &address, 0, nil, &size, $0)
        }
        return status == noErr ? value : nil
    }

    static func array<T>(
        _ object: AudioObjectID, _ selector: AudioObjectPropertySelector,
        scope: AudioObjectPropertyScope = kAudioObjectPropertyScopeGlobal, zero: T
    ) -> [T] {
        var address = address(selector, scope)
        var size: UInt32 = 0
        guard AudioObjectGetPropertyDataSize(object, &address, 0, nil, &size) == noErr, size > 0
        else { return [] }
        let stride = MemoryLayout<T>.stride
        var values = [T](repeating: zero, count: Int(size) / stride)
        let status = values.withUnsafeMutableBytes { raw -> OSStatus in
            guard let base = raw.baseAddress else { return -1 }
            return AudioObjectGetPropertyData(object, &address, 0, nil, &size, base)
        }
        guard status == noErr else { return [] }
        return Array(values.prefix(Int(size) / stride))
    }

    static func string(
        _ object: AudioObjectID, _ selector: AudioObjectPropertySelector,
        scope: AudioObjectPropertyScope = kAudioObjectPropertyScopeGlobal
    ) -> String? {
        var address = address(selector, scope)
        var value: Unmanaged<CFString>?
        var size = UInt32(MemoryLayout<Unmanaged<CFString>?>.size)
        let status = withUnsafeMutablePointer(to: &value) {
            AudioObjectGetPropertyData(object, &address, 0, nil, &size, $0)
        }
        guard status == noErr, let value else { return nil }
        return value.takeRetainedValue() as String
    }

    // MARK: Processes

    struct Process: Sendable, Equatable {
        var id: AudioObjectID
        var pid: pid_t
        var bundleId: String
        var outputting: Bool
    }

    static func processes() -> [Process] {
        array(system, kAudioHardwarePropertyProcessObjectList, zero: AudioObjectID(0)).map { id in
            Process(
                id: id,
                pid: value(id, kAudioProcessPropertyPID, initial: pid_t(-1)) ?? -1,
                bundleId: string(id, kAudioProcessPropertyBundleID) ?? "",
                outputting: (value(id, kAudioProcessPropertyIsRunningOutput, initial: UInt32(0))
                    ?? 0) != 0)
        }
    }

    // MARK: Devices

    static func devices() -> [AudioObjectID] {
        array(system, kAudioHardwarePropertyDevices, zero: AudioObjectID(0))
    }

    static func defaultOutputDevice() -> AudioObjectID? {
        let id = value(system, kAudioHardwarePropertyDefaultOutputDevice, initial: AudioObjectID(0))
        return id == 0 ? nil : id
    }

    static func defaultInputDevice() -> AudioObjectID? {
        let id = value(system, kAudioHardwarePropertyDefaultInputDevice, initial: AudioObjectID(0))
        return id == 0 ? nil : id
    }

    static func uid(_ device: AudioObjectID) -> String? {
        string(device, kAudioDevicePropertyDeviceUID)
    }

    static func name(_ device: AudioObjectID) -> String? {
        string(device, kAudioObjectPropertyName)
    }

    /// The device with this UID if it exists and is alive. A device that is still listed but
    /// gone (unplugged, Bluetooth out of range) counts as missing.
    static func aliveDevice(uid: String) -> AudioObjectID? {
        devices().first { self.uid($0) == uid && isAlive($0) }
    }

    static func isAlive(_ device: AudioObjectID) -> Bool {
        (value(device, kAudioDevicePropertyDeviceIsAlive, initial: UInt32(0)) ?? 0) != 0
    }

    static func isHidden(_ device: AudioObjectID) -> Bool {
        (value(device, kAudioDevicePropertyIsHidden, initial: UInt32(0)) ?? 0) != 0
    }

    static func nominalRate(_ device: AudioObjectID) -> Double? {
        value(device, kAudioDevicePropertyNominalSampleRate, initial: Float64(0))
            .flatMap { $0 > 0 ? $0 : nil }
    }

    static func tapRate(_ tap: AudioObjectID) -> Double? {
        value(tap, kAudioTapPropertyFormat, initial: AudioStreamBasicDescription())
            .flatMap { $0.mSampleRate > 0 ? $0.mSampleRate : nil }
    }

    /// Channel count of each input buffer the device delivers to an IOProc.
    static func inputBuffers(_ device: AudioObjectID) -> [Int] {
        var address = address(kAudioDevicePropertyStreamConfiguration, kAudioObjectPropertyScopeInput)
        var size: UInt32 = 0
        guard AudioObjectGetPropertyDataSize(device, &address, 0, nil, &size) == noErr,
            size >= UInt32(MemoryLayout<AudioBufferList>.size)
        else { return [] }
        let raw = UnsafeMutableRawPointer.allocate(
            byteCount: Int(size), alignment: MemoryLayout<AudioBufferList>.alignment)
        defer { raw.deallocate() }
        guard AudioObjectGetPropertyData(device, &address, 0, nil, &size, raw) == noErr else {
            return []
        }
        let list = UnsafeMutableAudioBufferListPointer(raw.assumingMemoryBound(to: AudioBufferList.self))
        return list.map { Int($0.mNumberChannels) }
    }
}
