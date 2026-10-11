"""团队智能日报生成器。

第一版：把本地 JSON 模拟数据整理成 Markdown 日报。
用法：python daily_report.py <输入JSON路径> [输出MD路径]
"""

import datetime
import json
import os
import sys
from report_core import (
    CATEGORY_NAMES,
    CATEGORY_ORDER,
    RECORD_FIELDS,
    group_by_member,
    render_report,
    type_name,
    validate,
)


def usage():
    return "用法：python daily_report.py <输入JSON路径> [输出MD路径]"


def exit_with_error(code, msg):
    """打印中文错误信息，并以指定退出码结束程序。"""
    print(msg)
    sys.exit(code)


def parse_args(argv):
    """解析命令行参数，返回 (输入路径, 输出路径)。"""
    if len(argv) < 2:
        exit_with_error(1, "错误：缺少输入文件路径。\n" + usage())
    if len(argv) > 3:
        exit_with_error(1, "错误：多余的命令行参数。\n" + usage())
    input_path = argv[1]
    output_path = argv[2] if len(argv) >= 3 else None
    return input_path, output_path


def load_json(path):
    """读取并解析 JSON 文件，返回解析后的对象。

    文件读取失败或 JSON 语法错误时，打印中文提示并以退出码 1 结束。
    """
    if os.path.isdir(path):
        exit_with_error(1, f"错误：路径是目录，不是文件：{path}")

    try:
        with open(path, "r", encoding="utf-8") as f:
            text = f.read()
    except FileNotFoundError:
        exit_with_error(1, f"错误：文件不存在：{path}")
    except PermissionError:
        exit_with_error(1, f"错误：没有权限读取文件：{path}")
    except UnicodeDecodeError:
        exit_with_error(1, f"错误：文件不是 UTF-8 编码：{path}")
    except OSError:
        exit_with_error(1, f"错误：无法读取文件：{path}")

    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        exit_with_error(
            1,
            f"错误：JSON 语法错误（第 {e.lineno} 行 第 {e.colno} 列）：{e.msg}",
        )


def detect_duplicates(data):
    """检测同类内内容完全相同的记录，返回提示信息列表（不删除记录）。"""
    messages = []
    for category in CATEGORY_ORDER:
        seen = {}
        for i, item in enumerate(data.get(category, []), start=1):
            key = json.dumps(item, ensure_ascii=False, sort_keys=True)
            if key in seen:
                messages.append(
                    f"提示：{CATEGORY_NAMES[category]}（{category}）第 {i} 条"
                    f"与第 {seen[key]} 条内容相同，已保留（不自动去重）"
                )
            else:
                seen[key] = i
    return messages


def write_output(md, out_path):
    """把日报文本写到 .md 文件。

    写入失败（目录不存在、无权限等）时，打印中文提示并以退出码 1 结束。
    """
    try:
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(md)
    except FileNotFoundError:
        exit_with_error(1, f"错误：输出目录不存在：{out_path}")
    except PermissionError:
        exit_with_error(1, f"错误：没有权限写入输出文件：{out_path}")
    except OSError:
        exit_with_error(1, f"错误：无法写入输出文件：{out_path}")


def main():
    input_path, output_path = parse_args(sys.argv)
    data = load_json(input_path)

    errors = validate(data)
    if errors:
        lines = ["输入数据存在以下错误："]
        lines += ["  - " + err for err in errors]
        lines.append("未生成日报。")
        exit_with_error(2, "\n".join(lines))

    for msg in detect_duplicates(data):
        print(msg)

    groups = group_by_member(data)
    today = datetime.date.today().isoformat()
    md = render_report(groups, today)

    if output_path is None:
        output_path = f"daily_report-{today}.md"

    if os.path.normcase(os.path.abspath(input_path)) == os.path.normcase(os.path.abspath(output_path)):
        exit_with_error(1, "错误：输出路径与输入文件相同，拒绝覆盖输入数据。")

    write_output(md, output_path)

    print(md)


if __name__ == "__main__":
    main()
