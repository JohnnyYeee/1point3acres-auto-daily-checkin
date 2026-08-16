# src/quiz.py
"""
一亩三分地「每日答题」自动作答。

思路（与签到相同的验证码套路）
------------------------------
1. GET  /api/daily_questions      取今日题目（题干 + 4 个选项）
2. 在本地题库 questions.json 里按题干查正确答案文本
3. 找出答案对应的选项号（1~4）
4. 用 2captcha 解 Turnstile，POST 提交答案

注意
----
- 答案对不对完全取决于题库是否覆盖该题。题库命中不到的新题会被安全跳过
  （不会乱猜、不会提交错误答案），并在日志里打印题干，方便你手动补进题库。
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Dict, List, Optional, Union

from loguru import logger

from .client import P3ASession
from .signer import CHECKIN_PAGE, SITEKEY, solve_turnstile

# 每日答题 API（取题 + 交卷是同一个地址，GET 取题 / POST 交卷）
DAILY_Q_URL = "https://api.1point3acres.com/api/daily_questions"

# 题库文件（仓库根目录 questions.json）
_BANK_PATH = Path(__file__).resolve().parent.parent / "questions.json"

AnswerType = Union[str, List[str]]


# ────────────────────────── 题库 ─────────────────────────────────
def load_bank(path: Path = _BANK_PATH) -> Dict[str, AnswerType]:
    """读取本地题库；文件缺失或损坏时返回空题库（答题整体跳过）。"""
    if not path.exists():
        logger.warning(f"题库文件不存在：{path}，答题将全部跳过")
        return {}
    try:
        with path.open("r", encoding="utf-8") as fp:
            bank = json.load(fp)
        logger.debug(f"题库载入成功，共 {len(bank)} 题")
        return bank
    except Exception as exc:  # noqa: BLE001
        logger.error(f"题库解析失败：{exc}")
        return {}


def _normalize(text: str) -> str:
    """去掉首尾空白、把全/半角问号统一，便于宽松匹配题干。"""
    return re.sub(r"\s+", "", text).rstrip("?？").strip()


def lookup_answer(
    question_text: str, bank: Dict[str, AnswerType]
) -> Optional[AnswerType]:
    """在题库里查答案：先精确匹配，再按归一化后的题干匹配。"""
    if question_text in bank:
        return bank[question_text]
    norm = _normalize(question_text)
    for q, a in bank.items():
        if _normalize(q) == norm:
            return a
    return None


def _match_option(answer: AnswerType, options: Dict[int, str]) -> Optional[int]:
    """在 4 个选项里找出与正确答案文本对应的选项号（1~4）。"""
    candidates = answer if isinstance(answer, list) else [answer]
    for num, opt in options.items():
        opt_norm = _normalize(opt)
        for cand in candidates:
            cand_norm = _normalize(cand)
            if opt_norm and (opt_norm == cand_norm
                             or opt_norm in cand_norm
                             or cand_norm in opt_norm):
                return num
    return None


# ────────────────────────── 主入口 ─────────────────────────────────
def answer_today(
    sess: P3ASession,
    captcha_api_key: Optional[str] = None,
) -> str:
    """
    自动完成今日答题并返回提示。

    Parameters
    ----------
    sess: P3ASession         已登录的会话
    captcha_api_key: str    2captcha API key
    """
    bank = load_bank()
    if not bank:
        return "跳过：题库为空"

    # 1) 取今日题目
    raw = sess.get(DAILY_Q_URL, headers={
        "Origin": "https://www.1point3acres.com",
    })
    try:
        data = json.loads(raw)
    except Exception:
        logger.error(f"取题返回非 JSON：\n{raw[:200]}")
        raise

    q = data.get("question") or {}
    # 已答过：接口通常不再返回题目，或带已完成标记
    if not q or not q.get("id"):
        msg = data.get("msg", "")
        if "已" in msg or data.get("errno") == -1:
            logger.info(f"今日已答题：{msg or '已完成'}")
            return "已答题 ✓"
        logger.info(f"未取到今日题目（可能已答或暂无题）：{data}")
        return "跳过：无今日题目"

    qid = q["id"]
    qtext = q.get("qc", "")
    options = {n: str(q.get(f"a{n}", "")) for n in (1, 2, 3, 4)}
    options = {n: v for n, v in options.items() if v}  # 去掉空选项
    logger.info(f"今日题目[{qid}]：{qtext}")

    # 2) 查题库 + 3) 定位选项号
    answer = lookup_answer(qtext, bank)
    if answer is None:
        logger.warning(f"题库未收录此题，安全跳过：{qtext}")
        return "跳过：题库无此题（请手动补题库）"

    option_num = _match_option(answer, options)
    if option_num is None:
        logger.warning(
            f"题库答案与选项对不上，安全跳过。答案={answer} 选项={options}"
        )
        return "跳过：答案与选项不匹配"
    logger.debug(f"选定答案：选项 {option_num} = {options[option_num]}")

    # 4) 解验证码并提交
    if not captcha_api_key:
        raise RuntimeError("缺少 captcha_api_key，无法获取 Turnstile token")
    token = solve_turnstile(SITEKEY, captcha_api_key, CHECKIN_PAGE)
    logger.debug("答题 2captcha token ok")

    payload = {
        "qid": qid,
        "answer": option_num,
        "captcha_response": token,
        "hashkey": "",
        "version": 2,
    }
    resp_text = sess.post(
        DAILY_Q_URL,
        data=json.dumps(payload),
        headers={
            "Content-Type": "application/json",
            "Origin": "https://www.1point3acres.com",
        },
    )

    # 解析响应
    try:
        result = json.loads(resp_text)
    except Exception:
        logger.error(f"交卷返回非 JSON：\n{resp_text[:200]}")
        raise

    msg = result.get("msg", "")
    errno = result.get("errno")
    if errno in (0, None) and ("正确" in msg or "成功" in msg or not msg):
        logger.success(f"✅ 答题成功：{msg or '已提交'}")
        return msg or "答题成功"
    if "已" in msg:  # 已经答过
        logger.info(f"今日已答题：{msg}")
        return "已答题 ✓"
    # 答错或其他错误：不抛异常（答错无实质惩罚），只提示
    logger.warning(f"答题未通过：{result}")
    return f"答题失败/答错：{msg or result}"
