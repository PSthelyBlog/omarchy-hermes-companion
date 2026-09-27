import QtQuick
import Quickshell
import Quickshell.Io
import qs.Ui
import qs.Commons

BarWidget {
  id: root
  moduleName: "hermes.companion"

  readonly property string home: Quickshell.env("HOME")
  readonly property string statePath: (Quickshell.env("XDG_STATE_HOME") !== "" ? Quickshell.env("XDG_STATE_HOME") : home + "/.local/state") + "/hermes-companion/state.json"
  readonly property string pluginDir: home + "/.config/omarchy/plugins/hermes.companion"
  readonly property string daemon: pluginDir + "/daemon/companion.py"
  property string hermesDir: home + "/.hermes/hermes-agent"
  readonly property string python: hermesDir + "/venv/bin/python"
  property bool hermesMissing: false
  FileView {
    path: root.pluginDir + "/companion.json"
    watchChanges: true
    printErrors: false
    onFileChanged: reload()
    onLoaded: { try { var c = JSON.parse(String(text() || "")); if (c.hermes_dir) root.hermesDir = c.hermes_dir } catch (e) {} ; hermesProbe.reload() }
  }
  FileView {
    id: hermesProbe
    path: root.python
    printErrors: false
    onLoaded: root.hermesMissing = false
    onLoadFailed: root.hermesMissing = true
  }

  property var st: ({})
  property double nowMs: Date.now()
  readonly property bool alive: !!st.updated && (nowMs / 1000 - st.updated) < 90
  readonly property string status: hermesMissing ? "no-hermes" : (alive ? String(st.status || "watching") : "offline")
  readonly property bool eyes: !!st.eyes
  readonly property bool muted: !!st.muted
  readonly property bool thinking: st.thinking !== false
  readonly property bool toasts: st.toasts !== false
  property bool actions: st.actions === true
  readonly property var remarks: st.remarks || []
  readonly property var visionCfg: st.vision || ({})
  readonly property var reasoningCfg: st.reasoning || ({})
  readonly property var modelRows: st.models || []

  property bool popupOpen: false
  function close() { popupOpen = false }

  readonly property string glyph: {
    switch (status) {
      case "offline": return "󰚌"
      case "no-hermes": return "󰀦"
      case "error": return "󰀦"
      case "thinking": return "󰔟"
      case "speaking": return "󰔊"
      case "listening-request": return "󰍬"
      case "approval": return "󰆍"
      case "paused": return "󰈉"
      default: return eyes ? "󰛐" : "󰈉"
    }
  }
  readonly property color glyphColor: {
    if (status === "offline") return Qt.darker(bar.barForeground, 2.0)
    if (status === "error" || status === "no-hermes") return bar.urgent
    if (status === "approval") return Color.urgent
    if (status === "thinking" || status === "speaking" || status === "listening-request") return Color.accent
    return bar.barForeground
  }

  implicitWidth: glyphText.implicitWidth + Style.space(14)
  implicitHeight: barSize

  FileView {
    path: root.statePath
    watchChanges: true
    printErrors: false
    onFileChanged: reload()
    onLoaded: { try { root.st = JSON.parse(String(text() || "")) } catch (e) { root.st = ({}) } }
    onLoadFailed: root.st = ({})
  }
  Timer { interval: 15000; running: true; repeat: true; onTriggered: root.nowMs = Date.now() }

  Process { id: ctl }
  function control(cmd) {
    ctl.command = [root.python, root.daemon, "--ctl", cmd]
    ctl.running = true
  }

  // "sending" follows the daemon: set on submit, cleared when it leaves thinking/speaking.
  property bool sending: false
  onStatusChanged: if (sending && status !== "thinking" && status !== "speaking") sending = false
  Timer { id: sendGuard; interval: 90000; onTriggered: root.sending = false }
  Process { id: askProc }
  function sendText() {
    var t = askField.text.trim()
    if (t === "" || root.sending) return
    root.sending = true
    sendGuard.restart()
    askField.text = ""
    askProc.command = [root.python, root.daemon, "--ctl", "text " + t]
    askProc.running = true
  }
  Process { id: svc }
  function service(op) {
    svc.command = ["systemctl", "--user", op, "hermes-companion.service"]
    svc.running = true
  }

  Text {
    id: glyphText
    anchors.centerIn: parent
    textFormat: Text.PlainText
    text: root.glyph
    color: root.glyphColor
    font.family: root.bar.fontFamily
    font.pixelSize: Style.font.icon
    Behavior on color { ColorAnimation { duration: 160 } }
    SequentialAnimation on opacity {
      running: root.status === "thinking" || root.status === "listening-request"
      loops: Animation.Infinite
      NumberAnimation { to: 0.35; duration: 500 }
      NumberAnimation { to: 1.0; duration: 500 }
      onRunningChanged: if (!running) glyphText.opacity = 1.0
    }
  }

  MouseArea {
    anchors.fill: parent
    hoverEnabled: true
    cursorShape: Qt.PointingHandCursor
    acceptedButtons: Qt.LeftButton | Qt.MiddleButton | Qt.RightButton
    onClicked: function(mouse) {
      if (mouse.button === Qt.MiddleButton) root.control("hush")
      else if (mouse.button === Qt.RightButton) root.control("listen")
      else root.popupOpen = !root.popupOpen
    }
    onEntered: if (root.bar) root.bar.showTooltip(root, root.hermesMissing ? "Hermes Agent not found at " + root.hermesDir + " — run install.sh" : "Hermes: " + root.status + (root.st.last_observation ? " — " + root.st.last_observation : "") + "\n(right-click to ask, middle-click to hush)")
    onExited: if (root.bar) root.bar.hideTooltip(root)
  }

  // Inline component: a titled, provider-grouped scrollable model list with
  // an effort ButtonGroup + Thinking switch underneath.
  component ModelPicker: Column {
    id: picker
    property string title: ""
    property string role: "vision"
    property bool visionOnly: false
    property bool allowSame: false
    property string current: ""
    property string effort: "low"
    property bool thinking: true
    property bool showControls: true
    spacing: Style.space(6)

    readonly property var rows: {
      var out = []
      if (allowSame) out.push({ value: "", label: "Same as vision model", header: false, vision: true, same: true })
      var src = root.modelRows
      var pendingHeader = null
      for (var i = 0; i < src.length; i++) {
        var r = src[i]
        if (r.header) { pendingHeader = r; continue }
        if (visionOnly && r.vision !== true) continue
        if (pendingHeader) { out.push(pendingHeader); pendingHeader = null }
        out.push(r)
      }
      return out
    }

    Text {
      textFormat: Text.PlainText
      text: picker.title
      color: Qt.darker(root.bar.foreground, 1.4)
      font.family: root.bar.fontFamily
      font.pixelSize: Style.font.caption
      font.bold: true
    }
    BorderSurface {
      width: parent.width
      height: Style.space(140)
      radius: Style.cornerRadius
      color: "transparent"
      borderSpec: Border.controlSpec("normal", root.bar.foreground, Color.accent)

      ListView {
        id: list
        anchors.fill: parent
        anchors.margins: Style.space(3)
        clip: true
        spacing: 1
        boundsBehavior: Flickable.StopAtBounds
        model: picker.rows
        currentIndex: {
          var m = picker.rows
          for (var i = 0; i < m.length; i++) if (!m[i].header && m[i].value === picker.current) return i
          return -1
        }
        Component.onCompleted: Qt.callLater(function() { list.positionViewAtIndex(Math.max(0, list.currentIndex), ListView.Contain) })
        onModelChanged: Qt.callLater(function() { list.positionViewAtIndex(Math.max(0, list.currentIndex), ListView.Contain) })
        delegate: Rectangle {
          required property var modelData
          required property int index
          width: list.width
          height: modelData.header ? Style.spacing.popupRowHeight * 0.9 : Style.spacing.popupRowHeight
          radius: Style.cornerRadius / 2
          readonly property bool isHeader: !!modelData.header
          readonly property bool isCurrent: !isHeader && modelData.value === picker.current
          color: isHeader ? "transparent"
               : isCurrent ? Style.selectedFillFor(root.bar.foreground, Color.accent)
               : (rowHover.hovered ? Style.hoverFillFor(root.bar.foreground, Color.accent) : "transparent")
          HoverHandler { id: rowHover; enabled: !parent.isHeader }
          Text {
            textFormat: Text.PlainText
            anchors.left: parent.left
            anchors.right: parent.right
            anchors.verticalCenter: parent.verticalCenter
            anchors.leftMargin: parent.isHeader ? Style.spacing.controlPaddingX / 2 : Style.spacing.controlPaddingX * 1.6
            anchors.rightMargin: Style.spacing.controlPaddingX
            text: parent.isHeader
              ? ("▸ " + modelData.label + (modelData.error ? "  (" + modelData.error + ")" : ""))
              : ((parent.isCurrent ? "󰄬 " : "") + modelData.label + (modelData.vision === null || modelData.vision === undefined ? "  ·  vision?" : (modelData.vision === false && !modelData.same ? "  ·  text-only" : "")))
            color: parent.isHeader ? Qt.darker(root.bar.foreground, 1.4) : (parent.isCurrent ? Color.accent : root.bar.foreground)
            font.family: root.bar.fontFamily
            font.pixelSize: parent.isHeader ? Style.font.caption : Style.font.bodySmall
            font.bold: parent.isHeader
            elide: Text.ElideRight
          }
          MouseArea {
            anchors.fill: parent
            enabled: !parent.isHeader
            cursorShape: Qt.PointingHandCursor
            onClicked: if (modelData.value !== picker.current) root.control("set-" + picker.role + " " + (modelData.value === "" ? "same" : modelData.value))
          }
        }
      }
    }
    Row {
      visible: picker.showControls
      width: parent.width
      spacing: Style.space(8)
      Text {
        textFormat: Text.PlainText
        text: "Effort"
        anchors.verticalCenter: parent.verticalCenter
        color: Qt.darker(root.bar.foreground, 1.4)
        font.family: root.bar.fontFamily
        font.pixelSize: Style.font.caption
        font.bold: true
      }
      ButtonGroup {
        anchors.verticalCenter: parent.verticalCenter
        foreground: root.bar.foreground
        fontFamily: root.bar.fontFamily
        fontSize: Style.font.bodySmall
        options: ["low", "medium", "high"]
        value: picker.effort
        enabled: picker.thinking
        opacity: picker.thinking ? 1.0 : 0.4
        onChanged: function(v) { if (v && v !== picker.effort) root.control("set-" + picker.role + "-effort " + v) }
      }
      Item { width: Style.space(6); height: 1 }
      ToggleSwitch {
        anchors.verticalCenter: parent.verticalCenter
        checked: picker.thinking
        foreground: root.bar.foreground
        onToggled: root.control("toggle-" + picker.role + "-thinking")
      }
      Text {
        textFormat: Text.PlainText
        text: "Thinking"
        anchors.verticalCenter: parent.verticalCenter
        color: root.bar.foreground
        font.family: root.bar.fontFamily
        font.pixelSize: Style.font.bodySmall
        MouseArea { anchors.fill: parent; cursorShape: Qt.PointingHandCursor; onClicked: root.control("toggle-" + picker.role + "-thinking") }
      }
    }
  }

  // KeyboardPanel (not PopupCard): the popup needs keyboard focus for the text field.
  KeyboardPanel {
    id: popup
    anchorItem: root
    bar: root.bar
    owner: root
    open: root.popupOpen
    focusTarget: askField
    contentWidth: popup.fittedContentWidth(Style.space(440))
    contentHeight: popup.fittedContentHeight(column.implicitHeight)

    Column {
      id: column
      anchors.fill: parent
      spacing: Style.space(8)

      Row {
        width: parent.width
        spacing: Style.space(8)
        Text {
          textFormat: Text.PlainText
          text: root.glyph
          color: root.bar.foreground
          font.family: root.bar.fontFamily
          font.pixelSize: Style.font.displayLarge
          anchors.verticalCenter: parent.verticalCenter
        }
        Column {
          width: parent.width - Style.space(40)
          Text {
            textFormat: Text.PlainText
            text: "Hermes Companion"
            color: root.bar.foreground
            font.family: root.bar.fontFamily
            font.pixelSize: Style.font.subtitle
            font.bold: true
          }
          Text {
            textFormat: Text.PlainText
            text: root.status + (root.st.ticks ? "  ·  " + root.st.ticks + " ticks, " + (root.st.frames_sent || 0) + " frames" : "")
            color: Qt.darker(root.bar.foreground, 1.4)
            font.family: root.bar.fontFamily
            font.pixelSize: Style.font.caption
          }
        }
      }

      Text {
        textFormat: Text.PlainText
        width: parent.width
        wrapMode: Text.Wrap
        text: root.st.last_observation || "(no observation yet)"
        color: Qt.darker(root.bar.foreground, 1.2)
        font.family: root.bar.fontFamily
        font.pixelSize: Style.font.bodySmall
        font.italic: true
      }


      Row {
        spacing: Style.space(6)
        Button { text: root.eyes ? "󰛐 Eyes on" : "󰈉 Eyes off"; foreground: root.bar.foreground; selected: root.eyes; onClicked: root.control("toggle-eyes") }
        Button { text: root.muted ? "󰖁 Quiet" : "󰕾 Talks"; foreground: root.bar.foreground; selected: !root.muted; onClicked: root.control("toggle-mute") }
        Button { text: root.toasts ? "󰍡 Toasts" : "󰍥 Toasts"; foreground: root.bar.foreground; selected: root.toasts; tooltipText: "On-screen output toasts"; onClicked: root.control("toggle-toasts") }
        Button { text: "󰆍 Actions"; foreground: root.bar.foreground; selected: root.actions; tooltipText: "Let requests run commands via a helper subagent (risky ones ask first)"; onClicked: root.control("toggle-actions") }
      }
      Row {
        spacing: Style.space(6)
        Button { text: "󰍬 Listen"; foreground: root.bar.foreground; tooltipText: "Ask Hermes by voice (also: right-click the icon)"; onClicked: root.control("listen") }
        Button { text: "Hush"; foreground: root.bar.foreground; onClicked: root.control("hush") }
        Button { text: "Look now"; foreground: root.bar.foreground; onClicked: root.control("tick") }
        Button { text: root.alive ? "Restart" : "Start"; foreground: root.bar.foreground; onClicked: root.service(root.alive ? "restart" : "start") }
        Button { text: "Stop"; foreground: root.bar.foreground; visible: root.alive; onClicked: root.service("stop") }
      }

      Text {
        textFormat: Text.PlainText
        visible: root.hermesMissing
        width: parent.width
        wrapMode: Text.Wrap
        text: "⚠ Hermes Agent not found at " + root.hermesDir + ". Run " + root.pluginDir + "/install.sh"
        color: root.bar.urgent
        font.family: root.bar.fontFamily
        font.pixelSize: Style.font.caption
      }
      Text {
        textFormat: Text.PlainText
        visible: !!root.st.last_error && !root.hermesMissing
        width: parent.width
        wrapMode: Text.Wrap
        text: "⚠ " + root.st.last_error
        color: root.bar.urgent
        font.family: root.bar.fontFamily
        font.pixelSize: Style.font.caption
      }

      Rectangle { width: parent.width; height: 1; color: Qt.darker(root.bar.foreground, 3) }

      // ---- Model section: vision + reasoning selectors, each with effort/thinking ----
      ModelPicker {
        width: parent.width
        title: "Vision model"
        role: "vision"
        visionOnly: true
        current: root.visionCfg.model || ""
        effort: root.visionCfg.effort || "low"
        thinking: root.visionCfg.thinking !== false
        showControls: true
      }
      ModelPicker {
        width: parent.width
        title: "Reasoning model"
        role: "reasoning"
        visionOnly: false
        allowSame: true
        current: root.reasoningCfg.model || ""
        effort: root.reasoningCfg.effort || "low"
        thinking: root.reasoningCfg.thinking !== false
        showControls: (root.reasoningCfg.model || "") !== ""
      }

      Rectangle { width: parent.width; height: 1; color: Qt.darker(root.bar.foreground, 3) }

      // ---- Text request ----
      Row {
        width: parent.width
        spacing: Style.space(6)
        TextField {
          id: askField
          width: parent.width - askBtn.width - Style.space(6)
          placeholderText: "Ask Hermes… (Enter to send)"
          font.family: root.bar.fontFamily
          font.pixelSize: Style.font.body
          foreground: root.bar.foreground
          enabled: root.alive && !root.sending
          onAccepted: root.sendText()
        }
        Button {
          id: askBtn
          anchors.verticalCenter: parent.verticalCenter
          iconText: root.sending ? "󰔟" : "󰒊"
          iconSpinning: root.sending
          foreground: root.bar.foreground
          enabled: root.alive && !root.sending && askField.text.trim() !== ""
          opacity: enabled ? 1.0 : 0.4
          tooltipText: "Send"
          onClicked: root.sendText()
        }
      }

      Rectangle { width: parent.width; height: 1; color: Qt.darker(root.bar.foreground, 3) }

      // ---- Recent remarks: scrollable, fixed height ----
      // Forget sits here, not in the button rows (no room there): it also empties this list.
      Item {
        width: parent.width
        height: Math.max(recentLabel.implicitHeight, forgetBtn.implicitHeight)
        Text {
          id: recentLabel
          anchors.left: parent.left
          anchors.verticalCenter: parent.verticalCenter
          textFormat: Text.PlainText
          text: "Recent"
          color: Qt.darker(root.bar.foreground, 1.4)
          font.family: root.bar.fontFamily
          font.pixelSize: Style.font.caption
          font.bold: true
        }
        Button {
          id: forgetBtn
          anchors.right: parent.right
          anchors.verticalCenter: parent.verticalCenter
          text: "Forget"
          foreground: root.bar.foreground
          tooltipText: "Forget the conversation and everything seen on screen"
          onClicked: root.control("forget")
        }
      }
      Flickable {
        id: remarksFlick
        width: parent.width
        height: Math.min(Style.space(160), remarksCol.implicitHeight)
        contentHeight: remarksCol.implicitHeight
        clip: true
        boundsBehavior: Flickable.StopAtBounds
        Column {
          id: remarksCol
          width: remarksFlick.width
          spacing: Style.space(6)
          Repeater {
            model: root.remarks
            delegate: Text {
              textFormat: Text.PlainText
              width: remarksCol.width
              wrapMode: Text.Wrap
              text: Qt.formatTime(new Date(modelData.ts * 1000), "HH:mm") + "  " + modelData.text
              color: modelData.urgency === "urgent" ? root.bar.urgent : root.bar.foreground
              font.family: root.bar.fontFamily
              font.pixelSize: Style.font.bodySmall
            }
          }
          Text {
            textFormat: Text.PlainText
            visible: root.remarks.length === 0
            text: "Nothing said yet."
            color: Qt.darker(root.bar.foreground, 1.6)
            font.family: root.bar.fontFamily
            font.pixelSize: Style.font.bodySmall
          }
        }
      }
    }
  }
}




