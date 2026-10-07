"""
The Gauntlet: AI investment banking interview practice (Use case #13, Chatbot).

Rhea, an AI interviewer, asks technical and behavioural IB questions, grades every answer against
key points, explains what a strong answer covers, and moves your "candidate stock" up or down.
Progress is kept across sessions in a bookmarkable link (or a downloadable file).

Run locally:  streamlit run app.py
"""

import html
import json
import os
import random
from datetime import datetime

import streamlit as st

import engine as eng
from llm_client import PROVIDER_NAMES, BadOutput, LLMClient, LLMError, detect_provider
from prompts import GRADER_SYSTEM, INTERVIEWER, REPORT_SYSTEM, grading_prompt, report_prompt

st.set_page_config(page_title="The Gauntlet · IB Interview Practice", page_icon="📈", layout="wide")

MAX_ANSWER_CHARS = 1500
RECENT_MESSAGES = 6

# ------------------------------------------------------------------ styling
st.markdown(
    """
<style>
@import url('https://fonts.googleapis.com/css2?family=IBM+Plex+Sans:wght@400;500;600;700&family=IBM+Plex+Mono:wght@500;600&family=IBM+Plex+Serif:wght@600&display=swap');
:root { --navy:#14213D; --gold:#B08D3C; --up:#1B7F4C; --down:#B3261E; --muted:#5B6070; }
html, body, [class*="css"], .stMarkdown, .stButton button, .stChatInput textarea { font-family: 'IBM Plex Sans', system-ui, sans-serif; }
h1, h2, h3 { font-family: 'IBM Plex Serif', Georgia, serif !important; color: var(--navy); }
.g-hero { border-bottom: 2px solid var(--gold); padding-bottom: .9rem; margin-bottom: 1.2rem; }
.g-kicker { font-family: 'IBM Plex Mono', monospace; color: var(--gold); letter-spacing: .12em; font-size: .8rem; }
.g-title { font-family: 'IBM Plex Serif', Georgia, serif; font-size: clamp(2.2rem, 5vw, 3.4rem); color: var(--navy); line-height: 1.05; margin: .2rem 0 .5rem; }
.g-sub { color: #3d4253; font-size: 1.05rem; max-width: 60ch; line-height: 1.55; }
.tk { background: var(--navy); color: #F5F1E6; border-radius: 10px; padding: 14px 16px; font-family: 'IBM Plex Mono', monospace; }
.tk .sym { color: var(--gold); font-size: .9rem; letter-spacing: .08em; }
.tk .px { font-size: 2.1rem; font-weight: 600; line-height: 1.1; margin-top: 2px; }
.tk .chg { font-size: .95rem; }
.tk .up { color: #6EE7A8; } .tk .down { color: #FF8A80; } .tk .flat { color: #C9C3B3; }
.tk .tape { margin-top: 6px; font-size: .8rem; color: #C9C3B3; overflow-wrap: anywhere; }
.score { display:inline-block; font-family:'IBM Plex Mono', monospace; font-weight:600; padding:2px 10px; border-radius:6px; color:white; }
.s-hi { background: var(--up); } .s-mid { background: var(--gold); } .s-lo { background: var(--down); }
.qhead { font-family:'IBM Plex Mono', monospace; font-size:.8rem; color: var(--gold); letter-spacing:.06em; text-transform: uppercase; }
.bar { height: 8px; background:#E8E6DF; border-radius: 4px; overflow:hidden; }
.bar > div { height: 100%; background: var(--navy); }
.verdict { font-family:'IBM Plex Serif', Georgia, serif; font-size: 2.2rem; color: var(--navy); line-height:1.15; }
</style>
""",
    unsafe_allow_html=True,
)

