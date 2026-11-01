//go:build windows

package main

import (
	"archive/zip"
	"bytes"
	"fmt"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"syscall"
	"time"
	"unsafe"

	_ "embed"
)

//go:embed payload.zip
var payload []byte

func main() {
	local := os.Getenv("LOCALAPPDATA")
	if local == "" {
		home, _ := os.UserHomeDir()
		local = filepath.Join(home, "AppData", "Local")
	}
	dir := filepath.Join(local, "书历")
	if !confirm("安装书历", "将把书历安装为本机软件：\n"+dir+"\n\n如果书历正在运行，安装程序会先关掉它再覆盖文件。阅读记录不会被覆盖。\n\n继续？") {
		return
	}
	stopRunning(dir)
	if err := unzip(dir); err != nil {
		stopRunning(dir)
		if err2 := unzip(dir); err2 != nil {
			alert("书历", "安装失败："+err2.Error()+"\n\n请在任务管理器里结束「书历」和 pythonw 后再试。")
			return
		}
	}
	if err := shortcuts(dir); err != nil {
		alert("书历", "已装好程序，但创建快捷方式失败："+err.Error())
	}
	registerUninstall(dir)
	if confirm("书历", "安装完成。现在打开？") {
		cmd := exec.Command(filepath.Join(dir, "书历.exe"))
		cmd.Dir = dir
		cmd.SysProcAttr = &syscall.SysProcAttr{HideWindow: true}
		_ = cmd.Start()
	}
}

