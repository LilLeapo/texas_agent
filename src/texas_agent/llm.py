"""慢循环 LLM 统一客户端 —— 局域网 Spark 上的 OpenAI 兼容端点 (Ollama)。

设计纪律与 commentator 相同: 超时/断网/任何异常一律静默返 None, 零抛错 ——
拔掉 Spark 整场无感, 下层各自接住(解说降级模板、仲裁跳操作员、审计静默消失)。
三个消费者: 解说员(纯文本)、认牌仲裁员(单张牌裁图)、发牌审计员(顶视帧)。

配置 config/table.yaml 的 llm: 段, base_url 留空即全局禁用:
    llm:
      base_url: ""            # 现场填 Spark 地址, 如 http://<SPARK_IP>:11434/v1
      model: qwen2.5vl:32b    # VLM 兼任文本, 少管一个模型
      timeout_s: 8
"""

from __future__ import annotations

import base64
import json
import urllib.request


class LlmClient:
    def __init__(self, base_url: str, model: str, timeout_s: float = 8.0,
                 text_model: str | None = None):
        self.base_url = base_url.rstrip("/")
        self.model = model                        # 视觉任务(认牌/审计)
        self.text_model = text_model or model     # 纯文本(解说)可换更快的小模型
        self.timeout_s = timeout_s

    def ask_text(self, prompt: str, timeout_s: float | None = None) -> str | None:
        """纯文本问答, 走 text_model。任何失败返 None, 永不抛。"""
        return self.ask(prompt, timeout_s=timeout_s, model=self.text_model)

    def ask(self, prompt: str, image_bgr=None, timeout_s: float | None = None,
            model: str | None = None) -> str | None:
        """文本(可选带单张 BGR 图)问答。任何失败返 None, 永不抛。"""
        try:
            content: object = prompt
            if image_bgr is not None:
                b64 = _encode_jpeg(image_bgr)
                if b64 is None:
                    return None
                content = [
                    {"type": "text", "text": prompt},
                    {"type": "image_url",
                     "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
                ]
            body = json.dumps({
                "model": model or self.model,
                "temperature": 0,
                "max_tokens": 160,   # 三个消费者都是短答(牌面码/一致性/两句解说), 压延迟
                "messages": [{"role": "user", "content": content}],
            }).encode()
            req = urllib.request.Request(
                f"{self.base_url}/chat/completions", data=body,
                headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=timeout_s or self.timeout_s) as resp:
                data = json.load(resp)
            text = data["choices"][0]["message"]["content"]
            return text.strip() or None
        except Exception:
            return None


def _encode_jpeg(image_bgr) -> str | None:
    try:
        import cv2
        h, w = image_bgr.shape[:2]
        if max(h, w) > 1280:  # 整帧(如顶视 1920 宽)降采样, 视觉预填充省一半时间
            k = 1280 / max(h, w)
            image_bgr = cv2.resize(image_bgr, (int(w * k), int(h * k)))
        ok, buf = cv2.imencode(".jpg", image_bgr, [cv2.IMWRITE_JPEG_QUALITY, 90])
        if not ok:
            return None
        return base64.b64encode(buf.tobytes()).decode()
    except Exception:
        return None


def from_config(path: str = "config/table.yaml") -> LlmClient | None:
    """读 llm: 段建客户端; 段缺失/base_url 留空/文件不存在 → None(全局禁用)。"""
    try:
        import yaml
        cfg = yaml.safe_load(open(path, encoding="utf-8")) or {}
    except Exception:
        return None
    llm = cfg.get("llm") or {}
    base_url = (llm.get("base_url") or "").strip()
    if not base_url:
        return None
    return LlmClient(base_url, llm.get("model", "qwen2.5vl:32b"),
                     float(llm.get("timeout_s", 8.0)),
                     text_model=llm.get("text_model"))
