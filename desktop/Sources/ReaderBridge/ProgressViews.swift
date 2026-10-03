import AppKit
import SwiftUI

struct ReadingProgressView: View {
    @EnvironmentObject var model: AppModel
    let status: BridgeStatus
    var pairing = false
    var deviceChoice: SetupDeviceChoice? = nil
    var crosspointModel: String? = nil
    @AppStorage("setup.devices") private var savedDeviceChoice = ""
    @AppStorage("setup.crosspointModel") private var savedCrossPointModel = ""
    @AppStorage("setup.positions") private var savedPositionChoice = ""
    private var choice: SetupDeviceChoice { deviceChoice ?? SetupDeviceChoice.resolved(saved: savedDeviceChoice, status: status) ?? .both }
    private var selectedModel: String { crosspointModel ?? SetupInput.selectedCrossPointModel(saved: savedCrossPointModel, status: status) }
    private var selectedDevice: BridgeStatus.SupportedDevice? { status.crossPointDevices.first { $0.id == selectedModel } }
    private var deviceName: String { selectedDevice?.name ?? "CrossPoint reader" }
    private var crossPointPaired: Bool { progress?.xteinkPaired == true && (progress?.xteinkModel ?? BridgeStatus.SupportedDevice.legacyX4Pro.id) == selectedModel }
    private var configured: Bool { progress?.isConfigured(for: choice, crosspointModel: choice.includesCrossPoint ? selectedModel : nil) == true }
    @State private var kindleMount = ""
    @State private var kindleAutoSync = true
    @State private var xteinkMount = ""
    @State private var deviceURL = ""
    @State private var useWiFi = false
    private var progress: BridgeStatus.LocalProgress? { status.localProgress }
    private var canConnectReader: Bool { SetupInput.canConnectReadingPositions(progress) }
    private var ready: Bool { canConnectReader && progress?.error.isEmpty == true }

    var body: some View {
        Card(title: "Reading positions") {
            if progress?.enabled == true {
                HStack {
                    Label(ready ? "Position sync on" : "Position sync needs attention", systemImage: "bookmark")
                        .font(.headline)
                    Spacer()
                    if !pairing {
                        Menu { Button("Turn off position sync") { Task { await model.perform("stop_progress", activity: "Stopping position sync…", success: "Position sync is off. Your saved places are kept."); if model.error == nil { savedPositionChoice = SetupPositionChoice.later.rawValue } } } } label: { Image(systemName: "ellipsis") }
                            .menuStyle(.borderlessButton).fixedSize()
                    }
                }
                if let error = progress?.error, !error.isEmpty { Text(error).font(.caption).foregroundStyle(.orange) }
                if !canConnectReader { startButton }
                if pairing { pairingSteps }
                else {
                    Text("\(progress?.bookCount ?? 0) saved positions")
                        .font(.callout).foregroundStyle(.secondary)
                    uploadActivity
                    Button(configured ? "Manage readers" : "Connect readers") { model.selection = .setup }
                        .buttonStyle(.borderedProminent)
                }
            } else {
                Text(choice == .both ? "Continue reading on either reader. Use the exact same EPUB file on both." : "Save your reading place on this Mac while your reader syncs.")
                    .font(.callout).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
                startButton
            }
        }
        .disabled(model.busy)
        .onAppear { populate() }
        .onChange(of: status.mounts.map(\.path)) { _ in populate() }
    }

    private var startButton: some View {
        Button(progress?.enabled == true ? "Restart position sync" : "Turn on position sync") {
            Task {
                await model.perform("start_progress", ["endpoint": progress?.endpoint.isEmpty == false ? progress!.endpoint : status.endpoint], activity: "Starting reading-position sync…", success: "Connect your readers below.")
                if model.error == nil { savedPositionChoice = SetupPositionChoice.enabled.rawValue; model.selection = .setup }
            }
        }.buttonStyle(.borderedProminent).disabled(status.endpoint.isEmpty && (progress?.endpoint.isEmpty ?? true))
    }

    @ViewBuilder private var pairingSteps: some View {
        if configured {
            Label(choice == .both ? "Readers paired" : "Reader paired", systemImage: "checkmark.circle").font(.callout).foregroundStyle(teal)
            uploadActivity
            DisclosureGroup("Reconnect readers") { readerConnections.padding(.top, 12) }.font(.callout)
        } else {
            readerConnections
        }
    }

