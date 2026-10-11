"""手动生成/重试日报：python web/run_daily_job.py --date YYYY-MM-DD"""

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from web.app import DATABASE_PATH
from web.daily_jobs import run_daily_report


def main(argv=None):
    parser = argparse.ArgumentParser(description="手动生成指定日期的团队日报")
    parser.add_argument("--date", required=True, help="工作日期，格式 YYYY-MM-DD")
    args = parser.parse_args(argv)
    result = run_daily_report(args.date, DATABASE_PATH)
    print(result["message"])
    return 1 if result["status"] == "failed" else 0


if __name__ == "__main__":
    raise SystemExit(main())
