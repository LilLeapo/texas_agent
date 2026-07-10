# 牌桌智脑 · 无机械臂版

真人荷官发牌，系统担任**全知裁判、导播与解说**：提词荷官下一步该发什么、视觉核验落位、
规则引擎实时推进与结算、精确胜率(上帝视角)、GTO 基线对照、LLM/模板解说，整场可回放。

执行手册见 [docs/执行手册-无机械臂版.md](docs/执行手册-无机械臂版.md)，
冻结消息 schema 见 [docs/schema.md](docs/schema.md)。

## 安装

```bash
python3 -m venv .venv
.venv/bin/pip install setuptools wheel Cython
.venv/bin/pip install --no-build-isolation eval7   # eval7 构建脚本未声明 Cython, 需绕过隔离
.venv/bin/pip install -e ".[vision,dev]"
```

## 快速开始

```bash
export PYTHONPATH=src   # 或 pip install -e . 后省略

# D1-1 验收: 预排剧情牌 + 脚本动作, 非交互跑完一手(转牌反超剧情)
python -m texas_agent.engine_cli --deck config/deck_order.txt \
    --script "c c c k / k r 300 c f c / r 500 c f / r 1000 c"

# 交互模式: 键盘申报动作, 荷官口头报牌
python -m texas_agent.engine_cli

# 事件 hub (独立进程, /pub 8765 /sub 8766, 自动落 sessions/*.jsonl)
python -m texas_agent.bus

# 回放 (演示保险): 终端重播 / 4 倍速 / 重播进 hub 给前端
python tools/replay.py sessions/xxx.jsonl --speed 4 [--ws]

# 生成占位 GTO 图表 (169 手牌 × 11 节点; 正式数据按 D0 转录)
python tools/make_sample_charts.py

# 测试 (40 项: 边池/纠错重放/故障注入/性能门/解说降级/VLM 降级/hub 端到端)
python -m pytest tests/
```

## 在 DGX Spark 上跑本体 (推理+编排一体机)

Spark 同时跑 Ollama(两模型常驻)与本体, VLM 调用走 localhost; 笔记本降级为显示/操作台。
代码同步: `rsync -a --exclude .venv --exclude sessions ./ dgx@<SPARK_IP>:~/texas_agent/`;
Spark 侧安装同上(先 `sudo apt install python3-dev`)。仓库里 `llm.base_url` 按纪律留空,
**每次同步后**在 Spark 上重设为本机:
`sed -i 's|base_url: ""|base_url: "http://localhost:11434/v1"|' config/table.yaml`

Linux 无 `say`, 提词/解说自动降级为纯屏幕; 声音由笔记本出:

```bash
# 笔记本上: 订阅 Spark 的 hub, 播报提词(+解说); 纯订阅者, 挂了不影响主循环
python tools/tts_client.py --host <SPARK_IP> --commentary
```

现场差异: 相机 USB 插 Spark(v4l2), 前端/上帝屏连 Spark 的 8765/8766。

## 模块 ↔ 代码对照

| 手册模块 | 代码 | 状态 |
|---|---|---|
| M1 hub + 引擎 | `src/texas_agent/bus.py` `engine.py` `engine_cli.py` | ✅ 已测 |
| M2 顶视感知 | `src/texas_agent/vision/calib.py` `stability.py` `zones.py` | ⚠️ 待现场硬件验证 |
| M3 读牌匹配器 | `src/texas_agent/vision/matcher.py` `live.py` + `tools/capture_templates.py` | ⚠️ 待现场硬件验证 |
| M4 提词器 | `src/texas_agent/prompter.py` | ✅ 已测(TTS 需 macOS) |
| M5 编排器 | `src/texas_agent/orchestrator.py` | ✅ 已测(含故障注入) |
| M6 分析层 | `src/texas_agent/analysis.py` | ✅ 已测(手算核对) |
| M6 解说 | `src/texas_agent/commentator.py` | ✅ 已测(降级零报错) |
| M7 GTO 查表 | `src/texas_agent/gto.py` + `charts/` | ✅ 已测(数据为占位) |
| 回放 | `tools/replay.py` | ✅ 已测 |
| 标定/区域工具 | `tools/zone_viz.py` | ⚠️ 待现场硬件验证 |
| 慢循环 LLM 客户端 | `src/texas_agent/llm.py` (Spark 上 Ollama, OpenAI 兼容) | ✅ 已测(失败静默) |
| 认牌仲裁员 + 发牌审计员 | `src/texas_agent/vlm.py` + `tools/bench_vlm.py` | ✅ 已测(降级零报错), ⚠️ 待 Spark 实测 |

配置: `config/table.yaml`(模式一键切换) `config/zones.yaml`(区域, 现场校准)
`config/deck_order.txt`(预排剧情牌序, 已校验 52 张不重不漏)。

## 关键约定 (细节见 docs/schema.md)

- 座位号消息里一律 0 基; PokerKit 语义 0=小盲…n-1=庄位, **单挑例外: 0=大盲, 1=庄位先行动**。
- 计算纪律: 分析只以"各家已知底牌+已发公共牌"为条件, 烧牌视为未知, 预排的未来信息绝不进入胜率。
- 识别宁可"不确定"(→操作员两键补录), 不可错判; GTO 查不到的节点静默, 绝不编造。
- 纠错 = 引擎操作日志全量重放 (`Engine.amend`), 审计链带署名+原值+新值。

## 尚未做 (按手册属 D0 采购/现场项或弹性项)

- 现场硬件: 相机架设、桌垫打印、52 张模板采集、阈值调参 (D1-2/D1-3/D2-3)
- GTO 正式图表转录 (现为占位数据, `source: placeholder`)、剧情牌 TexasSolver 离线求解缓存
- 进阶档分析: 牌面质构、加注 EV 曲线、阻断牌、运气表 (核心档已全)
- 语音申报动作 (LLM 三接入口已完成: 解说 `Commentator(llm_fn=...)` / 认牌仲裁 / 发牌审计,
  端点配置见 `config/table.yaml` 的 `llm:` 段, base_url 留空即全部禁用)
