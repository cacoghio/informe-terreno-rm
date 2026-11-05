"""run_agent: el agente Claude (Claude Agent SDK) sobre la lista cerrada de diez tools de ``agent_tools`` (spec sec. 7).

Tres capas independientes, porque ``allowed_tools`` solo NO restringe (solo pre-aprueba):
1. ``tools=[]`` y ``setting_sources=[]`` + ``strict_mcp_config``: el modelo ni ve las herramientas integradas, ni carga
   ~/.claude (hooks, plugins, skills, CLAUDE.md), ni otros servidores MCP;
2. ``disallowed_tools`` explicito (Bash, escritura/edicion de archivos, lectura de rutas, web, sub-agentes...);
3. ``can_use_tool`` que DENIEGA POR DEFECTO y solo deja pasar las diez tools ``mcp__terreno__*`` con entrada valida
   (sin rutas, URL, CRS ni campos extra). Cada intento queda en ``agent_log.json``.
Las tools corren en proceso como servidor MCP del SDK; el agente no recibe credenciales ni rutas.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from terreno.agent_tools import (
    HUMAN_REVIEW_NAME,
    MCP_PREFIX,
    MCP_SERVER_NAME,
    TOOL_NAMES,
    AgentSession,
    build_sdk_tools,
    decide_tool_use,
)
from terreno.pipeline import REJECTION_NAME
from terreno.report import REPORT_NAME

DEFAULT_MODEL = "sonnet"
DEFAULT_MAX_TURNS = 24
DISALLOWED_BUILTINS = [
    "Bash", "BashOutput", "KillShell", "PowerShell", "Write", "Edit", "MultiEdit", "NotebookEdit", "Read", "Glob", "Grep",
    "WebFetch", "WebSearch", "Task", "Agent", "TodoWrite", "ExitPlanMode", "Skill", "SlashCommand", "ToolSearch", "Monitor",
    "TaskStop", "SendMessage", "AskUserQuestion", "EnterWorktree", "ExitWorktree",
]

SYSTEM_PROMPT = f"""Eres el operador de un pipeline de informes preliminares de terreno en Chile. No eres topografo ni calculas nada:
solo ejecutas, EN ORDEN, las diez tools del servidor '{MCP_SERVER_NAME}' y las mismas reglas del codigo deciden todo lo demas.

Reglas inviolables:
- Solo existen esas tools. No hay shell, archivos ni web. No intentes otras: se deniegan y quedan registradas.
- No eliges el CRS, la zona UTM, la resolucion, formulas ni umbrales: ninguna tool los acepta. No escribes cifras ni texto del
  informe: el PDF sale del manifiesto. Tu respuesta final es un parrafo breve que solo nombra el resultado y los archivos,
  sin inventar numeros.
- El poligono se copia EXACTAMENTE del pedido (GeoJSON Polygon, lon/lat EPSG:4326) en fetch_dem. Si el pedido no trae un
  poligono valido, o esta fuera de Chile continental, o no se puede cumplir con estas tools, llama reject_request con el motivo
  y nada mas: no adivines ni completes datos.
- Si cualquier tool devuelve status 'rechazado' o 'detenido', el pipeline termino: no reintentes ni sigas; resume el motivo.

