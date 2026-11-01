//go:build windows

package main

import (
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"syscall"
	"unsafe"
)

func main() {
	exe, err := os.Executable()
	if err != nil {
		alert("书历", "找不到安装目录："+err.Error())
		return
	}
	root, _ := filepath.EvalSymlinks(exe)
	root = filepath.Dir(root)
	if !confirm("卸载书历", "卸载程序文件？\n\n阅读数据仍保留在用户目录的 .reading-log 里。") {
		return
	}
	killServer()
	bat := filepath.Join(os.TempDir(), "shuli-uninstall.bat")
	home, _ := os.UserHomeDir()
	desktop := filepath.Join(home, "Desktop")
	start := filepath.Join(os.Getenv("APPDATA"), "Microsoft", "Windows", "Start Menu", "Programs", "书历")
	script := strings.Join([]string{
		"@echo off",
		"timeout /t 1 /nobreak >nul",
		"rmdir /s /q " + quote(root),
		"del /f /q " + quote(filepath.Join(desktop, "书历.lnk")),
		"del /f /q " + quote(filepath.Join(desktop, "退出书历.lnk")),
		"rmdir /s /q " + quote(start),
		`reg delete "HKCU\Software\Microsoft\Windows\CurrentVersion\Uninstall\Shuli" /f >nul 2>&1`,
		`del /f /q "%~f0"`,
	}, "\r\n")
	if err := os.WriteFile(bat, []byte(script), 0o644); err != nil {
		alert("书历", "无法写入卸载脚本："+err.Error())
		return
	}
	cmd := exec.Command("cmd", "/c", "start", "", "/min", bat)
	cmd.SysProcAttr = &syscall.SysProcAttr{HideWindow: true}
	_ = cmd.Start()
}

func killServer() {
	home, err := os.UserHomeDir()
	if err != nil {
		return
	}
	raw, err := os.ReadFile(filepath.Join(home, ".reading-log", "win.pid"))
	if err != nil {
		return
	}
	pid := strings.TrimSpace(string(raw))
	if pid == "" {
		return
	}
	cmd := exec.Command("taskkill", "/PID", pid, "/F", "/T")
	cmd.SysProcAttr = &syscall.SysProcAttr{HideWindow: true, CreationFlags: 0x08000000}
	_ = cmd.Run()
}

func quote(path string) string {
	return `"` + path + `"`
}

func alert(title, text string) {
	msg(title, text, 0x30)
}

func confirm(title, text string) bool {
	return msg(title, text, 0x24) == 6
}

func msg(title, text string, flags uintptr) uintptr {
	user32 := syscall.NewLazyDLL("user32.dll")
	proc := user32.NewProc("MessageBoxW")
	t, _ := syscall.UTF16PtrFromString(title)
	m, _ := syscall.UTF16PtrFromString(text)
	r, _, _ := proc.Call(0, uintptr(unsafe.Pointer(m)), uintptr(unsafe.Pointer(t)), flags)
	return r
}
