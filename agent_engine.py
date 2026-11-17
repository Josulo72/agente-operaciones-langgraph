"""Motor de agente con herramientas acotadas y confirmación de escrituras."""
from __future__ import annotations
import ast
import json
import math
import operator
import os
import re
import sqlite3
import time
from pathlib import Path
from typing import Any, TypedDict
import requests

ROOT = Path(__file__).resolve().parent
DATABASE = ROOT / "datos" / "operaciones.sqlite3"
OLLAMA = "http://127.0.0.1:11434"
API_URL = "https://api.openai.com/v1"
API_MODEL = "gpt-4o-mini"

def load_env(path=None):
    """Carga pares NOMBRE=valor de un archivo .env sin pisar variables ya definidas."""
    try:
        lines = Path(path or ROOT / ".env").read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    for line in lines:
        name, separator, value = line.strip().partition("=")
        if separator and name.strip() and not name.startswith("#"):
            os.environ.setdefault(name.strip(), value.strip().strip("\"'"))

def api_settings():
    key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not key:
        raise ValueError("Falta OPENAI_API_KEY. Copia .env.example como .env y escribe ahí tu clave.")
    return key, (os.environ.get("OPENAI_BASE_URL", "").strip() or API_URL).rstrip("/")

class State(TypedDict, total=False):
    question: str
    request_id: str
    model: str
    mode: str
    approved: bool
    plan: dict
    status: str
    error: str
    result: Any
    response: str
    trace: list

def calculate(expression):
    if not isinstance(expression, str) or not 1 <= len(expression) <= 160:
        raise ValueError("La expresión debe tener entre 1 y 160 caracteres.")
    tree = ast.parse(expression.strip(), mode="eval")
    if sum(1 for _ in ast.walk(tree)) > 60:
        raise ValueError("Expresión demasiado compleja.")
    binary = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
              ast.Div: operator.truediv, ast.Mod: operator.mod, ast.Pow: operator.pow}
    unary = {ast.UAdd: operator.pos, ast.USub: operator.neg}
    def visit(node):
        if isinstance(node, ast.Constant) and type(node.value) in (int, float):
            value = node.value
        elif isinstance(node, ast.BinOp) and type(node.op) in binary:
            left, right = visit(node.left), visit(node.right)
            if isinstance(node.op, ast.Pow) and (abs(right) > 10 or abs(left) > 1000000):
                raise ValueError("Potencia fuera de los límites de la demo.")
            value = binary[type(node.op)](left, right)
        elif isinstance(node, ast.UnaryOp) and type(node.op) in unary:
            value = unary[type(node.op)](visit(node.operand))
        else:
            raise ValueError("Solo se admite aritmética; no se ejecuta código Python.")
        if type(value) not in (int, float) or not math.isfinite(value) or abs(value) > 1e12:
            raise ValueError("Resultado fuera del intervalo permitido.")
        return value
    result = visit(tree.body)
    return round(result, 10) if isinstance(result, float) else result

def bounded_text(value, label, limit):
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > limit:
        raise ValueError(f"{label}: texto obligatorio de hasta {limit} caracteres.")
    return value.strip()

def validate_plan(plan):
    if not isinstance(plan, dict) or set(plan) != {"tool", "arguments"}:
        raise ValueError("El plan debe contener tool y arguments.")
    tool, args = plan["tool"], plan["arguments"]
    if not isinstance(tool, str) or not isinstance(args, dict):
        raise ValueError("Formato de herramienta no válido.")
    schemas = {"calculate": {"expression"}, "stock": {"product"},
               "ticket": {"title", "description"}, "unsupported": {"reason"}}
    if tool not in schemas or set(args) != schemas[tool]:
        raise ValueError("Herramienta o parámetros no permitidos.")
    if tool == "calculate":
        args = {"expression": bounded_text(args["expression"], "Expresion", 160)}
        calculate(args["expression"])
    elif tool == "stock":
        args = {"product": bounded_text(args["product"], "Producto", 100)}
    elif tool == "ticket":
        args = {"title": bounded_text(args["title"], "Titulo", 120),
                "description": bounded_text(args["description"], "Descripcion", 1500)}
    else:
        args = {"reason": bounded_text(args["reason"], "Motivo", 500)}
    return {"tool": tool, "arguments": args}

