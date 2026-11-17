"""Interfaz local del agente de operaciones con LangGraph."""
import json
import time
import uuid
import streamlit as st
from agent_engine import API_MODEL, Store, build_graph, load_env

load_env()

st.set_page_config(page_title="Agente de operaciones | Jorge Rollón", page_icon="⚙️", layout="wide")
st.title("Agente de operaciones")
st.caption("Jorge Ángel Rollón Rafael · Python · LangGraph · SQLite · Ollama o API")
st.write("Un planificador elige una herramienta. El programa valida sus parámetros, controla la confirmación y muestra el resultado real.")

with st.sidebar:
    modes = {"Demostración sin modelo": "demo", "Modelo local con Ollama": "ollama", "Modelo por API": "api"}
    mode = modes[st.radio("Modo", list(modes))]
    if mode == "api":
        model = st.text_input("Modelo de la API", value=API_MODEL)
        st.caption("API compatible con OpenAI. La clave se lee de OPENAI_API_KEY (archivo .env); no se escribe aquí. La petición sale de este equipo.")
    else:
        model = st.text_input("Modelo en Ollama", value="qwen2.5:1.5b", disabled=mode == "demo")
        st.caption("Ollama debe estar abierto para usar el modelo local. Nada sale de este equipo.")
    st.divider()
    st.write("Prueba estas instrucciones:")
    st.code("calcula (120 * 3) * 1.21\nstock de monitor\nincidencia: Impresora | No imprime desde esta mañana", language=None)

if mode == "demo":
    st.info("Modo determinista: admite los comandos de ejemplo. No hay un modelo de IA tomando decisiones en este modo.")
elif mode == "ollama":
    st.info("Modo IA local: el modelo interpreta la petición y propone una herramienta. Los resultados proceden de Python o SQLite.")
else:
    st.info("Modo API: un modelo remoto interpreta la petición y propone una herramienta. Los resultados proceden de Python o SQLite.")

def stock_table(items):
    return [{"Código": item["sku"], "Producto": item["name"], "Unidades": item["quantity"],
             "Precio (€)": item["price_cents"] / 100} for item in items]

def trace_table(trace):
    keys = list(dict.fromkeys(key for row in trace for key in row))
    return [{key: row.get(key, "") if key == "milisegundos" else str(row.get(key, "")) for key in keys}
            for row in trace]

@st.cache_resource
def graph():
    return build_graph()

with st.expander("Flujo de trabajo"):
    st.graphviz_chart('digraph { rankdir=TB; inicio -> planificar; inicio -> validar [label="confirmación"]; planificar -> validar; validar -> ejecutar [label="plan permitido"]; validar -> responder [label="rechazo o pendiente"]; ejecutar -> responder; responder -> fin; }')
    st.caption("Un máximo de una herramienta por petición. Confirmar reutiliza el plan revisado.")

with st.form("request"):
    question = st.text_area("¿Qué necesitas?", max_chars=1500)
    submit = st.form_submit_button("Ejecutar consulta")

if submit:
    if not question.strip():
        st.warning("Escribe una petición.")
    else:
        try:
            with st.spinner("Recorriendo el flujo del agente..."):
                start = time.perf_counter()
                state = graph().invoke({
                    "question": question.strip(), "request_id": uuid.uuid4().hex,
                    "model": model, "mode": mode, "approved": False,
                    "trace": [], "status": "new",
                }, config={"recursion_limit": 12})
                state["seconds"] = round(time.perf_counter() - start, 2)
                st.session_state.last = state
        except Exception as error:
            st.session_state.pop("last", None)
            st.error(f"No se pudo completar el flujo: {error}")

state = st.session_state.get("last")
if state:
    st.subheader("Última consulta")
    st.caption(f"Petición: {state['question']} | Modo: {state['mode']}")
    if state["status"] == "error":
        st.error(state["response"])
    else:
        st.write(state["response"])
    if state.get("plan"):
        with st.expander("Plan propuesto", expanded=state["status"] == "needs_confirmation"):
            st.json(state["plan"])
    if state["status"] == "needs_confirmation":
        st.warning("Confirmar guardará exactamente el título y la descripción mostrados en la base SQLite local.")
        confirm, cancel = st.columns(2)
        if confirm.button("Confirmar registro de la incidencia"):
            try:
                resumed = {**state, "approved": True, "status": "planned", "error": ""}
                st.session_state.last = graph().invoke(resumed, config={"recursion_limit": 12})
                st.rerun()
            except Exception as error:
                st.error(f"No se pudo confirmar: {error}")
        if cancel.button("Cancelar incidencia"):
            st.session_state.last = {**state, "status": "cancelled",
                "response": "Incidencia cancelada. No se ha registrado."}
            st.rerun()
    if state.get("result") is not None:
        result = state["result"]
        if state["plan"]["tool"] == "stock":
            if result["items"]:
                st.dataframe(stock_table(result["items"]), use_container_width=True)
            else:
                st.write("Ningún producto coincide con la consulta.")
        else:
            st.json(result)
    with st.expander("Trazabilidad y tiempos"):
        st.dataframe(trace_table(state.get("trace", [])), use_container_width=True)
        st.caption("Los tiempos corresponden a las ejecuciones observadas. La traza es un registro técnico, no el razonamiento interno del modelo.")
    st.download_button("Descargar evidencia de la consulta", json.dumps(state, ensure_ascii=False, indent=2),
                       file_name="consulta_agente.json", mime="application/json")

st.divider()
stock_tab, tickets_tab = st.tabs(["Inventario ficticio", "Incidencias locales"])
try:
    with Store() as store:
        with stock_tab:
            st.dataframe(stock_table(store.inventory()), use_container_width=True)
            st.caption("Datos ficticios de demostración. No hay conexión a ninguna empresa.")
        with tickets_tab:
            st.dataframe([{"Nº": t["id"], "Título": t["title"], "Descripción": t["description"],
                           "Creada (UTC)": t["created_at"]} for t in store.tickets()], use_container_width=True)
except Exception as error:
    st.error(f"No se pudo leer la base local: {error}")
