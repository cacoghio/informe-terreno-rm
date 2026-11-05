"""Task 13: capa de agente con lista cerrada de tools. Todo offline: ningun test llama al modelo."""
import asyncio
import json
from pathlib import Path

import pytest
from shapely.geometry import box, mapping

from terreno.agent import (
    DISALLOWED_BUILTINS,
    AgentRunResult,
    build_agent_options,
    make_can_use_tool,
    summarize_messages,
)
from terreno.agent_tools import (
    HUMAN_REVIEW_NAME,
    MCP_PREFIX,
    OUTPUT_SCHEMAS,
    SCHEMAS,
    TOOL_NAMES,
    TOOLS,
    AgentSession,
    build_sdk_tools,
    decide_tool_use,
)
from terreno.manifest import hash_manifest, read_manifest, result_hash
from terreno.pipeline import REJECTION_NAME, run_pipeline
from tests.test_report import pdf_text
from tests.test_pipeline import LAT0, LON0, POLY, SHIFT, TINY, aux_ok, fetchers

TEN = ["fetch_dem", "reproject", "compute_terrain", "compare_sources", "score_confidence", "build_glb", "render_pdf",
       "publish_viewer", "reject_request", "request_human_review"]


def gj(poly):
    return json.loads(json.dumps(mapping(poly)))


def session(tmp_path, name="s", **kw):
    kw.setdefault("fetch", fetchers(tmp_path))
    kw.setdefault("fetch_aux", aux_ok)
    kw.setdefault("shift_fn", SHIFT)
    return AgentSession(tmp_path / name, **kw)


def run_all(s, poly=POLY, *, glb=True):
    steps = [("fetch_dem", {"polygon": gj(poly)}), ("reproject", {}), ("compute_terrain", {}), ("compare_sources", {}),
             ("score_confidence", {})]
    if glb:
        steps += [("build_glb", {}), ("render_pdf", {}), ("publish_viewer", {})]
    out = {}
    for name, args in steps:
        out[name] = TOOLS[name](s, args)
        assert out[name]["status"] in ("ok", "no_disponible"), (name, out[name])
    return out


# ---------- registro ----------

def test_registry_has_exactly_ten_tools(tmp_path):
    assert sorted(TOOLS) == sorted(TEN) and len(TOOLS) == 10 and tuple(TOOL_NAMES) == tuple(TOOLS)
    assert set(SCHEMAS) == set(OUTPUT_SCHEMAS) == set(TEN)
    sdk_tools = build_sdk_tools(session(tmp_path))  # las mismas diez, como tools MCP en proceso
    assert sorted(t.name for t in sdk_tools) == sorted(TEN)
    for t in TEN:
        assert callable(TOOLS[t])
        assert SCHEMAS[t]["additionalProperties"] is False and OUTPUT_SCHEMAS[t]["additionalProperties"] is False


# ---------- can_use_tool ----------

@pytest.mark.parametrize("name,args", [
    ("Bash", {"command": "ls"}), ("Write", {"file_path": "C:/x.txt", "content": "x"}), ("Edit", {"file_path": "C:/x"}),
    ("Read", {"file_path": "C:/Windows/win.ini"}), ("Glob", {"pattern": "*"}), ("Grep", {"pattern": "x"}),
    ("WebFetch", {"url": "https://example.com"}), ("WebSearch", {"query": "x"}), ("Task", {"prompt": "x"}),
    ("Agent", {"prompt": "x"}), ("NotebookEdit", {}), ("TodoWrite", {}), ("mcp__otro__fetch_dem", {}),
    ("fetch_dem", {}), (MCP_PREFIX + "execute_code", {"code": "print(1)"}), (MCP_PREFIX + "fetch_dem ", {}),
    ("", {}), (MCP_PREFIX, {}),
])
def test_can_use_tool_denies_unlisted_tool(name, args):
    d = decide_tool_use(name, args)
    assert d.allowed is False and d.message


def test_can_use_tool_allows_only_the_ten_with_valid_input():
    valid = {"fetch_dem": {"polygon": gj(POLY)}, "build_glb": {"exaggeration": 2.0},
             "reject_request": {"reason": "fuera de Chile"}, "request_human_review": {"reason": "nivel C"}}
    for t in TEN:
        assert decide_tool_use(MCP_PREFIX + t, valid.get(t, {})).allowed is True, t


