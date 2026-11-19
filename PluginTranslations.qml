import QtQuick
import Quickshell.Io
import qs.Commons as Commons

QtObject {
  id: root
  property string preference: "system"

  // System mode retains every language and regional fallback provided by the shell.
  readonly property Commons.Translations shellStrings: Commons.Translations {
    directory: Qt.resolvedUrl("i18n")
  }
  readonly property FileView russianFile: FileView {
    path: root.preference === "ru" ? root.shellStrings.catalogPath("ru") : ""
    blockLoading: true
    watchChanges: true
    printErrors: false
    onFileChanged: reload()
  }
  readonly property var russian: preference === "ru" ? shellStrings.parseCatalog(russianFile.text()) : ({})

  function t(key, fallback) {
    if (preference !== "en" && preference !== "ru") return shellStrings.t(key, fallback)
    const value = (preference === "ru" ? russian[key] : undefined) || shellStrings.englishStrings[key]
    return typeof value === "string" ? value : (fallback !== undefined ? fallback : key)
  }
}
