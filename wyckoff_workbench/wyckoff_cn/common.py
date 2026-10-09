import hashlib
import json
from datetime import date, datetime, timezone, timedelta

# Modern Chinese-market dates use UTC+08:00. Avoid requiring tzdata on Windows.
# Imports before 1992 are outside this release's supported date scope.
SHANGHAI = timezone(timedelta(hours=8), name="Asia/Shanghai")


class ResearchError(ValueError):
    pass


def now():
    return datetime.now(timezone.utc).isoformat()


def instant(value):
    if not isinstance(value, str):
        raise ResearchError("时间必须是包含时区的 ISO 8601 字符串")
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ResearchError("时间格式错误: " + value) from exc
    if result.tzinfo is None:
        raise ResearchError("时间缺少时区: " + value)
    return result.astimezone(timezone.utc)


def iso_date(value, name="日期"):
    """Parse only canonical YYYY-MM-DD strings used by persisted records."""
    if not isinstance(value, str):
        raise ResearchError(name + "应为YYYY-MM-DD")
    try:
        result = date.fromisoformat(value)
    except ValueError as exc:
        raise ResearchError(name + "应为YYYY-MM-DD") from exc
    if result.isoformat() != value:
        raise ResearchError(name + "应为YYYY-MM-DD")
    return result


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def require_text(value, name):
    if not isinstance(value, str) or not value.strip():
        raise ResearchError(name + "不能为空")
    return value.strip()