@pytest.mark.parametrize("args", [
    {"path": "C:/Windows/x.pdf"}, {"file_path": "../../x"}, {"out_dir": "C:/"}, {"cwd": "C:/"}, {"url": "https://x"},
    {"epsg": 32718}, {"crs": "EPSG:4326"}, {"polygon": {"nested": {"path": "x"}}},
])
def test_can_use_tool_denies_paths_and_extra_fields_even_on_listed_tools(args):
    for t in ("render_pdf", "publish_viewer", "reproject", "fetch_dem"):
        assert decide_tool_use(MCP_PREFIX + t, args).allowed is False, (t, args)


def test_async_can_use_tool_callback_denies_by_default_and_logs(tmp_path):
    s = session(tmp_path)
    cb = make_can_use_tool(s)
    from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny, ToolPermissionContext

    ctx = ToolPermissionContext()
    r1 = asyncio.run(cb("Bash", {"command": "rm -rf /"}, ctx))
    r2 = asyncio.run(cb(MCP_PREFIX + "reproject", {}, ctx))
    r3 = asyncio.run(cb(MCP_PREFIX + "render_pdf", {"path": "C:/x.pdf"}, ctx))
    assert isinstance(r1, PermissionResultDeny) and isinstance(r2, PermissionResultAllow) and isinstance(r3, PermissionResultDeny)
    assert [(a["tool"], a["allowed"]) for a in s.audit] == [("Bash", False), (MCP_PREFIX + "reproject", True),
                                                             (MCP_PREFIX + "render_pdf", False)]
    assert [d["tool"] for d in s.forbidden_attempts] == ["Bash", MCP_PREFIX + "render_pdf"]


def test_agent_options_are_locked_down(tmp_path):
    s = session(tmp_path)
    o = build_agent_options(s)
    for t in ("Bash", "Write", "Edit", "MultiEdit", "NotebookEdit", "Read", "Glob", "Grep", "WebFetch", "WebSearch", "Task",
              "Agent", "TodoWrite"):
        assert t in o.disallowed_tools and t in DISALLOWED_BUILTINS, t
    assert o.can_use_tool is not None
    assert o.tools == [] and o.setting_sources == [] and o.strict_mcp_config is True  # ni built-ins ni ~/.claude
    assert o.permission_mode not in ("bypassPermissions", "acceptEdits", "auto", "dontAsk")
    assert list(o.mcp_servers) == ["terreno"] and o.mcp_servers["terreno"]["type"] == "sdk"
    assert all(t.startswith(MCP_PREFIX) for t in o.allowed_tools) and len(o.allowed_tools) <= 10
    assert o.max_turns and 5 <= o.max_turns <= 40
    assert Path(o.cwd) == s.out_dir
    assert "Bash" not in o.system_prompt and "reject_request" in o.system_prompt


# ---------- esquemas ----------

@pytest.mark.parametrize("name", TEN)
def test_tool_schema_rejects_extra_fields(tmp_path, name):
    s = session(tmp_path, name=f"x_{name}")
    base = {"fetch_dem": {"polygon": gj(POLY)}, "reject_request": {"reason": "motivo valido"},
            "request_human_review": {"reason": "motivo valido"}}.get(name, {})
    r = TOOLS[name](s, {**base, "campo_extra": 1})
    assert r["status"] == "entrada_invalida" and "campo_extra" in r["reason"]
    assert s.rejected is None and not (s.out_dir / REJECTION_NAME).exists()  # una entrada mal formada no es un rechazo del sitio


@pytest.mark.parametrize("name,args", [
    ("fetch_dem", {"polygon": "no es un objeto"}), ("fetch_dem", {"polygon": {"type": "Point", "coordinates": [0, 0]}}),
    ("fetch_dem", {"polygon": gj(POLY), "source": "srtm"}), ("fetch_dem", {}),
    ("build_glb", {"exaggeration": "2"}), ("build_glb", {"exaggeration": 0.2}), ("build_glb", {"exaggeration": 99}),
    ("reject_request", {"reason": 5}), ("reject_request", {"reason": ""}), ("reject_request", {}),
    ("request_human_review", {"reason": ["x"]}), ("request_human_review", {"reason": "x" * 501}),
])
def test_tool_schema_rejects_wrong_types(tmp_path, name, args):
    s = session(tmp_path)
    r = TOOLS[name](s, args)
    assert r["status"] == "entrada_invalida", r
    assert not any((s.out_dir / f).exists() for f in (REJECTION_NAME, HUMAN_REVIEW_NAME, "manifest.json"))


