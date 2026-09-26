#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""batch_apply.py —— 批量操作部分成功执行器（纯标准库，单文件）

输入：
  1) 数据快照文件：JSON 对象（扁平的 键 -> 标量值）
  2) 操作列表文件：每行一条 ->  操作类型 目标 新值 优先级
     - 操作类型: set(改/要求目标已存在) add(新增/要求目标不存在) delete(删除/新值写 -)
                 另有测试用操作 boom：模拟"应用中途意外错误"以验证回滚
     - 新值: JSON 标量字面量（数字/字符串/true/false/null），delete 用 - 占位
     - 优先级: 整数，数值越大优先级越高
     - 空行与 # 开头的行忽略

执行规则：
  - 按优先级从高到低应用；同优先级按定义顺序（稳定排序）
  - 每条操作应用前做前置检查（目标存在性、新值合法性），失败则跳过并报告，其余继续（部分成功）
  - 覆盖冲突：目标已被更高优先级的操作改写过时，低优先级操作跳过并报告
    （理由：高优先级先落地，再让低优先级覆盖会让"优先级"形同虚设，跳过最直观且可预测）
  - 同优先级改同一目标不算冲突：按定义顺序后者覆盖前者（规则内自洽）
  - 任一操作在"应用中途"发生意外错误：把本次已应用的全部操作回滚，恢复原快照，整批中止

输出：
  - 新快照 JSON -> stdout（或 --out 指定文件）
  - 操作记录/报告 -> stderr（或 --report 指定文件）

