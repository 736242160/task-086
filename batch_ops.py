#!/usr/bin/env python3
"""batch_ops.py — 批量操作工具（纯标准库，单文件）

功能：
  读取数据快照（JSON）与操作列表，按优先级从高到低（同优先级按定义顺序）
  应用操作，输出应用后的快照与执行报告。

特性：
  * 应用前校验：目标是否存在、新值是否合法；失败的操作跳过并报告，其余继续（部分成功）
  * 覆盖冲突：低优先级操作试图修改已被高优先级操作改过的目标时，
    跳过低优先级操作、保留高优先级结果，并报告冲突
    （理由：优先级代表意图强度，允许低优先级覆盖会使排序失去意义；
     同优先级同目标按定义顺序后者覆盖前者，不视为冲突）
  * 回滚：任一操作在应用中途发生意外错误（非校验类失败），
    撤销本次已应用的全部操作，恢复原快照并报告

操作列表格式（每行一条，# 开头为注释，空行忽略）：
  操作类型 目标 新值 优先级
  - 操作类型: SET（更新，目标须已存在）/ ADD（新增，目标须不存在）/ DELETE（删除）
  - 新值: JSON 字面量（如 123、"abc"、true、[1,2]、{"a":1}），
          无法按 JSON 解析时按普通字符串处理；DELETE 用 - 占位
  - 优先级: 整数，越大越先应用
  含空格的值请用引号包裹（按 shell 规则切分），如：
  SET greeting "hello world" 10

用法：
  python3 batch_ops.py snapshot.json ops.txt
  python3 batch_ops.py snapshot.json ops.txt -o new_snapshot.json -r report.json

退出码：0=全部应用成功  1=部分成功（有跳过/冲突）  2=已回滚  3=参数或 IO 错误
"""

from __future__ import annotations

import argparse
import copy
import json
import shlex
import sys
from dataclasses import dataclass, field
from typing import Any

OP_TYPES = ("SET", "ADD", "DELETE")


class ValidationError(Exception):
    """可预期的校验失败：跳过该操作，其余继续。"""


@dataclass
class Operation:
    seq: int            # 定义顺序（从 0 开始）
    lineno: int         # 在操作文件中的行号
    raw: str            # 原始行
    op_type: str
    target: str
    value: Any          # DELETE 为 None
    priority: int


@dataclass
class Report:
    applied: list = field(default_factory=list)    # 成功应用的操作
    skipped: list = field(default_factory=list)    # 校验失败被跳过的操作
    conflicts: list = field(default_factory=list)  # 覆盖冲突
    parse_errors: list = field(default_factory=list)  # 无法解析的行
    rolled_back: bool = False
    rollback_reason: str | None = None

    @property
    def status(self) -> str:
        if self.rolled_back:
            return "rolled_back"
        if self.skipped or self.conflicts or self.parse_errors:
            return "partial_success"
        return "success"

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "rolled_back": self.rolled_back,
            "rollback_reason": self.rollback_reason,
            "applied": self.applied,
            "skipped": self.skipped,
            "conflicts": self.conflicts,
            "parse_errors": self.parse_errors,
        }


def parse_value(token: str) -> Any:
    """新值解析：优先按 JSON 字面量解析，失败则按普通字符串。"""
    try:
        return json.loads(token)
    except (json.JSONDecodeError, ValueError):
        return token


def parse_ops(text: str, report: Report) -> list[Operation]:
    """解析操作列表。无法解析的行记入报告并跳过。"""
    ops: list[Operation] = []
    for lineno, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            parts = shlex.split(line)
        except ValueError as exc:
            report.parse_errors.append(
                {"lineno": lineno, "raw": raw_line, "reason": f"切分失败: {exc}"})
            continue
        if len(parts) != 4:
            report.parse_errors.append(
                {"lineno": lineno, "raw": raw_line,
                 "reason": f"应为 4 个字段（操作类型 目标 新值 优先级），实际 {len(parts)} 个"})
            continue
        op_type, target, value_token, prio_token = parts
        op_type = op_type.upper()
        if op_type not in OP_TYPES:
            report.parse_errors.append(
                {"lineno": lineno, "raw": raw_line,
                 "reason": f"未知操作类型 {op_type!r}，支持 {OP_TYPES}"})
            continue
        try:
            priority = int(prio_token)
        except ValueError:
            report.parse_errors.append(
                {"lineno": lineno, "raw": raw_line,
                 "reason": f"优先级 {prio_token!r} 不是整数"})
            continue
        value = None if op_type == "DELETE" else parse_value(value_token)
        ops.append(Operation(seq=len(ops), lineno=lineno, raw=raw_line,
                             op_type=op_type, target=target,
                             value=value, priority=priority))
    return ops


