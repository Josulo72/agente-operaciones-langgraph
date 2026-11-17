"""Pruebas reproducibles sin Ollama ni llamadas a un modelo."""
import importlib.util
import os
import sqlite3
import tempfile
import unittest
import requests
from pathlib import Path
from unittest import mock
from agent_engine import (Store, calculate, validate_plan, demo_plan, make_nodes, build_graph,
                          api_plan, load_env, run_request, answer_confirmation, PENDING)

HAS_LANGGRAPH=importlib.util.find_spec("langgraph") is not None

class AgentTests(unittest.TestCase):
    def setUp(self):
        self.db=sqlite3.connect(":memory:")
        self.store=Store(connection=self.db)
        self.nodes=make_nodes(lambda: Store(connection=self.db), planner=demo_plan)
    def tearDown(self):
        self.db.close()
    def state(self,question="stock de monitor",approved=False):
        return {"question":question,"request_id":"test-request","mode":"demo",
                "model":"unused","approved":approved,"status":"new","trace":[]}
    def run_nodes(self,state):
        for name in ("planificar","validar"):
            state.update(self.nodes[name](state))
        if state["status"]=="ready":
            state.update(self.nodes["ejecutar"](state))
        state.update(self.nodes["responder"](state))
        return state
    def test_arithmetic(self):
        self.assertEqual(calculate("(120*3)*1.21"),435.6)
    def test_negative_and_power(self):
        self.assertEqual(calculate("-2 + 3**2"),7)
    def test_reject_code(self):
        with self.assertRaises(ValueError): calculate("__import__('os').getcwd()")
    def test_reject_boolean(self):
        with self.assertRaises(ValueError): calculate("True + 1")
    def test_reject_excessive_power(self):
        with self.assertRaises(ValueError): calculate("10**100")
    def test_reject_nonfinite(self):
        with self.assertRaises(ValueError): calculate("1e999")
    def test_zero_division(self):
        with self.assertRaises(ZeroDivisionError): calculate("1/0")
    def test_reject_large_result(self):
        with self.assertRaises(ValueError): calculate("1000000000000*2")
    def test_reject_list(self):
        with self.assertRaises(ValueError): calculate("[1,2]")
    def test_tool_allowlist(self):
        with self.assertRaises(ValueError): validate_plan({"tool":"shell","arguments":{"cmd":"whoami"}})
    def test_reject_extra_arguments(self):
        with self.assertRaises(ValueError): validate_plan({"tool":"stock","arguments":{"product":"monitor","extra":1}})
    def test_reject_bad_plan(self):
        with self.assertRaises(ValueError): validate_plan([])
    def test_reject_bad_ticket(self):
        with self.assertRaises(ValueError): validate_plan({"tool":"ticket","arguments":{"title":"","description":"falla"}})
    def test_stock_accent(self):
        self.assertEqual(len(self.store.inventory("portatil")),1)
    def test_stock_plural(self):
        self.assertEqual([r["sku"] for r in self.store.inventory("teclados")],["TEC-01"])
        self.assertEqual([r["sku"] for r in self.store.inventory("ratones")],["RAT-01"])
        self.assertEqual([r["sku"] for r in self.store.inventory("Monitores")],["MON-24"])
    def test_zero_stock_is_reported(self):
        self.assertEqual(self.store.inventory("raton")[0]["quantity"],0)
    def test_stock_no_match(self):
        self.assertEqual(self.store.inventory("inexistente"),[])
    def test_stock_sql_like_text(self):
        self.assertEqual(self.store.inventory("' OR 1=1 --"),[])
    def test_ticket_needs_confirmation(self):
        result=self.run_nodes(self.state("incidencia: Impresora | No imprime"))
        self.assertEqual(result["status"],"needs_confirmation")
        self.assertEqual(self.store.tickets(),[])
    def test_execute_checks_confirmation_again(self):
        state=self.state("incidencia: Impresora | No imprime")
        state.update(plan=demo_plan(state),status="ready",approved=False)
        result=self.nodes["ejecutar"](state)
        self.assertEqual(result["status"],"error")
        self.assertEqual(self.store.tickets(),[])
    def test_approved_ticket(self):
        result=self.run_nodes(self.state("incidencia: Impresora | No imprime",True))
        self.assertEqual(result["status"],"done")
        self.assertEqual(len(self.store.tickets()),1)
    def test_idempotent_ticket(self):
        first=self.store.create_ticket("one","Titulo","Descripcion")
        second=self.store.create_ticket("one","Titulo","Descripcion")
        self.assertEqual(first["id"],second["id"])
        self.assertEqual(len(self.store.tickets()),1)
    def test_replay_with_changed_data(self):
        self.store.create_ticket("one","Titulo","Descripcion")
        with self.assertRaises(ValueError): self.store.create_ticket("one","Otro","Descripcion")
        self.assertEqual(len(self.store.tickets()),1)
    def test_parameterized_ticket(self):
        title="Error '); DROP TABLE tickets;--"
        self.store.create_ticket("two",title,"Texto de ejemplo")
        self.assertEqual(self.store.tickets()[0]["title"],title)
    def test_unsupported_does_not_write(self):
        result=self.run_nodes(self.state("Envia un correo"))
        self.assertEqual(result["status"],"unsupported")
        self.assertEqual(self.store.tickets(),[])
    def test_calculator_tool(self):
        result=self.run_nodes(self.state("calcula 12*3"))
        self.assertEqual(result["result"]["value"],36)
        self.assertEqual([e["nodo"] for e in result["trace"]],
                         ["planificar","validar","ejecutar","responder"])
    def test_planning_failure_does_not_write(self):
        def broken(state): raise RuntimeError("modelo no disponible")
        nodes=make_nodes(lambda: Store(connection=self.db),planner=broken)
        state=self.state()
        state.update(nodes["planificar"](state))
        state.update(nodes["validar"](state))
        self.assertEqual(state["status"],"error")
        self.assertEqual(self.store.tickets(),[])
    def test_invalid_model_plan_does_not_execute(self):
        nodes=make_nodes(lambda: Store(connection=self.db),planner=lambda state:{"tool":"shell","arguments":{}})
        state=self.state()
        state.update(nodes["planificar"](state))
        state.update(nodes["validar"](state))
        self.assertEqual(state["status"],"error")
        self.assertEqual(self.store.tickets(),[])
    def test_ollama_closed_gives_clear_error(self):
        nodes=make_nodes(lambda: Store(connection=self.db))
        with mock.patch("agent_engine.requests.post",side_effect=requests.ConnectionError("rechazada")):
            state={**self.state(),"mode":"ollama"}
            state.update(nodes["planificar"](state))
            state.update(nodes["validar"](state))
        self.assertEqual(state["status"],"error")
        self.assertIn("no hay respuesta de Ollama",state["error"])
        self.assertNotIn("rechazada",state["error"])
    def test_api_plan_without_key_does_not_call(self):
        with mock.patch.dict(os.environ,{"OPENAI_API_KEY":""}), mock.patch("agent_engine.requests.post") as post:
            with self.assertRaises(ValueError): api_plan(self.state())
            post.assert_not_called()
    def test_api_mode_validates_remote_plan(self):
        reply=mock.Mock()
        reply.json.return_value={"choices":[{"message":{"content":'{"tool":"stock","arguments":{"product":"monitor"}}'}}]}
        nodes=make_nodes(lambda: Store(connection=self.db))
        env={"OPENAI_API_KEY":"clave-de-prueba","OPENAI_BASE_URL":"http://127.0.0.1:9/v1/"}
        with mock.patch.dict(os.environ,env), mock.patch("agent_engine.requests.post",return_value=reply) as post:
            state={**self.state(),"mode":"api","model":"modelo"}
            for name in ("planificar","validar","ejecutar","responder"):
                state.update(nodes[name](state))
        self.assertEqual(post.call_args.args[0],"http://127.0.0.1:9/v1/chat/completions")
        self.assertEqual(post.call_args.kwargs["headers"],{"Authorization":"Bearer clave-de-prueba"})
        self.assertEqual(state["status"],"done")
        self.assertEqual(len(state["result"]["items"]),1)
    def test_api_mode_rejects_unknown_tool(self):
        reply=mock.Mock()
        reply.json.return_value={"choices":[{"message":{"content":'{"tool":"shell","arguments":{"cmd":"whoami"}}'}}]}
        nodes=make_nodes(lambda: Store(connection=self.db))
        with mock.patch.dict(os.environ,{"OPENAI_API_KEY":"clave-de-prueba"}), mock.patch("agent_engine.requests.post",return_value=reply):
            state={**self.state(),"mode":"api"}
            state.update(nodes["planificar"](state))
            state.update(nodes["validar"](state))
        self.assertEqual(state["status"],"error")
        self.assertEqual(self.store.tickets(),[])
    def test_load_env_keeps_existing_values(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/".env"
            path.write_text('# nota\nPRUEBA_NUEVA="uno"\nPRUEBA_PUESTA=dos\nlinea suelta\n',encoding="utf-8")
            with mock.patch.dict(os.environ,{"PRUEBA_PUESTA":"original"}):
                os.environ.pop("PRUEBA_NUEVA",None)
                load_env(path)
                self.assertEqual(os.environ["PRUEBA_NUEVA"],"uno")
                self.assertEqual(os.environ["PRUEBA_PUESTA"],"original")
            load_env(Path(folder)/"no-existe")

@unittest.skipUnless(HAS_LANGGRAPH,"LangGraph no instalado")
class GraphTests(unittest.TestCase):
    """El grafo compilado de verdad: pausa con interrupt(), reanudacion y cancelacion."""
    def setUp(self):
        self.db=sqlite3.connect(":memory:")
        self.store=Store(connection=self.db)
        self.graph=build_graph(lambda: Store(connection=self.db),planner=demo_plan)
    def tearDown(self):
        self.db.close()
    def state(self,question,request_id="peticion-1"):
        return {"question":question,"request_id":request_id,"mode":"demo",
                "model":"unused","approved":False,"status":"new","trace":[]}
    def nodes(self,state):
        return [e["nodo"] for e in state["trace"]]
    def test_ticket_pauses_before_writing(self):
        pending=run_request(self.graph,self.state("incidencia: Impresora | No imprime"))
        self.assertEqual(pending["status"],"needs_confirmation")
        self.assertEqual(pending["response"],PENDING)
        self.assertEqual(self.nodes(pending),["planificar","validar"])
        self.assertNotIn("__interrupt__",pending)
        self.assertEqual(self.store.tickets(),[])
    def test_confirm_resumes_and_writes_once(self):
        pending=run_request(self.graph,self.state("incidencia: Impresora | No imprime"))
        done=answer_confirmation(self.graph,pending["request_id"],True)
        self.assertEqual(done["status"],"done")
        self.assertEqual(self.nodes(done),["planificar","validar","confirmar","ejecutar","responder"])
        self.assertEqual(done["plan"],pending["plan"])
        self.assertEqual(len(self.store.tickets()),1)
        with self.assertRaises(LookupError): answer_confirmation(self.graph,pending["request_id"],True)
        self.assertEqual(len(self.store.tickets()),1)
    def test_cancel_writes_nothing(self):
        pending=run_request(self.graph,self.state("incidencia: Impresora | No imprime"))
        cancelled=answer_confirmation(self.graph,pending["request_id"],False)
        self.assertEqual(cancelled["status"],"cancelled")
        self.assertEqual(self.nodes(cancelled),["planificar","validar","confirmar","responder"])
        self.assertEqual(self.store.tickets(),[])
    def test_only_true_confirms(self):
        pending=run_request(self.graph,self.state("incidencia: Impresora | No imprime"))
        result=answer_confirmation(self.graph,pending["request_id"],"si")
        self.assertEqual(result["status"],"cancelled")
        self.assertEqual(self.store.tickets(),[])
    def test_unknown_request_cannot_be_confirmed(self):
        with self.assertRaises(LookupError): answer_confirmation(self.graph,"no-existe",True)
        self.assertEqual(self.store.tickets(),[])
    def test_requests_wait_independently(self):
        first=run_request(self.graph,self.state("incidencia: Impresora | No imprime","a"))
        second=run_request(self.graph,self.state("incidencia: Proyector | No enciende","b"))
        answer_confirmation(self.graph,second["request_id"],True)
        self.assertEqual([t["title"] for t in self.store.tickets()],["Proyector"])
        answer_confirmation(self.graph,first["request_id"],True)
        self.assertEqual(len(self.store.tickets()),2)
    def test_other_tools_do_not_pause(self):
        done=run_request(self.graph,self.state("calcula 12*3"))
        self.assertEqual(done["result"]["value"],36)
        self.assertEqual(self.nodes(done),["planificar","validar","ejecutar","responder"])
    def test_rejected_request_does_not_pause(self):
        done=run_request(self.graph,self.state("Envia un correo"))
        self.assertEqual(done["status"],"unsupported")
        self.assertEqual(self.nodes(done),["planificar","validar","responder"])
    def test_state_is_json_serializable(self):
        import json
        pending=run_request(self.graph,self.state("incidencia: Impresora | No imprime"))
        json.dumps(pending)
        json.dumps(answer_confirmation(self.graph,pending["request_id"],True))

if __name__=="__main__":
    unittest.main(verbosity=2)
