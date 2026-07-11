# Panthera + Linker O6：Linux Agent 联调说明

## 目标

Linux Agent 只通过 HTTP 向机械臂控制电脑提交已批准程序。Agent 不直接访问 CAN、USB、WSL 或 O6。

机械臂控制电脑负责 Panthera 从臂、Windows O6 灵巧手、每个到点后的手部握手、速度限幅，以及最终 Reset。

## 123 的确切含义

123 是一条连续批次，不是三个独立任务。它依次执行：

~~~text
A1 -> yuzhuaqu_3 -> A4 -> zhuapai_2 -> A5 -> D2 -> C1 -> open
A1 -> yuzhuaqu_3 -> A4 -> zhuapai_2 -> A5 -> D2 -> C2 -> open
A1 -> yuzhuaqu_3 -> A4 -> zhuapai_2 -> A5 -> D2 -> C3 -> open
最后：平滑回 Reset，电机制动保持，进入 idle 等待
~~~

在 C1 和 C2 完成后不会回 Reset。只有整个 123 批次全部完成后才回一次 Reset。

可按任意顺序、可重复编排 C1 至 C5：

~~~text
123          => C1, C2, C3
531          => C5, C3, C1
C3,C1,C5     => C3, C1, C5
~~~

## 机械臂控制电脑启动

### 终端 1：WSL 常驻调度服务器

~~~bash
conda activate panthera_o6
cd ~/Panthera-HT_Host/panthera_python/lan_orchestrator
./start_o6_agent_dispatcher.sh
~~~

该终端保持运行，不会自动退出。

- 输入 OPENING：执行 B1 至 B8；完成 B8 后回 Reset 并等待。
- 输入 123：连续执行 C1、C2、C3；完成后只回一次 Reset。
- 输入 1 至 5：执行单个 C1 至 C5。
- 输入 status：显示当前状态。
- 按一次 Ctrl+C：取消当前任务、平滑回 Reset、制动并退出。不要连续按 Ctrl+C。

### 终端 2：Windows O6 灵巧手客户端

~~~powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "D:\AI_HKS_BeiJing_Codex\O6_Panthera_Handoff_20260711\O6_Panthera_Handoff\integration\start_o6_hand_client_windows.ps1"
~~~

该终端必须保持运行，并显示等待 Panthera 事件。不要运行其他占用 PCAN_USBBUS1 的 O6 程序。

## 局域网准备

先启动终端 1。随后在机械臂控制电脑上使用管理员 PowerShell：

~~~powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "\\wsl.localhost\Ubuntu-22.04\home\pxy\Panthera-HT_Host\panthera_python\lan_orchestrator\windows\setup_windows_portproxy.ps1"
~~~

输出中的 Windows LAN addresses 之一即 Agent 使用的地址。

~~~text
BASE=http://<WINDOWS_LAN_IP>:5100
TOKEN=d51909351dc444a7a167825488f0e930
~~~

每次 WSL shutdown 或 Windows 重启后，需要重新执行端口转发脚本。令牌只应提供给参与联调的 Agent 电脑。

## Agent HTTP 接口

所有请求均需以下 Header：

~~~text
X-Panthera-Token: d51909351dc444a7a167825488f0e930
~~~

### 查询状态

~~~bash
curl -sS "$BASE/api/status" -H "X-Panthera-Token: $TOKEN"
~~~

只有当 running 为 false 且 phase 为 idle 时，Agent 才能发下一条任务。

### 查询程序库

~~~bash
curl -sS "$BASE/api/programs" -H "X-Panthera-Token: $TOKEN"
~~~

单程序包括 OPENING、B8、C1 至 C5。

### 运行单个 C 点

~~~bash
curl -sS -X POST "$BASE/api/programs/C4/run" \
  -H "X-Panthera-Token: $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{}'
~~~

### 运行批次：Linux Agent 的 123

~~~bash
curl -sS -X POST "$BASE/api/batch/run" \
  -H "X-Panthera-Token: $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"programs":["C1","C2","C3"]}'
~~~

运行 531：

~~~bash
curl -sS -X POST "$BASE/api/batch/run" \
  -H "X-Panthera-Token: $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"programs":["C5","C3","C1"]}'
~~~

HTTP 202 只表示服务器已接收任务，不表示任务完成。随后轮询 status，直到 running 为 false 且 phase 为 idle。

## Python 3 标准库 Agent 示例

保存为 panthera_agent.py 后运行：python3 panthera_agent.py 123。

~~~python
#!/usr/bin/env python3
import json
import sys
from urllib.request import Request, urlopen

BASE = "http://<WINDOWS_LAN_IP>:5100"
TOKEN = "d51909351dc444a7a167825488f0e930"

selection = sys.argv[1] if len(sys.argv) > 1 else "123"
if selection.isdigit():
    programs = [f"C{digit}" for digit in selection]
else:
    programs = [item.strip().upper() for item in selection.split(",")]

allowed = {"C1", "C2", "C3", "C4", "C5"}
if not programs or any(item not in allowed for item in programs):
    raise SystemExit("用法：python3 panthera_agent.py 123 或 C3,C1,C5")

request = Request(
    f"{BASE}/api/batch/run",
    data=json.dumps({"programs": programs}).encode("utf-8"),
    method="POST",
    headers={
        "Content-Type": "application/json",
        "X-Panthera-Token": TOKEN,
    },
)
with urlopen(request, timeout=10) as response:
    print(response.read().decode("utf-8"))
~~~

## 安全约束

- 机械臂运行时不得提交第二个任务；服务器会拒绝并返回 HTTP 409。
- 需要中止时优先使用现场急停。远程 POST /api/stop 会停止当前轨迹并保持当前位置。
- 需要回 Reset 后退出时，由机械臂侧在终端 1 按一次 Ctrl+C，等待 Reset 完成。
- 不要使用 kill -9、强制关闭 WSL、强制关机或连续 Ctrl+C；这些方式无法保证 Reset。