def test_tool_output_is_validated_against_its_schema(tmp_path, monkeypatch):
    import terreno.agent_tools as at

    s = session(tmp_path)
    assert TOOLS["fetch_dem"](s, {"polygon": gj(POLY)})["status"] == "ok"
    monkeypatch.setitem(at._HANDLERS, "reproject", lambda sess, args: {"status": "ok", "epsg": 32719, "algo_no_declarado": 1})
    r = TOOLS["reproject"](s, {})
    assert r["status"] == "error" and "salida" in r["reason"] and "reproject" not in s.done


def test_non_object_arguments_are_invalid(tmp_path):
    s = session(tmp_path)
    for bad in (None, [], "x", 3):
        assert TOOLS["reproject"](s, bad)["status"] == "entrada_invalida"


# ---------- rechazo de primera clase ----------

def test_reject_request_is_logged_and_stops_pipeline(tmp_path):
    s = session(tmp_path)
    r = TOOLS["reject_request"](s, {"reason": "poligono fuera de Chile"})
    assert r["status"] == "rechazado" and r["reason"] == "poligono fuera de Chile"
    rej = json.loads((s.out_dir / REJECTION_NAME).read_text(encoding="utf-8"))
    assert rej["rejected"] is True and rej["reason"] == "poligono fuera de Chile" and rej["by"] == "agente" and rej["stage"]
    # el pipeline quedo detenido: ninguna tool de calculo ni de salida corre, y no hay archivos de informe
    for name, args in [("fetch_dem", {"polygon": gj(POLY)}), ("reproject", {}), ("compute_terrain", {}), ("build_glb", {}),
                       ("render_pdf", {}), ("publish_viewer", {}), ("request_human_review", {"reason": "x y z"})]:
        out = TOOLS[name](s, args)
        assert out["status"] == "detenido", (name, out)
    for f in ("manifest.json", "report.pdf", "terreno.glb", "predio.geojson"):
        assert not (s.out_dir / f).exists()
    assert not (s.out_dir / "publicado").exists()
    assert s.rejected["reason"] == "poligono fuera de Chile"


def test_polygon_outside_chile_is_rejected_by_the_tool_and_logged(tmp_path):
    s = session(tmp_path)
    r = TOOLS["fetch_dem"](s, {"polygon": gj(box(10.0, 50.0, 10.001, 50.001))})
    assert r["status"] == "rechazado"
    rej = json.loads((s.out_dir / REJECTION_NAME).read_text(encoding="utf-8"))
    assert rej["by"] == "tool:fetch_dem" and rej["stage"] == "fetch_dem" and rej["reason"]
    assert TOOLS["reproject"](s, {})["status"] == "detenido"


def test_reject_after_pdf_is_refused_and_before_it_cleans_deliverables(tmp_path):
    s = session(tmp_path, "a")
    for name, args in [("fetch_dem", {"polygon": gj(POLY)}), ("reproject", {}), ("compute_terrain", {}),
                       ("compare_sources", {}), ("score_confidence", {}), ("build_glb", {})]:
        assert TOOLS[name](s, args)["status"] in ("ok", "no_disponible")
    assert (s.out_dir / "terreno.glb").exists() and (s.out_dir / "manifest.json").exists()
    assert TOOLS["reject_request"](s, {"reason": "cambie de opinion"})["status"] == "rechazado"
    for f in ("manifest.json", "terreno.glb", "predio.geojson"):
        assert not (s.out_dir / f).exists(), f
    assert not (s.out_dir / "layers").exists()
    # despues del PDF ya no se puede rechazar: corresponde la revision humana
    s2 = session(tmp_path, "b")
    run_all(s2, glb=True)
    r = TOOLS["reject_request"](s2, {"reason": "demasiado tarde"})
    assert r["status"] == "orden_invalido" and "request_human_review" in r["reason"]
    assert (s2.out_dir / "report.pdf").exists() and not (s2.out_dir / REJECTION_NAME).exists()


# ---------- el agente no elige CRS ni formulas ----------

