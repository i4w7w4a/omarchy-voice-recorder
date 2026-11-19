pragma ComponentBehavior: Bound

import QtQuick
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Commons as Commons
import qs.Ui

Panel {
  id: root
  moduleName: "re_pick.voice-recorder"
  manageIpc: false
  implicitWidth: mic.implicitWidth
  implicitHeight: mic.implicitHeight

  readonly property var words: strings
  property var snapshot: ({})
  readonly property string phase: snapshot.phase || "loading"
  readonly property bool capturing: phase === "recording"
  readonly property bool working: capturing || phase === "starting" || phase === "processing"
  readonly property bool locked: working || action.running || picker.running || phase === "loading"
  readonly property bool canSetPreferences: !action.running && phase !== "loading"
  readonly property bool canRecord: !locked || (capturing && !action.running)
  readonly property bool canRetry: phase === "error" && !!snapshot.original && !snapshot.cleaned
  property var liveState: ({})
  property bool liveCommandFailed: false
  readonly property string livePhase: liveState.phase || "off"
  readonly property bool liveActive: ["starting", "on", "stopping"].includes(livePhase)
  readonly property bool liveCanToggle: !liveCommand.running && livePhase !== "stopping"
  readonly property string liveProblem: liveCommandFailed ? "backend" : (liveState.error || "")
  property bool settingsPage: false
  property string localError: ""
  property double now: Date.now() / 1000
  readonly property string timecode: {
    const elapsed = capturing && snapshot.startedAt ? now - snapshot.startedAt : snapshot.duration || 0
    const seconds = Math.max(0, Math.floor(elapsed))
    const two = n => String(n).padStart(2, "0")
    return (seconds >= 3600 ? Math.floor(seconds / 3600) + ":" : "") + two(Math.floor(seconds / 60) % 60) + ":" + two(seconds % 60)
  }

  function script(name) {
    return decodeURIComponent(String(Qt.resolvedUrl("scripts/" + name)).replace(/^file:\/\//, ""))
  }
  function update(raw) {
    try {
      const data = JSON.parse(raw)
      if (!data || typeof data.phase !== "string") return false
      snapshot = data
      return true
    } catch (e) { return false }
  }
  function run(args) {
    if (action.running) return
    localError = ""
    action.command = ["/usr/bin/python3", "-B", script("recorder.py")].concat(args)
    action.running = true
  }
  function readLive(raw) {
    try {
      const state = JSON.parse(raw)
      if (!["off", "starting", "on", "stopping", "error"].includes(state.phase)) return false
      liveState = state
      liveCommandFailed = false
      return true
    } catch (e) { return false }
  }
  function liveRequest(verb) {
    if (liveCommand.running) return
    liveCommand.command = ["/usr/bin/python3", "-B", script("live.py"), verb]
    liveCommand.running = true
  }
  function toggleLive() {
    if (liveCanToggle) liveRequest(liveActive ? "stop" : "start")
  }
  function recordAction() {
    if (!canRecord) return
    stopSound()
    run([capturing ? "stop" : "start"])
  }
  function chooseFolder() {
    if (locked) return
    const language = snapshot.language
    picker.environment = language === "en" || language === "ru" ? { LANGUAGE: language } : {}
    picker.command = ["/usr/bin/python3", "-B", script("folder-picker.py"), snapshot.folder || "", strings.t("folder.choose")]
    // An external desktop dialog must receive pointer events above the layer surface.
    close()
    picker.running = true
  }
  function openFolder(path) {
    if (path) {
      close()
      Quickshell.execDetached(["xdg-open", path])
    }
  }
  function stopSound() {
    player.pending = ""
    if (player.current !== "") {
      player.stopping = true
      player.running = false
    }
  }
  function play(path) {
    if (!path || !opened || working) return
    localError = ""
    if (player.current !== "") {
      player.pending = player.current === path ? "" : path
      player.stopping = true
      player.running = false
      return
    }
    player.current = path
    player.command = ["pw-play", path]
    player.running = true
  }
  readonly property string playing: player.current
  onWorkingChanged: if (working) stopSound()
  onOpenedChanged: {
    if (opened) {
      now = Date.now() / 1000
      run(["status"])
      liveRequest("status")
    } else {
      stopSound()
    }
  }

  PluginTranslations { id: strings; preference: root.snapshot.language || "system" }
  Component.onCompleted: run(["status"])

  Process {
    id: action
    stdout: StdioCollector { id: output; waitForEnd: true }
    onExited: {
      if (!root.update(output.text)) root.localError = "backend"
      else stateFile.reload()
    }
  }
  FileView {
    id: stateFile
    path: Quickshell.env("XDG_RUNTIME_DIR") + "/omarchy-voice-recorder/state.json"
    watchChanges: true
    printErrors: false
    onLoaded: root.update(text())
    onFileChanged: reload()
  }
  Process {
    id: liveCommand
    stdout: StdioCollector { id: liveOutput; waitForEnd: true }
    onExited: {
      if (!root.readLive(liveOutput.text)) root.liveCommandFailed = true
      else liveFile.reload()
    }
  }
  FileView {
    id: liveFile
    path: Quickshell.env("XDG_RUNTIME_DIR") + "/omarchy-voice-recorder/live.json"
    watchChanges: true
    printErrors: false
    onLoaded: root.readLive(text())
    onFileChanged: reload()
  }
  Timer {
    // Reconcile a stale on-state if the streaming worker exits unexpectedly.
    interval: 5000
    repeat: true
    running: root.liveActive
    onTriggered: root.liveRequest("status")
  }
  Timer {
    interval: 250
    repeat: true
    running: root.capturing
    onTriggered: root.now = Date.now() / 1000
  }
  Timer {
    // Recover if a worker dies without writing its final snapshot.
    interval: 5000
    repeat: true
    running: root.opened && root.working
    onTriggered: root.run(["status"])
  }

  Process {
    id: player
    property string current: ""
    property string pending: ""
    property bool stopping: false
    onExited: function(code) {
      const next = pending
      if (code !== 0 && !stopping) root.localError = "play"
      pending = ""
      stopping = false
      current = ""
      if (next) Qt.callLater(() => root.play(next))
    }
  }
  Process {
    id: picker
    stdout: StdioCollector { id: picked; waitForEnd: true }
    onExited: function(code) {
      root.settingsPage = true
      const path = picked.text.replace(/\r?\n$/, "")
      // Start the settings update first. Opening will otherwise start a competing status read.
      if (code === 0 && path.startsWith("/")) root.run(["set-folder", path])
      root.open()
      if (code !== 0) root.localError = "picker"
    }
  }

  BarIconButton {
    id: mic
    anchors.fill: parent
    bar: root.bar
    slotSize: Style.bar.statusSlot
    labelText: root.capturing && !vertical ? root.timecode : ""
    fixedWidth: vertical ? -1 : slotSize + (labelText ? labelMetrics.advanceWidth(labelText) + Style.space(4) : 0)
    FontMetrics { id: labelMetrics; font.family: mic.fontFamily; font.pixelSize: mic.fontSize }
    RotationAnimation on textRotation {
      from: 0
      to: 360
      duration: Style.duration(950)
      loops: Animation.Infinite
      running: root.phase === "processing" && !Style.reduceMotion
      onStopped: mic.textRotation = 0
    }
    text: root.capturing ? "󰑊" : root.phase === "processing" ? "󰑐" : "󰍬"
    active: root.working || root.phase === "error" || root.liveActive
    activeColor: root.capturing || root.phase === "error" ? Commons.Color.urgent : Commons.Color.accent
    readonly property string fileHint: root.capturing ? strings.t("tip.stop").arg(root.timecode)
      : root.phase === "processing" ? strings.t("state.processing") : strings.t("tip.start")
    tooltipText: root.liveActive ? strings.t("live.tooltip").arg(strings.t("live." + root.livePhase)).arg(fileHint) : fileHint
    Rectangle {
      visible: root.liveActive
      width: Math.max(Style.space(6), Math.round(mic.visualIconSize * 0.45))
      height: width
      radius: width / 2
      anchors.right: parent.right
      anchors.top: parent.top
      anchors.rightMargin: Math.max(0, (mic.slotSize - mic.canvasSize) / 2)
      anchors.topMargin: Math.max(0, (mic.height - mic.canvasSize) / 2)
      color: Commons.Color.accent
      border.width: 1
      border.color: Commons.Color.bar.background
    }
    pressFeedbackButtons: Qt.LeftButton | Qt.RightButton
    onPressed: function(buttonCode) {
      if (buttonCode === Qt.RightButton) root.recordAction()
      else if (buttonCode === Qt.LeftButton) {
        if (!root.opened) root.settingsPage = false
        root.toggle()
      }
    }
  }

  KeyboardPanel {
    id: popup
    anchorItem: mic
    owner: root
    bar: root.bar
    open: root.opened
    focusTarget: loadedContent
    contentWidth: fittedContentWidth(Style.space(355))
    contentHeight: fittedContentHeight(loadedContentHeight)
    content: RecorderView { recorder: root }
  }
}