# ------------------------------------------------------------------ state
DEFAULTS = dict(
    phase="setup", name="", topics=list(eng.TOPICS), mode="Adaptive", rounds=5,
    messages=[], results=[], asked=[], q=None, difficulty="Easy",
    answer_parts=[], follow_up_used=False, hint_used=False,
    price=eng.START_PRICE, off_topic=0, pending=None, notice=None, report=None,
    human_requests=[],
)
S = st.session_state
for k, v in DEFAULTS.items():
    if k not in S:
        S[k] = v.copy() if isinstance(v, (list, dict)) else v

if "sessions" not in S:  # progress across sessions, restored from the link
    S.sessions = eng.decode_progress(st.query_params.get("p", ""))


def reset_interview():
    for k in ("messages", "results", "asked", "answer_parts", "human_requests"):
        S[k] = []
    S.q, S.follow_up_used, S.hint_used, S.report = None, False, False, None
    S.price, S.off_topic, S.difficulty = eng.START_PRICE, 0, "Easy"


# ------------------------------------------------------------------ AI client
def _secret(name):
    try:
        value = st.secrets.get(name)
    except Exception:
        value = None
    return value or os.environ.get(name)


def api_key():
    key = _secret("GROQ_API_KEY") or _secret("GEMINI_API_KEY")
    return str(key).strip().strip('"').strip("'") if key else None


def provider_name():
    key = api_key()
    return PROVIDER_NAMES[detect_provider(key)] if key else "an AI service"


def client():
    key = api_key()
    if not key:
        raise LLMError("Rhea isn't connected yet. The app owner needs to add GROQ_API_KEY in Streamlit Secrets.")
    if S.get("_client_key") != key:
        S["_client"] = LLMClient(key, _secret("LLM_MODEL"))
        S["_client_key"] = key
    return S["_client"]


# ------------------------------------------------------------------ interview flow
def say(text, kind="text", **data):
    S.messages.append({"role": "assistant", "kind": kind, "content": text, **data})


def ask_next_question(intro=None):
    last = S.results[-1]["score"] if S.results and S.results[-1]["status"] != "skipped" else None
    if S.results:
        S.difficulty = eng.next_difficulty(S.mode, S.difficulty, last)
    q = eng.pick_question(S.topics, S.difficulty, S.asked)
    if q is None:
        finish()
        return
    S.q = q
    S.asked.append(q["id"])
    S.answer_parts, S.follow_up_used, S.hint_used = [], False, False
    n = len(S.results) + 1
    say(q["question"], kind="question", header=f"Q{n} of {S.rounds} · {q['topic']} · {q['difficulty']}", intro=intro)


def start_interview():
    reset_interview()
    S.difficulty = "Easy" if S.mode == "Adaptive" else S.mode
    S.phase = "interview"
    greeting = (f"Hi{(' ' + S.name) if S.name else ''}, I'm **{INTERVIEWER}**, an AI interviewer. "
                f"I'll ask {S.rounds} questions, grade each answer and show you what a strong answer covers. "
                "Type **hint**, **skip** or **I don't know** any time. Every answer moves your stock. Let's begin.")
    say(greeting)
    ask_next_question()


def record(status, score, points=None, misconceptions=None, explanation="", verdict_line=""):
    q = S.q
    before = S.price
    if status != "skipped":
        S.price = eng.move_price(S.price, score)
    S.results.append({
        "id": q["id"], "topic": q["topic"], "difficulty": q["difficulty"], "question": q["question"],
        "status": status, "score": score, "points": points or [], "misconceptions": misconceptions or [],
        "answer": " / ".join(S.answer_parts), "hint_used": S.hint_used,
    })
    change = (S.price - before) / before * 100
    say(verdict_line, kind="feedback", score=score, status=status, change=change, points=points or [],
        misconceptions=misconceptions or [], explanation=explanation, model_answer=q["model_answer"],
        hint_used=S.hint_used)
    if len(S.results) >= S.rounds:
        finish()
    else:
        ask_next_question()


def recent_text():
    out = []
    for m in S.messages[-RECENT_MESSAGES:]:
        who = "CANDIDATE" if m["role"] == "user" else INTERVIEWER.upper()
        out.append(f"{who}: {m['content']}")
    return "\n".join(out)