class Store:
    def __init__(self, path=DATABASE, connection=None):
        self.owns_connection = connection is None
        if connection is None:
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            connection = sqlite3.connect(str(path), timeout=10)
        self.db = connection
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS inventory (
          sku TEXT PRIMARY KEY, name TEXT NOT NULL, quantity INTEGER NOT NULL, price_cents INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS tickets (
          id INTEGER PRIMARY KEY AUTOINCREMENT, request_id TEXT UNIQUE NOT NULL,
          title TEXT NOT NULL, description TEXT NOT NULL,
          created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
        );
        """)
        self.db.executemany("INSERT OR IGNORE INTO inventory VALUES (?,?,?,?)", [
            ("PORT-01", "Portátil oficina", 12, 64900),
            ("MON-24", "Monitor 24 pulgadas", 8, 12900),
            ("TEC-01", "Teclado USB", 25, 1900),
            ("RAT-01", "Ratón USB", 0, 1200),
        ])
        self.db.commit()
    def __enter__(self):
        return self
    def __exit__(self, *args):
        if self.owns_connection:
            self.db.close()
    def inventory(self, product=""):
        query = product.casefold()
        rows = self.db.execute("SELECT * FROM inventory ORDER BY sku").fetchall()
        def normalize(text):
            import unicodedata
            return "".join(c for c in unicodedata.normalize("NFD", text.casefold())
                           if unicodedata.category(c) != "Mn")
        wanted = normalize(query)
        forms = {wanted}
        if len(wanted) > 3 and wanted.endswith("s"):
            forms.add(wanted[:-1])
        if len(wanted) > 4 and wanted.endswith("es"):
            forms.add(wanted[:-2])
        return [dict(row) for row in rows if any(form in normalize(row["name"])
                or form in normalize(row["sku"]) for form in forms)]
    def tickets(self):
        return [dict(row) for row in self.db.execute("SELECT * FROM tickets ORDER BY id DESC")]
    def create_ticket(self, request_id, title, description):
        request_id = bounded_text(request_id, "Identificador", 100)
        title = bounded_text(title, "Titulo", 120)
        description = bounded_text(description, "Descripcion", 1500)
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO tickets(request_id,title,description) VALUES (?,?,?)",
                            (request_id, title, description))
            row = self.db.execute("SELECT * FROM tickets WHERE request_id=?", (request_id,)).fetchone()
            if row["title"] != title or row["description"] != description:
                raise ValueError("La solicitud ya existe con otro contenido.")
        return dict(row)

PROMPT = """Eres el planificador de una demo de operaciones. Devuelve solo JSON.
Elige UNA herramienta por consulta. No ejecutes herramientas ni inventes resultados.
Esquemas exactos:
{"tool":"calculate","arguments":{"expression":"(120 * 3) * 1.21"}}
{"tool":"stock","arguments":{"product":"monitor"}}
{"tool":"ticket","arguments":{"title":"Título","description":"Descripción"}}
{"tool":"unsupported","arguments":{"reason":"Motivo o aclaración necesaria"}}
calculate admite + - * / % ** y paréntesis. Para un porcentaje usa división entre 100.
stock consulta existencias por nombre o SKU. Pasa solo el nombre del producto, en singular.
ticket prepara una incidencia; el usuario confirmará antes de registrarla.
No inventes detalles de incidencias. Si falta información, usa unsupported.
No combines acciones: pide que se envíen por separado.
Las peticiones fuera de estas tres funciones van a unsupported.
No tienes acceso a archivos del equipo, Internet, correo ni comandos del sistema."""

def ollama_post(payload, timeout):
    try:
        return requests.post(OLLAMA + "/api/chat", json=payload, timeout=timeout)
    except requests.ConnectionError:
        raise RuntimeError("no hay respuesta de Ollama en este equipo. Ábrelo y vuelve a intentarlo, o usa el modo demostración o el modo API.") from None

def ollama_plan(state):
    response = ollama_post({
        "model": state["model"], "stream": False, "format": "json",
        "messages": [{"role": "system", "content": PROMPT},
                     {"role": "user", "content": state["question"]}],
        "options": {"temperature": 0, "num_ctx": 4096, "num_predict": 350},
    }, (5, 180))
    response.raise_for_status()
    return json.loads(response.json()["message"]["content"])

def api_plan(state):
    """Mismo planificador contra una API compatible con OpenAI (chat/completions)."""
    key, base = api_settings()
    response = requests.post(base + "/chat/completions", headers={"Authorization": "Bearer " + key}, json={
        "model": state["model"], "temperature": 0, "max_tokens": 350,
        "response_format": {"type": "json_object"},
        "messages": [{"role": "system", "content": PROMPT},
                     {"role": "user", "content": state["question"]}],
    }, timeout=(5, 120))
    response.raise_for_status()
    return json.loads(response.json()["choices"][0]["message"]["content"])

def demo_plan(state):
    text = state["question"].strip()
    if text.lower().startswith("calcula "):
        return {"tool": "calculate", "arguments": {"expression": text[8:].strip()}}
    if text.lower().startswith("stock de "):
        return {"tool": "stock", "arguments": {"product": text[9:].strip()}}
    match = re.fullmatch(r"incidencia:\s*([^|]+)\|(.+)", text, re.I | re.S)
    if match:
        return {"tool": "ticket", "arguments": {"title": match[1].strip(), "description": match[2].strip()}}
    return {"tool": "unsupported", "arguments": {"reason":
        "Modo demostración: usa calcula EXPRESIÓN, stock de PRODUCTO o incidencia: TÍTULO | DESCRIPCIÓN."}}

def event(state, node, start, **details):
    return state.get("trace", []) + [{"nodo": node, "milisegundos":
        round((time.perf_counter() - start) * 1000, 2), **details}]

def make_nodes(store_factory=Store, planner=None):
    def plan(state):
        start = time.perf_counter()
        try:
            fn = planner or {"demo": demo_plan, "api": api_plan}.get(state["mode"], ollama_plan)
            proposal = fn(state)
            return {"plan": proposal, "status": "planned", "error": "",
                    "trace": event(state, "planificar", start, modo=state["mode"])}
        except Exception as error:
            return {"status": "error", "error": f"Fallo de planificación: {error}",
                    "trace": event(state, "planificar", start, error=True)}
    def validate(state):
        start = time.perf_counter()
        if state.get("status") == "error":
            return {"trace": event(state, "validar", start, omitido=True)}
        try:
            checked = validate_plan(state["plan"])
            if checked["tool"] == "unsupported":
                status = "unsupported"
            elif checked["tool"] == "ticket" and state.get("approved") is not True:
                status = "needs_confirmation"
            else:
                status = "ready"
            return {"plan": checked, "status": status, "error": "",
                    "trace": event(state, "validar", start, herramienta=checked["tool"], estado=status)}
        except Exception as error:
            return {"status": "error", "error": str(error),
                    "trace": event(state, "validar", start, error=True)}
    def execute(state):
        start = time.perf_counter()
        try:
            if state["status"] != "ready":
                raise ValueError("No se ha autorizado la ejecución del plan.")
            checked = validate_plan(state["plan"])
            tool, args = checked["tool"], checked["arguments"]
            if tool == "calculate":
                result = {"expression": args["expression"], "value": calculate(args["expression"])}
            elif tool == "stock":
                with store_factory() as store:
                    result = {"product": args["product"], "items": store.inventory(args["product"])}
            elif tool == "ticket":
                if state.get("approved") is not True:
                    raise ValueError("Falta confirmación del usuario.")
                with store_factory() as store:
                    result = store.create_ticket(state["request_id"], **args)
            else:
                raise ValueError("Esta herramienta no puede ejecutarse.")
            return {"result": result, "status": "done",
                    "trace": event(state, "ejecutar", start, herramienta=tool)}
        except Exception as error:
            return {"status": "error", "error": str(error),
                    "trace": event(state, "ejecutar", start, error=True)}
    def finish(state):
        start = time.perf_counter()
        status = state["status"]
        if status == "error":
            text = state.get("error", "La operación no se completó.")
        elif status == "needs_confirmation":
            text = PENDING
        elif status == "unsupported":
            text = state["plan"]["arguments"]["reason"]
        elif status == "cancelled":
            text = "Incidencia cancelada. No se ha registrado."
        elif status == "done":
            tool, result = state["plan"]["tool"], state["result"]
            if tool == "calculate":
                text = f"Resultado: {result['value']}"
            elif tool == "stock":
                text = f"Productos encontrados: {len(result['items'])}. Existencias de demostración."
            else:
                text = f"Incidencia registrada con número {result['id']}."
        else:
            text = "Estado no reconocido; no se presenta un resultado."
        return {"response": text, "trace": event(state, "responder", start)}
    return {"planificar": plan, "validar": validate, "ejecutar": execute, "responder": finish}

PENDING = "Incidencia preparada. Revisa el contenido y confirma para registrarla."

def build_graph(store_factory=Store, planner=None, checkpointer=None):
    from langgraph.checkpoint.memory import InMemorySaver
    from langgraph.graph import END, START, StateGraph
    from langgraph.types import interrupt

    def confirm(state):
        # interrupt() detiene el grafo aqui y guarda el estado en el checkpointer.
        # Al reanudar, el nodo se ejecuta otra vez desde el principio y interrupt()
        # devuelve la respuesta del usuario, por eso no se escribe nada antes de esta linea.
        answer = interrupt({"plan": state["plan"]})
        start = time.perf_counter()
        if answer is True:
            return {"approved": True, "status": "ready",
                    "trace": event(state, "confirmar", start, respuesta="confirmada")}
        return {"approved": False, "status": "cancelled",
                "trace": event(state, "confirmar", start, respuesta="cancelada")}

    def after_validation(state):
        if state["status"] == "ready":
            return "ejecutar"
        return "confirmar" if state["status"] == "needs_confirmation" else "responder"

    nodes = make_nodes(store_factory, planner)
    graph = StateGraph(State)
    for name, fn in nodes.items():
        graph.add_node(name, fn)
    graph.add_node("confirmar", confirm)
    graph.add_edge(START, "planificar")
    graph.add_edge("planificar", "validar")
    graph.add_conditional_edges("validar", after_validation,
        {"ejecutar": "ejecutar", "confirmar": "confirmar", "responder": "responder"})
    graph.add_conditional_edges("confirmar",
        lambda state: "ejecutar" if state["status"] == "ready" else "responder",
        {"ejecutar": "ejecutar", "responder": "responder"})
    graph.add_edge("ejecutar", "responder")
    graph.add_edge("responder", END)
    return graph.compile(checkpointer=checkpointer or InMemorySaver())

def thread_config(request_id):
    return {"configurable": {"thread_id": request_id}, "recursion_limit": 12}

def snapshot(output):
    """Estado apto para la interfaz y para JSON: sin los objetos internos de la pausa."""
    state = {key: value for key, value in output.items() if key != "__interrupt__"}
    if "__interrupt__" in output:
        state["response"] = PENDING
    return state

def run_request(graph, state):
    """Lanza una peticion nueva en su propio hilo. Si es una incidencia, vuelve en pausa."""
    return snapshot(graph.invoke(state, thread_config(state["request_id"])))

def answer_confirmation(graph, request_id, approved):
    """Reanuda la peticion que esta esperando confirmacion con la respuesta del usuario."""
    from langgraph.types import Command
    config = thread_config(request_id)
    if graph.get_state(config).next != ("confirmar",):
        raise LookupError("Esta incidencia ya no está pendiente de confirmación. Vuelve a enviar la petición.")
    return snapshot(graph.invoke(Command(resume=approved is True), config))
