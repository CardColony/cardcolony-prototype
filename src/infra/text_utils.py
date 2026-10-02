"""文本规范化辅助（N2 提取，供多处共用）。

v0.04 起 ``FileIOController._normalize_newlines`` 只修了文件写入路径；
v0.05 把同样的兜底逻辑提取到本模块，供
``file_io_controller`` / ``dialog_store`` / ``session_manager`` 三处写入路径共用，
避免字面 ``\\n``（反斜杠 + n 两字符）污染落盘内容与后续 dialog_context。
"""

from __future__ import annotations


def normalize_newlines(content: str) -> str:
    """兜底：把字面 ``\\n``（反斜杠 + n 两字符）还原成真换行。

    续写路径 / 部分模型 gateway 响应里，真换行会被错误转义成字面 ``\\n``，
    导致落盘的 markdown 全挤在一行无法渲染。本方法对所有写入的文件正文
    / dialog / info 统一兜底。

    规则：
    - 用占位符隔离 ``\\\\``（字面双反斜杠），避免被误伤；
    - ``\\n`` → 真换行；``\\t`` → 真 Tab；``\\"`` → 引号；``\\/`` → 斜杠；
    - 还原字面双反斜杠；
    - 清理行尾单反斜杠残留（多层转义未完全处理的痕迹）。

    仅用于写入路径（文件 / dialog / info 正文），不作用于 regex_replace 的 pattern。
    """
    if not content or "\\n" not in content:
        return content
    PLACEHOLDER = "\x00BACKSLASH\x00"
    s = content.replace("\\\\", PLACEHOLDER)
    for old, new in (("\\n", "\n"), ("\\t", "\t"), ('\\"', '"'), ("\\/", "/")):
        s = s.replace(old, new)
    s = s.replace(PLACEHOLDER, "\\")
    lines = s.split("\n")
    cleaned = [
        line.rstrip("\\") if line.endswith("\\") and not line.endswith("\\\\") else line
        for line in lines
    ]
    return "\n".join(cleaned)


__all__ = ["normalize_newlines"]