def handle_message(text, from_button=False):
    """One candidate message -> one model call -> code decides what happens next."""
    if not from_button:
        S.messages.append({"role": "user", "kind": "text", "content": text})
    try:
        data = client().generate_json(
            GRADER_SYSTEM,
            grading_prompt(S.q, S.answer_parts, text, S.follow_up_used, S.hint_used, recent_text()),
            temperature=0.2,
        )
    except LLMError as e:
        S.notice = ("error", e.user_message, e.detail)
        if not from_button:
            S.messages.pop()  # let them resend the same answer
        return
    except BadOutput:
        say("Sorry, I lost my train of thought. Could you give me that answer again?")
        return

    intent = data.get("intent", "answer")
    reply = str(data.get("reply", "")).strip()

    if intent == "vague" and not S.follow_up_used:
        S.answer_parts.append(text)
        S.follow_up_used = True
        say(reply or "Go one level deeper. What exactly would you walk me through?")
    elif intent in ("answer", "vague"):
        S.answer_parts.append(text)
        points = eng.clean_points(S.q, data.get("points"))
        misconceptions = [str(m) for m in (data.get("misconceptions") or [])][:3]
        score = eng.score_answer(points, misconceptions, S.hint_used)
        record("answered", score, points, misconceptions, str(data.get("explanation", "")), reply)
    elif intent == "dont_know":
        record("dont_know", 0.0, eng.clean_points(S.q, []), [], str(data.get("explanation", "")),
               "No problem. Saying so beats bluffing in a real interview. Here's what to cover.")
    elif intent == "skip":
        record("skipped", 0.0, [], [], "", "Skipped. It won't count toward your average, and your stock stays flat.")
    elif intent == "hint":
        if S.hint_used:
            say("You've had your hint for this one. Give it your best shot, or type **skip**.")
        else:
            S.hint_used = True
            say((reply or "Think about which statement each item hits first.") + f"  \n*Hint used: −{eng.HINT_PENALTY:g} point.*")
    elif intent in ("off_topic", "injection"):
        S.off_topic += 1
        say((reply or "Let's keep this to the interview.") + f"  \n\n**Back to the question:** {S.q['question']}")
    else:  # clarify
        say(reply or f"Take it at face value: {S.q['question']}")


def finish():
    S.phase = "report"
    overall, by_topic = eng.summarize(S.results)
    answered = [r for r in S.results if r["status"] != "skipped"]
    S.report = {"overall": overall, "by_topic": by_topic, "ai": None}
    if answered:
        try:
            S.report["ai"] = client().generate_json(REPORT_SYSTEM, report_prompt(S.results, overall, by_topic), 0.3)
        except (LLMError, BadOutput):
            S.report["ai"] = None
        S.sessions.append(eng.session_record(datetime.now().strftime("%d %b"), overall, len(answered),
                                             S.price, by_topic))
        st.query_params["p"] = eng.encode_progress(S.sessions)


# ------------------------------------------------------------------ rendering helpers
def score_badge(score):
    cls = "s-hi" if score >= 7 else "s-mid" if score >= 4 else "s-lo"
    return f'<span class="score {cls}">{score:g} / 10</span>'


