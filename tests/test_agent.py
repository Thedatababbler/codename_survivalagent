"""BudgetedAgent with a scripted model and a fake shell: no model call, no sandbox needed."""

from minisweagent.exceptions import FormatError, Submitted

from adamas_unit.agent import BudgetedAgent
from adamas_unit.ledger import Budget, Ledger

TEMPLATES = {"system_template": "sys", "instance_template": "{{task}}"}


class Model:
    """Replies in order; each reply is (command, prompt_tokens) or 'format_error'."""

    def __init__(self, replies):
        self.replies = list(replies)

    def query(self, messages, **kw):
        r = self.replies.pop(0)
        if r == "format_error":
            raise FormatError({"role": "user", "content": "bad", "extra": {"response": {"usage": {"prompt_tokens": 7}}}})
        cmd, pt = r
        return {"role": "assistant", "content": "", "extra": {"actions": [{"command": cmd}],
                                                             "response": {"usage": {"prompt_tokens": pt, "completion_tokens": 1}}}}

    def format_message(self, **kw):
        return dict(kw)

    def format_observation_messages(self, message, outputs, template_vars=None):
        return [{"role": "user", "content": o["output"]} for o in outputs]

    def get_template_vars(self, **kw):
        return {}

    def serialize(self):
        return {}


class Env:
    class config:
        timeout = 30

    def __init__(self):
        self.ran = []

    def execute(self, action, cwd="", *, timeout=None):
        self.ran.append(action["command"])
        if action["command"] == "submit":
            raise Submitted({"role": "exit", "content": "", "extra": {"exit_status": "Submitted", "submission": ""}})
        return {"output": "ok", "returncode": 0, "exception_info": ""}

    def get_template_vars(self, **kw):
        return {}

    def serialize(self):
        return {}


def ledger(tmp_path, tokens=1000):
    lg = Ledger(Budget(tokens=tokens, seconds=1000, compressions=2, repairs=2), 1, path=tmp_path / "l.json", floor_share=0.0)
    lg.open_unit(0)
    return lg


def agent(tmp_path, replies, lg=None, **kw):
    lg = lg or ledger(tmp_path)
    ls = lg.lease(0, "builder")
    return BudgetedAgent(Model(replies), Env(), ledger=lg, lease=ls, **TEMPLATES, **kw), lg


def test_tokens_booked_and_submit(tmp_path):
    a, lg = agent(tmp_path, [("ls", 100), ("submit", 120)], margin_tokens=10)
    assert a.run("t")["exit_status"] == "Submitted"
    assert lg.used.tokens == 220


def test_margin_stops_before_overspending(tmp_path):
    a, lg = agent(tmp_path, [("ls", 500), ("ls", 400), ("ls", 1)], margin_tokens=150)
    assert a.run("t")["exit_status"] == "TokensExhausted"
    assert lg.used.tokens == 900                         # third call not made: 100 left < margin 150


def test_format_error_is_booked(tmp_path):
    a, lg = agent(tmp_path, ["format_error", ("submit", 10)], margin_tokens=1)
    a.run("t")
    assert lg.used.tokens == 17


def test_request_budget_goes_to_the_planner_not_the_shell(tmp_path):
    seen = []

    def planner(req):
        seen.append(req)
        got = a.ledger.grant(req["lease"], req["kind"], req["amount"], requested=req["amount"], reason=req["reason"],
                             decided_by="test")
        return f"granted {got}"

    lg = Ledger(Budget(tokens=1000, seconds=1000, compressions=0, repairs=0), 2, path=tmp_path / "l.json", floor_share=0.1)
    lg.open_unit(0)                                      # 500 tokens
    a = BudgetedAgent(Model([('request_budget tokens=5000 reason="tests failing"', 10), ("submit", 10)]), Env(),
                      ledger=lg, lease=lg.lease(0, "builder"), planner=planner, margin_tokens=1, **TEMPLATES)
    a.run("t")
    assert seen and seen[0]["kind"] == "tokens" and seen[0]["amount"] == 5000
    assert "request_budget" not in " ".join(a.env.ran)
    assert lg.events[[e["event"] for e in lg.events].index("grant")]["granted"] == 1000 - 10 - 100 - 490  # cap: left - floor - held


def test_compression_uses_the_pool_then_stops(tmp_path):
    def summarizer(msgs):
        return "did things", 5, 1

    replies = [("ls", 300)] * 3 + [("ls", 300)] * 3 + [("ls", 300)] * 3
    lg = Ledger(Budget(tokens=10**6, seconds=1000, compressions=1, repairs=0), 1, path=tmp_path / "l.json", floor_share=0.0)
    lg.open_unit(0)
    a = BudgetedAgent(Model(replies), Env(), ledger=lg, lease=lg.lease(0, "builder"), summarizer=summarizer,
                      context_window=250, keep_recent=1, margin_tokens=1, **TEMPLATES)
    out = a.run("t")
    assert a.compressions == 1 and lg.used.compressions == 1
    assert out["exit_status"] == "ContextExhausted"
    assert any("Summary of your earlier work" in str(m.get("content")) for m in a.messages)


def test_reclaim_ends_the_session(tmp_path):
    a, lg = agent(tmp_path, [("ls", 10)] * 5, margin_tokens=1, probe_every=2, progress=lambda ag: "reclaim")
    assert a.run("t")["exit_status"] == "Reclaimed"
    assert a.n_calls == 2
