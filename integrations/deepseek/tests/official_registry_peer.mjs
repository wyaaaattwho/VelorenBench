// Actual official snapshot/rendering plus production plugin, with a Python broker.
import { createRequire } from 'node:module'
import { join } from 'node:path'
import { pathToFileURL } from 'node:url'
const require = createRequire(join(process.env.DSH_RUNTIME, 'package.json'))
const { Context } = await import(require.resolve('@deepseek-ai/cordis'))
const { default: SystemPrompt } = await import(require.resolve('@deepseek-ai/dsh-system-prompt'))
const { ToolRuntime } = await import(require.resolve('@deepseek-ai/dsh-tools'))
const { LocalAttachmentStore } = await import(require.resolve('@deepseek-ai/dsh-attachment-local'))
const plugin = await import(pathToFileURL(process.argv[2]).href)
const ctx = new Context()
try {
  new SystemPrompt(ctx, {})
  new ToolRuntime(ctx, {})
  new LocalAttachmentStore(ctx, { dshHome: process.argv[4] })
  await plugin.apply(ctx, { socket: process.argv[3], sessionId: 'synthetic-registry' })
  const execute = (name) => ctx.tools.execute({
    callId: `actual-${name}`, name, arguments: {}, signal: new AbortController().signal,
  })
  const result = await execute('game_observe')
  const guards = []
  for (const invalid of [-0, NaN, Infinity]) {
    const dispose = ctx.tools.register({
      name: 'invalid-output-guard', description: 'Synthetic official-guard regression',
      parameters: { type: 'object', properties: {}, required: [], additionalProperties: false },
      output: { schema: { type: 'object', additionalProperties: true },
        render: () => [{ type: 'text', text: 'must not render invalid raw numbers' }] },
      execute: () => ({ invalid }),
    })
    guards.push(await execute('invalid-output-guard'))
    dispose()
  }
  console.log(JSON.stringify({ result, guards }))
} finally {
  await ctx.fiber.dispose()
}