def test_agent_cannot_override_crs(tmp_path):
    s = session(tmp_path)
    assert TOOLS["fetch_dem"](s, {"polygon": gj(POLY)})["status"] == "ok"
    for args in ({"epsg": 32718}, {"crs": "EPSG:4326"}, {"utm_zone": 18}, {"pixel_m": 10}, {"resampling": "cubic"}):
        r = TOOLS["reproject"](s, args)
        assert r["status"] == "entrada_invalida", args
    assert s.raster is None  # nada se calculo con el CRS pedido
    ok = TOOLS["reproject"](s, {})
    assert ok["status"] == "ok" and ok["epsg"] == 32719 and ok["pixel_m"] == 30.0  # zona del centroide, por codigo


def test_utm_zone_comes_from_the_centroid_not_from_the_agent(tmp_path):
    west = box(-72.6 - 0.0015, -33.45 - 0.0015, -72.6 + 0.0015, -33.45 + 0.0015)  # lon < -72 -> 18 S
    from tests.test_reproject import make_dem
    from terreno.fetch import DemResult

    def glo(poly, cache_dir):
        d = make_dem(cache_dir, -72.6, -33.45, n=60, name="glo.tif")
        return DemResult(path=d.path, tiles=["t"], source_url="x", acquired="2026-10-23", nodata_fraction=0.0, source="glo30")

    s = session(tmp_path, fetch={"glo30": glo, "aw3d30": glo})
    TOOLS["fetch_dem"](s, {"polygon": gj(west)})
    assert TOOLS["reproject"](s, {})["epsg"] == 32718


def test_order_is_enforced_and_steps_run_once(tmp_path):
    s = session(tmp_path)
    for name in ("reproject", "compute_terrain", "compare_sources", "score_confidence", "build_glb", "render_pdf",
                 "publish_viewer"):
        r = TOOLS[name](s, {})
        assert r["status"] == "orden_invalido", (name, r)
    assert TOOLS["fetch_dem"](s, {"polygon": gj(POLY)})["status"] == "ok"
    again = TOOLS["fetch_dem"](s, {"polygon": gj(TINY)})  # otro poligono a mitad de camino: no
    assert again["status"] == "orden_invalido" and "ya se ejecuto" in again["reason"]
    assert TOOLS["compute_terrain"](s, {})["status"] == "orden_invalido"  # falta reproject
    assert s.rejected is None  # un error de orden no es un rechazo del sitio


# ---------- el flujo completo es el mismo pipeline ----------

def test_tool_sequence_gives_the_same_result_hash_as_run_pipeline(tmp_path):
    s = session(tmp_path, "agente")
    out = run_all(s)
    assert out["score_confidence"]["overall"] in ("A", "B", "C")
    ref = run_pipeline(POLY, tmp_path / "ref", fetch=fetchers(tmp_path), fetch_aux=aux_ok, shift_fn=SHIFT,
                       cache_dir=tmp_path / "refcache")
    assert out["score_confidence"]["result_hash"] == ref.result_hash == result_hash(s.out_dir)
    m = read_manifest(s.out_dir)
    assert hash_manifest(m) == m["result_hash"]
    for f in ("manifest.json", "terreno.glb", "predio.geojson", "report.pdf", "layers/pendiente.png"):
        assert (s.out_dir / f).exists(), f


def test_report_text_comes_from_manifest_not_from_agent_arguments(tmp_path):
    s = session(tmp_path)
    out = run_all(s)
    assert "site_name" not in SCHEMAS["render_pdf"]["properties"] and SCHEMAS["render_pdf"]["properties"] == {}
    text = pdf_text(s.out_dir / "report.pdf")
    assert "Copernicus" in text and out["score_confidence"]["result_hash"][:12] in text.replace(" ", "")


def test_exaggeration_chosen_by_agent_is_labelled_in_glb_and_pdf(tmp_path):
    s = session(tmp_path)
    for name, args in [("fetch_dem", {"polygon": gj(POLY)}), ("reproject", {}), ("compute_terrain", {}),
                       ("compare_sources", {}), ("score_confidence", {}), ("build_glb", {"exaggeration": 2.5}),
                       ("render_pdf", {})]:
        assert TOOLS[name](s, args)["status"] in ("ok", "no_disponible")
    text = pdf_text(s.out_dir / "report.pdf")
    assert "×2,5" in text.replace(" ", "")
    glb = (s.out_dir / "terreno.glb").read_bytes()
    assert b'"exaggeration":2.5' in glb.replace(b" ", b"")