退出码：0=正常完成（允许部分成功）  1=发生意外错误已回滚  2=输入文件本身无法解析
"""

import argparse
import copy
import json
import sys

OPS = {"set", "add", "delete", "boom"}


def parse_value(token):
    """把新值 token 解析为 JSON 标量；非法则抛 ValueError。"""
    if token == "-":
        return None
    try:
        value = json.loads(token)
    except json.JSONDecodeError as exc:
        raise ValueError("新值不是合法的 JSON 字面量: %r" % token) from exc
    if isinstance(value, (dict, list)):
        raise ValueError("新值只允许标量（数字/字符串/布尔/null），不允许对象或数组: %r" % token)
    return value


def parse_ops(path):
    """解析操作文件，返回 (合法操作列表, 解析错误列表)。"""
    ops, errors = [], []
    with open(path, encoding="utf-8") as fh:
        for lineno, raw in enumerate(fh, 1):
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split()
            if len(parts) != 4:
                errors.append((lineno, line, "格式错误：应为 4 列（操作类型 目标 新值 优先级）"))
                continue
            kind, target, value_tok, prio_tok = parts
            if kind not in OPS:
                errors.append((lineno, line, "未知操作类型: %r（支持 set/add/delete）" % kind))
                continue
            try:
                priority = int(prio_tok)
            except ValueError:
                errors.append((lineno, line, "优先级必须是整数: %r" % prio_tok))
                continue
            if kind == "delete":
                if value_tok != "-":
                    errors.append((lineno, line, "delete 的新值列必须是占位符 '-'"))
                    continue
                value = None
            else:
                try:
                    value = parse_value(value_tok)
                except ValueError as exc:
                    errors.append((lineno, line, str(exc)))
                    continue
            ops.append({
                "line": lineno, "kind": kind, "target": target,
                "value": value, "priority": priority, "seq": len(ops),
            })
    return ops, errors


def apply_ops(snapshot, ops):
    """按规则应用操作，返回 (新快照, 应用记录, 是否发生回滚)。"""
    data = copy.deepcopy(snapshot)
    original = copy.deepcopy(snapshot)   # 回滚基准：整批开始前的快照
    ordered = sorted(ops, key=lambda op: (-op["priority"], op["seq"]))
    changed_by = {}                      # target -> 首个改写它的操作（用于覆盖冲突判定）
    records = []

    for op in ordered:
        desc = "第%d行 %s %s" % (op["line"], op["kind"], op["target"])
        # 1) 覆盖冲突：目标已被更高优先级操作改写（同优先级不算冲突，后者覆盖）
        winner = changed_by.get(op["target"])
        if op["kind"] != "boom" and winner is not None and winner["priority"] > op["priority"]:
            records.append({
                "line": op["line"], "op": desc, "status": "SKIPPED",
                "reason": "覆盖冲突：目标已被更高优先级操作改写（第%d行 %s，优先级 %d > %d），按规则跳过"
                          % (winner["line"], winner["kind"], winner["priority"], op["priority"]),
            })
            continue
        # 2) 前置检查：目标存在性
        if op["kind"] == "set" and op["target"] not in data:
            records.append({"line": op["line"], "op": desc, "status": "SKIPPED",
                            "reason": "前置检查失败：目标 %r 不存在，set 要求目标已存在" % op["target"]})
            continue
        if op["kind"] == "add" and op["target"] in data:
            records.append({"line": op["line"], "op": desc, "status": "SKIPPED",
                            "reason": "前置检查失败：目标 %r 已存在，add 要求目标不存在" % op["target"]})
            continue
        if op["kind"] == "delete" and op["target"] not in data:
            records.append({"line": op["line"], "op": desc, "status": "SKIPPED",
                            "reason": "前置检查失败：目标 %r 不存在，无法删除" % op["target"]})
            continue
        # 3) 应用（此处模拟"应用中途意外错误"：boom 在通过全部前置检查后炸掉）
        try:
            if op["kind"] == "boom":
                raise RuntimeError("模拟的意外错误：操作应用到一半失败（boom）")
            if op["kind"] == "delete":
                del data[op["target"]]
            else:
                data[op["target"]] = op["value"]
        except Exception as exc:  # 意外错误 -> 回滚本次已应用的全部操作
            rolled_back = [r for r in records if r["status"] == "APPLIED"]
            data = original
            records.append({"line": op["line"], "op": desc, "status": "ERROR",
                            "reason": "应用中途意外错误: %s；已回滚此前 %d 条已应用操作，恢复原快照"
                                      % (exc, len(rolled_back))})
            return data, records, True
        # 4) 登记成功
        changed_by.setdefault(op["target"], op)
        records.append({"line": op["line"], "op": desc, "status": "APPLIED", "reason": ""})

    return data, records, False


def build_report(parse_errors, records, rolled_back):
    lines = ["===== 批量操作执行报告 ====="]
    if parse_errors:
        lines.append("【解析失败的操作行】（未参与执行）")
        for lineno, text, reason in parse_errors:
            lines.append("  第%d行 SKIPPED  %s | %s" % (lineno, reason, text))
    lines.append("【操作记录】（按实际应用顺序）")
    for rec in records:
        suffix = (" | " + rec["reason"]) if rec["reason"] else ""
        lines.append("  第%d行 %-7s %s%s" % (rec["line"], rec["status"], rec["op"], suffix))
    applied = sum(1 for r in records if r["status"] == "APPLIED")
    skipped = sum(1 for r in records if r["status"] == "SKIPPED") + len(parse_errors)
    errored = sum(1 for r in records if r["status"] == "ERROR")
    lines.append("【汇总】应用 %d 条，跳过 %d 条，意外错误 %d 条" % (applied, skipped, errored))
    lines.append("【结果】" + ("发生意外错误，已整体回滚，快照保持原样" if rolled_back
                               else "执行完成（部分成功：失败项已跳过，成功项已生效）"))
    return "\n".join(lines) + "\n"


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="批量操作部分成功执行器：按优先级应用操作，失败跳过，意外错误整体回滚")
    parser.add_argument("--snapshot", required=True, help="数据快照文件（JSON 对象）")
    parser.add_argument("--ops", required=True, help="操作列表文件（每行：操作类型 目标 新值 优先级）")
    parser.add_argument("--out", help="新快照输出文件（默认 stdout）")
    parser.add_argument("--report", help="报告输出文件（默认 stderr）")
    args = parser.parse_args(argv)

    try:
        with open(args.snapshot, encoding="utf-8") as fh:
            snapshot = json.load(fh)
        if not isinstance(snapshot, dict):
            raise ValueError("快照必须是 JSON 对象（键 -> 值）")
    except (OSError, ValueError) as exc:
        print("无法读取快照文件: %s" % exc, file=sys.stderr)
        return 2
    try:
        ops, parse_errors = parse_ops(args.ops)
    except OSError as exc:
        print("无法读取操作文件: %s" % exc, file=sys.stderr)
        return 2

    new_snapshot, records, rolled_back = apply_ops(snapshot, ops)
    report = build_report(parse_errors, records, rolled_back)

    out_text = json.dumps(new_snapshot, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(out_text)
    else:
        sys.stdout.write(out_text)
    if args.report:
        with open(args.report, "w", encoding="utf-8") as fh:
            fh.write(report)
    else:
        sys.stderr.write(report)
    return 1 if rolled_back else 0


if __name__ == "__main__":
    sys.exit(main())
