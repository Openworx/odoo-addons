# -*- coding: utf-8 -*-
"""Built-in tools the agent uses to steer the conversation itself.

Both tools only ever mutate ``ctx.state`` (the engine persists it back once
the call succeeds) or set ``ctx.user_input_request`` (the engine pauses the
turn for it): neither ever touches the session/channel directly. Skills and
tools are readable by any AI user, so the requesting user's own rights are
enough for them; only the chat's skill list is read through the (system-only)
session, with ``sudo()``.
"""
from __future__ import annotations

from ..engine.html_output import render_ai_markdown
from ..engine.tools_registry import ToolResult, builtin_tool

_MIN_CHOICES = 2
_MAX_CHOICES = 4


def _chat_session(ctx):
    """The chat's session, or ``None`` (direct mode)."""
    if not ctx.session_id:
        return None
    # sessions are system-only; the session id comes from the engine, never from the model
    return ctx.env['ow.ai.session'].sudo().browse(ctx.session_id).exists() or None


def _available_skills(ctx, session):
    """The skills the chat offers (its web search switch applies), else the agent's (direct mode)."""
    if session:
        return session._get_available_skills().sudo(False)
    return ctx.env['ow.ai.agent'].browse(ctx.agent_id)._get_available_skills()


def _callable_tools(session, tools):
    """The ``tools`` the model can call: those the chat offers (its web search
    switch applies), else the active ones (direct mode)."""
    if session:
        return session._offered_tools(tools)
    return tools.filtered('active')


@builtin_tool('interaction.load_skills')
def load_skills(ctx, skill_ids=None, **_kw):
    """Load the instructions and tools of the requested skills.

    Every active tool of a loaded skill joins the session's tools; the answer
    names only those the model can call now (a skill mixing web and other
    tools, loaded while web search is off, lists only its other tools).
    """
    session = _chat_session(ctx)
    allowed = _available_skills(ctx, session)
    requested_ids = list(dict.fromkeys(skill_ids or []))
    loaded = allowed.filtered(lambda skill: skill.id in requested_ids)
    not_available = [skill_id for skill_id in requested_ids if skill_id not in allowed.ids]

    loaded_skills = list(ctx.state.get('loaded_skills') or [])
    ctx.state['loaded_skills'] = list(dict.fromkeys(loaded_skills + loaded.ids))
    available_tools = list(ctx.state.get('available_tools') or [])
    new_tools = loaded.tool_ids.filtered('active')
    ctx.state['available_tools'] = list(dict.fromkeys(available_tools + new_tools.ids))

    sections = []
    for skill in loaded:
        section = f"## Skill: {skill.name}\n{skill.instructions or ''}"
        tool_names = _callable_tools(session, skill.tool_ids).mapped('tool_name')
        if tool_names:
            section += f"\n\nTools now available: {', '.join(tool_names)}"
        sections.append(section)
    if not_available:
        sections.append(
            "Not available: " + ', '.join(str(skill_id) for skill_id in not_available))
    response = '\n\n'.join(sections) if sections else "No skills were loaded."

    names = ', '.join(loaded.mapped('name'))
    return ToolResult(
        response=response,
        summary={'icon': 'article', 'text': f"Loaded skills: {names}" if names else "No skills loaded"})


@builtin_tool('interaction.ask_user_question')
def ask_user_question(ctx, question=None, choices=None, multi_select=False, allow_free_text=False, **_kw):
    """Ask the user a clarifying multiple-choice (or free-text) question."""
    choices = choices or []
    cleaned = list(dict.fromkeys(choice.strip() for choice in choices if isinstance(choice, str) and choice.strip()))
    if len(cleaned) != len(choices) or not (_MIN_CHOICES <= len(cleaned) <= _MAX_CHOICES):
        raise ValueError(f"choices must be {_MIN_CHOICES} to {_MAX_CHOICES} non-empty, distinct options")

    ctx.user_input_request = {
        'type': 'question',
        'body': str(render_ai_markdown(question or '', base_url=ctx.env.user.get_base_url())),
        'choices': cleaned,
        'multi_select': bool(multi_select),
        'allow_free_text': bool(allow_free_text),
    }
    return ToolResult(
        response="Waiting for the user's answer.",
        summary={'icon': 'help', 'text': "Asked the user a question"})