def test_compare_sources_unavailable_is_not_a_rejection(tmp_path):
    from terreno.errors import RejectRequest

    f = fetchers(tmp_path)

    def no_key(poly, cache_dir):
        raise RejectRequest("falta OPENTOPOGRAPHY_API_KEY")

    s = session(tmp_path, fetch={**f, "aw3d30": no_key})
    TOOLS["fetch_dem"](s, {"polygon": gj(POLY)})
    TOOLS["reproject"](s, {})
    TOOLS["compute_terrain"](s, {})
    r = TOOLS["compare_sources"](s, {})
    assert r["status"] == "no_disponible" and "OPENTOPOGRAPHY" in r["reason"] and s.rejected is None
    assert TOOLS["score_confidence"](s, {})["status"] == "ok"
    assert read_manifest(s.out_dir)["results"]["comparison"]["status"] == "no_disponible"


# ---------- revision humana ----------

def test_request_human_review_writes_visible_flag(tmp_path):
    s = session(tmp_path)
    run_all(s, glb=False)
    r = TOOLS["request_human_review"](s, {"reason": "predio chico: pendiente no concluyente"})
    assert r["status"] == "revision_humana_registrada" and r["flag_file"] == HUMAN_REVIEW_NAME
    flag = json.loads((s.out_dir / HUMAN_REVIEW_NAME).read_text(encoding="utf-8"))
    assert flag["required"] is True and "no concluyente" in flag["reason"] and flag["by"] == "agente"
    assert flag["level"] in ("A", "B", "C") and flag["result_hash"] == result_hash(s.out_dir)
    assert HUMAN_REVIEW_NAME.isupper() or HUMAN_REVIEW_NAME.startswith("REVISION")  # nombre bien visible en el listado
    assert s.human_review is True


def test_level_C_forces_the_flag_even_if_the_agent_forgets(tmp_path):
    s = session(tmp_path)
    out = run_all(s, poly=TINY)  # ~4 pixeles: no concluyente, nivel C
    assert out["score_confidence"]["overall"] == "C" and out["score_confidence"]["human_review_required"] is True
    assert not (s.out_dir / HUMAN_REVIEW_NAME).exists()
    s.finalize()  # el anfitrion (codigo, no el agente) lo escribe
    flag = json.loads((s.out_dir / HUMAN_REVIEW_NAME).read_text(encoding="utf-8"))
    assert flag["by"] == "regla de confianza (codigo)" and flag["level"] == "C" and s.human_review is True


def test_finalize_does_not_flag_when_not_required_and_does_not_double_flag(tmp_path):
    s = session(tmp_path)
    out = run_all(s)
    if not out["score_confidence"]["human_review_required"]:
        s.finalize()
        assert not (s.out_dir / HUMAN_REVIEW_NAME).exists()
    TOOLS["request_human_review"](s, {"reason": "cifra absoluta pedida por el cliente"})
    s.finalize()
    assert json.loads((s.out_dir / HUMAN_REVIEW_NAME).read_text(encoding="utf-8"))["by"] == "agente"


# ---------- publicacion estatica ----------

def test_publish_viewer_copies_only_static_files_and_sanitises_manifest(tmp_path):
    s = session(tmp_path)
    out = run_all(s)
    pub = s.out_dir / "publicado"
    assert out["publish_viewer"]["directory"] == "publicado" and out["publish_viewer"]["viewer_entry"] == "publicado/index.html"
    for f in ("index.html", "app.js", "style.css", "sun.js", "texts.js", "legend.json", "datos/terreno.glb",
              "datos/predio.geojson", "datos/manifest.json", "datos/layers/pendiente.png", "datos/layers/layers.json",
              "vendor/three/three.module.min.js" if (Path("viewer/vendor/three/three.module.min.js")).exists() else "vendor/VERSIONS.json"):
        assert (pub / f).exists(), f
    names = {p.relative_to(pub).as_posix() for p in pub.rglob("*") if p.is_file()}
    assert not any(n.startswith(("work", "cache", "context")) or "rejection" in n or n.endswith((".gpkg", ".tif", ".pdf"))
                   for n in names)
    assert max(p.stat().st_size for p in pub.rglob("*") if p.is_file()) < 25 * 1024 * 1024  # limite por archivo de Cloudflare Pages
    pm = json.loads((pub / "datos" / "manifest.json").read_text(encoding="utf-8"))
    blob = json.dumps(pm)
    assert "\\\\" not in blob and "C:" not in blob and "Users" not in blob and "python_executable" not in blob
    assert pm["result_hash"] == result_hash(s.out_dir) and hash_manifest(pm) == pm["result_hash"]  # el hash no depende de lo volatil
    assert set(pm["volatile"]) <= {"generated_at", "acquired"}