    @ViewBuilder private var uploadActivity: some View {
        if let uploads = progress?.uploads, !uploads.isEmpty {
            ForEach(uploads.indices, id: \.self) { index in
                let upload = uploads[index]
                Text("\(upload.device) · received \(Date(timeIntervalSince1970: upload.receivedAt).formatted(date: .abbreviated, time: .shortened))")
                    .font(.caption).foregroundStyle(.secondary)
            }
        } else if configured {
            Text("Waiting for reader activity").font(.caption).foregroundStyle(.secondary)
        }
    }

    private var readerConnections: some View {
        VStack(alignment: .leading, spacing: 16) {
            if choice.includesKindle {
                Divider()
                Label(progress?.kindlePaired == true ? "Kindle settings saved" : "Connect your Kindle", systemImage: progress?.kindlePaired == true ? "checkmark.circle" : "cable.connector")
                    .font(.headline)
                Text("Close KOReader and connect by USB.").font(.callout).foregroundStyle(.secondary)
                Toggle("Sync automatically", isOn: $kindleAutoSync).font(.callout)
                if kindleAutoSync {
                    Text("KOReader turns Wi-Fi on when needed.").font(.caption).foregroundStyle(.secondary)
                }
                HStack {
                    TextField("Kindle USB folder", text: $kindleMount).textFieldStyle(.roundedBorder)
                    Button("Choose…") { if let path = chooseFolder() { kindleMount = path } }
                    Button(progress?.kindlePaired == true ? "Reconnect Kindle" : "Connect Kindle") {
                        Task { await model.perform("pair_progress_kindle", ["mount": kindleMount, "auto_sync": kindleAutoSync], activity: "Copying positions and connecting Kindle…", success: "Kindle settings saved. Eject it and reopen KOReader.") }
                    }.disabled(!canConnectReader || kindleMount.isEmpty)
                }
            }
            if choice.includesCrossPoint {
                Divider()
                Label(crossPointPaired ? "\(deviceName) settings saved" : "Connect \(deviceName)", systemImage: crossPointPaired ? "checkmark.circle" : "cable.connector")
                    .font(.headline)
                Text(useWiFi ? "First setup on older firmware: open File Transfer, then enter its address. Existing or protected settings need USB or the SD card." : "Connect the reader in USB drive mode, or insert its SD card into your Mac. Choose the mounted reader folder.").font(.callout).foregroundStyle(.secondary)
                HStack {
                    if !useWiFi {
                        TextField("Reader USB / SD card folder", text: $xteinkMount).textFieldStyle(.roundedBorder)
                        Button("Choose…") { if let path = chooseFolder() { xteinkMount = path } }
                    } else {
                        TextField("http://192.168.1.42", text: $deviceURL).textFieldStyle(.roundedBorder).accessibilityLabel("CrossPoint File Transfer address")
                    }
                    Button(crossPointPaired ? "Reconnect reader" : "Connect reader") {
                        Task {
                            var parameters: [String: Any] = ["model": selectedModel]
                            parameters[useWiFi ? "device_url" : "mount"] = useWiFi ? deviceURL : xteinkMount
                            await model.perform("pair_progress_xteink", parameters, activity: "Connecting reader positions…", success: useWiFi ? "Reader settings saved. Leave File Transfer and restart the reader." : "Reader settings saved. Eject the reader or card, then restart the reader.")
                        }
                    }.disabled(!canConnectReader || selectedDevice?.pairingSupported != true || selectedDevice?.capabilities.progress != true || (useWiFi ? !SetupInput.validLANAddress(deviceURL) : xteinkMount.isEmpty))
                }
                Toggle("Use Wi-Fi with older firmware", isOn: $useWiFi).font(.caption)
            }
        }
    }
    private func populate() {
        kindleMount = SetupInput.suggestedMount(kind: "kindle", mounts: status.mounts, current: kindleMount)
        xteinkMount = SetupInput.suggestedMount(kind: "xteink", mounts: status.mounts, current: xteinkMount)
        if deviceURL.isEmpty { deviceURL = status.xteink.url }
    }
}
