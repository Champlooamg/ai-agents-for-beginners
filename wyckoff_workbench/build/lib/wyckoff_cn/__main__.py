import argparse
import json
import sys
from pathlib import Path

from .bootstrap import bootstrap
from .common import ResearchError
from .data import import_csv, quality
from .portfolio import save_order_plan
from .reports import audit, daily_report, export_bundle
from .research import (analysis, freeze_card, freeze_protocol, gate, record_risk,
                       register_candidate, transition_candidate)
from .store import Store


def main():
    parser = argparse.ArgumentParser(description="中国市场威科夫本地研究工作台 v0.6.3.0")
    parser.add_argument("--data-dir", default=str(Path(__file__).resolve().parents[1] / "data"))
    subs = parser.add_subparsers(dest="command", required=True)
    server = subs.add_parser("serve")
    server.add_argument("--port", type=int, default=8765)
    server.add_argument("--open-browser", action="store_true")
    for c in ("demo", "doctor", "gate"):
        subs.add_parser(c)
    export = subs.add_parser("export")
    export.add_argument("--out", required=True)
    report = subs.add_parser("report")
    report.add_argument("--out", required=True)
    load = subs.add_parser("import-csv")
    load.add_argument("--file", required=True)
    load.add_argument("--metadata", required=True)
    for c in ("analyze", "freeze-card", "candidate", "transition", "risk", "order-plan", "freeze-protocol"):
        p = subs.add_parser(c)
        p.add_argument("--request", required=True)
    args = parser.parse_args()
    store = Store(args.data_dir)
    try:
        if args.command == "serve":
            if not store.records("snapshot"):
                project = Path(__file__).resolve().parents[1]
                if (project / "原始资料_V0.2").is_dir():
                    bootstrap(store, project)
                else:
                    freeze_protocol(store)
            elif not store.records("protocol"):
                freeze_protocol(store)
            store.close()
            from .server import serve
            serve(args.data_dir, Path(__file__).resolve().parents[1], args.port, args.open_browser)
            return 0
        if args.command == "demo":
            result = bootstrap(store, Path(__file__).resolve().parents[1])
        elif args.command == "doctor":
            result = audit(store)
        elif args.command == "gate":
            result = gate(store)
        elif args.command == "import-csv":
            result = quality(import_csv(store, Path(args.file).read_bytes(), json.loads(Path(args.metadata).read_text(encoding="utf-8-sig"))))
        elif args.command in ("export", "report"):
            dest = Path(args.out)
            dest.parent.mkdir(parents=True, exist_ok=True)
            if args.command == "export":
                dest.write_bytes(export_bundle(store))
            else:
                dest.write_text(daily_report(store), encoding="utf-8")
            result = {"output": str(dest.resolve())}
        else:
            methods = {"analyze": analysis, "freeze-card": freeze_card,
                       "candidate": register_candidate, "transition": transition_candidate,
                       "risk": record_risk, "order-plan": save_order_plan,
                       "freeze-protocol": freeze_protocol}
            request = json.loads(Path(args.request).read_text(encoding="utf-8-sig"))
            result = methods[args.command](store, request)
        print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
        return 1 if args.command == "doctor" and not result["ok"] else 0
    except (ResearchError, OSError, ValueError) as exc:
        print("操作失败：" + str(exc), file=sys.stderr)
        return 1
    finally:
        try:
            store.close()
        except Exception:
            pass


if __name__ == "__main__":
    sys.exit(main())
