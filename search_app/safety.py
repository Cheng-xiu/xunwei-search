"""Explainable checks for public-post discovery, not private-person profiling.

These narrow checks are a guardrail, not an age-verification or moderation
service. Ambiguous birth-year shorthand is returned to the user unchanged.
"""

from __future__ import annotations

from datetime import date
import re
import unicodedata


_DATING = re.compile(r"相亲|征婚|找对象|婚恋|脱单|择偶|征[男女]友|找[男女]朋友|恋爱对象|dating", re.I)
_PEOPLE = re.compile(r"女生|男生|女孩|男孩|女性|男性|某人|这个人|此人|博主|作者|同一个人|同一人|对象|相亲|征婚")
_WEALTH = re.compile(r"家境|身家|净资产|家庭资产|家庭收入|财富等级|财产|存款|年薪|月薪|收入水平|资产[是达超在约]|[aＡａ][6789]\s*(?:以上|以下|级|家庭|女生|男生)?", re.I)
_INFERENCE = re.compile(r"推断|推测|估算|扒出|挖出|判断.{0,8}(?:家境|财富|资产)|画像|筛选|精准定位")
_CROSS_IDENTITY = re.compile(
    r"跨平台.{0,18}(?:关联|身份|小号|同一人|同一个人|账号匹配|账户匹配)"
    r"|(?:关联|匹配|串联).{0,12}(?:账号|账户|身份|小号)"
    r"|(?:同一个人|同一人|这个人|此人|她|他|博主).{0,12}(?:其他|所有|其它|另一个).{0,5}(?:账号|账户|小号)"
    r"|(?:查出|扒出|挖出|定位).{0,12}(?:真实身份|家庭住址|身份证|手机号|小号)"
    r"|人肉(?:搜索|她|他|这个人)?"
)


def _result(allowed: bool, message: str = "", *, dating: bool = False, warnings: list[str] | None = None) -> dict:
    return {
        "allowed": allowed,
        "message": message,
        "warnings": warnings or [],
        "public_post_only": dating,
    }


def _expand_year(value: str, today_year: int) -> int:
    number = int(value)
    if len(value) == 4:
        return number
    # Only called after an explicit birth-year expression has matched.
    return 2000 + number if number <= today_year % 100 else 1900 + number


def check_query(query: str) -> dict:
    """Return a readable decision without rewriting the user's search.

    Birth-year arithmetic is restricted to explicit birth language. Admission,
    graduation and historical years are not interpreted as ages.
    """
    if not isinstance(query, str) or not query.strip():
        return _result(False, "请输入要搜索的内容。")
    text = unicodedata.normalize("NFKC", query).strip().lower()
    dating = bool(_DATING.search(text))
    if _CROSS_IDENTITY.search(text):
        return _result(False, "不支持关联私人个人在不同平台的身份、寻找小号或收集私人联系方式。可以搜索公开帖子中的主题内容。", dating=dating)

    wealth_target = bool(_WEALTH.search(text) and (
        dating or (_PEOPLE.search(text) and (
            _INFERENCE.search(text)
            or re.search(r"以上|以下|超过|高于|低于|百万|千万|亿|[aＡａ][6789]|这个|此人|某人|博主|作者", text, re.I)
        ))
    ))
    financial_warning = "请去掉针对个人家境、财产或收入的筛选和推断条件。"
    if dating:
        # '07-10年的' is not unambiguously a birth range. Ask, do not infer.
        shorthand = re.search(r"(?<!\d)(\d{2}|(?:19|20)\d{2})\s*(?:年)?\s*[-—–~～至到]\s*(\d{2}|(?:19|20)\d{2})\s*年", text)
        if shorthand is None:
            shorthand = re.search(r"(?<!\d)\d{2}\s*年的", text)
        if shorthand:
            context = text[max(0, shorthand.start() - 12):shorthand.end() + 12]
            explicit_birth = bool(re.search(r"出生|生于|生年|年生|生日|生人", context))
            explicit_school = bool(re.search(r"入学|毕业|届|级|开学|历史|期间|当年|举办|活动|文章|报道|新闻", context))
            if not explicit_birth and not explicit_school:
                label = shorthand.group(0)
                warnings = [financial_warning] if wealth_target else []
                return _result(False, f"请先说明“{label}”指的是出生年份、入学年份还是其他时间。相亲帖检索仅限成年人本人主动公开发布的内容。", dating=True, warnings=warnings)

        if re.search(r"未成年|未满\s*18|不满\s*18|小学生|初中生|高中生|十[一二三四五六七]?岁", text):
            # Explicitly excluding minors is not a request to target them.
            cleaned = re.sub(r"(?:排除|不找|不要|不包含|过滤|剔除|非)\s*(?:未成年人?|未满\s*18\s*岁(?:的人)?|小学生|初中生|高中生)", "", text)
            if re.search(r"未成年|未满\s*18|不满\s*18|小学生|初中生|高中生|十[一二三四五六七]?岁", cleaned):
                return _result(False, "不支持检索或筛选未成年人的相亲、征婚或恋爱对象信息。请仅检索成年人本人主动公开发布的帖子。", dating=True)

        for age in re.finditer(r"(?<!\d)(\d{1,2})(?:\s*[-—–~～至到]\s*(\d{1,2}))?\s*(?:周)?岁", text):
            values = [int(age.group(1))]
            if age.group(2):
                values.append(int(age.group(2)))
            if min(values) < 18:
                return _result(False, "该年龄条件包含未成年人，不能用于相亲或恋爱对象检索。请使用明确的成年人条件。", dating=True)

        current_year = date.today().year
        year_pattern = r"(?<!\d)(\d{4}|\d{2})(?:\s*年)?(?:\s*[-—–~～至到]\s*(\d{4}|\d{2}))?\s*年?"
        explicit_birth_patterns = [
            re.compile(year_pattern + r"\s*(?:出生|生人|生的|生(?=$|[，,。；;、\s的女男]))"),
            re.compile(r"(?:出生于|生于|出生年份(?:为|是)?)[\s:：]*" + year_pattern),
        ]
        for pattern in explicit_birth_patterns:
            for birth in pattern.finditer(text):
                years = [_expand_year(birth.group(1), current_year)]
                if birth.group(2):
                    years.append(_expand_year(birth.group(2), current_year))
                # A birth year alone does not establish that this year's 18th
                # birthday has already passed; require an explicit adult limit.
                age = current_year - max(years)
                if age < 18:
                    return _result(False, "该出生年份条件包含未成年人，不能用于相亲或恋爱对象检索。请仅检索明确已满 18 岁的成年人公开自发帖。", dating=True)
                if age == 18 and not re.search(r"已满\s*18|成年|18\s*岁以上", text):
                    return _result(False, "只凭出生年份不能确认是否已满 18 岁。请明确限定为已满 18 岁的成年人本人公开发帖。", dating=True)

    if wealth_target:
        return _result(False, "不支持针对私人个人的家境、财产或收入画像与筛选。" + financial_warning, dating=dating)
    if dating:
        return _result(True, "仅检索成年人本人主动公开发布的相亲或征婚帖，不推断个人属性或关联账号。", dating=True, warnings=["年龄、学校和地区等条件须由帖子明确自述；缺少证据时标记为未确认。"])
    return _result(True)
