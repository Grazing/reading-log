//go:build windows

package main

import (
	"io"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"syscall"
	"time"
	"unsafe"

	"github.com/jchv/go-webview2"
)

const appURL = "http://127.0.0.1:18766/"

func init() {
	// Without this, Windows stretches a 96 DPI window and the whole UI looks blurry.
	user32 := syscall.NewLazyDLL("user32.dll")
	setCtx := user32.NewProc("SetProcessDpiAwarenessContext")
	// DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = (HANDLE)-4
	r, _, _ := setCtx.Call(^uintptr(3))
	if r == 0 {
		syscall.NewLazyDLL("user32.dll").NewProc("SetProcessDPIAware").Call()
	}
}

func main() {
	root, err := installRoot()
	if err != nil {
		alert("书历", "找不到安装目录："+err.Error())
		return
	}
	if len(os.Args) > 1 && (os.Args[1] == "--quit" || os.Args[1] == "/quit") {
		quit(root)
		return
	}
	if err := seedIfNeeded(root); err != nil {
		alert("书历", "复制测试数据失败："+err.Error())
		return
	}
	started := false
	if alive() && !winChromeReady() {
		quit(root)
		deadline := time.Now().Add(3 * time.Second)
		for time.Now().Before(deadline) && alive() {
			time.Sleep(150 * time.Millisecond)
		}
	}
	if !alive() {
		if err := startServer(root); err != nil {
			alert("书历", "启动失败："+err.Error()+"\n\n详见 "+logPath())
			return
		}
		if !waitAlive(20 * time.Second) {
			alert("书历", "服务没有在 20 秒内起来。\n\n详见 "+logPath())
			return
		}
		started = true
	}
	width, height := windowSize()
	w := webview2.NewWithOptions(webview2.WebViewOptions{
		Debug:     false,
		AutoFocus: true,
		DataPath:  filepath.Join(dataDir(), "webview2"),
		WindowOptions: webview2.WindowOptions{
			Title:  "书历",
			Width:  width,
			Height: height,
			Center: true,
		},
	})
	if w == nil {
		if started {
			killPidFile(pidPath())
			_ = os.Remove(pidPath())
		}
		alert("书历", "无法打开书历窗口。请先安装 Microsoft Edge WebView2 运行时后再试。")
		return
	}
	defer w.Destroy()
	w.Navigate(appURL)
	w.Run()
	if started {
		killPidFile(pidPath())
		_ = os.Remove(pidPath())
	}
}

func windowSize() (uint, uint) {
	user32 := syscall.NewLazyDLL("user32.dll")
	dpi, _, _ := user32.NewProc("GetDpiForSystem").Call()
	scale := 1.0
	if dpi >= 96 {
		scale = float64(dpi) / 96
	}
	width := uint(1280 * scale)
	height := uint(800 * scale)
	cx, _, _ := user32.NewProc("GetSystemMetrics").Call(0)
	cy, _, _ := user32.NewProc("GetSystemMetrics").Call(1)
	if cx > 120 && width > uint(cx)-80 {
		width = uint(cx) - 80
	}
	if cy > 120 && height > uint(cy)-80 {
		height = uint(cy) - 80
	}
	if width < 960 {
		width = 960
	}
	if height < 640 {
		height = 640
	}
	return width, height
}

func installRoot() (string, error) {
	exe, err := os.Executable()
	if err != nil {
		return "", err
	}
	exe, err = filepath.EvalSymlinks(exe)
	if err != nil {
		return "", err
	}
	return filepath.Dir(exe), nil
}

func alive() bool {
	client := http.Client{Timeout: 800 * time.Millisecond}
	resp, err := client.Get(appURL + "api/status")
	if err != nil {
		return false
	}
	defer resp.Body.Close()
	io.Copy(io.Discard, resp.Body)
	return resp.StatusCode == 200
}

func winChromeReady() bool {
	client := http.Client{Timeout: 800 * time.Millisecond}
	resp, err := client.Get(appURL + "api/win/chrome")
	if err != nil {
		return false
	}
	defer resp.Body.Close()
	body, err := io.ReadAll(io.LimitReader(resp.Body, 4096))
	if err != nil || resp.StatusCode != 200 {
		return false
	}
	// Unknown routes fall through to index.html, which is also HTTP 200.
	return strings.Contains(string(body), `"windows"`)
}

