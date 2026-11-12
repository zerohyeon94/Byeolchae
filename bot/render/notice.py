"""[별채] 안내 문구."""

from bot.workflow.state import NoticeCode

_TEXT = {
    NoticeCode.STOPPED: "작업을 중지했다. 실행 중인 에이전트를 종료했다.",
    NoticeCode.REJECTED: "지금 처리할 수 없는 입력이다.",
    NoticeCode.DECISION_RECORDED: ("작업을 기록하고 GitHub Issue를 만들었다."),
    NoticeCode.AGENT_FAILED: "응답을 받지 못했다. 같은 질문을 다시 보내면 다시 요청한다.",
    NoticeCode.NOT_IMPLEMENTED: "아직 구현되지 않은 명령이다.",
    NoticeCode.NEEDS_HUMAN: "확인이 필요하다. 판단 후 /resume으로 재개하거나 /stop으로 끝낸다.",
    NoticeCode.REMINDER: "이 작업이 Zero를 기다린 지 오래됐다. 다시 알린다 (1회).",
    NoticeCode.INTERRUPTED: (
        "봇이 재시작되어 진행 중이던 단계를 멈췄다. 확인 후 판단하거나 /stop으로 끝낸다."
    ),
}


def render_notice(code: NoticeCode, detail: str = "") -> str:
    if code is NoticeCode.REJECTED and detail:
        return detail
    text = _TEXT[code]
    return f"{text}\n{detail}" if detail else text
