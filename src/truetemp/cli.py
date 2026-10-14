import argparse
import asyncio
import csv
from datetime import datetime
from pathlib import Path
import sys

from bleak.exc import BleakError
from rich.console import Console

from .export import export_csv
from . import __version__
from .ble import scan, collect
from .config import read_config, save_reference, save_devices, duration


def time_arg(value):
    try:
        return duration(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def parser():
    root = argparse.ArgumentParser(prog="truetemp", description="REF / DUT 采集与本机温湿度标定")
    root.add_argument("--version", action="version", version=f"TrueTemp {__version__}")
    commands = root.add_subparsers(dest="command", required=True)
    scan_cmd = commands.add_parser("scan", help="扫描并选择 REF 和 DUT")
    scan_cmd.add_argument("--duration", type=time_arg, default=30.0)
    scan_cmd.add_argument("--no-interactive", action="store_true")
    device_cmd = commands.add_parser("device", help="配置 REF 和 DUT")
    devices = device_cmd.add_subparsers(dest="action", required=True)
    set_cmd = devices.add_parser("set", help="保存设备标识")
    set_cmd.add_argument("role", choices=["reference", "dut"])
    set_cmd.add_argument("device_id")
    show_cmd = devices.add_parser("show", help="查看设备配置")
    collect_cmd = commands.add_parser("collect", help="持续采集已配置设备，Ctrl+C 停止")
    collect_cmd.add_argument("--output", type=Path)
    collect_cmd.add_argument("--note", default="")
    for command in (scan_cmd, set_cmd, show_cmd, collect_cmd):
        command.add_argument("--config", type=Path, default=Path("truetemp.toml"))
    export_cmd = commands.add_parser("export", help="将采集会话导出为 CSV")
    export_cmd.add_argument("session", type=Path, help="采集会话目录")
    export_cmd.add_argument("--output", type=Path, help="输出 CSV，默认 <会话目录>.csv")
    export_cmd.add_argument("--device", choices=["reference", "dut"], default="reference", help="导出设备，默认 reference")
    dataset_cmd = commands.add_parser("dataset", help="构建全精度配对数据集")
    build_cmd = dataset_cmd.add_subparsers(dest="action", required=True).add_parser("build")
    build_cmd.add_argument("--output", type=Path,
                           help="输出数据集目录，默认 data/dataset")
    train_cmd = commands.add_parser("train", help="训练温度和水汽压 LightGBM 模型")
    train_cmd.add_argument("dataset", type=Path)
    train_cmd.add_argument("--output", type=Path, required=True)
    train_cmd.add_argument("--config", type=Path, help="训练 TOML 配置，省略使用默认值")
    evaluate_cmd = commands.add_parser("evaluate", help="在独立 test 集考试并生成报告")
    evaluate_cmd.add_argument("dataset", type=Path)
    evaluate_cmd.add_argument("--model", type=Path, required=True)
    evaluate_cmd.add_argument("--output", type=Path, required=True)
    evaluate_cmd.add_argument("--exam", action="store_true", help="未通过或覆盖不足返回退出码 3")
    predict_cmd = commands.add_parser("predict", help="离线校正 DUT CSV，无需 REF")
    predict_cmd.add_argument("source", type=Path)
    predict_cmd.add_argument("--model", type=Path, required=True)
    predict_cmd.add_argument("--output", type=Path, required=True)
    return root


def print_exam_summary(console, report):
    """在命令行汇总考试关键结果与解读。"""
    t_h, rh_h = 0.3, 3.0
    inside = report.get('in_range_count', 0)
    all_final = report.get('scores', {}).get('final')
    final_in = report.get('in_range_scores', {}).get('final') or {}
    line = f"样本 {report['count']} | 范围内 {inside} | 范围外 {report.get('out_of_range_count', 0)} | 非物理 {report.get('quality_failures', 0)} | 配对覆盖 {report.get('pairing_coverage', 0):.0%}"
    if inside and final_in.get('temperature'):
        tf, hf = final_in['temperature'], final_in['humidity']
        t_ok = tf['max_abs'] <= t_h + 1e-12
        h_ok = hf['max_abs'] <= rh_h + 1e-12
        line += f"\n正式成绩（范围内）：温度 最大 {tf['max_abs']:.3f}°C {'通过' if t_ok else '未过'}(±{t_h}) | 湿度 最大 {hf['max_abs']:.3f}%RH {'通过' if h_ok else '未过'}(±{rh_h})"
    else:
        line += "\n正式成绩：范围内无样本，精度未验证"
    if all_final:
        tf, hf = all_final['temperature'], all_final['humidity']
        r2t = f" R² {tf['r2']:.3f}" if tf.get('r2') is not None else ""
        r2h = f" R² {hf['r2']:.3f}" if hf.get('r2') is not None else ""
        line += (f"\n全部样本参考：温度 MAE {tf['mae']:.3f}/P95 {tf['p95']:.3f}/最大 {tf['max_abs']:.3f}°C{r2t}"
                 f" | 湿度 MAE {hf['mae']:.3f}/P95 {hf['p95']:.3f}/最大 {hf['max_abs']:.3f}%RH{r2h}")
    derived = report.get('derived') or {}
    if derived.get('dew_point') is not None:
        line += (f"\n派生量（均值）：露点 {derived['dew_point']:.2f}°C"
                 f" | 绝对湿度 {derived['absolute_humidity']:.2f} g/m³")
    line += f"\n覆盖：{'达标' if report['coverage'].get('complete') else '未声明或不足'}"
    console.print("== 考试结果 ==", markup=False)
    console.print(line, markup=False)
    reasons = []
    if not inside:
        reasons.append("范围内无样本：考试数据温湿度都在量程内，请检查 ref 真值或声明范围")
    elif any(final_in.get('temperature', {}).get('max_abs', 0) > t_h + 1e-12 for _ in (0,)):
        reasons.append(f"温度最大误差超 ±{t_h}°C")
    if inside and any(final_in.get('humidity', {}).get('max_abs', 0) > rh_h + 1e-12 for _ in (0,)):
        reasons.append(f"湿度最大误差超 ±{rh_h}%RH")
    if not report['coverage'].get('complete'):
        reasons.append("coverage 未声明或覆盖不足，考试门槛不成立")
    if reasons:
        console.print("解读：" + "；".join(reasons), markup=False)


def select_reference(devices, current, config_path, console, ask=input):
    if current and not any(d.address.casefold() == current.casefold() for d in devices):
        console.print(f"当前 REF 本轮未发现：{current}", markup=False)
    if not devices:
        console.print("未发现 Sensirion 设备。请检查设备广播、蓝牙状态后重新扫描。")
        return
    for i, device in enumerate(devices, 1):
        if device.error:
            console.print(f"设备 {i}：{device.error}", markup=False)
    hint = "保留当前" if current else "跳过"
    while True:
        answer = ask(f"选择标准表 [编号 / Enter {hint} / q 退出]：").strip()
        if not answer or answer.lower() == "q":
            console.print("设备配置未变更。")
            return
        if not answer.isdigit() or not 1 <= int(answer) <= len(devices):
            console.print("请输入列表中的编号。")
            continue
        selected = devices[int(answer) - 1]
        if selected.reading is None:
            console.print("该设备尚无可解析的 SHT4x 广播，不能设为 REF。")
            continue
        if selected.address.casefold() == (current or "").casefold():
            console.print("设备配置未变更。")
        else:
            save_reference(config_path, selected.address)
            console.print(f"已保存到 {config_path.resolve()}\n标准表：{selected.name}  {selected.address}", markup=False)
        console.print("运行 truetemp collect 开始采集，Ctrl+C 保存并停止。")
        return


def select_devices(devices, config_path, console, ask=input):
    if not devices:
        console.print("未发现 REF / DUT 设备，请检查广播状态后重新扫描。")
        return
    configured = dict(read_config(config_path).get("devices", {}))
    choices = dict(configured)
    for role, label in (("reference", "标准表"), ("dut", "DUT")):
        current = configured.get(role)
        if current and not any(d.address.casefold() == current.casefold() for d in devices):
            console.print(f"当前 {label} 本轮未发现：{current}", markup=False)
        while True:
            answer = ask(f"选择{label} [编号 / Enter {'保留当前' if current else '跳过'} / q 退出]：").strip()
            if answer.lower() == "q":
                console.print("设备配置未变更。")
                return
            if not answer:
                break
            if not answer.isdigit() or not 1 <= int(answer) <= len(devices):
                console.print("请输入列表中的编号。")
                continue
            selected = devices[int(answer) - 1]
            if selected.role != role or selected.reading is None:
                console.print(f"该设备不是有效的 {label} 候选：{selected.error or '角色不匹配'}", markup=False)
                continue
            choices[role] = selected.address
            break
    if choices != configured:
        save_devices(config_path, {k: v for k, v in choices.items() if k in ("reference", "dut")})
        console.print(f"设备配置已保存：{config_path.resolve()}", markup=False)
    else:
        console.print("设备配置未变更。")
    if not choices.get("reference"):
        console.print("尚未设置 REF，请使用 truetemp device set reference REF_ID。")
    else:
        console.print("运行 truetemp collect 开始采集，Ctrl+C 保存并停止。")


def main(argv=None):
    args = parser().parse_args(argv)
    console = Console()
    try:
        if args.command == "export":
            output, count = export_csv(args.session, args.output, device=args.device)
            console.print(f"已导出 {count} 条记录：{output}", markup=False)
            return 0
        if args.command == "dataset":
            from .dataset import build_dataset, recent_sessions
            default_dir = Path("data") / "dataset"
            output = (args.output or default_dir).resolve()
            candidates = recent_sessions("data", 5)
            if not candidates:
                console.print("data/ 下没有已正常关闭的会话，无法构建数据集。请先完整运行 collect。", style="yellow")
                return 1
            console.print("最近的采集会话：")
            for i, (path, meta) in enumerate(candidates, 1):
                console.print(f"  {i}. {path.name} · 结束 {meta['ended_at']} · 间隔 {meta.get('interval_seconds')}s · 样本 REF {meta.get('ref_samples')}/DUT {meta.get('dut_samples')}", markup=False)
            def parse_list(label, prompt):
                raw = input(prompt).strip()
                if not raw:
                    return set()
                try:
                    items = {int(x) for x in raw.replace('，', ',').split(',') if x}
                except ValueError:
                    console.print(f"{label} 编号无效，需为数字。", style="yellow")
                    return None
                bad = [x for x in items if not 1 <= x <= len(candidates)]
                if bad:
                    console.print(f"{label} 编号越界：{sorted(bad)}，需在 1-{len(candidates)} 内。", style="yellow")
                    return None
                return items
            train_idx = parse_list("train", f"train 会话编号（逗号分隔，可多选，回车跳过）：")
            if train_idx is None:
                return 1
            valid_idx = parse_list("validation", f"validation 会话编号（逗号分隔，可多选，回车跳过）：")
            if valid_idx is None:
                return 1
            test_idx = parse_list("test", f"test 会话编号（逗号分隔，可多选，回车跳过）：")
            if test_idx is None:
                return 1
            if train_idx & valid_idx or train_idx & test_idx or valid_idx & test_idx:
                console.print("同一会话不能同时归为 train / validation / test。", style="yellow")
                return 1
            if not train_idx and not valid_idx and not test_idx:
                console.print("未选择任何会话。", style="yellow")
                return 1
            manifest_path = Path("data") / ".truetemp-interactive-manifest.toml"
            events = []
            for i, (path, _) in enumerate(candidates, 1):
                if i in train_idx:
                    split = "train"
                elif i in valid_idx:
                    split = "validation"
                elif i in test_idx:
                    split = "test"
                else:
                    continue
                events.append(f'[[sessions]]\npath = "{path}"\nsplit = "{split}"')
            manifest_path.write_text('\n'.join(events) + '\n')
            try:
                counts = build_dataset(manifest_path, output)
            finally:
                manifest_path.unlink(missing_ok=True)
            console.print(f"数据集已保存：{output}\n样本数：{counts}", markup=False)
            return 0
        if args.command == "train":
            from .training import train
            model_id, warnings = train(args.dataset, args.output, args.config)
            console.print(f"模型已保存：{args.output.resolve()}\n模型 ID：{model_id}", markup=False)
            for warning in warnings:
                console.print(warning, style="yellow", markup=False)
            return 0
        if args.command == "evaluate":
            from .evaluation import evaluate
            report, code = evaluate(args.dataset, args.model, args.output, args.exam)
            print_exam_summary(console, report)
            console.print(f"报告：{args.output.resolve() / 'report.md'}\n退出码 {code}" + ("（考试通过）" if code == 0 else "（未通过，待验收）" if code == 3 else f"（失败）"), markup=False)
            return code
        if args.command == "predict":
            from .calibration import predict_csv
            count = predict_csv(args.source, args.model, args.output)
            console.print(f"已校正 {count} 条记录：{args.output.resolve()}", markup=False)
            return 0
        config = read_config(args.config)
        reference = config.get("devices", {}).get("reference")
        dut = config.get("devices", {}).get("dut")
        if args.command == "device":
            if args.action == "set":
                save_devices(args.config, {args.role: args.device_id})
                console.print(f"已保存 {args.role}：{args.device_id}\n配置：{args.config.resolve()}", markup=False)
            else:
                console.print(f"配置：{args.config.resolve()}\n标准表：{reference or '未设置'}\nDUT：{dut or '未设置'}", markup=False)
        elif args.command == "scan":
            devices = asyncio.run(scan(args.duration, reference, console, dut=dut))
            if not args.no_interactive and sys.stdin.isatty() and sys.stdout.isatty():
                select_devices(devices, args.config, console)
            elif not devices:
                console.print("未发现 REF / DUT 设备。")
        elif args.command == "collect":
            if not reference:
                raise ValueError("请先运行 truetemp scan 选择 REF，或 truetemp device set reference REF_ID")
            interval = duration(str(config.get("collection", {}).get("interval", "5s")))
            output = args.output or Path("data") / datetime.now().strftime("%Y%m%d-%H%M%S-%f")
            asyncio.run(collect(output, reference, interval, args.note, console, dut=dut))
        return 0
    except (KeyboardInterrupt, EOFError):
        console.print("已退出，未保存本次设备选择。")
        return 0
    except (ValueError, TypeError, KeyError, csv.Error) as exc:
        console.print(f"参数或配置错误：{exc}", style="red", markup=False)
        return 2
    except (BleakError, TimeoutError) as exc:
        console.print(f"蓝牙扫描失败：{exc}\n请检查系统蓝牙开关，以及系统设置 → 隐私与安全性 → 蓝牙中的运行应用权限。", style="red", markup=False)
        return 1
    except OSError as exc:
        console.print(f"文件或系统操作失败：{exc}", style="red", markup=False)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