func waitAlive(d time.Duration) bool {
	deadline := time.Now().Add(d)
	for time.Now().Before(deadline) {
		if alive() {
			return true
		}
		time.Sleep(250 * time.Millisecond)
	}
	return false
}

func startServer(root string) error {
	python := filepath.Join(root, "python", "pythonw.exe")
	appDir := filepath.Join(root, "app")
	script := filepath.Join(appDir, "app.py")
	if err := os.MkdirAll(dataDir(), 0o755); err != nil {
		return err
	}
	logf, err := os.OpenFile(logPath(), os.O_CREATE|os.O_APPEND|os.O_WRONLY, 0o644)
	if err != nil {
		return err
	}
	exe, exeErr := os.Executable()
	if exeErr != nil || exe == "" {
		exe = filepath.Join(root, "书历.exe")
	}
	cmd := exec.Command(python, script)
	cmd.Dir = appDir
	cmd.Env = append(os.Environ(),
		"READING_LOG_NO_BROWSER=1",
		"PYTHONUTF8=1",
		"PYTHONIOENCODING=utf-8",
		"READING_LOG_EXE="+exe,
	)
	cmd.Stdout = logf
	cmd.Stderr = logf
	cmd.SysProcAttr = &syscall.SysProcAttr{
		HideWindow:    true,
		CreationFlags: 0x08000000, // CREATE_NO_WINDOW
	}
	if err := cmd.Start(); err != nil {
		logf.Close()
		return err
	}
	pid := cmd.Process.Pid
	_ = os.WriteFile(pidPath(), []byte(strconv.Itoa(pid)), 0o644)
	// Detach: closing the launcher must not stop the server.
	_ = cmd.Process.Release()
	logf.Close()
	return nil
}

func quit(root string) {
	_ = root
	killPidFile(pidPath())
	_ = os.Remove(pidPath())
	self := os.Getpid()
	ps := `$self = ` + strconv.Itoa(self) + `
Get-Process | Where-Object { $_.ProcessName -eq '书历' -and $_.Id -ne $self } | Stop-Process -Force -ErrorAction SilentlyContinue`
	cmd := exec.Command("powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps)
	cmd.SysProcAttr = &syscall.SysProcAttr{HideWindow: true, CreationFlags: 0x08000000}
	_ = cmd.Run()
}

func killPidFile(path string) {
	raw, err := os.ReadFile(path)
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

func seedIfNeeded(root string) error {
	src := filepath.Join(root, "seed-data")
	info, err := os.Stat(src)
	if err != nil || !info.IsDir() {
		return nil
	}
	if _, err := os.Stat(filepath.Join(dataDir(), "reading.db")); err == nil {
		return nil
	}
	return copyTree(src, dataDir())
}

func dataDir() string {
	home, err := os.UserHomeDir()
	if err != nil || home == "" {
		return filepath.Join(os.Getenv("USERPROFILE"), ".reading-log")
	}
	return filepath.Join(home, ".reading-log")
}

func logPath() string { return filepath.Join(dataDir(), "win-server.log") }
func pidPath() string { return filepath.Join(dataDir(), "win.pid") }

func copyTree(src, dst string) error {
	return filepath.Walk(src, func(path string, info os.FileInfo, err error) error {
		if err != nil {
			return err
		}
		rel, err := filepath.Rel(src, path)
		if err != nil {
			return err
		}
		if rel == "." {
			return os.MkdirAll(dst, 0o755)
		}
		target := filepath.Join(dst, rel)
		if info.IsDir() {
			return os.MkdirAll(target, 0o755)
		}
		return copyFile(path, target)
	})
}

func copyFile(src, dst string) error {
	if err := os.MkdirAll(filepath.Dir(dst), 0o755); err != nil {
		return err
	}
	in, err := os.Open(src)
	if err != nil {
		return err
	}
	defer in.Close()
	out, err := os.OpenFile(dst, os.O_CREATE|os.O_TRUNC|os.O_WRONLY, 0o644)
	if err != nil {
		return err
	}
	defer out.Close()
	_, err = io.Copy(out, in)
	return err
}

func alert(title, text string) {
	user32 := syscall.NewLazyDLL("user32.dll")
	proc := user32.NewProc("MessageBoxW")
	t, _ := syscall.UTF16PtrFromString(title)
	m, _ := syscall.UTF16PtrFromString(text)
	proc.Call(0, uintptr(unsafe.Pointer(m)), uintptr(unsafe.Pointer(t)), 0x00000030)
}
