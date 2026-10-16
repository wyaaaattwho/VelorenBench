import asyncio
import json
from pathlib import Path

from dsh_external_v1.dsh_native_protocol import common, error_record
from dsh_external_v1.dsh_schema import provider_schema


class PlayerTools:
    """Map genuine dsh call IDs to unchanged common native handlers and results."""

    def __init__(self, native):
        self.evidence = native.evidence
        self.player = common.GameTools(native, self.evidence)
        self.handlers = {tool.name: tool.handler for tool in self.player.tools}

    def definitions(self):
        definitions = []
        for tool in self.player.tools:
            parameters, changes = provider_schema(tool.parameters)
            if changes:
                self.evidence.write("dsh_provider_schema_projection", tool_name=tool.name,
                                    changes=changes, native_validation="unchanged",
                                    integer_transport="refuse_unsafe_never_round")
            definitions.append({"name": tool.name, "description": tool.description,
                                "parameters": parameters})
        return definitions

    async def execute(self, request):
        context = request["context"]
        if (context["harness"] != "official-deepseek-harness"
                or not isinstance(context["session_id"], str) or not context["session_id"]
                or not isinstance(context["call_id"], str) or not context["call_id"]):
            raise ValueError("Missing genuine dsh execution identity")
        name = request["name"]
        if name not in self.handlers:
            raise ValueError("Only unchanged common native player tools are available")
        invocation = common.ToolInvocation(
            session_id=context["session_id"], tool_call_id=context["call_id"],
            tool_name=name, arguments=request["arguments"])
        self.evidence.write("dsh_common_tool_mapping", transport_id=request["id"],
                            dsh_session_id=context["session_id"], dsh_call_id=context["call_id"],
                            harness="official-deepseek-harness", common_contract=common.INTERFACE_VERSION)
        handler = self.handlers[name]
        if handler is None:
            raise RuntimeError("Common tool has no handler")
        result = await handler(invocation)
        payload = json.loads(result.text_result_for_llm)
        return {"ok": result.result_type == "success", "result": payload,
                **({"error": result.error} if result.result_type != "success" else {})}


class ToolBroker:
    def __init__(self, tools, path: Path):
        self.tools, self.path = tools, path
        self.server = None
        self.attached = False
        self.handlers = set()
        self.writers = set()

    async def start(self):
        self.server = await asyncio.start_unix_server(
            self.connection, path=str(self.path), limit=16 * 1024 * 1024)
        self.path.chmod(0o600)

    async def connection(self, reader, writer):
        self.writers.add(writer)
        self.handlers.add(asyncio.current_task())
        try:
            if self.attached:
                raise RuntimeError("Dsh tool broker already has an owner")
            self.attached = True
            writer.write((json.dumps({"tools": self.tools.definitions()}) + "\n").encode())
            await writer.drain()

            async def dispatch(request):
                try:
                    result = await self.tools.execute(request)
                except (ValueError, RuntimeError, TypeError, KeyError, OSError) as error:
                    result = {"ok": False, "error": error_record(error),
                              "effect_status": "unknown", "retry_policy": "Never automatically resubmit."}
                    self.tools.evidence.write("dsh_mapping_error", error=result)
                writer.write((json.dumps({"id": request["id"], **result}, allow_nan=False) + "\n").encode())
                await writer.drain()

            async with asyncio.TaskGroup() as group:
                while line := await reader.readline():
                    group.create_task(dispatch(json.loads(line)))
        except (RuntimeError, ValueError, OSError, ExceptionGroup) as error:
            self.tools.evidence.write("broker_error", error=error_record(error))
        finally:
            writer.close()
            await writer.wait_closed()
            self.writers.discard(writer)
            self.handlers.discard(asyncio.current_task())

    async def close(self):
        if self.server is not None:
            self.server.close()
            await self.server.wait_closed()
        for writer in tuple(self.writers):
            writer.close()
        for task in tuple(self.handlers):
            task.cancel()
        await asyncio.gather(*self.handlers, return_exceptions=True)
        self.path.unlink(missing_ok=True)