def test_publish_requires_glb_and_refuses_after_rejection(tmp_path):
    s = session(tmp_path)
    TOOLS["fetch_dem"](s, {"polygon": gj(POLY)})
    assert TOOLS["publish_viewer"](s, {})["status"] == "orden_invalido"


# ---------- mensajes del SDK -> resultado ----------

def test_summarize_messages_collects_tool_calls_usage_and_denials(tmp_path):
    from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock, ToolUseBlock

    msgs = [
        AssistantMessage(content=[TextBlock(text="Parto."), ToolUseBlock(id="1", name=MCP_PREFIX + "fetch_dem", input={})],
                         model="m"),
        AssistantMessage(content=[ToolUseBlock(id="2", name="Bash", input={"command": "ls"})], model="m"),
        AssistantMessage(content=[TextBlock(text="Listo.")], model="m"),
        ResultMessage(subtype="success", duration_ms=1234, duration_api_ms=1000, is_error=False, num_turns=3, session_id="x",
                      total_cost_usd=0.05, usage={"input_tokens": 10, "output_tokens": 5, "cache_read_input_tokens": 7},
                      result="Listo."),
    ]
    info = summarize_messages(msgs)
    assert info["tool_uses"] == [MCP_PREFIX + "fetch_dem", "Bash"] and info["final_text"] == "Listo."
    assert info["num_turns"] == 3 and info["duration_ms"] == 1234 and info["total_cost_usd"] == 0.05
    assert info["usage"]["output_tokens"] == 5 and info["is_error"] is False


def test_agent_run_result_outcome_labels():
    r = AgentRunResult(out_dir=Path("x"), outcome="rechazado")
    assert r.outcome == "rechazado" and r.human_review is False and r.forbidden_attempts == []


# ---------- run_agent con un cliente simulado (sin modelo): orquestacion y resultado ----------

class ScriptedClient:
    """Hace lo que haria el SDK: pasa cada tool_use por can_use_tool y, si se permite, ejecuta la tool de la sesion."""

    def __init__(self, options, session, script):
        self.options, self.session, self.script, self.request = options, session, script, None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def query(self, request):
        self.request = request

    async def receive_response(self):
        from claude_agent_sdk import AssistantMessage, PermissionResultAllow, ResultMessage, TextBlock, ToolUseBlock, ToolPermissionContext

        for i, (name, args) in enumerate(self.script):
            yield AssistantMessage(content=[ToolUseBlock(id=str(i), name=name, input=args)], model="m")
            d = await self.options.can_use_tool(name, args, ToolPermissionContext())
            if isinstance(d, PermissionResultAllow):
                TOOLS[name[len(MCP_PREFIX):]](self.session, args)
        yield AssistantMessage(content=[TextBlock(text="Terminado.")], model="m")
        yield ResultMessage(subtype="success", duration_ms=10, duration_api_ms=5, is_error=False, num_turns=len(self.script),
                            session_id="s", total_cost_usd=0.01, usage={"input_tokens": 1, "output_tokens": 2}, result="Terminado.")


def test_run_agent_full_report_outcome_and_forbidden_attempts_are_recorded(tmp_path):
    from terreno.agent import run_agent

    s = session(tmp_path, "run")
    P = MCP_PREFIX
    script = [("Bash", {"command": "curl evil"}), (P + "fetch_dem", {"polygon": gj(TINY)}), (P + "reproject", {}),
              (P + "compute_terrain", {}), (P + "compare_sources", {}), (P + "score_confidence", {}), (P + "build_glb", {}),
              (P + "render_pdf", {}), (P + "publish_viewer", {})]  # TINY -> nivel C: el agente "olvida" request_human_review
    res = run_agent("pedido", s.out_dir, session=s, client_factory=lambda o: ScriptedClient(o, s, script))
    assert res.outcome == "informe" and res.is_error is False and res.num_turns == len(script) and res.usage["output_tokens"] == 2
    assert [f["tool"] for f in res.forbidden_attempts] == ["Bash"] and res.tool_uses[0] == "Bash"
    assert res.human_review is True and (s.out_dir / HUMAN_REVIEW_NAME).exists()  # lo impuso el codigo
    assert res.result_hash == result_hash(s.out_dir) and res.final_text == "Terminado."
    log = json.loads((s.out_dir / "agent_log.json").read_text(encoding="utf-8"))
    assert log["forbidden_attempts"][0]["tool"] == "Bash" and len(log["calls"]) == 8


