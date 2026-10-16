import asyncio
import json
import math
from pathlib import Path

from scripts.rpg_benchmark.player_types import Invocation as ToolInvocation
from scripts.rpg_benchmark.player import tools_for, content_blocks

from dsh_external_v1.dsh_native_protocol import common, error_record
from dsh_external_v1.dsh_schema import native_arguments, provider_schema


def validate_public_text(text):
    """Validate, but never reserialize, the common model-facing JSON text.

    Signed finite zero and arbitrary JSON integers are valid here. They must
    stay inside text, not become JavaScript numbers in a shadow payload.
    """
    def invalid_constant(value):
        raise ValueError(f"Nonfinite public JSON constant: {value}")

    def finite_float(value):
        number = float(value)
        if not math.isfinite(number):
            raise ValueError("Nonfinite public JSON number")
        return number

    json.loads(text, parse_constant=invalid_constant, parse_float=finite_float)


class PlayerTools:
    """Map genuine dsh call IDs to unchanged common native handlers and results."""

    def __init__(self, native, configuration, *, frames=None, episode):
        self.evidence = native.evidence
        self.player = tools_for(native, self.evidence, configuration, frames=frames, episode=episode)
        self.handlers = {tool.name: tool.handler for tool in self.player.tools}
        self.schemas = {tool.name: tool.parameters for tool in self.player.tools}

    def definitions(self):
        definitions = []
        for tool in self.player.tools:
            parameters, changes = provider_schema(tool.parameters)
            if changes:
                self.evidence.write("dsh_provider_schema_projection", tool_name=tool.name,
                                    changes=changes, native_validation="unchanged",
                                    integer_transport="exact_decimal_or_safe_integer_never_round")
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
        invocation = ToolInvocation(
            session_id=context["session_id"], tool_call_id=context["call_id"],
            tool_name=name, arguments=native_arguments(self.schemas[name], request["arguments"]))
        self.evidence.write("dsh_common_tool_mapping", transport_id=request["id"],
                            dsh_session_id=context["session_id"], dsh_call_id=context["call_id"],
                            tool_name=name, harness="official-deepseek-harness",
                            common_contract=common.INTERFACE_VERSION,
                            **({"wire_arguments": request["arguments"],
                                "native_arguments": invocation.arguments,
                                "argument_schema": self.schemas[name]}
                               if invocation.arguments != request["arguments"] else {}))
        handler = self.handlers[name]
        if handler is None:
            raise RuntimeError("Common tool has no handler")
        result = await handler(invocation)
        validate_public_text(result.text_result_for_llm)
        return {"ok": result.result_type == "success",
                "content": content_blocks(result),
                "source_images": [{"sha256": image.sha256, "byte_length": len(image.data)}
                                  for image in result.images],
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
                    result = {"ok": False, "error": {"code": "public_tool_transport_failed"},
                              "effect_status": "unknown", "retry_policy": "Never automatically resubmit."}
                    self.tools.evidence.write("dsh_mapping_error", error=error_record(error))
                writer.write((json.dumps({"id": request["id"], **result}, allow_nan=False) + "\n").encode())
                await writer.drain()

            async with asyncio.TaskGroup() as group:
                while line := await reader.readline():
                    row = json.loads(line)
                    if row.get("kind") == "media_admitted":
                        self.tools.evidence.write("dsh_media_admitted", **{
                            key: row[key] for key in ("call_id", "source_images", "attachments")})
                    else:
                        group.create_task(dispatch(row))
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