def render_message(m):
    avatar = "🧑‍💼" if m["role"] == "user" else "📈"
    with st.chat_message(m["role"], avatar=avatar):
        if m["kind"] == "question":
            if m.get("intro"):
                st.markdown(m["intro"])
            st.markdown(f'<div class="qhead">{html.escape(m["header"])}</div>', unsafe_allow_html=True)
            st.markdown(f"**{m['content']}**")
        elif m["kind"] == "feedback":
            if m["status"] == "skipped":
                st.markdown(m["content"])
                return
            arrow = "▲" if m["change"] > 0 else "▼" if m["change"] < 0 else "▬"
            st.markdown(f"{score_badge(m['score'])} &nbsp; Stock {arrow} {m['change']:+.1f}%", unsafe_allow_html=True)
            if m["content"]:
                st.markdown(m["content"])
            if m["points"] and m["status"] == "answered":
                icons = {"hit": "✅", "partial": "🟡", "miss": "❌"}
                st.markdown("\n".join(f"- {icons[p['status']]} {p['point']}" for p in m["points"]))
            for mc in m["misconceptions"]:
                st.markdown(f"⚠️ **Watch out:** {mc}")
            if m.get("hint_used"):
                st.caption(f"Hint used (−{eng.HINT_PENALTY:g}).")
            if m["explanation"]:
                st.markdown(f"**Why it matters:** {m['explanation']}")
            with st.expander("What a strong answer sounds like"):
                st.markdown(m["model_answer"])
        else:
            st.markdown(m["content"])


def ticker_html(price, start=eng.START_PRICE):
    change = (price - start) / start * 100
    cls = "up" if change > 0 else "down" if change < 0 else "flat"
    arrow = "▲" if change > 0 else "▼" if change < 0 else "▬"
    tape = " ".join(
        f"{r['id']}{'▲' if r['score'] > 5 else '▼' if r['score'] < 5 else '▬'}" for r in S.results if r["status"] != "skipped"
    ) or "awaiting first trade"
    return (f'<div class="tk"><div class="sym">{html.escape(eng.ticker_symbol(S.name))} · LIVE</div>'
            f'<div class="px">₹{price:,.2f}</div><div class="chg {cls}">{arrow} {change:+.1f}% today</div>'
            f'<div class="tape">{html.escape(tape)}</div></div>')


def topic_bars(by_topic):
    for t, v in sorted(by_topic.items(), key=lambda x: -x[1]):
        st.markdown(f"<div style='display:flex;justify-content:space-between;font-size:.9rem'><span>{html.escape(t)}</span>"
                    f"<span style=\"font-family:'IBM Plex Mono'\">{v:g}</span></div>"
                    f"<div class='bar'><div style='width:{v * 10:.0f}%'></div></div>", unsafe_allow_html=True)


def history_chart():
    if len(S.sessions) >= 2:
        st.markdown("**Your progress across sessions**")
        st.line_chart({"Average score": [s["a"] for s in S.sessions]}, height=180)
        st.caption("Sessions: " + " · ".join(s["d"] for s in S.sessions))


# ------------------------------------------------------------------ sidebar
def queue(action):
    S.pending = action


with st.sidebar:
    if S.phase in ("interview", "report"):
        st.markdown(ticker_html(S.price), unsafe_allow_html=True)
        st.write("")
    if S.phase == "interview":
        st.progress(len(S.results) / S.rounds, text=f"Question {min(len(S.results) + 1, S.rounds)} of {S.rounds}")
        _, live = eng.summarize(S.results)
        if live:
            topic_bars(live)
        c1, c2 = st.columns(2)
        c1.button("💡 Hint", on_click=queue, args=("hint",), disabled=S.hint_used, width="stretch")
        c2.button("⏭️ Skip", on_click=queue, args=("skip",), width="stretch")
        st.button("End interview now", on_click=queue, args=("end",), disabled=not S.results, width="stretch")
        st.divider()
        if st.button("🙋 Request a human mock interview", width="stretch"):
            ref = f"HM-{random.randint(1000, 9999)}"
            weakest = min(live, key=live.get) if live else "general"
            S.human_requests.append(ref)
            st.toast(f"Request {ref} sent. A human mentor will focus on {weakest}.")
        if S.human_requests:
            st.caption("Human mock requests: " + ", ".join(S.human_requests))

    with st.expander("💾 Save or restore progress"):
        st.caption("Your progress is saved in this page's link after each interview. Bookmark it to keep your history, "
                   "or download a progress file.")
        st.download_button("Download progress file", json.dumps(S.sessions, indent=1),
                           file_name="gauntlet_progress.json", disabled=not S.sessions, width="stretch")
        up = st.file_uploader("Restore from file", type=["json"], label_visibility="collapsed")
        if up is not None and not S.get("_restored"):
            try:
                data = json.loads(up.getvalue())
                restored = eng.decode_progress(eng.encode_progress(data if isinstance(data, list) else []))
                if not restored:
                    raise ValueError
                S.sessions, S["_restored"] = restored, True
                st.query_params["p"] = eng.encode_progress(S.sessions)
                st.success(f"Restored {len(restored)} sessions.")
            except Exception:
                st.error("That doesn't look like a Gauntlet progress file.")

    st.divider()
    st.caption(f"{INTERVIEWER} is an AI interviewer powered by {provider_name()}. It can misjudge an answer, so treat "
               f"scores as practice feedback, not a hiring decision. Your answers are sent to {provider_name()}'s API "
               "to be graded; nothing is stored on a server. Don't share personal details.")
    c = S.get("_client")
    if c and c.active_model:
        st.caption(f"Model: {c.active_model}")

