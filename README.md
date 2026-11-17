# Codex 额度猫

为 **ChatGPT Plus 用户**开发的 Windows 桌宠，用气泡显示 Codex 常规五小时额度和本窗周预算中较紧的一项，每 30 秒刷新。周余额偏多时，猫猫会提醒。

## 启动

1. 在 Windows 10 或 11 上安装 Python 3（含 Tkinter），安装时勾选将 Python 添加到 PATH，或安装 Python 的 Windows 启动器。
2. 安装 ChatGPT 桌面版并登录 Plus 账号，或安装 Codex CLI 并用同一个账号登录。账号需提供五小时与每周两项 Codex 额度。
3. 从 GitHub 选择 **Code → Download ZIP**，解压到普通文件夹，双击 **Launch-CodexQuota.cmd**。
4. 如果安装的是 Microsoft Store 版 ChatGPT，也可以双击 **Launch-ChatGPT-and-Quota.cmd** 一起启动。

鼠标移到猫猫或气泡上可以看详情。按住左键拖动。右键菜单可刷新、切换置顶、关闭提示音和退出。

## 怎么算

- 常规五小时余量和本周总余额来自 Codex App Server 的只读接口。
- 每个五小时窗口开始时，把当时的周余额按距周重置的剩余窗口数平摊。本窗分到的预算记为 **100%**。只有额度消耗会扣减该百分比，时间流逝不会。
- 本窗预算可以用到 0%，它是自行设定的使用节奏，周总余额和常规五小时余量仍由 Codex 决定。
- 如果中途打开，程序会尝试用本地用量样本估算窗初余额。周余额偏多提醒也依赖样本估算，样本不足时暂不显示。
- 若当前窗口余额全部用完后，后续窗口需要平均用到 70% 或以上，或者后续全部用满预计仍有余额，猫猫会提醒。

## 数据和故障排查

程序在本机调用已登录的 Codex，并读取近期本地会话日志里的额度字段来估算换算比例。额度猫没有遥测功能，不上传日志、对话或用量记录。Codex 官方接口需要网络连接。

如果桌宠一直提示等更新，可以在解压的目录运行 `py -3 CodexQuota.pyw --check`，检查两项额度和估算是否可用。此命令会显示本账号的额度，贴出诊断结果前可以去掉额度数字。

这是独立项目，与 OpenAI 无隶属关系。额度来源见 [Codex App Server](https://learn.chatgpt.com/docs/app-server#6-rate-limits-chatgpt)。