func stopRunning(dir string) {
	home, _ := os.UserHomeDir()
	if home != "" {
		raw, err := os.ReadFile(filepath.Join(home, ".reading-log", "win.pid"))
		if err == nil {
			pid := strings.TrimSpace(string(raw))
			if pid != "" {
				cmd := exec.Command("taskkill", "/PID", pid, "/F", "/T")
				cmd.SysProcAttr = &syscall.SysProcAttr{HideWindow: true, CreationFlags: 0x08000000}
				_ = cmd.Run()
			}
		}
	}
	ps := `$root = $env:INSTDIR
Get-CimInstance Win32_Process | Where-Object {
  ($_.Name -eq '书历.exe') -or
  ($_.ExecutablePath -and $_.ExecutablePath.StartsWith($root, [System.StringComparison]::OrdinalIgnoreCase))
} | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }`
	cmd := exec.Command("powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps)
	cmd.Env = append(os.Environ(), "INSTDIR="+dir)
	cmd.SysProcAttr = &syscall.SysProcAttr{HideWindow: true, CreationFlags: 0x08000000}
	_ = cmd.Run()
	time.Sleep(700 * time.Millisecond)
}

func writeNew(dest string, rc io.Reader) error {
	var last error
	for i := 0; i < 8; i++ {
		out, err := os.OpenFile(dest, os.O_CREATE|os.O_TRUNC|os.O_WRONLY, 0o755)
		if err != nil {
			last = err
			time.Sleep(300 * time.Millisecond)
			continue
		}
		_, err = io.Copy(out, rc)
		out.Close()
		return err
	}
	return last
}

func unzip(dir string) error {
	if err := os.MkdirAll(dir, 0o755); err != nil {
		return err
	}
	r, err := zip.NewReader(bytes.NewReader(payload), int64(len(payload)))
	if err != nil {
		return err
	}
	root := filepath.Clean(dir)
	for _, f := range r.File {
		name := filepath.Clean(filepath.FromSlash(f.Name))
		if name == "." || strings.HasPrefix(name, "..") {
			continue
		}
		dest := filepath.Join(root, name)
		if !strings.HasPrefix(dest, root+string(os.PathSeparator)) && dest != root {
			continue
		}
		if f.FileInfo().IsDir() {
			if err := os.MkdirAll(dest, 0o755); err != nil {
				return err
			}
			continue
		}
		if err := os.MkdirAll(filepath.Dir(dest), 0o755); err != nil {
			return err
		}
		rc, err := f.Open()
		if err != nil {
			return err
		}
		if err := writeNew(dest, rc); err != nil {
			rc.Close()
			return fmt.Errorf("%s: %w", dest, err)
		}
		rc.Close()
	}
	return nil
}

func shortcuts(dir string) error {
	home, err := os.UserHomeDir()
	if err != nil {
		return err
	}
	desktop := filepath.Join(home, "Desktop")
	start := filepath.Join(os.Getenv("APPDATA"), "Microsoft", "Windows", "Start Menu", "Programs", "书历")
	if err := os.MkdirAll(start, 0o755); err != nil {
		return err
	}
	exe := filepath.Join(dir, "书历.exe")
	icon := filepath.Join(dir, "shuli.ico")
	un := filepath.Join(dir, "卸载书历.exe")
	items := []struct{ lnk, target, args string }{
		{filepath.Join(desktop, "书历.lnk"), exe, ""},
		{filepath.Join(start, "书历.lnk"), exe, ""},
		{filepath.Join(start, "退出书历.lnk"), exe, "--quit"},
		{filepath.Join(start, "卸载书历.lnk"), un, ""},
	}
	var b strings.Builder
	b.WriteString("$ws = New-Object -ComObject WScript.Shell\n")
	for i, it := range items {
		b.WriteString(fmt.Sprintf("$s = $ws.CreateShortcut($env:LNK%d)\n", i))
		b.WriteString(fmt.Sprintf("$s.TargetPath = $env:TGT%d\n", i))
		if it.args != "" {
			b.WriteString(fmt.Sprintf("$s.Arguments = '%s'\n", it.args))
		}
		b.WriteString(fmt.Sprintf("$s.WorkingDirectory = $env:WORKDIR\n"))
		b.WriteString(fmt.Sprintf("$s.IconLocation = $env:ICON\n"))
		b.WriteString("$s.Save()\n")
	}
	cmd := exec.Command("powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", b.String())
	cmd.Env = append(os.Environ(), "WORKDIR="+dir, "ICON="+icon)
	for i, it := range items {
		cmd.Env = append(cmd.Env,
			fmt.Sprintf("LNK%d=%s", i, it.lnk),
			fmt.Sprintf("TGT%d=%s", i, it.target),
		)
	}
	cmd.SysProcAttr = &syscall.SysProcAttr{HideWindow: true, CreationFlags: 0x08000000}
	out, err := cmd.CombinedOutput()
	if err != nil {
		return fmt.Errorf("%w: %s", err, strings.TrimSpace(string(out)))
	}
	return nil
}

func registerUninstall(dir string) {
	un := filepath.Join(dir, "卸载书历.exe")
	ps := `$p = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\Shuli'
New-Item -Path $p -Force | Out-Null
Set-ItemProperty -Path $p -Name DisplayName -Value '书历'
Set-ItemProperty -Path $p -Name UninstallString -Value $env:UNINST
Set-ItemProperty -Path $p -Name InstallLocation -Value $env:INSTDIR
Set-ItemProperty -Path $p -Name NoModify -Type DWord -Value 1
Set-ItemProperty -Path $p -Name NoRepair -Type DWord -Value 1
`
	cmd := exec.Command("powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps)
	cmd.Env = append(os.Environ(), "UNINST="+un, "INSTDIR="+dir)
	cmd.SysProcAttr = &syscall.SysProcAttr{HideWindow: true, CreationFlags: 0x08000000}
	_ = cmd.Run()
}

func alert(title, text string) { msg(title, text, 0x30) }

func confirm(title, text string) bool { return msg(title, text, 0x24) == 6 }

func msg(title, text string, flags uintptr) uintptr {
	user32 := syscall.NewLazyDLL("user32.dll")
	proc := user32.NewProc("MessageBoxW")
	t, _ := syscall.UTF16PtrFromString(title)
	m, _ := syscall.UTF16PtrFromString(text)
	r, _, _ := proc.Call(0, uintptr(unsafe.Pointer(m)), uintptr(unsafe.Pointer(t)), flags)
	return r
}
