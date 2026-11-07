"""School entry configuration for Chaoxing's shared office seat application."""
from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import parse_qs, urlencode, urlsplit


DEFAULT_ENTRY_URL = (
    "https://office.chaoxing.com/front/third/apps/seat/index"
    "?fidEnc=214d62ddb0e920e7&mappId=4309667"
)
OFFICE_ORIGIN = "https://office.chaoxing.com"
HEBUT_NAME = "河北工业大学"
SCHOOL_BINDING_MESSAGE = (
    "页面要求绑定学号/工号或单位身份。请先核对当前预约入口是否属于该账号的学校；"
    "若入口正确，请先在学习通本校入口完成正常身份绑定，再重新测试。"
)


def binding_prompt(text: str) -> bool:
    """Recognize explicit identity prompts, not the mere mention of a school ID."""
    if not isinstance(text, str):
        return False
    matches = re.finditer(
        r"(?:绑定|认证)[^。！？\n]{0,18}(?:学号|工号|单位身份)|"
        r"(?:学号|工号|单位身份)[^。！？\n]{0,18}(?:未绑定|需要绑定|尚未认证)", text)
    return any(not re.search(r'(?:无需|无须|不用|不必|不需要|已经|已|取消|解除)\s*$',
                             text[max(0, match.start()-8):match.start()]) for match in matches)


@dataclass(frozen=True)
class SchoolEntry:
    department: str
    app_id: str

    @classmethod
    def parse(cls, value: str) -> SchoolEntry:
        if not isinstance(value, str) or value != value.strip() or len(value) > 2048:
            raise ValueError("请粘贴完整的学习通图书馆预约入口链接。")
        parts = urlsplit(value)
        if (parts.scheme != "https" or parts.netloc != "office.chaoxing.com"
                or parts.path not in {"/front/third/apps/seat/index", "/front/third/apps/seat/list"}
                or parts.username or parts.password or parts.fragment):
            raise ValueError("目前支持 office.chaoxing.com 的学习通座位预约入口；其他系统需要另行适配。")
        params = parse_qs(parts.query, keep_blank_values=True)
        if any(len(values) != 1 for values in params.values()):
            raise ValueError("预约入口包含重复参数，请重新复制原始入口。")
        departments = [params[key][0] for key in ("fidEnc", "deptIdEnc") if key in params]
        if (not departments or len(set(departments)) != 1
                or re.fullmatch(r"[A-Za-z0-9_-]{1,128}", departments[0]) is None):
            raise ValueError("预约入口缺少有效的 fidEnc / deptIdEnc 学校标识。")
        app_id = params.get("mappId", [""])[0]
        if re.fullmatch(r"[0-9]{1,20}", app_id) is None:
            raise ValueError("预约入口缺少 mappId，请从学习通重新复制包含学校应用标识的完整入口。")
        return cls(departments[0], app_id)

    @property
    def url(self) -> str:
        return OFFICE_ORIGIN + "/front/third/apps/seat/index?" + urlencode(
            {"fidEnc": self.department, "mappId": self.app_id})

    @property
    def identity_parameters(self) -> dict[str, str]:
        return {"fidEnc": self.department, "mappId": self.app_id}

    @property
    def is_hebut(self) -> bool:
        return self.department == "214d62ddb0e920e7"

    def matches_page(self, value: str) -> bool:
        parts = urlsplit(value)
        params = parse_qs(parts.query, keep_blank_values=True)
        departments = [item for key in ("fidEnc", "deptIdEnc") for item in params.get(key, [])]
        apps = params.get("mappId", [])
        return (parts.scheme == 'https' and parts.netloc == 'office.chaoxing.com'
                and not parts.fragment and not parts.username and not parts.password
                and parts.path in {'/front/third/apps/seat/index', '/front/third/apps/seat/list'}
                and all(len(values) == 1 for values in params.values())
                and bool(departments) and all(item == self.department for item in departments)
                and len(departments) <= 2 and (not apps or apps == [self.app_id]))


def validate_school_target(name: str, value: str) -> SchoolEntry:
    if not isinstance(name, str):
        raise ValueError('学校名称格式不正确。')
    if not value:
        raise ValueError('尚未设置学校预约入口。请点击“学校与放号规则”，选择学校或粘贴目标学校的预约链接。')
    entry = SchoolEntry.parse(value)
    if entry.is_hebut and name.strip() != HEBUT_NAME:
        raise ValueError('当前预约链接仍属于河北工业大学。学校名称只是备注，请更换目标学校的完整预约入口。')
    if not entry.is_hebut and name.strip() == HEBUT_NAME:
        raise ValueError('当前预约链接已更换，请将学校名称改为该入口对应的学校，避免混用配置。')
    return entry


def validate_location(campus: str, floor: str, third_level: str = "", room_id: int = 0) -> None:
    for name, value, required in (("校区/馆名", campus, True), ("楼层/二级区域", floor, True),
                                  ("三级区域", third_level, False)):
        if (not isinstance(value, str) or value != value.strip() or len(value) > 120
                or (required and not value) or any(ord(char) < 32 for char in value)):
            raise ValueError(f"{name}请按学校页面填写，最多 120 字。")
    if type(room_id) is not int or not 0 <= room_id <= 2**63 - 1:
        raise ValueError("房间编号应为正整数，留空时自动匹配唯一房间。")


def location_key(entry_url: str, campus: str, floor: str, third_level: str = "", room_id: int = 0) -> str:
    if not entry_url:
        import json
        return json.dumps(['unconfigured', campus, floor, third_level, room_id], ensure_ascii=False)
    # Keep the original key for unchanged HEBUT settings, so old floor choices migrate.
    entry = SchoolEntry.parse(entry_url)
    if entry.url == DEFAULT_ENTRY_URL and not third_level and not room_id:
        return f"{campus}|{floor}"
    import json
    return json.dumps([entry.department, entry.app_id, campus, floor, third_level, room_id], ensure_ascii=False)
