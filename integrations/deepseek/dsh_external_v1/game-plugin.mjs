import net from 'node:net'
import { createInterface } from 'node:readline'
import { randomUUID } from 'node:crypto'
import { once } from 'node:events'
import { createRequire } from 'node:module'
import { join } from 'node:path'
import { assertExactIntegerArguments } from './integer-transport.mjs'
const runtimeRequire = createRequire(join(process.env.DSH_RUNTIME, 'package.json'))
const { default: Schema } = await import(runtimeRequire.resolve('@deepseek-ai/schemastery'))

export const name = 'native-player-tools'
export const inject = ['tools', 'attachments']
export const Config = Schema.object({ socket: Schema.string().required(), sessionId: Schema.string().required() })

export async function apply(ctx, config) {
  const socket = net.createConnection(config.socket)
  await once(socket, 'connect')
  const lines = createInterface({ input: socket })
  const iterator = lines[Symbol.asyncIterator]()
  const first = await iterator.next()
  if (first.done) throw new Error('tool broker closed before discovery')
  const definitions = JSON.parse(first.value).tools
  const pending = new Map()
  const failPending = (error) => {
    for (const { reject } of pending.values()) reject(error)
    pending.clear()
  }
  socket.on('error', failPending)
  socket.on('close', () => failPending(new Error('native tool transport closed; effect unknown')))
  const reader = (async () => {
    for await (const line of { [Symbol.asyncIterator]: () => iterator }) {
      const row = JSON.parse(line)
      const entry = pending.get(row.id)
      if (!entry) throw new Error('unknown/duplicate broker response ID')
      pending.delete(row.id)
      entry.resolve(row)
    }
  })()
  reader.catch(failPending)
  ctx.effect(() => {
    const disposers = definitions.map((definition) => ctx.tools.register({
      ...definition,
      output: {
        schema: { type: 'object', additionalProperties: true },
        render: (_args, value) => value.content,
      },
      isConcurrencySafe: () => true,
      execute: (arguments_, exec) => {
        if (exec.signal.aborted) throw new Error('tool cancelled before native submission')
        assertExactIntegerArguments(definition.parameters, arguments_, definition.name)
        const id = randomUUID()
        return new Promise((resolve, reject) => {
          pending.set(id, { resolve, reject })
          socket.write(JSON.stringify({ id, name: definition.name, arguments: arguments_, context: { session_id: config.sessionId, call_id: exec.callId, harness: 'official-deepseek-harness' } }) + '\n')
        }).then(async (row) => {
          // Common text is authoritative JSON, not an object to parse through JS
          // numbers. This also preserves projected refusal content verbatim.
          if (!row.ok) throw new Error(row.content
            ? row.content.filter(block => block.type === 'text').map(block => block.text).join('\n')
            : JSON.stringify(row))
          const content = []
          const attachments = []
          for (const block of row.content) {
            if (block.type === 'text') content.push(block)
            else if (block.type === 'image') {
              const data = Buffer.from(block.data, 'base64')
              if (data.toString('base64') !== block.data) throw new Error('noncanonical image base64')
              const attachment = await ctx.attachments.saveImage({ data, mediaType: block.mimeType })
              content.push({ type: 'image', attachment })
              attachments.push(attachment)
            } else throw new Error('unsupported public tool content')
          }
          if (attachments.length) socket.write(JSON.stringify({
            kind: 'media_admitted', call_id: exec.callId,
            source_images: row.source_images, attachments,
          }) + '\n')
          return { content }
        })
      },
    }))
    return async () => {
      for (const dispose of disposers) dispose()
      socket.destroy()
      failPending(new Error('runtime retiring; native effect may be unknown'))
      lines.close()
      await reader
    }
  }, 'native-player-tools')
}