Procedimiento: fetch_dem -> reproject -> compute_terrain -> compare_sources (si responde no_disponible, sigue) ->
score_confidence -> (si human_review_required es true, llama request_human_review con un motivo breve) -> build_glb ->
render_pdf -> publish_viewer. Cada tool se llama una sola vez. Un status 'orden_invalido' o 'entrada_invalida' es tu error:
corrigelo una vez; no es un rechazo del sitio."""


@dataclass
class AgentRunResult:
    out_dir: Path
    outcome: str  # "informe" | "rechazado" | "incompleto" | "error"
    final_text: str = ""
    tool_uses: list = field(default_factory=list)
    tool_calls: list = field(default_factory=list)
    forbidden_attempts: list = field(default_factory=list)
    human_review: bool = False
    result_hash: str | None = None
    rejection: dict | None = None
    wall_seconds: float = 0.0
    duration_ms: int | None = None
    duration_api_ms: int | None = None
    num_turns: int | None = None
    usage: dict = field(default_factory=dict)
    total_cost_usd: float | None = None
    model_usage: dict | None = None
    is_error: bool = False
    error: str | None = None


def make_can_use_tool(session: AgentSession) -> Callable:
    """Callback ``can_use_tool``: politica ``decide_tool_use`` (deniega por defecto) + registro de cada intento."""
    from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny

    async def can_use_tool(tool_name: str, input_data: dict[str, Any], context) -> Any:
        d = decide_tool_use(tool_name, input_data)
        session.log_permission(tool_name, d.allowed, d.message)
        if d.allowed:
            return PermissionResultAllow()
        return PermissionResultDeny(message=d.message, interrupt=False)

    return can_use_tool


def build_agent_options(session: AgentSession, *, model: str | None = DEFAULT_MODEL, max_turns: int = DEFAULT_MAX_TURNS,
                        max_budget_usd: float | None = None):
    from claude_agent_sdk import ClaudeAgentOptions, create_sdk_mcp_server

    server = create_sdk_mcp_server(MCP_SERVER_NAME, tools=build_sdk_tools(session))
    return ClaudeAgentOptions(
        system_prompt=SYSTEM_PROMPT,
        tools=[],  # sin herramientas integradas
        mcp_servers={MCP_SERVER_NAME: server},
        strict_mcp_config=True,
        # allowed_tools VACIO a proposito: pre-aprobar tools saltaria can_use_tool (que valida entrada y deja registro);
        # asi cada llamada, tambien a las diez, pasa por la politica que deniega por defecto
        allowed_tools=[],
        disallowed_tools=list(DISALLOWED_BUILTINS),
        can_use_tool=make_can_use_tool(session),
        permission_mode="default",
        setting_sources=[],
        skills=[],
        cwd=str(session.out_dir),
        model=model,
        max_turns=max_turns,
        max_budget_usd=max_budget_usd,
    )


def summarize_messages(messages) -> dict:
    """Resume la conversacion: tools que el modelo intento usar, texto final, tiempos, tokens y costo."""
    from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock, ToolUseBlock

    tool_uses: list[str] = []
    last_text = ""
    info: dict = {}
    for m in messages:
        if isinstance(m, AssistantMessage):
            txt = "".join(b.text for b in m.content if isinstance(b, TextBlock))
            if txt.strip():
                last_text = txt.strip()
            tool_uses += [b.name for b in m.content if isinstance(b, ToolUseBlock)]
        elif isinstance(m, ResultMessage):
            info = {"duration_ms": m.duration_ms, "duration_api_ms": m.duration_api_ms, "num_turns": m.num_turns,
                    "total_cost_usd": m.total_cost_usd, "usage": dict(m.usage or {}), "model_usage": m.model_usage,
                    "is_error": bool(m.is_error), "result": m.result, "errors": m.errors}
    return {"tool_uses": tool_uses, "final_text": (info.get("result") or last_text or ""), "duration_ms": info.get("duration_ms"),
            "duration_api_ms": info.get("duration_api_ms"), "num_turns": info.get("num_turns"),
            "total_cost_usd": info.get("total_cost_usd"), "usage": info.get("usage", {}), "model_usage": info.get("model_usage"),
            "is_error": info.get("is_error", False), "errors": info.get("errors")}


async def _drive(request: str, options, client_factory: Callable | None) -> list:
    from claude_agent_sdk import ClaudeSDKClient

    factory = client_factory or (lambda o: ClaudeSDKClient(options=o))
    msgs: list = []
    async with factory(options) as client:
        await client.query(request)  # modo streaming: requisito de can_use_tool
        async for m in client.receive_response():
            msgs.append(m)
    return msgs


def run_agent(request: str, out_dir: Path, *, session: AgentSession | None = None, model: str | None = DEFAULT_MODEL,
              max_turns: int = DEFAULT_MAX_TURNS, max_budget_usd: float | None = None, client_factory: Callable | None = None,
              **session_kwargs) -> AgentRunResult:
    """Corre el agente sobre ``request`` (texto con el poligono GeoJSON) y escribe todo en ``out_dir``.

    ``session_kwargs`` (site_name, fetch, fetch_aux, ... ) van a ``AgentSession``. No hay camino por el que el agente escriba
    fuera de ``out_dir`` ni ejecute algo que no sea una de las diez tools. La autenticacion es la del login de Claude Code de la
    maquina: este modulo nunca lee ni fija claves.
    """
    session = session or AgentSession(Path(out_dir), **session_kwargs)
    options = build_agent_options(session, model=model, max_turns=max_turns, max_budget_usd=max_budget_usd)
    t0 = time.perf_counter()
    msgs: list = []
    error = None
    try:
        msgs = asyncio.run(_drive(request, options, client_factory))
    except Exception as exc:  # CLI no encontrado, login invalido, proceso caido: se informa, no se oculta
        error = f"{type(exc).__name__}: {str(exc)[:500]}"
    wall = time.perf_counter() - t0
    info = summarize_messages(msgs)
    session.finalize()

    out = session.out_dir
    if error or info["is_error"]:
        outcome = "error"
    elif session.rejected is not None:
        outcome = "rechazado"
    elif (out / REPORT_NAME).exists() and session.manifest is not None:
        outcome = "informe"
    else:
        outcome = "incompleto"
    return AgentRunResult(
        out_dir=out, outcome=outcome, final_text=info["final_text"], tool_uses=info["tool_uses"], tool_calls=list(session.calls),
        forbidden_attempts=list(session.forbidden_attempts), human_review=session.human_review,
        result_hash=session.manifest["result_hash"] if session.manifest else None, rejection=session.rejected,
        wall_seconds=round(wall, 2), duration_ms=info["duration_ms"], duration_api_ms=info["duration_api_ms"],
        num_turns=info["num_turns"], usage=info["usage"], total_cost_usd=info["total_cost_usd"], model_usage=info["model_usage"],
        is_error=bool(error or info["is_error"]), error=error or ("; ".join(info["errors"]) if info.get("errors") else None))