def validate(op: Operation, snapshot: dict) -> None:
    """应用前校验，失败抛 ValidationError（跳过该操作，不影响其余）。"""
    exists = op.target in snapshot
    if op.op_type == "SET":
        if not exists:
            raise ValidationError(f"目标 {op.target!r} 不存在，无法 SET")
    elif op.op_type == "ADD":
        if exists:
            raise ValidationError(f"目标 {op.target!r} 已存在，无法 ADD")
    elif op.op_type == "DELETE":
        if not exists:
            raise ValidationError(f"目标 {op.target!r} 不存在，无法 DELETE")
    if op.op_type in ("SET", "ADD"):
        if op.value is None:
            raise ValidationError("新值非法：不允许为 null（或占位符 '-'）")
        if isinstance(op.value, str) and op.value == "":
            raise ValidationError("新值非法：不允许为空字符串")


def apply_op(op: Operation, snapshot: dict) -> None:
    """真正修改快照。此处抛出的非预期异常将触发整体回滚。"""
    if op.op_type == "DELETE":
        del snapshot[op.target]
    else:  # SET / ADD
        snapshot[op.target] = op.value


def describe(op: Operation) -> dict:
    return {"seq": op.seq, "lineno": op.lineno, "op": op.op_type,
            "target": op.target, "value": op.value, "priority": op.priority}


def run(snapshot: dict, ops: list[Operation], report: Report) -> dict:
    """按优先级应用操作，返回结果快照（回滚时返回原快照的深拷贝）。"""
    original = copy.deepcopy(snapshot)
    working = copy.deepcopy(snapshot)
    # 优先级从高到低；同优先级保持定义顺序（seq 升序，稳定排序）
    ordered = sorted(ops, key=lambda o: (-o.priority, o.seq))
    # 记录每个目标被哪条操作改过（用于覆盖冲突检测）
    modified_by: dict[str, Operation] = {}

    try:
        for op in ordered:
            try:
                validate(op, working)
            except ValidationError as exc:
                report.skipped.append({**describe(op), "reason": str(exc)})
                continue

            winner = modified_by.get(op.target)
            if winner is not None and winner.priority > op.priority:
                # 覆盖冲突：低优先级不得覆盖高优先级的修改，跳过并报告
                report.conflicts.append({
                    **describe(op),
                    "reason": "覆盖冲突：目标已被更高优先级的操作修改，"
                              "按规则保留高优先级结果，跳过本操作",
                    "winner": describe(winner),
                })
                continue

            apply_op(op, working)  # 意外异常 -> 外层捕获 -> 回滚
            modified_by[op.target] = op
            report.applied.append(describe(op))
    except Exception as exc:  # 意外错误：回滚本次已应用的全部操作
        report.rolled_back = True
        report.rollback_reason = (
            f"操作 #{op.seq}（第 {op.lineno} 行）应用中途发生意外错误: "
            f"{type(exc).__name__}: {exc}；已回滚全部已应用操作")
        report.applied.clear()
        return original

    return working


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="批量操作工具：优先级排序、部分成功、覆盖冲突报告、意外错误回滚")
    parser.add_argument("snapshot", help="数据快照文件（JSON 对象）")
    parser.add_argument("ops", help="操作列表文件（每行：操作类型 目标 新值 优先级）")
    parser.add_argument("-o", "--out", help="结果快照输出文件（默认与报告一起输出到 stdout）")
    parser.add_argument("-r", "--report", help="报告输出文件（默认与快照一起输出到 stdout）")
    args = parser.parse_args(argv)

    try:
        with open(args.snapshot, encoding="utf-8") as f:
            snapshot = json.load(f)
        if not isinstance(snapshot, dict):
            raise ValueError("快照必须是 JSON 对象（key -> value）")
        with open(args.ops, encoding="utf-8") as f:
            ops_text = f.read()
    except (OSError, ValueError) as exc:
        print(f"读取输入失败: {exc}", file=sys.stderr)
        return 3

    report = Report()
    ops = parse_ops(ops_text, report)
    result = run(snapshot, ops, report)

    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
    if args.report:
        with open(args.report, "w", encoding="utf-8") as f:
            json.dump(report.to_dict(), f, ensure_ascii=False, indent=2)
    if not args.out and not args.report:
        json.dump({"snapshot": result, "report": report.to_dict()},
                  sys.stdout, ensure_ascii=False, indent=2)
        print()
    else:
        if not args.out:
            json.dump(result, sys.stdout, ensure_ascii=False, indent=2)
            print()
        if not args.report:
            json.dump(report.to_dict(), sys.stderr, ensure_ascii=False, indent=2)
            print(file=sys.stderr)

    return {"success": 0, "partial_success": 1, "rolled_back": 2}[report.status]


if __name__ == "__main__":
    sys.exit(main())
