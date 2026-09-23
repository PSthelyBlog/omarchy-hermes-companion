import QtQuick
import QtQuick.Layouts
import Quickshell
import Quickshell.Io
import Quickshell.Wayland
import Quickshell.Hyprland
import qs.Commons
import qs.Ui

// Service: keeps the companion daemon running while the shell is up and
// renders the companion's spoken output as always-on-top toasts.
Item {
  id: root
  property var shell: null

  readonly property string home: Quickshell.env("HOME")
  readonly property string stateDir: (Quickshell.env("XDG_STATE_HOME") !== "" ? Quickshell.env("XDG_STATE_HOME") : home + "/.local/state") + "/hermes-companion"

  // ---------------------------------------------------------------- daemon
  // Start the unit only when the Hermes runtime the installer recorded is present;
  // otherwise the widget shows "Hermes not found" instead of a restart loop.
  property string hermesDir: home + "/.hermes/hermes-agent"
  Process { id: svc }
  Process { id: decide }
  readonly property string pythonBin: root.hermesDir + "/venv/bin/python"
  function sendDecision(approve) {
    decide.command = [root.pythonBin, root.pluginDir + "/daemon/companion.py", "--ctl", "decide " + (approve ? "yes" : "no")]
    decide.running = true
  }
  FileView {
    path: root.pluginDir + "/companion.json"
    printErrors: false
    onLoaded: { try { var c = JSON.parse(String(text() || "")); if (c.hermes_dir) root.hermesDir = c.hermes_dir } catch (e) {} ; installed.reload() }
    onLoadFailed: installed.reload()
  }
  readonly property string pluginDir: home + "/.config/omarchy/plugins/hermes.companion"
  // Written by install.sh on success. Missing => first enable after `omarchy plugin add`:
  // open the installer in a floating terminal (deps, systemd unit, keybinds).
  FileView {
    id: installed
    path: root.pluginDir + "/.installed"
    printErrors: false
    onLoaded: probe.reload()
    onLoadFailed: {
      if (root.installLaunched) return
      root.installLaunched = true
      console.log("hermes-companion: not installed yet, launching install.sh")
      svc.command = ["omarchy-launch-floating-terminal-with-presentation", root.pluginDir + "/install.sh"]
      svc.running = true
    }
  }
  // Service.qml can be instantiated more than once during enable/rescan; a
  // persisted flag keeps the installer terminal from opening twice.
  PersistentProperties {
    id: persisted
    reloadableId: "hermes-companion-service"
    property bool installLaunched: false
  }
  property alias installLaunched: persisted.installLaunched
  FileView {
    id: probe
    path: root.hermesDir + "/venv/bin/python"
    printErrors: false
    onLoaded: { svc.command = ["systemctl", "--user", "start", "hermes-companion.service"]; svc.running = true }
    onLoadFailed: console.warn("hermes-companion: Hermes not found at", root.hermesDir, "- not starting daemon")
  }

  // ---------------------------------------------------------------- toasts
  property bool toastsEnabled: true
  readonly property int toastLifetime: 12000     // ms visible (pauses on hover)
  readonly property int toastWidth: Style.space(420)
  // Where the toast stack spawns and how far from that corner — set via
  // companion.json's toast_position or `--ctl set-toast-position <anchor>,<mx>,<my>`.
  property string toastAnchor: "bottom-right"
  property int toastMarginX: 0
  property int toastMarginY: 0
  readonly property bool toastAnchorTop: toastAnchor === "top-right" || toastAnchor === "top-left"
  readonly property bool toastAnchorRight: toastAnchor === "top-right" || toastAnchor === "bottom-right"

  FileView {
    path: root.stateDir + "/state.json"
    watchChanges: true
    printErrors: false
    onFileChanged: reload()
    onLoaded: {
      try {
        var s = JSON.parse(String(text() || ""))
        root.toastsEnabled = s.toasts !== false
        var tp = s.toast_position
        if (tp) {
          if (typeof tp.anchor === "string") root.toastAnchor = tp.anchor
          if (typeof tp.margin_x === "number") root.toastMarginX = tp.margin_x
          if (typeof tp.margin_y === "number") root.toastMarginY = tp.margin_y
        }
      } catch (e) {}
    }
  }

  property double lastToastTs: 0

  // FileView with debounce: a rapid burst of toast writes (several sentences
  // spoken back to back) used to trigger one shell reload per write, each
  // costing a couple of seconds of Quickshell reload lag. Debouncing the
  // reload to at most once per second lets several writes land as one
  // reload while keeping delivery latency low.
  Timer {
    id: toastDebounce
    interval: 1000  // debounce to 1 reload per second max
    repeat: false
    onTriggered: toastFile.reload()
  }

  FileView {
    id: toastFile
    path: root.stateDir + "/toast.json"
    watchChanges: true
    printErrors: false
    onFileChanged: toastDebounce.restart()
    onLoaded: root.ingest(text())
  }

  function ingest(content) {
    var t
    try { t = JSON.parse(String(content || "")) } catch (e) { return }
    if (!t || !t.ts || t.ts <= root.lastToastTs) return
    root.lastToastTs = t.ts
    if (!root.toastsEnabled) return
    toastModel.insert(0, { text: String(t.text || ""), kind: String(t.kind || "remark"), note: String(t.note || ""), ts: t.ts })
    while (toastModel.count > 5) toastModel.remove(toastModel.count - 1)
  }

  ListModel { id: toastModel }

  // Only the currently focused monitor gets toasts — a single PanelWindow
  // bound to Hyprland.focusedMonitor, re-resolved whenever focus moves.
  // Previously every screen got its own PanelWindow (Variants over
  // Quickshell.screens), so a toast appeared on every monitor at once.
  readonly property var focusedScreen: {
    var name = Hyprland.focusedMonitor ? String(Hyprland.focusedMonitor.name || "") : ""
    if (!name) return Quickshell.screens.length > 0 ? Quickshell.screens[0] : null
    for (var i = 0; i < Quickshell.screens.length; i++) {
      if (Quickshell.screens[i].name === name) return Quickshell.screens[i]
    }
    return Quickshell.screens.length > 0 ? Quickshell.screens[0] : null
  }

  // Bar clearance: same calculation as the native notifications plugin
  // (Service.qml), so our toasts clear the bar the same way theirs do —
  // only applied when the toast anchor sits on the bar's own edge.
  readonly property string barPosition: shell && shell.barConfig ? String(shell.barConfig.position || "top") : "top"
  readonly property bool barVertical: barPosition === "left" || barPosition === "right"
  readonly property int defaultBarSize: barVertical ? Style.bar.sizeVertical : Style.bar.sizeHorizontal
  readonly property int liveBarSize: shell && shell.bar && !shell.bar.barHidden ? Math.max(0, shell.bar.barSize) : defaultBarSize
  readonly property int barClearance: liveBarSize + Style.gapsOut
  readonly property bool toastNearBar: (barPosition === "top" && toastAnchorTop) || (barPosition === "bottom" && !toastAnchorTop)
      || (barPosition === "left" && !toastAnchorRight) || (barPosition === "right" && toastAnchorRight)

  PanelWindow {
    id: win
    screen: root.focusedScreen
    visible: toastModel.count > 0 && root.focusedScreen !== null
    color: "transparent"
    WlrLayershell.namespace: "hermes-companion-toasts"
    WlrLayershell.layer: WlrLayer.Overlay
    WlrLayershell.keyboardFocus: WlrKeyboardFocus.None
    exclusionMode: ExclusionMode.Ignore
    anchors { top: true; bottom: true; left: true; right: true }
    mask: Region { item: col }

    ColumnLayout {
      id: col
      anchors.right: root.toastAnchorRight ? parent.right : undefined
      anchors.left: root.toastAnchorRight ? undefined : parent.left
      anchors.top: root.toastAnchorTop ? parent.top : undefined
      anchors.bottom: root.toastAnchorTop ? undefined : parent.bottom
      anchors.rightMargin: Style.gapsOut + Style.space(24) + root.toastMarginX
      anchors.leftMargin: Style.gapsOut + Style.space(24) + root.toastMarginX
      anchors.topMargin: (root.toastNearBar && !root.barVertical ? root.barClearance : Style.gapsOut) + Style.space(8) + root.toastMarginY
      anchors.bottomMargin: (root.toastNearBar && !root.barVertical ? root.barClearance : Style.gapsOut) + Style.space(8) + root.toastMarginY
      spacing: Style.space(8)

      // Newest toast is inserted at index 0 (top of the stack); each toast
      // has its own timer so older ones leave last (first-in, last-out).
      Repeater {
        model: toastModel

        delegate: Item {
          id: slot
          required property int index
          required property string text
          required property string kind
          required property string note
          required property double ts
          readonly property bool quiet: kind === "observation"
          readonly property bool approval: kind === "approval"
          readonly property int lifetime: approval ? 31000 : (quiet ? root.toastLifetime * 0.6 : root.toastLifetime)

          Layout.preferredWidth: root.toastWidth
          Layout.alignment: root.toastAnchorRight ? Qt.AlignRight : Qt.AlignLeft
          implicitHeight: card.height

          property real remaining: 1.0
          property bool leaving: false
          readonly property bool ticking: !leaving && !hover.hovered

          Timer {
            interval: 50; repeat: true; running: slot.ticking
            onTriggered: {
              slot.remaining -= 50.0 / slot.lifetime
              if (slot.remaining <= 0) { slot.remaining = 0; slot.dismiss() }
            }
          }

          function dismiss() {
            if (slot.leaving) return
            slot.leaving = true
            leaveAnim.start()
          }

          SequentialAnimation {
            id: leaveAnim
            ParallelAnimation {
              NumberAnimation { target: card; property: "opacity"; to: 0; duration: 350; easing.type: Easing.InQuad }
              NumberAnimation { target: card; property: "x"; to: root.toastAnchorRight ? Style.space(40) : -Style.space(40); duration: 350; easing.type: Easing.InQuad }
            }
            ScriptAction { script: { for (var i = 0; i < toastModel.count; i++) if (toastModel.get(i).ts === slot.ts) { toastModel.remove(i); break } } }
          }

          Rectangle {
            id: card
            width: root.toastWidth
            height: content.implicitHeight + Style.space(28)
            radius: Style.cornerRadius
            color: Util.alpha("#0b0d10", slot.quiet ? 0.72 : 0.86)
            border.width: 1
            border.color: Util.alpha(Color.popups.border, 0.7)
            opacity: 0
            x: root.toastAnchorRight ? Style.space(40) : -Style.space(40)

            Component.onCompleted: enterAnim.start()
            ParallelAnimation {
              id: enterAnim
              NumberAnimation { target: card; property: "opacity"; to: 1; duration: 300; easing.type: Easing.OutQuad }
              NumberAnimation { target: card; property: "x"; to: 0; duration: 300; easing.type: Easing.OutCubic }
            }

            HoverHandler { id: hover }

            Column {
              id: content
              anchors.fill: parent
              anchors.margins: Style.space(14)
              spacing: Style.space(6)

              Row {
                spacing: Style.space(8)
                Text {
                  textFormat: Text.PlainText
                  text: slot.kind === "reply" ? "󰍬" : (slot.kind === "urgent" ? "󰀦" : (slot.kind === "held" ? "󰖁" : (slot.approval ? "󰆍" : (slot.kind === "action" ? "󰑮" : "󰛐"))))
                  color: (slot.kind === "urgent" || slot.approval) ? Color.urgent : (slot.quiet ? Util.alpha("#ffffff", 0.5) : Color.accent)
                  font.family: Style.font.family
                  font.pixelSize: Style.font.body
                }
                Text {
                  // model/screen-derived (slot.note): never interpret markup
                  textFormat: Text.PlainText
                  text: "Hermes" + (slot.kind === "urgent" ? "  ·  urgent" : (slot.approval ? "  ·  approve?  " + slot.note : (slot.kind === "action" ? "  ·  helper" : (slot.kind === "held" ? "  ·  held: " + slot.note : (slot.quiet ? "  ·  observing" : "")))))
                  color: Util.alpha("#ffffff", 0.75)
                  font.family: Style.font.family
                  font.pixelSize: Style.font.caption
                  font.bold: true
                }
                Text {
                  textFormat: Text.PlainText
                  text: Qt.formatTime(new Date(slot.ts * 1000), "HH:mm")
                  color: Util.alpha("#ffffff", 0.45)
                  font.family: Style.font.family
                  font.pixelSize: Style.font.caption
                }
              }

              Text {
                id: body
                width: card.width - Style.space(28)
                wrapMode: Text.Wrap
                // model/screen-derived (slot.text): never interpret markup
                textFormat: Text.PlainText
                text: slot.text
                color: slot.quiet ? Util.alpha("#f4f4f4", 0.7) : "#f4f4f4"
                font.family: Style.font.family
                font.pixelSize: slot.quiet ? Style.font.bodySmall : Style.font.body
                font.italic: slot.quiet
              }

              Row {
                visible: slot.approval
                spacing: Style.space(8)
                Button { text: "󰐊 Run"; foreground: "#f4f4f4"; onClicked: { root.sendDecision(true); slot.dismiss() } }
                Button { text: "󰜺 Skip"; foreground: "#f4f4f4"; onClicked: { root.sendDecision(false); slot.dismiss() } }
                Text {
                  anchors.verticalCenter: parent.verticalCenter
                  textFormat: Text.PlainText
                  text: "or say yes / no"
                  color: Util.alpha("#ffffff", 0.45)
                  font.family: Style.font.family
                  font.pixelSize: Style.font.caption
                }
              }
            }

            // lifetime progress (hidden while hovered)
            Rectangle {
              anchors.left: parent.left; anchors.right: parent.right; anchors.bottom: parent.bottom
              anchors.margins: 1
              height: 2
              color: "transparent"
              Rectangle {
                height: parent.height
                width: parent.width * slot.remaining
                radius: 1
                color: Util.alpha(Color.accent, hover.hovered ? 0.25 : 0.8)
              }
            }

            MouseArea {
              anchors.fill: parent
              z: -1
              acceptedButtons: Qt.LeftButton | Qt.MiddleButton
              onClicked: if (!slot.approval) slot.dismiss()
            }
          }
        }
      }
    }
  }
}
