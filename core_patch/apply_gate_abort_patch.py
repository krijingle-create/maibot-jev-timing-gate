"""给 MaiBot 核心打补丁：让 maisaka.planner.before_request 的 abort 真正生效。

为什么需要它
------------
MaiBot（dev / main / neo-mai，截至 2026-09-22）里 `maisaka.planner.before_request` 的
HookSpec 是 `allow_abort=False`，而且消费点只读 `before_request_result.kwargs`、
**从不检查 `.aborted`**。于是插件即使返回 abort，也会被调度器忽略（只记一条 warning），
被判"无需参与"的轮次仍会真的发一次模型请求 —— 实测每次约 3.6 秒延迟、约 250 输出 token，
而且模型为遵守该指令写的推理会留在上下文里污染后续轮次。

本补丁只改两处 `src/maisaka/chat_loop_service.py`：
  1. planner.before_request 的 allow_abort: False -> True
  2. hook 返回后：若 aborted，则不发起模型请求，直接合成"本轮无需参与"结果
     · 输出刻意构造成"纯文本、无工具调用"，使下游走与过去**完全相同**的分支：
       _handle_planner_no_tool_retry → cycle_end.reason="planner_no_tool_end"
       → 空闲退避计数照常 +1（保住 MaiBot 原生的"读空气"节流）

不装也能用：插件会同时返回 abort 与极简改写，核心不支持 abort 时自动降级为"极简改写"。

用法（在 MaiBot 根目录或其上层执行）
------------------------------------
    python apply_gate_abort_patch.py /path/to/MaiBot          # 打补丁
    python apply_gate_abort_patch.py /path/to/MaiBot --revert  # 撤销（用 .bak 恢复）

幂等：已打过会提示 skip。撤销只认本脚本生成的 .bak.<TAG> 备份。
注意：改动核心后 `git status` 会显示该文件为 modified；升级 MaiBot 前建议先 --revert，
      升级后再重新打（`git pull` 遇到已修改文件会冲突）。
"""

import argparse
import os
import shutil
import sys

TAG = "20260922-gate-abort"
REL = os.path.join("src", "maisaka", "chat_loop_service.py")
SPEC_MARK = 'name="maisaka.planner.before_request"'
CONSUMER_ANCHOR = "        before_request_kwargs = before_request_result.kwargs\n"
# 检测标记用子串匹配，兼容带前缀的历史写法
MARKER = "门控中止本轮：已跳过模型请求"

SKIP_BLOCK = '''        # --- 插件门控：hook 允许中止 → 不发起模型请求，直接合成"本轮无需参与"结果 ---
        # 相比"极简改写"，这里一个 token 都不花，也不会把模型"如何遵守指令"的推理写进上下文。
        # 输出刻意构造成"纯文本、无工具调用"，让下游走与过去完全相同的分支：
        # _handle_planner_no_tool_retry → cycle_end.reason="planner_no_tool_end"
        # → 空闲退避计数照常 +1，不影响 MaiBot 原生节流。
        if getattr(before_request_result, "aborted", False):
            _gate_skip_item = (
                ContextItemBuilder()
                .set_role(RoleType.Assistant)
                .add_text_part("（本轮无需参与，已结束。）")
                .build()
            )
            _gate_skip_items: tuple[ContextItem, ...] = (_gate_skip_item,)
            if logical_turn_id:
                _gate_skip_items = bind_output_items_to_turn(_gate_skip_items, logical_turn_id)
            logger.info(
                "门控中止本轮：已跳过模型请求（不消耗 token）"
                f" request_kind={request_kind} session_id={self._session_id}"
            )
            return ChatResponse(
                output_items=tuple(_gate_skip_items),
                request_messages=list(built_messages),
                selected_history_count=len(selected_history),
                tool_count=0,
                prompt_tokens=0,
                built_message_count=len(built_messages),
                completion_tokens=0,
                total_tokens=0,
                model_name="",
                duration_ms=0.0,
                prompt_section=None,
                prompt_html_uri=None,
                generation_attempts=(),
            )
'''


def main():
    ap = argparse.ArgumentParser(description="为 Jev 参与门控打开 planner hook 的 abort 支持")
    ap.add_argument("maibot_root", help="MaiBot 根目录（含 src/ 与 bot.py）")
    ap.add_argument("--revert", action="store_true", help="撤销补丁（从 .bak 恢复）")
    args = ap.parse_args()

    path = os.path.join(args.maibot_root, REL)
    if not os.path.isfile(path):
        sys.exit("找不到目标文件: %s" % path)
    backup = path + ".bak." + TAG

    if args.revert:
        if not os.path.isfile(backup):
            sys.exit("没有找到备份 %s，无法撤销（若已升级 MaiBot，请直接 git checkout 该文件）" % backup)
        shutil.copyfile(backup, path)
        print("已撤销：%s（从 %s 恢复）" % (path, os.path.basename(backup)))
        print("重启 MaiBot 后生效。")
        return

    with open(path, encoding="utf-8") as fh:
        src = fh.read()

    if MARKER in src:
        print("skip: 已打过补丁")
        return
    if CONSUMER_ANCHOR not in src or src.count(CONSUMER_ANCHOR) != 1:
        sys.exit("!! 找不到唯一的消费点锚点，MaiBot 版本可能已变化，需人工处理")
    if SPEC_MARK not in src:
        sys.exit("!! 找不到 planner.before_request 的 HookSpec")

    i = src.index(SPEC_MARK)
    j = src.index("allow_abort=", i)
    line_end = src.index("\n", j)
    if "allow_abort=False" not in src[j:line_end]:
        sys.exit("!! planner spec 的 allow_abort 不是 False：%r" % src[j:line_end])
    src = src[:j] + src[j:line_end].replace("allow_abort=False", "allow_abort=True") + src[line_end:]
    src = src.replace(CONSUMER_ANCHOR, CONSUMER_ANCHOR + SKIP_BLOCK, 1)

    assert src.count("allow_abort=True") == 1, "allow_abort=True 应恰好 1 处"
    assert src.count("allow_abort=False") == 5, "其余 5 个 spec 应保持 False"
    compile(src, path, "exec")           # 语法自检；失败不写盘

    if not os.path.isfile(backup):
        shutil.copyfile(path, backup)
        print("已备份 -> %s" % backup)
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write(src)
    print("补丁已应用：%s" % path)
    print("重启 MaiBot 后生效（核心改动必须完整重启，不热重载）。")
    print("生效后日志会多一行：`门控中止本轮：已跳过模型请求（不消耗 token）`")


if __name__ == "__main__":
    main()