# ------------------------------------------------------------------ notices
if S.notice:
    kind, text, *detail = S.notice
    st.error(text)
    if detail and detail[0]:
        with st.expander("Technical details"):
            st.code(str(detail[0])[-2000:])
    S.notice = None

# ================================================================== SETUP
if S.phase == "setup":
    st.markdown(
        '<div class="g-hero"><div class="g-kicker">IB INTERVIEW PRACTICE</div>'
        '<div class="g-title">The Gauntlet</div>'
        f'<div class="g-sub">{INTERVIEWER}, your AI interviewer, fires real investment banking questions at you, grades every '
        'answer against what bankers listen for, and shows you what a strong answer covers. '
        'Every answer moves your candidate stock. Make it climb.</div></div>',
        unsafe_allow_html=True,
    )
    if S.sessions:
        best = max(s["a"] for s in S.sessions)
        last = S.sessions[-1]
        st.info(f"Welcome back. {len(S.sessions)} session(s) on record. Last score **{last['a']}/10**, best **{best}/10**.")
        history_chart()

    left, right = st.columns([3, 2], gap="large")
    with left:
        name = st.text_input("Your first name (for your ticker symbol)", value=S.name, max_chars=20,
                             placeholder="e.g. Aditya")
        topics = st.multiselect("Topics", eng.TOPICS, default=S.topics)
    with right:
        mode = st.selectbox("Difficulty", ["Adaptive", "Easy", "Medium", "Hard"],
                            index=["Adaptive", "Easy", "Medium", "Hard"].index(S.mode),
                            help="Adaptive starts easy, goes up after a score of 7+, down after 4 or less.")
        rounds = st.radio("Questions", [5, 8, 10], index=[5, 8, 10].index(S.rounds), horizontal=True)

    problems = []
    if not topics:
        problems.append("Pick at least one topic.")
    if not api_key():
        problems.append("Rhea isn't connected yet. The app owner needs to add GROQ_API_KEY in Streamlit Secrets.")
    if topics and sum(1 for q in eng.QUESTIONS if q["topic"] in topics) < rounds:
        problems.append("Not enough questions in those topics. Add a topic or choose fewer questions.")

    if st.button("Enter the Gauntlet", type="primary", disabled=bool(problems)):
        S.name, S.topics, S.mode, S.rounds = name.strip(), topics, mode, rounds
        start_interview()
        st.rerun()
    for p in problems:
        st.caption(f"• {p}")