def test_run_agent_rejection_outcome_leaves_no_report_files(tmp_path):
    from terreno.agent import run_agent

    s = session(tmp_path, "rej")
    script = [(MCP_PREFIX + "reject_request", {"reason": "el poligono esta fuera de Chile"})]
    res = run_agent("pedido", s.out_dir, session=s, client_factory=lambda o: ScriptedClient(o, s, script))
    assert res.outcome == "rechazado" and res.rejection["reason"] == "el poligono esta fuera de Chile"
    assert (s.out_dir / REJECTION_NAME).exists()
    assert not any((s.out_dir / f).exists() for f in ("report.pdf", "manifest.json", "terreno.glb", HUMAN_REVIEW_NAME))


def test_run_agent_reports_sdk_failure_instead_of_hiding_it(tmp_path):
    from terreno.agent import run_agent

    class Broken:
        def __init__(self, o):
            pass

        async def __aenter__(self):
            raise RuntimeError("credenciales invalidas")

        async def __aexit__(self, *a):
            return False

    s = session(tmp_path, "err")
    res = run_agent("pedido", s.out_dir, session=s, client_factory=Broken)
    assert res.outcome == "error" and res.is_error and "credenciales" in res.error


# ---------- AUD-1 I2: score_confidence no corre antes de intentar compare_sources ----------

def _prefix(s):
    for name, args in (("fetch_dem", {"polygon": gj(POLY)}), ("reproject", {}), ("compute_terrain", {})):
        assert TOOLS[name](s, args)["status"] == "ok"


def test_score_confidence_requires_compare_sources_to_have_been_attempted(tmp_path):
    """El agente no puede saltarse la comparacion para que un C por desacuerdo (RMS > 6 m) salga como B."""
    # AW3D30 con relieve que no coincide (+-20 m en franjas): desacuerdo centrado > 6 m -> C. Un desfase constante ya no
    # cuenta como desacuerdo (calibracion RM 2026-10-31).
    import numpy as np
    from tests.test_pipeline import ramp_fn
    stripes = lambda lon, lat: ramp_fn()(lon, lat) + 20.0 * np.sign(np.sin((lon - LON0) * 20000.0))
    s = session(tmp_path, fetch=fetchers(tmp_path, aw_fn=stripes))
    _prefix(s)
    r = TOOLS["score_confidence"](s, {})
    assert r["status"] == "orden_invalido" and "compare_sources" in r["reason"]
    assert s.confidence is None and not (s.out_dir / "manifest.json").exists()  # no se escribio ningun manifiesto sin comparar
    assert TOOLS["compare_sources"](s, {})["status"] == "ok"
    ok = TOOLS["score_confidence"](s, {})
    assert ok["status"] == "ok" and ok["overall"] == "C"
    # y una vez que existe el manifiesto, compare_sources ya no se puede ejecutar "despues"
    late = TOOLS["compare_sources"](s, {})
    assert late["status"] == "orden_invalido" and "ya se ejecuto" in late["reason"]


def test_unavailable_comparison_counts_as_attempted_and_the_reason_is_explicit(tmp_path):
    from terreno.errors import RejectRequest

    def no_key(poly, cache_dir):
        raise RejectRequest("falta OPENTOPOGRAPHY_API_KEY")

    s = session(tmp_path, fetch={**fetchers(tmp_path), "aw3d30": no_key})
    _prefix(s)
    assert TOOLS["compare_sources"](s, {})["status"] == "no_disponible"
    assert TOOLS["score_confidence"](s, {})["status"] == "ok"
    cmp_ = read_manifest(s.out_dir)["results"]["comparison"]
    assert cmp_["status"] == "no_disponible" and "OPENTOPOGRAPHY" in cmp_["reason"] and "no solicitado" not in cmp_["reason"]
