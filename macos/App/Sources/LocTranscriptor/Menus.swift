// The main menu, rebuilt when the interface language changes.

import AppCore
import AppKit

@MainActor
enum MainMenu {
    static func build(lang: Lang, target: AppDelegate) -> NSMenu {
        let main = NSMenu()

        let app = submenu(in: main, title: Text.appName.string(lang))
        app.addItem(item(Text.about.string(lang), #selector(AppDelegate.showAbout), target: target))
        app.addItem(.separator())
        app.addItem(item(Text.settings.string(lang), #selector(AppDelegate.openSettings), ",", target: target))
        let login = item(Text.openAtLogin.string(lang), #selector(AppDelegate.toggleLoginItem), target: target)
        login.tag = MenuTag.openAtLogin
        app.addItem(login)
        app.addItem(.separator())
        app.addItem(item(Text.hide.string(lang), #selector(NSApplication.hide(_:)), "h"))
        let others = item(Text.hideOthers.string(lang), #selector(NSApplication.hideOtherApplications(_:)), "h")
        others.keyEquivalentModifierMask = [.command, .option]
        app.addItem(others)
        app.addItem(item(Text.showAll.string(lang), #selector(NSApplication.unhideAllApplications(_:))))
        app.addItem(.separator())
        app.addItem(item(Text.quit.string(lang), #selector(NSApplication.terminate(_:)), "q"))

        // Without the Edit menu, ⌘C/⌘V/⌘A don't work in the page's text fields.
        let edit = submenu(in: main, title: Text.edit.string(lang))
        edit.addItem(item(Text.undo.string(lang), Selector(("undo:")), "z"))
        let redo = item(Text.redo.string(lang), Selector(("redo:")), "z")
        redo.keyEquivalentModifierMask = [.command, .shift]
        edit.addItem(redo)
        edit.addItem(.separator())
        edit.addItem(item(Text.cut.string(lang), #selector(NSText.cut(_:)), "x"))
        edit.addItem(item(Text.copy.string(lang), #selector(NSText.copy(_:)), "c"))
        edit.addItem(item(Text.paste.string(lang), #selector(NSText.paste(_:)), "v"))
        edit.addItem(item(Text.delete.string(lang), #selector(NSText.delete(_:))))
        edit.addItem(item(Text.selectAll.string(lang), #selector(NSText.selectAll(_:)), "a"))

        let view = submenu(in: main, title: Text.view.string(lang))
        view.addItem(item(Text.reload.string(lang), #selector(AppDelegate.reloadPage), "r", target: target))
        view.addItem(.separator())
        view.addItem(item(Text.actualSize.string(lang), #selector(AppDelegate.actualSize), "0", target: target))
        view.addItem(item(Text.zoomIn.string(lang), #selector(AppDelegate.zoomIn), "+", target: target))
        // ⌘= also zooms in (the + key without Shift), as in Safari.
        let zoomInPlain = item(Text.zoomIn.string(lang), #selector(AppDelegate.zoomIn), "=", target: target)
        zoomInPlain.isHidden = true
        zoomInPlain.allowsKeyEquivalentWhenHidden = true
        view.addItem(zoomInPlain)
        view.addItem(item(Text.zoomOut.string(lang), #selector(AppDelegate.zoomOut), "-", target: target))
        view.addItem(.separator())
        // AppKit retitles it "Exit Full Screen" while the window is in full screen.
        let fullScreen = item(Text.enterFullScreen.string(lang), #selector(NSWindow.toggleFullScreen(_:)), "f")
        fullScreen.keyEquivalentModifierMask = [.control, .command]
        view.addItem(fullScreen)

        let window = submenu(in: main, title: Text.window.string(lang))
        window.addItem(item(Text.minimize.string(lang), #selector(NSWindow.performMiniaturize(_:)), "m"))
        window.addItem(item(Text.zoom.string(lang), #selector(NSWindow.performZoom(_:))))
        window.addItem(item(Text.close.string(lang), #selector(NSWindow.performClose(_:)), "w"))
        window.addItem(.separator())
        window.addItem(item(Text.bringAllToFront.string(lang), #selector(NSApplication.arrangeInFront(_:))))
        NSApp.windowsMenu = window

        let help = submenu(in: main, title: Text.help.string(lang))
        help.addItem(item(Text.showTips.string(lang), #selector(AppDelegate.showTips), target: target))
        help.addItem(.separator())
        help.addItem(item(Text.github.string(lang), #selector(AppDelegate.openGitHub), target: target))
        NSApp.helpMenu = help

        return main
    }

    private static func submenu(in main: NSMenu, title: String) -> NSMenu {
        let holder = NSMenuItem(title: title, action: nil, keyEquivalent: "")
        let menu = NSMenu(title: title)
        holder.submenu = menu
        main.addItem(holder)
        return menu
    }

    private static func item(_ title: String, _ action: Selector, _ key: String = "", target: AnyObject? = nil)
        -> NSMenuItem
    {
        let entry = NSMenuItem(title: title, action: action, keyEquivalent: key)
        entry.target = target
        return entry
    }
}

enum MenuTag {
    static let openAtLogin = 1001
}