# ================================================================== INTERVIEW
elif S.phase == "interview":
    typed = st.chat_input("Your answer...")

    action, S.pending = S.pending, None
    if action == "hint" and not S.hint_used:
        with st.spinner(f"{INTERVIEWER} is thinking..."):
            S.messages.append({"role": "user", "kind": "text", "content": "💡 Hint, please."})
            handle_message("hint please", from_button=True)
        st.rerun()
    elif action == "skip":
        S.messages.append({"role": "user", "kind": "text", "content": "⏭️ Skip"})
        record("skipped", 0.0, [], [], "", "Skipped. It won't count toward your average, and your stock stays flat.")
        st.rerun()
    elif action == "end":
        finish()
        st.rerun()

    if typed is not None:
        text = typed.strip()
        if not text:
            st.toast("Type an answer first, or use Skip.")
        elif len(text) > MAX_ANSWER_CHARS:
            st.toast(f"Keep it under {MAX_ANSWER_CHARS} characters. Interviewers love concise answers.")
        else:
            with st.spinner(f"{INTERVIEWER} is listening..."):
                handle_message(text)
        st.rerun()

    st.markdown(f'<div class="g-kicker">THE GAUNTLET · {html.escape(" · ".join(S.topics) if len(S.topics) < 4 else "MIXED TOPICS")}'
                f' · {S.mode.upper()}</div>', unsafe_allow_html=True)
    for m in S.messages:
        render_message(m)
    if action == "end" or S.phase == "report":
        st.rerun()

# ================================================================== REPORT
elif S.phase == "report":
    r = S.report
    answered = [x for x in S.results if x["status"] != "skipped"]
    change = (S.price - eng.START_PRICE) / eng.START_PRICE * 100

    st.markdown('<div class="g-kicker">INTERVIEW DEBRIEF</div>', unsafe_allow_html=True)
    if not answered:
        st.markdown('<div class="verdict">No answers to grade.</div>', unsafe_allow_html=True)
        st.write("You skipped every question, so there's nothing to score. Run it back when you're ready.")
    else:
        st.markdown(f'<div class="verdict">{html.escape(eng.verdict(r["overall"]))}</div>', unsafe_allow_html=True)
        m1, m2, m3 = st.columns(3)
        m1.metric("Average score", f"{r['overall']:g} / 10")
        m2.metric("Closing stock price", f"₹{S.price:,.2f}", f"{change:+.1f}%")
        m3.metric("Answered", f"{len(answered)} of {len(S.results)}")

        ai = r.get("ai") or {}
        left, right = st.columns([3, 2], gap="large")
        with left:
            if ai.get("headline"):
                st.markdown(f"#### {ai['headline']}")
            for title, key in [("What went well", "strengths"), ("Knowledge gaps", "gaps"), ("Drills before next time", "drills")]:
                if ai.get(key):
                    st.markdown(f"**{title}**")
                    st.markdown("\n".join(f"- {x}" for x in ai[key]))
            if not ai:
                missed = [p["point"] for x in answered for p in x["points"] if p["status"] == "miss"][:5]
                st.markdown("**Points to revise**")
                st.markdown("\n".join(f"- {p}" for p in missed) or "- Nothing major. Try a harder difficulty.")
        with right:
            st.markdown("**Score by topic**")
            topic_bars(r["by_topic"])
            st.write("")
            history_chart()

        st.markdown("### Question by question")
        for x in S.results:
            label = "skipped" if x["status"] == "skipped" else f"{x['score']:g}/10"
            with st.expander(f"{x['topic']} · {x['difficulty']} · {label}: {x['question']}"):
                if x["answer"]:
                    st.markdown(f"**Your answer:** {x['answer']}")
                if x["points"]:
                    icons = {"hit": "✅", "partial": "🟡", "miss": "❌"}
                    st.markdown("\n".join(f"- {icons[p['status']]} {p['point']}" for p in x["points"]))
                st.markdown(f"**Strong answer:** {eng.BY_ID[x['id']]['model_answer']}")
        st.caption("Progress saved to this page's link. Bookmark it to track your scores over time.")

    c1, c2 = st.columns(2)
    if c1.button("Run it back (same settings)", type="primary", width="stretch"):
        start_interview()
        st.rerun()
    if c2.button("Change settings", width="stretch"):
        reset_interview()
        S.phase = "setup"
        st.rerun()

st.caption("Practice tool with sample questions. AI feedback can be wrong; verify key concepts with your course materials.")
