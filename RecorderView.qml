pragma ComponentBehavior: Bound

import QtQuick
import QtQuick.Layouts
import QtQuick.Controls as Controls
import Quickshell
import Quickshell.Services.Pipewire
import qs.Commons
import qs.Commons as Commons
import qs.Ui

PanelKeyCatcher {
  id: view
  required property var recorder
  readonly property var words: recorder.words
  readonly property var stateData: recorder.snapshot
  readonly property bool settings: recorder.settingsPage
  readonly property var models: stateData.models || []
  readonly property var chosen: models.find(item => item.id === stateData.model) || null
  readonly property var used: models.find(item => item.id === stateData.cleanedModel) || null
  readonly property var liveModel: models.find(item => item.id === recorder.liveState.model) || null
  readonly property string requestedLiveModel: recorder.liveState.requestedModel || recorder.liveState.model || ""
  readonly property bool liveFallback: recorder.livePhase === "on" && !!liveModel && requestedLiveModel !== liveModel.id
  readonly property bool liveChanged: recorder.liveActive && !!chosen && !!liveModel
    && (chosen.id !== requestedLiveModel || stateData.strength !== recorder.liveState.strength
        || (stateData.device || "auto") !== (recorder.liveState.requestedDevice || "auto"))
  readonly property bool canApply: !!stateData.original && !!stateData.cleaned && stateData.matched === false
  function rateText(rate) { return words.t("model.rate").arg(rate / 1000) }
  function variantText(model, strength) {
    if (!model) return ""
    if (strength > 0) return words.t("model.variant").arg(model.label).arg(rateText(model.rate)).arg(strength)
    return words.t("model.unknownStrength").arg(model.label).arg(rateText(model.rate))
  }
  function selectModel(model) {
    if (model && model.id !== stateData.model && !recorder.locked) recorder.run(["set-model", model.id])
  }
  function selectRate(rate) {
    const group = models.filter(item => item.rate === rate)
    selectModel(group.find(item => item.label === chosen?.label)
                || group.find(item => item.label === "DPDFNet-2") || group[0])
  }
  readonly property color ink: Commons.Color.popups.text
  readonly property color muted: Util.alpha(ink, 0.72)
  readonly property color highlight: recorder.capturing ? Commons.Color.urgent : Commons.Color.accent
  readonly property string family: recorder.bar?.fontFamily || Style.font.family
  readonly property var inputNode: Pipewire.defaultAudioSource
  readonly property string problem: recorder.localError || (recorder.phase === "error" ? (stateData.error || "backend") : "")
  readonly property string pathLabel: {
    const path = stateData.folder || ""
    const home = Quickshell.env("HOME")
    return path === home || path.startsWith(home + "/") ? "~" + path.slice(home.length) : path
  }

  implicitHeight: stack.implicitHeight
  property Button cursor: null
  onCloseRequested: if (settings) recorder.settingsPage = false; else recorder.close()
  onTabRequested: function(direction) { move(direction) }
  onMoveRequested: function(dx, dy) { move(dy || dx) }
  onActivateRequested: {
    if (cursor?.visible && cursor.enabled) cursor.clicked()
    else if (!settings && mainButton.enabled) mainButton.clicked()
  }
  function move(step) {
    const available = (settings
      ? [settingsButton, systemLanguage, russian, english, fullRate, wideRate, phoneRate,
         baseModelButton, net2, net4, net8, mild, normal, strong, autoDevice, nvidia, cpu, folderButton]
      : [mainButton, dry, wet, applyButton, retryButton, filesButton, liveButton, settingsButton])
      .filter(item => item.visible && item.enabled)
    if (!available.length) return
    const at = available.indexOf(cursor)
    cursor = available[(at < 0 ? (step > 0 ? 0 : available.length - 1)
                                : (at + step + available.length) % available.length)]
    const pos = cursor.mapToItem(stack, 0, 0)
    if (pos.y < scroll.contentY) scroll.contentY = pos.y
    else if (pos.y + cursor.height > scroll.contentY + scroll.height) scroll.contentY = pos.y + cursor.height - scroll.height
  }
  onSettingsChanged: {
    cursor = null
    scroll.contentY = 0
    if (recorder.opened && !Style.reduceMotion) pageFade.restart()
  }
  Connections {
    target: view.recorder
    function onOpenedChanged() { view.cursor = null; scroll.contentY = 0 }
  }

  component Label: Text {
    textFormat: Text.PlainText
    color: view.ink
    font.family: view.family
    font.pixelSize: Style.font.body
    verticalAlignment: Text.AlignVCenter
    elide: Text.ElideRight
  }
  component Action: Button {
    id: action
    foreground: view.ink
    accent: Commons.Color.accent
    fontFamily: view.family
    opacity: enabled ? 1 : 0.48
    hasCursor: view.cursor === action
    Accessible.role: Accessible.Button
    Accessible.name: text || tooltipText
    Accessible.onPressAction: if (enabled) clicked()
  }

  Flickable {
    id: scroll
    anchors.fill: parent
    clip: true
    contentWidth: width
    contentHeight: stack.implicitHeight
    boundsBehavior: Flickable.StopAtBounds
    interactive: contentHeight > height
    Controls.ScrollBar.vertical: Controls.ScrollBar { policy: Controls.ScrollBar.AsNeeded }

    Column {
      id: stack
      width: scroll.width
      spacing: Style.space(12)

      RowLayout {
        width: parent.width
        spacing: Style.space(9)
        Rectangle {
          Layout.preferredWidth: Style.space(38)
          Layout.preferredHeight: width
          radius: Math.max(Style.cornerRadius, Style.space(6))
          color: Util.alpha(Commons.Color.accent, 0.13)
          Label {
            anchors.centerIn: parent
            color: Commons.Color.accent
            font.pixelSize: Style.font.display
            text: "󰍬"
          }
        }
        Column {
          Layout.fillWidth: true
          spacing: Style.space(1)
          Label { width: parent.width; text: view.words.t(view.settings ? "settings.title" : "title"); font.pixelSize: Style.font.heading; font.bold: true }
          Label { width: parent.width; text: view.words.t("format"); font.pixelSize: Style.font.bodySmall; color: view.muted }
        }
        Action {
          id: settingsButton
          iconText: view.settings ? "󰅖" : "󰒓"
          tooltipText: view.words.t(view.settings ? "settings.back" : "settings.title")
          onClicked: {
            view.recorder.stopSound()
            view.recorder.settingsPage = !view.settings
          }
        }
      }

      PanelSeparator { foreground: view.ink }

      Column {
        id: pages
        width: parent.width
        spacing: Style.space(12)
        NumberAnimation {
          id: pageFade
          target: pages
          property: "opacity"
          from: 0.60
          to: 1
          duration: Style.duration(160)
          easing.type: Easing.OutCubic
        }

        Column {
          visible: !view.settings
          width: parent.width
          spacing: Style.space(12)

          RowLayout {
            width: parent.width
            spacing: Style.space(8)
            Column {
              Layout.fillWidth: true
              spacing: Style.space(3)
              Row {
                spacing: Style.space(6)
                Rectangle {
                  anchors.verticalCenter: parent.verticalCenter
                  width: Style.space(7)
                  height: width
                  radius: width / 2
                  color: view.recorder.capturing || view.recorder.phase === "error" ? Commons.Color.urgent : Commons.Color.accent
                  SequentialAnimation on opacity {
                    running: view.recorder.capturing && view.recorder.opened && !Style.reduceMotion
                    loops: Animation.Infinite
                    NumberAnimation { from: 1; to: 0.45; duration: Style.duration(230); easing.type: Easing.OutCubic }
                    PauseAnimation { duration: Style.duration(230) }
                    NumberAnimation { from: 0.45; to: 1; duration: Style.duration(230); easing.type: Easing.OutCubic }
                    PauseAnimation { duration: Style.duration(230) }
                  }
                }
                Label { text: view.words.t("state." + view.recorder.phase); font.bold: true }
              }
              Label {
                width: parent.width
                color: view.muted
                font.pixelSize: Style.font.bodySmall
                text: view.inputNode?.nickname || view.inputNode?.description || view.words.t("input.default")
              }
            }
            Label {
              text: view.recorder.timecode
              font.pixelSize: Style.font.displayLarge
              font.weight: Font.DemiBold
              color: view.recorder.capturing ? Commons.Color.urgent : view.ink
            }
          }

          Action {
            id: mainButton
            objectName: "recordButton"
            width: parent.width
            verticalPadding: Style.space(10)
            fontSize: Style.font.subtitle
            iconText: view.recorder.capturing ? "󰓛" : view.recorder.working ? "󰑐" : "󰑊"
            iconSpinning: view.recorder.working && !view.recorder.capturing
            foreground: view.highlight
            background: Util.alpha(view.highlight, 0.15)
            enabled: view.recorder.canRecord
            text: view.words.t(view.recorder.capturing ? "record.stop"
                               : view.recorder.phase === "processing" ? "record.processing"
                               : view.recorder.phase === "starting" ? "record.starting" : "record.start")
            onClicked: view.recorder.recordAction()
          }

          Column {
            visible: !!view.stateData.original && !view.recorder.working
            width: parent.width
            spacing: Style.space(6)
            RowLayout {
              width: parent.width
              Label {
                Layout.fillWidth: true
                text: view.words.t("take.title")
                font.bold: true
              }
              Action {
                id: filesButton
                iconText: "󰉋"
                tooltipText: view.words.t("take.open")
                onClicked: view.recorder.openFolder(view.stateData.take)
              }
            }
            Label {
              width: parent.width
              visible: !!view.stateData.cleaned && !!view.used
              wrapMode: Text.WordWrap
              color: view.muted
              font.pixelSize: Style.font.bodySmall
              text: view.words.t("take.processed").arg(view.variantText(view.used, view.stateData.cleanedStrength))
            }
            RowLayout {
              width: parent.width
              spacing: Style.space(6)
              Action {
                id: dry
                Layout.fillWidth: true
                Layout.preferredWidth: 1
                verticalPadding: Style.space(7)
                bordered: true
                selected: view.recorder.playing !== "" && view.recorder.playing === view.stateData.original
                text: view.words.t("take.original")
                iconText: selected ? "󰓛" : "󰐊"
                onClicked: view.recorder.play(view.stateData.original)
              }
              Action {
                id: wet
                Layout.fillWidth: true
                Layout.preferredWidth: 1
                verticalPadding: Style.space(7)
                bordered: true
                enabled: !!view.stateData.cleaned
                selected: enabled && view.recorder.playing === view.stateData.cleaned
                text: view.words.t("take.cleaned")
                iconText: selected ? "󰓛" : "󰐊"
                onClicked: view.recorder.play(view.stateData.cleaned)
              }
            }
          }
          Column {
            visible: view.canApply && !view.recorder.working
            width: parent.width
            spacing: Style.space(5)
            Label {
              width: parent.width
              color: view.muted
              font.pixelSize: Style.font.bodySmall
              wrapMode: Text.WordWrap
              text: view.words.t("take.selected").arg(view.variantText(view.chosen, view.stateData.strength))
            }
            Action {
              id: applyButton
              width: parent.width
              bordered: true
              enabled: !view.recorder.locked && !!view.chosen?.installed
              iconText: "󰑐"
              text: view.words.t("take.apply")
              onClicked: {
                view.recorder.stopSound()
                view.recorder.run(["reprocess"])
              }
            }
          }
          Action {
            id: retryButton
            visible: view.recorder.canRetry
            enabled: !view.recorder.locked
            width: parent.width
            bordered: true
            iconText: "󰑐"
            text: view.words.t("record.retry")
            onClicked: view.recorder.run(["retry"])
          }
          PanelSeparator { foreground: view.ink }
          Column {
            width: parent.width
            spacing: Style.space(5)
            RowLayout {
              width: parent.width
              spacing: Style.space(8)
              Column {
                Layout.fillWidth: true
                spacing: Style.space(2)
                Label { width: parent.width; text: view.words.t("live.title"); font.bold: true }
                Label {
                  width: parent.width
                  text: view.words.t("live." + view.recorder.livePhase)
                  color: view.recorder.liveActive ? Commons.Color.accent : view.muted
                  font.pixelSize: Style.font.bodySmall
                }
              }
              Action {
                id: liveButton
                objectName: "liveToggle"
                enabled: view.recorder.liveCanToggle
                selected: view.recorder.livePhase === "on"
                bordered: true
                iconText: view.recorder.liveActive ? "󰓛" : "󰐊"
                text: view.words.t(view.recorder.liveActive ? "live.stop" : "live.start")
                Accessible.name: view.words.t("live.action").arg(text)
                Accessible.description: view.words.t("live." + view.recorder.livePhase)
                onClicked: view.recorder.toggleLive()
              }
            }
            Label {
              width: parent.width
              visible: view.recorder.liveActive && !!view.liveModel
              wrapMode: Text.WordWrap
              text: {
                const variant = view.variantText(view.liveModel, view.recorder.liveState.strength)
                const device = view.recorder.liveState.device
                return view.recorder.livePhase === "on" && (device === "cuda" || device === "cpu")
                  ? view.words.t("live.variantDevice").arg(variant).arg(view.words.t("device." + device))
                  : variant
              }
              color: view.muted
              font.pixelSize: Style.font.bodySmall
            }
            Label {
              width: parent.width
              wrapMode: Text.WordWrap
              text: view.words.t(view.recorder.liveActive ? "live.routingOn" : "live.routingOff")
              color: view.muted
              font.pixelSize: Style.font.bodySmall
            }
            Label {
              width: parent.width
              visible: view.liveFallback
              wrapMode: Text.WordWrap
              text: view.words.t("live.fallback")
              color: view.muted
              font.pixelSize: Style.font.bodySmall
            }
            Label {
              width: parent.width
              visible: view.liveChanged
              wrapMode: Text.WordWrap
              text: view.words.t("live.restart")
              color: view.muted
              font.pixelSize: Style.font.bodySmall
            }
            Label {
              width: parent.width
              visible: view.recorder.liveProblem !== ""
              wrapMode: Text.WordWrap
              text: view.recorder.liveProblem === "overload" && view.recorder.liveState.requestedDevice === "cuda"
                ? view.words.t("live.error.cudaOverload")
                : view.words.t("live.error." + view.recorder.liveProblem, view.words.t("live.error.backend"))
              color: Commons.Color.urgent
              font.pixelSize: Style.font.bodySmall
            }
          }
        }

        Column {
          visible: view.settings
          width: parent.width
          spacing: Style.space(12)
          Column {
            width: parent.width
            spacing: Style.space(5)
            Label { text: view.words.t("language.title"); font.bold: true }
            RowLayout {
              width: parent.width
              spacing: Style.space(4)
              Language { id: systemLanguage; choice: "system" }
              Language { id: russian; choice: "ru" }
              Language { id: english; choice: "en" }
            }
          }
          Column {
            width: parent.width
            spacing: Style.space(6)
            Label { text: view.words.t("model.title"); font.bold: true }
            RowLayout {
              width: parent.width
              spacing: Style.space(4)
              Rate { id: fullRate; rate: 48000 }
              Rate { id: wideRate; rate: 16000 }
              Rate { id: phoneRate; rate: 8000 }
            }
            RowLayout {
              width: parent.width
              spacing: Style.space(4)
              Variant { id: baseModelButton; label: "Baseline" }
              Variant { id: net2; label: "DPDFNet-2" }
              Variant { id: net4; label: "DPDFNet-4" }
              Variant { id: net8; label: "DPDFNet-8" }
            }
            Label {
              width: parent.width
              visible: !!view.chosen
              wrapMode: Text.WordWrap
              color: view.muted
              font.pixelSize: Style.font.bodySmall
              text: view.chosen ? view.words.t("model.note." + view.chosen.id, "") : ""
            }
            Label {
              width: parent.width
              visible: !!view.chosen && !view.chosen.installed
              wrapMode: Text.WordWrap
              color: Commons.Color.urgent
              font.pixelSize: Style.font.bodySmall
              text: view.words.t("model.missing")
            }
          }
          Column {
            width: parent.width
            spacing: Style.space(7)
            Label { text: view.words.t("strength.title"); font.bold: true }
            RowLayout {
              width: parent.width
              spacing: Style.space(4)
              Strength { id: mild; value: 6 }
              Strength { id: normal; value: 12 }
              Strength { id: strong; value: 24 }
            }
            Label {
              width: parent.width
              wrapMode: Text.WordWrap
              color: view.muted
              font.pixelSize: Style.font.bodySmall
              text: view.words.t("strength.note." + (view.stateData.strength || 12))
            }
          }
          Column {
            width: parent.width
            spacing: Style.space(6)
            Label { text: view.words.t("device.title"); font.bold: true }
            RowLayout {
              width: parent.width
              spacing: Style.space(4)
              Device { id: autoDevice; choice: "auto" }
              Device { id: nvidia; choice: "cuda" }
              Device { id: cpu; choice: "cpu" }
            }
            Label {
              width: parent.width
              wrapMode: Text.WordWrap
              color: view.muted
              font.pixelSize: Style.font.bodySmall
              text: view.words.t("device.note." + (view.stateData.device || "auto"))
            }
          }
          Column {
            width: parent.width
            spacing: Style.space(4)
            Label { text: view.words.t("folder.title"); font.bold: true }
            Label { width: parent.width; text: view.pathLabel; elide: Text.ElideMiddle; color: view.muted }
            Action {
              id: folderButton
              width: parent.width
              bordered: true
              enabled: !view.recorder.locked
              iconText: "󰉋"
              text: view.words.t("folder.choose")
              onClicked: view.recorder.chooseFolder()
            }
          }
          Label {
            width: parent.width
            visible: view.recorder.working
            wrapMode: Text.WordWrap
            text: view.words.t("settings.busy")
            color: view.muted
          }
        }
        Label {
          visible: view.problem !== ""
          width: parent.width
          wrapMode: Text.WordWrap
          text: view.problem ? view.words.t("error." + view.problem, view.words.t("error.backend")) : ""
          color: Commons.Color.urgent
        }
      }
    }
  }

  component Segment: Action {
    Layout.fillWidth: true
    Layout.preferredWidth: 1
    horizontalPadding: Style.space(4)
    verticalPadding: Style.space(7)
    bordered: true
    enabled: !view.recorder.locked
  }

  component Language: Segment {
    required property string choice
    objectName: "language-" + choice
    enabled: view.recorder.canSetPreferences
    selected: (view.stateData.language || "system") === choice
    text: view.words.t("language." + choice)
    onClicked: if (!selected) view.recorder.run(["set-language", choice])
  }

  component Device: Segment {
    required property string choice
    objectName: "device-" + choice
    enabled: view.recorder.canSetPreferences
    selected: (view.stateData.device || "auto") === choice
    text: view.words.t("device." + choice)
    onClicked: if (!selected) view.recorder.run(["set-device", choice])
  }

  component Rate: Segment {
    required property int rate
    objectName: "rate-" + rate
    enabled: !view.recorder.locked && view.models.some(item => item.rate === rate)
    selected: view.chosen?.rate === rate
    text: view.rateText(rate)
    onClicked: view.selectRate(rate)
  }

  component Variant: Segment {
    required property string label
    readonly property var info: view.models.find(item => item.rate === view.chosen?.rate && item.label === label) || null
    objectName: "variant-" + label
    visible: info !== null
    selected: info !== null && info.id === view.stateData.model
    text: label
    onClicked: view.selectModel(info)
  }
  component Strength: Segment {
    required property int value
    selected: Number(view.stateData.strength || 12) === value
    text: view.words.t("strength." + value)
    onClicked: if (!selected) view.recorder.run(["set-strength", String(value)])
  }
}
