import argparse
import datetime as dt
import json
import math
import os
from pathlib import Path
import queue
import shutil
import statistics
import subprocess
import threading
import time
import tkinter as tk

WINDOW = 5 * 3600
WEEK = 7 * 86400
REFRESH = 30
CODEX_HOME = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))


def quota(bucket, history=False):
    if not bucket or bucket.get("limit_id" if history else "limitId") != "codex":
        return None
    if history and bucket.get("plan_type") != "plus":
        return None
    duration, used, reset = (
        ("window_minutes", "used_percent", "resets_at") if history
        else ("windowDurationMins", "usedPercent", "resetsAt")
    )
    windows = {}
    for key in ("primary", "secondary"):
        item = bucket.get(key) or {}
        percent = item.get(used)
        if isinstance(percent, (int, float)) and 0 <= percent <= 100:
            windows[item.get(duration)] = (item.get(reset), float(percent))
    if 300 in windows and 10080 in windows:
        return (*windows[300], *windows[10080])
    return None


def find_codex():
    executable = shutil.which("codex.exe")
    if executable:
        return executable
    powershell = (
        Path(os.environ.get("SystemRoot", r"C:\Windows"))
        / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    )
    try:
        package = subprocess.run(
            [str(powershell), "-NoProfile", "-NonInteractive", "-Command",
             "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; "
             "Get-AppxPackage -Name OpenAI.Codex | Sort-Object Version -Descending | "
             "Select-Object -ExpandProperty InstallLocation"],
            capture_output=True, encoding="utf-8", errors="replace", timeout=10,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        for location in package.stdout.splitlines():
            if location.strip():
                candidate = Path(location.strip()) / "app" / "resources" / "codex.exe"
                if candidate.is_file():
                    return str(candidate)
    except (OSError, subprocess.TimeoutExpired):
        pass
    raise RuntimeError("找不到 Codex。请确认已安装 Codex 桌面版，或将 codex.exe 加入 PATH。")


def read_quota():
    executable = find_codex()
    replies = queue.Queue()
    with subprocess.Popen(
        [executable, "app-server"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL, text=True, encoding="utf-8", errors="replace",
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    ) as process:
        def read_lines():
            try:
                for line in process.stdout:
                    try:
                        reply = json.loads(line)
                    except ValueError:
                        continue
                    if reply.get("id") in (1, 2):
                        replies.put(reply)
            finally:
                replies.put(None)

        def send(message):
            process.stdin.write(json.dumps(message) + "\n")
            process.stdin.flush()

        def receive():
            try:
                reply = replies.get(timeout=15)
            except queue.Empty:
                raise RuntimeError("额度接口响应超时。") from None
            if reply is None:
                raise RuntimeError("Codex 额度接口未启动，请检查 Codex 安装和登录状态。")
            if "error" in reply:
                raise RuntimeError("Codex 暂时无法读取额度，请检查登录和网络。")
            return reply["result"]

        reader = threading.Thread(target=read_lines, daemon=True)
        reader.start()
        try:
            send({"id": 1, "method": "initialize", "params": {
                "clientInfo": {"name": "codex_quota_widget", "version": "1.0.0"}
            }})
            receive()
            send({"method": "initialized"})
            send({"id": 2, "method": "account/rateLimits/read"})
            result = receive()
            bucket = (result.get("rateLimitsByLimitId") or {}).get("codex") or result.get("rateLimits")
            current = quota(bucket)
            if current is None:
                raise RuntimeError("当前账号未返回主额度的五小时和每周窗口。")
            return current
        finally:
            if process.poll() is None:
                process.kill()
            process.wait()
            reader.join(timeout=1)


def history_points(now):
    """Keep only main Plus rate-limit observations from recent local rollouts."""
    files = []
    for offset in range(14):
        day = dt.datetime.fromtimestamp(now - offset * 86400)
        folder = CODEX_HOME / "sessions" / day.strftime("%Y/%m/%d")
        for path in folder.glob("*.jsonl"):
            try:
                files.append((path.stat().st_mtime, path))
            except OSError:
                continue
    for _, path in sorted(files, reverse=True)[:100]:
        try:
            with path.open("rb") as stream:
                if stream.seek(0, 2) > 4_000_000:
                    stream.seek(-4_000_000, 2)
                    stream.readline()
                else:
                    stream.seek(0)
                for line in stream:
                    if b'"type":"token_count"' in line and b'"rate_limits":{' in line:
                        try:
                            payload = json.loads(line).get("payload", {})
                            point = quota(payload.get("rate_limits"), history=True)
                        except (ValueError, AttributeError):
                            continue
                        if point:
                            yield point
        except OSError:
            continue


def estimate_ratio(points, now):
    groups = {}
    for short_reset, short_used, week_reset, week_used in points:
        if short_reset and week_reset and short_reset > now - 14 * 86400:
            if short_used < 100 and week_used < 100:
                groups.setdefault((short_reset, week_reset), set()).add((short_used, week_used))
    samples = []
    for group in groups.values():
        first, last = min(group), max(group)
        short_delta, week_delta = last[0] - first[0], last[1] - first[1]
        if short_delta >= 8 and week_delta >= 2:
            samples.append((short_delta, week_delta))
    if sum(week for _, week in samples) < 4:
        return None, 0
    median = statistics.median(short / week for short, week in samples)
    samples = [(short, week) for short, week in samples if median / 2 <= short / week <= median * 2]
    return sum(short for short, _ in samples) / sum(week for _, week in samples), len(samples)


def weekly_window_left(current, points, ratio, now):
    short_reset, short_used, week_reset, week_used = current
    if not week_reset or week_reset <= now:
        return None
    if week_used >= 100:
        return 0.0
    if not short_reset:
        return 100.0 if short_used == 0 else None
    if short_reset <= now:
        return None
    window_start = short_reset - WINDOW
    week_start = week_reset - WEEK
    allocation_start = max(window_start, week_start)
    first_short, first_week = min(
        (point[1], point[3]) for point in (*points, current)
        if point[0] == short_reset and point[2] == week_reset
    )
    # When opened mid-window, recover usage before the earliest sample where possible.
    if ratio and window_start >= week_start:
        first_week = max(0, first_week - first_short / ratio)
    windows = max(1, (week_reset - allocation_start) / WINDOW)
    budget = (100 - first_week) / windows
    spent = max(0, week_used - first_week)
    return 100 * max(0, 1 - spent / budget)


def plan(current, ratio, now):
    short_reset, short_used, week_reset, week_used = current
    if not week_reset or week_reset <= now or ratio is None:
        return None
    if not short_reset or short_reset <= now:
        return None
    short_left = 100 - short_used
    weekly_units = (100 - week_used) * ratio / 100
    # Count actual starts before the weekly reset, including a final partial window.
    later_windows = max(0, math.ceil((week_reset - short_reset) / WINDOW))
    later_needed = max(0, weekly_units - short_left / 100)
    return later_needed, later_windows


def amount(units):
    rounded = round(units * 100)
    full, extra = divmod(rounded, 100)
    if rounded == 0:
        return "少于 1% 的一窗" if units > 0 else "0"
    if full == 0:
        return f"一窗的 {extra}%"
    return f"{full} 个完整窗口" + (f" + 一窗的 {extra}%" if extra else "")


def clock_label(timestamp):
    return dt.datetime.fromtimestamp(timestamp).strftime("%m-%d %H:%M") if timestamp else "待开始"


def percentage(value):
    if 0 < value < 0.05:
        return "<0.1%"
    return f"{value:.1f}%"


class Widget:
    TRANSPARENT = "#ff00ff"
    INK = "#44312f"
    CREAM = "#ffebcb"
    WIDTH = 239
    HEIGHT = 78
    TAIL_CURLED = (13, 65, 5, 65, 2, 62, 2, 52, 7, 49, 12, 49,
                   12, 54, 8, 54, 8, 59, 18, 59, 18, 65)
    TAIL_RELAXED = (13, 65, 8, 62, 5, 59, 1, 54, 1, 50, 5, 49,
                    8, 53, 11, 55, 14, 58, 18, 59, 18, 65)

    def __init__(self, root):
        self.root = root
        self.points = set()
        self.current = None
        self.ratio = None
        self.last_sync = None
        self.error = None
        self.result = queue.Queue()
        self.next_refresh = 0
        self.refreshing = False
        self.last_alert = None
        self.beep = tk.BooleanVar(value=True)
        self.pin = tk.BooleanVar(value=True)
        self.drag_origin = None
        self.hover_timer = None
        self.hover_region = None
        self.cat_timer = None
        self.head_offset = 0
        root.title("Codex 额度猫")
        root.overrideredirect(True)
        root.attributes("-topmost", True)
        root.attributes("-transparentcolor", self.TRANSPARENT)
        x = max(0, root.winfo_screenwidth() - self.WIDTH - 18)
        y = max(0, root.winfo_screenheight() - self.HEIGHT - 60)
        root.geometry(f"{self.WIDTH}x{self.HEIGHT}+{x}+{y}")
        self.canvas = tk.Canvas(root, bg=self.TRANSPARENT, highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)
        self.draw_cat()
        self.canvas.bind("<Enter>", self.track_hover)
        self.canvas.bind("<Motion>", self.track_hover)
        self.canvas.bind("<Leave>", self.clear_hover)
        self.canvas.bind("<ButtonPress-1>", self.start_drag)
        self.canvas.bind("<B1-Motion>", self.drag)
        self.canvas.bind("<ButtonRelease-1>", self.finish_drag)
        self.canvas.bind("<Button-3>", self.open_menu)

        self.details = tk.Toplevel(root)
        self.details.withdraw()
        self.details.overrideredirect(True)
        self.details.attributes("-topmost", True)
        self.detail_text = tk.Label(
            self.details, bg="#fff7eb", fg=self.INK, font=("Microsoft YaHei UI", 9),
            justify="left", anchor="w", wraplength=290, padx=12, pady=9,
            highlightthickness=1, highlightbackground="#dac1a3",
        )
        self.detail_text.pack()
        self.menu = tk.Menu(root, tearoff=False)
        self.menu.add_command(label="立即刷新", command=self.refresh)
        self.menu.add_checkbutton(label="置顶", variable=self.pin, command=self.toggle_pin)
        self.menu.add_checkbutton(label="提示音", variable=self.beep)
        self.menu.add_separator()
        self.menu.add_command(label="关闭猫猫", command=root.destroy)
        self.tick()

    def draw_cat(self):
        art = self.canvas
        ink, fur = self.INK, "#f4c078"
        self.tail = art.create_polygon(*self.TAIL_CURLED, fill=fur, outline=ink, width=2)
        art.create_polygon(
            13, 66, 13, 56, 18, 50, 43, 50, 50, 57, 50, 67,
            47, 71, 17, 71, fill=fur, outline=ink, width=2,
        )
        art.create_polygon(
            13, 42, 13, 20, 17, 20, 25, 26, 42, 26, 50, 20, 54, 20,
            54, 42, 58, 47, 58, 55, 52, 61, 18, 61, 11, 55, 11, 47,
            fill=fur, outline=ink, width=2, tags="cat_head",
        )
        art.create_polygon(17, 25, 23, 30, 17, 34, fill="#df8b83", tags="cat_head")
        art.create_polygon(50, 25, 44, 30, 50, 34, fill="#df8b83", tags="cat_head")
        art.create_rectangle(29, 27, 32, 34, fill="#bd7750", width=0, tags="cat_head")
        art.create_rectangle(36, 27, 39, 34, fill="#bd7750", width=0, tags="cat_head")
        art.create_polygon(24, 55, 28, 48, 40, 48, 45, 55, 41, 59, 27, 59,
                           fill=self.CREAM, tags="cat_head")
        for x in (23, 44):
            art.create_rectangle(x, 40, x + 3, 46, fill=ink, width=0, tags="cat_head")
        for x in (16, 47):
            art.create_rectangle(x, 48, x + 7, 51, fill="#e68f85", width=0, tags="cat_head")
        art.create_line(30, 51, 32, 53, 34, 51, 36, 53, 38, 51,
                        fill=ink, width=2, tags="cat_head")
        art.create_line(26, 66, 26, 70, fill=ink, width=2)
        art.create_line(39, 66, 39, 70, fill=ink, width=2)
        self.bubble = art.create_polygon(
            77, 9, 226, 9, 234, 17, 234, 53, 226, 61, 78, 61,
            71, 56, 61, 55, 70, 47, 70, 17,
            fill=self.CREAM, outline=ink, width=2,
        )
        self.speech = art.create_text(
            152, 27, fill=ink, font=("Microsoft YaHei UI", 11, "bold"),
        )
        self.caption = art.create_text(
            152, 46, fill="#774d42", font=("Microsoft YaHei UI", 9),
        )

    def say(self, speech, caption, color, details):
        self.canvas.itemconfigure(self.bubble, fill=color)
        self.canvas.itemconfigure(self.speech, text=speech)
        self.canvas.itemconfigure(self.caption, text=caption)
        self.detail_text.configure(text=details)

    def track_hover(self, event):
        if self.drag_origin is not None:
            return
        region = None
        if 70 <= event.x <= 234 and 9 <= event.y <= 61:
            region = "bubble"
        elif 0 <= event.x < 61 and 14 <= event.y <= 73:
            region = "cat"
        if region == self.hover_region:
            return
        self.clear_hover()
        self.hover_region = region
        if region == "cat":
            self.animate_cat()
        elif region == "bubble":
            self.hover_timer = self.root.after(280, self.show_details)

    def pose_tail(self, relax):
        points = [round(curled + (relaxed - curled) * relax)
                  for curled, relaxed in zip(self.TAIL_CURLED, self.TAIL_RELAXED)]
        self.canvas.coords(self.tail, *points)

    def pose_head(self, offset):
        self.canvas.move("cat_head", offset - self.head_offset, 0)
        self.head_offset = offset

    def animate_cat(self, frame=1):
        frames_per_cycle = 20
        phase = math.tau * frame / frames_per_cycle
        self.pose_tail((1 - math.cos(phase)) / 2)
        self.pose_head(round(3 * math.sin(phase)))
        self.cat_timer = self.root.after(34, self.animate_cat, (frame + 1) % frames_per_cycle)

    def clear_hover(self, _event=None):
        self.hover_region = None
        self.hide_details()
        if self.cat_timer is not None:
            self.root.after_cancel(self.cat_timer)
            self.cat_timer = None
            self.pose_tail(0)
            self.pose_head(0)

    def show_details(self):
        self.hover_timer = None
        self.details.update_idletasks()
        width, height = self.details.winfo_reqwidth(), self.details.winfo_reqheight()
        x = max(0, min(self.root.winfo_x(), self.root.winfo_screenwidth() - width))
        y = self.root.winfo_y() - height - 4
        if y < 0:
            y = self.root.winfo_y() + self.HEIGHT + 4
        self.details.geometry(f"+{x}+{y}")
        self.details.deiconify()
        self.details.lift()

    def hide_details(self, _event=None):
        if self.hover_timer is not None:
            self.root.after_cancel(self.hover_timer)
            self.hover_timer = None
        self.details.withdraw()

    def start_drag(self, event):
        self.clear_hover()
        self.drag_origin = (event.x, event.y)

    def drag(self, event):
        dx, dy = self.drag_origin
        x = max(0, min(event.x_root - dx, self.root.winfo_screenwidth() - self.WIDTH))
        y = max(0, min(event.y_root - dy, self.root.winfo_screenheight() - self.HEIGHT))
        self.root.geometry(f"+{x}+{y}")

    def finish_drag(self, event):
        self.drag_origin = None
        self.track_hover(event)

    def toggle_pin(self):
        self.root.attributes("-topmost", self.pin.get())
        self.details.attributes("-topmost", self.pin.get())

    def open_menu(self, event):
        self.clear_hover()
        try:
            self.menu.tk_popup(event.x_root, event.y_root)
        finally:
            self.menu.grab_release()

    def refresh(self):
        if self.refreshing:
            return
        self.refreshing = True
        load_history = self.last_sync is None

        def fetch():
            try:
                current = read_quota()
                points = set(history_points(time.time())) if load_history else set()
                self.result.put((current, points, None))
            except (OSError, RuntimeError, KeyError, ValueError) as exc:
                message = str(exc) if isinstance(exc, RuntimeError) else "无法读取额度，请检查 Codex 安装和登录状态。"
                self.result.put((None, set(), message))

        threading.Thread(target=fetch, daemon=True).start()

    def tick(self):
        now = time.time()
        try:
            current, points, error = self.result.get_nowait()
        except queue.Empty:
            pass
        else:
            self.error = error
            self.refreshing = False
            self.next_refresh = now + REFRESH
            if current:
                new_window = self.current is None or current[0] != self.current[0]
                self.last_sync = now
                self.current = current
                self.points.update(points)
                self.points.add(current)
                self.points = {point for point in self.points if point[0] and point[0] > now - 14 * 86400}
                if new_window or self.ratio is None:
                    self.ratio, _ = estimate_ratio(self.points, now)
        if now >= self.next_refresh:
            self.refresh()
        self.render(now)
        self.root.after(1000, self.tick)

    def render(self, now):
        if self.current is None:
            self.say(
                "等一下喵…", "暂时读不到额度" if self.error else "猫猫在看额度",
                self.CREAM, self.error or "正在连接本机 Codex…",
            )
            return
        short_reset, short_used, week_reset, week_used = self.current
        short_left, week_left = 100 - short_used, 100 - week_used
        weekly_left = weekly_window_left(self.current, self.points, self.ratio, now)
        text = ""
        headline, value = "五小时剩余 · 等待周额度折算", short_left
        if weekly_left is not None:
            if math.isclose(weekly_left, short_left):
                headline = "两项余量相同"
            elif weekly_left < short_left:
                headline, value = "当前显示周额度分给本窗的预算", weekly_left
            else:
                headline = "当前显示常规五小时额度"
        estimate = plan(self.current, self.ratio, now)
        if estimate:
            needed, windows = estimate
            if needed > windows:
                text = (f"猫猫掰着爪子算过啦！用完这窗和后续 {windows} 窗，"
                        f"周额度还会剩约 {amount(needed - windows)}喵。")
            elif windows and needed / windows >= 0.7:
                text = f"趁这周还有时间，快用起来喵！若先用完这窗，后续还要用约 {amount(needed)}。"
        stale = self.error or now - self.last_sync > 2 * REFRESH
        expired = any(reset and reset <= now for reset in (short_reset, week_reset))
        status = f"上次同步 {dt.datetime.fromtimestamp(self.last_sync):%H:%M:%S} · 每 {REFRESH} 秒刷新"
        if stale or expired:
            self.say("等一下喵…", "猫猫在等更新", self.CREAM,
                     f"{self.error or '正在等待额度更新'}\n{status}")
            return
        weekly_value = percentage(weekly_left) if weekly_left is not None else "待计算"
        details = [
            headline,
            f"常规五小时  {percentage(short_left)}  ·  {clock_label(short_reset)} 重置",
            f"本窗周预算  {weekly_value}",
            f"本周总余额  {percentage(week_left)}  ·  {clock_label(week_reset)} 重置",
        ]
        if text:
            details.extend(("", text))
        details.extend(("", status))
        low = value <= 10
        caption = "抓紧用喵，别浪费！" if text else "快见底了喵！" if low else "喵～"
        color = "#ffd6ae" if low else "#f8dfb9" if text else self.CREAM
        self.say(f"还剩 {percentage(value)}", caption, color, "\n".join(details))
        alert = (low, bool(text))
        if any(alert) and alert != self.last_alert and self.beep.get():
            self.root.bell()
        self.last_alert = alert


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true", help="read live quotas and print diagnostics")
    args = parser.parse_args()
    if args.check:
        now = time.time()
        current = read_quota()
        points = {*history_points(now), current}
        ratio, samples = estimate_ratio(points, now)
        print(json.dumps({
            "five_hour_left": 100 - current[1], "weekly_left": 100 - current[3],
            "weekly_window_left": weekly_window_left(current, points, ratio, now),
            "ratio": ratio, "samples": samples, "projection": plan(current, ratio, now),
        }))
        return
    root = tk.Tk()
    Widget(root)
    root.mainloop()


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        import sys
        if sys.stderr is not None:
            raise
        import ctypes
        ctypes.windll.user32.MessageBoxW(
            None, f"额度猫启动失败。\n\n{exc}", "Codex 额度猫", 0x10,
        )
